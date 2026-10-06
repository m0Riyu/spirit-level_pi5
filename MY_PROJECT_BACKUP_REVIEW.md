# my_project Git 備份確認清單

盤點日期：2026-10-06（Asia/Taipei）。本清單尚未提交或推送。

目前 Git 根目錄：`/home/user/my_project/live_yolo1_app`；分支：`feature/websocket-dashboard`；HEAD：`f86c567`。盤點前工作目錄乾淨。

## 建議範圍

- 主程式、Dashboard、獨立 AprilTag 程式、設定、文件與測試：32 個已追蹤檔案。
- 外部模型、刻度工具與校正資料：42 個檔案，合計 14,630,919 bytes；SHA-256 比對均與已追蹤的 `backups/v1-b` 相同，不需要重複複製。
- 可新增本次 Pi 手動量測備份：12 個 session，16 筆成功 CSV 紀錄，32 張 ROI JPEG；manual_captures 目前合計 1,380,281 bytes（含狀態資料庫）。
- 建議使用新的日期備份目錄保存 Pi LOG，不覆蓋既有 v1-b；SQLite 如納入，使用 SQLite backup API 建立一致快照。
- 確認後才建立量測備份、提交並推送到既有 origin 分支。

## 主專案已追蹤檔案

以下路徑相對於 Git 根目錄：

- `.gitattributes`
- `.gitignore`
- `APRILTAG_README.md`
- `MANUAL_CAPTURE_REPORT.md`
- `MANUAL_CAPTURE_TEST_RESULTS.txt`
- `README.md`
- `VERSION.md`
- `app.py`
- `apriltag_config.py`
- `apriltag_measurement.py`
- `bubble_measurement.py`
- `camera.py`
- `camera_undistortion.py`
- `config.py`
- `csv_logger.py`
- `dashboard/capture-log.js`
- `dashboard/index.html`
- `detector.py`
- `display.py`
- `main.py`
- `manual_capture.py`
- `requirements.txt`
- `stability.py`
- `telemetry_server.py`
- `test_app.py`
- `test_apriltag_measurement.py`
- `test_bubble_measurement.py`
- `test_camera.py`
- `test_dashboard.py`
- `test_manual_capture.py`
- `test_stability.py`
- `test_telemetry_server.py`

## 外部依賴及工具備份明細

來源路徑相對於 `/home/user/my_project`。對應 Git 備份路徑為 `backups/v1-b/<來源路徑>`。此備份是快照，執行時仍讀取原本來源位置；還原環境時需複製回對應來源位置。

| 來源路徑 | bytes | 與 Git 備份比對 |
|---|---:|---|
| `best_128x608_ncnn_model/metadata.yaml` | 407 | 相同 |
| `best_128x608_ncnn_model/model.ncnn.bin` | 12042780 | 相同 |
| `best_128x608_ncnn_model/model.ncnn.param` | 16546 | 相同 |
| `best_128x608_ncnn_model/model_ncnn.py` | 685 | 相同 |
| `binary_stream_tuner_project/README.md` | 3253 | 相同 |
| `binary_stream_tuner_project/analyze_tick_measurements.py` | 17368 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20260926_014128_460579.json` | 361 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20260926_014128_460579.png` | 3681 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20260926_014128_460579_tick_analysis.png` | 11299 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20260926_014128_460579_tick_measurement.json` | 24318 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20261002_072923_517806.json` | 1444 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20261002_072923_517806.png` | 4223 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20261002_072923_517806_tick_analysis.png` | 12359 | 相同 |
| `binary_stream_tuner_project/binary_captures/binary_20261002_072923_517806_tick_measurement.json` | 25373 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/binary_stream_tuner.py` | 34704 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/camera.py` | 3148 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/camera_undistortion.py` | 2500 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/config.py` | 1866 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/measurement_logger.py` | 13746 | 相同 |
| `binary_stream_tuner_project/live_yolo1_app/test_undistortion.py` | 9677 | 相同 |
| `binary_stream_tuner_project/tick_scale_calibration.py` | 34266 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260903_004/result/clean/camera_calibration_clean.npz` | 9001 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114541_138_0001.jpg` | 130812 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114544_400_0002.jpg` | 134261 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114558_499_0003.jpg` | 136034 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114602_334_0004.jpg` | 133629 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114607_800_0005.jpg` | 132957 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114611_601_0006.jpg` | 132466 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114618_266_0007.jpg` | 142087 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114623_700_0008.jpg` | 133841 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114629_267_0009.jpg` | 134415 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114633_035_0010.jpg` | 141485 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114636_734_0011.jpg` | 145225 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114640_334_0012.jpg` | 143072 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114642_634_0013.jpg` | 141953 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114646_634_0014.jpg` | 141248 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114650_198_0015.jpg` | 134869 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114656_832_0016.jpg` | 138063 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/annotated/capture_20260904_114700_132_0017.jpg` | 135425 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/corner_coverage_validation.png` | 22726 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/validation_report.csv` | 2401 | 相同 |
| `live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result/validation_using_20260903_004/validation_summary.txt` | 945 | 相同 |

主程式實際的相機矩陣來源為 `20260903_004/result/clean/camera_calibration_clean.npz`；指定的 `20260904_001/result` 包含使用這份矩陣的驗證資料，兩者均已備份。

## Pi 手動紀錄候選備份

來源：`/home/user/my_project/logs/manual_captures/`。包含各 session 的 metadata、CSV、images，以及狀態資料庫。手機 LOG 在手機 IndexedDB 中，必須在手機按「匯出手機端 LOG」另行保存，本次 Pi 備份無法取得。

| session | CSV 成功列 | ROI 圖片 |
|---|---:|---:|
| `20261002_115902_75c6ccb5` | 0 | 0 |
| `20261002_123347_a1362ae7` | 2 | 4 |
| `20261002_124101_94a15589` | 0 | 0 |
| `20261002_125948_2b97110f` | 0 | 0 |
| `20261002_132934_6692b5e4` | 0 | 0 |
| `20261002_144335_3904e3cd` | 0 | 0 |
| `20261002_144547_e3a09e11` | 0 | 0 |
| `20261002_145946_64bcd440` | 14 | 28 |
| `20261002_154914_02dd4bb2` | 0 | 0 |
| `20261002_154916_b526cf06` | 0 | 0 |
| `20261002_171055_c413550e` | 0 | 0 |
| `20261002_203350_c4274796` | 0 | 0 |

## 預設不新增

- `.venv`、`__pycache__`、`.git`、暫存檔及秘密金鑰。
- 未被目前設定引用的其他模型與 `.pt` 訓練權重。
- 舊版 `live_yolo1_app_judy` 全目錄；`live_yolo1_app_judy_a` 中未使用的其他校正批次。
- 根目錄的舊版程式與參考檔（live_yolo.py、train_ncnn.py、t.py 等）。
- 舊歷史紀錄已有 `backups/v1-b/measurement_history.tar.gz`；本次不自動新增其他逐幀歷史資料。

## 待使用者確認

建議：保留目前已上 Git 的全部程式與依賴備份，新增本次 Pi 手動紀錄快照及本確認清單，再提交、推送。也可只推送確認清單、不上傳量測 LOG。

## 使用者已確認

使用者確認一起備份 Pi LOG，並明確要求備份 binary_stream_tuner_project。已建立新快照：`backups/snapshot_20261006_175649`。包含完整 tuner 程式、設定、分析與歷史 LOG（排除環境與快取），不僅是 v1-b 中的工具程式。
