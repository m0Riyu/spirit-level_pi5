"""Regression tests for direct V4L2 focus and camera startup failures."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import camera
import config
from camera_undistortion import FullFrameUndistorter


FOCUS_CONTROLS = (
    "focus_absolute 0x009a090a (int) : "
    "min=0 max=4095 step=1 default=0 value=3711\n"
)


class FocusTests(unittest.TestCase):
    def test_clamps_to_driver_range_and_step(self):
        self.assertEqual(camera.clamp_focus(-10, 0, 4095, 1), 0)
        self.assertEqual(camera.clamp_focus(5000, 0, 4095, 1), 4095)
        self.assertEqual(camera.clamp_focus(3711, 0, 4095, 1), 3711)
        self.assertEqual(camera.clamp_focus(3711, 0, 4096, 16), 3712)
        self.assertEqual(camera.clamp_focus(107, 3, 115, 16), 99)

    def test_parses_focus_control_and_rejects_missing_control(self):
        with patch.object(camera, "run_v4l2", return_value=FOCUS_CONTROLS):
            self.assertEqual(
                camera.query_focus_control("/dev/v4l-subdev3"),
                (0, 4095, 1, 0, 3711),
            )
        with patch.object(camera, "run_v4l2", return_value="brightness: 0"):
            with self.assertRaisesRegex(RuntimeError, "沒有可用"):
                camera.query_focus_control("/dev/v4l-subdev3")

    def test_writes_aligned_value_and_verifies_readback(self):
        with patch.object(
            camera, "run_v4l2", return_value="focus_absolute: 3712\n"
        ) as run:
            self.assertEqual(
                camera.set_focus("/dev/v4l-subdev3", 3711, 0, 4096, 16),
                (3712, 3712),
            )
            run.assert_called_once_with(
                "/dev/v4l-subdev3", "--set-ctrl", "focus_absolute=3712",
                "--get-ctrl", "focus_absolute",
            )

    def test_rejects_missing_or_mismatched_readback(self):
        for output in ("", "focus_absolute: 0\n"):
            with self.subTest(output=output):
                with patch.object(camera, "run_v4l2", return_value=output):
                    with self.assertRaises(RuntimeError):
                        camera.set_focus("/dev/v4l-subdev3", 3711, 0, 4095, 1)

    def test_v4l2_command_failure_reports_driver_error(self):
        result = subprocess.CompletedProcess([], 1, "", "Permission denied")
        with patch.object(camera.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                camera.run_v4l2(Path("/dev/v4l-subdev3"), "--list-ctrls")
            run.assert_called_once_with(
                ["v4l2-ctl", "-d", "/dev/v4l-subdev3", "--list-ctrls"],
                check=False, capture_output=True, text=True, timeout=10,
            )


class CameraStartupTests(unittest.TestCase):
    def test_starts_stream_then_applies_fixed_focus_and_discards_settling_frames(self):
        instance = Mock()
        instance.camera_controls = {"ExposureTime": (), "LensPosition": (), "AfMode": ()}

        def focus_after_start(*args):
            instance.start.assert_called_once_with()
            return 3711, 3711

        with (
            patch.object(camera, "load_calibration", return_value=(np.eye(3), np.zeros(5))),
            patch.object(camera, "FullFrameUndistorter"),
            patch.object(camera, "Picamera2", return_value=instance),
            patch.object(camera, "find_vcm_device", return_value=Path("/dev/v4l-subdev3")),
            patch.object(camera, "query_focus_control", return_value=(0, 4095, 1, 0, 0)),
            patch.object(camera, "set_focus", side_effect=focus_after_start) as focus,
            patch.object(camera.time, "monotonic", side_effect=[0.0, 0.1, 0.3]),
            patch.object(config, "VCM_FOCUS_ABSOLUTE", 3711),
            patch.object(config, "VCM_FOCUS_SETTLE_SECONDS", 0.25),
        ):
            result = camera.create_camera(
                {"ExposureTime": 1000, "LensPosition": 12.0, "AfMode": 2}
            )

        self.assertIs(result, instance)
        instance.set_controls.assert_called_once_with({"ExposureTime": 1000})
        focus.assert_called_once_with(Path("/dev/v4l-subdev3"), 3711, 0, 4095, 1)
        instance.capture_array.assert_called_once_with("main")
        instance.close.assert_not_called()
        instance.create_preview_configuration.assert_called_once_with(
            main={"size": (config.FRAME_WIDTH, config.FRAME_HEIGHT), "format": "RGB888"},
            raw={"size": (config.RAW_WIDTH, config.RAW_HEIGHT)},
        )

    def test_focus_failure_closes_camera_and_propagates_error(self):
        instance = Mock()
        instance.camera_controls = {}
        with (
            patch.object(camera, "load_calibration", return_value=(np.eye(3), np.zeros(5))),
            patch.object(camera, "FullFrameUndistorter"),
            patch.object(camera, "Picamera2", return_value=instance),
            patch.object(camera, "find_vcm_device", side_effect=RuntimeError("no VCM")),
        ):
            with self.assertRaisesRegex(RuntimeError, "no VCM"):
                camera.create_camera()
        instance.start.assert_called_once_with()
        instance.close.assert_called_once_with()

    def test_invalid_calibration_closes_camera_before_starting(self):
        instance = Mock()
        with (
            patch.object(camera, "Picamera2", return_value=instance),
            patch.object(camera, "load_calibration", side_effect=ValueError("wrong calibration size")),
        ):
            with self.assertRaisesRegex(ValueError, "wrong calibration size"):
                camera.create_camera()
        instance.start.assert_not_called()
        instance.close.assert_called_once_with()

    def test_rectifies_full_frame_before_cropping_and_reuses_maps(self):
        matrix = np.array([[760., 0, 480], [0, 760., 270], [0, 0, 1]])
        distortion = np.array([.14, -.78, .001, .0008, 1.01])
        frame = np.random.default_rng(10).integers(0, 256, (540, 960, 3), dtype=np.uint8)
        undistorter = FullFrameUndistorter(matrix, distortion, (960, 540))
        instance = Mock(frame_undistorter=undistorter)
        instance.capture_array.return_value = frame
        expected = cv2.undistort(frame, matrix, distortion, None, undistorter.camera_matrix)
        with patch("camera_undistortion.cv2.initUndistortRectifyMap") as create_maps:
            for _ in range(2):
                corrected, roi = camera.capture_roi(instance)
                self.assertEqual(corrected.shape, (540, 960, 3))
                self.assertEqual(roi.shape, (160, 740, 3))
                # Float maps and undistort's fixed-point maps can round
                # interpolation differently on this high-frequency image.
                self.assertLess(np.abs(corrected.astype(float) - expected.astype(float)).mean(), 1.0)
                np.testing.assert_array_equal(roi, corrected[190:350, 110:850])
                self.assertFalse(np.shares_memory(roi, corrected))
            create_maps.assert_not_called()
        self.assertGreater(np.abs(corrected.astype(float) - frame.astype(float)).mean(), 1)

    def test_capture_frames_also_returns_the_frame_before_undistortion(self):
        matrix = np.array([[760., 0, 480], [0, 760., 270], [0, 0, 1]])
        frame = np.random.default_rng(11).integers(0, 256, (540, 960, 3), dtype=np.uint8)
        undistorter = FullFrameUndistorter(matrix, np.array([.14, -.78, .001, .0008, 1.01]), (960, 540))
        instance = Mock(frame_undistorter=undistorter)
        instance.capture_array.return_value = frame.copy()
        raw, corrected, roi = camera.capture_frames(instance)
        instance.capture_array.assert_called_once_with("main")
        np.testing.assert_array_equal(raw, frame)
        np.testing.assert_array_equal(roi, corrected[190:350, 110:850])
        self.assertGreater(np.abs(corrected.astype(float) - raw.astype(float)).mean(), 1)


if __name__ == "__main__":
    unittest.main()
