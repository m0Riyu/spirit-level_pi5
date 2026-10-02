#!/usr/bin/env python3
"""Calibrate spirit-level tick positions from a cropped tube ROI.

The script accepts either a normal grayscale/BGR image or an already-binary
image. It detects vertical tick marks by summing dark pixels along the Y axis,
excludes the central logo, and selects the long valid tick nearest each side of
the logo as a reference. Long ticks remain an internal classification and are
not drawn in green.

1. A JSON calibration file.
2. An annotated preview image.
3. The binary mask used by the detector.

Coordinates in the JSON are relative to the input image.  For the current
system, feed the same 740 x 160 ROI that is cropped from the 960 x 540 runtime
image.  If a logo detector is already available, pass its X bounds through
--logo-x1 and --logo-x2 for the most deterministic result.

Example:
    python3 tick_scale_calibration.py binary_roi.png --binary

    python3 tick_scale_calibration.py roi.png \
        --logo-x1 305 --logo-x2 435 --show
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import cv2
import numpy as np


@dataclass
class TickCandidate:
    x: float
    x_start: int
    x_end: int
    width: int
    vertical_score: int
    total_dark_pixels: int
    side: str
    is_long: bool
    selected_reference: bool = False
    tick_id: Optional[int] = None
    x_at_axis: Optional[float] = None
    x_full_centroid: Optional[float] = None
    pair_center_x: Optional[float] = None
    valid: bool = True
    invalid_reason: str = ""


def find_runs(active: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive start/end indices of True runs in a 1-D array."""
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None

    for index, value in enumerate(active.astype(bool)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index - 1))
            start = None

    if start is not None:
        runs.append((start, len(active) - 1))

    return runs


def make_dark_mask(
    gray: np.ndarray,
    mode: str,
    adaptive_block_size: int,
    adaptive_c: float,
    vertical_open_height: int,
) -> tuple[np.ndarray, str]:
    """Return a uint8 mask in which dark image features are white (255)."""
    actual_mode = mode

    if mode == "auto":
        unique_values = np.unique(gray)
        looks_binary = (
            len(unique_values) <= 4
            and int(unique_values.min()) <= 5
            and int(unique_values.max()) >= 250
        )
        actual_mode = "binary" if looks_binary else "otsu"

    if actual_mode == "binary":
        mask = np.where(gray < 128, 255, 0).astype(np.uint8)
    elif actual_mode == "otsu":
        _, mask = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
    elif actual_mode == "adaptive":
        block_size = max(3, int(adaptive_block_size))
        if block_size % 2 == 0:
            block_size += 1
        mask = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block_size,
            adaptive_c,
        )
    else:
        raise ValueError(f"Unsupported threshold mode: {mode}")

    if vertical_open_height > 1:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (1, int(vertical_open_height))
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    return mask, actual_mode


def detect_logo_exclusion(
    column_score: np.ndarray,
    active_runs: Sequence[tuple[int, int]],
    image_width: int,
    max_line_width: int,
    logo_margin: int,
    provided_x1: Optional[int],
    provided_x2: Optional[int],
) -> tuple[int, int, str]:
    """Find or accept the central logo X interval."""
    if (provided_x1 is None) != (provided_x2 is None):
        raise ValueError("--logo-x1 and --logo-x2 must be supplied together")

    if provided_x1 is not None and provided_x2 is not None:
        raw_x1, raw_x2 = sorted((int(provided_x1), int(provided_x2)))
        source = "provided"
    else:
        central_min = int(round(image_width * 0.25))
        central_max = int(round(image_width * 0.75))
        wide_runs = []

        for x_start, x_end in active_runs:
            width = x_end - x_start + 1
            center = (x_start + x_end) / 2.0
            if width > max_line_width and central_min <= center <= central_max:
                darkness = int(column_score[x_start : x_end + 1].sum())
                wide_runs.append((darkness, width, x_start, x_end))

        if wide_runs:
            _, _, raw_x1, raw_x2 = max(wide_runs)
            source = "auto_wide_region"
        else:
            # Safe fallback for a centered logo.  A warning is written to JSON.
            raw_x1 = int(round(image_width * 0.42))
            raw_x2 = int(round(image_width * 0.58))
            source = "fallback_center_band"

    x1 = max(0, raw_x1 - int(logo_margin))
    x2 = min(image_width - 1, raw_x2 + int(logo_margin))
    return x1, x2, source


