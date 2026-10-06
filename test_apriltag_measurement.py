"""Tests for the independent Jetson-style AprilTag measurement program."""

import contextlib
import csv
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import apriltag_config as config
import apriltag_measurement as measurement


MATRIX = np.array([[800., 0, 480], [0, 800, 270], [0, 0, 1]])
SIZE_M = 0.007


def rotation_xyz(pitch, yaw, roll):
    x, y, z = np.radians([pitch, yaw, roll])
    rx = np.array([[1., 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]])
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1., 0], [-math.sin(y), 0, math.cos(y)]])
    rz = np.array([[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1.]])
    return rz @ ry @ rx


def project_corners(rotation, translation, matrix=MATRIX):
    h = SIZE_M / 2
    objects = np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]])
    rvec, _ = cv2.Rodrigues(rotation @ np.diag([1., -1., -1.]))
    corners, _ = cv2.projectPoints(objects, rvec, np.asarray(translation, dtype=float), matrix, np.zeros(5))
    return corners.reshape(4, 2)


def synthetic_frame():
    # Independent fake scenes; no board layout or real mounting is assumed.
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    scene = {
        7: (rotation_xyz(-8, 5, 15), np.array([-.035, .020, .080])),
        13: (rotation_xyz(10, -6, -20), np.array([.035, -.020, .085])),
    }
    gray = np.full((540, 960), 255, dtype=np.uint8)
    for tag_id, (rotation, translation) in scene.items():
        tag = cv2.aruco.generateImageMarker(dictionary, tag_id, 240)
        source = np.float32([[0, 0], [239, 0], [239, 239], [0, 239]])
        destination = project_corners(rotation, translation).astype(np.float32)
        transform = cv2.getPerspectiveTransform(source, destination)
        warped = cv2.warpPerspective(tag, transform, (960, 540), flags=cv2.INTER_NEAREST, borderValue=255)
        gray = np.minimum(gray, warped)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), scene


