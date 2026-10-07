"""Battery protection for the service: warn, count down, then shut down safely.

PowerMonitor reads the INA219s once a second and its BatteryAlarm decides
normal / warning / shutdown from the battery voltage under load (debounced,
with hysteresis; thresholds 暫定 in config.py). PowerGuard turns that into:

  warning   -> notify the phone (yellow banner)
  shutdown  -> notify with a countdown (red banner); when it ends, run the
               same safe shutdown as the ⚙ page (captures written, camera
               closed, then poweroff)
  charging  -> a running countdown is cancelled and the alarm reset

If the monitor is unavailable (I2C error) nothing is shut down: the state is
reported as unavailable so the page can say so.
"""

import logging
import threading
import time

import config


class PowerGuard:
    def __init__(self, monitor, controller, *, publish=None, clock=time.monotonic,
                 countdown_seconds=None, interval_seconds=1.0, publish_interval_seconds=None):
        self.monitor = monitor
        self.controller = controller
        self.publish = publish
        self.clock = clock
        self.countdown_seconds = (config.POWER_SHUTDOWN_COUNTDOWN_SECONDS
                                  if countdown_seconds is None else countdown_seconds)
        self.interval_seconds = interval_seconds
        self.publish_interval_seconds = (config.POWER_PUBLISH_INTERVAL_SECONDS
                                         if publish_interval_seconds is None else publish_interval_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._countdown_started = None
        self._shutdown_requested = False
        self._next_publish = 0.0
        self._last_state = None
        self._status = self._compose(self.monitor.snapshot(), self.monitor.details(), "unavailable", None)

    # ---- lifecycle --------------------------------------------------------
    def start(self):
        self.monitor.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="power-guard", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self.monitor.stop()

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            try:
                self.check()
            except Exception:  # protection must keep running whatever one sample does
                logging.getLogger(__name__).exception("power guard check failed")

    def status(self):
        with self._lock:
            return dict(self._status)

    # ---- decision ---------------------------------------------------------
    def check(self):
        """One decision step (called every second by the thread; tests call it directly)."""
        snapshot, details = self.monitor.snapshot(), self.monitor.details()
        now = self.clock()
        level = details.get("alarm_level", "normal")
        remaining = None
        if snapshot["status"] != "ok":
            state = "unavailable"
        elif level == "shutdown" and snapshot["charging"] is True:
            # Mains came back during the countdown: keep running.
            self.monitor.alarm.reset()
            self._countdown_started = None
            state = "normal"
        elif level == "shutdown":
            if self._countdown_started is None:
                self._countdown_started = now
            remaining = max(0.0, self.countdown_seconds - (now - self._countdown_started))
            state = "shutting_down" if self._shutdown_requested else "countdown"
        else:
            state = "warning" if level == "warning" else "normal"
        if state in ("normal", "warning"):
            self._countdown_started = None
        status = self._compose(snapshot, details, state, remaining)
        with self._lock:
            self._status = status
        changed = state != self._last_state
        self._last_state = state
        if self.publish is not None and (changed or state == "countdown" or now >= self._next_publish):
            self._next_publish = now + self.publish_interval_seconds
            self.publish({"type": "power", "schema_version": 1, **status})
        if state == "countdown" and remaining == 0 and not self._shutdown_requested:
            self._shutdown_requested = True
            self._shut_down()
        return status

    def _shut_down(self):
        print("電池電壓過低：開始安全關機。", flush=True)
        result = self.controller.request("shutdown")
        if len(result) > 2 and result[2] is not None:
            result[2]()  # run poweroff after the camera is closed (no HTTP response to wait for)

    @staticmethod
    def _compose(snapshot, details, state, remaining):
        return {
            "state": state,  # normal / warning / countdown / shutting_down / unavailable
            "battery_v": snapshot.get("battery_v"),
            "discharge_a": snapshot.get("discharge_a"),
            "output_5v_v": snapshot.get("output_5v_v"),
            "output_5v_a": snapshot.get("output_5v_a"),
            "charging": snapshot.get("charging"),
            "throttled_hex": snapshot.get("throttled_hex"),
            "monitor_status": snapshot.get("status"),
            "error": details.get("error", ""),
            "countdown_remaining_seconds": remaining,
            "warn_v": config.POWER_WARN_VOLTAGE_V,
            "shutdown_v": config.POWER_SHUTDOWN_VOLTAGE_V,
            "thresholds_provisional": config.POWER_THRESHOLDS_PROVISIONAL,
            "current_calibrated": False,
        }
