"""One processor at a time; switching changes who gets the next frame.

Switch requests may come from HTTP threads. They take effect on the
processing loop between two frames (leave old -> enter new), so a processor
never sees a half-switched state and the camera stays open throughout.
The last request wins when several phones switch at once.
"""

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

MODES = ("measure", "align", "ticks")
MODE_LABELS = {"measure": "量測", "align": "相機對位", "ticks": "刻度檢查"}
TIMEZONE = ZoneInfo("Asia/Taipei")


class ModeManager:
    def __init__(self, processors, initial="measure", on_change=None):
        if set(processors) != set(MODES) or initial not in MODES:
            raise ValueError(f"processors must be exactly {MODES}")
        self.processors = processors
        self._lock = threading.Lock()
        self._on_change = on_change
        self._mode = initial
        self._entered = False
        self._pending = None  # (mode, client_id, requested_monotonic)
        self._sequence = 0
        self._changed_by = ""
        self._changed_at_iso = ""
        self._last_switch_ms = None

    @property
    def mode(self):
        with self._lock:
            return self._mode

    def request(self, mode, client_id=""):
        """Ask for a mode from any thread; returns the state to report."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        client_id = str(client_id or "")[:64]
        with self._lock:
            self._pending = (mode, client_id, time.monotonic())
        return self.state()

    def state(self):
        with self._lock:
            pending = self._pending[0] if self._pending else None
            return {"mode": self._mode, "mode_label": MODE_LABELS[self._mode],
                    "pending_mode": pending, "mode_sequence": self._sequence,
                    "changed_by": self._changed_by, "changed_at_iso": self._changed_at_iso,
                    "last_switch_ms": self._last_switch_ms}

    def _apply_pending(self):
        with self._lock:
            pending, self._pending = self._pending, None
        if not self._entered:
            self.processors[self._mode].enter()
            self._entered = True
        if pending is None:
            return
        mode, client_id, requested = pending
        with self._lock:
            previous = self._mode
        if mode != previous:
            self.processors[previous].leave()
            self.processors[mode].enter()
        with self._lock:
            self._mode = mode
            self._sequence += 1
            self._changed_by = client_id
            self._changed_at_iso = datetime.now(TIMEZONE).isoformat(timespec="milliseconds")
            self._last_switch_ms = (time.monotonic() - requested) * 1000.0
        if self._on_change is not None:
            self._on_change(self.state())

    def step(self, camera):
        """Run one frame through the active processor. True means stop."""
        self._apply_pending()
        processor = self.processors[self._mode]
        context = processor.before_capture()
        frame = camera.capture()
        return bool(processor.process(frame, context))

    def close(self):
        """Service shutdown, not a mode switch: CaptureManager.close() reports
        unfinished captures as SERVER_SHUTDOWN, so leave() is not called."""
        for processor in self.processors.values():
            close = getattr(processor, "close", None)
            if close is not None:
                close()
