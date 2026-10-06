# 水平儀視覺量測系統：整合架構規格

> **給實作者（在 Raspberry Pi 上執行的 Claude Code）**
>
> 這份文件是在 Windows 端分析 Pi LOG、MF400U.docx 對照數據與現有程式後整理出的
> 決策與規格。請先完整讀完，再依「8. 實作階段」逐階段進行。每個階段完成後先跑
> 測試、做一次 commit，並向使用者回報結果，取得同意後再進入下一階段。
>
> 目錄約定：
>
> | 目錄 | 角色 | 規則 |
> |---|---|---|
> | `/home/user/my_project` | 現行版本（機台測試備用） | **完全不可修改**，只能讀取 |
> | `/home/user/my_project_V2` | 開發版本 | 所有開發都在這裡進行 |
> | `/home/user/spirit-level_pi5` | GitHub 快照 clone，本文件與 `calibration_test/` 的來源 | 只讀 |
>
> 本文件中的 `my_project/...` 相對路徑，一律指 `my_project_V2/...`。
>
> 工作規則：
> 1. 原目錄 `/home/user/my_project` 不可修改；其中 `live_yolo1_app` 的 git 若有未 commit
>    的修改，先回報使用者。
> 2. V2 的 `live_yolo1_app` 以 `git clone` 從原目錄建立（保留歷史），開分支
>    `feature/integrated-modes` 開發。
> 3. 停止、重啟相機程式或 systemd 服務前，必須先詢問使用者。新舊版共用同一顆相機，
>    不能同時執行；使用相機前要由使用者確認舊版已停止。
> 4. `logs/`、`capture_requests.sqlite3`、既有量測資料只讀不改。
> 5. 開發期間 V2 使用 port **8100（HTTP）/ 8865（WebSocket）**，避免與舊版的 8000 / 8765
>    衝突；P5 之前不得設定開機自動啟動。
> 6. 介面文字使用繁體中文；程式風格比照現有程式（dataclass、純函式、`config.py`
>    集中設定、`test_*.py` 單元測試）。
> 7. 本文件與現有程式不一致時，以實際程式為準並回報差異；本文件中標記
>    「待確認」的項目，實作前要先問使用者。

---

## 1. 目標

將三個目前各自獨立的功能整合為**單一服務、手機網頁操作**的系統：

| # | 功能 | 用途 |
|---|---|---|
| ① | 量測 | YOLO 偵測氣泡 → 斜率 / 角度，手機觸發紀錄（目前唯一可從手機使用的功能） |
| ② | 相機對位 | AprilTag 量相機角度，引導使用者轉**彈簧螺絲**把相機調回基準姿態 |
| ③ | 刻度檢查 | 量測刻度位置，更新「像素 → 格數」幾何校正參數 |

另外加入系統控制（關機、重開機、重啟服務、狀態、LOG 下載）。

三者的關係**不是平行的工具，而是固定流程**：

```text
② 相機對位（粗調）→ ③ 刻度檢查（更新幾何校正）→ ① 量測
```

相機只要動過，③ 就必須重做；③ 未重新確認前，① 顯示「參數待確認」。

---

## 2. 已確認的事實與分析結論

### 2.1 現況

| 功能 | 入口 | 介面 | 相機 |
|---|---|---|---|
| ① | `live_yolo1_app/main.py` → `app.run()` | HTTP :8000 ＋ WebSocket :8765，`dashboard/` | `camera.py`（含 AK7375 對焦 3711） |
| ② | `live_yolo1_app/apriltag_measurement.py` | OpenCV 視窗 | 自行建立 Picamera2，需先結束其他相機程式 |
| ③ | `binary_stream_tuner_project/live_yolo1_app/binary_stream_tuner.py` | tkinter ＋ OpenCV 視窗 | **另一份複製的 `camera.py`，沒有設定 VCM 對焦** |

可重用的核心函式：
- `apriltag_measurement.measure_frame()`、`estimate_tag_pose()`、`summarize()`
- `tick_scale_calibration.calibrate_ticks()`
- `bubble_measurement.BubbleCalibration`、`telemetry_server.build_telemetry_payload()`
- `manual_capture.CaptureManager`、`stability.StabilityTracker`

**風險：** ③ 的相機沒有設定對焦，對焦改變會影響畫面放大倍率，量到的 px/格 可能
與 ① 執行時不同。整合後三個功能必須共用同一份相機設定。

### 2.2 量測準確度分析（MF400U.docx 與 LOG `20261002_145946_64bcd440`）