class AprilTagTests(unittest.TestCase):
    def test_defaults_are_seven_mm_and_validated_calibration(self):
        args = measurement.parse_args([])
        self.assertEqual(args.tag_size_mm, 7.0)
        self.assertEqual(args.focus, 3711)
        self.assertEqual(args.calibration, config.CALIBRATION_NPZ)
        for size in ("0", "-7", "nan"):
            with self.subTest(size=size), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    measurement.parse_args(["--tag-size-mm", size])

    def test_known_seven_mm_pose_matches_jetson_angle_convention(self):
        rotation = rotation_xyz(-10, 8, 15)
        translation = np.array([.002, -.001, .060])
        pose = measurement.estimate_tag_pose(project_corners(rotation, translation), MATRIX, SIZE_M)
        self.assertTrue(pose["pose_valid"])
        np.testing.assert_allclose(pose["rotation"], rotation, atol=1e-8)
        np.testing.assert_allclose([pose["x_mm"], pose["y_mm"], pose["z_mm"]], translation * 1000, atol=1e-5)
        for key, angle in (("pitch_deg", -10), ("yaw_deg", 8), ("roll_deg", 15)):
            self.assertAlmostEqual(pose[key], angle, places=5)
        self.assertAlmostEqual(pose["distance_mm"], np.linalg.norm(translation) * 1000, places=5)
        self.assertLess(pose["reprojection_rmse_px"], 1e-5)

    def test_jetson_180_degree_correction_does_not_change_distance(self):
        rotation = rotation_xyz(5, -12, -170)
        corners = project_corners(rotation, [0, 0, .080])
        pose = measurement.estimate_tag_pose(corners, MATRIX, SIZE_M, 180)
        expected = rotation @ np.diag([-1., -1., 1.])
        np.testing.assert_allclose(pose["rotation"], expected, atol=1e-8)
        self.assertAlmostEqual(pose["distance_mm"], 80, places=5)

    def test_full_frame_detection_and_per_id_rotation_override(self):
        frame, scene = synthetic_frame()
        detector = measurement.create_detector("tag36h11")
        results = measurement.measure_frame(frame, detector, MATRIX, SIZE_M,
                                             default_correction=180, corrections={7: 0})
        self.assertEqual([result["tag_id"] for result in results], [7, 13])
        self.assertTrue(all(result["pose_valid"] for result in results))
        for result in results:
            expected_distance = np.linalg.norm(scene[result["tag_id"]][1]) * 1000
            self.assertAlmostEqual(result["distance_mm"], expected_distance, delta=2)
        self.assertAlmostEqual(results[0]["roll_deg"], 15, delta=2)
        self.assertAlmostEqual(results[1]["roll_deg"], 160, delta=2)
        self.assertGreater(results[0]["center_y_px"], 350)  # Outside the old ROI.
        self.assertLess(results[1]["center_y_px"], 190)

    def test_undistortion_preserves_full_frame_and_uses_new_matrix(self):
        distortion = np.array([.14, -.78, .001, .0008, 1.01])
        frame, _ = synthetic_frame()
        original = frame.copy()
        undistorter = measurement.FullFrameUndistorter(MATRIX, distortion, (960, 540), alpha=1)
        output = undistorter.process(frame)
        self.assertEqual(output.shape, (540, 960, 3))
        self.assertEqual(undistorter.map_x.shape, (540, 960))
        self.assertFalse(np.allclose(undistorter.camera_matrix, MATRIX))
        reference = cv2.undistort(frame, MATRIX, distortion, None, undistorter.camera_matrix)
        self.assertLess(np.abs(output.astype(float) - reference.astype(float)).mean(), 0.1)
        np.testing.assert_array_equal(frame, original)
        with self.assertRaisesRegex(ValueError, "Frame size"):
            undistorter.process(frame[:160])

    def test_calibration_rejects_mismatched_image_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.npz"
            np.savez(path, camera_matrix=MATRIX, dist_coeffs=np.zeros(5), image_width=960, image_height=540)
            matrix, _ = measurement.load_calibration(path, (960, 540))
            np.testing.assert_array_equal(matrix, MATRIX)
            with self.assertRaisesRegex(ValueError, "differs"):
                measurement.load_calibration(path, (1280, 720))

    def test_circular_average_handles_wraparound_and_missing_poses(self):
        self.assertAlmostEqual(abs(measurement.circular_mean_degrees([-179, 179])), 180)
        self.assertIsNone(measurement.circular_mean_degrees([0, 180]))
        rows = [{"pose_valid": True, "pitch_deg": 0, "yaw_deg": 2, "roll_deg": roll, "distance_mm": distance}
                for roll, distance in ((179, 40), (-179, 44))]
        summary = measurement.summarize(rows)
        self.assertAlmostEqual(abs(summary["roll_deg"]), 180)
        self.assertEqual(summary["distance_mm"], 42)
        self.assertIsNone(measurement.summarize([])["distance_mm"])

    def test_pixel_ruler_ignores_sidebar_and_resets_on_third_point(self):
        ruler = measurement.PixelRuler(960, 540)
        with contextlib.redirect_stdout(io.StringIO()):
            ruler.mouse_callback(cv2.EVENT_LBUTTONDOWN, 1000, 20, None, None)
            self.assertEqual(ruler.points, [])
            for point in ((20, 30), (120, 90)):
                ruler.mouse_callback(cv2.EVENT_LBUTTONDOWN, *point, None, None)
            self.assertEqual(ruler.distance_x, 100)
            ruler.mouse_callback(cv2.EVENT_LBUTTONDOWN, 25, 40, None, None)
        self.assertEqual(ruler.points, [(25, 40)])
        self.assertIsNone(ruler.distance_x)

    def test_side_by_side_canvas_does_not_modify_camera_image(self):
        frame, _ = synthetic_frame()
        original = frame.copy()
        rows = measurement.measure_frame(frame, measurement.create_detector("tag36h11"), MATRIX, SIZE_M)
        ruler = measurement.PixelRuler(960, 540)
        canvas = measurement.build_canvas(frame, rows, MATRIX, SIZE_M, ruler)
        self.assertEqual(canvas.shape, (540, 960 + config.SIDEBAR_WIDTH, 3))
        np.testing.assert_array_equal(frame, original)

    def test_image_mode_runs_rectification_pose_and_csv_without_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            image_path, calibration_path, csv_path = directory / "tags.png", directory / "calibration.npz", directory / "run.csv"
            frame, scene = synthetic_frame()
            self.assertTrue(cv2.imwrite(str(image_path), frame))
            np.savez(calibration_path, camera_matrix=MATRIX, dist_coeffs=np.zeros(5), image_width=960, image_height=540)
            args = measurement.parse_args(["--image", str(image_path), "--calibration", str(calibration_path),
                                           "--headless", "--csv", str(csv_path)])
            with contextlib.redirect_stdout(io.StringIO()), patch.object(measurement, "create_camera") as camera:
                measurement.run(args)
                camera.assert_not_called()
            with csv_path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["tag_id"] for row in rows], ["7", "13"])
            self.assertTrue(all(row["pose_valid"] == "True" for row in rows))
            original = csv_path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FileExistsError):
                measurement.run(args)
            self.assertEqual(csv_path.read_bytes(), original)

    def test_camera_comes_from_the_shared_module_with_requested_focus(self):
        import camera
        with patch.object(camera, "create_camera", return_value="shared") as shared:
            self.assertEqual(measurement.create_camera(3700), "shared")
        shared.assert_called_once_with(focus=3700)

    def test_busy_camera_stops_before_windows_or_csv(self):
        import camera
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "run.csv"
            args = measurement.parse_args(["--headless", "--csv", str(csv_path)])
            with (patch.object(camera, "acquire_camera_lock", side_effect=camera.CameraBusyError("相機正被其他程式使用")),
                  patch.object(measurement, "create_camera") as create):
                with self.assertRaisesRegex(RuntimeError, "其他程式"):
                    measurement.run(args)
            create.assert_not_called()
            self.assertFalse(csv_path.exists())


if __name__ == "__main__":
    unittest.main()
