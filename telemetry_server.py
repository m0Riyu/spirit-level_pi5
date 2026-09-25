"""Non-blocking WebSocket telemetry and local dashboard HTTP server."""

import asyncio
import json
import math
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def build_telemetry_payload(
    frame_id,
    detection,
    measurement,
    timings,
    *,
    mm_per_m_per_div,
    level_tolerance_mm_per_m,
):
    """Build the versioned JSON message sent to dashboard clients."""
    valid = bool(detection.detected and measurement.valid)
    offset_px = float(measurement.offset_px) if valid else None

    if valid:
        pixels_per_div = float(measurement.pitch_px_per_div)
        pixels_per_1_mmm = pixels_per_div / float(mm_per_m_per_div)
        slope_mm_per_m = offset_px / pixels_per_1_mmm
        angle_degrees = math.degrees(math.atan(slope_mm_per_m / 1000.0))
        system_state = (
            "LEVEL"
            if abs(slope_mm_per_m) <= float(level_tolerance_mm_per_m)
            else "ADJUST"
        )
    else:
        pixels_per_div = None
        pixels_per_1_mmm = None
        slope_mm_per_m = None
        angle_degrees = None
        system_state = "SEARCHING" if not detection.detected else "ERROR"

    return {
        "type": "telemetry",
        "schema_version": 1,
        "frame_id": int(frame_id),
        "sent_at_epoch_ms": time.time() * 1000.0,
        "system_state": system_state,
        "detected": bool(detection.detected),
        "detection_count": int(detection.detection_count),
        "confidence": float(detection.confidence) if detection.detected else None,
        "measurement": {
            "valid": valid,
            "error": measurement.error,
            "bubble_center_x_roi": (
                float(measurement.center_x_roi) if valid else None
            ),
            "scale_center_x_roi": (
                float(measurement.scale_center_x_roi)
                if measurement.scale_center_x_roi not in (None, "")
                else None
            ),
            "pitch_px_per_div": (
                float(measurement.pitch_px_per_div)
                if measurement.pitch_px_per_div not in (None, "")
                else None
            ),
            "pixels_per_1_mmm": pixels_per_1_mmm,
            "offset_px": offset_px,
            "offset_div": float(measurement.offset_div) if valid else None,
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


class _NoCacheRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
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
    ):
        self.websocket_host = websocket_host
        self.websocket_port = int(websocket_port)
        self.dashboard_host = dashboard_host
        self.dashboard_port = int(dashboard_port)
        self.dashboard_directory = Path(dashboard_directory)

        self._clients = set()
        self._loop = None
        self._stop_future = None
        self._queue = None
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
        self._queue = asyncio.Queue(maxsize=1)

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
        while True:
            message = await self._queue.get()
            clients = list(self._clients)
            if not clients:
                continue
            results = await asyncio.gather(
                *(client.send(message) for client in clients),
                return_exceptions=True,
            )
            for client, result in zip(clients, results):
                if isinstance(result, Exception):
                    self._clients.discard(client)

    def publish(self, payload):
        if self._loop is None or self._queue is None:
            return
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._loop.call_soon_threadsafe(self._enqueue_latest, message)

    def _enqueue_latest(self, message):
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(message)

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
