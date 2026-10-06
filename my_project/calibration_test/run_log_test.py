"""Offline test: compare bubble-center methods and slope calibration on LOG images.

Methods
  A  current system: YOLO box center, nominal 0.02 mm/m per division
  B  YOLO box center + fitted gain/zero offset
  C  ring-tip refined center + fitted gain/zero offset

B and C are scored with leave-one-reference-out cross validation: every
reference angle (with all its repeat captures) is predicted by a fit made
without it, so no point is scored by a calibration that saw it.

Usage:
    python run_log_test.py [session_dir] [reference_csv]
"""

import csv
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

from bubble_edge_refine import refine_bubble

HERE = Path(__file__).resolve().parent
DEFAULT_SESSION = HERE.parent / "logs" / "manual_captures" / "20261002_145946_64bcd440"
DEFAULT_REFERENCE = HERE / "reference_points.csv"
OUTPUT = HERE / "output"

SCALE_CENTER_X = 370.25        # reference_midpoint_x of the tick calibration
PITCH_PX_PER_DIV = 18.0        # global_pitch.pitch_px
NOMINAL_MM_PER_M_PER_DIV = 0.02


def div_to_deg(offset_div, mm_per_m_per_div, zero_offset_div=0.0):
    slope = (offset_div - zero_offset_div) * mm_per_m_per_div
    return math.degrees(math.atan(slope / 1000.0))


def fit_calibration(offset_div, reference_deg):
    """Least squares for reference = gain*(div - zero) in mm/m units."""
    reference_mmm = np.tan(np.radians(reference_deg)) * 1000.0
    gain, intercept = np.polyfit(offset_div, reference_mmm, 1)
    return float(gain), float(-intercept / gain)   # mm/m per div, zero div


def cross_validate(offset_div, reference_deg):
    predictions = np.empty_like(reference_deg)
    for value in np.unique(reference_deg):
        held_out = reference_deg == value
        gain, zero = fit_calibration(offset_div[~held_out], reference_deg[~held_out])
        predictions[held_out] = [div_to_deg(d, gain, zero) for d in offset_div[held_out]]
    return predictions


def stats(error):
    return {"max_abs_deg": float(np.max(np.abs(error))),
            "rms_deg": float(np.sqrt(np.mean(error ** 2))),
            "mean_deg": float(np.mean(error))}


def main():
    session = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SESSION
    reference_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_REFERENCE
    OUTPUT.mkdir(exist_ok=True)

    with reference_path.open(encoding="utf-8") as file:
        references = {int(r["sample_id"]): r for r in csv.DictReader(file)
                      if r["session_id"] == session.name}
    with (session / "pi_capture_log.csv").open(encoding="utf-8") as file:
        log_rows = list(csv.DictReader(file))

    records = []
    for row in log_rows:
        sample_id = int(row["sample_id"])
        image = cv2.imread(str(session / row["clean_image_path"]))
        box = [float(row[k]) for k in ("x1_roi", "y1_roi", "x2_roi", "y2_roi")]
        refined = refine_bubble(image, *box)
        yolo_center = (box[0] + box[2]) / 2
        reference = references.get(sample_id, {})
        records.append({
            "sample_id": sample_id,
            "dl_s4w_deg": float(reference["dl_s4w_deg"]) if reference.get("dl_s4w_deg") else None,
            "log_angle_deg": float(row["angle_degrees"]),
            "yolo_center_x": yolo_center,
            "yolo_width_px": box[2] - box[0],
            "ring_valid": refined.valid,
            "ring_error": refined.error,
            "ring_center_x": refined.center_x,
            "ring_length_px": refined.length_px,
            "yolo_div": (yolo_center - SCALE_CENTER_X) / PITCH_PX_PER_DIV,
            "ring_div": (refined.center_x - SCALE_CENTER_X) / PITCH_PX_PER_DIV,
        })

    labeled = [r for r in records if r["dl_s4w_deg"] is not None and r["ring_valid"]]
    reference_deg = np.array([r["dl_s4w_deg"] for r in labeled])
    yolo_div = np.array([r["yolo_div"] for r in labeled])
    ring_div = np.array([r["ring_div"] for r in labeled])

    method_a = np.array([div_to_deg(d, NOMINAL_MM_PER_M_PER_DIV) for d in yolo_div])
    method_b = cross_validate(yolo_div, reference_deg)
    method_c = cross_validate(ring_div, reference_deg)
    for record, a, b, c in zip(labeled, method_a, method_b, method_c):
        record.update(method_a_deg=a, method_b_cv_deg=b, method_c_cv_deg=c)

    yolo_gain, yolo_zero = fit_calibration(yolo_div, reference_deg)
    ring_gain, ring_zero = fit_calibration(ring_div, reference_deg)

    def repeatability(key):
        spreads = []
        for value in np.unique(reference_deg):
            group = [r[key] for r in labeled if r["dl_s4w_deg"] == value]
            if len(group) > 1:
                spreads.append(max(group) - min(group))
        return max(spreads) if spreads else 0.0

    summary = {
        "session": session.name,
        "labeled_captures": len(labeled),
        "reference_angles": int(len(np.unique(reference_deg))),
        "method_A_current_system": stats(method_a - reference_deg),
        "method_B_yolo_calibrated_cv": stats(method_b - reference_deg),
        "method_C_ring_calibrated_cv": stats(method_c - reference_deg),
        "calibration_all_points": {
            "yolo": {"mm_per_m_per_div": yolo_gain, "zero_offset_div": yolo_zero},
            "ring": {"mm_per_m_per_div": ring_gain, "zero_offset_div": ring_zero},
        },
        "self_consistency": {
            "yolo_width_std_px": float(np.std([r["yolo_width_px"] for r in records])),
            "ring_length_std_px": float(np.std([r["ring_length_px"] for r in records])),
            "yolo_repeat_spread_px": repeatability("yolo_center_x"),
            "ring_repeat_spread_px": repeatability("ring_center_x"),
        },
    }

    fields = list(records[0].keys()) + ["method_a_deg", "method_b_cv_deg", "method_c_cv_deg"]
    with (OUTPUT / "per_capture.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"{'id':>3} {'ref':>8} {'A now':>8} {'B yolo':>8} {'C ring':>8}   err A / B / C (1e-4 deg)")
    for r in labeled:
        ref = r["dl_s4w_deg"]
        print(f"{r['sample_id']:>3} {ref:+.4f} {r['method_a_deg']:+.5f} {r['method_b_cv_deg']:+.5f} "
              f"{r['method_c_cv_deg']:+.5f}   {(r['method_a_deg'] - ref) * 1e4:+5.1f} "
              f"{(r['method_b_cv_deg'] - ref) * 1e4:+5.1f} {(r['method_c_cv_deg'] - ref) * 1e4:+5.1f}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
