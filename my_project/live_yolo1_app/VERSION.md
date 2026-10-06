# 第一版 B版

Git 標記：`v1-b`。版本日期：2026-10-02。
推送分支：`feature/websocket-dashboard`。

本版本包含：

- YOLO 主程式：AK7375 對焦 `3711`，完整 960 × 540 影像先去畸變，再擷取中央 740 × 160 ROI。
- 氣泡刻度：目前採用 `binary_20261002_072923_517806_tick_measurement.json`，零點 `370.25 px`、每格 `18.0 px/div`；已去畸變的刻度校正不再重複轉換。
- 網頁儀表：氣泡位置即時更新，移除位置動畫造成的延遲。
- 獨立 AprilTag 程式：參考 Jetson 版本，Tag 邊長 7 mm，完整影像去畸變、不裁切，不接入 YOLO 流程。
- 獨立刻度量測專案：去畸變後才選取 ROI、二值化與量測，保存影像幾何資料。

相機 K、D 使用 `20260904_001/result` 驗證報告引用的
`20260903_004/result/clean/camera_calibration_clean.npz`。

## 備份位置

主程式、AprilTag 程式及網頁放在儲存庫根目錄。
外部專案與本版使用的檔案放在 `backups/v1-b/`：

```text
backups/v1-b/
├── binary_stream_tuner_project/   # 程式、設定、原始及去畸變刻度擷取
├── best_128x608_ncnn_model/       # 本版 YOLO 模型
├── live_yolo1_app_judy_a/         # 校正 NPZ、驗證報告與驗證圖
├── measurement_history.tar.gz   # 刻度紀錄、離線分析及已完成的 YOLO CSV
└── manifest.json                # 檔案大小與 SHA-256
```

本版備份保留各專案的獨立執行流程。`measurement_history.tar.gz` 的檔案
路徑以 `my_project` 為根目錄，包含刻度專案的 `logs/`、`tick_analysis/`
及主程式已完成的 `logs/yolo_128x608_metrics_*.csv`。

## 還原外部檔案

在取出 `v1-b` 的 `live_yolo1_app` 目錄中執行，可還原成原本的相鄰目錄布局：

```bash
cp -a backups/v1-b/binary_stream_tuner_project ../
cp -a backups/v1-b/best_128x608_ncnn_model ../
cp -a backups/v1-b/live_yolo1_app_judy_a ../
tar -xzf backups/v1-b/measurement_history.tar.gz -C ..
```

執行方式及相機環境需求見 [README.md](README.md)、
[APRILTAG_README.md](APRILTAG_README.md) 與
[刻度專案 README](backups/v1-b/binary_stream_tuner_project/README.md)。

## 驗證

主程式相關回歸測試 46 項、獨立刻度專案測試 6 項通過。
主程式曾完成 5 幀相機與 YOLO 實機驗證；獨立刻度程式本次實機測試因
相機被其他程式佔用而未完成。
