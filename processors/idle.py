"""Shared base for modes that do not run YOLO (② alignment, ③ ticks)."""

import time


class IdleProcessor:
    """Publishes a heartbeat with frame timing; subclasses add the real work."""
    mode = ""
    status = "not_implemented"
    message = ""

    def __init__(self, *, publish=None, publish_interval_seconds=0.5):
        self.publish = publish
        self.publish_interval_seconds = publish_interval_seconds
        self._next_publish = 0.0

    def enter(self):
        self._next_publish = 0.0

    def leave(self):
        pass

    def before_capture(self):
        return None

    def process(self, frame, context):
        now = time.monotonic()
        if self.publish is not None and now >= self._next_publish:
            self._next_publish = now + self.publish_interval_seconds
            self.publish({"type": self.mode, "schema_version": 1, "mode": self.mode, "frame_id": frame.frame_id,
                          "sent_at_epoch_ms": time.time() * 1000.0, "status": self.status, "message": self.message,
                          "capture_ms": frame.capture_ms})
        return False
