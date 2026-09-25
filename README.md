# 相機刻度量測系統

## YOLO 氣泡位置量測

執行：

```bash
cd /home/user/my_project/live_yolo1_app
python3 main.py
```

程式使用刻度校正 JSON 中的：

- `reference_midpoint_x = 373.0 px` 作為刻度零點。
- `global_pitch.pitch_px = 19.0 px/div` 作為每格像素數。

YOLO 偵測到氣泡後，位置計算為：

```text
bubble_offset_px  = bubble_center_x_roi - scale_center_x_roi
bubble_offset_div = bubble_offset_px / pitch_px_per_div
```

結果為正表示氣泡在刻度中心右側，負值表示在左側。終端、預覽畫面及
CSV 都會輸出偏移像素與偏移格數。校正檔不存在或 ROI 尺寸不相符時，
程式會保留原本的相機與 YOLO 功能，並將量測標記為不可用。

## 先看這裡

進行即時刻度量測時，主要執行：

```bash
cd /home/user/my_project
python3 live_yolo1_app/binary_stream_tuner.py
```

程式會完成以下流程：

```text
擷取相機畫面
→ 裁切中央 ROI
→ 二值化
→ 找出左右刻度
→ 計算刻度間距與中心
→ 每幀寫入 CSV
→ 實驗結束後進行離線穩定性分析
```

一般實驗不需要單獨執行 `tick_scale_calibration.py` 或 `measurement_logger.py`，它們會由 `binary_stream_tuner.py` 自動呼叫。

---

## 一、開始實驗前要做什麼

### 1. 填寫實驗條件

開啟 `live_yolo1_app/config.py`，設定本次實驗資訊：

```python
EXPERIMENT_LABEL = "刻度重複性測試"
SETUP_LABEL = "第一次安裝"
LIGHTING_LABEL = "室內LED"
```

建議每次重新安裝設備、更換照明或改變實驗條件時，使用不同名稱。

### 2. 確認記錄模式

正式收集資料建議使用：

```python
MEASUREMENT_RECORD_MODE = "every_frame"
MEASUREMENT_LOGGER_MODE = "append-only"
```

代表：

- 每一幀都記錄。
- 每次啟動建立新的 run。
- 不會覆寫之前的實驗資料。

調整參數、不需要保存所有資料時，可以改成：

```python
MEASUREMENT_RECORD_MODE = "interval"
MEASUREMENT_LOGGER_MODE = "circular"
MEASUREMENT_LOG_INTERVAL_SECONDS = 1.0
```

終端顯示頻率可另外調整，不影響正式 CSV 記錄：

```python
MEASUREMENT_PRINT_INTERVAL_SECONDS = 1.0
```

---

## 二、啟動即時刻度量測

```bash
cd /home/user/my_project
python3 live_yolo1_app/binary_stream_tuner.py
```

程式啟動時會：

1. 載入 `binary_captures/binary_20260806_172823_503501.json` 的二值化與相機設定。
2. 產生唯一的 `run_id`。
3. 建立本次實驗資料夾與 `session_metadata.json`。
4. 啟動相機、二值化、刻度辨識與 CSV 記錄。

結束方式：

- 在預覽視窗按 `q`。
- 按控制面板的「結束」。
- 在終端按 `Ctrl+C`。

---

## 三、畫面要怎麼看

### Original Preview

顯示相機原始 ROI，用來確認：

- 刻度是否清楚。
- 曝光是否過暗或過亮。
- 鏡頭是否對焦。
- ROI 是否包含完整刻度與中央 LOGO。

### Tick Measurement

顯示二值化與刻度分析結果：

- 藍色：短刻度。
- 綠色：長刻度。
- 黃色：選中的參考刻度。
- 紫色：左右刻度計算出的中心。
- `PASS`：本幀辨識正常。
- `WARN`：可以量測，但存在一致性警告。
- `FAIL`：刻度不足、參考刻度缺失或分析發生錯誤。

即使某幀為 `FAIL`，程式仍會把該幀與失敗原因寫入 CSV。

### 控制面板

控制面板分為：

- 二值化設定：C、Block Size、Morph Kernel。
- 手動曝光：曝光時間、類比增益、亮度、對比。
- 白平衡／焦距：白平衡模式與鏡頭位置。

按「儲存目前畫面」或鍵盤 `s`，會另外保存：

- 二值化圖片。
- 當下參數 JSON。
- 刻度量測 JSON。
- 彩色刻度標註圖。

---

## 四、程式會輸出什麼

每次啟動都會建立獨立資料夾：

