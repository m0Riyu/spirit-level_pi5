"""② camera alignment math: averaged tag angles, screw model, guidance.

The camera mount has a fixed pivot and two spring screws at the corners of a
right triangle; each screw tilts one axis. Tilts show up as the AprilTag's
pitch and yaw. Roll (about the lens axis) cannot be adjusted mechanically and
is shown for reference only. The target is the recorded baseline pose, not
zero: the tag plane need not be parallel to the vial.
"""

import math
from fractions import Fraction

import numpy as np

AXES = ("pitch_deg", "yaw_deg", "roll_deg")
ADJUSTABLE = ("pitch_deg", "yaw_deg")
SCREWS = ("A", "B")


def wrap_deg(value):
    """Angle difference folded into (-180, 180]."""
    folded = math.fmod(value + 180.0, 360.0)
    folded = folded + 360.0 if folded <= 0 else folded
    return folded - 180.0


def average_angles(samples):
    """Circular mean and spread (deg) per axis over per-frame tag summaries."""
    result = {"frames": len(samples)}
    for axis in AXES:
        values = [sample[axis] for sample in samples if sample.get(axis) is not None]
        if not values:
            result[axis], result[f"{axis}_std"] = None, None
            continue
        radians = np.radians(values)
        mean = math.degrees(math.atan2(float(np.sin(radians).mean()), float(np.cos(radians).mean())))
        spread = [wrap_deg(value - mean) for value in values]
        result[axis], result[f"{axis}_std"] = mean, float(np.std(spread))
    return result


def delta_from_baseline(current, baseline):
    return {axis: (None if current.get(axis) is None or baseline.get(axis) is None
                   else wrap_deg(current[axis] - baseline[axis])) for axis in AXES}


def screw_model(teach, turn):
    """Sensitivity matrix from teaching: columns are degrees per full turn.

    teach[screw] = {"before": angles, "after": angles} for a clockwise `turn`.
    """
    columns = []
    for screw in SCREWS:
        change = delta_from_baseline(teach[screw]["after"], teach[screw]["before"])
        columns.append([change[axis] / turn for axis in ADJUSTABLE])
    matrix = np.array(columns).T  # rows: pitch, yaw; columns: A, B
    if abs(np.linalg.det(matrix)) < 1e-9:
        raise ValueError("兩顆螺絲的影響方向幾乎相同，無法建立模型；請重新教學")
    return {"axes": list(ADJUSTABLE), "screws": list(SCREWS), "turn": turn,
            "matrix_deg_per_turn": matrix.tolist(),
            "roll_deg_per_turn": [delta_from_baseline(teach[s]["after"], teach[s]["before"])["roll_deg"] / turn
                                  for s in SCREWS]}


def turns_label(turns, step=Fraction(1, 8)):
    """Human-readable size of a turn, rounded to the nearest 1/8 turn."""
    amount = Fraction(round(abs(turns) / step)) * step
    if amount == 0:
        return "少於 1/8 圈"
    whole, rest = divmod(amount, 1)
    parts = ([f"{whole}"] if whole else []) + ([f"{rest.numerator}/{rest.denominator}"] if rest else [])
    return f"約 {' 又 '.join(parts)} 圈"


def guidance(delta, model, tolerance_deg):
    """Turns per screw that bring pitch/yaw back to the baseline."""
    within = all(delta.get(axis) is not None and abs(delta[axis]) <= tolerance_deg for axis in ADJUSTABLE)
    result = {"within_tolerance": within, "tolerance_deg": tolerance_deg, "screws": [],
              "roll_deg": delta.get("roll_deg"), "roll_note": "機構無法調整，僅供參考"}
    if model is None or any(delta.get(axis) is None for axis in ADJUSTABLE):
        return result
    matrix = np.array(model["matrix_deg_per_turn"])
    error = np.array([delta[axis] for axis in ADJUSTABLE])
    turns = np.linalg.solve(matrix, -error)
    for index, screw in enumerate(SCREWS):
        effect = float(np.max(np.abs(matrix[:, index] * turns[index])))
        ok = effect <= tolerance_deg
        result["screws"].append({
            "screw": screw, "turns": float(turns[index]), "ok": ok,
            "direction": "順時針" if turns[index] > 0 else "逆時針",
            "label": "✓" if ok else f"{'順時針' if turns[index] > 0 else '逆時針'} {turns_label(turns[index])}",
        })
    return result
