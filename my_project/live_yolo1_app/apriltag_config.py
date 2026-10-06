"""Independent settings for apriltag_measurement.py (does not change YOLO)."""

from pathlib import Path


PROJECT_DIRECTORY = Path(__file__).resolve().parent.parent
DISPLAY_WIDTH = 960
DISPLAY_HEIGHT = 540
RAW_WIDTH = 2328
RAW_HEIGHT = 1748
SIDEBAR_WIDTH = 360
WINDOW_NAME = "AprilTag Measurement"
RAW_WINDOW_NAME = "Raw Camera Input (Original)"
SHOW_RAW_WINDOW = True

TAG_FAMILY = "tag36h11"
TAG_SIZE_METER = 0.007  # Exactly 7 mm, between opposite black/white boundaries.
VCM_FOCUS_ABSOLUTE = 3711
VCM_SETTLE_SECONDS = 0.25

# The supplied directory contains validation results. Its report says K/D
# came from the following clean calibration NPZ (960 x 540).
CALIBRATION_RESULT_DIRECTORY = (
    PROJECT_DIRECTORY / "live_yolo1_app_judy_a/camera_calibration"
    / "snapshots/20260904_001/result"
)
CALIBRATION_NPZ = (
    CALIBRATION_RESULT_DIRECTORY.parent.parent
    / "20260903_004/result/clean/camera_calibration_clean.npz"
)
UNDISTORT_ALPHA = 1.0  # Keep the full field of view; never crop to valid ROI.

# Matches the Jetson script's R @ Rz(180 deg). Set to 0 for upright tags.
POSE_ROTATION_CORRECTION_DEG = 180.0
# Override only IDs whose physical mounting needs a different correction.
# Empty means use the common correction above; no physical layout is inferred.
TAG_ROTATION_CORRECTIONS_DEG = {}
ROLL_LEVEL_TOLERANCE_DEG = 2.0  # Sidebar color threshold from the Jetson version.

PRINT_INTERVAL_SECONDS = 1.0
