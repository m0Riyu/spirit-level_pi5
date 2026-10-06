"""Per-mode frame processors driven by ModeManager.

Each processor implements:
    enter() / leave()          mode switched to / away from this processor
    before_capture() -> ctx    runs before the frame is captured
    process(frame, ctx) -> bool  handle one camera_service.Frame; True stops
    close()                    optional; service shutdown
"""
