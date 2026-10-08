"""Fit a new vial calibration from reference captures (① 參考值與連拍).

    python fit_vial.py SESSION_DIR [SESSION_DIR ...] [--exclude BURST_ID ...] [--activate]

Uses each burst's summary row: burst_median_offset_div against the DL-S4W
reading (reference_deg). Least squares:

    reference_mm_per_m = mm_per_m_per_div * (offset_div - zero_offset_div)

offset_div depends on the geometry, so every row must come from the active
geometry version. The new version is written but NOT activated unless
--activate is given (activate later from ③ or the calibration API).
"""

import argparse
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path

from calibration_store import TIMEZONE, CalibrationStore, VialCalibration

CSV_NAME = "pi_capture_log.csv"


def deg_to_mm_per_m(deg):
    return math.tan(math.radians(deg)) * 1000


def mm_per_m_to_deg(mm_per_m):
    return math.degrees(math.atan(mm_per_m / 1000))


def read_points(session_dirs, exclude=()):
    """Summary rows with a reference value, as dicts; excluded burst ids are skipped."""
    exclude = set(exclude)
    points, seen = [], set()
    for directory in session_dirs:
        directory = Path(directory)
        with (directory / CSV_NAME).open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                if row.get("row_type") != "summary":
                    continue
                burst = row["burst_id"]
                seen.add(burst)
                if burst in exclude or not row.get("reference_deg") or not row.get("burst_median_offset_div"):
                    continue
                points.append({"session": directory.name, "burst_id": burst,
                               "reference_deg": float(row["reference_deg"]),
                               "offset_div": float(row["burst_median_offset_div"]),
                               "angle_deg": float(row["burst_median_angle_degrees"]),
                               "geometry_version": row["geometry_version"], "vial_version": row["vial_version"]})
    unknown = exclude - seen
    if unknown:
        raise ValueError(f"excluded burst ids not found: {sorted(unknown)}")
    return points


def fit(points):
    """(mm_per_m_per_div, zero_offset_div) by least squares of mm/m on div."""
    if len({point["offset_div"] for point in points}) < 2:
        raise ValueError("need at least two different bubble positions")
    x = [point["offset_div"] for point in points]
    y = [deg_to_mm_per_m(point["reference_deg"]) for point in points]
    mean_x, mean_y = statistics.fmean(x), statistics.fmean(y)
    gain = (sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
            / sum((a - mean_x) ** 2 for a in x))
    if gain <= 0:
        raise ValueError("fitted gain is not positive")
    return gain, mean_x - mean_y / gain


def errors_deg(points, gain, zero):
    """θsys − θref per point for a given calibration."""
    vial = VialCalibration("check", gain, zero)
    return [mm_per_m_to_deg(vial.slope_mm_per_m(point["offset_div"])) - point["reference_deg"] for point in points]


def error_stats(errors):
    return {"n": len(errors), "mean_deg": statistics.fmean(errors),
            "rms_deg": math.sqrt(statistics.fmean(e * e for e in errors)),
            "max_abs_deg": max(abs(e) for e in errors)}


def leave_one_session_out(points):
    """Fit on the other sessions, test on the held-out one (needs 2+ sessions)."""
    sessions = sorted({point["session"] for point in points})
    if len(sessions) < 2:
        return None
    errors, per_session = [], {}
    for held_out in sessions:
        gain, zero = fit([point for point in points if point["session"] != held_out])
        held = errors_deg([point for point in points if point["session"] == held_out], gain, zero)
        per_session[held_out] = error_stats(held)
        errors += held
    return {"overall": error_stats(errors), "per_session": per_session}


def vial_record(points, store, *, excluded=(), now=None):
    geometries = {point["geometry_version"] for point in points}
    if len(geometries) != 1:
        raise ValueError(f"captures use different geometry versions: {sorted(geometries)}")
    geometry = geometries.pop()
    active_geometry = store.active_state("geometry")["version"]
    if geometry != active_geometry:
        raise ValueError(f"captures use geometry {geometry}, but {active_geometry} is active")
    gain, zero = fit(points)
    previous = store.active_state("vial")["version"]
    old = store.load_vial(previous) if previous else None
    now = now or datetime.now(TIMEZONE)
    return {
        "schema_version": 1,
        "version": store.new_version("vial", now),
        "created_at_iso": now.isoformat(timespec="seconds"),
        "mm_per_m_per_div": gain,
        "zero_offset_div": zero,
        "fit_method": "least squares: reference_mm_per_m = mm_per_m_per_div * (offset_div - zero_offset_div); "
                      "offset_div = burst median from the live system (YOLO box ends, active geometry)",
        "fit_name": "reference_captures",
        "reference_instrument": "LEVELNIC DL-S4W (CNC A axis, 1:1)",
        "source_sessions": sorted({point["session"] for point in points}),
        "excluded_bursts": sorted(excluded),
        "labeled_captures": len(points),
        "reference_angles": len({point["reference_deg"] for point in points}),
        "geometry_version": geometry,
        "fit_residual": error_stats(errors_deg(points, gain, zero)),
        "cross_validation_leave_one_session_out": leave_one_session_out(points),
        "previous_vial_error": (error_stats(errors_deg(points, old.mm_per_m_per_div, old.zero_offset_div))
                                if old else None),
        "previous_version": previous,
    }


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sessions", nargs="+", type=Path, help="logs/manual_captures/<session> directories")
    parser.add_argument("--exclude", nargs="*", default=[], help="burst_id values to leave out")
    parser.add_argument("--activate", action="store_true", help="activate the new version immediately")
    parser.add_argument("--calibration", type=Path, help="calibration directory (default: config)")
    options = parser.parse_args(arguments)
    store = CalibrationStore(options.calibration)
    points = read_points(options.sessions, options.exclude)
    record = vial_record(points, store, excluded=options.exclude)
    path = store.save("vial", record, activate=options.activate)
    residual, previous = record["fit_residual"], record["previous_vial_error"]
    print(f"新 vial：{record['version']}（{'已套用' if options.activate else '未套用'}）→ {path}")
    print(f"  每格 {record['mm_per_m_per_div']:.6f} mm/m、零點 {record['zero_offset_div']:+.4f} 格，"
          f"{record['labeled_captures']} 點")
    print(f"  擬合殘差 RMS {residual['rms_deg']:.6f}°、最大 {residual['max_abs_deg']:.6f}°")
    if previous:
        print(f"  原 vial（{record['previous_version']}）：平均 {previous['mean_deg']:+.6f}°、"
              f"RMS {previous['rms_deg']:.6f}°、最大 {previous['max_abs_deg']:.6f}°")
    cv = record["cross_validation_leave_one_session_out"]
    if cv:
        print(f"  逐回交叉驗證：RMS {cv['overall']['rms_deg']:.6f}°、最大 {cv['overall']['max_abs_deg']:.6f}°")
    return record


if __name__ == "__main__":
    main()
