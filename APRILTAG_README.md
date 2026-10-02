# 獨立 AprilTag 量測：Jetson 版本移植

參考 `/home/user/JetsonNano_PTZ/test_apriltag_only.py` 重寫，入口為
`apriltag_measurement.py`，參數集中在 `apriltag_config.py`。不接入原有 YOLO
或 WebSocket 流程，也不需要四張 Tag 的中心座標或共同板面布局。

## 執行

先結束其他正在使用相機的程式，再執行：

```bash
cd /home/user/my_project/live_yolo1_app
.venv/bin/python apriltag_measurement.py
```

預設 `tag36h11`、Tag 邊長 **7 mm（0.007 m）**、影像 `960×540`、
RAW `2328×1748`、AK7375 V4L2 對焦 `3711`。
Jetson 原檔雖註解為 7 mm，實際值是 0.00725 m；本版依本次指定使用
0.007 m。Pi 使用 Picamera2 與 V4L2，沒有沿用 Jetson 的 GStreamer/I2C。

畫面包含原始影像視窗，以及「完整去畸變影像＋側邊欄」視窗。
側邊欄顯示平均 Pitch／Yaw／Roll、距離、各 Tag 的距離與重投影誤差。
影像中繪出標籤框、中心、ID、座標軸及中心十字線。

在去畸變影像區點兩次，`Dist X` 顯示兩點的水平像素距離；第三次點擊
重新開始。側邊欄的點擊不計入量測。這是像素尺，不是實體毫米尺。
按 `q`、Esc 或 Ctrl+C 結束。

## 去畸變參數與完整畫面

本次指定的結果目錄：

```text
/home/user/my_project/live_yolo1_app_judy_a/camera_calibration/snapshots/20260904_001/result
```

這裡是獨立驗證結果，沒有校正 NPZ。
`validation_using_20260903_004/validation_summary.txt` 記載使用的 K/D
來自以下校正檔（驗證結果 PASS），本版載入同一份參數：

```text
/home/user/my_project/live_yolo1_app_judy_a/camera_calibration/snapshots/20260903_004/result/clean/camera_calibration_clean.npz
```

程式啟動時載入 K/D，依 `UNDISTORT_ALPHA=1.0` 計算新內參，並預先建立
remap 表。每幀先去畸變，再偵測 Tag；姿態與座標軸投影使用**新內參＋
零畸變係數**，避免重複補償。輸出保持 `960×540`，不用有效 ROI 裁切，
也不做中央裁切；邊緣可能有黑色留白。

像素中心、四角與滑鼠座標都對應去畸變後的完整畫面。校正 NPZ 的影像
尺寸必須與設定相符，且參數應對應目前相機模式及對焦設定。

## 參數

在 `apriltag_config.py` 調整：

| 設定 | 預設／用途 |
| --- | --- |
| `TAG_FAMILY` | `tag36h11` |
| `TAG_SIZE_METER` | `0.007`，單位公尺 |
| `CALIBRATION_NPZ` | 上述驗證報告所使用的 K/D 檔 |
| `VCM_FOCUS_ABSOLUTE` | `3711` |
| `DISPLAY_WIDTH/HEIGHT` | `960/540`，需與校正尺寸一致 |
| `RAW_WIDTH/HEIGHT` | `2328/1748` |
| `SIDEBAR_WIDTH` | `360`，加在影像右側，不佔用相機畫面 |
| `SHOW_RAW_WINDOW` | `True` |
| `UNDISTORT_ALPHA` | `1.0`，保留完整視野 |
| `POSE_ROTATION_CORRECTION_DEG` | `180.0`，沿用 Jetson 的貼反補償 |
| `TAG_ROTATION_CORRECTIONS_DEG` | 空 mapping；可按 ID 覆寫旋轉補償 |
| `ROLL_LEVEL_TOLERANCE_DEG` | `2.0`，沿用 Roll 在範圍內為綠色、外為紅色的顯示門檻 |

原 Jetson 範例對所有 Tag 的姿態套用 `R @ Rz(180°)`。本版預設保留它，
但如果目前實體 Tag 為正立，應把 `POSE_ROTATION_CORRECTION_DEG` 改成
`0.0`。若各 Tag 貼法不同，可填入 `{實際ID: 實測補償角度}`，個別設定
優先。這是局部座標的旋轉補償，不會建立或推測 Tag 的實體板面位置。

姿態角度的命名沿用 Jetson 公式：Pitch 繞 X、Yaw 繞 Y、Roll 繞 Z。
相機座標 X 向右、Y 向下、Z 向前；偵測姿態先轉換為 Jetson 使用的
Tag 座標方向，再套用旋轉補償。距離是相機到各 Tag 中心的直線距離，
不是單獨的 Z 深度。

側邊欄是各 Tag 姿態的平均，不代表共同板面的 PnP 解，也沒有矩形布局
假設。平均角度採圓周平均，避免 +179° 與 -179° 得到 0°；方向互相
抵銷時顯示 `ambiguous`。Tag 的貼法不同會影響這個平均的意義。

## 7 mm 要量哪個邊界

`tag36h11` 的實體邊長是**兩條相對「外側黑框與白色留白的交界線」
之間的距離**，例如黑框左外緣到右外緣。這個方框的四個外角對應偵測器
回傳角點。不可把紙張／貼紙外緣、外側白色留白、黑框內緣或中間編碼格
寬度當成 Tag 邊長。印製後請確認這兩個交界線之間確實為 7 mm。

## 無視窗、CSV 與圖片測試

```bash
.venv/bin/python apriltag_measurement.py --headless --frames 100
.venv/bin/python apriltag_measurement.py --csv /home/user/my_project/logs/apriltag_run.csv
.venv/bin/python apriltag_measurement.py --image /path/to/960x540_photo.png --headless
```

CSV 每幀每個 Tag 一列，未偵測時寫入 `detected=0`；已存在的檔案不覆寫。
CLI 可用 `--tag-size-mm`、`--calibration`、`--family`、`--focus` 暫時覆寫
參數，預設直接執行即可使用 7 mm。

目前環境已有 OpenCV AprilTag 字典，因此使用 `cv2.aruco` 偵測及
`SOLVEPNP_IPPE_SQUARE` 計算單 Tag 姿態，不需安裝 Jetson 的 `apriltag`
Python 套件。完整重寫前的版本保存在：

```text
/home/user/my_project/git_backups/apriltag_before_jetson_rewrite
```

驗證：

```bash
.venv/bin/python -m unittest test_apriltag_measurement -v
```

測試使用已知 7 mm 合成標籤，驗證姿態角度、距離、旋轉補償、完整
去畸變、畫面兩端偵測、像素尺與 CSV。實際精度仍需實體標籤驗證。

參考：[OpenCV 四角順序](https://docs.opencv.org/4.x/d5/dae/tutorial_aruco_detection.html)、
[PnP 座標與算法](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html)、
[AprilTag 座標與尺寸定義](https://github.com/AprilRobotics/apriltag#pose-estimation)。
