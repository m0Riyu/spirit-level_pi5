# 手機單次量測完成報告

此報告補充 README 的完整操作規格。本次所有測試使用 mock camera/fake ROI，沒有啟動真實相機。

最新修訂：兩端 LOG 統一 Asia/Taipei（UTC+08:00）；手機既有 UTC 紀錄在匯出時轉換。操作區只顯示兩個按鈕及紀錄筆數，錯誤併入筆數列。保留使用者的 50 幀視窗及操作區位於頁面上方的最新調整。

1. **版本與檔案**：修改前為 `feature/websocket-dashboard`、`v1-b`、`543ab05`。保留原先未提交的 `MAX_MEASURABLE_SLOPE_MM_PER_M=0.12`、`ENABLE_IMAGE_STREAM=False`。主要功能已在目前 WIP commit `e6e07a6`。本次提交包含 Keyring 修正、統一時區與精簡介面。相機、去畸變、YOLO、BubbleMeasurement 與 CSV logger 核心沒有重寫。

   修改：`app.py`、`config.py`、`display.py`、`telemetry_server.py`、`dashboard/index.html`、`README.md`、`test_app.py`。
   新增：`stability.py`、`manual_capture.py`、`dashboard/capture-log.js`、`test_stability.py`、`test_manual_capture.py`、`test_dashboard.py`。另保存本報告與 `MANUAL_CAPTURE_TEST_RESULTS.txt`。

2. **資料流**：HTTP thread 只驗證 UUID、冪等查詢、排入有界 queue。主迴圈在 capture 前接受要求，依既有流程完成擷取、去畸變、ROI、YOLO、量測與穩定度，再凍結 ROI 和純量。背景 writer 產生標註、JPEG、圖片原子更名及 CSV。HTTP GET 查結果，WebSocket 只傳數值。

3. **穩定度**：目前 config.py 為 50 筆視窗；至少 45 筆有效且目前幀有效（初始工程值為 20 筆、18 筆）。使用母體標準差 `sqrt(Σ(s_i−mean)²/N)`、極差 `max−min`。std ≤0.002 mm/m、range ≤0.006 mm/m，連續維持 1.5 秒才穩定。歷史僅五項純量，有界 deque；NaN/Infinity 不參與統計。固定傾角仍可穩定。門檻為尚未科學驗證的工程初值，全部在 config.py。預設不要求穩定才能拍攝。

4. **Pi LOG 完整欄位**：

```text
session_id
sample_id
record_id
request_id
frame_id
request_received_at_iso
request_received_at_epoch_ms
frame_started_at_iso
frame_started_at_epoch_ms
capture_completed_at_iso
capture_completed_at_epoch_ms
prediction_completed_at_iso
prediction_completed_at_epoch_ms
saved_at_iso
saved_at_epoch_ms
detected
detection_count
class_id
class_name
confidence
x1_roi
y1_roi
x2_roi
y2_roi
center_x_roi
center_y_roi
box_width
box_height
measurement_valid
measurement_error
bubble_center_x_roi
scale_center_x_roi
pitch_px_per_div
bubble_offset_px
bubble_offset_div
bubble_absolute_offset_div
bubble_direction
slope_mm_per_m
angle_degrees
within_official_range
system_state
stability_state
stability_stable
stability_reason
stability_sample_count
stability_valid_count
stability_valid_ratio
stability_mean_slope_mm_per_m
stability_std_slope_mm_per_m
stability_range_slope_mm_per_m
stability_duration_seconds
capture_ms
predict_ms
yolo_preprocess_ms
yolo_inference_ms
yolo_postprocess_ms
process_ms
fps
annotation_ms
image_write_ms
csv_write_ms
queue_wait_ms
request_to_frame_ms
request_to_saved_ms
cpu_temperature_c
load_average_1m
disk_free_mb
clean_image_path
annotated_image_path
clean_image_width
clean_image_height
annotated_image_width
annotated_image_height
```

5. **手機 LOG 完整欄位**：

```text
request_id
session_id
sample_id
record_id
pi_frame_id
status
error_code
error_message
client_pressed_at_iso
client_pressed_at_epoch_ms
trigger_ack_received_at_iso
trigger_ack_received_at_epoch_ms
pi_saved_response_received_at_iso
pi_saved_response_received_at_epoch_ms
connection_state_at_press
websocket_url
latest_telemetry_frame_id_at_press
latest_telemetry_sent_at_epoch_ms
latest_telemetry_received_at_epoch_ms
message_rate_hz_at_press
data_latency_median_ms_at_press
total_latency_median_ms_at_press
inference_median_ms_at_press
clock_offset_ms_at_press
trigger_ack_ms
button_to_saved_response_ms
status_poll_count
```

