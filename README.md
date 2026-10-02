# 相機刻度量測系統

目前版本：[第一版 B版](VERSION.md)，Git 標記 `v1-b`。

本工作目錄以 `feature/websocket-dashboard` / `v1-b` / `543ab05` 為基礎，
新增手機單次紀錄；`v1-b` 標記仍指向原版。新功能操作與完整 LOG 規格見
[手機單次拍攝與兩端 LOG](#手機單次拍攝與兩端-log)。

## YOLO 氣泡位置量測

執行：

```bash
cd /home/user/my_project/live_yolo1_app
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

對焦使用 `../live_yolo1_app_judy/camera_calibration/vcm_focus_absolute_test.py` 相同的 AK7375
V4L2 `focus_absolute` 控制方式。`config.py` 預設為
`VCM_FOCUS_ABSOLUTE = 3711`、`VCM_FOCUS_SETTLE_SECONDS = 0.25`；啟動相機後
會自動尋找馬達裝置、依驅動範圍與步距設定並讀回確認，再開始擷取影像。
需要系統已安裝 `v4l2-ctl`（Debian/Raspberry Pi OS 的 `v4l-utils`）。
啟動時終端會顯示設定值與實際值，設定失敗會停止啟動並釋放相機。

`main.py` 的影像流程為：

```text
960 × 540 相機影像 → 全畫面去畸變 → 中央 740 × 160 ROI
→ YOLO → 氣泡量測 → 穩定度 → 數值 WebSocket
                         ↘ 手機觸發時：同幀 ROI × 2 ＋ Pi CSV
```

去畸變使用
`/home/user/my_project/live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result`
驗證報告中的同一組 K、D。該目錄只有驗證結果，實際 NPZ 是報告引用的
`../20260903_004/result/clean/camera_calibration_clean.npz`（相對於
`snapshots/20260904_001`）。路徑由 `config.py` 的 `CAMERA_CALIBRATION_NPZ`
設定；`UNDISTORT_ALPHA = 1.0` 保留完整輸出尺寸與邊緣黑區，不採用
OpenCV 回傳的裁切區。ROI 在去畸變後才擷取，後續影像座標皆為去畸變座標。
映射表只在相機啟動時建立，逐幀使用 `remap`；校正檔缺失、無效或與
960 × 540 尺寸不符時，會停止啟動並釋放相機。

刻度校正 JSON 的 `reference_midpoint_x` 是刻度零點，
`global_pitch.pitch_px` 是每格像素數。程式會辨識座標系：

- 新刻度量測程式輸出的 `image_geometry.coordinate_system = "undistorted"`
  已是去畸變 ROI 座標。主程式確認 K、D、新 K、畫面尺寸及 ROI 原點一致後，
  直接使用校正檔中的零點與間距。目前設定為 **18.0 px/div**。
- 舊 JSON 未記錄 `image_geometry` 時，按原始 ROI 座標處理：使用 `axis_y`
  轉換零點與中心附近間距，格數則將偵測中心反向映射至原始校正座標計算。

使用去畸變後的刻度校正檔時：

```text
bubble_offset_px  = bubble_center_x_roi - scale_center_x_roi
bubble_offset_div = bubble_offset_px / global_pitch.pitch_px
```

結果為正表示氣泡在刻度中心右側，負值表示在左側。終端、預覽畫面及
CSV 都會輸出去畸變座標中的偏移像素與校正格數；校正 JSON 保留不改。
使用舊原始座標校正檔時，`pitch_px_per_div` 表示零點附近的間距，格數以
原始校正座標換算。刻度校正檔不存在、舊檔缺少 `axis_y`，或影像幾何不符時，
程式會保留原本的相機與 YOLO 功能，並將量測標記為不可用。

## 手機 WebSocket 接收畫面

啟動 `main.py` 後，終端會印出接收端網址，例如：

```text
http://192.168.50.46:8000
```

手機和 Raspberry Pi 連到同一個網路後，直接以瀏覽器開啟該網址。
接收端包含 Jetson Nano 上一版的欄位：

- 傳輸效率、數據延遲、推理時間及系統總延遲。
- 系統狀態。
- 坡度、像素位移及傾斜角度。

WebSocket 位址為 `ws://<Pi IP>:8765`。傳輸採用 JSON schema version 1；
網頁斷線後會自動重新連線，且網路傳送不會阻塞相機推論。
氣泡量測每幀更新；摺疊的效能資訊每 0.5 秒刷新，傳輸效率採最近
3 秒平均，其餘時間採最近 30 幀中位數，避免單幀波動造成數字快速
跳動。延遲定義沿用 Jetson Nano 舊版：網頁先經 `/time` 與 Pi 對時；
「數據延遲」是推論完成至手機收到的端到端時間，「系統總延遲」是
影像擷取完成至手機收到資料的端到端時間。

坡度由完成座標轉換的偏移格數換算，與本機量測及 CSV 一致：

```text
slope_mm_per_m = bubble_offset_div * 0.02
angle_degrees = atan(slope_mm_per_m / 1000)
```

系統狀態以坡度判定：`-0.01 ≤ slope_mm_per_m ≤ 0.01` 為 `LEVEL`；
超過 LEVEL 範圍、但仍在 `config.py` 的量測範圍內為 `ADJUST`。
本次保留工作目錄原有 `MAX_MEASURABLE_SLOPE_MM_PER_M = 0.12`：
坡度小於 `-0.12` 或大於 `+0.12 mm/m` 時顯示「超出範圍」，
接收畫面不顯示坡度、像素位移、角度及格數，但氣泡會停在位置圖的
最左端或最右端，並標示偏左／偏右，保留調整方向提示。

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
cd /home/user/my_project/live_yolo1_app
.venv/bin/python main.py
```

這是 YOLO 偵測主流程，不是刻度穩定性資料收集工具。

### 無畫面記錄

若不需要預覽視窗，可在 `config.py` 設定：

```python
ENABLE_TUNER_DISPLAY = False
```

相機量測與 CSV 記錄不依賴 `cv2.imshow()`，可按 `Ctrl+C` 結束。

## 手機單次拍攝與兩端 LOG

以下設定只適用於 YOLO `main.py`；獨立的刻度工具及 AprilTag 工具沿用各自流程。

### 啟動與操作

```bash
cd /home/user/my_project/live_yolo1_app
.venv/bin/python main.py
```

手機與 Pi 連同一網路，開啟終端印出的 `http://<Pi IP>:8000`。
WebSocket 為 `ws://<Pi IP>:8765`。先展開「氣泡穩定度」，等待顯示「穩定」，
按「記錄並拍照」，確認「已配對」筆數增加；實驗結束按「匯出手機端 LOG」。
量測操作區只保留兩個按鈕與手機紀錄筆數，採用緊湊排列；失敗訊息顯示在筆數列，
record_id 保存在兩端 LOG，不另外占用頁面列。
關閉本機預覽時以終端 `Ctrl+C` 結束；若啟用本機預覽，也可按 `q`。
正常結束會保存已凍結的工作、將尚未選幀的要求標記為 `SERVER_SHUTDOWN`，
不詢問是否刪除手動紀錄。

預設：

```python
ENABLE_IMAGE_STREAM = False
ENABLE_CONTINUOUS_CSV = False
TELEMETRY_SEND_EVERY = 2
REQUIRE_STABLE_FOR_CAPTURE = False
JPEG_QUALITY = 95
```

`ENABLE_IMAGE_STREAM` 是既有名稱，實際只控制 **Pi 本機 `cv2.imshow()` 預覽**。
現在本機預覽直接顯示 clean ROI，不逐幀產生標註圖。WebSocket 一律只有數值 JSON，
不傳 JPEG、frame、base64，也沒有手機影像串流。相機、全畫面去畸變、YOLO、量測、
穩定度、按鍵拍攝均不依賴本機預覽。`TELEMETRY_SEND_EVERY = 2` 在推論約 10 FPS
時約傳 5 Hz；它只減少傳送次數，不降低 YOLO 執行次數。實際 FPS 仍須用相機實測。

未按鍵時只建立 session metadata、CSV 表頭及持久 request 索引；**沒有樣本 row、
沒有 JPEG 編碼、沒有圖片寫入、沒有標註影像**。主程式不建立舊 `.csv.part`，
也不呼叫 `ask_to_save_csv()`。若明確設 `ENABLE_CONTINUOUS_CSV = True`，才啟用舊的
逐幀 CSV 和結束時的 Y/N 詢問；該選擇只影響舊 CSV，不會刪除手動成功樣本。

### 執行緒與同幀保證

```text
手機：先將按下當下的純數值保存至 IndexedDB，產生 request_id
  → POST /api/captures（只有 request_id）
HTTP thread：驗證、冪等查詢、加入有界 request queue
主迴圈：在每次 capture_roi() 前取出當時已排入的要求
  → 擷取原有一幀 → 全畫面去畸變 → 740×160 ROI → YOLO
  → BubbleMeasurement → StabilityResult → 凍結 ROI.copy() 與純量
  → 有界 writer queue
背景 writer：clean 的副本標註 → JPEG 暫存檔 × 2 → 原子更名
  → CSV append / flush / fsync → saved
手機：GET status，使用原 request_id 更新同一筆 IndexedDB
```

在 capture 或 YOLO 中途抵達的要求等到下一輪，不能使用正在處理的舊幀。
HTTP thread 與 writer 不呼叫相機，也不額外拍攝。clean 是實際送入 YOLO 的
去畸變 ROI，主迴圈在任何預覽或標註之前 `.copy()`；writer 只拿到這張
740×160 clean 與純量，不持有 full frame、Ultralytics Result 或歷史偵測框。
annotated 由 `clean.copy()` 產生，畫 detection box、氣泡中心、刻度中心線、
record/frame ID、confidence、offset px/div、slope、angle 與兩種狀態。
三行小字及 43 px 深色半透明底保留氣泡主要區域；完整數值以 CSV 為準。
Picamera2 的 RGB888 在目前 Pi 上提供 OpenCV 使用的 BGR byte order，JPEG 不額外交換通道。

兩張圖、CSV row 及 API response 都取自同一個凍結工作，共用 `frame_id`、
`record_id`、`request_id`。JPEG 為有損格式，clean 表示無標註來源，不代表 JPEG
解碼後每個像素完全無損。兩張圖固定 740×160，不保存 960×540 full frame。

request queue、writer queue、active requests 預設上限皆為 4；穩定度歷史只保留
20 筆五項純量。已完成 request 不累積在 Python dictionary，而是保存到
`logs/manual_captures/capture_requests.sqlite3`，使用 UUID primary key 防重複。
相同 UUID 在 pending、processing、saved、rejected、error 時均回傳既有狀態，
即使 Pi 重啟也不建立第二筆。啟動會將中斷要求恢復為已保存（若 CSV 和兩圖完整）
或 `SERVER_RESTARTED`。不要移除這份索引後重送舊 UUID。

### 穩定度公式與調整

每幀在 `stability.py` 將 `(monotonic_timestamp, valid, slope_mm_per_m, offset_div,
confidence)` 放入 `deque(maxlen=20)`。任何非有限 slope、offset、confidence
均標記無效，不參與統計；歷史不包含影像、box、Result 或 telemetry payload。

對視窗內 N 個有效坡度 `s_i`：

```text
valid_ratio = valid_count / sample_count
mean = Σs_i / N
std = sqrt(Σ(s_i - mean)² / N)  # 母體標準差，不是 N-1
range = max(s_i) - min(s_i)
```

```python
STABILITY_WINDOW_SIZE = 20
STABILITY_MIN_VALID_RATIO = 0.90
STABILITY_MAX_STD_MM_PER_M = 0.002
STABILITY_MAX_RANGE_MM_PER_M = 0.006
STABILITY_HOLD_SECONDS = 1.5
```

必須先填滿 20 筆視窗，至少 `ceil(20×0.90)=18` 筆有效且當前幀有效。
目前無效或完整視窗有效率不足為 `NO_MEASUREMENT`；尚未填滿視窗為
`WARMING_UP`；std/range 超標或符合波動條件但 hold 尚未滿為 `UNSTABLE`；
波動條件連續維持至少 1.5 秒才為 `STABLE`。無效、有效率下降、std/range 超標
均重置 hold。所有持續時間使用 `time.monotonic()`，兩端 LOG 日期統一為
台灣 `Asia/Taipei`（UTC+08:00）的 ISO 8601，例如 `2026-10-02T09:35:00.000+08:00`。
Pi 不依賴作業系統時區，手機不依賴瀏覽器時區；epoch ms 與延遲計算不變。
session ID、Pi 舊逐幀 CSV（若啟用）及手機匯出檔名也使用台灣時間。

這些是初始工程門檻，尚未經科學驗證。在 `config.py` 修改後重新啟動；std/range
單位都是 mm/m。穩定只表示停止波動，固定傾角（包括 `ADJUST` 或 `OUT_OF_RANGE`）
仍可能穩定，不取代水平或量測範圍判定。既有公式與正負方向不變：
`slope = offset_div × 0.02`，`angle_degrees = degrees(atan(slope/1000))`。

`REQUIRE_STABLE_FOR_CAPTURE = False` 時暖機、無有效量測、不穩定仍可保存，
按鈕說明提示警告，CSV 忠實記錄狀態；沒有偵測時座標留空，不填虛構 0。
設為 True 時手機停用按鍵，Pi 在選定幀完成穩定度計算後再次檢查，
不符合則 `rejected / NOT_STABLE`，不寫圖片與樣本 row。

### HTTP API

POST 和 GET 都使用 Dashboard 同源 HTTP，結果不放到 latest-only WebSocket queue。

```http
POST /api/captures
Content-Type: application/json

{"request_id":"550e8400-e29b-41d4-a716-446655440000"}
```

```json
{"status":"pending","request_id":"550e8400-e29b-41d4-a716-446655440000"}
```

首次接受回 `202`。JSON/UUID 錯誤 `400`，queue 滿 `429`，工作數達上限 `409`，
相機或保存服務未準備完成 `503`。只接受 request_id，其他欄位一律 `400`。
相同 UUID 回既有狀態，pending 回 `202`，其餘既有狀態回 `200`。

```http
GET /api/captures/550e8400-e29b-41d4-a716-446655440000
```

```json
{
  "status":"saved",
  "request_id":"550e8400-e29b-41d4-a716-446655440000",
  "session_id":"20261002_092500_a1b2c3d4",
  "sample_id":12,
  "record_id":"20261002_092500_a1b2c3d4_000012",
  "frame_id":4852,
  "saved_at_epoch_ms":1790904365123
}
```

查無 UUID 為 `404`；其他狀態 `pending / processing / saved / rejected / error`。
成功只回上面七個欄位，不含量測 LOG、圖片、base64 或 Pi 絕對路徑。

```json
{"status":"error","request_id":"550e8400-e29b-41d4-a716-446655440000",
 "error_code":"IMAGE_WRITE_FAILED","message":"annotated ROI could not be saved"}
```

另外 `GET /api/captures/ready` 只提供 `ready` 和 `require_stable_for_capture`，
供前端判斷 HTTP／相機可用性。重送始終使用原 UUID；只有新按鍵才產生新 UUID。

### Pi 輸出及寫入完整性

```text
/home/user/my_project/logs/manual_captures/
├── capture_requests.sqlite3
└── 20261002_092500_a1b2c3d4/
    ├── session_metadata.json
    ├── pi_capture_log.csv
    └── images/
        ├── 20261002_092500_a1b2c3d4_000012_clean.jpg
        └── 20261002_092500_a1b2c3d4_000012_annotated.jpg
```

session ID 使用本機日期時間與隨機字串，每次啟動不同；sample ID 從 1 遞增，
失敗嘗試可能留下號碼缺口；record ID 是 session ID 加六位 sample ID。
圖片路徑以 session directory 為基準，例如 `images/<record_id>_clean.jpg`。

writer 先寫 `<record_id>_clean.tmp.jpg` 與 `_annotated.tmp.jpg`，兩檔均成功並
flush/fsync 後才用 `os.replace()` 更名、同步 images directory，最後寫 CSV。
CSV 立即 flush/fsync；只有完整提交後 request 才為 saved。CSV 寫入失敗會截回
該列開始前的位置，僅刪除本次未完成圖片及暫存檔，不刪其他成功樣本。
編碼失敗、磁碟滿、圖片／CSV 寫入失敗、worker 例外均顯示終端錯誤與 HTTP error，
worker 繼續下一筆，不終止 YOLO。系統資訊取得失敗只留空。

`csv_write_ms` 計算實際 append/flush/fsync；接著同一 writer 更新該列自身的
診斷時間並再次 flush/fsync，不把第二次診斷更新算入 `csv_write_ms`。
`saved_at` 與 `request_to_saved_ms` 是兩張圖及 CSV 首次完成 durable 寫入的時點；
saved 狀態會等待診斷更新也完成。手機查到 saved 的時間另外保存，包含排程、網路與 polling。

Pi CSV **完整 73 個欄位**（以程式 `manual_capture.PI_LOG_FIELDS` 為準）：

```text
session_id, sample_id, record_id, request_id, frame_id
request_received_at_iso, request_received_at_epoch_ms
frame_started_at_iso, frame_started_at_epoch_ms
capture_completed_at_iso, capture_completed_at_epoch_ms
prediction_completed_at_iso, prediction_completed_at_epoch_ms
saved_at_iso, saved_at_epoch_ms
detected, detection_count, class_id, class_name, confidence
x1_roi, y1_roi, x2_roi, y2_roi, center_x_roi, center_y_roi, box_width, box_height
measurement_valid, measurement_error, bubble_center_x_roi, scale_center_x_roi
pitch_px_per_div, bubble_offset_px, bubble_offset_div, bubble_absolute_offset_div
bubble_direction, slope_mm_per_m, angle_degrees, within_official_range, system_state
stability_state, stability_stable, stability_reason, stability_sample_count
stability_valid_count, stability_valid_ratio, stability_mean_slope_mm_per_m
stability_std_slope_mm_per_m, stability_range_slope_mm_per_m, stability_duration_seconds
capture_ms, predict_ms, yolo_preprocess_ms, yolo_inference_ms, yolo_postprocess_ms
process_ms, fps, annotation_ms, image_write_ms, csv_write_ms, queue_wait_ms
request_to_frame_ms, request_to_saved_ms
cpu_temperature_c, load_average_1m, disk_free_mb
clean_image_path, annotated_image_path, clean_image_width, clean_image_height
annotated_image_width, annotated_image_height
```

`queue_wait_ms`：收到 request 至主迴圈接受；`request_to_frame_ms`：收到至選定幀
完成推論、量測、穩定度；`annotation_ms`：此次 annotated 生成；`image_write_ms`：
兩張 JPEG 編碼、寫入及更名。相機/YOLO/process 的 ms 均沿用同幀計時。
CPU 溫度讀 `/sys/class/thermal/thermal_zone0/temp`、load 使用 `os.getloadavg()`、
磁碟使用 `shutil.disk_usage()`，只在 writer 處理按鍵工作時讀，不增加 psutil。

`session_metadata.json` 每次啟動一次，完整固定欄位：

```text
schema_version, session_id, started_at_iso, log_timezone, hostname, git_commit, git_branch
python_version, model_path, model_image_size, camera_frame_width, camera_frame_height
roi_width, roi_height, roi_x1, roi_y1, roi_x2, roi_y2, confidence_threshold
calibration_source, camera_calibration_source, mm_per_m_per_div
level_tolerance_mm_per_m, max_measurable_slope_mm_per_m
stability_window_size, stability_min_valid_ratio, stability_max_std_mm_per_m
stability_max_range_mm_per_m, stability_hold_seconds, require_stable_for_capture
continuous_csv_enabled, image_stream_enabled, websocket_image_stream_enabled, jpeg_quality
```

Git 查詢失敗為 `unknown`。`image_stream_enabled` 仍表示本機預覽，
`websocket_image_stream_enabled` 固定 False。

### 手機 LOG、匯出與延遲

使用 IndexedDB `bubble-phone-captures-v1` / `captures`，primary key 為 request_id。
按鍵當下同步凍結原始 numeric rolling metrics，先保存 pending，再送 HTTP；
完成後更新同一列。斷線保持 pending/processing 與原 UUID，按鈕停用直到狀態確認；
頁面重新開啟會繼續 GET 查詢，若尚未成功送到 Pi，則以相同 UUID 重送 POST。
IndexedDB 不可用時顯示錯誤、停用拍攝，不偷偷只保存在 JS 變數中。

手機 CSV **完整 27 個欄位**：

```text
request_id, session_id, sample_id, record_id, pi_frame_id, status, error_code, error_message
client_pressed_at_iso, client_pressed_at_epoch_ms
trigger_ack_received_at_iso, trigger_ack_received_at_epoch_ms
pi_saved_response_received_at_iso, pi_saved_response_received_at_epoch_ms
connection_state_at_press, websocket_url, latest_telemetry_frame_id_at_press
latest_telemetry_sent_at_epoch_ms, latest_telemetry_received_at_epoch_ms
message_rate_hz_at_press, data_latency_median_ms_at_press, total_latency_median_ms_at_press
inference_median_ms_at_press, clock_offset_ms_at_press
trigger_ack_ms, button_to_saved_response_ms, status_poll_count
```

尚未成功時 session/sample/record/pi_frame 欄位為 null，CSV 為空字串；沒有冒充 0。
傳輸效率是最近 3 秒；數據延遲、總延遲、推理時間是最近 30 筆有效中位數。
clock offset 由 `/time` 的 HTTP RTT/2 估算。數據延遲計自 Pi 推論完成、
總延遲計自擷取完成到手機接收，均為 **接收端校時估計值**，不是精密同步實測。
這些數值僅留手機，不回傳 Pi；傳輸數值來自按下當下，不是 Pi 完成之後。

trigger ack 與 button-to-saved 時差使用 `performance.now()`；`status_poll_count`
累計 GET 次數，預設每 900 ms 查詢。`button_to_saved_response_ms` 包含 polling interval
與網路耗時；Pi 真正寫入耗時請看 Pi LOG。跨頁面重新整理沒有可比較的 performance
clock，恢復要求的這些時差留空，保留原 pressed timestamp 與數值，不以 wall clock 偽造。
`trigger_ack_ms` 只計收到 POST `202` 的時間；若遺失 `202` 回覆而重送只收到
既有狀態 `200`，該值留空，ack 的接收日期仍記錄首次實際收到的成功 POST 回覆。

按「匯出手機端 LOG」下載 `phone_capture_log_<台灣日期時間>.csv`，UTF-8 BOM、標準 CSV
逗號／雙引號／換行 escaping，按 `client_pressed_at_epoch_ms` 排序，包含 pending、
processing、saved、rejected、error，匯出後不刪 IndexedDB。頁面顯示總數、已配對、
等待與失敗筆數。既有手機 UTC 紀錄在匯出時轉為 `+08:00`，保留同一個 epoch 時點；
不覆寫舊 Pi CSV 檔案。匯出失敗顯示訊息、原資料仍保留。
無痕模式、清除網站資料、更換手機、改用不同 IP/hostname/port（不同 origin）可能
遺失或看不到舊紀錄；每次實驗結束請匯出並確認下載檔案。

### 兩端合併

主要 join key 為 **request_id**；record_id 用於人工檢查與查圖片；必要時再以
`session_id + sample_id` 關聯。Pi CSV 與手機 CSV 的四個共同欄位名稱完全相同：
`request_id, session_id, sample_id, record_id`。不要只用 sample_id，重啟會重新從 1 開始。
例如將全部 Pi session CSV 合併後，與手機 CSV 以 request_id 做 full outer join，
保留尚未配對的 pending/error；交叉檢查 record_id 與 session/sample 一致。
`latest_telemetry_frame_id_at_press` 是按下時手機剛收到的舊幀，`pi_frame_id` 才是
Pi 實際保存的下一幀，兩者不要求相等。此版不另製作合併工具。

### 測試與實機驗證

```bash
cd /home/user/my_project/live_yolo1_app
.venv/bin/python -m unittest discover -v
```

全部既有測試保留；既有 app 逐幀 CSV 回歸測試明確開啟 legacy 開關。
新測試使用 fake frame/mock camera，檢查穩定度、同幀保存、背景失敗、API、
重複要求與關閉。若安裝 Chromium，還會用真瀏覽器與 IndexedDB 驗證重送、
紀錄恢復、數值凍結、按鈕停用、CSV escaping 與匯出失敗；缺少 Chromium 時
只跳過該瀏覽器測試，其餘測試仍執行。瀏覽器測試使用獨立暫存 profile、
`--headless=new --password-store=basic --no-first-run`，保留 Chromium sandbox；
等待 DOMContentLoaded 與 `#captureButton`，不等待 networkidle，避免重連及
定時查詢造成無限等待，也不呼叫桌面 Keyring。

實機請依序驗證：啟動後無本機視窗、手機只顯示數值；等待穩定；按一次保存；
檢查 Pi 一列與兩張 740×160 圖；匯出手機 CSV 比對 request/record/frame ID 與 `+08:00` 日期；
拍攝後斷網及 pending 時重整，再連線確認仍是一筆；觀察 FPS 是否維持約 10、
確認 telemetry 約 5 Hz；Ctrl+C 後無 Y/N，已成功檔案保留。
相機色彩、小字可讀性、真手機 IndexedDB／下載、Wi-Fi 斷線恢復、長時間運作、
真磁碟滿及穩定門檻的物理適切性仍需實機驗證。
