"""⚙ system: PIN lockout, safe shutdown order, LOG zip, sudoers rule."""

import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

import app
import camera_service
import config
import system_controller
from detector import Detection, Prediction
from system_controller import PinGuard, SystemController, capture_sessions, session_zip
from test_telemetry_server import unused_port


class PinTests(unittest.TestCase):
    def test_accepts_pin_and_locks_after_five_misses(self):
        clock = Mock(return_value=100.)
        guard = PinGuard("2468", clock=clock)
        self.assertIsNone(guard.check("2468"))
        for remaining in (4, 3, 2, 1):
            status, body = guard.check("0000")
            self.assertEqual((status, body["remaining_attempts"]), (401, remaining))
        self.assertEqual(guard.check("0000")[0], 423)
        self.assertEqual(guard.check("2468")[0], 423)  # even the right PIN waits out the lock
        clock.return_value = 161.
        self.assertIsNone(guard.check("2468"))

    def test_missing_pin_disables_protected_actions(self):
        self.assertEqual(PinGuard("").check("anything")[1]["error_code"], "PIN_NOT_CONFIGURED")
        with patch.dict(os.environ, {"LEVELSVC_PIN": "1357"}):
            self.assertIsNone(PinGuard().check("1357"))


class FakeCaptures:
    def __init__(self, log, busy_polls=3):
        self.log, self.busy_polls = log, busy_polls

    def set_ready(self, ready):
        self.log.append(f"ready={ready}")

    def idle(self):
        self.busy_polls -= 1
        if self.busy_polls <= 0:
            self.log.append("writer drained")
            return True
        return False

    def cancel_pending(self, code, message):
        self.log.append(f"cancel {code}")
        return 0


