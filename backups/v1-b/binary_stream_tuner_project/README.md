# Binary Stream Tuner 獨立專案

這是 `binary_stream_tuner.py` 及其相關模組、設定擷取與量測 log 的獨立副本。

## 執行

在 Raspberry Pi 桌面環境中：

```bash
cd /home/user/my_project/binary_stream_tuner_project
python3 live_yolo1_app/binary_stream_tuner.py
```

需要 Raspberry Pi 相機、Picamera2/libcamera、OpenCV、NumPy 與 Tkinter。

執行後會顯示三個預覽視窗：

- `Original Preview`：去畸變後的彩色畫面或中央 ROI。
- `Binary Preview`：沒有標記與文字的純二值化畫面。
- `Tick Measurement`：彩色刻度標記與量測數值。

控制面板已縮小為 `500 x 570`，滑桿軌道為 390 px。另有一個
`Tick Dashboard` 即時儀表板，最多每秒更新 5 次，依畫面 X 座標從左到右顯示：

- 刻度順序、左右側與 tick ID。
- X 座標與二值刻度粗細 `WIDTH`。
- 與左邊上一條線的中心間隔 `GAP←`。
- 分析帶內的單欄最大黑色高度 `LENGTH`。
- 黑色像素總數 `AREA`。
- `VALID`、`REFERENCE` 或無效原因。

關閉儀表板後，可按控制面板的「刻度儀表板」再次顯示。

影像處理順序為：

```text
960 × 540 相機影像 → 全畫面去畸變 → 選擇 ROI／全畫面
→ 自適應二值化 → 刻度辨識與量測 → 預覽／CSV／儲存擷取
```

校正參數採用
`/home/user/my_project/live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result`
驗證報告中的 K、D。該目錄是驗證結果；實際使用報告引用的
`snapshots/20260903_004/result/clean/camera_calibration_clean.npz`。
路徑設定在 `live_yolo1_app/config.py` 的 `CAMERA_CALIBRATION_NPZ`，
`UNDISTORT_ALPHA = 1.0` 保留完整 960 × 540 輸出與邊緣黑區；中央 ROI
仍為 740 × 160。映射表於啟動時建立一次，校正檔無效或尺寸不符時停止啟動。

刻度 X、零點、GAP 與 PITCH 都由去畸變影像重新量測。每次量測 run 的
`image_geometry.json` 記錄校正來源、K、D、新 K 與 ROI 範圍；新擷取的設定
JSON 和刻度量測 JSON 也含 `image_geometry`，記錄去畸變座標與 ROI 原點。
既有擷取檔與歷史 CSV 保留原本的座標與數值。

## 目錄

```text
binary_stream_tuner_project/
├── live_yolo1_app/
│   ├── binary_stream_tuner.py
│   ├── camera.py
│   ├── camera_undistortion.py
│   ├── config.py
│   └── measurement_logger.py
├── tick_scale_calibration.py
├── analyze_tick_measurements.py
├── binary_captures/
└── logs/
    └── tick_measurement_runs/
```

`binary_captures/` 內含程式目前指定的初始參數 JSON。新擷取與新 log 都會寫入這個獨立專案，不會寫回上層原專案的資料夾。

## 離線分析

```bash
python3 analyze_tick_measurements.py logs/tick_measurement_runs
```

## 目前刻度設定

- LOGO 排除區固定為 ROI 座標 `304..441`。
- LOGO margin 為 `0`。
- 內部仍以 `LENGTH >= 60 px` 判定長刻度，但不顯示綠線；一般有效刻度統一為藍色。
- 固定 LOGO 左右最近的有效長刻度為黃色基準刻度。
- `tick_positions.csv` 以 `is_long` 欄位保留長刻度判定結果。
- CSV 預設約每秒記錄一次量測。
