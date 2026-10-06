"""The single owner of the camera: one open for the whole service lifetime.

Mode switches never reopen it, so every mode shares the same focus,
undistortion and ROI. capture() is synchronous on the processing loop, which
keeps "only frames captured after a trigger" exact; the newest frame is also
kept for readers such as a preview stream.
"""

import threading
import time
from dataclasses import dataclass

from camera import capture_frames, close_camera, create_camera, describe_image_geometry


@dataclass(frozen=True)
class Frame:
    frame_id: int
    raw: object        # sensor frame before undistortion
    full: object       # undistorted full frame
    roi: object        # bubble ROI cropped from the undistorted frame
    started_epoch_ms: float
    captured_epoch_ms: float
    capture_ms: float


class CameraService:
    def __init__(self, camera_factory=create_camera):
        self._factory = camera_factory
        self._lock = threading.Lock()
        self._latest = None
        self.camera = None
        self.open_count = 0
        self.frame_id = 0

    def open(self):
        if self.camera is None:
            self.camera = self._factory()
            self.open_count += 1
        return self.camera

    @property
    def undistorter(self):
        return self.camera.frame_undistorter

    def image_geometry(self):
        return describe_image_geometry(self.camera)

    def capture(self):
        started_epoch_ms = time.time() * 1000.0
        start = time.perf_counter()
        raw, full, roi = capture_frames(self.camera)
        capture_ms = (time.perf_counter() - start) * 1000.0
        self.frame_id += 1
        frame = Frame(self.frame_id, raw, full, roi, started_epoch_ms, time.time() * 1000.0, capture_ms)
        with self._lock:
            self._latest = frame
        return frame

    def latest(self):
        with self._lock:
            return self._latest

    def close(self):
        if self.camera is not None:
            camera, self.camera = self.camera, None
            close_camera(camera)