參考儀 LEVELNIC DL-S4W 與 CNC A 軸的關係為 1:1（斜率 1.005），單位為度。
本系統讀值 ≈ **0.876 × 參考值 + 0.00048°**。

| 問題 | 數值 | 原因 | 處理 |
|---|---|---|---|
| 倍率偏小 12.4% | 實際 ≈ **0.02283 mm/m／格**（程式用標稱值 0.02） | 水平儀管實際靈敏度與標稱值不同 | 水平儀物理校正（倍率） |
| 零點偏移 | **+0.427 格**（≈ +0.0005°、7.6 px） | 刻度幾何中心 ≠ 水平儀真正的水平點 | 水平儀物理校正（零點） |
| 殘差 ±0.0002° | 系統性，不是雜訊 | 推測為水平儀管弧度不均或 CNC 背隙 | 需正反向量測再判斷 |
| LOG 無法追溯 | 文件 13 點只有 10 點對得到 LOG | 沒有欄位記錄參考值 | 拍攝時輸入參考值 |

離線測試（`calibration_test/run_log_test.py`，以「留一個參考角度」交叉驗證）：

| 方法 | 最大誤差 | 均方根誤差 |
|---|---|---|
| 現行系統 | 0.0011° | 0.00055° |
| **YOLO 框中心 ＋ 倍率與零點校正（採用）** | **0.00024°** | **0.00013°** |
| 氣泡亮環次像素定位 ＋ 校正（**不採用**，沒有改善） | 0.00028° | 0.00016° |
| 刻度多項式 1–3 次（目前相機很正，沒有改善；保留作為相機調整後的保險） | 0.00024° | 0.00013° |

其他事實：
- 同一角度重複拍攝，YOLO 中心只差 0.3 px（≈0.00001°），辨識重複性很好。
- 刻度間距 18.0 px/格，25 個間距的標準差 0.23 px；刻度位置偵測解析度只有 0.5 px。
- 刻度擬合的二次項 ≈ -0.0004，左右放大率幾乎相同，相機目前接近垂直。
- 畫面中央（RSK 標誌）沒有刻度；最內側刻度距中心約 ±80.5 px（約 4.5 格），
  氣泡**中心**落在無刻度區，氣泡**兩端**落在刻度區。

### 2.3 機構與使用者決定

- **相機座：** 三個支點呈直角三角形，直角頂點為固定支點，另兩個頂點為彈簧調整螺絲。
  兩顆螺絲各自控制一個互相垂直的傾斜軸，彼此獨立。**機構無法調整 Roll**（繞鏡頭軸旋轉）。
- **AprilTag：** 貼在水平儀上方的玻璃觀景平面，不隨相機移動，可作為固定參考。
  但玻璃平面與水平儀管不一定平行，所以**對位目標是「回到基準角度」，不是歸零**。
- **刻度：** 量完直接更新系統參數；不要把所有間距平均成單一 px/格，改用多項式表示每個位置的換算關係。

---

## 3. 校正模型：分成兩層

```text
                 影像幾何校正（③，相機動過就重做）       水平儀物理校正（CNC，換水平儀管才重做）
氣泡兩端像素 x1, x2 ──────────────→ 兩端格數 ──平均──→ offset_div ──────────────→ slope_mm_per_m
                     div = f(x)                                 (offset_div − zero_offset_div)
                                                                 × mm_per_m_per_div
```

### 3.1 影像幾何校正：像素 → 格數

- 取 26 條刻度的位置 `x_k`，對應格數 `D_k = ±(k + h)`（左負右正，`k` 為由內往外的
  tick_id）。`h` 是最內側刻度到中心的格數，**由擬合求出，不要寫死**（目前約 4.5）。
- 擬合 `div = f(x) = Σ cᵢ (x − x_center)ⁱ`（`x_center` 為左右刻度對的中點，現為 370.25，
  係數以升冪儲存），用 **2 次多項式**（預設，可設定為 1–3 次）。以全部刻度做最小平方法；
  **不要**直接使用個別間距值，因為偵測解析度只有 0.5 px，個別間距帶有約 ±1.4% 的誤差。
- 降低刻度位置雜訊：連續取 N 幀（預設 20）的刻度位置取中位數；
  並改用次像素的 `x_full_centroid`，或在 `calibrate_ticks` 內加入次像素定位。
