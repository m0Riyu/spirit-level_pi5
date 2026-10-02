"""Bounded scalar-only stability statistics; independent of level/range state."""

import math
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import NamedTuple


class ScalarSample(NamedTuple):
    timestamp: float
    valid: bool
    slope_mm_per_m: object
    offset_div: object
    confidence: object


@dataclass(frozen=True)
class StabilityResult:
    state: str
    stable: bool
    reason: str
    sample_count: int
    valid_count: int
    valid_ratio: float
    mean_slope_mm_per_m: object
    std_slope_mm_per_m: object
    range_slope_mm_per_m: object
    stable_duration_seconds: float

    def as_dict(self):
        return asdict(self)


def finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class StabilityTracker:
    def __init__(self, window_size=20, min_valid_ratio=.90, max_std=.002,
                 max_range=.006, hold_seconds=1.5):
        if window_size < 2 or not 0 < min_valid_ratio <= 1:
            raise ValueError("invalid stability window/valid ratio")
        if any(finite_number(x) is None or x < 0
               for x in (max_std, max_range, hold_seconds)):
            raise ValueError("stability thresholds must be finite and nonnegative")
        self.history = deque(maxlen=window_size)
        self.min_valid_ratio = min_valid_ratio
        self.min_valid_count = max(2, math.ceil(window_size * min_valid_ratio))
        self.max_std = max_std
        self.max_range = max_range
        self.hold_seconds = hold_seconds
        self._quiet_since = None

    def update(self, valid, slope_mm_per_m, offset_div, confidence, *, now=None):
        now = time.monotonic() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("monotonic timestamp must be finite")
        if self.history and now < self.history[-1].timestamp:
            self._quiet_since = None
        slope, offset, conf = map(finite_number, (slope_mm_per_m, offset_div, confidence))
        valid = bool(valid and slope is not None and offset is not None and conf is not None)
        self.history.append(ScalarSample(now, valid, slope if valid else None,
                                         offset if valid else None, conf if valid else None))
        values = [sample.slope_mm_per_m for sample in self.history if sample.valid]
        count, valid_count = len(self.history), len(values)
        ratio = valid_count / count
        mean = statistics.fmean(values) if values else None
        std = statistics.pstdev(values) if values else None  # Population standard deviation.
        span = max(values) - min(values) if values else None
        duration = 0.0
        if not valid or (count == self.history.maxlen and ratio < self.min_valid_ratio):
            state, reason = "NO_MEASUREMENT", "current_invalid" if not valid else "valid_ratio_low"
        elif count < self.history.maxlen or valid_count < self.min_valid_count:
            state, reason = "WARMING_UP", "insufficient_samples"
        elif std > self.max_std:
            state, reason = "UNSTABLE", "std_exceeds_threshold"
        elif span > self.max_range:
            state, reason = "UNSTABLE", "range_exceeds_threshold"
        else:
            if self._quiet_since is None:
                self._quiet_since = now
            duration = max(0., now - self._quiet_since)
            state = "STABLE" if duration >= self.hold_seconds else "UNSTABLE"
            reason = "thresholds_held" if state == "STABLE" else "hold_time_pending"
            return StabilityResult(state, state == "STABLE", reason, count, valid_count,
                                   ratio, mean, std, span, duration)
        self._quiet_since = None
        return StabilityResult(state, False, reason, count, valid_count,
                               ratio, mean, std, span, duration)
