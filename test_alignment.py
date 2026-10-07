"""② camera alignment: angle math, screw model, processor flow, real tags."""

import glob
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

import config
import processors.align as align_module
from alignment import (average_angles, average_samples, delta_from_baseline, guidance, screw_model,
                       tick_center_guidance, turns_label, wrap_deg)
from calibration_runtime import CalibrationRuntime
from calibration_store import CalibrationStore
from processors.align import AlignProcessor
from processors.measure import MeasureProcessor


def angles(pitch, yaw, roll):
    return {"pitch_deg": pitch, "yaw_deg": yaw, "roll_deg": roll}


class AngleMathTests(unittest.TestCase):
    def test_wrap_and_circular_mean_across_180(self):
        self.assertAlmostEqual(wrap_deg(181.), -179.)
        self.assertAlmostEqual(wrap_deg(-180.), 180.)
        mean = average_angles([angles(0, 0, 179.9), angles(0, 0, -179.9), angles(0, 0, 179.95)])
        self.assertAlmostEqual(abs(mean["roll_deg"]), 179.983, places=2)
        self.assertLess(mean["roll_deg_std"], .1)
        self.assertAlmostEqual(delta_from_baseline(angles(0, 0, -179.9), angles(0, 0, 179.9))["roll_deg"], .2, places=9)

    def test_screw_model_inverts_to_the_turns_that_restore_the_baseline(self):
        base = angles(10., 20., 179.)
        teach = {"A": {"before": base, "after": angles(10.30, 20.02, 179.01)},
                 "B": {"before": base, "after": angles(9.99, 19.80, 179.)}}
        model = screw_model(teach, .25)
        np.testing.assert_allclose(model["matrix_deg_per_turn"], [[1.2, -.04], [.08, -.8]], atol=1e-9)
        # Camera is off by what +1/2 turn of A and -1/4 turn of B would produce.
        delta = {"pitch_deg": .6 + .01, "yaw_deg": .04 + .2, "roll_deg": .12}
        result = guidance(delta, model, .1)
        turns = {item["screw"]: item["turns"] for item in result["screws"]}
        self.assertAlmostEqual(turns["A"], -.5, places=6)
        self.assertAlmostEqual(turns["B"], .25, places=6)
        self.assertEqual([item["label"] for item in result["screws"]], ["逆時針 約 1/2 圈", "順時針 約 1/4 圈"])
        self.assertFalse(result["within_tolerance"])
        self.assertEqual(result["roll_note"], "機構無法調整，僅供參考")

    def test_small_errors_show_check_marks(self):
        model = {"matrix_deg_per_turn": [[1.2, 0.], [0., -.8]]}
        result = guidance({"pitch_deg": .05, "yaw_deg": -.3, "roll_deg": 0.}, model, .1)
        self.assertEqual([item["ok"] for item in result["screws"]], [True, False])
        self.assertEqual(result["screws"][0]["label"], "✓")
        self.assertTrue(guidance({"pitch_deg": .05, "yaw_deg": -.09, "roll_deg": 3.}, model, .1)["within_tolerance"])

    def test_tick_center_guidance_uses_the_screw_that_moves_the_ticks_most(self):
        base = {**angles(10., 20., 179.), "tick_center_px": 381.}
        teach = {"A": {"before": base, "after": {**angles(10.3, 20., 179.), "tick_center_px": 378.5}},
                 "B": {"before": base, "after": {**angles(10., 19.8, 179.), "tick_center_px": 381.5}}}
        model = screw_model(teach, .25)
        self.assertEqual(model["tick_center_px_per_turn"], [-10., 2.])
        result = tick_center_guidance(11., model, 10.)
        self.assertEqual((result["within_tolerance"], result["screw"]["screw"]), (False, "A"))
        self.assertAlmostEqual(result["screw"]["turns"], 1.1)
        self.assertEqual(result["screw"]["label"], "順時針 約 1 又 1/8 圈")
        self.assertEqual(tick_center_guidance(-4., model, 10.)["screw"]["label"], "✓")
        self.assertIsNone(tick_center_guidance(11., None, 10.)["screw"])
        mean = average_samples([{**angles(0, 0, 0), "tick_center_px": 380.}, {**angles(0, 0, 0), "tick_center_px": 381.},
                                {"pitch_deg": None, "yaw_deg": None, "roll_deg": None, "tick_center_px": 382.}])
        self.assertEqual((mean["tick_center_px"], mean["frames"]), (381., 3))

    def test_parallel_screws_are_rejected(self):
        base = angles(0, 0, 0)
        teach = {"A": {"before": base, "after": angles(.3, .1, 0)}, "B": {"before": base, "after": angles(.6, .2, 0)}}
        with self.assertRaises(ValueError):
            screw_model(teach, .25)

    def test_turn_labels(self):
        self.assertEqual(turns_label(.27), "約 1/4 圈")
        self.assertEqual(turns_label(-1.38), "約 1 又 3/8 圈")
        self.assertEqual(turns_label(.03), "少於 1/8 圈")


