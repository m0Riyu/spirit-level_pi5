"""Tests for WebSocket payloads and server transport."""

import asyncio
import json
import math
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from telemetry_server import TelemetryServer, build_telemetry_payload


def unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TelemetryPayloadTests(unittest.TestCase):
    def setUp(self):
        self.detection = SimpleNamespace(
            detected=1,
            detection_count=1,
            confidence=0.91,
        )
        self.measurement = SimpleNamespace(
            valid=1,
            error="",
            center_x_roi=409.0,
            scale_center_x_roi=373.0,
            pitch_px_per_div=19.0,
            offset_px=36.0,
            offset_div=36.0 / 19.0,
            direction="right",
        )
        self.timings = {
            "capture_ms": 4.0,
            "predict_ms": 18.0,
            "yolo_inference_ms": 16.4,
            "process_ms": 24.0,
            "fps": 30.0,
        }

    def build(self):
        return build_telemetry_payload(
            7,
            self.detection,
            self.measurement,
            self.timings,
            mm_per_m_per_div=0.02,
            level_tolerance_mm_per_m=0.01,
            max_measurable_slope_mm_per_m=0.1,
        )

    def test_uses_json_pitch_for_physical_conversion(self):
        payload = self.build()
        measurement = payload["measurement"]
        self.assertEqual(measurement["pitch_px_per_div"], 19.0)
        self.assertEqual(measurement["pixels_per_1_mmm"], 950.0)
        self.assertAlmostEqual(measurement["slope_mm_per_m"], 36.0 / 950.0)
        self.assertAlmostEqual(
            measurement["angle_degrees"],
            math.degrees(math.atan((36.0 / 950.0) / 1000.0)),
        )
        self.assertEqual(payload["system_state"], "ADJUST")

    def test_small_offset_is_level(self):
        self.measurement.offset_px = 5.0
        self.measurement.offset_div = 5.0 / 19.0
        self.assertEqual(self.build()["system_state"], "LEVEL")

    def test_positive_boundary_is_level(self):
        self.measurement.offset_px = 9.5
        self.measurement.offset_div = 0.5
        self.assertAlmostEqual(
            self.measurement.offset_px / 950.0, 0.01
        )
        self.assertEqual(self.build()["system_state"], "LEVEL")

    def test_negative_boundary_is_level(self):
        self.measurement.offset_px = -9.5
        self.measurement.offset_div = -0.5
        self.assertEqual(self.build()["system_state"], "LEVEL")

    def test_outside_level_range_requires_adjustment(self):
        self.measurement.offset_px = 9.6
        self.measurement.offset_div = 9.6 / 19.0
        self.assertEqual(self.build()["system_state"], "ADJUST")

    def test_official_positive_boundary_is_in_range(self):
        self.measurement.offset_px = 95.0
        self.measurement.offset_div = 5.0
        payload = self.build()
        self.assertEqual(payload["system_state"], "ADJUST")
        self.assertTrue(payload["measurement"]["within_official_range"])

    def test_official_negative_boundary_is_in_range(self):
        self.measurement.offset_px = -95.0
        self.measurement.offset_div = -5.0
        payload = self.build()
        self.assertEqual(payload["system_state"], "ADJUST")
        self.assertTrue(payload["measurement"]["within_official_range"])

    def test_above_official_range_is_out_of_range(self):
        self.measurement.offset_px = 95.1
        self.measurement.offset_div = 95.1 / 19.0
        payload = self.build()
        self.assertEqual(payload["system_state"], "OUT_OF_RANGE")
        self.assertFalse(payload["measurement"]["within_official_range"])

    def test_below_official_range_is_out_of_range(self):
        self.measurement.offset_px = -95.1
        self.measurement.offset_div = -95.1 / 19.0
        payload = self.build()
        self.assertEqual(payload["system_state"], "OUT_OF_RANGE")
        self.assertFalse(payload["measurement"]["within_official_range"])

    def test_missing_detection_is_searching(self):
        self.detection.detected = 0
        self.detection.detection_count = 0
        self.measurement.valid = 0
        payload = self.build()
        self.assertEqual(payload["system_state"], "SEARCHING")
        self.assertIsNone(payload["measurement"]["slope_mm_per_m"])


class TelemetryServerIntegrationTests(unittest.TestCase):
    def test_serves_dashboard_and_broadcasts(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory)
            (dashboard / "index.html").write_text("dashboard-ok", encoding="utf-8")
            websocket_port = unused_port()
            dashboard_port = unused_port()
            server = TelemetryServer(
                websocket_host="127.0.0.1",
                websocket_port=websocket_port,
                dashboard_host="127.0.0.1",
                dashboard_port=dashboard_port,
                dashboard_directory=dashboard,
            )
            server.start()
            try:
                page = urllib.request.urlopen(
                    f"http://127.0.0.1:{dashboard_port}", timeout=2
                ).read()
                self.assertIn(b"dashboard-ok", page)
                time_response = json.loads(
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{dashboard_port}/time", timeout=2
                    ).read()
                )
                self.assertGreater(time_response["server_time_epoch_ms"], 0)
                received = asyncio.run(
                    self._receive_message(server, websocket_port)
                )
                self.assertEqual(received["frame_id"], 42)
            finally:
                server.stop()

    async def _receive_message(self, server, websocket_port):
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{websocket_port}") as websocket:
            hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
            self.assertEqual(hello["type"], "hello")
            server.publish({"type": "telemetry", "frame_id": 42})
            raw = await asyncio.wait_for(websocket.recv(), timeout=2)
            return json.loads(raw)


class DashboardLayoutTests(unittest.TestCase):
    def test_bubble_card_precedes_other_measurements(self):
        html = (Path(__file__).parent / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        bubble_card = html.index('<section class="card level-visual">')
        state_card = html.index('id="stateCard"')
        slope_card = html.index('<section class="card hero-reading">')
        self.assertLess(bubble_card, state_card)
        self.assertLess(bubble_card, slope_card)

    def test_performance_metrics_are_collapsed_by_default(self):
        html = (Path(__file__).parent / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('<details class="card diagnostics">', html)
        self.assertNotIn('<details class="card diagnostics" open>', html)
        for label in (
            "傳輸效率",
            "數據延遲",
            "推理時間",
            "系統總延遲",
        ):
            self.assertIn(label, html)

    def test_performance_metrics_use_rolling_aggregates(self):
        html = (Path(__file__).parent / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("const RATE_WINDOW_MS = 3000", html)
        self.assertIn("const FRAME_SAMPLE_COUNT = 30", html)
        self.assertIn("function median(samples)", html)
        self.assertIn("async function calibrateTime()", html)
        self.assertIn("estimatedServerNow - resultReadyAt", html)
        self.assertIn("estimatedServerNow - capturedAt", html)
        self.assertIn("setInterval(updatePerformanceMetrics, 500)", html)

    def test_out_of_range_hides_measurement_values(self):
        html = (Path(__file__).parent / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('OUT_OF_RANGE: "超出範圍"', html)
        self.assertIn("function showOutOfRange()", html)
        self.assertIn(
            "measurement.valid && measurement.within_official_range", html
        )


if __name__ == "__main__":
    unittest.main()
