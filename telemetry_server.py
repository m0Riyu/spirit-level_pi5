"""Non-blocking WebSocket telemetry and local dashboard HTTP server."""

import asyncio
import json
import logging
import math
import re
import shutil
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dataclasses import dataclass

from manual_capture import parse_capture_options, valid_request_id
from stability import finite_number

PIN_HEADER = "X-Levelsvc-Pin"


@dataclass
class FileDownload:
    """Route result streamed as a file attachment (deleted after sending)."""
    path: Path
    filename: str
    content_type: str = "application/zip"
    delete: bool = True


def build_telemetry_payload(
    frame_id,
    detection,
    measurement,
    timings,
    *,
    mm_per_m_per_div,
    level_tolerance_mm_per_m,
    max_measurable_slope_mm_per_m,
    zero_offset_div=0.0,
    calibration=None,
):
    """Build the versioned JSON message sent to dashboard clients.

    offset_div keeps its meaning (geometric divisions from the scale center);
    slope applies the vial calibration: (offset_div - zero) * mm_per_m_per_div.
    """
    valid = bool(detection.detected and measurement.valid and all(
        finite_number(value) is not None for value in (
            measurement.offset_px, measurement.offset_div, measurement.center_x_roi,
            measurement.scale_center_x_roi, measurement.pitch_px_per_div, detection.confidence,
        )
    ) and finite_number(measurement.pitch_px_per_div) > 0)
    offset_px = float(measurement.offset_px) if valid else None

    if valid:
        pixels_per_div = float(measurement.pitch_px_per_div)
        pixels_per_1_mmm = pixels_per_div / float(mm_per_m_per_div)
        # Rectification makes spacing nonlinear; calibrated divisions already
        # account for that geometry and are shared with the CSV/preview.
        slope_mm_per_m = (float(measurement.offset_div) - float(zero_offset_div)) * float(mm_per_m_per_div)
        angle_degrees = math.degrees(math.atan(slope_mm_per_m / 1000.0))
        absolute_slope = abs(slope_mm_per_m)
        maximum_slope = float(max_measurable_slope_mm_per_m)
        within_official_range = (
            absolute_slope <= maximum_slope
            or math.isclose(absolute_slope, maximum_slope, rel_tol=0, abs_tol=1e-12)
        )
        if not within_official_range:
            system_state = "OUT_OF_RANGE"
        elif (
            absolute_slope <= float(level_tolerance_mm_per_m)
            or math.isclose(absolute_slope, float(level_tolerance_mm_per_m),
                            rel_tol=0, abs_tol=1e-12)
        ):
            system_state = "LEVEL"
        else:
            system_state = "ADJUST"
    else:
        pixels_per_div = None
        pixels_per_1_mmm = None
        slope_mm_per_m = None
        angle_degrees = None
        within_official_range = False
        system_state = "SEARCHING" if not detection.detected else "ERROR"

    payload = {
        "type": "telemetry",
        "schema_version": 1,
        "frame_id": int(frame_id),
        "sent_at_epoch_ms": time.time() * 1000.0,
        "system_state": system_state,
        "detected": bool(detection.detected),
        "detection_count": int(detection.detection_count),
        "confidence": finite_number(detection.confidence) if detection.detected else None,
        "measurement": {
            "valid": valid,
            "within_official_range": within_official_range,
            "error": measurement.error or ("nonfinite_measurement" if measurement.valid and not valid else ""),
            "bubble_center_x_roi": (
                float(measurement.center_x_roi) if valid else None
            ),
            "scale_center_x_roi": (
                finite_number(measurement.scale_center_x_roi)
                if measurement.scale_center_x_roi not in (None, "")
                else None
            ),
            "pitch_px_per_div": (
                finite_number(measurement.pitch_px_per_div)
                if measurement.pitch_px_per_div not in (None, "")
                else None
            ),
            "pixels_per_1_mmm": pixels_per_1_mmm,
            "offset_px": offset_px,
            "offset_div": float(measurement.offset_div) if valid else None,
            # Divisions from the vial's true level point (zero offset removed);
            # the dashboard bubble uses this so it matches the displayed slope.
            "level_offset_div": float(measurement.offset_div) - float(zero_offset_div) if valid else None,
            "direction": measurement.direction if valid else "",
            "slope_mm_per_m": slope_mm_per_m,
            "angle_degrees": angle_degrees,
        },
        "performance": {
            "capture_ms": float(timings["capture_ms"]),
            "predict_ms": float(timings["predict_ms"]),
            "inference_ms": float(timings["yolo_inference_ms"]),
            "processing_ms": float(timings["process_ms"]),
            "fps": float(timings["fps"]),
        },
    }
    if calibration is not None:
        payload["calibration"] = dict(calibration)
    return payload


