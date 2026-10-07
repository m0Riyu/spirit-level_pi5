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
import camera_service
import config
import processors.measure
from calibration_store import CalibrationStore
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
        # Geometry is measured in rectified ROI pixels: 2 divisions right of 373.
        point = (373 + 2 * 19 + 110, 80 + 190)
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
            store = CalibrationStore(directory / "calibration")
            store.save("geometry", {
                "version": "20261002T072923_geometry", "polynomial_degree": 1, "x_center_px": 373.,
                "coefficients_x_to_div": [0., 1 / 19], "checks": {"passed": True},
                "image_geometry": {
                    "coordinate_system": "undistorted", "frame_size": [960, 540], "roi_origin": [110, 190],
                    "original_camera_matrix": undistorter.original_camera_matrix.tolist(),
                    "dist_coeffs": undistorter.distortion.reshape(-1).tolist(),
                    "new_camera_matrix": undistorter.camera_matrix.tolist()},
            }, pending_confirmation=True)
            store.save("vial", {"version": "20261006T200000_vial", "mm_per_m_per_div": .025, "zero_offset_div": .5})
            with (
                patch.object(config, "CALIBRATION_DIRECTORY", store.root),
                patch.object(config, "LOG_DIRECTORY", directory),
                patch.object(config, "POWER_MONITOR_ENABLED", False),
                patch.object(config, "ENABLE_BUBBLE_MEASUREMENT", True),
                patch.object(config, "ENABLE_WEBSOCKET", True),
                patch.object(config, "ENABLE_IMAGE_STREAM", True),
                patch.object(config, "ENABLE_CONTINUOUS_CSV", True),
                patch.object(config, "TELEMETRY_SEND_EVERY", 1),
                patch.object(app, "YoloDetector", return_value=detector),
                patch.object(app, "create_camera", return_value=camera),
                patch.object(app, "TelemetryServer", return_value=telemetry),
                patch.object(processors.measure, "show_frame", return_value=True) as show,
                patch.object(app, "system_summary", return_value={}),
                patch.object(app, "close_windows"),
                patch.object(app, "ask_to_save_csv", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                app.run()
            detector.predict.assert_called_once()
            np.testing.assert_array_equal(show.call_args.args[0], expected_roi)
            self.assertAlmostEqual(show.call_args.args[2].offset_div, 2., places=8)
            payload = telemetry.publish.call_args.args[0]
            # Vial layer: (2 - 0.5 zero) * 0.025 mm/m per division.
            self.assertAlmostEqual(payload["measurement"]["offset_div"], 2., places=8)
            self.assertAlmostEqual(payload["measurement"]["slope_mm_per_m"], 0.0375, places=8)
            self.assertEqual(payload["system_state"], "ADJUST")
            self.assertEqual(payload["calibration"], {
                "geometry_version": "20261002T072923_geometry", "geometry_pending_confirmation": True,
                "vial_version": "20261006T200000_vial", "alignment_version": "",
                "mm_per_m_per_div": .025, "zero_offset_div": .5})
            metadata = json.loads(next(directory.glob("manual_captures/*/session_metadata.json")).read_text())
            self.assertEqual(metadata["calibration"]["vial_version"], "20261006T200000_vial")
            self.assertTrue(metadata["calibration_source"].endswith("20261002T072923_geometry.json"))
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


class CalibrationMismatchTests(unittest.TestCase):
    def test_missing_calibration_runs_yolo_only(self):
        with tempfile.TemporaryDirectory() as directory, (
                patch.object(config, "CALIBRATION_DIRECTORY", Path(directory) / "none")), (
                patch.object(config, "LOG_DIRECTORY", Path(directory))), (
                patch.object(config, "POWER_MONITOR_ENABLED", False)), (
                contextlib.redirect_stdout(io.StringIO())) as output:
            store = CalibrationStore()
            with self.assertRaises(FileNotFoundError):
                store.load_geometry()
            camera = Mock(frame_undistorter=Mock())
            telemetry = Mock()
            telemetry.dashboard_urls.return_value = []
            detector = Mock()
            detector.predict.side_effect = KeyboardInterrupt
            with (patch.object(app, "YoloDetector", return_value=detector),
                  patch.object(app, "create_camera", return_value=camera),
                  patch.object(camera_service, "capture_frames", return_value=(None, None, np.zeros((160, 740, 3), np.uint8))),
                  patch.object(app, "TelemetryServer", return_value=telemetry),
                  patch.object(app, "close_windows"),
                  patch.object(config, "ENABLE_CONTINUOUS_CSV", False)):
                app.run()
        self.assertIn("無法載入校正檔", output.getvalue())
