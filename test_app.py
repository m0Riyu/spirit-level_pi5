"""Verify main.py consumers receive rectified imagery and calibrated values."""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import app
import config
from camera_undistortion import FullFrameUndistorter
from detector import Detection, Prediction


class MainUndistortionTests(unittest.TestCase):
    def test_rectified_roi_flows_to_detection_preview_csv_and_telemetry(self):
        matrix = np.array([[760., 0, 480], [0, 760., 270], [0, 0, 1]])
        undistorter = FullFrameUndistorter(
            matrix, np.array([.14, -.78, .001, .0008, 1.01]), (960, 540),
        )
        original = np.random.default_rng(20).integers(0, 256, (540, 960, 3), dtype=np.uint8)
        expected_roi = undistorter.process(original)[190:350, 110:850]
        point = undistorter.undistort_points([(373 + 2 * 19 + 110, 80 + 190)])[0]
        camera = Mock(frame_undistorter=undistorter)
        camera.capture_array.return_value = original
        detection = Detection(
            detected=1, detection_count=1, confidence=0.9,
            center_x_roi=point[0] - 110, center_y_roi=point[1] - 190,
            center_x_full=point[0], center_y_full=point[1],
        )

        def predict(roi):
            np.testing.assert_array_equal(roi, expected_roi)
            return Prediction(SimpleNamespace(plot=lambda: roi.copy()), detection, 10., 1., 8., 1.)

        detector = Mock()
        detector.predict.side_effect = predict
        telemetry = Mock()
        telemetry.dashboard_urls.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            scale_path = directory / "ticks.json"
            scale_path.write_text(json.dumps({
                "status": "PASS", "image_width": 740, "image_height": 160,
                "reference_midpoint_x": 373., "global_pitch": {"pitch_px": 19.}, "axis_y": 80,
            }))
            with (
                patch.object(config, "BUBBLE_CALIBRATION_PATH", scale_path),
                patch.object(config, "LOG_DIRECTORY", directory),
                patch.object(config, "ENABLE_BUBBLE_MEASUREMENT", True),
                patch.object(config, "ENABLE_WEBSOCKET", True),
                patch.object(config, "ENABLE_IMAGE_STREAM", True),
                patch.object(config, "TELEMETRY_SEND_EVERY", 1),
                patch.object(app, "YoloDetector", return_value=detector),
                patch.object(app, "create_camera", return_value=camera),
                patch.object(app, "TelemetryServer", return_value=telemetry),
                patch.object(app, "show_frame", return_value=True) as show,
                patch.object(app, "close_windows"),
                patch.object(app, "ask_to_save_csv", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.run()
            detector.predict.assert_called_once()
            np.testing.assert_array_equal(show.call_args.args[0], expected_roi)
            self.assertAlmostEqual(show.call_args.args[2].offset_div, 2., places=8)
            payload = telemetry.publish.call_args.args[0]
            self.assertAlmostEqual(payload["measurement"]["slope_mm_per_m"], 0.04, places=8)
            self.assertEqual(payload["system_state"], "ADJUST")
            with next(directory.glob("*.csv")).open(newline="") as file:
                row = next(csv.DictReader(file))
            self.assertAlmostEqual(float(row["bubble_offset_div"]), 2., places=8)
            self.assertAlmostEqual(float(row["center_x_full"]), point[0], places=8)
            self.assertAlmostEqual(float(row["center_x_roi"]), point[0] - 110, places=8)
        camera.stop.assert_called_once_with()
        camera.close.assert_called_once_with()
        telemetry.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