6. **API 範例**：

```http
POST /api/captures
Content-Type: application/json

{"request_id":"550e8400-e29b-41d4-a716-446655440000"}
```

```json
{"status":"pending","request_id":"550e8400-e29b-41d4-a716-446655440000"}
```

首次接受為 202。JSON/UUID 無效 400、queue 滿 429、active 上限 409、未準備好 503。GET 查無要求 404。GET `/api/captures/<request_id>` 成功範例：

```json
{"status":"saved","request_id":"550e8400-e29b-41d4-a716-446655440000","session_id":"20261002_092500_a1b2c3d4","sample_id":12,"record_id":"20261002_092500_a1b2c3d4_000012","frame_id":4852,"saved_at_epoch_ms":1790904365123}
```

回覆僅七個必要欄位，不含量測 LOG、影像或絕對路徑。失敗為 status error/rejected 加 request_id、error_code、message。

7. **正式輸出資料夾範例**（本次測試 session 在暫存資料夾中，未產生真實實驗樣本）：

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

8. **ROI 產生**：clean 是實際送入 YOLO 的去畸變 bubble_roi，在主迴圈 `.copy()`；annotated 由 clean.copy() 加上框線、氣泡中心、刻度中心線及三行小字。都為 740×160，JPEG quality 95，沿用 BGR byte order。不保存 960×540 full frame。clean JPEG 無標註，但 JPEG 本身是有損格式。

9. **同幀保證**：每筆 writer job 只有一張凍結 clean ROI 和該幀純量；兩圖與 CSV、API 共用相同 frame_id/record_id/request_id。不重新呼叫相機，不使用舊快取。中途到達要求延至下一輪 capture。

10. **防重複**：request_id 為 UUID、手機 IndexedDB primary key；Pi SQLite primary key，重送回既有狀態。完成狀態持久保存，重啟也不重複工作。兩端皆更新原要求，不建立第二筆紀錄。

11. **手機匯出**：按「匯出手機端 LOG」下載 UTF-8 BOM CSV，依按下時間排序，包含成功、等待、失敗；正確 escaping，匯出後不清除 IndexedDB。無痕、清除網站資料、更換手機或網站 origin 可能遺失資料，實驗結束務必匯出。手機 metrics 在按下當下凍結，從不傳回 Pi。

12. **兩端合併**：主要 join key 為 request_id，其次 record_id，再次 session_id+sample_id；四個共同欄位名稱一致，不單用 sample_id。Pi 保存下一幀，因此 pi_frame_id 不必等於手機按下時的 latest_telemetry_frame_id_at_press。手機延遲是接收端對時估計；button_to_saved_response_ms 包含 900 ms polling 的間隔。跨頁面重整的 monotonic 時差留空。

13. **測試**：

```bash
cd /home/user/my_project/live_yolo1_app
.venv/bin/python -m unittest discover -v
```

最終結果：102 tests，8.721 秒，OK；另外 Chromium 測試內 35 項瀏覽器/IndexedDB 斷言通過，沒有 skip。完整逐項結果在 `MANUAL_CAPTURE_TEST_RESULTS.txt`。`git diff --check` 通過。

    Chromium 已使用 `--headless=new --password-store=basic --no-first-run`，移除預設 `--no-sandbox`；等待 DOMContentLoaded 與 #captureButton，不等待 networkidle。主機上先前的測試 Chromium 已不在執行。

14. **Pi 操作**：若舊主程式仍在跑，先 Ctrl+C 正常結束，再於專案目錄以 `.venv/bin/python main.py` 啟動。手機開 `http://<Pi IP>:8000`，確認連線，等待穩定後按「記錄並拍照」，從匯出的 LOG 檢查 record_id、Pi 一列 CSV 與兩張 ROI，實驗結束匯出手機 CSV。Ctrl+C 不出現舊 CSV Y/N 問題，成功手動紀錄保留。

15. **尚待實機驗證**：真實相機約 10 FPS、實際 Wi-Fi/手機瀏覽器、IndexedDB 下載、pending 重整與斷線、JPEG 色彩/小字、長時間運作、真實磁碟滿、物理穩定門檻。自動測試已涵蓋相應 mock 錯誤流程，但不能替代這些實機量測。