def weighted_x_center(
    column_score: np.ndarray, x_start: int, x_end: int
) -> float:
    xs = np.arange(x_start, x_end + 1, dtype=np.float64)
    weights = column_score[x_start : x_end + 1].astype(np.float64)
    weight_sum = float(weights.sum())
    if weight_sum <= 0:
        return (x_start + x_end) / 2.0
    return float(np.dot(xs, weights) / weight_sum)


def extract_candidates(
    mask: np.ndarray,
    column_score: np.ndarray,
    runs: Sequence[tuple[int, int]],
    logo_x1: int,
    logo_x2: int,
    max_line_width: int,
    long_line_score: int,
    axis_y: int,
    axis_half_height: int,
) -> list[TickCandidate]:
    candidates: list[TickCandidate] = []

    for x_start, x_end in runs:
        if x_end >= logo_x1 and x_start <= logo_x2:
            continue

        width = x_end - x_start + 1
        if width > max_line_width:
            continue

        score_slice = column_score[x_start : x_end + 1]
        vertical_score = int(score_slice.max())
        total_dark_pixels = int(score_slice.sum())
        x_center = weighted_x_center(column_score, x_start, x_end)

        axis_y1 = max(0, axis_y - axis_half_height)
        axis_y2 = min(mask.shape[0], axis_y + axis_half_height + 1)
        axis_score = (mask[axis_y1:axis_y2, x_start : x_end + 1] > 0).sum(
            axis=0
        )
        axis_weight = int(axis_score.sum())
        if axis_weight > 0:
            axis_xs = np.arange(x_start, x_end + 1, dtype=np.float64)
            x_at_axis: Optional[float] = float(
                np.dot(axis_xs, axis_score.astype(np.float64)) / axis_weight
            )
            valid = True
            invalid_reason = ""
        else:
            x_at_axis = None
            valid = False
            invalid_reason = "no_dark_pixels_at_axis"

        if x_center < logo_x1:
            side = "left"
        elif x_center > logo_x2:
            side = "right"
        else:
            continue

        candidates.append(
            TickCandidate(
                x=x_center,
                x_start=x_start,
                x_end=x_end,
                width=width,
                vertical_score=vertical_score,
                total_dark_pixels=total_dark_pixels,
                side=side,
                is_long=vertical_score >= long_line_score,
                x_at_axis=x_at_axis,
                x_full_centroid=x_center,
                valid=valid,
                invalid_reason=invalid_reason,
            )
        )

    return sorted(candidates, key=lambda item: item.x)


def assign_tick_ids(
    candidates: Sequence[TickCandidate], expected_ticks_per_side: int
) -> None:
    """Assign matching IDs from the logo outward on the left and right."""
    for side in ("left", "right"):
        usable = [
            item for item in candidates if item.side == side and item.valid
        ]
        usable.sort(key=lambda item: item.x, reverse=(side == "left"))

        for tick_id, candidate in enumerate(usable):
            if tick_id >= expected_ticks_per_side:
                candidate.valid = False
                candidate.invalid_reason = "extra_tick_beyond_expected_count"
                candidate.tick_id = None
                continue
            candidate.tick_id = tick_id


def describe_values(values: Sequence[float]) -> dict[str, Any]:
    """Return JSON-safe spatial statistics for a set of values."""
    array = np.asarray(list(values), dtype=np.float64)
    if len(array) == 0:
        return {
            "count": 0,
            "mean_px": None,
            "median_px": None,
            "std_px": None,
            "min_px": None,
            "max_px": None,
            "range_px": None,
            "coefficient_of_variation": None,
            "median_absolute_deviation_px": None,
        }

    mean = float(np.mean(array))
    median = float(np.median(array))
    return {
        "count": int(len(array)),
        "mean_px": mean,
        "median_px": median,
        "std_px": float(np.std(array)),
        "min_px": float(np.min(array)),
        "max_px": float(np.max(array)),
        "range_px": float(np.ptp(array)),
        "coefficient_of_variation": (
            float(np.std(array) / mean) if mean != 0 else None
        ),
        "median_absolute_deviation_px": float(
            np.median(np.abs(array - median))
        ),
    }


