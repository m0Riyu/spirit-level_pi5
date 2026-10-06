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

    def measure(self, bubble_center_x_roi, x1_roi=None, x2_roi=None):
        """Divisions of the bubble. With the box ends, offset = (f(x1) + f(x2)) / 2:
        both ends lie on the ticked part of the scale (interpolation), while the
        center lies in the logo gap. For a degree-1 f this equals f(center)."""
        if bubble_center_x_roi in (None, ""):
            return BubbleMeasurement(
                error="bubble_not_detected", scale_center_x_roi=self.zero_x_roi(),
                pitch_px_per_div=self.px_per_div(self.zero_x_roi()), **self._labels(),
            )
        center_x = float(bubble_center_x_roi)
        scale_center = self.zero_x_roi()
        offset_px = center_x - scale_center
        if x1_roi in (None, "") or x2_roi in (None, ""):
            offset_div = self.divisions(center_x)
        else:
            offset_div = (self.divisions(x1_roi) + self.divisions(x2_roi)) / 2
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


def fit_geometry(ticks, *, degree, version, created_at_iso, frames_used=1, roll_deg=None,
                 focus_absolute=None, image_geometry=None, previous=None, source_image="",
                 expected_ticks=26, max_residual_rms_px=0.5, max_pitch_change=0.03):
    """Least squares f(x) = sum c_i (x - x_center)^i with f(x_k) = ±(k + h).

    h (innermost tick to center, in divisions) is fitted, not assumed. All
    ticks enter one fit, so the 0.5 px detection steps average out instead of
    entering a single spacing. Returns the geometry JSON record with checks.
    """
    if not 1 <= int(degree) <= 3:
        raise ValueError("polynomial degree must be 1-3")
    by_key = {(tick["side"], tick["tick_id"]): float(tick["x"]) for tick in ticks}
    pairs = [(by_key[("left", k)] + by_key[("right", k)]) / 2 for side, k in by_key
             if side == "left" and ("right", k) in by_key]
    if len(pairs) < 2 or len(ticks) < degree + 3:
        raise ValueError("too few ticks on both sides to fit the scale")
    x_center = float(np.median(pairs))
    x = np.array([float(tick["x"]) for tick in ticks])
    sign = np.array([-1. if tick["side"] == "left" else 1. for tick in ticks])
    tick_id = np.array([float(tick["tick_id"]) for tick in ticks])
    design = np.column_stack([(x - x_center) ** power for power in range(degree + 1)] + [-sign])
    solution = np.linalg.lstsq(design, sign * tick_id, rcond=None)[0]
    coefficients, inner_half_gap = solution[:-1], float(solution[-1])
    geometry = GeometryCalibration(version, degree, x_center, tuple(coefficients))
    slope = np.array([geometry.derivative(value) for value in x])
    residual_px = (polynomial.polyval(x - x_center, coefficients) - sign * (tick_id + inner_half_gap)) / slope
    rms, worst = float(np.sqrt(np.mean(residual_px ** 2))), float(np.max(np.abs(residual_px)))

    scale_center = geometry.zero_x_roi()
    pitch = geometry.px_per_div(scale_center)
    left_pitch = float(np.mean([geometry.px_per_div(value) for value in x[sign < 0]]))
    right_pitch = float(np.mean([geometry.px_per_div(value) for value in x[sign > 0]]))
    previous_pitch = None
    if previous is not None:
        previous_pitch = previous.px_per_div(previous.zero_x_roi())
    change = None if previous_pitch is None else pitch / previous_pitch - 1
    checks = {
        "tick_count": len(ticks), "expected_tick_count": expected_ticks,
        "tick_count_ok": len(ticks) == expected_ticks,
        "residual_rms_ok": rms < max_residual_rms_px, "max_residual_rms_px": max_residual_rms_px,
        "pitch_change_vs_previous": change, "max_pitch_change": max_pitch_change,
        "pitch_change_ok": change is None or abs(change) < max_pitch_change,
    }
    checks["passed"] = checks["tick_count_ok"] and checks["residual_rms_ok"]
    checks["needs_confirmation"] = checks["passed"] and not checks["pitch_change_ok"]
    return {
        "schema_version": 1,
        "version": version,
        "created_at_iso": created_at_iso,
        "polynomial_degree": int(degree),
        "x_center_px": x_center,
        "coefficients_x_to_div": [float(value) for value in coefficients],
        "inner_half_gap_div": inner_half_gap,
        "tick_positions_px": list(ticks),
        "fit_residual_rms_px": rms,
        "fit_residual_max_px": worst,
        "frames_used": int(frames_used),
        "focus_absolute": focus_absolute,
        "image_geometry": dict(image_geometry or {}),
        "scale_center_x_px": scale_center,
        "px_per_div_center": pitch,
        "previous_px_per_div_center": previous_pitch,
        "left_right_magnification_diff": (left_pitch - right_pitch) / ((left_pitch + right_pitch) / 2),
        "roll_deg": roll_deg,
        "checks": checks,
        "source_image": source_image,
        "previous_version": previous.version if previous is not None else None,
    }