class _NoCacheRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, capture_manager=None, state_provider=None, mode_handler=None,
                 routes=(), preview=None, pin_guard=None, **kwargs):
        self.pin_guard = pin_guard
        self.capture_manager = capture_manager
        self.state_provider = state_provider
        self.mode_handler = mode_handler
        self.routes = routes
        self.preview = preview
        super().__init__(*args, **kwargs)

    def _route(self, method):
        """Generic API. Routes are (method, regex, handler, needs_pin); handler(params,
        body) returns (status, json | FileDownload[, after_response])."""
        path = self.path.split("?", 1)[0]
        for route_method, pattern, handler, needs_pin in self.routes:
            match = pattern.fullmatch(path) if route_method == method else None
            if match is None:
                continue
            body = {}
            if method == "POST":
                body = self._read_json_object(4096, allow_empty=True)
                if body is None:
                    return True
            if needs_pin:
                refused = (503, {"status": "error", "error_code": "PIN_NOT_CONFIGURED", "message": "PIN 未設定"}) \
                    if self.pin_guard is None else self.pin_guard.check(self.headers.get(PIN_HEADER))
                if refused is not None:
                    self._json(*refused)
                    return True
            after = None
            try:
                result = handler(match.groupdict(), body)
                status, response = result[:2]
                after = result[2] if len(result) > 2 else None
            except ValueError as error:
                status, response = 400, {"status": "error", "error_code": "INVALID_REQUEST", "message": str(error)}
            except FileNotFoundError as error:
                status, response = 404, {"status": "error", "error_code": "NOT_FOUND", "message": str(error)}
            except Exception:
                logging.getLogger(__name__).exception("API %s %s failed", method, path)
                status, response = 500, {"status": "error", "error_code": "INTERNAL_ERROR", "message": "server error"}
            if isinstance(response, FileDownload):
                self._send_file(response)
            else:
                self._json(status, response)
            if after is not None:
                after()
            return True
        return False

    def _send_file(self, download):
        try:
            size = download.path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", download.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{download.filename}"')
            self.end_headers()
            with download.path.open("rb") as file:
                shutil.copyfileobj(file, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if download.delete:
                download.path.unlink(missing_ok=True)

    def _stream_preview(self):
        if self.preview is None or not self.preview.source:
            self._json(409, {"status": "error", "error_code": "PREVIEW_UNAVAILABLE",
                             "message": "預覽只在相機對位與刻度檢查模式提供"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            for jpeg in self.preview.frames():
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_object(self, maximum_bytes, allow_empty=False):
        """Same-origin JSON object body, or None after an error response."""
        # Same-origin browser API. A foreign Origin cannot enqueue work.
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != f"http://{self.headers.get('Host')}":
            self._json(403, {"status": "error", "error_code": "INVALID_ORIGIN", "message": "same-origin request required"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if allow_empty and length == 0:
                return {}
            if not 0 < length <= maximum_bytes:
                raise ValueError(f"JSON body must be between 1 and {maximum_bytes} bytes")
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload
        except (ValueError, UnicodeError, OSError) as error:
            self._json(400, {"status": "error", "error_code": "INVALID_JSON", "message": str(error)})
            return None

    def do_POST(self):
        if self._route("POST"):
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/mode":
            self._post_mode()
            return
        if path != "/api/captures":
            self._json(404, {"status": "error", "error_code": "NOT_FOUND", "message": "API not found"})
            return
        payload = self._read_json_object(2048)
        if payload is None:
            return
        if "request_id" not in payload:
            self._json(400, {"status": "error", "error_code": "INVALID_JSON", "message": "request_id is required"})
            return
        if not valid_request_id(payload["request_id"]):
            self._json(400, {"status": "error", "error_code": "INVALID_REQUEST_ID", "message": "request_id must be a UUID"})
            return
        try:
            options = parse_capture_options(payload)
        except ValueError as error:
            self._json(400, {"status": "error", "error_code": "INVALID_CAPTURE_OPTIONS", "message": str(error)})
            return
        if self.capture_manager is None:
            self._json(503, {"status": "error", "error_code": "SERVER_NOT_READY", "message": "capture service unavailable"})
            return
        try:
            status, response = self.capture_manager.submit(payload["request_id"], options)
            self._json(status, response)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Capture API submit failed")
            self._json(503, {"status": "error", "error_code": "STATUS_STORE_UNAVAILABLE", "message": "capture status storage unavailable"})

    def _post_mode(self):
        payload = self._read_json_object(1024)
        if payload is None:
            return
        if self.mode_handler is None:
            self._json(503, {"status": "error", "error_code": "SERVER_NOT_READY", "message": "mode control unavailable"})
            return
        if set(payload) - {"mode", "client_id"} or not isinstance(payload.get("client_id", ""), str):
            self._json(400, {"status": "error", "error_code": "INVALID_JSON", "message": "send mode and optional client_id"})
            return
        try:
            self._json(200, self.mode_handler(payload.get("mode"), payload.get("client_id", "")))
        except ValueError as error:
            self._json(400, {"status": "error", "error_code": "INVALID_MODE", "message": str(error)})

    def do_GET(self):
        if self._route("GET"):
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/preview.mjpg":
            self._stream_preview()
            return
        if path == "/api/state":
            if self.state_provider is None:
                self._json(503, {"status": "error", "error_code": "SERVER_NOT_READY", "message": "state unavailable"})
            else:
                self._json(200, self.state_provider())
            return
        if path == "/api/captures/ready":
            response = {"ready": self.capture_manager is not None and self.capture_manager.ready,
                        "require_stable_for_capture": bool(self.capture_manager and self.capture_manager.require_stable)}
            if self.capture_manager is not None:
                response["burst_frames_default"] = self.capture_manager.default_burst_frames
                response["storage"] = self.capture_manager.storage_estimate()
            self._json(200, response)
            return
        if path.startswith("/api/captures/"):
            request_id = path[len("/api/captures/"):]
            if not valid_request_id(request_id):
                self._json(400, {"status": "error", "error_code": "INVALID_REQUEST_ID", "message": "request_id must be a UUID"})
            elif self.capture_manager is None:
                self._json(503, {"status": "error", "error_code": "SERVER_NOT_READY", "message": "capture service unavailable"})
            else:
                try:
                    response = self.capture_manager.get_status(request_id)
                    if response is None:
                        self._json(404, {"status": "error", "request_id": request_id,
                                         "error_code": "REQUEST_NOT_FOUND", "message": "request not found"})
                    else:
                        self._json(200, response)
                except Exception:
                    self._json(503, {"status": "error", "error_code": "STATUS_STORE_UNAVAILABLE", "message": "capture status storage unavailable"})
            return
        if self.path.split("?", 1)[0] == "/time":
            body = json.dumps(
                {"server_time_epoch_ms": time.time() * 1000.0},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format_string, *args):
        # Keep the real-time inference terminal readable.
        return


class TelemetryServer:
    """Serve a dashboard and broadcast only the newest telemetry frame."""

    def __init__(
        self,
        *,
        websocket_host,
        websocket_port,
        dashboard_host,
        dashboard_port,
        dashboard_directory,
        capture_manager=None,
        state_provider=None,
        mode_handler=None,
        routes=(),
        preview=None,
        pin_guard=None,
    ):
        self.websocket_host = websocket_host
        self.websocket_port = int(websocket_port)
        self.dashboard_host = dashboard_host
        self.dashboard_port = int(dashboard_port)
        self.dashboard_directory = Path(dashboard_directory)
        self.capture_manager = capture_manager
        self.state_provider = state_provider
        self.mode_handler = mode_handler
        self.routes = [(route[0], re.compile(route[1]), route[2], bool(route[3]) if len(route) > 3 else False)
                       for route in routes]
        self.preview = preview
        self.pin_guard = pin_guard

        self._clients = set()
        self._loop = None
        self._stop_future = None
        self._latest = {}  # message type -> newest JSON text (event loop only)
        self._wakeup = None
        self._websocket_thread = None
        self._http_thread = None
        self._http_server = None
        self._ready = threading.Event()
        self._startup_error = None

    def start(self):
        if not self.dashboard_directory.is_dir():
            raise FileNotFoundError(
                f"dashboard directory not found: {self.dashboard_directory}"
            )

        handler = partial(
            _NoCacheRequestHandler,
            directory=str(self.dashboard_directory),
            capture_manager=self.capture_manager,
            state_provider=self.state_provider,
            mode_handler=self.mode_handler,
            routes=self.routes,
            preview=self.preview,
            pin_guard=self.pin_guard,
        )
        self._http_server = ThreadingHTTPServer(
            (self.dashboard_host, self.dashboard_port), handler
        )
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="dashboard-http",
            daemon=True,
        )
        self._http_thread.start()

        self._websocket_thread = threading.Thread(
            target=self._run_websocket_thread,
            name="telemetry-websocket",
            daemon=True,
        )
        self._websocket_thread.start()
        if not self._ready.wait(timeout=5.0):
            self.stop()
            raise RuntimeError("WebSocket server startup timed out")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(f"WebSocket server failed to start: {error}")

    def _run_websocket_thread(self):
        try:
            asyncio.run(self._serve_websocket())
        except Exception as error:
            self._startup_error = error
            self._ready.set()

    async def _serve_websocket(self):
        try:
            from websockets.asyncio.server import serve
        except ImportError as error:
            raise RuntimeError(
                "missing dependency; run: python3 -m pip install -r requirements.txt"
            ) from error

        self._loop = asyncio.get_running_loop()
        self._stop_future = self._loop.create_future()
        self._wakeup = asyncio.Event()

        async with serve(
            self._handle_client,
            self.websocket_host,
            self.websocket_port,
            ping_interval=20,
            ping_timeout=20,
        ):
            broadcaster = asyncio.create_task(self._broadcast_loop())
            self._ready.set()
            await self._stop_future
            broadcaster.cancel()
            await asyncio.gather(broadcaster, return_exceptions=True)
            clients = list(self._clients)
            if clients:
                await asyncio.gather(
                    *(client.close(code=1001) for client in clients),
                    return_exceptions=True,
                )

    async def _handle_client(self, websocket):
        self._clients.add(websocket)
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "schema_version": 1,
                        "server_time_epoch_ms": time.time() * 1000.0,
                    },
                    separators=(",", ":"),
                )
            )
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    continue
                if message.get("type") == "ping":
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "pong",
                                "client_sent_epoch_ms": message.get(
                                    "client_sent_epoch_ms"
                                ),
                                "server_time_epoch_ms": time.time() * 1000.0,
                            },
                            separators=(",", ":"),
                        )
                    )
        finally:
            self._clients.discard(websocket)

    async def _broadcast_loop(self):
        # Only the newest message of each type is sent: a slow phone skips
        # stale telemetry, but a "state" change is never displaced by it.
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            messages, self._latest = list(self._latest.values()), {}
            clients = list(self._clients)
            for message in messages if clients else ():
                results = await asyncio.gather(
                    *(client.send(message) for client in clients),
                    return_exceptions=True,
                )
                for client, result in zip(clients, results):
                    if isinstance(result, Exception):
                        self._clients.discard(client)

    def publish(self, payload):
        if self._loop is None or self._wakeup is None:
            return
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._loop.call_soon_threadsafe(self._enqueue_latest, payload.get("type", "telemetry"), message)

    def _enqueue_latest(self, message_type, message):
        self._latest[message_type] = message
        self._wakeup.set()

    def stop(self):
        if self._loop is not None and self._stop_future is not None:
            def request_stop():
                if not self._stop_future.done():
                    self._stop_future.set_result(None)

            self._loop.call_soon_threadsafe(request_stop)
        if self._websocket_thread is not None:
            self._websocket_thread.join(timeout=5.0)
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5.0)

    def dashboard_urls(self):
        addresses = {"127.0.0.1"}
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
        return [
            f"http://{address}:{self.dashboard_port}"
            for address in sorted(addresses, key=lambda value: value.startswith("127."))
        ]