- 氣泡位置：`offset_div = (f(x1_roi) + f(x2_roi)) / 2`，x1、x2 為 YOLO 框左右邊界。
  兩端都落在刻度區，所以是內插，不需要外插。
- **回歸測試要求：** `f` 為 1 次、且 `h` 與 pitch 對應現行值時，輸出必須和現行的
  `(center − 370.25) / 18.0` 一致（誤差小於 1e-6 格）。

### 3.2 水平儀物理校正：格數 → 斜率

```text
slope_mm_per_m = (offset_div − zero_offset_div) × mm_per_m_per_div
angle_degrees  = degrees(atan(slope_mm_per_m / 1000))
```

- 初始值（由 2026-10-02 數據擬合）：`mm_per_m_per_div = 0.02283`、`zero_offset_div = +0.427`。
- 因為以「格」為單位，**相機調整不影響這兩個值**。只有更換或重新安裝水平儀管、或 CNC
  重新量測時才更新。
- 「水平」門檻 `LEVEL_TOLERANCE_MM_PER_M` 和量測上限 `MAX_MEASURABLE_SLOPE_MM_PER_M`
  在倍率改變後是否調整：**待確認**（倍率改為 0.02283 後，上限 0.12 mm/m 對應的格數從 6 格變為約 5.26 格）。

### 3.3 校正檔與版本管理

所有校正參數從 `config.py` 移到 `my_project/calibration/`，每次更新產生新檔、保留舊檔：

```text
calibration/
├── geometry/            # 影像幾何校正（③）
│   ├── 20261006T184000_geometry.json
│   └── active.json      # {"version": "20261006T184000_geometry"}
├── vial/                # 水平儀物理校正（CNC）
│   ├── 20261002T155000_vial.json
│   └── active.json
└── alignment/           # 對位基準與螺絲模型（②）
    ├── 20261006T183000_alignment.json
    └── active.json
```

`geometry/*.json`：

```json
{
  "schema_version": 1,
  "version": "20261006T184000_geometry",
  "created_at_iso": "2026-10-06T18:40:00+08:00",
  "polynomial_degree": 2,
  "x_center_px": 370.25,
  "coefficients_x_to_div": [0.0, 0.05556, 0.0],
  "inner_half_gap_div": 4.48,
  "tick_positions_px": [{"side": "left", "tick_id": 0, "x": 289.5}],
  "fit_residual_rms_px": 0.26,
  "fit_residual_max_px": 0.60,
  "frames_used": 20,
  "focus_absolute": 3711,
  "image_geometry": {"coordinate_system": "undistorted", "frame_size": [960, 540], "roi_origin": [110, 190]},
  "checks": {"tick_count": 26, "passed": true},
  "source_image": "calibration/geometry/20261006T184000_roi.png",
  "previous_version": "20261002T072923_geometry"
}
```

`vial/*.json`：`mm_per_m_per_div`、`zero_offset_div`、擬合方式、參考儀型號、資料來源的
session 與 sample、交叉驗證誤差。

`alignment/*.json`：基準 tag 角度（Roll/Pitch/Yaw，多幀平均與標準差）、螺絲模型
（見 5.2）、建立時間、對應的 geometry 版本。

規則：
- 舊的 `binary_*_tick_measurement.json` 需寫一支一次性轉換程式，轉成第一版 geometry
  （1 次多項式，數值等同現行）。
- 執行時讀 `active.json`；`session_metadata.json` 與每筆 LOG 記錄當下三種校正的版本。
- 網頁可以列出歷史版本，並把任一版本設為 active（退回）。

---

## 4. 系統架構

```text
┌──────────────── levelsvc（單一 systemd 服務）────────────────┐
│ CameraService（唯一持有 Picamera2）                           │
│   對焦 3711 → 去畸變 → 完整畫面 + ROI，最新一幀放在共享緩衝區  │
│                                                              │
│ ModeManager：同一時間只有一個處理器在跑                         │
│   ├─ MeasureProcessor   ① YOLO → 量測 → 穩定度 → 紀錄         │
│   ├─ AlignProcessor     ② AprilTag → 與基準比較 → 螺絲提示     │
│   └─ TickProcessor      ③ 多幀刻度 → 擬合 → 檢查 → 待套用      │
│                                                              │
│ CalibrationStore   讀寫 calibration/，版本管理                  │
│ CaptureManager     （既有）手機觸發紀錄                         │
│ SystemController   狀態、重啟、關機                             │
│ WebServer          HTTP :8000 ＋ WebSocket :8765（既有，擴充）   │
└──────────────────────────────────────────────────────────────┘
```

