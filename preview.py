"""Low-rate JPEG preview shared by the ② and ③ processors and /api/preview.mjpg.

Frames are encoded only while a browser is watching, at most PREVIEW_MAX_FPS,
so the preview costs nothing in ① measurement mode.
"""

import threading
import time

import cv2

import config


class PreviewBuffer:
    def __init__(self, max_fps=None, quality=None):
        self.max_fps = max_fps or config.PREVIEW_MAX_FPS
        self.quality = quality or config.PREVIEW_JPEG_QUALITY
        self._condition = threading.Condition()
        self._jpeg = None
        self._sequence = 0
        self._viewers = 0
        self._next_encode = 0.0
        self.source = ""  # mode that currently feeds the preview, "" if none

    @property
    def wanted(self):
        with self._condition:
            return self._viewers > 0 and bool(self.source) and time.monotonic() >= self._next_encode

    def set_source(self, mode):
        with self._condition:
            self.source = mode
            if not mode:
                self._jpeg = None
            self._condition.notify_all()

    def publish(self, image):
        """Encode and hand out one preview image (call only when wanted)."""
        encoded, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not encoded:
            return
        with self._condition:
            self._next_encode = time.monotonic() + 1 / self.max_fps
            self._jpeg = data.tobytes()
            self._sequence += 1
            self._condition.notify_all()

    def frames(self, timeout=2.0):
        """Yield new JPEG frames for one viewer until the source mode ends."""
        with self._condition:
            self._viewers += 1
            seen = self._sequence
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._sequence != seen or not self.source, timeout)
                    if not self.source:
                        return
                    if self._sequence == seen:
                        continue
                    seen, jpeg = self._sequence, self._jpeg
                yield jpeg
        finally:
            with self._condition:
                self._viewers -= 1
