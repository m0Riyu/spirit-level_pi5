"""Convert a detected bubble center into calibrated scale divisions."""

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class BubbleCalibration:
    center_x_roi: float
    pitch_px_per_div: float
    image_width: int
    image_height: int
    status: str
    created_utc: str
    source_path: str
    _original_calibration: object = field(default=None, repr=False, compare=False)
    _undistorter: object = field(default=None, repr=False, compare=False)
    _roi_origin: tuple = field(default=(0, 0), repr=False)
    _axis_y_roi: float = field(default=0.0, repr=False)

    @classmethod
    def from_json(cls, path, expected_size=None, *, undistorter=None, roi_origin=(0, 0)):
        calibration_path = Path(path)
        with calibration_path.open(encoding="utf-8") as file:
            data = json.load(file)

        status = str(data.get("status", ""))
        if status not in {"PASS", "WARN"}:
            raise ValueError(f"calibration status is {status or 'missing'}")

        center_x = data.get("reference_midpoint_x")
        pitch = data.get("global_pitch", {}).get("pitch_px")
        if center_x is None:
            raise ValueError("reference_midpoint_x is missing")
        if pitch is None or float(pitch) <= 0:
            raise ValueError("global_pitch.pitch_px must be positive")

        image_width = int(data["image_width"])
        image_height = int(data["image_height"])
        if expected_size is not None:
            expected_width, expected_height = expected_size
            if (image_width, image_height) != (
                int(expected_width),
                int(expected_height),
            ):
                raise ValueError(
                    "calibration image size "
                    f"{image_width}x{image_height} does not match ROI "
                    f"{expected_width}x{expected_height}"
                )

        calibration = cls(
            center_x_roi=float(center_x),
            pitch_px_per_div=float(pitch),
            image_width=image_width,
            image_height=image_height,
            status=status,
            created_utc=str(data.get("created_utc", "")),
            source_path=str(calibration_path.resolve()),
        )
        if undistorter is None:
            return calibration
        geometry = data.get("image_geometry", {})
        coordinate_system = geometry.get("coordinate_system", "original")
        if coordinate_system == "undistorted":
            # Tuner measurements are already in the rectified ROI. Validate
            # their geometry and use their measured center/pitch directly.
            expected_geometry = {
                "frame_size": undistorter.image_size,
                "roi_origin": roi_origin,
                "original_camera_matrix": undistorter.original_camera_matrix,
                "dist_coeffs": undistorter.distortion.reshape(-1),
                "new_camera_matrix": undistorter.camera_matrix,
            }
            for name, expected in expected_geometry.items():
                recorded = np.asarray(geometry[name], dtype=float)
                expected = np.asarray(expected, dtype=float)
                if name == "dist_coeffs":
                    recorded = recorded.reshape(-1)
                if (recorded.shape != expected.shape
                        or not np.allclose(recorded, expected, rtol=1e-8, atol=1e-8)):
                    raise ValueError(f"undistorted calibration {name} differs from runtime geometry")
            return calibration
        if coordinate_system != "original":
            raise ValueError(f"unsupported calibration coordinate system: {coordinate_system}")
        if "axis_y" not in data:
            raise ValueError("axis_y is required to convert the original tick calibration")
        axis_y = float(data["axis_y"])
        if not math.isfinite(axis_y) or not 0 <= axis_y < image_height:
            raise ValueError("axis_y must be inside the original calibration ROI")
        origin_x, origin_y = roi_origin
        half_pitch = calibration.pitch_px_per_div / 2
        points = undistorter.undistort_points([
            (calibration.center_x_roi + origin_x, axis_y + origin_y),
            (calibration.center_x_roi - half_pitch + origin_x, axis_y + origin_y),
            (calibration.center_x_roi + half_pitch + origin_x, axis_y + origin_y),
        ])
        return replace(
            calibration,
            center_x_roi=float(points[0, 0] - origin_x),
            pitch_px_per_div=float(points[2, 0] - points[1, 0]),
            _axis_y_roi=float(points[0, 1] - origin_y),
            _original_calibration=calibration,
            _undistorter=undistorter,
            _roi_origin=tuple(roi_origin),
        )

    def measure(self, bubble_center_x_roi, bubble_center_y_roi=None):
        if bubble_center_x_roi in (None, ""):
            return BubbleMeasurement.not_detected(self)

        center_x = float(bubble_center_x_roi)
        offset_px = center_x - self.center_x_roi
        offset_div = offset_px / self.pitch_px_per_div
        scale_center_x = self.center_x_roi
        if self._undistorter is not None:
            # The old JSON describes original pixels. Recover that coordinate
            # for divisions, preserving its measured scale even though remap
            # changes pixel spacing nonlinearly across the image.
            center_y = (self._axis_y_roi if bubble_center_y_roi in (None, "")
                        else float(bubble_center_y_roi))
            origin_x, origin_y = self._roi_origin
            original_x, original_y = self._undistorter.distort_points([
                (center_x + origin_x, center_y + origin_y),
            ])[0]
            original = self._original_calibration
            offset_div = (original_x - origin_x - original.center_x_roi) / original.pitch_px_per_div
            scale_center_x = float(self._undistorter.undistort_points([
                (original.center_x_roi + origin_x, original_y),
            ])[0, 0] - origin_x)
            offset_px = center_x - scale_center_x
        if offset_px > 0:
            direction = "right"
        elif offset_px < 0:
            direction = "left"
        else:
            direction = "center"

        return BubbleMeasurement(
            valid=1,
            error="",
            center_x_roi=center_x,
            scale_center_x_roi=scale_center_x,
            pitch_px_per_div=self.pitch_px_per_div,
            offset_px=offset_px,
            offset_div=offset_div,
            absolute_offset_div=abs(offset_div),
            direction=direction,
            calibration_status=self.status,
            calibration_created_utc=self.created_utc,
            calibration_source=self.source_path,
        )


@dataclass(frozen=True)
class BubbleMeasurement:
    valid: int = 0
    error: str = ""
    center_x_roi: object = ""
    scale_center_x_roi: object = ""
    pitch_px_per_div: object = ""
    offset_px: object = ""
    offset_div: object = ""
    absolute_offset_div: object = ""
    direction: str = ""
    calibration_status: str = ""
    calibration_created_utc: str = ""
    calibration_source: str = ""

    @classmethod
    def disabled(cls, error="measurement_disabled"):
        return cls(error=error)

    @classmethod
    def not_detected(cls, calibration):
        return cls(
            error="bubble_not_detected",
            scale_center_x_roi=calibration.center_x_roi,
            pitch_px_per_div=calibration.pitch_px_per_div,
            calibration_status=calibration.status,
            calibration_created_utc=calibration.created_utc,
            calibration_source=calibration.source_path,
        )

    def as_dict(self):
        return {
            "measurement_valid": self.valid,
            "measurement_error": self.error,
            "bubble_center_x_roi": self.center_x_roi,
            "scale_center_x_roi": self.scale_center_x_roi,
            "pitch_px_per_div": self.pitch_px_per_div,
            "bubble_offset_px": self.offset_px,
            "bubble_offset_div": self.offset_div,
            "bubble_absolute_offset_div": self.absolute_offset_div,
            "bubble_direction": self.direction,
            "calibration_status": self.calibration_status,
            "calibration_created_utc": self.calibration_created_utc,
            "calibration_source": self.calibration_source,
        }
