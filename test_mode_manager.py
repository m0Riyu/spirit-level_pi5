"""One camera open, one processor per frame, switching between frames."""

import json
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from websockets.sync.client import connect

import config
from camera_service import CameraService
from manual_capture import CaptureManager
from mode_manager import MODES, ModeManager
from processors.align import AlignProcessor
from processors.measure import MeasureProcessor
from processors.ticks import TickProcessor
from telemetry_server import TelemetryServer
from test_manual_capture import freeze
from test_telemetry_server import unused_port


class FakeCamera:
    """Stands in for Picamera2 + undistorter: ~30 fps frames, counts opens."""
    def __init__(self, frame_seconds=.03):
        self.frame_seconds = frame_seconds
        self.frame_undistorter = Mock(process=lambda image: image)

    def capture_array(self, stream):
        time.sleep(self.frame_seconds)
        return np.zeros((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), np.uint8)


class RecordingProcessor:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def enter(self):
        self.log.append(("enter", self.name))

    def leave(self):
        self.log.append(("leave", self.name))

    def before_capture(self):
        return self.name

    def process(self, frame, context):
        self.log.append(("frame", self.name, frame.frame_id))
        return False


def recording_manager(log, **kwargs):
    return ModeManager({mode: RecordingProcessor(mode, log) for mode in MODES}, **kwargs)


class ModeManagerTests(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=lambda: FakeCamera(0))
        self.camera = CameraService(self.factory)
        self.camera.open()

    def test_switch_happens_between_frames_and_never_reopens_camera(self):
        log, states = [], []
        manager = recording_manager(log, on_change=states.append)
        manager.step(self.camera)
        manager.request("ticks", "phone-a")
        manager.step(self.camera)
        manager.request("align", "phone-b")
        manager.step(self.camera)
        self.assertEqual(log, [("enter", "measure"), ("frame", "measure", 1),
                               ("leave", "measure"), ("enter", "ticks"), ("frame", "ticks", 2),
                               ("leave", "ticks"), ("enter", "align"), ("frame", "align", 3)])
        self.factory.assert_called_once_with()
        self.assertEqual(self.camera.open_count, 1)
        self.assertEqual([(state["mode"], state["changed_by"], state["mode_sequence"]) for state in states],
                         [("ticks", "phone-a", 1), ("align", "phone-b", 2)])

    def test_last_request_wins_and_same_mode_does_not_reenter(self):
        log = []
        manager = recording_manager(log)
        manager.step(self.camera)
        manager.request("align", "a")
        manager.request("measure", "b")  # arrives before the next frame
        manager.step(self.camera)
        self.assertEqual([entry for entry in log if entry[0] != "frame"], [("enter", "measure")])
        state = manager.state()
        self.assertEqual((state["mode"], state["changed_by"], state["pending_mode"]), ("measure", "b", None))

    def test_invalid_mode_is_rejected(self):
        manager = recording_manager([])
        for mode in ("system", "", None):
            with self.assertRaises(ValueError):
                manager.request(mode)

    def test_switch_latency_is_below_half_a_second_at_camera_frame_rate(self):
        camera = CameraService(lambda: FakeCamera(.035))  # slower than the real ~30 fps
        camera.open()
        manager = recording_manager([])
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                manager.step(camera)

        thread = threading.Thread(target=loop)
        thread.start()
        try:
            latencies = []
            for mode in ("ticks", "align", "measure", "align", "ticks", "measure"):
                sequence = manager.state()["mode_sequence"]
                manager.request(mode, "phone")
                deadline = time.monotonic() + 2
                while manager.state()["mode_sequence"] == sequence and time.monotonic() < deadline:
                    time.sleep(.002)
                latencies.append(manager.state()["last_switch_ms"])
        finally:
            stop.set()
            thread.join()
        self.assertLess(max(latencies), 500)
        self.assertEqual(camera.open_count, 1)
        print(f"Mode switch latency: max {max(latencies):.1f} ms over {len(latencies)} switches")

    def test_only_measure_mode_runs_yolo(self):
        detector = Mock()
        with tempfile.TemporaryDirectory() as directory:
            captures = CaptureManager(directory, default_burst_frames=1)
            try:
                measure = MeasureProcessor(detector=detector, captures=captures)
                manager = ModeManager({"measure": measure, "align": AlignProcessor(), "ticks": TickProcessor()},
                                      initial="ticks")
                for mode in ("ticks", "align"):
                    manager.request(mode)
                    for _ in range(3):
                        manager.step(self.camera)
                detector.predict.assert_not_called()
            finally:
                captures.close()


