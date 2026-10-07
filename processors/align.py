"""Mode ②: re-aim the camera with the screws until the scale's tick center is
back near the ROI center, then complete alignment (geometry -> pending, redo ③).

The target is the tick center (median of left/right tick pair midpoints) within
±TICK_CENTER_TOLERANCE_PX of the ROI center. AprilTag pitch/yaw/roll against an
optional baseline are shown as a second reference.

Tags are measured on the undistorted FULL frame (not the ROI) with the same
camera matrix as the service. Angles are a moving average of the last
ALIGN_AVERAGE_FRAMES frames; their spread is shown as the reading noise.
Teaching and new baselines average frames captured AFTER the button press, so
a screw still turning cannot leak into the recorded angles.
"""

import collections
import json
import threading
import time
from datetime import datetime

import cv2

import apriltag_config
import config
from alignment import SCREWS, average_samples, delta_from_baseline, guidance, screw_model, tick_center_guidance
from apriltag_measurement import create_detector, measure_frame, summarize
from calibration_store import TIMEZONE
from tick_detection import center_status, detect_ticks, scale_center


class Collection:
    """Average the next `frames` valid samples, then call `done(angles)`."""
    def __init__(self, label, frames, done):
        self.label, self.frames, self.done = label, frames, done
        self.samples = []


