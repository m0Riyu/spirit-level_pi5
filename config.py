"""Application settings and paths."""

import os
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
# ② camera alignment (AprilTags on the undistorted full frame).
ALIGN_AVERAGE_FRAMES = 10             # moving average; also frames per teaching reading
ALIGN_TARGET_DEG = {"pitch_deg": 0.0, "yaw_deg": 0.0}  # camera square to the AprilTag plane
ALIGN_TOLERANCE_DEG = 0.10            # pitch and yaw vs the target
ALIGN_HOLD_SECONDS = 3.0              # in range this long before "完成對位"
ALIGN_TEACH_TURN = 0.25               # clockwise turn used while teaching each screw
ALIGN_PUBLISH_INTERVAL_SECONDS = 0.1  # 10 Hz so the page reacts within 0.5 s

# Tick center (median of left/right pair midpoints) vs the ROI center. With
# pitch/yaw zeroed by ②, this is decided by where the vial sits; beyond the
# tolerance ① asks to check the vial placement (③ handles any position).
TICK_CENTER_TARGET_PX = ROI_WIDTH / 2
TICK_CENTER_TOLERANCE_PX = 10.0
TICK_MONITOR_INTERVAL_SECONDS = 1.0   # ① checks the tick center this often
TICK_GEOMETRY_DRIFT_WARN_PX = 1.5     # ① hint: ticks moved vs the active geometry -> redo ③
POSE_MONITOR_INTERVAL_SECONDS = 1.0   # ① measures the AprilTag pose this often (~22 ms)
POSE_MONITOR_WINDOW = 10              # ① averages this many pose readings (10 s) before warning

PREVIEW_MAX_FPS = 5
PREVIEW_JPEG_QUALITY = 60

# ⚙ system control. The PIN comes from /etc/levelsvc/env (EnvironmentFile of
# the systemd unit), never from git. Commands run through sudoers rules that
# allow only these three; LEVELSVC_SYSTEM_DRY_RUN=1 logs them instead.
PIN_ENVIRONMENT_VARIABLE = "LEVELSVC_PIN"
PIN_MAX_FAILURES = 5
PIN_LOCKOUT_SECONDS = 60
SYSTEM_COMMANDS = {
    "restart-service": ["/usr/bin/systemctl", "restart", "levelsvc"],
    "reboot": ["/usr/bin/systemctl", "reboot"],
    "shutdown": ["/usr/bin/systemctl", "poweroff"],
}
SYSTEM_DRAIN_TIMEOUT_SECONDS = 10     # wait for captures to finish writing
SYSTEM_COMMAND_DELAY_SECONDS = 1.0    # let the HTTP response reach the phone first
SYSTEM_DRY_RUN = os.environ.get("LEVELSVC_SYSTEM_DRY_RUN") == "1"

# ⚡ Battery module power monitor (Mcuzone 3003 21700 5V5A PD RP5, INA219 × 3).
# Developed standalone in /home/user/power_test; moved here unchanged.
# 實測（2026-10-08，USB 測試儀與 Pi 5 PMIC 交叉比對）：0x41、0x44 的電流讀值都偏低
# （約 2.5–2.9 倍與約 1.7 倍），電流只作趨勢參考；安全關機只依電池電壓，不受影響。
POWER_I2C_BUS = 1                      # GPIO2 SDA / GPIO3 SCL
POWER_SAMPLE_INTERVAL_SECONDS = 1.0

# 依廠商腳本 INA219_10MR1126.py：0x40 充電、0x44 電池放電、0x41 5V 輸出（i2cdetect 已確認存在）。
POWER_CHANNEL_ADDRESSES = {"charge": 0x40, "discharge": 0x44, "output_5v": 0x41}
# 分流電阻：由廠商校正值反推 0.04096 / (26868 × 0.1524 mA) = 0.0100 Ω（檔名 10MR）。
# 腳本註解另有 5 mΩ 的數值，待電表對照確認。
POWER_SHUNT_OHMS = {"charge": 0.010, "discharge": 0.010, "output_5v": 0.010}
# 電池電壓取放電通道的 bus 電壓（分流電阻負載側 = 負載下電池電壓）。
POWER_BATTERY_VOLTAGE_CHANNEL = "discharge"
# 晶片維持出廠預設（config 0x399F：±320 mV）；本模組只讀不寫，不使用 calibration/current 暫存器。
POWER_SHUNT_PGA_MV = 320

# 暫定：充電電流大於此值視為充電中（無實測依據）。
POWER_CHARGING_CURRENT_A = 0.05

# 暫定門檻（負載下電池電壓，無實測依據，不可當作已驗證的數字）。
POWER_WARN_VOLTAGE_V = 3.3
POWER_SHUTDOWN_VOLTAGE_V = 3.1
POWER_HYSTERESIS_V = 0.05              # 暫定
POWER_DEBOUNCE_SECONDS = 10.0          # 暫定：連續低於門檻達此秒數才觸發
POWER_THRESHOLDS_PROVISIONAL = True

# 剩餘電量估計：曲線為 ((電壓 V, 百分比), ...)，實測前為 None → 結果標示「未校準」。
POWER_CAPACITY_MAH = 10000             # 2 × 21700 並聯，標稱值
POWER_DISCHARGE_CURVE = None
POWER_DISCHARGE_CURVE_CALIBRATED = False
POWER_SOC_MAX_GAP_SECONDS = 30.0       # 讀值中斷超過此秒數就不積分

POWER_THROTTLED_COMMAND = ("vcgencmd", "get_throttled")
POWER_THROTTLED_TIMEOUT_SECONDS = 2.0
POWER_MONITOR_ENABLED = True
POWER_SHUTDOWN_COUNTDOWN_SECONDS = 30.0  # 暫定：觸發關機門檻後，通知倒數這麼久才安全關機
POWER_PUBLISH_INTERVAL_SECONDS = 2.0     # 網頁電源狀態更新頻率

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
