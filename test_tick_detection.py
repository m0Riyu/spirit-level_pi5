"""③ tick check: detection on synthetic ROIs, polynomial fit, processor flow, API."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

import app
import config
from calibration_runtime import CalibrationRuntime
from calibration_store import CalibrationStore
from geometry_calibration import GeometryCalibration, fit_geometry
from mode_manager import ModeManager
from preview import PreviewBuffer
from processors.measure import MeasureProcessor
from processors.ticks import TickProcessor
from system_controller import PinGuard
from telemetry_server import TelemetryServer
from test_telemetry_server import unused_port
from tick_detection import combine_frames, detect_ticks

SCALE = 10  # supersampling for sub-pixel tick positions


def big(x):
    """Supersampled pixel whose INTER_AREA downscale lands on ROI coordinate x."""
    return int(round(x * SCALE + (SCALE - 1) / 2))


def tick_x(center, side, k, pitch=18., h=4.5, quadratic=0.):
    offset = (k + h) * pitch
    offset += quadratic * offset ** 2
    return center + (offset if side == "right" else -offset)


def synthetic_roi(center=370.25, pitch=18., h=4.5, lean_px=0., hide=(), bubble=True, quadratic=0., seed=0):
    """740x160 ROI: 26 ticks (sub-pixel), RSK-like logo strokes, bubble outline, noise."""
    canvas = np.full((160 * SCALE, 740 * SCALE), 165, np.uint8)
    for side in ("left", "right"):
        for k in range(13):
            if (side, k) in hide:
                continue
            x = tick_x(center, side, k, pitch, h, quadratic)
            top, bottom = (30, 125) if k % 4 == 0 else (45, 115)
            x_top, x_bottom = x - lean_px / 2, x + lean_px / 2
            cv2.line(canvas, (big(x_top), big(top)), (big(x_bottom), big(bottom)), 40, 2 * SCALE)
    for dx in (-40, -27, -12, 3, 18, 33, 46):  # logo strokes inside the central gap
        cv2.line(canvas, (big(center + dx), big(50)), (big(center + dx + 6), big(110)), 45, 3 * SCALE)
    if bubble:
        cv2.ellipse(canvas, (big(center + 20), big(80)), (95 * SCALE, 38 * SCALE), 0, 0, 360, 70, 3 * SCALE)
    roi = cv2.resize(canvas, (740, 160), interpolation=cv2.INTER_AREA)
    roi = np.clip(roi + np.random.default_rng(seed).normal(0, 2, roi.shape), 0, 255).astype(np.uint8)
    return cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)


class DetectionTests(unittest.TestCase):
    def test_finds_all_ticks_sub_pixel_despite_logo_and_bubble_edge(self):
        frame = detect_ticks(synthetic_roi(), 370.25, 18.)
        self.assertEqual((frame.count("left"), frame.count("right")), (13, 13))
        errors = [abs(x - tick_x(370.25, side, k)) for (side, k), x in frame.positions.items()]
        self.assertLess(max(errors), .25)

    def test_camera_shift_is_followed_from_an_old_prior(self):
        frame = detect_ticks(synthetic_roi(center=383.6), prior_center=370.25, prior_pitch=18.)
        self.assertEqual(frame.count(), 26)
        pairs = [(frame.positions[("left", k)] + frame.positions[("right", k)]) / 2 for k in range(13)]
        self.assertAlmostEqual(float(np.median(pairs)), 383.6, delta=.2)

    def test_hidden_tick_is_skipped_without_shifting_ids(self):
        frame = detect_ticks(synthetic_roi(hide=(("right", 5),)), 370.25, 18.)
        self.assertEqual(frame.count(), 25)
        self.assertNotIn(("right", 5), frame.positions)
        self.assertAlmostEqual(frame.positions[("right", 6)], tick_x(370.25, "right", 6), delta=.3)

    def test_roll_is_measured_from_tick_lean(self):
        lean = 1.4  # px over the 95/70 px tick length
        frame = detect_ticks(synthetic_roi(lean_px=lean, bubble=False), 370.25, 18.)
        expected = np.degrees(np.arctan(lean / 82))
        self.assertAlmostEqual(frame.roll_deg, expected, delta=.35)
        self.assertGreater(frame.roll_deg, 0)

    def test_frames_are_combined_by_median_and_rare_ticks_dropped(self):
        frames = [detect_ticks(synthetic_roi(seed=seed), 370.25, 18.) for seed in range(5)]
        frames[0].positions[("left", 0)] += 5  # one outlier frame
        ticks, _ = combine_frames(frames + [SimpleNamespace(positions={("right", 13): 700.}, roll_deg=None)])
        self.assertEqual(len(ticks), 26)
        inner = next(tick for tick in ticks if (tick["side"], tick["tick_id"]) == ("left", 0))
        self.assertAlmostEqual(inner["x"], tick_x(370.25, "left", 0), delta=.25)


def ideal_ticks(center=370.25, pitch=18., h=4.48, quadratic=0.):
    return [{"side": side, "tick_id": k, "x": tick_x(center, side, k, pitch, h, quadratic)}
            for side in ("left", "right") for k in range(13)]


class FitTests(unittest.TestCase):
    def fit(self, ticks, degree=2, previous=None):
        return fit_geometry(ticks, degree=degree, version="t_geometry", created_at_iso="", previous=previous)

    def test_recovers_center_pitch_and_inner_half_gap(self):
        record = self.fit(ideal_ticks())
        self.assertAlmostEqual(record["x_center_px"], 370.25, places=9)
        self.assertAlmostEqual(record["inner_half_gap_div"], 4.48, places=9)
        self.assertAlmostEqual(record["px_per_div_center"], 18., places=9)
        self.assertAlmostEqual(record["coefficients_x_to_div"][2], 0., places=12)
        self.assertLess(record["fit_residual_max_px"], 1e-9)
        self.assertTrue(record["checks"]["passed"])

    def test_degree_one_reproduces_old_formula(self):
        geometry = GeometryCalibration.from_dict(self.fit(ideal_ticks(h=4.5), degree=1))
        for x in np.linspace(0, 740, 75):
            self.assertLess(abs(geometry.divisions(x) - (x - 370.25) / 18.), 1e-6)

    def test_unequal_left_right_magnification_is_recovered_by_degree_two(self):
        # f(x) = c1 u + c2 u^2 with u = x - 370: ticks placed where f = ±(k + h).
        c1, c2, h = 1 / 18., -4e-6, 4.48
        ticks = []
        for side, sign in (("left", -1), ("right", 1)):
            for k in range(13):
                roots = np.roots([c2, c1, -sign * (k + h)])
                u = min((root.real for root in roots if abs(root.imag) < 1e-12), key=abs)
                ticks.append({"side": side, "tick_id": k, "x": 370. + u})
        linear, quadratic = self.fit(ticks, 1), self.fit(ticks, 2)
        self.assertGreater(linear["fit_residual_rms_px"], .5)
        self.assertLess(quadratic["fit_residual_max_px"], 1e-6)
        self.assertAlmostEqual(quadratic["coefficients_x_to_div"][2], c2, delta=1e-10)
        self.assertAlmostEqual(quadratic["inner_half_gap_div"], h, places=6)
        # c2 < 0: f' is larger on the left, so the left side has fewer px per division.
        self.assertLess(quadratic["left_right_magnification_diff"], -.05)

    def test_checks_count_residual_and_pitch_change(self):
        missing = self.fit(ideal_ticks()[1:])
        self.assertFalse(missing["checks"]["tick_count_ok"] or missing["checks"]["passed"])
        noisy = ideal_ticks()
        for index, tick in enumerate(noisy):
            tick["x"] += (-1) ** index * .8
        self.assertFalse(self.fit(noisy)["checks"]["residual_rms_ok"])
        previous = GeometryCalibration("old_geometry", 1, 370.25, (0., 1 / 18.))
        changed = self.fit(ideal_ticks(pitch=18.9), previous=previous)
        self.assertAlmostEqual(changed["checks"]["pitch_change_vs_previous"], .05, places=6)
        self.assertTrue(changed["checks"]["passed"] and changed["checks"]["needs_confirmation"])
        same = self.fit(ideal_ticks(pitch=18.2), previous=previous)
        self.assertFalse(same["checks"]["needs_confirmation"])

    def test_bubble_ends_average_two_positions_on_the_scale(self):
        geometry = GeometryCalibration("g_geometry", 2, 370., (0., 1 / 18., 2e-5))
        measured = geometry.measure(400., 300., 500.)
        self.assertAlmostEqual(measured.offset_div, (geometry.divisions(300.) + geometry.divisions(500.)) / 2, places=12)
        self.assertNotAlmostEqual(measured.offset_div, geometry.divisions(400.), places=4)
        linear = GeometryCalibration("g_geometry", 1, 370.25, (0., 1 / 18.))
        self.assertAlmostEqual(linear.measure(401., 310., 492.).offset_div, linear.divisions(401.), places=12)


class FakeCameraService:
    def __init__(self, undistorter=None):
        self.undistorter = undistorter
        self.camera = object()

    def image_geometry(self):
        return {"coordinate_system": "undistorted", "frame_size": [960, 540], "roi_origin": [110, 190],
                "focus_absolute": 3711}


def make_runtime(directory, center=370.25):
    store = CalibrationStore(Path(directory) / "calibration")
    store.save("geometry", {"version": "20261002T072923_geometry", "polynomial_degree": 1, "x_center_px": center,
                            "coefficients_x_to_div": [0., 1 / 18.]})
    store.save("vial", {"version": "20261006T200000_vial", "mm_per_m_per_div": .0228, "zero_offset_div": .3})
    measure = MeasureProcessor(detector=Mock(), captures=Mock())
    runtime = CalibrationRuntime(store, measure)
    runtime.reload()
    return store, measure, runtime


def frame(roi, frame_id=1):
    return SimpleNamespace(frame_id=frame_id, roi=roi, capture_ms=1.)


class TickProcessorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store, self.measure, self.runtime = make_runtime(self.directory.name)
        self.published = []
        self.ticks = TickProcessor(runtime=self.runtime, camera=FakeCameraService(), publish=self.published.append,
                                   frames=5)
        self.ticks.enter()

    def run_frames(self, roi, count):
        for index in range(count):
            self.ticks.process(frame(roi, index + 1), None)

    def test_measure_fit_apply_reloads_measurement_without_restart(self):
        moved = synthetic_roi(center=381.1)
        started = self.ticks.request_measure()
        self.assertEqual(self.ticks.request_measure()["id"], started["id"])  # one at a time
        self.run_frames(moved, 5)
        result = self.ticks.result(started["id"])
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["result"]["checks"]["passed"])
        self.assertAlmostEqual(result["result"]["x_center_px"], 381.1, delta=.2)
        self.assertAlmostEqual(result["result"]["previous_scale_center_x_px"], 370.25, places=6)
        self.assertEqual(result["result"]["focus_absolute"], 3711)
        status, body = self.ticks.apply(started["id"])
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.active_state("geometry"), {"version": body["version"], "pending_confirmation": False})
        self.assertEqual(self.measure.geometry.version, body["version"])
        self.assertAlmostEqual(self.measure.geometry.zero_x_roi(), 381.1, delta=.2)
        self.assertTrue((self.store.root.parent / self.store.load("geometry")[0]["source_image"]).is_file())
        self.assertEqual(self.ticks.apply(started["id"])[1]["version"], body["version"])  # idempotent
        self.assertTrue(any(message.get("finished") for message in self.published))

    def test_occluded_ticks_fail_checks_and_cannot_be_applied(self):
        started = self.ticks.request_measure()
        self.run_frames(synthetic_roi(hide=(("left", 2), ("left", 3), ("right", 4))), 5)
        result = self.ticks.result(started["id"])
        self.assertFalse(result["result"]["checks"]["passed"])
        self.assertIn("稍微傾斜水平儀", result["message"])
        self.assertEqual(self.ticks.apply(started["id"])[1]["error_code"], "CHECKS_FAILED")
        self.assertEqual(self.store.active_state("geometry")["version"], "20261002T072923_geometry")

    def test_large_pitch_change_needs_second_confirmation(self):
        started = self.ticks.request_measure()
        self.run_frames(synthetic_roi(pitch=18.9), 5)
        self.assertEqual(self.ticks.apply(started["id"])[1]["error_code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.ticks.apply(started["id"], confirm=True)[0], 200)

    def test_leaving_mode_cancels_running_measurement(self):
        started = self.ticks.request_measure()
        self.run_frames(synthetic_roi(), 2)
        self.ticks.leave()
        self.assertEqual(self.ticks.result(started["id"])["status"], "cancelled")

    def test_rollback_restores_previous_geometry(self):
        started = self.ticks.request_measure()
        self.run_frames(synthetic_roi(center=381.1), 5)
        self.ticks.apply(started["id"])
        self.runtime.activate("geometry", "20261002T072923_geometry")
        self.assertEqual(self.measure.geometry.version, "20261002T072923_geometry")
        self.assertEqual([item["active"] for item in self.runtime.history("geometry")["versions"]], [True, False])


class PreviewTests(unittest.TestCase):
    def test_encodes_only_with_viewers_and_ends_with_mode(self):
        buffer = PreviewBuffer(max_fps=1000)
        buffer.set_source("ticks")
        self.assertFalse(buffer.wanted)
        frames = buffer.frames(timeout=.2)
        received = []
        thread = threading.Thread(target=lambda: received.extend(frames))
        thread.start()
        deadline = time.monotonic() + 2
        while not buffer.wanted and time.monotonic() < deadline:
            time.sleep(.005)
        buffer.publish(np.zeros((16, 16, 3), np.uint8))
        while not received and time.monotonic() < deadline:
            time.sleep(.005)
        buffer.set_source("")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(received[0].startswith(b"\xff\xd8"))


class TickApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store, self.measure, self.runtime = make_runtime(self.directory.name)
        self.preview = PreviewBuffer(max_fps=1000)
        self.ticks = TickProcessor(runtime=self.runtime, camera=FakeCameraService(), preview=self.preview, frames=3)
        processors = {"measure": Mock(), "align": Mock(), "ticks": self.ticks}
        self.manager = ModeManager(processors)
        self.server = TelemetryServer(
            websocket_host="127.0.0.1", websocket_port=unused_port(), dashboard_host="127.0.0.1", dashboard_port=0,
            dashboard_directory=config.APP_DIRECTORY / "dashboard", mode_handler=self.manager.request,
            routes=app.api_routes(self.manager, self.ticks, self.runtime), preview=self.preview,
            pin_guard=PinGuard("2468"))
        self.server.start()
        self.addCleanup(self.server.stop)
        self.url = f"http://127.0.0.1:{self.server._http_server.server_address[1]}"

    def call(self, path, body=None, pin=None):
        headers = {"Content-Type": "application/json", **({"X-Levelsvc-Pin": pin} if pin else {})}
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method="GET" if body is None else "POST")
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    def switch_to_ticks(self):
        self.manager.request("ticks")
        self.manager._apply_pending()

    def test_measure_requires_ticks_mode_then_apply_and_rollback(self):
        self.assertEqual(self.call("/api/ticks/measure", {})[1]["error_code"], "WRONG_MODE")
        self.switch_to_ticks()
        status, started = self.call("/api/ticks/measure", {})
        self.assertEqual(status, 202)
        for index in range(3):
            self.ticks.process(frame(synthetic_roi(center=381.1), index), None)
        status, result = self.call(f"/api/ticks/{started['id']}")
        self.assertEqual((status, result["status"]), (200, "done"))
        status, applied = self.call(f"/api/ticks/{started['id']}/apply", {})
        self.assertEqual(status, 200, applied)
        history = self.call("/api/calibration/geometry")[1]
        self.assertEqual(history["active"]["version"], applied["version"])
        self.assertEqual(len(history["versions"]), 2)
        rollback = "/api/calibration/geometry/20261002T072923_geometry/activate"
        self.assertEqual(self.call(rollback, {})[1]["error_code"], "PIN_INVALID")
        self.assertEqual(self.measure.geometry.version, applied["version"])
        status, _ = self.call(rollback, {}, pin="2468")
        self.assertEqual(status, 200)
        self.assertEqual(self.measure.geometry.version, "20261002T072923_geometry")
        self.assertEqual(self.call("/api/calibration/geometry/20250101T000000_geometry/activate", {}, pin="2468")[0], 404)
        self.assertEqual(self.call("/api/ticks/000000000000")[0], 404)

    def test_preview_only_in_preview_modes(self):
        status, body = self.call("/api/preview.mjpg")
        self.assertEqual((status, body["error_code"]), (409, "PREVIEW_UNAVAILABLE"))
        self.switch_to_ticks()
        received = []

        def read():
            with urlopen(self.url + "/api/preview.mjpg", timeout=3) as response:
                received.append(response.headers["Content-Type"])
                received.append(response.read(200))

        reader = threading.Thread(target=read)
        reader.start()
        deadline = time.monotonic() + 3
        while not self.preview.wanted and time.monotonic() < deadline:
            time.sleep(.005)
        self.ticks.process(frame(synthetic_roi()), None)
        reader.join(3)
        self.preview.set_source("")
        self.assertTrue(received[0].startswith("multipart/x-mixed-replace"))
        self.assertIn(b"Content-Type: image/jpeg", received[1])


if __name__ == "__main__":
    unittest.main()
