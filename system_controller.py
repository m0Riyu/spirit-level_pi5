"""Read-only system status for /api/state (service control arrives in P5)."""

import os
import shutil
import time
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
