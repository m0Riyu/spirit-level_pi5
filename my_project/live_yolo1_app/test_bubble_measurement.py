"""Tests for calibrated bubble position conversion."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bubble_measurement import BubbleCalibration
from camera_undistortion import FullFrameUndistorter


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

    def test_rectified_pixels_preserve_calibrated_divisions_across_image(self):
        data = json.loads(self.path.read_text())
        data["axis_y"] = 80
        self.path.write_text(json.dumps(data))
        original_json = self.path.read_bytes()
        matrix = np.array([[760., 0, 480], [0, 760., 270], [0, 0, 1]])
        undistorter = FullFrameUndistorter(matrix, np.array([.14, -.78, .001, .0008, 1.01]), (960, 540))
        calibration = BubbleCalibration.from_json(
            self.path, (740, 160), undistorter=undistorter, roi_origin=(110, 190),
        )
        for y in (30., 80., 125.):
            for divisions in (-15., -5., -1., 0., 1., 5., 15.):
                with self.subTest(divisions=divisions, y=y):
                    point = undistorter.undistort_points([(373 + divisions * 19 + 110, y + 190)])[0]
                    result = calibration.measure(point[0] - 110, point[1] - 190)
                    self.assertAlmostEqual(result.offset_div, divisions, places=8)
                    self.assertAlmostEqual(result.offset_px, result.center_x_roi - result.scale_center_x_roi)
        self.assertEqual(self.path.read_bytes(), original_json)
        self.assertNotAlmostEqual(calibration.pitch_px_per_div, 19., places=2)
        self.assertEqual(calibration.measure(None).valid, 0)

    def test_rectification_requires_known_original_measurement_axis(self):
        with self.assertRaisesRegex(ValueError, "axis_y is required"):
            BubbleCalibration.from_json(self.path, undistorter=object(), roi_origin=(110, 190))

    def rectified_fixture(self):
        matrix = np.array([[760., 0, 480], [0, 760., 270], [0, 0, 1]])
        undistorter = FullFrameUndistorter(matrix, np.array([.14, -.78, .001, .0008, 1.01]), (960, 540))
        data = json.loads(self.path.read_text())
        data["global_pitch"]["pitch_px"] = 18.
        data["image_geometry"] = {
            "coordinate_system": "undistorted",
            "frame_size": [960, 540], "roi_origin": [110, 190],
            "original_camera_matrix": matrix.tolist(),
            "dist_coeffs": undistorter.distortion.tolist(),
            "new_camera_matrix": undistorter.camera_matrix.tolist(),
        }
        return data, undistorter

    def test_tuner_rectified_calibration_is_used_without_second_conversion(self):
        data, undistorter = self.rectified_fixture()
        self.path.write_text(json.dumps(data))
        calibration = BubbleCalibration.from_json(
            self.path, (740, 160), undistorter=undistorter, roi_origin=(110, 190),
        )
        self.assertEqual(calibration.center_x_roi, 373.)
        self.assertEqual(calibration.pitch_px_per_div, 18.)
        self.assertEqual(calibration.measure(391., 80.).offset_div, 1.)
        self.assertEqual(calibration.measure(355., 80.).offset_div, -1.)
        self.assertEqual(calibration.measure(373., 80.).offset_div, 0.)

    def test_rectified_calibration_rejects_different_roi_or_camera_geometry(self):
        for field, value in (
            ("roi_origin", [100, 190]),
            ("new_camera_matrix", np.eye(3).tolist()),
            ("dist_coeffs", [0.] * 5),
        ):
            with self.subTest(field=field):
                data, undistorter = self.rectified_fixture()
                data["image_geometry"][field] = value
                self.path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "differs from runtime geometry"):
                    BubbleCalibration.from_json(
                        self.path, (740, 160), undistorter=undistorter, roi_origin=(110, 190),
                    )


if __name__ == "__main__":
    unittest.main()
