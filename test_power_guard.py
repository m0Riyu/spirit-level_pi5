"""PowerGuard: warning, countdown, cancel on charging, safe shutdown, unavailable."""

import unittest
from unittest.mock import Mock

import config
import power_monitor as pm
from power_guard import PowerGuard


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeMonitor:
    def __init__(self):
        self.level = "normal"
        self.battery_v = 3.6
        self.charging = False
        self.status = "ok"
        self.alarm = Mock()
        self.alarm.reset.side_effect = lambda: setattr(self, "level", "normal")

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {"battery_v": self.battery_v if self.status == "ok" else None, "discharge_a": 1.7, "output_5v_v": 5.2,
                "output_5v_a": .7, "charging": self.charging, "throttled_hex": "0x0", "status": self.status}

    def details(self):
        return {"alarm_level": self.level, "error": "" if self.status == "ok" else "I2C 讀取失敗"}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.monitor = FakeMonitor()
        self.after = Mock()
        self.controller = Mock()
        self.controller.request.return_value = (200, {"status": "safe_to_power_off"}, self.after)
        self.published = []
        self.guard = PowerGuard(self.monitor, self.controller, publish=self.published.append, clock=self.clock,
                                countdown_seconds=30, publish_interval_seconds=2)

    def step(self, seconds=1.0):
        self.clock.now += seconds
        return self.guard.check()

    def test_normal_and_warning_notify_without_shutdown(self):
        self.assertEqual(self.step()["state"], "normal")
        self.monitor.level = "warning"
        status = self.step()
        self.assertEqual(status["state"], "warning")
        self.assertEqual(self.published[-1]["type"], "power")
        self.assertEqual(self.published[-1]["state"], "warning")  # change is published at once
        self.assertEqual((status["warn_v"], status["shutdown_v"]), (config.POWER_WARN_VOLTAGE_V, config.POWER_SHUTDOWN_VOLTAGE_V))
        self.assertTrue(status["thresholds_provisional"])
        self.assertFalse(status["current_calibrated"])
        self.controller.request.assert_not_called()

    def test_countdown_then_safe_shutdown_exactly_once(self):
        self.monitor.level = "shutdown"
        status = self.step()
        self.assertEqual((status["state"], status["countdown_remaining_seconds"]), ("countdown", 30))
        for _ in range(29):
            status = self.step()
        self.assertEqual(status["countdown_remaining_seconds"], 1)
        self.controller.request.assert_not_called()
        self.assertTrue(all(message["state"] == "countdown" for message in self.published[-29:]))  # every second
        status = self.step()
        self.controller.request.assert_called_once_with("shutdown")
        self.after.assert_called_once_with()  # poweroff runs after the camera is closed
        self.assertEqual(self.step()["state"], "shutting_down")
        self.step()
        self.controller.request.assert_called_once()

    def test_charging_cancels_the_countdown(self):
        self.monitor.level = "shutdown"
        self.step()
        self.step(10)
        self.monitor.charging = True
        status = self.step()
        self.assertEqual(status["state"], "normal")
        self.monitor.alarm.reset.assert_called_once_with()
        self.monitor.charging = False
        self.monitor.level = "shutdown"  # dropping again later starts a full new countdown
        self.assertEqual(self.step()["countdown_remaining_seconds"], 30)
        self.controller.request.assert_not_called()

    def test_unavailable_monitor_never_shuts_down(self):
        self.monitor.status = "unavailable"
        self.monitor.level = "shutdown"
        for _ in range(60):
            status = self.step()
        self.assertEqual(status["state"], "unavailable")
        self.assertIn("I2C", status["error"])
        self.controller.request.assert_not_called()

    def test_busy_controller_is_not_called_twice(self):
        self.controller.request.return_value = (409, {"error_code": "BUSY"})
        self.monitor.level = "shutdown"
        for _ in range(35):
            self.step()
        self.controller.request.assert_called_once_with("shutdown")

    def test_real_alarm_end_to_end_from_voltage(self):
        alarm = pm.BatteryAlarm(pm.ThresholdConfig(3.3, 3.1, .05, 10), clock=self.clock)
        self.monitor.alarm = alarm
        self.monitor.details = lambda: {"alarm_level": alarm.update(self.monitor.battery_v), "error": ""}
        self.monitor.battery_v = 3.05
        states = [self.step()["state"] for _ in range(45)]
        self.assertEqual(states[:10], ["normal"] * 10)  # debounce: nothing for the first 10 s
        self.assertIn("countdown", states)
        self.assertEqual(states.index("countdown"), 10)  # 10 s below 3.1 V
        self.controller.request.assert_called_once_with("shutdown")  # 10 s + 30 s countdown


if __name__ == "__main__":
    unittest.main()
