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

# Tuner JSON records its image coordinate system. Rectified measurements are
# used directly; legacy JSON without this metadata uses original ROI pixels.
ENABLE_BUBBLE_MEASUREMENT = True
BUBBLE_CALIBRATION_PATH = (
    PROJECT_DIRECTORY
    / "binary_stream_tuner_project"
    / "binary_captures"
    / "binary_20261002_072923_517806_tick_measurement.json"
)

# Web dashboard and WebSocket telemetry. Open http://<Pi IP>:8100 on a phone
# connected to the same network. The WebSocket endpoint is ws://<Pi IP>:8865.
ENABLE_WEBSOCKET = True
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = 8865
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8100
TELEMETRY_SEND_EVERY = 2  # About 5 Hz at 10 inference FPS; YOLO still runs every frame.

# Physical conversion uses calibrated divisions after coordinate conversion.
# The current rectified tick JSON records 18 pixels per division.
MM_PER_M_PER_DIV = 0.02
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
