"""Versioned calibration files under calibration/<kind>/, one active per kind.

Every update writes a new <timestamp>_<kind>.json and never edits old files.
active.json names the version in use; pointing it back is a rollback.
"""

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from geometry_calibration import GeometryCalibration

KINDS = ("geometry", "vial", "alignment")
TIMEZONE = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class VialCalibration:
    """Physical vial calibration: slope = (div - zero_offset_div) * mm_per_m_per_div."""
    version: str
    mm_per_m_per_div: float
    zero_offset_div: float
    created_at_iso: str = ""
    source_path: str = ""

    def __post_init__(self):
        if not (math.isfinite(self.mm_per_m_per_div) and self.mm_per_m_per_div > 0):
            raise ValueError("mm_per_m_per_div must be positive")
        if not math.isfinite(self.zero_offset_div):
            raise ValueError("zero_offset_div must be finite")

    @classmethod
    def from_dict(cls, data, source_path=""):
        return cls(str(data["version"]), float(data["mm_per_m_per_div"]), float(data["zero_offset_div"]),
                   str(data.get("created_at_iso", "")), str(source_path))

    def slope_mm_per_m(self, offset_div):
        return (float(offset_div) - self.zero_offset_div) * self.mm_per_m_per_div


# Nameplate value with no zero correction; reproduces the pre-V2 conversion.
NOMINAL_VIAL = VialCalibration("nominal", 0.02, 0.0)


def new_version(kind, now=None):
    now = now or datetime.now(TIMEZONE)
    return f"{now.astimezone(TIMEZONE).strftime('%Y%m%dT%H%M%S')}_{kind}"


def _write_json(path, data, *, exclusive):
    if exclusive and path.exists():
        raise FileExistsError(f"calibration version already exists: {path.name}")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class CalibrationStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else config.CALIBRATION_DIRECTORY

    @staticmethod
    def _check_kind(kind):
        if kind not in KINDS:
            raise ValueError(f"unknown calibration kind: {kind}")

    def path(self, kind, version):
        self._check_kind(kind)
        if not version.endswith(f"_{kind}") or "/" in version or version.startswith("."):
            raise ValueError(f"invalid {kind} version name: {version}")
        return self.root / kind / f"{version}.json"

    def new_version(self, kind, now=None):
        """Timestamp version name, moved forward a second if already taken."""
        now = (now or datetime.now(TIMEZONE)).replace(microsecond=0)
        while self.path(kind, new_version(kind, now)).exists():
            now += timedelta(seconds=1)
        return new_version(kind, now)

    def active_state(self, kind):
        """{"version": str | None, "pending_confirmation": bool}."""
        self._check_kind(kind)
        try:
            data = json.loads((self.root / kind / "active.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": None, "pending_confirmation": False}
        return {"version": data.get("version"), "pending_confirmation": bool(data.get("pending_confirmation", False))}

    def load(self, kind, version=None):
        version = version or self.active_state(kind)["version"]
        if version is None:
            raise FileNotFoundError(f"no active {kind} calibration in {self.root / kind}")
        path = self.path(kind, version)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != version:
            raise ValueError(f"{path.name} records version {data.get('version')!r}")
        return data, path

    def versions(self, kind):
        self._check_kind(kind)
        active = self.active_state(kind)["version"]
        result = []
        for path in sorted((self.root / kind).glob(f"*_{kind}.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            result.append({"version": data.get("version", path.stem), "created_at_iso": data.get("created_at_iso", ""),
                           "previous_version": data.get("previous_version"), "active": path.stem == active})
        return result

    def save(self, kind, data, *, activate=True, pending_confirmation=False):
        """Write a NEW version file (never overwrite) and optionally activate it."""
        version = data["version"]
        path = self.path(kind, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(data)
        record.setdefault("previous_version", self.active_state(kind)["version"])
        _write_json(path, record, exclusive=True)
        if activate:
            self.activate(kind, version, pending_confirmation=pending_confirmation)
        return path

    def activate(self, kind, version, *, pending_confirmation=False):
        if not self.path(kind, version).is_file():
            raise FileNotFoundError(f"{kind} version not found: {version}")
        state = {"version": version, "pending_confirmation": bool(pending_confirmation),
                 "activated_at_iso": datetime.now(TIMEZONE).isoformat(timespec="seconds")}
        _write_json(self.root / kind / "active.json", state, exclusive=False)

    def load_geometry(self, version=None):
        data, path = self.load("geometry", version)
        return GeometryCalibration.from_dict(data, path)

    def load_vial(self, version=None):
        data, path = self.load("vial", version)
        return VialCalibration.from_dict(data, path)

    def summary(self):
        """Active versions for LOG metadata, telemetry and /api/state."""
        return {kind: self.active_state(kind) for kind in KINDS}


def calibration_info(store, geometry, vial):
    """Versions and vial constants recorded in telemetry and every LOG row."""
    state = store.summary()
    return {
        "geometry_version": geometry.version,
        "geometry_pending_confirmation": state["geometry"]["pending_confirmation"],
        "vial_version": vial.version,
        "alignment_version": state["alignment"]["version"] or "",
        "mm_per_m_per_div": vial.mm_per_m_per_div,
        "zero_offset_div": vial.zero_offset_div,
    }
