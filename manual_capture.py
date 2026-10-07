"""Idempotent manual captures: HTTP queues IDs, inference freezes, worker writes.

Only bounded active requests and writer jobs are held in RAM. Terminal statuses
are persisted in SQLite, so retries also remain idempotent after a Pi restart.
No camera calls or Ultralytics results are used in this module.
"""

import csv
import errno
import io
import json
import logging
import math
import os
import platform
import queue
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

import config
from display import annotate_capture_roi
from stability import finite_number

LOG = logging.getLogger(__name__)
LOG_TIMEZONE = ZoneInfo("Asia/Taipei")
PI_LOG_FIELDS = (
    "session_id sample_id record_id request_id frame_id "
    "request_received_at_iso request_received_at_epoch_ms "
    "frame_started_at_iso frame_started_at_epoch_ms "
    "capture_completed_at_iso capture_completed_at_epoch_ms "
    "prediction_completed_at_iso prediction_completed_at_epoch_ms "
    "saved_at_iso saved_at_epoch_ms "
    "detected detection_count class_id class_name confidence "
    "x1_roi y1_roi x2_roi y2_roi center_x_roi center_y_roi box_width box_height "
    "measurement_valid measurement_error bubble_center_x_roi scale_center_x_roi "
    "pitch_px_per_div bubble_offset_px bubble_offset_div bubble_absolute_offset_div "
    "bubble_direction slope_mm_per_m angle_degrees within_official_range system_state "
    "stability_state stability_stable stability_reason stability_sample_count "
    "stability_valid_count stability_valid_ratio stability_mean_slope_mm_per_m "
    "stability_std_slope_mm_per_m stability_range_slope_mm_per_m stability_duration_seconds "
    "capture_ms predict_ms yolo_preprocess_ms yolo_inference_ms yolo_postprocess_ms "
    "process_ms fps annotation_ms image_write_ms csv_write_ms queue_wait_ms "
    "request_to_frame_ms request_to_saved_ms "
    "cpu_temperature_c load_average_1m disk_free_mb "
    "clean_image_path annotated_image_path clean_image_width clean_image_height "
    "annotated_image_width annotated_image_height "
    # V2: burst rows, field reference values, raw frame and calibration versions.
    "row_type burst_id burst_index burst_size "
    "reference_deg a_axis_deg sweep_direction note "
    "raw_image_path raw_frame_id "
    "geometry_version geometry_pending_confirmation vial_version alignment_version "
    "mm_per_m_per_div zero_offset_div "
    "burst_valid_count burst_median_slope_mm_per_m burst_std_slope_mm_per_m "
    "burst_median_angle_degrees burst_std_angle_degrees "
    "burst_median_offset_div burst_std_offset_div "
    "burst_median_center_x_roi burst_std_center_x_roi "
    "tick_center_x_px tick_center_offset_px"
).split()
CAPTURE_OPTION_FIELDS = ("reference_deg", "a_axis_deg", "sweep_direction", "note", "burst_frames")
SWEEP_DIRECTIONS = ("forward", "backward", "zero_check")
CALIBRATION_ROW_FIELDS = ("geometry_version", "geometry_pending_confirmation", "vial_version",
                          "alignment_version", "mm_per_m_per_div", "zero_offset_div")
BURST_SUMMARY_FIELDS = (("slope_mm_per_m", "slope_mm_per_m"), ("angle_degrees", "angle_degrees"),
                        ("offset_div", "bubble_offset_div"), ("center_x_roi", "center_x_roi"))
TERMINAL_STATES = {"saved", "rejected", "error"}
SAVED_RESPONSE_FIELDS = ("status", "request_id", "session_id", "sample_id",
                         "record_id", "frame_id", "saved_at_epoch_ms")


def wall_time(epoch_ms=None):
    epoch_ms = time.time() * 1000 if epoch_ms is None else epoch_ms
    iso = datetime.fromtimestamp(epoch_ms / 1000, LOG_TIMEZONE).isoformat(timespec="milliseconds")
    return iso, epoch_ms


