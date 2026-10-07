"""Mode ①: YOLO -> two-layer calibration -> stability -> telemetry / captures."""

import collections
import time

import apriltag_config
import config
from alignment import average_angles
from apriltag_measurement import create_detector, measure_frame, summarize
from bubble_measurement import BubbleMeasurement
from calibration_store import NOMINAL_VIAL
from display import show_clean_frame as show_frame
from stability import StabilityTracker
from telemetry_server import build_telemetry_payload
from tick_detection import center_status, detect_ticks, scale_center


def print_metrics(frame_id, detection, measurement, timings):
    if frame_id % config.PRINT_EVERY != 0:
        return

    common = (
        f"capture={timings['capture_ms']:.1f}ms | "
        f"predict={timings['predict_ms']:.1f}ms | "
        f"process={timings['process_ms']:.1f}ms | "
        f"fps={timings['fps']:.1f}"
    )
    if detection.detected:
        measurement_text = (
            f"offset={measurement.offset_px:+.2f}px | "
            f"div={measurement.offset_div:+.3f} | "
            f"direction={measurement.direction} | "
            if measurement.valid
            else f"measurement={measurement.error} | "
        )
        print(
            f"frame={frame_id} | "
            f"conf={detection.confidence:.4f} | "
            f"count={detection.detection_count} | "
            f"center_x_roi={detection.center_x_roi:.2f}px | "
            f"{measurement_text}"
            f"{common}"
        )
    else:
        print(f"frame={frame_id} | 未偵測 | {common}")


def new_stability_tracker():
    return StabilityTracker(
        config.STABILITY_WINDOW_SIZE, config.STABILITY_MIN_VALID_RATIO,
        config.STABILITY_MAX_STD_MM_PER_M, config.STABILITY_MAX_RANGE_MM_PER_M,
        config.STABILITY_HOLD_SECONDS,
    )