class FakeClock:
    def __init__(self):
        self.now = 1000.

    def __call__(self):
        return self.now


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CalibrationStore(Path(self.directory.name) / "calibration")
        self.store.save("geometry", {"version": "20261007T001604_geometry", "polynomial_degree": 1,
                                     "x_center_px": 380., "coefficients_x_to_div": [0., 1 / 18.]})
        self.store.save("vial", {"version": "20261007T001334_vial", "mm_per_m_per_div": .0228, "zero_offset_div": .3})
        self.measure = MeasureProcessor(detector=Mock(), captures=Mock())
        self.runtime = CalibrationRuntime(self.store, self.measure)
        self.runtime.reload()
        self.published = []
        camera = SimpleNamespace(undistorter=SimpleNamespace(camera_matrix=np.eye(3)))
        self.align = AlignProcessor(store=self.store, runtime=self.runtime, camera=camera, publish=self.published.append,
                                    detector_factory=Mock, log_directory=Path(self.directory.name) / "alignment_logs")
        self.pose = angles(10., 20., 179.5)
        self.center = 381.  # tick center: +11 px from the ROI center (370)
        self.clock = FakeClock()
        for target, name in ((align_module, "measure_frame"), (align_module, "summarize"), (align_module.time, "monotonic"),
                             (align_module, "detect_ticks"), (align_module, "scale_center")):
            replacement = {"measure_frame": lambda *args: [{"tag_id": 7, "pose_valid": True}],
                           "summarize": lambda measurements: {"tag_count": 4, "pose_count": 4, **self.pose},
                           "monotonic": self.clock, "detect_ticks": lambda *args: None,
                           "scale_center": lambda frame: self.center}[name]
            patcher = patch.object(target, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.align.enter()

    def frames(self, count=config.ALIGN_AVERAGE_FRAMES, seconds=.04):
        for index in range(count):
            self.clock.now += seconds
            self.align.process(SimpleNamespace(frame_id=index, full=np.zeros((540, 960, 3), np.uint8),
                                               roi=np.zeros((160, 740, 3), np.uint8), capture_ms=1.), None)
        return self.align.status()

    def test_noise_and_absolute_angles_without_baseline(self):
        status = self.frames()
        self.assertTrue(status["window_full"])
        self.assertAlmostEqual(status["angles"]["pitch_deg"], 10., places=9)
        self.assertAlmostEqual(status["angles"]["pitch_deg_std"], 0., places=9)
        self.assertIsNone(status["delta"])
        self.assertFalse(status["can_complete"])

    def test_baseline_teaching_tick_center_target_hold_and_complete(self):
        status = self.frames()
        self.assertAlmostEqual(status["tick_center"]["offset_px"], 11., places=9)
        self.assertFalse(status["tick_center"]["within"])
        self.assertIsNone(status["tick_center"]["guidance"]["screw"])  # no screw model yet
        self.assertEqual(self.align.set_baseline()[1]["error_code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.align.set_baseline(confirm=True)[0], 202)
        self.frames()
        baseline_version = self.store.active_state("alignment")["version"]
        self.assertIsNotNone(baseline_version)
        baseline = self.store.load("alignment")[0]["baseline"]
        self.assertAlmostEqual(baseline["pitch_deg"], 10., places=9)
        self.assertAlmostEqual(baseline["tick_center_px"], 381., places=9)
        self.assertEqual(self.measure.calibration["alignment_version"], baseline_version)

        # Teach: A (1/4 turn) pitch +0.30 deg, ticks -2.5 px; B yaw -0.20 deg, ticks +0.5 px.
        self.assertEqual(self.align.teach("A", "finish")[1]["error_code"], "NOT_STARTED")
        self.align.teach("A", "start")
        self.frames()
        self.pose, self.center = angles(10.3, 20., 179.5), 378.5
        self.align.teach("A", "finish")
        self.frames()
        self.align.teach("B", "start")
        self.frames()
        self.pose, self.center = angles(10.3, 19.8, 179.5), 379.
        self.align.teach("B", "finish")
        self.frames()
        model = self.store.load("alignment")[0]["screw_model"]
        np.testing.assert_allclose(model["matrix_deg_per_turn"], [[1.2, 0.], [0., -.8]], atol=1e-9)
        np.testing.assert_allclose(model["tick_center_px_per_turn"], [-10., 2.], atol=1e-9)
        self.assertAlmostEqual(self.store.load("alignment")[0]["baseline"]["yaw_deg"], 20., places=9)  # baseline kept

        status = self.frames()
        # AprilTag reference guidance: both screws were turned clockwise, undo both.
        self.assertEqual([item["label"] for item in status["guidance"]["screws"]], ["逆時針 約 1/4 圈", "逆時針 約 1/4 圈"])
        # Screw target: ticks +9 px from the ROI center, inside ±10 px already.
        self.assertTrue(status["tick_center"]["within"])
        self.center = 385.  # +15 px: A clockwise 1.5 turn would bring it to 370
        status = self.frames()
        self.assertEqual(status["tick_center"]["guidance"]["screw"]["screw"], "A")
        self.assertEqual(status["tick_center"]["guidance"]["screw"]["label"], "順時針 約 1 又 1/2 圈")
        self.assertEqual(self.align.complete()[1]["error_code"], "NOT_IN_RANGE")

        self.center = 372.  # back within ±10 px
        self.published.clear()
        status = self.frames(count=config.ALIGN_AVERAGE_FRAMES)
        self.assertTrue(status["tick_center"]["within"])
        self.assertTrue(any(message["entered_range"] for message in self.published))
        self.assertFalse(status["can_complete"])
        status = self.frames(count=80)  # 3.2 s more in range
        self.assertTrue(status["can_complete"])
        code, result = self.align.complete()
        self.assertEqual(code, 200)
        log = json.loads(Path(result["log"]).read_text())
        self.assertAlmostEqual(log["before"]["pitch_deg"], 10., places=9)
        self.assertAlmostEqual(log["tick_center_after"]["x_px"], 372., places=9)
        self.assertTrue(self.store.active_state("geometry")["pending_confirmation"])
        self.assertEqual(result["calibration"]["status"], "pending")
        self.assertEqual(result["next"], "#/ticks")

    def test_publish_rate_is_capped(self):
        self.frames(count=50, seconds=.01)  # 0.5 s of frames at 100 fps
        self.assertLessEqual(len(self.published), 6)


REAL_RAW = sorted(glob.glob(str(config.LOG_DIRECTORY / "manual_captures/*/images/*_raw.png")))


class RealTagTests(unittest.TestCase):
    @unittest.skipUnless(REAL_RAW, "no saved raw frame in logs/")
    def test_saved_raw_frame_has_four_tags_with_valid_pose(self):
        from apriltag_measurement import create_detector, measure_frame, summarize
        from camera_undistortion import FullFrameUndistorter, load_calibration
        import apriltag_config
        size = (config.FRAME_WIDTH, config.FRAME_HEIGHT)
        undistorter = FullFrameUndistorter(*load_calibration(config.CAMERA_CALIBRATION_NPZ, size), size,
                                           alpha=config.UNDISTORT_ALPHA)
        full = undistorter.process(cv2.imread(REAL_RAW[0], cv2.IMREAD_UNCHANGED))
        results = measure_frame(full, create_detector(apriltag_config.TAG_FAMILY), undistorter.camera_matrix,
                                apriltag_config.TAG_SIZE_METER)
        summary = summarize(results)
        self.assertEqual(summary["pose_count"], 4, results)
        for axis in ("pitch_deg", "yaw_deg", "roll_deg"):
            self.assertTrue(math.isfinite(summary[axis]))


if __name__ == "__main__":
    unittest.main()
