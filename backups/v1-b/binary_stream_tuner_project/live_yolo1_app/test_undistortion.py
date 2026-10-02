"""Verify undistortion precedes ROI, tick measurement, and saved outputs."""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import binary_stream_tuner as tuner
import camera
import config
from camera_undistortion import FullFrameUndistorter, load_calibration


MATRIX = np.array([[763., 0, 480], [0, 763., 270], [0, 0, 1.]])
DISTORTION = np.array([.14, -.78, .001, .0008, 1.01])
PARAMETERS = {"block_size": 139, "c_value": 20, "morph_kernel": 4, "use_roi": True}


def synthetic_ticks():
    frame = np.full((540, 960, 3), 255, dtype=np.uint8)
    for side in (-1, 1):
        for index in range(13):
            x = (290 - index * 19 if side < 0 else 455 + index * 19) + 110
            y1, y2 = (20, 140) if index in (1, 5, 9) else (55, 110)
            cv2.rectangle(frame, (x - 1, y1 + 190), (x + 1, y2 + 190), (0, 0, 0), -1)
    return frame


class UndistortionTests(unittest.TestCase):
    def test_calibration_validates_size_metadata_and_coefficients(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.npz"
            np.savez(path, camera_matrix=MATRIX, dist_coeffs=DISTORTION, image_width=960, image_height=540)
            matrix, distortion = load_calibration(path, (960, 540))
            np.testing.assert_array_equal(matrix, MATRIX)
            np.testing.assert_array_equal(distortion, DISTORTION)
            with self.assertRaisesRegex(ValueError, "differs"):
                load_calibration(path, (740, 160))
            np.savez(path, camera_matrix=MATRIX, dist_coeffs=DISTORTION)
            with self.assertRaisesRegex(ValueError, "width and height"):
                load_calibration(path, (960, 540))
            np.savez(path, camera_matrix=MATRIX, dist_coeffs=[float("nan")] * 5,
                     image_width=960, image_height=540)
            with self.assertRaisesRegex(ValueError, "Invalid"):
                load_calibration(path, (960, 540))

    def test_capture_rectifies_before_roi_and_reuses_maps(self):
        undistorter = FullFrameUndistorter(MATRIX, DISTORTION, (960, 540))
        original = synthetic_ticks()
        expected = cv2.undistort(original, MATRIX, DISTORTION, None, undistorter.camera_matrix)
        instance = Mock(frame_undistorter=undistorter)
        instance.capture_array.return_value = original
        original_copy = original.copy()
        with patch("camera_undistortion.cv2.initUndistortRectifyMap") as create_maps:
            for _ in range(2):
                full_frame, roi = camera.capture_roi(instance)
                self.assertEqual(full_frame.shape, (540, 960, 3))
                self.assertEqual(roi.shape, (160, 740, 3))
                self.assertLess(np.abs(full_frame.astype(float) - expected.astype(float)).mean(), 0.1)
                np.testing.assert_array_equal(roi, full_frame[190:350, 110:850])
                np.testing.assert_array_equal(tuner.select_image(full_frame, True), roi)
                self.assertIs(tuner.select_image(full_frame, False), full_frame)
            create_maps.assert_not_called()
        np.testing.assert_array_equal(original, original_copy)
        self.assertFalse(np.array_equal(full_frame, original))
        with self.assertRaisesRegex(ValueError, "Frame size"):
            undistorter.process(roi)

    def test_missing_calibration_prevents_camera_start(self):
        with (
            patch.object(camera, "load_calibration", side_effect=FileNotFoundError("missing calibration")),
            patch.object(camera, "Picamera2") as picamera,
        ):
            with self.assertRaises(FileNotFoundError):
                camera.create_camera()
        picamera.assert_not_called()

    def test_camera_start_failure_releases_camera(self):
        instance = Mock()
        instance.camera_controls = {}
        instance.start.side_effect = RuntimeError("camera busy")
        with (
            patch.object(camera, "load_calibration", return_value=(MATRIX, DISTORTION)),
            patch.object(camera, "Picamera2", return_value=instance),
        ):
            with self.assertRaisesRegex(RuntimeError, "camera busy"):
                camera.create_camera()
        instance.close.assert_called_once_with()

    def test_startup_builds_maps_once_and_preserves_camera_controls(self):
        instance = Mock()
        instance.camera_controls = {"ExposureTime": (), "LensPosition": ()}
        with (
            patch.object(camera, "load_calibration", return_value=(MATRIX, DISTORTION)),
            patch.object(camera, "Picamera2", return_value=instance),
            patch("camera_undistortion.cv2.initUndistortRectifyMap",
                  wraps=cv2.initUndistortRectifyMap) as create_maps,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            returned = camera.create_camera({"ExposureTime": 13000, "LensPosition": 12.})
            self.assertIs(returned, instance)
            create_maps.assert_called_once()
            self.assertIsInstance(instance.frame_undistorter, FullFrameUndistorter)
        instance.set_controls.assert_called_once_with({"ExposureTime": 13000, "LensPosition": 12.})
        instance.start.assert_called_once_with()
        instance.close.assert_not_called()

    def test_run_uses_rectified_measurements_in_previews_csv_and_saved_files(self):
        original = synthetic_ticks()
        undistorter = FullFrameUndistorter(MATRIX, DISTORTION, (960, 540))
        instance = Mock(frame_undistorter=undistorter)
        instance.capture_array.return_value = original
        rectified = undistorter.process(original)
        expected_source = tuner.select_image(rectified, True)
        expected_binary = tuner.binarize(expected_source, PARAMETERS)
        expected_measurement, _ = tuner.measure_ticks(expected_binary)
        raw_measurement, _ = tuner.measure_ticks(tuner.binarize(tuner.select_image(original, True), PARAMETERS))
        self.assertEqual(expected_measurement["status"], "PASS")
        self.assertEqual(expected_measurement["left_candidate_count"], 13)
        self.assertEqual(expected_measurement["right_candidate_count"], 13)
        self.assertNotEqual(expected_measurement["reference_midpoint_x"], raw_measurement["reference_midpoint_x"])
        panel = Mock()
        panel.update.side_effect = [True, False]
        panel.read_parameters.return_value = PARAMETERS
        panel.read_camera_settings.return_value = {}
        panel.consume_camera_settings.return_value = None
        panel.consume_save_request.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            with (
                patch.object(config, "MEASUREMENT_RUN_DIRECTORY", directory / "runs"),
                patch.object(tuner, "CAPTURE_DIRECTORY", directory / "captures"),
                patch.object(tuner, "load_saved_settings", return_value=PARAMETERS),
                patch.object(tuner, "ControlPanel", return_value=panel),
                patch.object(tuner, "create_camera", return_value=instance),
                patch.object(tuner.cv2, "namedWindow"),
                patch.object(tuner.cv2, "resizeWindow"),
                patch.object(tuner.cv2, "imshow") as imshow,
                patch.object(tuner.cv2, "waitKey", return_value=-1),
                patch.object(tuner.cv2, "destroyAllWindows"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                tuner.run()
            shown = {call.args[0]: call.args[1] for call in imshow.call_args_list}
            np.testing.assert_array_equal(shown[tuner.ORIGINAL_WINDOW], expected_source)
            np.testing.assert_array_equal(shown[tuner.BINARY_WINDOW], expected_binary)
            displayed_measurement = panel.update_measurement_dashboard.call_args.args[0]
            self.assertEqual(displayed_measurement["reference_midpoint_x"], expected_measurement["reference_midpoint_x"])
            geometry_path = next((directory / "runs").glob("*/image_geometry.json"))
            geometry = json.loads(geometry_path.read_text())
            self.assertEqual(geometry["coordinate_system"], "undistorted")
            np.testing.assert_allclose(geometry["new_camera_matrix"], undistorter.camera_matrix)
            with (geometry_path.parent / "frame_summary.csv").open(newline="") as file:
                summary = next(csv.DictReader(file))
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(float(summary["reference_midpoint_x"]), expected_measurement["reference_midpoint_x"])
            saved_path = next((directory / "captures").glob("*_tick_measurement.json"))
            saved_measurement = json.loads(saved_path.read_text())
            self.assertEqual(saved_measurement["reference_midpoint_x"], expected_measurement["reference_midpoint_x"])
            self.assertEqual(saved_measurement["image_geometry"]["roi_origin"], [110, 190])
            settings_path = saved_path.with_name(saved_path.name.replace("_tick_measurement", ""))
            self.assertEqual(json.loads(settings_path.read_text())["image_geometry"]["coordinate_system"], "undistorted")
            binary_path = saved_path.with_name(saved_path.name.replace("_tick_measurement.json", ".png"))
            np.testing.assert_array_equal(cv2.imread(str(binary_path), cv2.IMREAD_GRAYSCALE), expected_binary)
        instance.stop.assert_called_once_with()
        instance.close.assert_called_once_with()
        panel.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