def build_local_gaps(
    candidates: Sequence[TickCandidate], expected_ticks_per_side: int
) -> dict[str, list[dict[str, Any]]]:
    """Build exactly expected_ticks_per_side - 1 local gaps per side."""
    output: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}

    for region in ("left", "right"):
        by_id = {
            item.tick_id: item
            for item in candidates
            if item.side == region and item.valid and item.tick_id is not None
        }
        for gap_id in range(1, expected_ticks_per_side):
            inner_tick = by_id.get(gap_id - 1)
            outer_tick = by_id.get(gap_id)
            valid = inner_tick is not None and outer_tick is not None
            invalid_reason = ""
            gap_px: Optional[float] = None

            if valid:
                if region == "left":
                    gap_px = inner_tick.x_at_axis - outer_tick.x_at_axis
                else:
                    gap_px = outer_tick.x_at_axis - inner_tick.x_at_axis
                if gap_px is None or gap_px <= 0:
                    valid = False
                    invalid_reason = "non_positive_gap"
                    gap_px = None
            else:
                invalid_reason = "missing_tick_for_gap"

            output[region].append(
                {
                    "gap_id": gap_id,
                    "region": region,
                    "inner_tick_id": gap_id - 1,
                    "outer_tick_id": gap_id,
                    "gap_px": gap_px,
                    "valid": valid,
                    "invalid_reason": invalid_reason,
                }
            )

    return output


def build_pair_centers(
    candidates: Sequence[TickCandidate], expected_ticks_per_side: int
) -> list[dict[str, Any]]:
    """Pair left/right ticks with the same ID and calculate their midpoint."""
    lookup = {
        (item.side, item.tick_id): item
        for item in candidates
        if item.valid and item.tick_id is not None
    }
    pairs: list[dict[str, Any]] = []

    for tick_id in range(expected_ticks_per_side):
        left = lookup.get(("left", tick_id))
        right = lookup.get(("right", tick_id))
        valid = left is not None and right is not None
        pair_center_x = (
            (left.x_at_axis + right.x_at_axis) / 2.0 if valid else None
        )
        invalid_reason = "" if valid else "missing_left_or_right_tick"

        if valid:
            left.pair_center_x = pair_center_x
            right.pair_center_x = pair_center_x

        pairs.append(
            {
                "tick_id": tick_id,
                "left_x_at_axis": None if left is None else left.x_at_axis,
                "right_x_at_axis": None if right is None else right.x_at_axis,
                "pair_center_x": pair_center_x,
                "valid": valid,
                "invalid_reason": invalid_reason,
            }
        )

    return pairs


def estimate_pitch(
    positions: Sequence[float], spacing_min: float, spacing_max: float
) -> dict[str, Any]:
    """Estimate local pixels/division using robust adjacent differences."""
    xs = np.asarray(sorted(positions), dtype=np.float64)
    if len(xs) < 2:
        return {
            "pitch_px": None,
            "median_absolute_deviation_px": None,
            "std_px": None,
            "valid_interval_count": 0,
            "intervals_px": [],
        }

    raw_differences = np.diff(xs)
    direct = raw_differences[
        (raw_differences >= spacing_min) & (raw_differences <= spacing_max)
    ]

    if len(direct) == 0:
        return {
            "pitch_px": None,
            "median_absolute_deviation_px": None,
            "std_px": None,
            "valid_interval_count": 0,
            "intervals_px": [float(value) for value in raw_differences],
        }

    initial_pitch = float(np.median(direct))
    normalized_intervals: list[float] = []

    # Recover a missing tick: a 2*pitch or 3*pitch gap is divided by its
    # nearest integer number of divisions before the final robust estimate.
    for difference in raw_differences:
        division_count = max(1, int(round(float(difference) / initial_pitch)))
        adjusted = float(difference) / division_count
        if spacing_min <= adjusted <= spacing_max:
            normalized_intervals.append(adjusted)

    values = np.asarray(normalized_intervals, dtype=np.float64)
    pitch = float(np.median(values))
    mad = float(np.median(np.abs(values - pitch)))
    std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0

    return {
        "pitch_px": pitch,
        "median_absolute_deviation_px": mad,
        "std_px": std,
        "valid_interval_count": int(len(values)),
        "intervals_px": [float(value) for value in values],
    }


