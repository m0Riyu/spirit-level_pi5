"""⚙ system: status, PIN, safe restart/reboot/shutdown, LOG download."""

import csv
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import config
from manual_capture import git_value

STARTED_MONOTONIC = time.monotonic()
_GIT = {}


def git_commit():
    if "commit" not in _GIT:
        _GIT["commit"] = git_value("rev-parse", "--short", "HEAD")
        _GIT["branch"] = git_value("branch", "--show-current")
    return _GIT["commit"], _GIT["branch"]


def system_summary():
    result = {"cpu_temperature_c": None, "load_average_1m": None, "disk_free_mb": None,
              "service_uptime_seconds": time.monotonic() - STARTED_MONOTONIC}
    try:
        result["cpu_temperature_c"] = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        pass
    try:
        result["load_average_1m"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    try:
        result["disk_free_mb"] = shutil.disk_usage(config.LOG_DIRECTORY if config.LOG_DIRECTORY.exists()
                                                   else config.PROJECT_DIRECTORY).free / 1024 ** 2
    except OSError:
        pass
    result["git_commit"], result["git_branch"] = git_commit()
    return result


class PinGuard:
    """PIN for power and calibration-rollback actions; locks after repeated misses."""

    def __init__(self, pin=None, max_failures=None, lockout_seconds=None, clock=time.monotonic):
        self.pin = os.environ.get(config.PIN_ENVIRONMENT_VARIABLE, "") if pin is None else pin
        self.max_failures = max_failures or config.PIN_MAX_FAILURES
        self.lockout_seconds = lockout_seconds or config.PIN_LOCKOUT_SECONDS
        self.clock = clock
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def check(self, supplied):
        """None when accepted, else (status, error body)."""
        if not self.pin:
            return 503, {"status": "error", "error_code": "PIN_NOT_CONFIGURED",
                         "message": "PIN 未設定（/etc/levelsvc/env），此操作停用"}
        with self._lock:
            now = self.clock()
            if now < self._locked_until:
                return 423, {"status": "error", "error_code": "PIN_LOCKED",
                             "message": f"PIN 錯誤次數過多，請 {self._locked_until - now:.0f} 秒後再試",
                             "retry_after_seconds": self._locked_until - now}
            if hmac.compare_digest(str(supplied or "").encode(), self.pin.encode()):
                self._failures = 0
                return None
            self._failures += 1
            remaining = self.max_failures - self._failures
            if remaining <= 0:
                self._failures, self._locked_until = 0, now + self.lockout_seconds
                return 423, {"status": "error", "error_code": "PIN_LOCKED",
                             "message": f"PIN 連續錯誤 {self.max_failures} 次，鎖定 {self.lockout_seconds} 秒",
                             "retry_after_seconds": self.lockout_seconds}
            return 401, {"status": "error", "error_code": "PIN_INVALID",
                         "message": f"PIN 錯誤（再錯 {remaining} 次將鎖定）", "remaining_attempts": remaining}


def run_system_command(argv):
    if config.SYSTEM_DRY_RUN:
        print(f"[dry-run] sudo -n {' '.join(argv)}", flush=True)
        return
    subprocess.run(["sudo", "-n", *argv], check=False, timeout=30)


class SystemController:
    """Restart / reboot / shutdown in the safe order of the spec.

    shutdown/reboot: stop accepting captures -> wait for the writer -> stop the
    processing loop and close the camera -> answer the phone ("可以斷電") ->
    run the command. The main loop watches stop_requested and sets
    camera_closed after closing the camera.
    """

    def __init__(self, captures, *, run_command=None, drain_timeout=None, command_delay=None):
        self.captures = captures
        self.run_command = run_command or (lambda argv: run_system_command(argv))
        self.drain_timeout = config.SYSTEM_DRAIN_TIMEOUT_SECONDS if drain_timeout is None else drain_timeout
        self.command_delay = config.SYSTEM_COMMAND_DELAY_SECONDS if command_delay is None else command_delay
        self.stop_requested = threading.Event()
        self.camera_closed = threading.Event()
        self.response_sent = threading.Event()
        self.action = None
        self._lock = threading.Lock()

    def _later(self, argv):
        def run():
            time.sleep(self.command_delay)
            self.run_command(argv)
        threading.Thread(target=run, name="system-command", daemon=True).start()

    def request(self, action):
        """Returns (status, body, after_response) for the HTTP route."""
        if action not in config.SYSTEM_COMMANDS:
            raise ValueError("action must be restart-service, reboot or shutdown")
        with self._lock:
            if self.action is not None:
                return 409, {"status": "error", "error_code": "BUSY", "message": f"已在執行 {self.action}"}
            self.action = action
        argv = config.SYSTEM_COMMANDS[action]
        if action == "restart-service":
            # systemd stops the service with SIGINT: the normal graceful path.
            return 202, {"status": "restarting", "message": "服務重新啟動中，約 15 秒後重新整理頁面"}, \
                lambda: self._later(argv)
        self.captures.set_ready(False)
        deadline = time.monotonic() + self.drain_timeout
        while not self.captures.idle() and time.monotonic() < deadline:
            time.sleep(.05)
        cancelled = self.captures.cancel_pending("SERVER_SHUTDOWN", "system is powering off")
        self.stop_requested.set()
        closed = self.camera_closed.wait(self.drain_timeout)
        body = {"status": "safe_to_power_off" if action == "shutdown" else "rebooting",
                "camera_closed": closed, "cancelled_captures": cancelled,
                "message": ("相機已關閉、資料已寫入。可以斷電（約 20 秒後系統關機完成）。" if action == "shutdown"
                            else "相機已關閉、資料已寫入，系統重新開機中。")}

        def after():
            self.response_sent.set()
            self._later(argv)
        return 200, body, after

    def camera_has_closed(self):
        """Called by the main loop after closing the camera."""
        self.camera_closed.set()
        if self.stop_requested.is_set():
            self.response_sent.wait(5)


SESSION_PATTERN = r"\d{8}_\d{6}_[0-9a-f]{8}"


def capture_sessions(root=None):
    root = Path(root or config.LOG_DIRECTORY / "manual_captures")
    sessions = []
    for directory in sorted(root.glob("*_*_*"), reverse=True):
        if not directory.is_dir() or not re.fullmatch(SESSION_PATTERN, directory.name):
            continue
        size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        try:
            with (directory / "pi_capture_log.csv").open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        except OSError:
            rows = []
        sessions.append({"session_id": directory.name, "size_mb": size / 1024 ** 2,
                         "samples": len({row["sample_id"] for row in rows}), "rows": len(rows)})
    return sessions


def session_zip(session_id, root=None, calibration_root=None):
    """Zip of one session plus the calibration versions it recorded."""
    if not re.fullmatch(SESSION_PATTERN, session_id):
        raise ValueError("invalid session id")
    directory = Path(root or config.LOG_DIRECTORY / "manual_captures") / session_id
    if not directory.is_dir():
        raise FileNotFoundError(session_id)
    calibration_root = Path(calibration_root or config.CALIBRATION_DIRECTORY)
    versions = set()
    try:
        metadata = json.loads((directory / "session_metadata.json").read_text(encoding="utf-8"))
        versions.update(value for key, value in metadata.get("calibration", {}).items()
                        if key.endswith("_version") and value)
    except (OSError, ValueError):
        pass
    try:
        with (directory / "pi_capture_log.csv").open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                versions.update(row.get(name) for name in ("geometry_version", "vial_version", "alignment_version"))
    except OSError:
        pass
    archive = tempfile.NamedTemporaryFile(prefix=f"{session_id}_", suffix=".zip", delete=False)
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and ".tmp." not in path.name:
                compress = zipfile.ZIP_STORED if path.suffix in (".jpg", ".png") else zipfile.ZIP_DEFLATED
                bundle.write(path, f"{session_id}/{path.relative_to(directory)}", compress_type=compress)
        for version in sorted(value for value in versions if value):
            kind = version.rsplit("_", 1)[-1]
            for path in sorted((calibration_root / kind).glob(f"{version}*")):
                bundle.write(path, f"{session_id}/calibration/{kind}/{path.name}", compress_type=zipfile.ZIP_DEFLATED)
    archive.close()
    return Path(archive.name)
