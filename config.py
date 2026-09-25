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

# 740 x 160 centered ROI.
ROI_WIDTH = 740
ROI_HEIGHT = 160
ROI_X1 = (FRAME_WIDTH - ROI_WIDTH) // 2
ROI_Y1 = (FRAME_HEIGHT - ROI_HEIGHT) // 2
ROI_X2 = ROI_X1 + ROI_WIDTH
ROI_Y2 = ROI_Y1 + ROI_HEIGHT

CONFIDENCE_THRESHOLD = 0.25
LENS_POSITION = 12.0

# Bubble position calibration. Coordinates in this JSON are relative to the
# same 740 x 160 ROI used by the YOLO detector.
ENABLE_BUBBLE_MEASUREMENT = True
BUBBLE_CALIBRATION_PATH = (
    PROJECT_DIRECTORY
    / "binary_stream_tuner_project"
    / "binary_captures"
    / "binary_20260926_014128_460579_tick_measurement.json"
)

# Web dashboard and WebSocket telemetry. Open http://<Pi IP>:8000 on a phone
# connected to the same network. The WebSocket endpoint is ws://<Pi IP>:8765.
ENABLE_WEBSOCKET = True
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = 8765
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8000
TELEMETRY_SEND_EVERY = 1

# Physical conversion retained from the Jetson Nano version. PIXELS_PER_DIV is
# loaded from the tick calibration JSON (currently 19.0), rather than fixed at
# the previous value of 23.
MM_PER_M_PER_DIV = 0.02
LEVEL_TOLERANCE_MM_PER_M = 0.01
MAX_MEASURABLE_SLOPE_MM_PER_M = 0.1

# 影像串流開關：True 顯示即時預覽；False 僅執行推論、輸出數值及 CSV。
ENABLE_IMAGE_STREAM = True
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