def valid_request_id(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower() and len(value) == 36
    except (ValueError, AttributeError):
        return False


def parse_capture_options(payload):
    """Validate the optional field-test values sent with one trigger."""
    unknown = set(payload) - {"request_id", *CAPTURE_OPTION_FIELDS}
    if unknown:
        raise ValueError("unknown fields: " + ", ".join(sorted(unknown)))
    options = {}
    for name in ("reference_deg", "a_axis_deg"):
        value = payload.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 90:
            raise ValueError(f"{name} must be a finite angle in degrees")
        options[name] = float(value)
    direction = payload.get("sweep_direction")
    if direction not in (None, ""):
        if direction not in SWEEP_DIRECTIONS:
            raise ValueError("sweep_direction must be forward, backward or zero_check")
        options["sweep_direction"] = direction
    note = payload.get("note")
    if note not in (None, ""):
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("note must be text of at most 200 characters")
        options["note"] = " ".join(note.split())
    frames = payload.get("burst_frames")
    if frames is not None:
        if (isinstance(frames, bool) or not isinstance(frames, int)
                or not 1 <= frames <= config.CAPTURE_BURST_MAX_FRAMES):
            raise ValueError(f"burst_frames must be an integer from 1 to {config.CAPTURE_BURST_MAX_FRAMES}")
        options["burst_frames"] = frames
    return options


def burst_summary(frame_rows):
    """Median and population standard deviation over the burst's valid frames."""
    valid = [row for row in frame_rows if row.get("measurement_valid") is True
             and finite_number(row.get("slope_mm_per_m")) is not None]
    summary = {"burst_valid_count": len(valid)}
    for name, source in BURST_SUMMARY_FIELDS:
        values = np.array([float(row[source]) for row in valid if finite_number(row.get(source)) is not None])
        summary[f"burst_median_{name}"] = float(np.median(values)) if values.size else ""
        summary[f"burst_std_{name}"] = float(np.std(values)) if values.size else ""
    return summary


def git_value(*arguments):
    try:
        return subprocess.run(["git", *arguments], cwd=config.APP_DIRECTORY,
                              check=True, capture_output=True, text=True, timeout=2).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def session_metadata(session_id, calibration=None):
    calibration = dict(calibration or {})
    return {
        "schema_version": 1, "session_id": session_id, "started_at_iso": wall_time()[0],
        "log_timezone": "Asia/Taipei",
        "hostname": socket.gethostname(), "git_commit": git_value("rev-parse", "HEAD"),
        "git_branch": git_value("branch", "--show-current"), "python_version": platform.python_version(),
        "model_path": str(config.MODEL_PATH), "model_image_size": list(config.MODEL_IMAGE_SIZE),
        "camera_frame_width": config.FRAME_WIDTH, "camera_frame_height": config.FRAME_HEIGHT,
        "roi_width": config.ROI_WIDTH, "roi_height": config.ROI_HEIGHT,
        "roi_x1": config.ROI_X1, "roi_y1": config.ROI_Y1,
        "roi_x2": config.ROI_X2, "roi_y2": config.ROI_Y2,
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "calibration_source": calibration.get("geometry_source", ""),
        "camera_calibration_source": str(config.CAMERA_CALIBRATION_NPZ),
        "mm_per_m_per_div": calibration.get("mm_per_m_per_div"),
        "zero_offset_div": calibration.get("zero_offset_div"),
        "calibration": calibration,
        "level_tolerance_mm_per_m": config.LEVEL_TOLERANCE_MM_PER_M,
        "max_measurable_slope_mm_per_m": config.MAX_MEASURABLE_SLOPE_MM_PER_M,
        "stability_window_size": config.STABILITY_WINDOW_SIZE,
        "stability_min_valid_ratio": config.STABILITY_MIN_VALID_RATIO,
        "stability_max_std_mm_per_m": config.STABILITY_MAX_STD_MM_PER_M,
        "stability_max_range_mm_per_m": config.STABILITY_MAX_RANGE_MM_PER_M,
        "stability_hold_seconds": config.STABILITY_HOLD_SECONDS,
        "require_stable_for_capture": config.REQUIRE_STABLE_FOR_CAPTURE,
        "continuous_csv_enabled": config.ENABLE_CONTINUOUS_CSV,
        "image_stream_enabled": config.ENABLE_IMAGE_STREAM,
        "websocket_image_stream_enabled": False, "jpeg_quality": config.JPEG_QUALITY,
        "capture_burst_frames_default": config.CAPTURE_BURST_FRAMES,
        "save_raw_frame_png": config.SAVE_RAW_FRAME_PNG, "raw_png_compression": config.RAW_PNG_COMPRESSION,
    }


@dataclass
class CaptureRequest:
    request_id: str
    received_monotonic: float
    received_iso: str
    received_epoch_ms: float
    accepted_monotonic: float = 0.
    options: dict = field(default_factory=dict)


@dataclass
class Burst:
    """Consecutive frames collected for one trigger before a single writer job."""
    request: CaptureRequest
    sample_id: int
    record_id: str
    size: int
    frames: list = field(default_factory=list)  # (clean ROI copy, CSV row)
    raw_frame: object = None
    raw_frame_id: object = ""


@dataclass
class CaptureJob:
    request: CaptureRequest
    frames: list
    raw_frame: object = None
    summary_row: dict = None


class CaptureFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class CaptureManager:
    def __init__(self, root=None, *, request_queue_size=None, writer_queue_size=None,
                 max_active=None, require_stable=None, default_burst_frames=None, calibration=None):
        self.root = Path(root) if root is not None else config.LOG_DIRECTORY / "manual_captures"
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = datetime.now(LOG_TIMEZONE).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.session_directory = self.root / self.session_id
        self.images_directory = self.session_directory / "images"
        self.images_directory.mkdir(parents=True, exist_ok=False)
        self.csv_path = self.session_directory / "pi_capture_log.csv"
        self._lock = threading.RLock()
        self._active = {}  # Bounded by max_active, never a history of requests.
        self._ready = False
        self._closed = False
        self._sample_id = 0
        self._bursts = {}  # request_id -> Burst still collecting frames.
        self._saved_bytes = 0
        self._saved_captures = 0
        self.default_burst_frames = int(default_burst_frames or config.CAPTURE_BURST_FRAMES)
        self.max_active = max_active if max_active is not None else config.CAPTURE_MAX_ACTIVE_REQUESTS
        self.require_stable = require_stable if require_stable is not None else config.REQUIRE_STABLE_FOR_CAPTURE
        self.requests = queue.Queue(maxsize=request_queue_size if request_queue_size is not None else config.CAPTURE_REQUEST_QUEUE_SIZE)
        self.jobs = queue.Queue(maxsize=writer_queue_size if writer_queue_size is not None else config.CAPTURE_WRITER_QUEUE_SIZE)
        if min(self.requests.maxsize, self.jobs.maxsize, self.max_active) < 1:
            raise ValueError("capture queues must be bounded and positive")
        self._database = sqlite3.connect(self.root / "capture_requests.sqlite3", check_same_thread=False)
        self._database.execute("PRAGMA synchronous=FULL")
        self._database.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, session_id TEXT, response TEXT)")
        self._recover_interrupted()
        metadata = session_metadata(self.session_id, calibration)
        metadata_path = self.session_directory / "session_metadata.json"
        with metadata_path.open("x", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        with self.csv_path.open("x", newline="", encoding="utf-8") as file:
            csv.DictWriter(file, PI_LOG_FIELDS).writeheader()
            file.flush()
            os.fsync(file.fileno())
        self._worker = threading.Thread(target=self._writer_loop, name="capture-writer", daemon=False)
        self._worker.start()

    def _recover_interrupted(self):
        # Stream statuses/CSV rather than loading an unbounded request history.
        for request_id, session_id, raw in self._database.execute("SELECT request_id, session_id, response FROM requests"):
            if json.loads(raw)["status"] in TERMINAL_STATES:
                continue
            response = {"status": "error", "request_id": request_id,
                        "error_code": "SERVER_RESTARTED", "message": "Pi restarted before capture completed"}
            old_directory = self.root / session_id
            try:
                with (old_directory / "pi_capture_log.csv").open(newline="", encoding="utf-8") as file:
                    for row in csv.DictReader(file):
                        if row["request_id"] == request_id:
                            if all((old_directory / row[name]).is_file() for name in ("clean_image_path", "annotated_image_path")):
                                response = {name: row[name] for name in SAVED_RESPONSE_FIELDS if name != "status"}
                                response.update(status="saved", sample_id=int(row["sample_id"]), frame_id=int(row["frame_id"]),
                                                saved_at_epoch_ms=float(row["saved_at_epoch_ms"]))
                            break
            except (OSError, KeyError, ValueError):
                pass
            self._persist(response, session_id)

    def _persist(self, response, session_id=None):
        self._database.execute("INSERT OR REPLACE INTO requests VALUES (?, ?, ?)",
                               (response["request_id"], session_id or self.session_id,
                                json.dumps(response, allow_nan=False)))
        self._database.commit()

    @property
    def ready(self):
        with self._lock:
            return self._ready and not self._closed and self._worker.is_alive()

    def set_ready(self, ready=True):
        with self._lock:
            self._ready = ready

    def get_status(self, request_id):
        request_id = request_id.lower()
        with self._lock:
            if request_id in self._active:
                return self._active[request_id].copy()
            row = self._database.execute("SELECT response FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def submit(self, request_id, options=None):
        if not valid_request_id(request_id):
            return 400, {"status": "error", "error_code": "INVALID_REQUEST_ID", "message": "request_id must be a UUID"}
        request_id = request_id.lower()
        with self._lock:
            previous = self.get_status(request_id)
            if previous is not None:
                return (202 if previous["status"] == "pending" else 200), previous
            if not self.ready:
                return 503, {"status": "error", "request_id": request_id,
                             "error_code": "SERVER_NOT_READY", "message": "camera/capture service is not ready"}
            # A disk outage may have left a few terminal statuses in bounded
            # RAM. Persist them from the HTTP thread after storage recovers,
            # allowing later requests without losing idempotency.
            for previous_id, response in list(self._active.items()):
                if response["status"] in TERMINAL_STATES:
                    self._persist(response)
                    self._active.pop(previous_id, None)
            if self.requests.full():
                return 429, {"status": "error", "request_id": request_id,
                             "error_code": "QUEUE_FULL", "message": "capture request queue is full"}
            if len(self._active) >= self.max_active:
                return 409, {"status": "error", "request_id": request_id,
                             "error_code": "CAPTURE_BUSY", "message": "maximum captures already in progress"}
            iso, epoch = wall_time()
            request = CaptureRequest(request_id, time.monotonic(), iso, epoch, options=dict(options or {}))
            response = {"status": "pending", "request_id": request_id}
            self._persist(response)
            self._active[request_id] = response
            self.requests.put_nowait(request)
            return 202, response.copy()

    def begin_frame(self):
        """Called ONLY at the top of inference, before this frame's capture.

        Requests arriving during capture/prediction wait for the next iteration.
        The lock defines the cutoff; no cached frame can satisfy a new request.
        Bursts already collecting frames also take this frame.
        """
        with self._lock:
            accepted = [burst.request for burst in self._bursts.values()]
            while not self.requests.empty():
                request = self.requests.get_nowait()
                request.accepted_monotonic = time.monotonic()
                self._active[request.request_id] = {"status": "processing", "request_id": request.request_id}
                accepted.append(request)
                self.requests.task_done()
        return accepted

    def freeze_frame(self, requests, bubble_roi, frame_id, detection, measurement,
                     timings, payload, stability, *, frame_started_epoch_ms,
                     capture_completed_epoch_ms, prediction_completed_epoch_ms,
                     frame_completed_monotonic, raw_frame=None):
        for request in requests:
            try:
                burst = self._bursts.get(request.request_id)
                if burst is None:
                    # Stability gates the trigger once, at the burst's first frame.
                    if self.require_stable and not stability.stable:
                        self._finish_error(request, "NOT_STABLE", "bubble is not stable", rejected=True)
                        continue
                    self._sample_id += 1
                    burst = Burst(request, self._sample_id, f"{self.session_id}_{self._sample_id:06d}",
                                  request.options.get("burst_frames", self.default_burst_frames))
                    with self._lock:
                        self._bursts[request.request_id] = burst
                if bubble_roi.shape != (config.ROI_HEIGHT, config.ROI_WIDTH, 3):
                    raise CaptureFailure("ROI_SIZE_INVALID", "ROI must be 740x160 with three channels")
                if not burst.frames and raw_frame is not None and config.SAVE_RAW_FRAME_PNG:
                    burst.raw_frame, burst.raw_frame_id = raw_frame.copy(), frame_id
                row = self._frame_row(burst, frame_id, detection, measurement, timings, payload, stability,
                                      frame_started_epoch_ms, capture_completed_epoch_ms,
                                      prediction_completed_epoch_ms, frame_completed_monotonic)
                # Only the trigger path copies pixels. Never retain result/full_frame.
                burst.frames.append((bubble_roi.copy(), row))
                if len(burst.frames) < burst.size:
                    continue
                with self._lock:
                    self._bursts.pop(request.request_id, None)
                summary = self._summary_row(burst) if burst.size > 1 else None
                self.jobs.put_nowait(CaptureJob(request, burst.frames, burst.raw_frame, summary))
            except queue.Full:
                self._finish_error(request, "WRITER_QUEUE_FULL", "capture writer queue is full")
            except Exception as error:
                self._finish_error(request, getattr(error, "code", "CAPTURE_FAILED"), str(error))

    def _burst_identity(self, burst):
        request = burst.request
        row = {"session_id": self.session_id, "sample_id": burst.sample_id,
               "record_id": burst.record_id, "request_id": request.request_id,
               "request_received_at_iso": request.received_iso,
               "request_received_at_epoch_ms": request.received_epoch_ms,
               "burst_id": burst.record_id, "burst_size": burst.size, "raw_frame_id": burst.raw_frame_id}
        row.update({name: request.options.get(name, "") for name in CAPTURE_OPTION_FIELDS if name != "burst_frames"})
        return row

    def _frame_row(self, burst, frame_id, detection, measurement, timings, payload, stability,
                   frame_started_epoch_ms, capture_completed_epoch_ms, prediction_completed_epoch_ms,
                   frame_completed_monotonic):
        request = burst.request
        row = self._burst_identity(burst)
        row.update(row_type="frame", burst_index=len(burst.frames) + 1, frame_id=frame_id)
        for name, epoch in (("frame_started", frame_started_epoch_ms),
                            ("capture_completed", capture_completed_epoch_ms),
                            ("prediction_completed", prediction_completed_epoch_ms)):
            row[f"{name}_at_iso"], row[f"{name}_at_epoch_ms"] = wall_time(epoch)
        row.update(detection.as_dict())
        if not detection.detected:
            for name in ("class_id", "class_name", "confidence", "x1_roi", "y1_roi", "x2_roi", "y2_roi",
                         "center_x_roi", "center_y_roi", "box_width", "box_height"):
                row[name] = ""
        row.update(measurement.as_dict())
        row["measurement_valid"] = payload["measurement"]["valid"]
        row["measurement_error"] = payload["measurement"]["error"]
        row.update({"slope_mm_per_m": payload["measurement"]["slope_mm_per_m"],
                    "angle_degrees": payload["measurement"]["angle_degrees"],
                    "within_official_range": payload["measurement"]["within_official_range"],
                    "system_state": payload["system_state"]})
        calibration = payload.get("calibration") or {}
        row.update({name: calibration.get(name, "") for name in CALIBRATION_ROW_FIELDS})
        # Latest ① tick check (at most a second old): reveals a camera bumped mid-session.
        tick_center = payload.get("tick_center") or {}
        row["tick_center_x_px"] = "" if tick_center.get("x_px") is None else tick_center["x_px"]
        row["tick_center_offset_px"] = "" if tick_center.get("offset_px") is None else tick_center["offset_px"]
        row.update(timings)
        for name, value in stability.as_dict().items():
            key = "stability_duration_seconds" if name == "stable_duration_seconds" else f"stability_{name}"
            row[key] = value
        row["queue_wait_ms"] = (request.accepted_monotonic - request.received_monotonic) * 1000
        row["request_to_frame_ms"] = (frame_completed_monotonic - request.received_monotonic) * 1000
        return row

    def _summary_row(self, burst):
        frame_rows = [row for _, row in burst.frames]
        row = self._burst_identity(burst)
        row.update(row_type="summary")
        row.update({name: frame_rows[0].get(name, "") for name in CALIBRATION_ROW_FIELDS})
        row.update(burst_summary(frame_rows))
        return row

    def _finish_error(self, request, code, message, *, rejected=False):
        response = {"status": "rejected" if rejected else "error", "request_id": request.request_id,
                    "error_code": code, "message": message}
        LOG.error("Capture %s: %s: %s", request.request_id, code, message)
        with self._lock:
            self._bursts.pop(request.request_id, None)
            self._active[request.request_id] = response
            try:
                self._persist(response)
            except Exception:
                # Keep the terminal status in bounded RAM if the disk itself failed.
                LOG.exception("Could not persist capture error status")
                return
            self._active.pop(request.request_id, None)

    def _writer_loop(self):
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                try:
                    self._save(job)
                except Exception as error:
                    code = getattr(error, "code", "WRITER_FAILED")
                    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                        code = "DISK_FULL"
                    LOG.exception("Capture writer failure")
                    self._finish_error(job.request, code, str(error))
            finally:
                self.jobs.task_done()

    def _system_snapshot(self):
        result = {"cpu_temperature_c": "", "load_average_1m": "", "disk_free_mb": ""}
        try:
            result["cpu_temperature_c"] = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            pass
        try:
            result["load_average_1m"] = os.getloadavg()[0]
        except (OSError, AttributeError):
            pass
        try:
            result["disk_free_mb"] = shutil.disk_usage(self.session_directory).free / 1024 ** 2
        except OSError:
            pass
        return result

    def _write_image(self, path, image):
        kind = "PNG" if path.suffix == ".png" else "JPEG"
        parameters = ([cv2.IMWRITE_PNG_COMPRESSION, config.RAW_PNG_COMPRESSION] if kind == "PNG"
                      else [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
        try:
            encoded, data = cv2.imencode(path.suffix, image, parameters)
        except cv2.error as error:
            raise CaptureFailure(f"{kind}_ENCODE_FAILED", f"{path.suffix} encoding failed") from error
        if not encoded:
            raise CaptureFailure(f"{kind}_ENCODE_FAILED", f"{path.suffix} encoding failed")
        try:
            with path.open("xb") as file:
                file.write(data.tobytes())
                file.flush()
                os.fsync(file.fileno())
        except OSError as error:
            code = "DISK_FULL" if error.errno == errno.ENOSPC else "IMAGE_WRITE_FAILED"
            raise CaptureFailure(code, f"{path.name} could not be saved") from error

    @staticmethod
    def _csv_line(row):
        values = {name: row.get(name, "") for name in PI_LOG_FIELDS}
        for name, value in values.items():
            if isinstance(value, float) and finite_number(value) is None:
                values[name] = ""
        buffer = io.StringIO(newline="")
        csv.DictWriter(buffer, PI_LOG_FIELDS).writerow(values)
        return buffer.getvalue().encode("utf-8")

    def _append_csv(self, rows, request):
        # One serialized writer. Roll back the whole burst on any CSV failure.
        # Measure a real append+flush+fsync, then finalize its own diagnostic
        # timing fields. csv_write_ms excludes that second diagnostic rewrite.
        def stamp():
            saved = wall_time()
            elapsed = (time.monotonic() - request.received_monotonic) * 1000
            for row in rows:
                row["saved_at_iso"], row["saved_at_epoch_ms"] = saved
                row["request_to_saved_ms"] = elapsed

        with self.csv_path.open("r+b") as file:
            file.seek(0, os.SEEK_END)
            position = file.tell()
            start = time.monotonic()
            try:
                for row in rows:
                    row["csv_write_ms"] = 0.
                stamp()
                file.write(b"".join(self._csv_line(row) for row in rows))
                file.flush()
                os.fsync(file.fileno())
                csv_write_ms = (time.monotonic() - start) * 1000
                for row in rows:
                    row["csv_write_ms"] = csv_write_ms
                # Timestamp of durable image+CSV completion; state becomes
                # saved only after these diagnostic fields are also durable.
                stamp()
                file.seek(position)
                data = b"".join(self._csv_line(row) for row in rows)
                file.write(data)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                return len(data)
            except Exception:
                file.seek(position)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                raise

    def _save(self, job):
        request = job.request
        frame_rows = [row for _, row in job.frames]
        rows = frame_rows + ([job.summary_row] if job.summary_row is not None else [])
        images = []  # (final path, image)
        for clean_roi, row in job.frames:
            start = time.monotonic()
            annotated = annotate_capture_roi(clean_roi, row)
            row["annotation_ms"] = (time.monotonic() - start) * 1000
            stem = f"{row['record_id']}_{row['burst_index']:02d}"
            for name, image in (("clean", clean_roi), ("annotated", annotated)):
                path = self.images_directory / f"{stem}_{name}.jpg"
                images.append((path, image))
                row[f"{name}_image_path"] = str(path.relative_to(self.session_directory))
                row[f"{name}_image_width"] = image.shape[1]
                row[f"{name}_image_height"] = image.shape[0]
        raw_path = ""
        if job.raw_frame is not None:
            path = self.images_directory / f"{frame_rows[0]['record_id']}_raw.png"
            images.append((path, job.raw_frame))
            raw_path = str(path.relative_to(self.session_directory))
        system = self._system_snapshot()
        for row in rows:
            row["raw_image_path"] = raw_path
        for row in frame_rows:
            row.update(system)
        finals = [path for path, _ in images]
        temporary = [path.with_name(path.stem + ".tmp" + path.suffix) for path in finals]
        csv_committed = False
        try:
            start = time.monotonic()
            for path, (_, image) in zip(temporary, images):
                self._write_image(path, image)
            for source, destination in zip(temporary, finals):
                os.replace(source, destination)
            written = sum(path.stat().st_size for path in finals)
            directory_fd = os.open(self.images_directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            for row in frame_rows:
                row["image_write_ms"] = (time.monotonic() - start) * 1000
            try:
                written += self._append_csv(rows, request)
            except Exception as error:
                code = "DISK_FULL" if getattr(error, "errno", None) == errno.ENOSPC else "CSV_WRITE_FAILED"
                raise CaptureFailure(code, "Pi CSV row could not be saved") from error
            csv_committed = True
            first = frame_rows[0]
            response = {"status": "saved", **{name: first[name] for name in SAVED_RESPONSE_FIELDS if name != "status"}}
            with self._lock:
                self._saved_bytes += written
                self._saved_captures += 1
                self._active[request.request_id] = response
                self._persist(response)
                self._active.pop(request.request_id, None)
            LOG.info("Capture saved: %s frames=%d first_frame=%s", first["record_id"], len(frame_rows), first["frame_id"])
        finally:
            for path in temporary + ([] if csv_committed else finals):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOG.exception("Could not remove incomplete capture file %s", path.name)

    def idle(self):
        """No queued, collecting or unwritten capture (safe to stop the camera)."""
        with self._lock:
            return not self._active and self.requests.empty() and self.jobs.unfinished_tasks == 0

    def storage_estimate(self):
        """Free disk and how many more default triggers fit above the reserve."""
        with self._lock:
            per_capture = (self._saved_bytes / self._saved_captures if self._saved_captures
                           else config.CAPTURE_BYTES_ESTIMATE * self.default_burst_frames / 15)
        try:
            free = shutil.disk_usage(self.session_directory).free
        except OSError:
            return {"disk_free_mb": None, "bytes_per_capture": per_capture, "estimated_remaining_captures": None}
        usable = max(0., free - config.CAPTURE_DISK_RESERVE_MB * 1024 ** 2)
        return {"disk_free_mb": free / 1024 ** 2, "bytes_per_capture": per_capture,
                "estimated_remaining_captures": int(usable // max(per_capture, 1))}

    def cancel_pending(self, code, message):
        """End queued requests and unfinished bursts (e.g. leaving measure mode).

        Frozen jobs already handed to the writer are still saved.
        """
        with self._lock:
            requests = []
            while not self.requests.empty():
                requests.append(self.requests.get_nowait())
                self.requests.task_done()
            requests += [burst.request for burst in self._bursts.values()]
        for request in requests:
            self._finish_error(request, code, message)
        return len(requests)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._ready = False
            self._closed = True
            while not self.requests.empty():
                request = self.requests.get_nowait()
                self._finish_error(request, "SERVER_SHUTDOWN", "Pi stopped before selecting a frame")
                self.requests.task_done()
            # A burst still collecting frames has no complete data to save.
            self._bursts.clear()
        # Finish frozen jobs, then stop. No terminal Y/N; successful files stay.
        self.jobs.put(None)
        self._worker.join()
        with self._lock:
            for request_id, response in list(self._active.items()):
                if response["status"] not in TERMINAL_STATES:
                    request = CaptureRequest(request_id, 0, "", 0)
                    self._finish_error(request, "SERVER_SHUTDOWN", "Pi stopped before frame processing completed")
            self._database.close()