設計重點：
- **相機只開一次。** 切換模式只改變每一幀交給哪個處理器，不重開相機，因此三個功能
  保證使用相同的對焦、去畸變與 ROI。目標切換時間小於 0.5 秒。
- 刪除 `binary_stream_tuner_project/live_yolo1_app/camera.py` 這份複製檔，改用共用模組。
- 原本的 OpenCV、tkinter 工具保留為命令列除錯工具（`--headless`、`--image`），
  但不得和服務同時開相機；啟動時若偵測到服務正在執行，就提示並結束。
- YOLO 只在模式 ① 執行，模式 ②、③ 不跑 YOLO，以降低負載。
- 模組拆分建議：`camera_service.py`、`mode_manager.py`、`processors/measure.py`、
  `processors/align.py`、`processors/ticks.py`、`calibration_store.py`、
  `geometry_calibration.py`、`system_controller.py`。保留 `main.py` 作為入口。

---

## 5. 各功能規格

### 5.1 ① 量測（在既有功能上擴充）

- 換算改用第 3 章的兩層校正。
- 手機觸發拍攝的請求加入欄位（全部選填）：
  `reference_deg`（DL-S4W 讀值）、`a_axis_deg`（CNC 設定值）、
  `sweep_direction`（`forward` / `backward` / `zero_check`）、`note`。
- **連拍：** 一次觸發拍 N 幀（預設 15，可設定），CSV 每幀一列，並共用同一個 `burst_id`；
  另外加一列彙總（中位數、標準差）。
- **保存原始畫面：** 每次觸發另存一張**未去畸變的完整畫面 PNG**（無損），供事後重新處理。
  需估算磁碟用量，並在系統頁顯示剩餘可拍張數。
- 若幾何校正狀態為「待確認」，頁面顯示黃色橫幅，但仍可量測。
- 既有 WebSocket telemetry schema 保持相容，新增欄位，不改既有欄位的意義。

### 5.2 ② 相機對位

**量測：** AprilTag 位於去畸變後的完整畫面（不是 ROI）。顯示 Roll/Pitch/Yaw 與基準的
**差值**，採移動平均（預設 10 幀），同時顯示目前的讀值雜訊（標準差）。
若 960×540 的精度不足，評估改用較高解析度：**待確認**（實作時先量測靜止狀態的雜訊並回報）。

**螺絲模型（教學）：**
1. 進入教學，記錄起始角度。
2. 使用者將螺絲 A 順時針轉 1/4 圈，按「完成」，系統記錄角度變化向量。
3. 螺絲 B 重複一次。
4. 存成 2×2 的靈敏度矩陣（每圈造成的角度變化）。因為支點呈直角，兩顆螺絲預期各自
   主要影響一個傾斜軸。

**引導：** 用靈敏度矩陣的反矩陣，把目前的角度差換算成每顆螺絲需要轉的圈數，顯示為：

```text
螺絲 A  順時針 約 1/4 圈   ◀ 紅
螺絲 B  ✓                   ◀ 綠
Roll   +0.12°（機構無法調整，僅供參考）
```

- 容許範圍預設 ±0.10°：**待確認**。全部在範圍內並持續 3 秒，才能按「完成對位」。
- 進入範圍時，手機震動（Vibration API，iOS 不支援）或發出提示音。
- 按「完成對位」：寫入一筆對位紀錄（調整前後角度），並把幾何校正狀態設為「待確認」，
  提示前往 ③。
- 「設為新基準」：只在系統完整校正、量測驗證後使用，需二次確認。

### 5.3 ③ 刻度檢查

1. 顯示 ROI 預覽，並疊上偵測到的刻度。
2. 按「量測」：取 N 幀 → 擬合 → 檢查，顯示結果：

```text
刻度數   26 / 26            ✅
擬合殘差 0.21 px（上限 0.5）  ✅
與目前版本差異：中心處 px/格 18.00 → 18.06（+0.3%）
左右放大率差 0.1%
Roll（由刻度線傾斜推得）+0.08°
[ 套用為新版本 ]  [ 重新量測 ]
```

3. 檢查項目（門檻皆可設定）：刻度數 = 26、殘差 RMS < 0.5 px、
   與目前版本的 px/格 差異 < 3%（超過時顯示警告，需二次確認才能套用）。
4. 套用後寫入 `geometry/` 新版本並設為 active，解除「待確認」狀態。
5. 量測時氣泡必須靜止；若刻度被氣泡邊緣遮住導致數量不足，提示使用者稍微傾斜水平儀後重試。