class MeasureModeCaptureTests(unittest.TestCase):
    def test_leaving_measure_ends_unfinished_burst_and_blocks_new_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            captures = CaptureManager(directory)
            try:
                measure = MeasureProcessor(detector=Mock(), captures=captures)
                measure.enter()
                queued, burst = str(uuid.uuid4()), str(uuid.uuid4())
                captures.submit(burst, {"burst_frames": 5})
                freeze(captures, captures.begin_frame())  # 1 of 5 frames collected
                self.assertEqual(captures.get_status(burst)["status"], "processing")
                captures.submit(queued)
                with self.assertLogs("manual_capture", level="ERROR"):
                    measure.leave()
                for request_id in (queued, burst):
                    self.assertEqual(captures.get_status(request_id)["error_code"], "MODE_CHANGED")
                self.assertEqual(captures.submit(str(uuid.uuid4()))[0], 503)
                self.assertEqual(captures.begin_frame(), [])
                measure.enter()
                self.assertTrue(captures.ready)
            finally:
                captures.close()


class StateApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = recording_manager([])
        self.websocket_port = unused_port()
        self.server = TelemetryServer(
            websocket_host="127.0.0.1", websocket_port=self.websocket_port, dashboard_host="127.0.0.1", dashboard_port=0,
            dashboard_directory=config.APP_DIRECTORY / "dashboard",
            state_provider=lambda: {"type": "state", **self.manager.state()}, mode_handler=self.manager.request)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server._http_server.server_address[1]}"

    def tearDown(self):
        self.server.stop()

    def call(self, path, body=None, headers=None):
        request = Request(self.url + path, data=body, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    def test_get_state_and_post_mode(self):
        self.assertEqual(self.call("/api/state")[1]["mode"], "measure")
        status, state = self.call("/api/mode", json.dumps({"mode": "ticks", "client_id": "phone-1"}).encode())
        self.assertEqual((status, state["pending_mode"]), (200, "ticks"))

    def test_bad_mode_requests_are_400_and_foreign_origin_403(self):
        for body in (b'{"mode": "system"}', b'{"mode": "align", "extra": 1}', b'{"mode": "align", "client_id": 5}', b"[]"):
            self.assertEqual(self.call("/api/mode", body)[0], 400, body)
        self.assertEqual(self.call("/api/mode", b'{"mode": "align"}', {"Origin": "http://evil.example"})[0], 403)
        self.assertIsNone(self.manager.state()["pending_mode"])

    def test_state_message_is_not_dropped_by_following_telemetry(self):
        with connect(f"ws://127.0.0.1:{self.websocket_port}") as websocket:
            self.assertEqual(json.loads(websocket.recv(timeout=3))["type"], "hello")
            self.server.publish({"type": "state", "mode": "ticks"})
            for frame_id in range(20):
                self.server.publish({"type": "telemetry", "frame_id": frame_id})
            received = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not ({"state", "telemetry"} <= {m["type"] for m in received}
                                                         and any(m.get("frame_id") == 19 for m in received)):
                received.append(json.loads(websocket.recv(timeout=3)))
        self.assertIn({"type": "state", "mode": "ticks"}, received)
        self.assertEqual(received[-1].get("frame_id"), 19)


if __name__ == "__main__":
    unittest.main()
