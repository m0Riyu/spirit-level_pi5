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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2

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
    "annotated_image_width annotated_image_height"
).split()
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


def git_value(*arguments):
    try:
        return subprocess.run(["git", *arguments], cwd=config.APP_DIRECTORY,
                              check=True, capture_output=True, text=True, timeout=2).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def session_metadata(session_id):
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
        "calibration_source": str(config.BUBBLE_CALIBRATION_PATH),
        "camera_calibration_source": str(config.CAMERA_CALIBRATION_NPZ),
        "mm_per_m_per_div": config.MM_PER_M_PER_DIV,
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
    }


@dataclass
class CaptureRequest:
    request_id: str
    received_monotonic: float
    received_iso: str
    received_epoch_ms: float
    accepted_monotonic: float = 0.


@dataclass
class CaptureJob:
    request: CaptureRequest
    clean_roi: object
    row: dict


class CaptureFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class CaptureManager:
    def __init__(self, root=None, *, request_queue_size=None, writer_queue_size=None,
                 max_active=None, require_stable=None):
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
        metadata = session_metadata(self.session_id)
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

    def submit(self, request_id):
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
            request = CaptureRequest(request_id, time.monotonic(), iso, epoch)
            response = {"status": "pending", "request_id": request_id}
            self._persist(response)
            self._active[request_id] = response
            self.requests.put_nowait(request)
            return 202, response.copy()

    def begin_frame(self):
        """Called ONLY at the top of inference, before this frame's capture.

        Requests arriving during capture/prediction wait for the next iteration.
        The lock defines the cutoff; no cached frame can satisfy a new request.
        """
        accepted = []
        with self._lock:
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
                     frame_completed_monotonic):
        for request in requests:
            try:
                if self.require_stable and not stability.stable:
                    self._finish_error(request, "NOT_STABLE", "bubble is not stable", rejected=True)
                    continue
                if bubble_roi.shape != (config.ROI_HEIGHT, config.ROI_WIDTH, 3):
                    raise CaptureFailure("ROI_SIZE_INVALID", "ROI must be 740x160 with three channels")
                self._sample_id += 1
                record_id = f"{self.session_id}_{self._sample_id:06d}"
                row = {"session_id": self.session_id, "sample_id": self._sample_id,
                       "record_id": record_id, "request_id": request.request_id, "frame_id": frame_id,
                       "request_received_at_iso": request.received_iso,
                       "request_received_at_epoch_ms": request.received_epoch_ms}
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
                row.update(timings)
                for name, value in stability.as_dict().items():
                    key = "stability_duration_seconds" if name == "stable_duration_seconds" else f"stability_{name}"
                    row[key] = value
                row["queue_wait_ms"] = (request.accepted_monotonic - request.received_monotonic) * 1000
                row["request_to_frame_ms"] = (frame_completed_monotonic - request.received_monotonic) * 1000
                # Only the trigger path copies pixels. Never retain result/full_frame.
                clean_roi = bubble_roi.copy()
                self.jobs.put_nowait(CaptureJob(request, clean_roi, row))
            except queue.Full:
                self._finish_error(request, "WRITER_QUEUE_FULL", "capture writer queue is full")
            except Exception as error:
                self._finish_error(request, getattr(error, "code", "CAPTURE_FAILED"), str(error))

    def _finish_error(self, request, code, message, *, rejected=False):
        response = {"status": "rejected" if rejected else "error", "request_id": request.request_id,
                    "error_code": code, "message": message}
        LOG.error("Capture %s: %s: %s", request.request_id, code, message)
        with self._lock:
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
        try:
            encoded, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
        except cv2.error as error:
            raise CaptureFailure("JPEG_ENCODE_FAILED", "ROI JPEG encoding failed") from error
        if not encoded:
            raise CaptureFailure("JPEG_ENCODE_FAILED", "ROI JPEG encoding failed")
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

    def _append_csv(self, row, request):
        # One serialized writer. Roll back just this row on any CSV failure.
        # Measure a real append+flush+fsync, then finalize its own diagnostic
        # timing fields. csv_write_ms excludes that second diagnostic rewrite.
        with self.csv_path.open("r+b") as file:
            file.seek(0, os.SEEK_END)
            position = file.tell()
            start = time.monotonic()
            try:
                row["csv_write_ms"] = 0.
                row["saved_at_iso"], row["saved_at_epoch_ms"] = wall_time()
                row["request_to_saved_ms"] = (time.monotonic() - request.received_monotonic) * 1000
                file.write(self._csv_line(row))
                file.flush()
                os.fsync(file.fileno())
                row["csv_write_ms"] = (time.monotonic() - start) * 1000
                # Timestamp of durable image+CSV completion; state becomes
                # saved only after these diagnostic fields are also durable.
                row["saved_at_iso"], row["saved_at_epoch_ms"] = wall_time()
                row["request_to_saved_ms"] = (time.monotonic() - request.received_monotonic) * 1000
                file.seek(position)
                file.write(self._csv_line(row))
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
            except Exception:
                file.seek(position)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
                raise

    def _save(self, job):
        row, request = job.row, job.request
        stem = row["record_id"]
        finals = [self.images_directory / f"{stem}_{name}.jpg" for name in ("clean", "annotated")]
        temporary = [path.with_name(path.stem + ".tmp.jpg") for path in finals]
        start = time.monotonic()
        annotated = annotate_capture_roi(job.clean_roi, row)
        row["annotation_ms"] = (time.monotonic() - start) * 1000
        row.update(self._system_snapshot())
        for name, path in zip(("clean", "annotated"), finals):
            row[f"{name}_image_path"] = str(path.relative_to(self.session_directory))
            row[f"{name}_image_width"] = job.clean_roi.shape[1]
            row[f"{name}_image_height"] = job.clean_roi.shape[0]
        csv_committed = False
        try:
            start = time.monotonic()
            self._write_image(temporary[0], job.clean_roi)
            self._write_image(temporary[1], annotated)
            for source, destination in zip(temporary, finals):
                os.replace(source, destination)
            directory_fd = os.open(self.images_directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            row["image_write_ms"] = (time.monotonic() - start) * 1000
            try:
                self._append_csv(row, request)
            except Exception as error:
                code = "DISK_FULL" if getattr(error, "errno", None) == errno.ENOSPC else "CSV_WRITE_FAILED"
                raise CaptureFailure(code, "Pi CSV row could not be saved") from error
            csv_committed = True
            response = {"status": "saved", **{name: row[name] for name in SAVED_RESPONSE_FIELDS if name != "status"}}
            with self._lock:
                self._active[request.request_id] = response
                self._persist(response)
                self._active.pop(request.request_id, None)
            LOG.info("Capture saved: %s frame=%s", stem, row["frame_id"])
        finally:
            for path in temporary + ([] if csv_committed else finals):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOG.exception("Could not remove incomplete capture file %s", path.name)

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
        # Finish frozen jobs, then stop. No terminal Y/N; successful files stay.
        self.jobs.put(None)
        self._worker.join()
        with self._lock:
            for request_id, response in list(self._active.items()):
                if response["status"] not in TERMINAL_STATES:
                    request = CaptureRequest(request_id, 0, "", 0)
                    self._finish_error(request, "SERVER_SHUTDOWN", "Pi stopped before frame processing completed")
            self._database.close()
