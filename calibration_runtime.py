"""Live calibration: load, check against the open camera, apply, roll back.

Startup, ③ apply and version activation all go through reload(), so the
measurement processor always gets a geometry that matches the camera's
undistortion, plus the vial calibration and the versions to log.
"""

import threading
from datetime import datetime

import cv2

import config
from bubble_measurement import check_undistorted_geometry
from calibration_store import KINDS, TIMEZONE, calibration_info


class CalibrationRuntime:
    def __init__(self, store, measure, undistorter=lambda: None):
        self.store = store
        self.measure = measure
        self.undistorter = undistorter
        self.error = ""
        self._lock = threading.Lock()

    def reload(self):
        """Load the active geometry + vial into the measurement processor."""
        with self._lock:
            geometry = vial = None
            info = {}
            self.error = ""
            if config.ENABLE_BUBBLE_MEASUREMENT:
                try:
                    geometry, vial = self.store.load_geometry(), self.store.load_vial()
                    undistorter = self.undistorter()
                    if undistorter is not None:
                        check_undistorted_geometry(geometry.image_geometry, undistorter,
                                                   (config.ROI_X1, config.ROI_Y1))
                    info = calibration_info(self.store, geometry, vial)
                except (OSError, KeyError, TypeError, ValueError) as error:
                    geometry = vial = None
                    info = {}
                    self.error = str(error)
            self.measure.set_calibration(geometry, vial, info)
            return self.status()

    def status(self):
        geometry = self.measure.geometry
        status = "unavailable" if geometry is None else (
            "pending" if self.measure.calibration.get("geometry_pending_confirmation") else "ok")
        return {"status": status, "error": self.error, **self.store.summary()}

    def history(self, kind):
        if kind not in KINDS:
            raise ValueError(f"unknown calibration kind: {kind}")
        return {"kind": kind, "active": self.store.active_state(kind), "versions": self.store.versions(kind)}

    def activate(self, kind, version):
        self.store.activate(kind, version)
        return self.reload()

    def apply_geometry(self, record, source_roi=None):
        """Save a ③ result as a new active geometry version (clears pending)."""
        now = datetime.now(TIMEZONE)
        record = dict(record, version=self.store.new_version("geometry", now), created_at_iso=now.isoformat(timespec="seconds"))
        if source_roi is not None:
            image_path = self.store.root / "geometry" / f"{record['version']}_roi.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            if cv2.imwrite(str(image_path), source_roi):
                record["source_image"] = str(image_path.relative_to(self.store.root.parent))
        record["previous_version"] = self.store.active_state("geometry")["version"]
        self.store.save("geometry", record, pending_confirmation=False)
        return record["version"], self.reload()
