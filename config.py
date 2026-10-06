"""Application settings and paths."""

from pathlib import Path


APP_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = APP_DIRECTORY.parent

MODEL_PATH = PROJECT_DIRECTORY / "best_128x608_ncnn_model"
MODEL_IMAGE_SIZE = (128, 608)

FRAME_WIDTH = 960
FRAME_HEIGHT = 540

RAW_WIDTH = 2328
RAW_HEIGHT = 1748

# 20260904_001 independently validates the cleaned 20260903_004 calibration.
# Its result directory contains validation reports, rather than another NPZ.
CAMERA_CALIBRATION_RESULT_DIRECTORY = (
    PROJECT_DIRECTORY
    / "live_yolo1_app_judy_a"
    / "camera_calibration"
    / "snapshots"
    / "20260904_001"
    / "result"
)
CAMERA_CALIBRATION_NPZ = (
    CAMERA_CALIBRATION_RESULT_DIRECTORY.parent.parent
    / "20260903_004"
    / "result"
    / "clean"
    / "camera_calibration_clean.npz"
)
UNDISTORT_ALPHA = 1.0

# 740 x 160 centered ROI.
ROI_WIDTH = 740
ROI_HEIGHT = 160
ROI_X1 = (FRAME_WIDTH - ROI_WIDTH) // 2
ROI_Y1 = (FRAME_HEIGHT - ROI_HEIGHT) // 2
ROI_X2 = ROI_X1 + ROI_WIDTH
ROI_Y2 = ROI_Y1 + ROI_HEIGHT

CONFIDENCE_THRESHOLD = 0.25
# AK7375 V4L2 focus_absolute (vcm_focus_absolute_test.py DEFAULT_INITIAL).
VCM_FOCUS_ABSOLUTE = 3711
VCM_FOCUS_SETTLE_SECONDS = 0.25
# Held by whichever process has the camera open (service or a debug tool).
CAMERA_LOCK_PATH = Path("/tmp/levelsvc-camera.lock")

# Two-layer calibration, versioned under calibration/<kind>/ (active.json):
#   geometry: rectified ROI pixel -> divisions (redo whenever the camera moves)
#   vial:     divisions -> mm/m, gain and zero offset (redo only for a new vial)
# migrate_calibration.py creates the first versions from the legacy tick JSON.
ENABLE_BUBBLE_MEASUREMENT = True
CALIBRATION_DIRECTORY = PROJECT_DIRECTORY / "calibration"

# ③ tick check: detection in the rectified ROI, fit, and acceptance checks.
TICK_BAND_Y1, TICK_BAND_Y2 = 55, 105  # ROI rows crossed by every tick
TICK_KERNEL_WIDTH_PX = 9              # black-hat kernel; wider than a tick line
TICK_PEAK_FRACTION = 0.25             # peak threshold between median and top
TICK_LOGO_HALF_WIDTH_PX = 65          # RSK logo gap around the scale center
TICK_MATCH_TOLERANCE = 0.25           # fraction of pitch when following ticks
TICK_EXPECTED_PER_SIDE = 13
TICK_NOMINAL_PITCH_PX = 18.0          # starting pitch when no geometry exists
TICK_MIN_FRAME_FRACTION = 0.5         # a tick must be found in half the frames
TICK_MEASURE_FRAMES = 20
GEOMETRY_POLYNOMIAL_DEGREE = 2        # 1-3
TICK_MAX_RESIDUAL_RMS_PX = 0.5
TICK_MAX_PITCH_CHANGE = 0.03          # vs the active version; above needs confirmation
PREVIEW_MAX_FPS = 5
PREVIEW_JPEG_QUALITY = 60

# Web dashboard and WebSocket telemetry. Open http://<Pi IP>:8100 on a phone
# connected to the same network. The WebSocket endpoint is ws://<Pi IP>:8865.
ENABLE_WEBSOCKET = True
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = 8865
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8100
TELEMETRY_SEND_EVERY = 2  # About 5 Hz at 10 inference FPS; YOLO still runs every frame.

# Thresholds are in mm/m after the vial calibration (kept unchanged when the
# vial gain changed from nominal 0.02 to the fitted value).
LEVEL_TOLERANCE_MM_PER_M = 0.01
MAX_MEASURABLE_SLOPE_MM_PER_M = 0.12

# This legacy name controls ONLY the Pi's local cv2.imshow() preview.
# WebSocket always carries numeric JSON, never images/JPEG/base64.
ENABLE_IMAGE_STREAM = False
ENABLE_CONTINUOUS_CSV = False  # Legacy .csv.part / Y/N prompt only when True.

# Engineering starting values, not scientifically validated thresholds.
STABILITY_WINDOW_SIZE = 50
STABILITY_MIN_VALID_RATIO = 0.90
STABILITY_MAX_STD_MM_PER_M = 0.002
STABILITY_MAX_RANGE_MM_PER_M = 0.006
STABILITY_HOLD_SECONDS = 1.5
REQUIRE_STABLE_FOR_CAPTURE = False

JPEG_QUALITY = 95
# One phone trigger records this many consecutive frames (one CSV row each,
# shared burst_id) plus a summary row. A request may ask for 1..MAX frames.
CAPTURE_BURST_FRAMES = 15
CAPTURE_BURST_MAX_FRAMES = 30
# Lossless full frame BEFORE undistortion, once per trigger, for reprocessing.
SAVE_RAW_FRAME_PNG = True
RAW_PNG_COMPRESSION = 1  # 0-9; 1 keeps encoding near 40 ms on the Pi 5.
# Remaining-capture estimate: bytes per trigger before this session has saved
# one (15 frames x two ROI JPEGs + raw PNG), and free space kept in reserve.
CAPTURE_BYTES_ESTIMATE = 2 * 1024 ** 2
CAPTURE_DISK_RESERVE_MB = 1024
CAPTURE_REQUEST_QUEUE_SIZE = 4
CAPTURE_WRITER_QUEUE_SIZE = 4
CAPTURE_MAX_ACTIVE_REQUESTS = 4
PRINT_EVERY = 10
FLUSH_EVERY = 50

# 正式驗證預設逐幀、append-only；調參時可改成 "interval" / "circular"。
MEASUREMENT_RECORD_MODE = "every_frame"  # "every_frame" or "interval"
MEASUREMENT_LOGGER_MODE = "append-only"  # "append-only" or "circular"
MEASUREMENT_LOG_INTERVAL_SECONDS = 1.0
MEASUREMENT_PRINT_INTERVAL_SECONDS = 1.0
MEASUREMENT_FLUSH_EVERY = 10
MEASUREMENT_CIRCULAR_MAX_FRAMES = 10000
ENABLE_TUNER_DISPLAY = True

# Optional labels written to session_metadata.json for cross-run comparison.
EXPERIMENT_LABEL = ""
SETUP_LABEL = ""
LIGHTING_LABEL = ""

LOG_DIRECTORY = PROJECT_DIRECTORY / "logs"
MEASUREMENT_RUN_DIRECTORY = LOG_DIRECTORY / "tick_measurement_runs"
WINDOW_TITLE = "Bubble YOLO 160x736"