def choose_reference_ticks(
    candidates: Sequence[TickCandidate], logo_x1: int, logo_x2: int
) -> tuple[Optional[TickCandidate], Optional[TickCandidate]]:
    left_long = [
        item
        for item in candidates
        if item.side == "left" and item.is_long and item.valid
    ]
    right_long = [
        item
        for item in candidates
        if item.side == "right" and item.is_long and item.valid
    ]

    # Use the long valid tick closest to each side of the fixed logo range.
    left_reference = max(left_long, key=lambda item: item.x, default=None)
    right_reference = min(right_long, key=lambda item: item.x, default=None)

    if left_reference is not None:
        left_reference.selected_reference = True
    if right_reference is not None:
        right_reference.selected_reference = True

    return left_reference, right_reference


def calibrate_ticks(
    image: np.ndarray,
    *,
    tick_y1: Optional[int] = None,
    tick_y2: Optional[int] = None,
    logo_x1: Optional[int] = None,
    logo_x2: Optional[int] = None,
    logo_margin: int = 10,
    threshold_mode: str = "auto",
    adaptive_block_size: int = 31,
    adaptive_c: float = 7.0,
    vertical_open_height: int = 3,
    min_column_score: int = 12,
    max_line_width: int = 8,
    long_line_score: int = 60,
    spacing_min: float = 10.0,
    spacing_max: float = 40.0,
    min_ticks_per_side: int = 3,
    axis_y: Optional[int] = None,
    axis_half_height: int = 2,
    expected_ticks_per_side: int = 13,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    """Run tick calibration and return result, annotated image, and mask."""
    if image is None or image.size == 0:
        raise ValueError("Input image is empty")

    if image.ndim == 2:
        gray = image.copy()
        annotated = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    elif image.ndim == 3 and image.shape[2] == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        annotated = image.copy()
    elif image.ndim == 3 and image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        annotated = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    else:
        raise ValueError(f"Unsupported image shape: {image.shape}")

    height, width = gray.shape
    y1 = int(round(height * 0.1875)) if tick_y1 is None else int(tick_y1)
    y2 = int(round(height * 0.78125)) if tick_y2 is None else int(tick_y2)
    y1 = max(0, min(height - 1, y1))
    y2 = max(y1 + 1, min(height, y2))
    resolved_axis_y = height // 2 if axis_y is None else int(axis_y)
    resolved_axis_y = max(0, min(height - 1, resolved_axis_y))
    resolved_axis_half_height = max(0, int(axis_half_height))
    expected_ticks_per_side = max(2, int(expected_ticks_per_side))

    mask, actual_threshold_mode = make_dark_mask(
        gray,
        threshold_mode,
        adaptive_block_size,
        adaptive_c,
        vertical_open_height,
    )

    band_mask = mask[y1:y2, :]
    column_score = (band_mask > 0).sum(axis=0).astype(np.int32)
    active = column_score >= int(min_column_score)
    initial_runs = find_runs(active)

    detected_logo_x1, detected_logo_x2, logo_source = detect_logo_exclusion(
        column_score,
        initial_runs,
        width,
        max_line_width,
        logo_margin,
        logo_x1,
        logo_x2,
    )

    candidates = extract_candidates(
        mask,
        column_score,
        initial_runs,
        detected_logo_x1,
        detected_logo_x2,
        max_line_width,
        long_line_score,
        resolved_axis_y,
        resolved_axis_half_height,
    )
    assign_tick_ids(candidates, expected_ticks_per_side)

    left_candidates = [
        item for item in candidates if item.side == "left" and item.valid
    ]
    right_candidates = [
        item for item in candidates if item.side == "right" and item.valid
    ]

    left_pitch = estimate_pitch(
        [item.x_at_axis for item in left_candidates], spacing_min, spacing_max
    )
    right_pitch = estimate_pitch(
        [item.x_at_axis for item in right_candidates], spacing_min, spacing_max
    )
    global_pitch = estimate_pitch(
        [
            item.x_at_axis
            for item in candidates
            if item.valid and item.x_at_axis is not None
        ],
        spacing_min,
        spacing_max,
    )

    left_reference, right_reference = choose_reference_ticks(
        candidates, detected_logo_x1, detected_logo_x2
    )

    zero_x: Optional[float] = None
    reference_span_px: Optional[float] = None
    if left_reference is not None and right_reference is not None:
        zero_x = (
            left_reference.x_at_axis + right_reference.x_at_axis
        ) / 2.0
        reference_span_px = (
            right_reference.x_at_axis - left_reference.x_at_axis
        )

    gaps = build_local_gaps(candidates, expected_ticks_per_side)
    pair_centers = build_pair_centers(candidates, expected_ticks_per_side)
    valid_pair_centers = [
        item["pair_center_x"] for item in pair_centers if item["valid"]
    ]
    inner_center_x = pair_centers[0]["pair_center_x"]
    all_pairs_center_x = (
        float(np.mean(valid_pair_centers)) if valid_pair_centers else None
    )
    pair_center_std_px = (
        float(np.std(valid_pair_centers)) if valid_pair_centers else None
    )

    left_gap_values = [
        item["gap_px"] for item in gaps["left"] if item["valid"]
    ]
    right_gap_values = [
        item["gap_px"] for item in gaps["right"] if item["valid"]
    ]
    left_right_gap_differences = []
    for left_gap, right_gap in zip(gaps["left"], gaps["right"]):
        valid = left_gap["valid"] and right_gap["valid"]
        difference = (
            left_gap["gap_px"] - right_gap["gap_px"] if valid else None
        )
        left_right_gap_differences.append(
            {
                "gap_id": left_gap["gap_id"],
                "left_gap_px": left_gap["gap_px"],
                "right_gap_px": right_gap["gap_px"],
                "difference_px": difference,
                "absolute_difference_px": (
                    abs(difference) if difference is not None else None
                ),
                "valid": valid,
            }
        )

    axis_centroid_offsets = [
        item.x_at_axis - item.x_full_centroid
        for item in candidates
        if item.valid
        and item.x_at_axis is not None
        and item.x_full_centroid is not None
    ]
    absolute_gap_differences = [
        item["absolute_difference_px"]
        for item in left_right_gap_differences
        if item["valid"]
    ]
    spatial_consistency = {
        "expected_ticks_per_side": expected_ticks_per_side,
        "valid_tick_count": sum(item.valid for item in candidates),
        "invalid_tick_count": sum(not item.valid for item in candidates),
        "left_gaps": describe_values(left_gap_values),
        "right_gaps": describe_values(right_gap_values),
        "all_gaps": describe_values(left_gap_values + right_gap_values),
        "left_right_gap_differences": left_right_gap_differences,
        "left_right_gap_absolute_difference": describe_values(
            absolute_gap_differences
        ),
        "pair_centers": describe_values(valid_pair_centers),
        "axis_minus_full_centroid": describe_values(axis_centroid_offsets),
    }

    warnings: list[str] = []
    errors: list[str] = []

    if logo_source == "fallback_center_band":
        warnings.append(
            "Logo could not be detected automatically; a centered fallback band was used."
        )
    if len(left_candidates) < min_ticks_per_side:
        errors.append("Too few left-side tick candidates.")
    if len(right_candidates) < min_ticks_per_side:
        errors.append("Too few right-side tick candidates.")
    if len(left_candidates) != expected_ticks_per_side:
        errors.append(
            f"Expected {expected_ticks_per_side} valid left-side ticks; "
            f"found {len(left_candidates)}."
        )
    if len(right_candidates) != expected_ticks_per_side:
        errors.append(
            f"Expected {expected_ticks_per_side} valid right-side ticks; "
            f"found {len(right_candidates)}."
        )
    invalid_candidates = [item for item in candidates if not item.valid]
    if invalid_candidates:
        warnings.append(
            f"{len(invalid_candidates)} tick candidates were marked invalid."
        )
    if left_pitch["pitch_px"] is None:
        errors.append("Left-side pixels/division could not be estimated.")
    if right_pitch["pitch_px"] is None:
        errors.append("Right-side pixels/division could not be estimated.")
    if left_reference is None:
        errors.append("Left long reference tick was not found.")
    if right_reference is None:
        errors.append("Right long reference tick was not found.")

    if left_pitch["pitch_px"] is not None and right_pitch["pitch_px"] is not None:
        mean_pitch = (left_pitch["pitch_px"] + right_pitch["pitch_px"]) / 2.0
        relative_difference = abs(
            left_pitch["pitch_px"] - right_pitch["pitch_px"]
        ) / mean_pitch
        if relative_difference > 0.05:
            warnings.append(
                "Left/right pixels-per-division differ by more than 5%; "
                "check perspective, crop, or false tick candidates."
            )

    if zero_x is not None:
        logo_center_x = (detected_logo_x1 + detected_logo_x2) / 2.0
        pitch_for_check = global_pitch["pitch_px"]
        if (
            pitch_for_check is not None
            and abs(zero_x - logo_center_x) > 0.5 * pitch_for_check
        ):
            warnings.append(
                "Reference-tick midpoint differs from logo center by more than half a division."
            )

    if errors:
        status = "FAIL"
    elif warnings:
        status = "WARN"
    else:
        status = "PASS"

    # Annotated output: blue=valid tick, yellow=selected reference,
    # red=invalid tick, magenta=reference midpoint.
    cv2.rectangle(
        annotated,
        (0, y1),
        (width - 1, y2 - 1),
        (255, 255, 0),
        1,
    )
    cv2.rectangle(
        annotated,
        (detected_logo_x1, y1),
        (detected_logo_x2, y2 - 1),
        (0, 0, 255),
        1,
    )
    cv2.line(
        annotated,
        (0, resolved_axis_y),
        (width - 1, resolved_axis_y),
        (128, 128, 128),
        1,
    )

    for candidate in candidates:
        x_value = (
            candidate.x_at_axis
            if candidate.x_at_axis is not None
            else candidate.x_full_centroid
        )
        x_draw = int(round(x_value))
        if not candidate.valid:
            color = (0, 0, 255)
            thickness = 1
        elif candidate.selected_reference:
            color = (0, 255, 255)
            thickness = 3
        else:
            color = (255, 0, 0)
            thickness = 1
        cv2.line(annotated, (x_draw, y1), (x_draw, y2 - 1), color, thickness)
        cv2.circle(annotated, (x_draw, resolved_axis_y), 2, color, -1)

    if zero_x is not None:
        zero_draw = int(round(zero_x))
        cv2.line(
            annotated,
            (zero_draw, max(0, y1 - 10)),
            (zero_draw, min(height - 1, y2 + 10)),
            (255, 0, 255),
            2,
        )

    result: dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "image_width": width,
        "image_height": height,
        "tick_band": {"y1": y1, "y2": y2},
        "axis_y": resolved_axis_y,
        "axis_half_height": resolved_axis_half_height,
        "threshold_mode": actual_threshold_mode,
        "logo_exclusion": {
            "x1": detected_logo_x1,
            "x2": detected_logo_x2,
            "source": logo_source,
            "margin_px": int(logo_margin),
        },
        "parameters": {
            "min_column_score": int(min_column_score),
            "max_line_width": int(max_line_width),
            "long_line_score": int(long_line_score),
            "spacing_min_px": float(spacing_min),
            "spacing_max_px": float(spacing_max),
            "vertical_open_height": int(vertical_open_height),
            "expected_ticks_per_side": expected_ticks_per_side,
        },
        "candidate_count": len(candidates),
        "left_candidate_count": len(left_candidates),
        "right_candidate_count": len(right_candidates),
        "candidates": [asdict(item) for item in candidates],
        "left_pitch": left_pitch,
        "right_pitch": right_pitch,
        "global_pitch": global_pitch,
        "left_reference_x": (
            None if left_reference is None else left_reference.x_at_axis
        ),
        "right_reference_x": (
            None if right_reference is None else right_reference.x_at_axis
        ),
        "left_reference_full_centroid_x": (
            None if left_reference is None else left_reference.x_full_centroid
        ),
        "right_reference_full_centroid_x": (
            None if right_reference is None else right_reference.x_full_centroid
        ),
        "reference_midpoint_x": zero_x,
        "reference_span_px": reference_span_px,
        "gaps": gaps,
        "pair_centers": pair_centers,
        "inner_center_x": inner_center_x,
        "all_pairs_center_x": all_pairs_center_x,
        "pair_center_std_px": pair_center_std_px,
        "spatial_consistency": spatial_consistency,
        "warnings": warnings,
        "errors": errors,
    }

    return result, annotated, mask


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect spirit-level scale ticks and create a calibration JSON."
    )
    parser.add_argument("image", type=Path, help="Cropped tube ROI image")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: <image folder>/tick_calibration_output)",
    )
    parser.add_argument("--tick-y1", type=int, default=None)
    parser.add_argument("--tick-y2", type=int, default=None)
    parser.add_argument("--logo-x1", type=int, default=None)
    parser.add_argument("--logo-x2", type=int, default=None)
    parser.add_argument("--logo-margin", type=int, default=10)
    parser.add_argument(
        "--threshold-mode",
        choices=("auto", "binary", "otsu", "adaptive"),
        default="auto",
    )
    parser.add_argument(
        "--binary",
        action="store_true",
        help="Shortcut for --threshold-mode binary",
    )
    parser.add_argument("--adaptive-block-size", type=int, default=31)
    parser.add_argument("--adaptive-c", type=float, default=7.0)
    parser.add_argument("--vertical-open-height", type=int, default=3)
    parser.add_argument("--min-column-score", type=int, default=12)
    parser.add_argument("--max-line-width", type=int, default=8)
    parser.add_argument("--long-line-score", type=int, default=60)
    parser.add_argument("--spacing-min", type=float, default=10.0)
    parser.add_argument("--spacing-max", type=float, default=40.0)
    parser.add_argument("--min-ticks-per-side", type=int, default=3)
    parser.add_argument(
        "--axis-y",
        type=int,
        default=None,
        help="Measurement axis Y coordinate (default: image center)",
    )
    parser.add_argument("--axis-half-height", type=int, default=2)
    parser.add_argument("--expected-ticks-per-side", type=int, default=13)
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the annotated image in an OpenCV window",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    image_path = args.image.expanduser().resolve()

    if not image_path.is_file():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 2

    image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        print(f"ERROR: OpenCV could not read: {image_path}", file=sys.stderr)
        return 2

    threshold_mode = "binary" if args.binary else args.threshold_mode

    try:
        result, annotated, mask = calibrate_ticks(
            image,
            tick_y1=args.tick_y1,
            tick_y2=args.tick_y2,
            logo_x1=args.logo_x1,
            logo_x2=args.logo_x2,
            logo_margin=args.logo_margin,
            threshold_mode=threshold_mode,
            adaptive_block_size=args.adaptive_block_size,
            adaptive_c=args.adaptive_c,
            vertical_open_height=args.vertical_open_height,
            min_column_score=args.min_column_score,
            max_line_width=args.max_line_width,
            long_line_score=args.long_line_score,
            spacing_min=args.spacing_min,
            spacing_max=args.spacing_max,
            min_ticks_per_side=args.min_ticks_per_side,
            axis_y=args.axis_y,
            axis_half_height=args.axis_half_height,
            expected_ticks_per_side=args.expected_ticks_per_side,
        )
    except (ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    result["source_image"] = str(image_path)

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else image_path.parent / "tick_calibration_output"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = image_path.stem
    json_path = output_dir / f"{stem}_tick_calibration.json"
    preview_path = output_dir / f"{stem}_tick_analysis.png"
    mask_path = output_dir / f"{stem}_tick_mask.png"

    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not cv2.imwrite(str(preview_path), annotated):
        print(f"ERROR: failed to save preview: {preview_path}", file=sys.stderr)
        return 3
    if not cv2.imwrite(str(mask_path), mask):
        print(f"ERROR: failed to save mask: {mask_path}", file=sys.stderr)
        return 3

    print(f"status                : {result['status']}")
    print(
        "logo exclusion        : "
        f"{result['logo_exclusion']['x1']} .. {result['logo_exclusion']['x2']} "
        f"({result['logo_exclusion']['source']})"
    )
    print(
        "tick candidates       : "
        f"left={result['left_candidate_count']}, "
        f"right={result['right_candidate_count']}"
    )
    print(f"left reference x      : {result['left_reference_x']}")
    print(f"right reference x     : {result['right_reference_x']}")
    print(f"reference midpoint x  : {result['reference_midpoint_x']}")
    print(f"left pitch px/div     : {result['left_pitch']['pitch_px']}")
    print(f"right pitch px/div    : {result['right_pitch']['pitch_px']}")
    print(f"global pitch px/div   : {result['global_pitch']['pitch_px']}")
    print(f"axis y +/- height     : {result['axis_y']} +/- {result['axis_half_height']}")
    print(f"inner center x        : {result['inner_center_x']}")
    print(f"all-pairs center x    : {result['all_pairs_center_x']}")
    print(f"pair center std px    : {result['pair_center_std_px']}")
    print(
        "valid local gaps      : "
        f"left={sum(item['valid'] for item in result['gaps']['left'])}/12, "
        f"right={sum(item['valid'] for item in result['gaps']['right'])}/12"
    )

    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    for error in result["errors"]:
        print(f"ERROR: {error}")

    print(f"JSON                   : {json_path}")
    print(f"annotated image        : {preview_path}")
    print(f"binary mask            : {mask_path}")

    if args.show:
        cv2.imshow("Tick calibration", annotated)
        cv2.imshow("Tick binary mask", mask)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    return 0 if result["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
