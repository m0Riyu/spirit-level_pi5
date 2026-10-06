# my_project 原始資料夾結構

GitHub 的 my_project/ 為已確認備份範圍的目錄快照，保留 Raspberry Pi 上相對於 /home/user/my_project/ 的原始路徑，可直接瀏覽、下載，不需要解壓縮。

```text
my_project/
├── live_yolo1_app/                 # 主程式、Dashboard、AprilTag、設定、測試與文件
├── binary_stream_tuner_project/   # 程式、設定、binary_captures、tick_analysis、logs
├── best_128x608_ncnn_model/        # 使用中的 NCNN 模型
├── live_yolo1_app_judy_a/          # 使用中的相機校正與驗證資料
└── logs/manual_captures/          # Pi LOG、同幀 ROI 圖片及狀態資料庫快照
```

GitHub 最新版僅保留 my_project/。Pi 原本執行目錄的程式保留於本機，未刪除；之後程式更新時需同步更新此快照。舊備份與先前根目錄檔案可從 Git 提交歷史取得。排除虛擬環境、快取、金鑰、未使用模型與未使用舊專案。手機 IndexedDB LOG 需另外匯出。

量測資料沿用使用者已確認的 2026-10-06 17:56:49（Asia/Taipei）快照，避免對執行中的相機流程或資料庫做任何修改。逐檔 SHA-256 詳見 BACKUP_MANIFEST.json。

下載後以 my_project/ 作為專案根目錄，可執行：

```bash
cd my_project/live_yolo1_app
.venv/bin/python main.py
```

虛擬環境與 Raspberry Pi 系統依賴需依原 README 安裝，未納入 Git。
