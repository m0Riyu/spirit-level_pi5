# 已確認的 my_project 備份

本快照包含完整 binary_stream_tuner_project 的程式、設定、binary_captures、tick_analysis 和 logs，以及 Pi manual_captures 的 metadata、CSV、兩張 ROI 圖片與 SQLite 狀態資料庫。排除項目與每個檔案 SHA-256 詳見 manifest.json。

既有模型與相機校正資料與 backups/v1-b 相同，仍使用該備份；未覆蓋 v1-b。主程式與 Dashboard 在 Git 根目錄。手機 IndexedDB 不在 Pi 上，需從手機匯出。

## 還原到暫存目錄

```bash
mkdir -p /tmp/pi-project-restore
tar -xzf binary_stream_tuner_project.tar.gz -C /tmp/pi-project-restore
tar -xzf manual_captures.tar.gz -C /tmp/pi-project-restore
```

檢查後，binary_stream_tuner_project 對應 /home/user/my_project/binary_stream_tuner_project；manual_captures 對應 /home/user/my_project/logs/manual_captures。不要直接覆蓋執行中的資料庫或既有量測檔。SQLite 透過 backup API 取得一致快照，其餘檔案為逐一複製後的快照，非整個應用程式的同時交易。
