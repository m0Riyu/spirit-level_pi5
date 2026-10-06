"""Find the vial's scale ticks in the rectified grayscale ROI (no binarization).

Ticks are thin dark vertical lines. A black-hat filter with a horizontal kernel
keeps only features narrower than the kernel; the median over the tick band's
rows rejects curved bubble edges, which cross any column in only a few rows.
Each side's ticks are then followed outwards from the RSK logo gap at the
running pitch, so logo strokes and stray marks are not counted. Positions are
sub-pixel (weighted centroid of the column profile).
"""

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

import config

SIDES = (("left", -1), ("right", 1))


@dataclass
class TickFrame:
    """Ticks of one frame: {(side, tick_id): x}, tick_id 0 is innermost."""
    positions: dict
    roll_deg: object = None
    peaks: list = field(default_factory=list)

    def count(self, side=None):
        return sum(1 for key in self.positions if side is None or key[0] == side)


def column_profile(roi, y1=None, y2=None):
    y1 = config.TICK_BAND_Y1 if y1 is None else y1
    y2 = config.TICK_BAND_Y2 if y2 is None else y2
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (config.TICK_KERNEL_WIDTH_PX, 1))
    hat = cv2.morphologyEx(gray[y1:y2].astype(np.float32), cv2.MORPH_BLACKHAT, kernel)
    return np.median(hat, axis=0), hat


def find_peaks(profile):
    base = float(np.median(profile))
    threshold = base + config.TICK_PEAK_FRACTION * (float(np.percentile(profile, 99.5)) - base)
    peaks = []
    for x in range(4, len(profile) - 4):
        if profile[x] >= threshold and profile[x] == profile[x - 3:x + 4].max():
            weights = np.clip(profile[x - 2:x + 3] - base, 0, None)
            if weights.sum() > 0:
                peaks.append(x + float(np.dot(weights, np.arange(-2, 3)) / weights.sum()))
    return np.array(peaks)


def follow_side(peaks, center, pitch, sign):
    """Walk outwards from the logo gap; a tick hidden by the bubble is skipped."""
    candidates = np.sort(peaks[(peaks - center) * sign > config.TICK_LOGO_HALF_WIDTH_PX])[::sign]
    found = {}
    if not len(candidates):
        return found
    found[0] = last = float(candidates[0])
    for tick_id in range(1, config.TICK_EXPECTED_PER_SIDE):
        expected = last + sign * pitch
        near = candidates[np.abs(candidates - expected) < config.TICK_MATCH_TOLERANCE * pitch]
        if len(near):
            found[tick_id] = last = float(near[np.argmin(np.abs(near - expected))])
            if len(found) > 1:
                ids = sorted(found)
                pitch = abs(float(np.polyfit(ids, [found[i] for i in ids], 1)[0]))
        else:
            last = expected
    return found


def tick_roll_deg(hat, positions):
    """Median lean of the tick lines (+ = lower end further right), degrees."""
    rows = np.arange(hat.shape[0], dtype=float)
    slopes = []
    for x in positions.values():
        left, right = int(round(x)) - 3, int(round(x)) + 4
        if left < 0 or right > hat.shape[1]:
            continue
        window = hat[:, left:right]
        weights = window.sum(axis=1)
        usable = weights > 0
        if usable.sum() < hat.shape[0] // 2:
            continue
        centroid = (window[usable] @ np.arange(left, right)) / weights[usable]
        slopes.append(np.polyfit(rows[usable], centroid, 1)[0])
    return math.degrees(math.atan(float(np.median(slopes)))) if slopes else None


def detect_ticks(roi, prior_center=None, prior_pitch=None):
    """Ticks in one rectified ROI, searched around the expected scale center."""
    center = config.ROI_WIDTH / 2 if prior_center is None else float(prior_center)
    pitch = config.TICK_NOMINAL_PITCH_PX if prior_pitch is None else float(prior_pitch)
    profile, hat = column_profile(roi)
    peaks = find_peaks(profile)
    positions = {}
    for side, sign in SIDES:
        positions.update({(side, tick_id): x for tick_id, x in follow_side(peaks, center, pitch, sign).items()})
    # Second pass around the measured center tolerates a camera that moved.
    pairs = [(positions[("left", k)] + positions[("right", k)]) / 2 for k in range(config.TICK_EXPECTED_PER_SIDE)
             if ("left", k) in positions and ("right", k) in positions]
    if pairs and abs(float(np.median(pairs)) - center) > 1:
        center = float(np.median(pairs))
        positions = {}
        for side, sign in SIDES:
            positions.update({(side, tick_id): x for tick_id, x in follow_side(peaks, center, pitch, sign).items()})
    return TickFrame(positions, tick_roll_deg(hat, positions), peaks.tolist())


def combine_frames(frames):
    """Median position per tick over frames; keep ticks seen in enough frames."""
    keys = sorted({key for frame in frames for key in frame.positions}, key=lambda key: (key[0], key[1]))
    ticks = []
    minimum = max(1, math.ceil(config.TICK_MIN_FRAME_FRACTION * len(frames)))
    for side, tick_id in keys:
        values = np.array([frame.positions[(side, tick_id)] for frame in frames if (side, tick_id) in frame.positions])
        if len(values) >= minimum:
            ticks.append({"side": side, "tick_id": tick_id, "x": float(np.median(values)),
                          "std_px": float(np.std(values)), "frames_found": int(len(values))})
    rolls = [frame.roll_deg for frame in frames if frame.roll_deg is not None]
    return ticks, (float(np.median(rolls)) if rolls else None)
