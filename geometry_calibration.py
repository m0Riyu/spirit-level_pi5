"""Image geometry calibration: rectified ROI pixel x -> scale divisions.

div = f(x) = sum(c_i * (x - x_center) ** i), coefficients in ascending order.
The camera geometry changes whenever the camera moves; the vial calibration
(divisions -> slope) does not, so the two layers are stored separately.
"""

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.polynomial import polynomial

from bubble_measurement import BubbleMeasurement


@dataclass(frozen=True)
class GeometryCalibration:
    version: str
    polynomial_degree: int
    x_center_px: float
    coefficients_x_to_div: tuple
    created_at_iso: str = ""
    status: str = "PASS"
    source_path: str = ""
    image_geometry: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        coefficients = tuple(float(value) for value in self.coefficients_x_to_div)
        if len(coefficients) != int(self.polynomial_degree) + 1:
            raise ValueError("coefficient count must be polynomial_degree + 1")
        if not all(math.isfinite(value) for value in coefficients) or not math.isfinite(self.x_center_px):
            raise ValueError("geometry coefficients must be finite")
        object.__setattr__(self, "coefficients_x_to_div", coefficients)
        if not self.derivative(self.x_center_px) > 0:
            raise ValueError("divisions must increase from left to right")

    @classmethod
    def from_dict(cls, data, source_path=""):
        checks = data.get("checks", {})
        return cls(
            version=str(data["version"]),
            polynomial_degree=int(data["polynomial_degree"]),
            x_center_px=float(data["x_center_px"]),
            coefficients_x_to_div=tuple(data["coefficients_x_to_div"]),
            created_at_iso=str(data.get("created_at_iso", "")),
            status="PASS" if checks.get("passed", True) else "WARN",
            source_path=str(source_path),
            image_geometry=dict(data.get("image_geometry", {})),
        )

    def divisions(self, x_roi):
        return float(polynomial.polyval(float(x_roi) - self.x_center_px, self.coefficients_x_to_div))

    def derivative(self, x_roi):
        slope = polynomial.polyder(self.coefficients_x_to_div)
        return float(polynomial.polyval(float(x_roi) - self.x_center_px, slope))

    def px_per_div(self, x_roi):
        return 1.0 / self.derivative(x_roi)

    def zero_x_roi(self):
        """Pixel where f(x) = 0, found by Newton steps from the scale center."""
        x = self.x_center_px
        for _ in range(20):
            step = self.divisions(x) / self.derivative(x)
            x -= step
            if abs(step) < 1e-12:
                break
        return x

    def measure(self, bubble_center_x_roi):
        if bubble_center_x_roi in (None, ""):
            return BubbleMeasurement(
                error="bubble_not_detected", scale_center_x_roi=self.zero_x_roi(),
                pitch_px_per_div=self.px_per_div(self.zero_x_roi()), **self._labels(),
            )
        center_x = float(bubble_center_x_roi)
        scale_center = self.zero_x_roi()
        offset_px = center_x - scale_center
        offset_div = self.divisions(center_x)
        direction = "right" if offset_div > 0 else "left" if offset_div < 0 else "center"
        return BubbleMeasurement(
            valid=1, center_x_roi=center_x, scale_center_x_roi=scale_center,
            pitch_px_per_div=self.px_per_div(scale_center), offset_px=offset_px,
            offset_div=offset_div, absolute_offset_div=abs(offset_div),
            direction=direction, **self._labels(),
        )

    def _labels(self):
        return {"calibration_status": self.status, "calibration_created_utc": self.created_at_iso,
                "calibration_source": self.source_path}


def tick_divisions(ticks, inner_half_gap_div):
    """Nominal division of each tick: left negative, right positive."""
    return np.array([(-1 if tick["side"] == "left" else 1) * (tick["tick_id"] + inner_half_gap_div)
                     for tick in ticks])


def legacy_tick_geometry(data, *, version, created_at_iso, source_path, previous_version=None):
    """Convert binary_*_tick_measurement.json into a degree-1 geometry record.

    Coefficients reproduce (x - reference_midpoint_x) / global_pitch exactly.
    inner_half_gap_div and residuals are evaluated for that fixed line.
    """
    if data.get("status") not in {"PASS", "WARN"}:
        raise ValueError(f"tick measurement status is {data.get('status') or 'missing'}")
    geometry = data.get("image_geometry", {})
    if geometry.get("coordinate_system") != "undistorted":
        raise ValueError("only undistorted (rectified ROI) tick measurements can be converted")
    x_center = float(data["reference_midpoint_x"])
    pitch = float(data["global_pitch"]["pitch_px"])
    if not pitch > 0:
        raise ValueError("global_pitch.pitch_px must be positive")
    ticks = [{"side": item["side"], "tick_id": int(item["tick_id"]), "x": float(item["x_at_axis"]),
              "x_full_centroid": float(item["x_full_centroid"])}
             for item in data["candidates"] if item.get("valid", True)]
    calibration = GeometryCalibration(version, 1, x_center, (0.0, 1.0 / pitch))
    observed = np.array([calibration.divisions(tick["x"]) for tick in ticks])
    signs = np.array([-1 if tick["side"] == "left" else 1 for tick in ticks])
    inner_half_gap = float(np.mean(signs * observed - [tick["tick_id"] for tick in ticks]))
    residual_px = (observed - tick_divisions(ticks, inner_half_gap)) * pitch
    expected_ticks = 2 * int(data.get("parameters", {}).get("expected_ticks_per_side", 13))
    return {
        "schema_version": 1,
        "version": version,
        "created_at_iso": created_at_iso,
        "polynomial_degree": 1,
        "x_center_px": x_center,
        "coefficients_x_to_div": list(calibration.coefficients_x_to_div),
        "inner_half_gap_div": inner_half_gap,
        "tick_positions_px": ticks,
        "fit_residual_rms_px": float(np.sqrt(np.mean(residual_px ** 2))),
        "fit_residual_max_px": float(np.max(np.abs(residual_px))),
        "frames_used": 1,
        "focus_absolute": None,
        "image_geometry": {**geometry, "frame_size": list(geometry["frame_size"]),
                           "roi_origin": list(geometry["roi_origin"])},
        "checks": {"tick_count": len(ticks), "expected_tick_count": expected_ticks,
                   "passed": len(ticks) == expected_ticks and data["status"] == "PASS"},
        "source_image": "",
        "source_tick_measurement": str(source_path),
        "source_created_utc": str(data.get("created_utc", "")),
        "conversion": "legacy degree-1: (x - reference_midpoint_x) / global_pitch.pitch_px",
        "previous_version": previous_version,
    }