### 5.4 系統

- 狀態：CPU 溫度、負載、磁碟剩餘、服務執行時間、目前模式、三種校正的 active 版本、目前 git commit。
- 按鈕：重啟服務、重新開機、關機。都需要 **PIN** 加上二次確認（關機按鈕使用長按 2 秒）。
- **安全關機順序：** 停止接收拍攝請求 → 等寫檔佇列清空 → 關閉相機 → 回應「可以斷電」 →
  執行 `systemctl poweroff`。
- **權限：** 服務以一般使用者身分執行；用 sudoers 只開放
  `/usr/bin/systemctl poweroff`、`reboot`、`restart levelsvc` 這幾個指令。
  不可用 root 執行整個服務。
- **PIN：** 存在 `/etc/levelsvc/env`（不進 git），以 HTTP header 傳送；連續錯誤 5 次後鎖定 1 分鐘。
- **LOG 下載：** 列出 session，打包成 zip 下載（包含 CSV、metadata、圖片、當時的校正檔）。

---

## 6. 網頁

單頁應用，延續現有的 `dashboard/`（純 HTML/JS，不引入建置工具）。

```text
#/            主選單：① 量測 ② 相機對位 ③ 刻度檢查 ⚙ 系統
              頂部狀態列：目前模式、校正狀態（正常 / 待確認）、溫度
#/measure     既有儀表板 ＋ 參考值輸入 ＋ 連拍設定
#/align       大字差值、螺絲提示、教學、完成對位
#/ticks       預覽、量測、檢查結果、套用、歷史版本與退回
#/system      狀態、LOG 下載、重啟、關機
```

- 進入頁面時自動送出切換模式的請求；同一時間有多支手機連線時，以最後切換的為準，
  其他手機顯示「模式已被切換」。
- 預覽影像只在 ②、③ 提供：MJPEG、低畫質、最多 5 fps，可關閉。
- 手機直式畫面優先；② 的數字要大到在一臂距離外看得清楚。

---

## 7. API

既有：`POST /api/captures`、`GET /api/captures/ready`、`GET /api/captures/{id}`、WebSocket telemetry。

