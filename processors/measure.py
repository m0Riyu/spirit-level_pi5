"""Mode ①: YOLO -> two-layer calibration -> stability -> telemetry / captures."""

import time

import config
from bubble_measurement import BubbleMeasurement
from calibration_store import NOMINAL_VIAL
from display import show_clean_frame as show_frame
from stability import StabilityTracker
from telemetry_server import build_telemetry_payload


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
                 geometry=None, vial=None, calibration=None):
        self.detector = detector
        self.captures = captures
        self.publish = publish
        self.logger = logger
        self.stability = new_stability_tracker()
        self.set_calibration(geometry, vial, calibration)

    def set_calibration(self, geometry, vial, calibration):
        """Swap the active calibration between frames (e.g. after ③)."""
        self.geometry, self.vial, self.calibration = geometry, vial, dict(calibration or {})

    def enter(self):
        # A window from before the switch would describe an older scene.
        self.stability = new_stability_tracker()
        self.captures.set_ready(True)

    def leave(self):
        self.captures.set_ready(False)
        self.captures.cancel_pending("MODE_CHANGED", "measurement mode was switched off before the capture finished")

    def before_capture(self):
        # Only requests already queued here can use this frame. A trigger
        # arriving during capture/YOLO must wait for the following frame.
        return {"requests": self.captures.begin_frame(), "loop_start": time.perf_counter()}

    def measure(self, detection):
        if self.geometry is None:
            return BubbleMeasurement.disabled(
                "calibration_unavailable" if config.ENABLE_BUBBLE_MEASUREMENT else "measurement_disabled")
        return self.geometry.measure(detection.center_x_roi if detection.detected else None)

    def process(self, frame, context):
        frame_id, loop_start = frame.frame_id, context["loop_start"]
        prediction = self.detector.predict(frame.roi)
        prediction_completed_at_epoch_ms = time.time() * 1000.0
        measurement = self.measure(prediction.detection)

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
        vial = self.vial or NOMINAL_VIAL
        payload = build_telemetry_payload(
            frame_id, prediction.detection, measurement, timings,
            mm_per_m_per_div=vial.mm_per_m_per_div,
            zero_offset_div=vial.zero_offset_div,
            level_tolerance_mm_per_m=config.LEVEL_TOLERANCE_MM_PER_M,
            max_measurable_slope_mm_per_m=config.MAX_MEASURABLE_SLOPE_MM_PER_M,
            calibration=self.calibration or None,
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