class AlignProcessor:
    mode = "align"

    def __init__(self, *, store=None, runtime=None, camera=None, publish=None, preview=None,
                 detector_factory=None, log_directory=None):
        self.store = store
        self.runtime = runtime
        self.camera = camera
        self.publish = publish
        self.preview = preview
        self.detector_factory = detector_factory or (lambda: create_detector(apriltag_config.TAG_FAMILY))
        self.log_directory = log_directory or config.LOG_DIRECTORY / "alignment"
        self.detector = None
        self._lock = threading.Lock()
        self._samples = collections.deque(maxlen=config.ALIGN_AVERAGE_FRAMES)
        self._collections = []
        self._teach = {}
        self._message = ""
        self._next_publish = 0.0
        self._in_range_since = None
        self._was_within = False
        self._start_angles = None
        self._last = {}
        self.alignment = None

    # ---- mode lifecycle -------------------------------------------------
    def enter(self):
        with self._lock:
            self._samples.clear()
            self._collections.clear()
            self._in_range_since, self._was_within, self._start_angles = None, False, None
            self._next_publish = 0.0
        self.alignment = self._load_alignment()
        if self.preview is not None:
            self.preview.set_source(self.mode)

    def leave(self):
        with self._lock:
            self._collections.clear()
            self._teach.clear()
        if self.preview is not None and self.preview.source == self.mode:
            self.preview.set_source("")

    def before_capture(self):
        return None

    def _load_alignment(self):
        if self.store is None or self.store.active_state("alignment")["version"] is None:
            return None
        data, _ = self.store.load("alignment")
        return data

    # ---- requests from HTTP threads ---------------------------------------
    def _collect(self, label, done):
        with self._lock:
            if any(job.label == label for job in self._collections):
                return
            self._collections.append(Collection(label, config.ALIGN_AVERAGE_FRAMES, done))

    def teach(self, screw, step):
        if screw not in SCREWS or step not in ("start", "finish"):
            raise ValueError("teach needs screw A/B and start/finish")
        if step == "finish" and "before" not in self._teach.get(screw, {}):
            return 409, {"status": "error", "error_code": "NOT_STARTED", "message": f"請先按螺絲 {screw} 的「開始」"}

        def done(angles):
            with self._lock:
                self._teach.setdefault(screw, {})["before" if step == "start" else "after"] = angles
                self._message = (f"已記錄螺絲 {screw} 起始角度，請順時針轉 {config.ALIGN_TEACH_TURN:g} 圈後按「完成」"
                                 if step == "start" else f"已記錄螺絲 {screw} 轉動後角度")
                teach = dict(self._teach)
            if all("after" in teach.get(name, {}) for name in SCREWS):
                self._save_model(teach)

        if step == "start":
            with self._lock:
                self._teach[screw] = {}
        self._collect(f"teach-{screw}-{step}", done)
        return 202, {"status": "collecting", "screw": screw, "step": step, "frames": config.ALIGN_AVERAGE_FRAMES}

    def _save_model(self, teach):
        try:
            model = screw_model(teach, config.ALIGN_TEACH_TURN)
        except ValueError as error:
            with self._lock:
                self._message = str(error)
                self._teach.clear()
            return
        model["created_at_iso"] = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        model["teach"] = teach
        baseline = (self.alignment or {}).get("baseline")
        self._save_alignment(baseline, model)
        with self._lock:
            self._teach.clear()
            self._message = "螺絲模型已建立並儲存"

    def _save_alignment(self, baseline, model):
        now = datetime.now(TIMEZONE)
        geometry = self.store.active_state("geometry")["version"]
        record = {"schema_version": 1, "version": self.store.new_version("alignment", now),
                  "created_at_iso": now.isoformat(timespec="seconds"), "baseline": baseline, "screw_model": model,
                  "geometry_version": geometry, "tag_family": apriltag_config.TAG_FAMILY,
                  "tag_size_m": apriltag_config.TAG_SIZE_METER}
        self.store.save("alignment", record)
        self.alignment = record
        if self.runtime is not None:
            self.runtime.reload()  # alignment version is logged with every capture
        return record

    def set_baseline(self, confirm=False):
        if not confirm:
            return 409, {"status": "error", "error_code": "CONFIRMATION_REQUIRED",
                         "message": "設為新基準只應在完整校正並驗證後使用，請再次確認"}

        def done(angles):
            record = self._save_alignment(angles, (self.alignment or {}).get("screw_model"))
            with self._lock:
                self._message = f"新基準已儲存：{record['version']}"
                self._in_range_since, self._was_within = None, False

        self._collect("baseline", done)
        return 202, {"status": "collecting", "frames": config.ALIGN_AVERAGE_FRAMES}

    def complete(self):
        with self._lock:
            status = dict(self._last)
            start = self._start_angles
        if not status.get("can_complete"):
            return 409, {"status": "error", "error_code": "NOT_IN_RANGE",
                         "message": f"刻度中心需在畫面中心 ±{config.TICK_CENTER_TOLERANCE_PX:g} px 內持續 "
                                    f"{config.ALIGN_HOLD_SECONDS:g} 秒"}
        now = datetime.now(TIMEZONE)
        geometry = self.store.active_state("geometry")
        log = {"schema_version": 1, "completed_at_iso": now.isoformat(timespec="milliseconds"),
               "alignment_version": (self.alignment or {}).get("version"), "baseline": (self.alignment or {}).get("baseline"),
               "before": start, "after": status["angles"], "delta_after": status["delta"],
               "tick_center_after": status["tick_center"], "tick_center_tolerance_px": config.TICK_CENTER_TOLERANCE_PX,
               "geometry_version": geometry["version"], "tolerance_deg": config.ALIGN_TOLERANCE_DEG}
        self.log_directory.mkdir(parents=True, exist_ok=True)
        path = self.log_directory / f"{now.strftime('%Y%m%dT%H%M%S')}_alignment_log.json"
        path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        # The camera moved: the image geometry must be re-checked in ③.
        if geometry["version"] is not None:
            self.store.activate("geometry", geometry["version"], pending_confirmation=True)
        calibration = self.runtime.reload() if self.runtime is not None else None
        return 200, {"status": "completed", "log": str(path), "next": "#/ticks", "calibration": calibration}

    def status(self):
        with self._lock:
            return dict(self._last)

    # ---- per frame ----------------------------------------------------------
    def process(self, frame, context):
        start = time.perf_counter()
        if self.detector is None:
            self.detector = self.detector_factory()
        matrix = self.camera.undistorter.camera_matrix
        measurements = measure_frame(frame.full, self.detector, matrix, apriltag_config.TAG_SIZE_METER)
        summary = summarize(measurements)
        geometry = self.runtime.measure.geometry if self.runtime is not None else None
        prior = (geometry.zero_x_roi(), geometry.px_per_div(geometry.zero_x_roi())) if geometry else (None, None)
        center = scale_center(detect_ticks(frame.roi, *prior))
        finished = []
        with self._lock:
            if summary["pose_count"] or center is not None:
                sample = {axis: summary[axis] for axis in ("pitch_deg", "yaw_deg", "roll_deg")}
                sample["tick_center_px"] = center
                self._samples.append(sample)
                for job in self._collections:
                    job.samples.append(sample)
                finished = [job for job in self._collections if len(job.samples) >= job.frames]
                self._collections = [job for job in self._collections if job not in finished]
            angles = average_samples(list(self._samples))
            full = len(self._samples) == self._samples.maxlen
            if full and self._start_angles is None:
                self._start_angles = angles
        for job in finished:
            job.done(average_samples(job.samples))

        alignment = self.alignment or {}
        baseline = alignment.get("baseline")
        delta = delta_from_baseline(angles, baseline) if baseline else None
        guide = guidance(delta, alignment.get("screw_model"), config.ALIGN_TOLERANCE_DEG) if delta and full else None
        tick_center = center_status(angles["tick_center_px"], geometry)
        tick_center["std_px"] = angles["tick_center_px_std"]
        tick_center["guidance"] = tick_center_guidance(tick_center["offset_px"], alignment.get("screw_model"),
                                                       config.TICK_CENTER_TOLERANCE_PX)
        now = time.monotonic()
        within = bool(full and tick_center["within"])
        with self._lock:
            entered = within and not self._was_within
            self._was_within = within
            self._in_range_since = (self._in_range_since or now) if within else None
            held = now - self._in_range_since if self._in_range_since else 0.0
            self._last = {
                "type": "align", "schema_version": 1, "mode": self.mode, "frame_id": frame.frame_id,
                "sent_at_epoch_ms": time.time() * 1000.0, "capture_ms": frame.capture_ms,
                "process_ms": (time.perf_counter() - start) * 1000.0,
                "tag_count": summary["tag_count"], "pose_count": summary["pose_count"],
                "tag_ids": [result["tag_id"] for result in measurements],
                "angles": angles, "window_full": full, "delta": delta, "guidance": guide,
                "tick_center": tick_center,
                "baseline_version": alignment.get("version") if baseline else None,
                "model_ready": alignment.get("screw_model") is not None,
                "tolerance_deg": config.ALIGN_TOLERANCE_DEG, "hold_seconds_required": config.ALIGN_HOLD_SECONDS,
                "in_range_seconds": held, "entered_range": entered,
                "can_complete": within and held >= config.ALIGN_HOLD_SECONDS,
                "teaching": {screw: sorted(steps) for screw, steps in self._teach.items()},
                "collecting": [job.label for job in self._collections],
                "message": self._message,
            }
            payload = dict(self._last)
        if self.preview is not None and self.preview.wanted:
            self.preview.publish(self.overlay(frame.full, measurements))
        if self.publish is not None and (entered or now >= self._next_publish):
            self._next_publish = now + config.ALIGN_PUBLISH_INTERVAL_SECONDS
            self.publish(payload)
        return False

    @staticmethod
    def overlay(full, measurements):
        image = full.copy()
        for result in measurements:
            points = [tuple(int(round(v)) for v in point) for point in result["corners_px"]]
            for a, b in zip(points, points[1:] + points[:1]):
                cv2.line(image, a, b, (0, 255, 0) if result["pose_valid"] else (0, 0, 255), 2)
            cv2.putText(image, str(result["tag_id"]), points[0], cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 255, 255), 2)
        width = 640
        return cv2.resize(image, (width, round(image.shape[0] * width / image.shape[1])), interpolation=cv2.INTER_AREA)