```text
logs/tick_measurement_runs/<run_id>/
├── session_metadata.json
├── frame_summary.csv
├── tick_positions.csv
├── tick_gaps.csv
└── pair_centers.csv
```

終端會印出實際路徑：

```text
量測資料夾：/home/user/my_project/logs/tick_measurement_runs/<run_id>
```

### session_metadata.json

記錄整個 run 的資訊：

- `run_id`。
- 開始與結束時間。
- 實驗、安裝及照明標籤。
- 記錄模式。
- 初始相機與二值化設定。
- 總幀數與實際記錄幀數。
- 四份 CSV 的完整路徑。
- 正常結束或錯誤原因。

### frame_summary.csv

每個被記錄的 frame 一列。建議先查看這份檔案。

主要欄位：

- `run_id`、`frame_id`、時間戳。
- `PASS`、`WARN` 或 `FAIL`。
- FAIL 原因與警告。
- 相機擷取時間 `capture_ms`。
- 二值化時間 `binary_ms`。
- 刻度分析時間 `measurement_ms`。
- 總處理時間 `total_processing_ms`。
- 左右刻度數量。
- 左右及全域 `px/div`。
- 中心位置與 pair center 標準差。
- gap 平均值、標準差與左右差異。
- 曝光、增益、亮度、對比、白平衡及焦距。

### tick_positions.csv

每幀每條偵測刻度一列。正常情況每幀約 26 列：

- 左側 13 條。
- 右側 13 條。
- `tick_id`：從中央向外編號 `0～12`。
- `x_at_axis`：刻度在水平量測軸上的 X 位置。
- `x_full_centroid`：整條刻度的完整質心位置。
- `pair_center_x`：左右相同 tick ID 的中心。
- 長刻度、參考刻度、有效狀態及失敗原因。

### tick_gaps.csv

每幀固定產生 24 列：

- 左側 12 個局部 gap。
- 右側 12 個局部 gap。
- `gap_id` 為 `1～12`。
- `gap_px` 為相鄰刻度的像素距離。
- 缺少刻度時仍會留下無效 gap 與原因。

### pair_centers.csv

每幀固定產生 13 列：

- 左右相同 `tick_id` 的 X 位置。
- `pair_center_x`。
- 是否有效與無效原因。

用來分析刻度中心是否隨時間、拆裝或照明變化而漂移。

---

## 五、實驗結束後做離線分析

分析所有 run：

```bash
cd /home/user/my_project
python3 analyze_tick_measurements.py logs/tick_measurement_runs
```

預設輸出資料夾：

```text
tick_analysis/
```

主要分析結果：

- `gap_time_stability.csv`：同一 gap 隨時間是否穩定。
- `gap_spatial_consistency.csv`：同一幀的 12 個 gap 是否一致。
- `left_right_gap_consistency.csv`：左右同編號 gap 是否相等。
- `pair_center_time_stability.csv`：每個 tick ID 的中心是否隨時間漂移。
- `pair_center_spatial_stability.csv`：同一幀的 pair centers 是否集中。
- `run_comparison.csv`：不同 run 或重新啟動後的比較。
- `condition_comparison.csv`：不同拆裝、實驗及照明條件的比較。
- `analysis_summary.json`：分析 run 與輸出筆數摘要。

### 與人工標註比較

人工標註 CSV 需要以下欄位：

```text
run_id,frame_id,tick_id,region,manual_x
```

執行：

```bash
python3 analyze_tick_measurements.py logs/tick_measurement_runs \
  --manual-annotations manual_ticks.csv
```

會另外產生：

- `manual_annotation_comparison.csv`：每個人工點與 `x_at_axis` 的誤差。
- `manual_annotation_summary.csv`：依 run、左右區域及 tick ID 統計誤差。

---

## 六、單張圖片分析

只有想分析一張已保存的圖片時，才需要直接執行：

```bash
python3 tick_scale_calibration.py image.png --binary --show
```

輸出：

- 單張圖片刻度分析 JSON。
- 彩色刻度標註圖。
- 刻度偵測 mask。

`tick_scale_calibration.py` 不會啟動相機，也不負責長時間 CSV 記錄。

---

## 七、其他程式

### YOLO 主程式

```bash
python3 live_yolo1_app/main.py
```

這是 YOLO 偵測主流程，不是刻度穩定性資料收集工具。

### 無畫面記錄

若不需要預覽視窗，可在 `config.py` 設定：

```python
ENABLE_TUNER_DISPLAY = False
```

相機量測與 CSV 記錄不依賴 `cv2.imshow()`，可按 `Ctrl+C` 結束。