class MeasureProcessor:
    def __init__(self, *, detector, captures, publish=None, logger=None,
                 geometry=None, vial=None, calibration=None, camera=None, tag_detector_factory=None):
        self.detector = detector
        self.captures = captures
        self.publish = publish
        self.logger = logger
        self.stability = new_stability_tracker()
        self.tick_center = center_status(None)
        self._next_tick_check = 0.0
        # ② target monitor: AprilTag pitch/yaw once a second, averaged.
        self.camera = camera
        self.tag_detector_factory = tag_detector_factory or (lambda: create_detector(apriltag_config.TAG_FAMILY))
        self._tag_detector = None
        self._pose_samples = collections.deque(maxlen=config.POSE_MONITOR_WINDOW)
        self._next_pose_check = 0.0
        self.camera_pose = self._pose_status()
        self.set_calibration(geometry, vial, calibration)

    def set_calibration(self, geometry, vial, calibration):
        """Swap the calibration from any thread (e.g. after ③ apply): one tuple
        assignment, so a frame never mixes old and new values."""
        self.active = (geometry, vial, dict(calibration or {}))

    @property
    def geometry(self):
        return self.active[0]

    @property
    def vial(self):
        return self.active[1]

    @property
    def calibration(self):
        return self.active[2]

    def enter(self):
        # A window from before the switch would describe an older scene.
        self.stability = new_stability_tracker()
        self._next_tick_check = 0.0
        self._pose_samples.clear()  # ② may just have moved the camera
        self._next_pose_check = 0.0
        self.camera_pose = self._pose_status()
        self.captures.set_ready(True)

    def leave(self):
        self.captures.set_ready(False)
        self.captures.cancel_pending("MODE_CHANGED", "measurement mode was switched off before the capture finished")

    def before_capture(self):
        # Only requests already queued here can use this frame. A trigger
        # arriving during capture/YOLO must wait for the following frame.
        return {"requests": self.captures.begin_frame(), "loop_start": time.perf_counter()}

    @staticmethod
    def measure(geometry, detection):
        if geometry is None:
            return BubbleMeasurement.disabled(
                "calibration_unavailable" if config.ENABLE_BUBBLE_MEASUREMENT else "measurement_disabled")
        if not detection.detected:
            return geometry.measure(None)
        return geometry.measure(detection.center_x_roi, detection.x1_roi, detection.x2_roi)

    def check_tick_center(self, roi, geometry):
        """Once a second: where the scale ticks are (screw trigger, camera-moved hint)."""
        now = time.monotonic()
        if now < self._next_tick_check:
            return self.tick_center
        self._next_tick_check = now + config.TICK_MONITOR_INTERVAL_SECONDS
        prior = (geometry.zero_x_roi(), geometry.px_per_div(geometry.zero_x_roi())) if geometry else (None, None)
        self.tick_center = center_status(scale_center(detect_ticks(roi, *prior)), geometry)
        return self.tick_center

    def _pose_status(self, tag_count=None):
        angles = average_angles(list(self._pose_samples))
        errors = {axis: (None if angles[axis] is None else angles[axis] - target)
                  for axis, target in config.ALIGN_TARGET_DEG.items()}
        within = None if not self._pose_samples else all(
            value is not None and abs(value) <= config.ALIGN_TOLERANCE_DEG for value in errors.values())
        return {"pitch_deg": angles["pitch_deg"], "yaw_deg": angles["yaw_deg"], "roll_deg": angles["roll_deg"],
                "pitch_error_deg": errors["pitch_deg"], "yaw_error_deg": errors["yaw_deg"],
                "within": within, "samples": len(self._pose_samples), "tag_count": tag_count,
                "tolerance_deg": config.ALIGN_TOLERANCE_DEG}

    def check_camera_pose(self, full):
        """Once a second: AprilTag pitch/yaw vs the ② target (zero), 10-reading average."""
        if self.camera is None or full is None:
            return self.camera_pose
        now = time.monotonic()
        if now < self._next_pose_check:
            return self.camera_pose
        self._next_pose_check = now + config.POSE_MONITOR_INTERVAL_SECONDS
        if self._tag_detector is None:
            self._tag_detector = self.tag_detector_factory()
        summary = summarize(measure_frame(full, self._tag_detector, self.camera.undistorter.camera_matrix,
                                          apriltag_config.TAG_SIZE_METER))
        if summary["pose_count"]:
            self._pose_samples.append({axis: summary[axis] for axis in ("pitch_deg", "yaw_deg", "roll_deg")})
        self.camera_pose = self._pose_status(summary["pose_count"])
        return self.camera_pose

    def process(self, frame, context):
        frame_id, loop_start = frame.frame_id, context["loop_start"]
        geometry, vial, calibration = self.active
        prediction = self.detector.predict(frame.roi)
        prediction_completed_at_epoch_ms = time.time() * 1000.0
        measurement = self.measure(geometry, prediction.detection)

        process_ms = (time.perf_counter() - loop_start) * 1000.0
        timings = {
            "capture_ms": frame.capture_ms,
            "predict_ms": prediction.predict_ms,
            "yolo_preprocess_ms": prediction.preprocess_ms,
            "yolo_inference_ms": prediction.inference_ms,
            "yolo_postprocess_ms": prediction.postprocess_ms,
            "plot_ms": 0.0,
            "process_ms": process_ms,
            "fps": 1000.0 / process_ms if process_ms > 0 else 0.0,
        }
        vial = vial or NOMINAL_VIAL
        payload = build_telemetry_payload(
            frame_id, prediction.detection, measurement, timings,
            mm_per_m_per_div=vial.mm_per_m_per_div,
            zero_offset_div=vial.zero_offset_div,
            level_tolerance_mm_per_m=config.LEVEL_TOLERANCE_MM_PER_M,
            max_measurable_slope_mm_per_m=config.MAX_MEASURABLE_SLOPE_MM_PER_M,
            calibration=calibration or None,
        )
        stability = self.stability.update(
            payload["measurement"]["valid"], payload["measurement"]["slope_mm_per_m"],
            payload["measurement"]["offset_div"], payload["confidence"],
        )
        # Include per-frame stability work in process/FPS metrics.
        timings["process_ms"] = (time.perf_counter() - loop_start) * 1000
        timings["fps"] = 1000 / timings["process_ms"] if timings["process_ms"] > 0 else 0.
        payload["performance"]["processing_ms"] = timings["process_ms"]
        payload["performance"]["fps"] = timings["fps"]
        if self.logger is not None:
            self.logger.write(frame_id, prediction.detection, measurement, timings)
        print_metrics(frame_id, prediction.detection, measurement, timings)
        payload["mode"] = "measure"
        payload["tick_center"] = self.check_tick_center(frame.roi, geometry)
        payload["camera_pose"] = self.check_camera_pose(frame.full)
        payload["stability"] = stability.as_dict()
        payload["capture"] = {"ready": self.captures.ready, "require_stable_for_capture": self.captures.require_stable}
        payload["frame_started_at_epoch_ms"] = frame.started_epoch_ms
        payload["capture_completed_at_epoch_ms"] = frame.captured_epoch_ms
        payload["prediction_completed_at_epoch_ms"] = prediction_completed_at_epoch_ms
        if context["requests"]:
            self.captures.freeze_frame(
                context["requests"], frame.roi, frame_id, prediction.detection, measurement,
                timings, payload, stability, frame_started_epoch_ms=frame.started_epoch_ms,
                capture_completed_epoch_ms=frame.captured_epoch_ms,
                prediction_completed_epoch_ms=prediction_completed_at_epoch_ms,
                frame_completed_monotonic=time.monotonic(), raw_frame=frame.raw,
            )

        if self.publish is not None and frame_id % config.TELEMETRY_SEND_EVERY == 0:
            self.publish(payload)

        return bool(config.ENABLE_IMAGE_STREAM and show_frame(
            frame.roi, prediction.detection, measurement, timings["fps"]))
