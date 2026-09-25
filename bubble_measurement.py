"""Convert a detected bubble center into calibrated scale divisions."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BubbleCalibration:
    center_x_roi: float
    pitch_px_per_div: float
    image_width: int
    image_height: int
    status: str
    created_utc: str
    source_path: str

    @classmethod
    def from_json(cls, path, expected_size=None):
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

        return cls(
            center_x_roi=float(center_x),
            pitch_px_per_div=float(pitch),
            image_width=image_width,
            image_height=image_height,
            status=status,
            created_utc=str(data.get("created_utc", "")),
            source_path=str(calibration_path.resolve()),
        )

    def measure(self, bubble_center_x_roi):
        if bubble_center_x_roi in (None, ""):
            return BubbleMeasurement.not_detected(self)

        center_x = float(bubble_center_x_roi)
        offset_px = center_x - self.center_x_roi
        offset_div = offset_px / self.pitch_px_per_div
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
            scale_center_x_roi=self.center_x_roi,
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
