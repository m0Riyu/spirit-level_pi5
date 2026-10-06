"""Mode ③: tick check -> multi-frame fit -> checks -> apply as new geometry.

Ticks are detected on every frame for the live view. A measurement collects
TICK_MEASURE_FRAMES frames, takes each tick's median position, fits the
geometry polynomial and checks it against the active version. Applying saves a
new geometry version and reloads the measurement processor (no restart).
"""

import threading
import time
import uuid

import cv2

import config
from geometry_calibration import fit_geometry
from tick_detection import combine_frames, detect_ticks

OCCLUDED_HINT = "刻度被氣泡邊緣遮住或未偵測完整，請稍微傾斜水平儀讓氣泡離開刻度後重試。"


class TickMeasurement:
    def __init__(self, frames_target):
        self.id = uuid.uuid4().hex[:12]
        self.status = "measuring"
        self.frames_target = frames_target
        self.frames = []
        self.first_roi = None
        self.record = None
        self.message = ""
        self.applied_version = None

    def as_dict(self):
        return {"id": self.id, "status": self.status, "frames_collected": len(self.frames),
                "frames_target": self.frames_target, "message": self.message,
                "applied_version": self.applied_version, "result": self.record}


class TickProcessor:
    mode = "ticks"

    def __init__(self, *, runtime=None, camera=None, publish=None, preview=None,
                 frames=None, publish_interval_seconds=0.5):
        self.runtime = runtime
        self.camera = camera
        self.publish = publish
        self.preview = preview
        self.frames_target = frames or config.TICK_MEASURE_FRAMES
        self.publish_interval_seconds = publish_interval_seconds
        self._lock = threading.Lock()
        self._current = None
        self._results = {}  # bounded: last few measurements
        self._next_publish = 0.0
        self._last = None

    # ---- mode lifecycle -------------------------------------------------
    def enter(self):
        self._next_publish = 0.0
        if self.preview is not None:
            self.preview.set_source(self.mode)

    def leave(self):
        with self._lock:
            if self._current is not None:
                self._current.status, self._current.message = "cancelled", "已離開刻度檢查模式，量測中止"
                self._current = None
        if self.preview is not None and self.preview.source == self.mode:
            self.preview.set_source("")

    def before_capture(self):
        return None

    # ---- requests from HTTP threads ---------------------------------------
    def request_measure(self):
        with self._lock:
            if self._current is None:
                self._current = TickMeasurement(self.frames_target)
                self._results[self._current.id] = self._current
                for old in list(self._results)[:-5]:
                    self._results.pop(old)
            return self._current.as_dict()

    def result(self, measurement_id):
        with self._lock:
            measurement = self._results.get(measurement_id)
            return None if measurement is None else measurement.as_dict()

    def apply(self, measurement_id, confirm=False):
        """Returns (http_status, body)."""
        with self._lock:
            measurement = self._results.get(measurement_id)
            if measurement is None:
                return 404, {"status": "error", "error_code": "NOT_FOUND", "message": "measurement not found"}
            if measurement.status != "done":
                return 409, {"status": "error", "error_code": "NOT_READY", "message": f"measurement is {measurement.status}"}
            if measurement.applied_version:
                return 200, {"status": "applied", "version": measurement.applied_version}
            checks = measurement.record["checks"]
            if not checks["passed"]:
                return 409, {"status": "error", "error_code": "CHECKS_FAILED", "message": "檢查未通過，不能套用"}
            if checks["needs_confirmation"] and not confirm:
                return 409, {"status": "error", "error_code": "CONFIRMATION_REQUIRED",
                             "message": "px/格 與目前版本差異超過門檻，需再次確認才能套用"}
            record, roi = measurement.record, measurement.first_roi
        version, status = self.runtime.apply_geometry(record, roi)
        with self._lock:
            measurement.applied_version = version
        return 200, {"status": "applied", "version": version, "calibration": status}

    # ---- per frame ----------------------------------------------------------
    def _prior(self):
        geometry = self.runtime.measure.geometry if self.runtime is not None else None
        if geometry is None:
            return None, None
        center = geometry.zero_x_roi()
        return center, geometry.px_per_div(center)

    def process(self, frame, context):
        prior_center, prior_pitch = self._prior()
        ticks = detect_ticks(frame.roi, prior_center, prior_pitch)
        self._last = ticks
        finished = None
        with self._lock:
            current = self._current
            if current is not None:
                if not current.frames:
                    current.first_roi = frame.roi.copy()
                current.frames.append(ticks)
                if len(current.frames) >= current.frames_target:
                    self._current, finished = None, current
        if finished is not None:
            self._finish(finished)
        if self.preview is not None and self.preview.wanted:
            self.preview.publish(self.overlay(frame.roi, ticks))
        now = time.monotonic()
        if self.publish is not None and (finished is not None or now >= self._next_publish):
            self._next_publish = now + self.publish_interval_seconds
            with self._lock:
                measuring = self._current.as_dict() if self._current is not None else None
            self.publish({"type": "ticks", "schema_version": 1, "mode": self.mode, "frame_id": frame.frame_id,
                          "sent_at_epoch_ms": time.time() * 1000.0, "capture_ms": frame.capture_ms,
                          "status": "measuring" if measuring else "live",
                          "tick_count": ticks.count(), "left_count": ticks.count("left"),
                          "right_count": ticks.count("right"),
                          "expected_tick_count": 2 * config.TICK_EXPECTED_PER_SIDE,
                          "roll_deg": ticks.roll_deg, "measurement": measuring,
                          "finished": finished.as_dict() if finished is not None else None})
        return False

    def _finish(self, measurement):
        ticks, roll = combine_frames(measurement.frames)
        previous = self.runtime.measure.geometry if self.runtime is not None else None
        camera_geometry = self.camera.image_geometry() if self.camera is not None else {}
        try:
            record = fit_geometry(
                ticks, degree=config.GEOMETRY_POLYNOMIAL_DEGREE, version="pending_geometry", created_at_iso="",
                frames_used=len(measurement.frames), roll_deg=roll,
                focus_absolute=camera_geometry.get("focus_absolute"), image_geometry=camera_geometry,
                previous=previous, expected_ticks=2 * config.TICK_EXPECTED_PER_SIDE,
                max_residual_rms_px=config.TICK_MAX_RESIDUAL_RMS_PX, max_pitch_change=config.TICK_MAX_PITCH_CHANGE)
        except (ValueError, ArithmeticError) as error:
            measurement.status, measurement.message = "failed", f"擬合失敗：{error}。{OCCLUDED_HINT}"
            return
        if previous is not None:
            record["previous_scale_center_x_px"] = previous.zero_x_roi()
        measurement.record = record
        measurement.status = "done"
        if not record["checks"]["tick_count_ok"]:
            measurement.message = f"偵測到 {len(ticks)} / {record['checks']['expected_tick_count']} 條刻度。{OCCLUDED_HINT}"

    @staticmethod
    def overlay(roi, ticks):
        image = roi.copy()
        for (side, tick_id), x in ticks.positions.items():
            position = int(round(x))
            cv2.line(image, (position, config.TICK_BAND_Y1), (position, config.TICK_BAND_Y2), (0, 220, 0), 1)
            if tick_id % 4 == 0:
                cv2.putText(image, str(tick_id), (position - 4, config.TICK_BAND_Y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, .33, (0, 220, 0), 1, cv2.LINE_AA)
        cv2.putText(image, f"ticks {ticks.count()}/{2 * config.TICK_EXPECTED_PER_SIDE}", (6, 14),
                    cv2.FONT_HERSHEY_SIMPLEX, .42, (255, 255, 255), 1, cv2.LINE_AA)
        return image
