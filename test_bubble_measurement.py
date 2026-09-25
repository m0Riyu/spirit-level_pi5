"""Tests for calibrated bubble position conversion."""

import json
import tempfile
import unittest
from pathlib import Path

from bubble_measurement import BubbleCalibration


class BubbleMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "calibration.json"
        self.path.write_text(
            json.dumps(
                {
                    "created_utc": "2026-09-25T17:41:28+00:00",
                    "status": "PASS",
                    "image_width": 740,
                    "image_height": 160,
                    "reference_midpoint_x": 373.0,
                    "global_pitch": {"pitch_px": 19.0},
                }
            ),
            encoding="utf-8",
        )
        self.calibration = BubbleCalibration.from_json(
            self.path, expected_size=(740, 160)
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_center_is_zero_divisions(self):
        measurement = self.calibration.measure(373.0)
        self.assertEqual(measurement.offset_px, 0.0)
        self.assertEqual(measurement.offset_div, 0.0)
        self.assertEqual(measurement.direction, "center")

    def test_one_division_right_is_positive(self):
        measurement = self.calibration.measure(392.0)
        self.assertEqual(measurement.offset_px, 19.0)
        self.assertEqual(measurement.offset_div, 1.0)
        self.assertEqual(measurement.direction, "right")

    def test_one_division_left_is_negative(self):
        measurement = self.calibration.measure(354.0)
        self.assertEqual(measurement.offset_px, -19.0)
        self.assertEqual(measurement.offset_div, -1.0)
        self.assertEqual(measurement.direction, "left")

    def test_missing_detection_is_invalid(self):
        measurement = self.calibration.measure(None)
        self.assertEqual(measurement.valid, 0)
        self.assertEqual(measurement.error, "bubble_not_detected")

    def test_rejects_wrong_roi_size(self):
        with self.assertRaisesRegex(ValueError, "does not match ROI"):
            BubbleCalibration.from_json(self.path, expected_size=(608, 128))


if __name__ == "__main__":
    unittest.main()
