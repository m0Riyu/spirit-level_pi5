# 斜率校正離線測試

用 `logs/manual_captures` 的 Pi LOG 與同幀 ROI 圖片，對照 MF400U.docx 的 DL-S4W
參考值，在不執行 YOLO 與相機的情況下比較：

- **A**：現行系統（YOLO 框中心，標稱 0.02 mm/m／格）
- **B**：YOLO 框中心 ＋ 線性校正（倍率、零點）
- **C**：亮環次像素精定位 ＋ 線性校正

B、C 以「留一個參考角度」交叉驗證評分：每個參考角度（含重複拍攝）都由不含
該角度的校正預測。

```bash
python run_log_test.py [session_dir] [reference_csv]
```

需要 `opencv-python`、`numpy`。結果輸出到 `output/per_capture.csv` 與
`output/summary.json`。`reference_points.csv` 記錄每筆 sample 對應的 A 軸角度與
DL-S4W 讀值（由 MF400U.docx 與 LOG 時間順序對應）。