新增：

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/state` | 模式、校正版本與狀態、系統摘要 |
| POST | `/api/mode` | `{"mode": "measure" \| "align" \| "ticks"}` |
| GET | `/api/preview.mjpg` | 預覽串流（只在 ②、③ 提供） |
| POST | `/api/align/teach/{A\|B}/start`、`/finish` | 螺絲教學 |
| POST | `/api/align/complete` | 完成對位 |
| POST | `/api/align/baseline` | 設為新基準（需 PIN） |
| POST | `/api/ticks/measure` | 執行多幀刻度量測，回傳結果 ID |
| POST | `/api/ticks/{id}/apply` | 套用為新的 geometry 版本 |
| GET | `/api/calibration/{geometry\|vial\|alignment}` | 歷史版本列表 |
| POST | `/api/calibration/{kind}/{version}/activate` | 退回指定版本（需 PIN） |
| GET | `/api/system/status` | 系統狀態 |
| POST | `/api/system/{restart-service\|reboot\|shutdown}` | 需 PIN |
| GET | `/api/logs/sessions`、`/api/logs/sessions/{id}.zip` | LOG 列表與下載 |

WebSocket 訊息加上 `type` 欄位：`telemetry`（既有）、`align`、`ticks`、`state`、`system`。

---

## 8. 實作階段

每個階段結束都要：所有測試通過 → commit → 回報使用者。

### P0 準備（建立 V2）
- 比對原目錄 `/home/user/my_project` 與 GitHub 快照 `/home/user/spirit-level_pi5/my_project`
  的程式差異並回報（以原目錄為準）。
- 建立 `/home/user/my_project_V2`，**執行前先列出要複製的內容與預估大小，給使用者確認**：
  - `live_yolo1_app/`：`git clone /home/user/my_project/live_yolo1_app`，開分支 `feature/integrated-modes`。
  - 從原目錄複製：`best_128x608_ncnn_model/`、`live_yolo1_app_judy_a/` 中使用中的校正檔、
    `binary_stream_tuner_project/`（不含大量 logs）。
  - 從 GitHub 快照複製：`ARCHITECTURE_SPEC.md`、`calibration_test/`。
  - 不複製：`.venv`（在 V2 依 README 重新建立）、`__pycache__`。
  - `logs/`：不整份複製；需要時唯讀引用原目錄，或只複製測試用的 session。
- 修改 V2 的 port 設定為 8100 / 8865。
- 在 V2 跑現有全部測試，記錄基準結果。
- 在 V2 執行 `calibration_test/run_log_test.py`（session 指向原目錄的 LOG），確認可以重現 2.2 的數字。
- 完成後回報，**不要自動進入 P1**。

### P1 量測正確性與現場測試資料（若機台測試時間接近，優先做這一階段）
- 實作 `vial` 校正（倍率、零點）與 `CalibrationStore`（先支援 vial、geometry 第一版轉換）。
- 量測換算改為兩層模型（geometry 先用 1 次多項式）。
- 拍攝時可輸入參考值、連拍、另存原始畫面 PNG、metadata 記錄校正版本。
- 擴充 `run_log_test.py`：直接讀 CSV 中的參考值欄位，不再需要 `reference_points.csv`。
- **驗收：** 回歸測試（倍率 0.02、零點 0 時輸出與舊版相同）；對現有 LOG 重算，結果與 2.2 一致。

### P2 單一服務與模式架構
- `CameraService`、`ModeManager`、共用相機模組；刪除複製的 `camera.py`；修正 ③ 的對焦問題。
- 網頁主選單與 `/api/mode`、`/api/state`；systemd 服務檔（安裝前先給使用者確認）。
- **驗收：** 模式切換小於 0.5 秒且不重開相機；① 的 FPS 不低於重構前的 90%。

### P3 刻度檢查（③）
- 多幀刻度量測、次像素定位、多項式擬合、檢查、版本套用與退回、網頁頁面。
- 量測改用氣泡兩端加多項式換算。
- **驗收：** 對 2026-10-02 的刻度影像，2 次擬合結果的二次項 ≈ 0；
  用新換算重跑 `run_log_test.py`，交叉驗證誤差不比 2.2 差。

### P4 相機對位（②）
- AlignProcessor、基準、螺絲教學、引導頁面、對位紀錄、完成後要求重做 ③。
- **驗收：** 靜止時角度雜訊（回報實測值）；轉動螺絲時頁面在 0.5 秒內反應。

### P5 系統控制
- 狀態、PIN、sudoers 設定（給使用者確認後再安裝）、安全關機順序、LOG 打包下載。

### P6 文件
- 更新 `README.md`：新的啟動方式、操作流程、校正檔說明、現場量測流程。

---

## 9. 現場量測流程（CNC ＋ DL-S4W，提供給使用者）

只有一次機台機會時，目標是**把事後修正需要的資料一次收齊**，並在離開前驗證資料完整。

1. 暖機 30 分鐘；確認對焦與畫面。
2. ② 對位確認 → ③ 刻度檢查（確認 geometry 為最新）。
3. 零點：在 0 位置拍攝；可以的話，把水平儀轉 180° 再拍一次（反轉法）。
4. 正向掃描：約 -0.0065° → +0.0065°，每 0.0005° 一點，每點等氣泡穩定（約 30 秒）後連拍，
   並輸入 A 軸與 DL-S4W 讀值。
5. 反向掃描：以相同的點走回。
6. 開始、中間、結束各做一次零點檢查（觀察漂移）。
7. 兩端各多量 1–2 點超出量程的位置。
8. **離開前**在 Pi 上執行分析：正向掃描用來校正、反向掃描用來驗證；
   資料有缺漏或離群點時當場補拍。

事後可以修正的：倍率、零點、非線性（校正表）、偵測演算法、剔除未穩定的幀。
事後無法補救的：缺少參考值、對焦或曝光不良、拍攝中途相機被碰動、點數不足、沒有保留驗證資料。

---

## 10. 待確認事項

| 項目 | 預設值 | 狀態 |
|---|---|---|
| 對位容許範圍 | ±0.10° | 待確認 |
| 倍率改變後，「水平」門檻與量測上限是否調整 | 維持 mm/m 數值不變 | 待確認 |
| AprilTag 是否需要更高解析度 | 先用 960×540，量測雜訊後決定 | 待確認 |
| 刻度檢查門檻（殘差、與前版差異） | 0.5 px、3% | 待確認 |
| 連拍張數 / 刻度量測幀數 | 15 / 20 | 待確認 |
| PIN 碼 | 使用者自行設定 | 待確認 |
| 剩餘 ±0.0002° 誤差的來源 | — | 待現場正反向量測資料 |
