"""Deterministic scalar stability tests; no camera/model dependencies."""
import math
import unittest

from stability import StabilityTracker


class StabilityTests(unittest.TestCase):
    def tracker(self, **kwargs):
        return StabilityTracker(window_size=20, **kwargs)

    def fill(self, tracker, values=None, start=0.):
        values = values if values is not None else [.04] * 20
        for i, value in enumerate(values):
            result = tracker.update(value is not None, value, 2., .95, now=start + i * .1)
        return result

    def test_insufficient_samples_are_warming_up(self):
        result = self.fill(self.tracker(), [.04] * 19)
        self.assertEqual(result.state, "WARMING_UP")
        self.assertFalse(result.stable)

    def test_insufficient_valid_ratio_is_no_measurement(self):
        result = self.fill(self.tracker(), [None] * 3 + [.04] * 17)
        self.assertEqual(result.state, "NO_MEASUREMENT")
        self.assertEqual(result.valid_count, 17)

    def test_current_invalid_cannot_be_stable(self):
        tracker = self.tracker(hold_seconds=0.)
        self.assertTrue(self.fill(tracker).stable)
        self.assertEqual(tracker.update(False, .04, 2, .95, now=2).state, "NO_MEASUREMENT")

    def test_std_above_threshold_is_unstable(self):
        result = self.fill(self.tracker(max_range=1.), [.03, .05] * 10)
        self.assertEqual(result.reason, "std_exceeds_threshold")

    def test_range_above_threshold_is_unstable(self):
        result = self.fill(self.tracker(max_std=1.), [.04] * 19 + [.05])
        self.assertEqual(result.reason, "range_exceeds_threshold")

    def test_hold_time_is_required(self):
        tracker = self.tracker()
        result = self.fill(tracker)
        self.assertEqual(result.state, "UNSTABLE")
        self.assertEqual(result.reason, "hold_time_pending")
        self.assertFalse(tracker.update(True, .04, 2, .95, now=3.39).stable)

    def test_sufficient_hold_is_stable(self):
        tracker = self.tracker()
        self.fill(tracker)
        result = tracker.update(True, .04, 2, .95, now=3.5)
        self.assertTrue(result.stable)
        self.assertGreaterEqual(result.stable_duration_seconds, 1.5)

    def test_fluctuation_resets_hold(self):
        tracker = self.tracker()
        self.fill(tracker)
        self.assertTrue(tracker.update(True, .04, 2, .95, now=3.5).stable)
        result = tracker.update(True, .08, 4, .95, now=3.6)
        self.assertFalse(result.stable)
        self.assertEqual(result.stable_duration_seconds, 0.)
        self.fill(tracker, [.04] * 20, start=4.)
        self.assertFalse(tracker.update(True, .04, 2, .95, now=6.).stable)

    def test_fixed_tilt_outside_level_range_is_stable(self):
        result = self.fill(self.tracker(hold_seconds=0.), [.2] * 20)
        self.assertTrue(result.stable)
        self.assertEqual(result.mean_slope_mm_per_m, .2)

    def test_nan_infinity_do_not_enter_statistics(self):
        tracker = self.tracker(hold_seconds=0.)
        result = self.fill(tracker, [math.nan, math.inf] + [.04] * 18)
        self.assertTrue(result.stable)
        self.assertEqual(result.valid_count, 18)
        self.assertAlmostEqual(result.mean_slope_mm_per_m, .04)
        self.assertEqual(result.std_slope_mm_per_m, 0.)
        self.assertIsNone(tracker.history[0].slope_mm_per_m)

    def test_offset_and_confidence_must_also_be_finite(self):
        tracker = self.tracker()
        for offset, confidence in ((math.nan, .9), (2, math.inf)):
            result = tracker.update(True, .04, offset, confidence)
            self.assertEqual(result.valid_count, 0)

    def test_deque_is_bounded_and_contains_only_five_scalars(self):
        tracker = self.tracker()
        for i in range(2000):
            tracker.update(True, .04, 2, .95, now=i * .1)
        self.assertEqual(len(tracker.history), 20)
        self.assertTrue(all(len(sample) == 5 for sample in tracker.history))

    def test_uses_population_standard_deviation(self):
        result = self.fill(self.tracker(max_std=1., max_range=1.), [0., .02] * 10)
        self.assertAlmostEqual(result.std_slope_mm_per_m, .01)


if __name__ == "__main__":
    unittest.main()