class SafeShutdownTests(unittest.TestCase):
    def test_order_stop_captures_drain_close_camera_respond_then_power_off(self):
        log = []
        controller = SystemController(FakeCaptures(log), run_command=lambda argv: log.append(" ".join(argv)),
                                      command_delay=0)

        def main_loop():
            controller.stop_requested.wait(5)
            log.append("loop stopped, camera closed")
            controller.camera_has_closed()
            log.append("main thread continues cleanup")

        loop = threading.Thread(target=main_loop)
        loop.start()
        status, body, after = controller.request("shutdown")
        log.append("response sent")
        after()
        loop.join(5)
        deadline = time.monotonic() + 2
        while "/usr/bin/systemctl poweroff" not in log and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual((status, body["status"], body["camera_closed"]), (200, "safe_to_power_off", True))
        self.assertIn("可以斷電", body["message"])
        order = [entry for entry in log if entry != "main thread continues cleanup"]
        self.assertEqual(order, ["ready=False", "writer drained", "cancel SERVER_SHUTDOWN",
                                 "loop stopped, camera closed", "response sent", "/usr/bin/systemctl poweroff"])
        self.assertEqual(controller.request("reboot")[1]["error_code"], "BUSY")

    def test_process_waits_until_the_power_command_ran(self):
        ran = threading.Event()
        controller = SystemController(FakeCaptures([]), run_command=lambda argv: (time.sleep(.3), ran.set()),
                                      command_delay=0)
        controller.camera_closed.set()
        status, body, after = controller.request("reboot")
        after()
        controller.wait_for_power_command(timeout=5)
        self.assertTrue(ran.is_set())
        restart = SystemController(Mock(), run_command=lambda argv: None)
        restart.request("restart-service")
        started = time.monotonic()
        restart.wait_for_power_command(timeout=5)  # never waits on its own restart
        self.assertLess(time.monotonic() - started, .1)

    def test_restart_service_runs_after_the_response(self):
        calls = []
        controller = SystemController(Mock(), run_command=calls.append, command_delay=0)
        status, body, after = controller.request("restart-service")
        self.assertEqual((status, calls), (202, []))
        after()
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(calls, [["/usr/bin/systemctl", "restart", "levelsvc"]])
        with self.assertRaises(ValueError):
            SystemController(Mock()).request("format-disk")

    def test_dry_run_only_logs_the_command(self):
        with patch.object(config, "SYSTEM_DRY_RUN", True), patch.object(system_controller.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            system_controller.run_system_command(["/usr/bin/systemctl", "poweroff"])
        run.assert_not_called()
        self.assertIn("[dry-run] sudo -n /usr/bin/systemctl poweroff", output.getvalue())


def make_session(root, calibration_root):
    session = root / "20261007_010000_abcdef12"
    (session / "images").mkdir(parents=True)
    (session / "images" / "x_01_clean.jpg").write_bytes(b"jpg")
    (session / "images" / "x_raw.png").write_bytes(b"png")
    (session / "session_metadata.json").write_text(json.dumps({"calibration": {
        "geometry_version": "20261007T001604_geometry", "vial_version": "20261007T001334_vial", "alignment_version": ""}}))
    with (session / "pi_capture_log.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, ["sample_id", "geometry_version", "vial_version", "alignment_version"])
        writer.writeheader()
        writer.writerow({"sample_id": 1, "geometry_version": "20261007T001604_geometry",
                         "vial_version": "20261007T001334_vial", "alignment_version": "20261007T020000_alignment"})
        writer.writerow({"sample_id": 1, "geometry_version": "20261007T001604_geometry",
                         "vial_version": "20261007T001334_vial", "alignment_version": "20261007T020000_alignment"})
    for kind, names in (("geometry", ["20261007T001604_geometry.json", "20261007T001604_geometry_roi.png",
                                      "20261002T072923_geometry.json"]),
                        ("vial", ["20261007T001334_vial.json"]), ("alignment", ["20261007T020000_alignment.json"])):
        (calibration_root / kind).mkdir(parents=True, exist_ok=True)
        for name in names:
            (calibration_root / kind / name).write_text("{}")
    return session


class LogDownloadTests(unittest.TestCase):
    def test_zip_has_session_files_and_only_the_calibrations_it_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calibration = Path(directory) / "manual_captures", Path(directory) / "calibration"
            session = make_session(root, calibration)
            (root / "not-a-session").mkdir()
            self.assertEqual(capture_sessions(root), [{"session_id": session.name, "size_mb": unittest.mock.ANY,
                                                       "samples": 1, "rows": 2}])
            path = session_zip(session.name, root, calibration)
            try:
                names = set(zipfile.ZipFile(path).namelist())
            finally:
                path.unlink()
            prefix = session.name + "/"
            self.assertEqual(names, {prefix + name for name in (
                "images/x_01_clean.jpg", "images/x_raw.png", "session_metadata.json", "pi_capture_log.csv",
                "calibration/geometry/20261007T001604_geometry.json",
                "calibration/geometry/20261007T001604_geometry_roi.png",
                "calibration/vial/20261007T001334_vial.json",
                "calibration/alignment/20261007T020000_alignment.json")})
            for bad in ("../etc", "20261007_010000_ABCDEF12"):
                with self.assertRaises(ValueError):
                    session_zip(bad, root, calibration)
            with self.assertRaises(FileNotFoundError):
                session_zip("20261007_010000_00000000", root, calibration)


class ServiceShutdownIntegrationTests(unittest.TestCase):
    """app.run() with a fake camera and real HTTP: PIN-protected safe shutdown."""

    def test_shutdown_over_http_stops_loop_closes_camera_then_powers_off(self):
        frame = np.zeros((540, 960, 3), np.uint8)
        camera = Mock(frame_undistorter=Mock(process=lambda image: image), focus_absolute=3711)
        camera.capture_array.side_effect = lambda stream: (time.sleep(.01), frame)[1]
        detector = Mock()
        detector.predict.return_value = Prediction(None, Detection(), 1., 0., 1., 0.)
        commands, http_port = [], unused_port()
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            for name, value in (("LOG_DIRECTORY", Path(directory)), ("CALIBRATION_DIRECTORY", Path(directory) / "cal"),
                                ("ENABLE_IMAGE_STREAM", False), ("ENABLE_CONTINUOUS_CSV", False),
                                ("WEBSOCKET_HOST", "127.0.0.1"), ("DASHBOARD_HOST", "127.0.0.1"),
                                ("WEBSOCKET_PORT", unused_port()), ("DASHBOARD_PORT", http_port),
                                ("SYSTEM_COMMAND_DELAY_SECONDS", 0), ("POWER_MONITOR_ENABLED", False)):
                stack.enter_context(patch.object(config, name, value))
            stack.enter_context(patch.dict(os.environ, {"LEVELSVC_PIN": "2468"}))
            stack.enter_context(patch.object(app, "YoloDetector", return_value=detector))
            stack.enter_context(patch.object(app, "create_camera", return_value=camera))
            stack.enter_context(patch.object(system_controller, "run_system_command", side_effect=commands.append))
            stack.enter_context(patch.object(app, "close_windows"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            service = threading.Thread(target=app.run)
            service.start()
            url = f"http://127.0.0.1:{http_port}"

            def post(path, pin=None):
                request = Request(url + path, data=b"{}", headers={"Content-Type": "application/json",
                                                                   **({"X-Levelsvc-Pin": pin} if pin else {})})
                try:
                    response = urlopen(request, timeout=15)
                except HTTPError as error:
                    response = error
                with response:
                    return response.code, json.loads(response.read())

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    urlopen(url + "/api/state", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.05)
            self.assertEqual(post("/api/system/shutdown")[0], 401)
            camera.close.assert_not_called()
            status, body = post("/api/system/shutdown", pin="2468")
            service.join(10)
            deadline = time.monotonic() + 3
            while not commands and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual((status, body["status"], body["camera_closed"]), (200, "safe_to_power_off", True))
        self.assertFalse(service.is_alive())
        camera.stop.assert_called_once_with()
        camera.close.assert_called_once_with()
        self.assertEqual(commands, [["/usr/bin/systemctl", "poweroff"]])


class BatteryShutdownIntegrationTests(unittest.TestCase):
    """app.run() with a fake camera: a low battery counts down, then powers off by itself."""

    def test_low_battery_counts_down_closes_camera_and_powers_off(self):
        frame = np.zeros((540, 960, 3), np.uint8)
        camera = Mock(frame_undistorter=Mock(process=lambda image: image), focus_absolute=3711)
        camera.capture_array.side_effect = lambda stream: (time.sleep(.01), frame)[1]
        detector = Mock()
        detector.predict.return_value = Prediction(None, Detection(), 1., 0., 1., 0.)
        battery = Mock()
        battery.snapshot.return_value = {"battery_v": 3.0, "discharge_a": 1.7, "output_5v_v": 5.2, "output_5v_a": .7,
                                         "charging": False, "throttled_hex": "0x0", "status": "ok"}
        battery.details.return_value = {"alarm_level": "shutdown", "error": ""}
        commands, published = [], []
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            for name, value in (("LOG_DIRECTORY", Path(directory)), ("CALIBRATION_DIRECTORY", Path(directory) / "cal"),
                                ("ENABLE_IMAGE_STREAM", False), ("ENABLE_CONTINUOUS_CSV", False), ("ENABLE_WEBSOCKET", False),
                                ("SYSTEM_COMMAND_DELAY_SECONDS", 0), ("POWER_MONITOR_ENABLED", True),
                                ("POWER_SHUTDOWN_COUNTDOWN_SECONDS", 1.5)):
                stack.enter_context(patch.object(config, name, value))
            stack.enter_context(patch.object(app, "PowerMonitor", return_value=battery))
            stack.enter_context(patch.object(app, "YoloDetector", return_value=detector))
            stack.enter_context(patch.object(app, "create_camera", return_value=camera))
            stack.enter_context(patch.object(system_controller, "run_system_command", side_effect=commands.append))
            stack.enter_context(patch.object(app, "close_windows"))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            service = threading.Thread(target=app.run)
            started = time.monotonic()
            service.start()
            service.join(15)
        self.assertFalse(service.is_alive())
        self.assertGreater(time.monotonic() - started, 1.5)  # the countdown was honoured
        camera.close.assert_called_once_with()
        self.assertEqual(commands, [["/usr/bin/systemctl", "poweroff"]])
        self.assertIn("電池電壓過低", output.getvalue())


class DeployTests(unittest.TestCase):
    def test_sudoers_allows_exactly_the_three_commands(self):
        text = (config.APP_DIRECTORY / "deploy/levelsvc.sudoers").read_text()
        rule = next(line for line in text.splitlines() if line and not line.startswith("#"))
        commands = rule.split("NOPASSWD:", 1)[1].split(",")
        self.assertEqual(sorted(command.strip() for command in commands),
                         sorted(" ".join(argv) for argv in config.SYSTEM_COMMANDS.values()))
        self.assertNotIn("ALL=(ALL", rule)

    @unittest.skipUnless(shutil.which("visudo"), "visudo unavailable")
    def test_sudoers_syntax(self):
        result = subprocess.run(["visudo", "-cf", str(config.APP_DIRECTORY / "deploy/levelsvc.sudoers")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
