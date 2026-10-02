"""用大字控制面板調整即時自適應二值化。

處理方式參考專案根目錄的 t.py，使用 Adaptive Gaussian 與形態學開運算。

執行方式：
    python3 live_yolo1_app/binary_stream_tuner.py
"""

import json
import sys
import tkinter as tk
import time
from datetime import datetime
from tkinter import ttk

import cv2

import config
from camera import (
    capture_frame, close_camera, create_camera,
    describe_image_geometry, normalize_camera_controls,
)

PROJECT_PATH = str(config.PROJECT_DIRECTORY)
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)

from tick_scale_calibration import calibrate_ticks
from measurement_logger import (
    CircularMeasurementCsvLogger,
    print_measurement,
)


ORIGINAL_WINDOW = "Original Preview"
BINARY_WINDOW = "Binary Preview"
PREVIEW_WINDOW = "Tick Measurement"
CAPTURE_DIRECTORY = config.PROJECT_DIRECTORY / "binary_captures"
SETTINGS_PATH = (
    CAPTURE_DIRECTORY / "binary_20260926_014128_460579.json"
)
WHITE_BALANCE_MODES = {
    "日光": "Daylight",
    "陰天": "Cloudy",
    "室內": "Indoor",
    "螢光燈": "Fluorescent",
    "鎢絲燈": "Tungsten",
    "白熾燈": "Incandescent",
}


def load_saved_settings(path=SETTINGS_PATH):
    """Load the requested binary and camera settings file."""
    with open(path, "r", encoding="utf-8") as settings_file:
        settings = json.load(settings_file)

    required = {"block_size", "c_value", "morph_kernel", "camera_controls"}
    missing = required.difference(settings)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"設定檔缺少必要欄位：{names}")
    return settings


class ControlPanel:
    """Compact controls and a live per-tick measurement dashboard."""

    def __init__(self, initial_settings):
        self.root = tk.Tk()
        self.root.title("黑色 LOGO／刻度過濾器")
        self.root.geometry("500x570")
        self.root.minsize(470, 540)
        self.root.configure(bg="#f3f4f6")

        camera_defaults = initial_settings["camera_controls"]
        saved_awb_mode = camera_defaults.get("AwbMode", "Daylight")
        saved_awb_label = next(
            (
                label
                for label, mode in WHITE_BALANCE_MODES.items()
                if mode == saved_awb_mode
            ),
            "日光",
        )

        self.initial_camera_settings = {
            "exposure_time": int(camera_defaults.get("ExposureTime", 20000)),
            "analogue_gain": float(camera_defaults.get("AnalogueGain", 2.0)),
            "brightness": float(camera_defaults.get("Brightness", 0.0)),
            "contrast": float(camera_defaults.get("Contrast", 1.0)),
            "white_balance_mode": saved_awb_label,
            "lens_position": float(
                camera_defaults.get("LensPosition", config.LENS_POSITION)
            ),
        }

        self.c_value = tk.IntVar(value=int(initial_settings["c_value"]))
        self.block_size = tk.IntVar(value=int(initial_settings["block_size"]))
        self.morph_kernel = tk.IntVar(
            value=int(initial_settings["morph_kernel"])
        )
        self.use_roi = tk.BooleanVar(
            value=bool(initial_settings.get("use_roi", True))
        )
        self.exposure_time = tk.IntVar(
            value=self.initial_camera_settings["exposure_time"]
        )
        self.analogue_gain = tk.DoubleVar(
            value=self.initial_camera_settings["analogue_gain"]
        )
        self.brightness = tk.DoubleVar(
            value=self.initial_camera_settings["brightness"]
        )
        self.contrast = tk.DoubleVar(
            value=self.initial_camera_settings["contrast"]
        )
        self.white_balance_mode = tk.StringVar(
            value=self.initial_camera_settings["white_balance_mode"]
        )
        self.lens_position = tk.DoubleVar(
            value=self.initial_camera_settings["lens_position"]
        )
        self.camera_settings_changed = True
        self.save_requested = False
        self.quit_requested = False
        self.dashboard_last_update = 0.0
        self.dashboard = None
        self.dashboard_summary = tk.StringVar(value="等待第一幀量測…")
        self.dashboard_tree = None

        self._build_interface()
        self._build_dashboard()
        self.root.protocol("WM_DELETE_WINDOW", self.request_quit)

    def _build_interface(self):
        title_font = ("Noto Sans CJK TC", 19, "bold")
        heading_font = ("Noto Sans CJK TC", 14, "bold")
        normal_font = ("Noto Sans CJK TC", 11)
        small_font = ("Noto Sans CJK TC", 9)

        tk.Label(
            self.root,
            text="自適應刻度過濾器",
            font=title_font,
            fg="#111827",
            bg="#f3f4f6",
        ).pack(pady=(10, 2))
        tk.Label(
            self.root,
            text=f"已載入：{SETTINGS_PATH.name}",
            font=normal_font,
            fg="#374151",
            bg="#f3f4f6",
        ).pack(pady=(0, 8))

        style = ttk.Style(self.root)
        style.configure(
            "TNotebook.Tab", font=heading_font, padding=(12, 5)
        )
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        binary_tab = tk.Frame(notebook, bg="#f3f4f6")
        camera_tab = tk.Frame(notebook, bg="#f3f4f6")
        color_tab = tk.Frame(notebook, bg="#f3f4f6")
        notebook.add(binary_tab, text="二值化設定")
        notebook.add(camera_tab, text="手動曝光")
        notebook.add(color_tab, text="白平衡／焦距")

        c_frame = tk.LabelFrame(
            binary_tab,
            text="1. 灰色排除強度 C（主要調整）",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=10,
        )
        c_frame.pack(fill="x", padx=22, pady=6)
        tk.Scale(
            c_frame,
            from_=0,
            to=50,
            variable=self.c_value,
            orient=tk.HORIZONTAL,
            length=390,
            width=18,
            sliderlength=26,
            font=heading_font,
            bg="#ffffff",
            highlightthickness=0,
        ).pack(fill="x")
        tk.Label(
            c_frame,
            text="← 降低：保留更多灰色　　提高：排除更多灰色 →",
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack(pady=(2, 0))

        tk.Label(
            c_frame,
            text=(
                f"已載入 {self.c_value.get()}；"
                "氣泡殘留就增加，刻度消失就降低"
            ),
            font=normal_font,
            fg="#b91c1c",
            bg="#ffffff",
        ).pack(pady=(8, 0))

        block_frame = tk.LabelFrame(
            binary_tab,
            text="2. 光線校正範圍 Block Size",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=8,
        )
        block_frame.pack(fill="x", padx=22, pady=8)
        tk.Scale(
            block_frame,
            from_=3,
            to=255,
            resolution=2,
            variable=self.block_size,
            orient=tk.HORIZONTAL,
            length=390,
            width=16,
            sliderlength=24,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
        ).pack(fill="x")
        tk.Label(
            block_frame,
            text=(
                f"已載入 {self.block_size.get()}；"
                "數值越大，參考的周圍範圍越廣"
            ),
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack()

        morph_frame = tk.LabelFrame(
            binary_tab,
            text="3. 雜訊清理 Morph Kernel",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=6,
        )
        morph_frame.pack(fill="x", padx=22, pady=8)
        tk.Scale(
            morph_frame,
            from_=0,
            to=10,
            variable=self.morph_kernel,
            orient=tk.HORIZONTAL,
            length=390,
            width=16,
            sliderlength=24,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
        ).pack(fill="x")
        tk.Label(
            morph_frame,
            text=(
                f"已載入 {self.morph_kernel.get()}；"
                "雜訊多可增加，刻度受損就降低"
            ),
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack()

        options = tk.Frame(binary_tab, bg="#f3f4f6")
        options.pack(fill="x", padx=30, pady=8)
        tk.Checkbutton(
            options,
            text="只處理中央 ROI",
            variable=self.use_roi,
            font=normal_font,
            bg="#f3f4f6",
            activebackground="#f3f4f6",
        ).pack(anchor="w")

        exposure_frame = tk.LabelFrame(
            camera_tab,
            text="1. 曝光時間 Exposure Time（微秒）",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=10,
        )
        exposure_frame.pack(fill="x", padx=22, pady=(14, 8))
        tk.Scale(
            exposure_frame,
            from_=100,
            to=30000,
            resolution=100,
            variable=self.exposure_time,
            orient=tk.HORIZONTAL,
            length=390,
            width=18,
            sliderlength=26,
            font=heading_font,
            bg="#ffffff",
            highlightthickness=0,
            command=self._camera_control_changed,
        ).pack(fill="x")
        tk.Label(
            exposure_frame,
            text="固定約 30 FPS；數值越大越亮，但更容易產生動態模糊",
            font=small_font,
            fg="#b91c1c",
            bg="#ffffff",
        ).pack()

        gain_frame = tk.LabelFrame(
            camera_tab,
            text="2. 類比增益 Analogue Gain",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=8,
        )
        gain_frame.pack(fill="x", padx=22, pady=6)
        tk.Scale(
            gain_frame,
            from_=1.0,
            to=16.0,
            resolution=0.1,
            variable=self.analogue_gain,
            orient=tk.HORIZONTAL,
            length=390,
            width=16,
            sliderlength=24,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
            command=self._camera_control_changed,
        ).pack(fill="x")
        tk.Label(
            gain_frame,
            text="提高會變亮，但也會增加影像雜訊",
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack()

        brightness_frame = tk.LabelFrame(
            camera_tab,
            text="3. 數位亮度 Brightness",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=5,
        )
        brightness_frame.pack(fill="x", padx=22, pady=6)
        tk.Scale(
            brightness_frame,
            from_=-1.0,
            to=1.0,
            resolution=0.05,
            variable=self.brightness,
            orient=tk.HORIZONTAL,
            length=390,
            width=15,
            sliderlength=22,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
            command=self._camera_control_changed,
        ).pack(fill="x")

        contrast_frame = tk.LabelFrame(
            camera_tab,
            text="4. 對比 Contrast",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=8,
        )
        contrast_frame.pack(fill="x", padx=22, pady=6)
        tk.Scale(
            contrast_frame,
            from_=0.5,
            to=2.0,
            resolution=0.05,
            variable=self.contrast,
            orient=tk.HORIZONTAL,
            length=390,
            width=16,
            sliderlength=24,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
            command=self._camera_control_changed,
        ).pack(fill="x")
        tk.Button(
            camera_tab,
            text="還原曝光參數",
            command=self.reset_exposure_controls,
            font=normal_font,
            fg="#ffffff",
            bg="#4b5563",
            activebackground="#374151",
            activeforeground="#ffffff",
            padx=18,
            pady=6,
        ).pack(pady=6)

        white_balance_frame = tk.LabelFrame(
            color_tab,
            text="1. 白平衡模式",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=18,
        )
        white_balance_frame.pack(fill="x", padx=22, pady=(22, 12))
        white_balance_box = ttk.Combobox(
            white_balance_frame,
            textvariable=self.white_balance_mode,
            values=tuple(WHITE_BALANCE_MODES),
            state="readonly",
            width=18,
            font=heading_font,
        )
        white_balance_box.pack(pady=(0, 12))
        white_balance_box.bind(
            "<<ComboboxSelected>>", self._camera_control_changed
        )
        tk.Label(
            white_balance_frame,
            text="依現場光源選一個模式，不需要調紅／藍增益",
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack()

        lens_frame = tk.LabelFrame(
            color_tab,
            text="2. 鏡頭位置 Lens Position",
            font=heading_font,
            fg="#111827",
            bg="#ffffff",
            padx=14,
            pady=8,
        )
        lens_frame.pack(fill="x", padx=22, pady=8)
        tk.Scale(
            lens_frame,
            from_=0.0,
            to=32.0,
            resolution=0.1,
            variable=self.lens_position,
            orient=tk.HORIZONTAL,
            length=390,
            width=16,
            sliderlength=24,
            font=normal_font,
            bg="#ffffff",
            highlightthickness=0,
            command=self._camera_control_changed,
        ).pack(fill="x")
        tk.Label(
            lens_frame,
            text="調到 LOGO 與細刻度邊緣最清楚的位置",
            font=small_font,
            fg="#4b5563",
            bg="#ffffff",
        ).pack()

        tk.Button(
            color_tab,
            text="還原白平衡／焦距",
            command=self.reset_color_controls,
            font=normal_font,
            fg="#ffffff",
            bg="#4b5563",
            activebackground="#374151",
            activeforeground="#ffffff",
            padx=18,
            pady=6,
        ).pack(pady=10)

        buttons = tk.Frame(self.root, bg="#f3f4f6")
        buttons.pack(fill="x", padx=12, pady=(5, 9))
        tk.Button(
            buttons,
            text="儲存畫面",
            command=self.request_save,
            font=normal_font,
            fg="#ffffff",
            bg="#047857",
            activebackground="#065f46",
            activeforeground="#ffffff",
            padx=10,
            pady=6,
        ).pack(side="left", expand=True, padx=3)
        tk.Button(
            buttons,
            text="刻度儀表板",
            command=self.show_dashboard,
            font=normal_font,
            fg="#ffffff",
            bg="#1d4ed8",
            activebackground="#1e40af",
            activeforeground="#ffffff",
            padx=10,
            pady=6,
        ).pack(side="left", expand=True, padx=3)
        tk.Button(
            buttons,
            text="結束",
            command=self.request_quit,
            font=normal_font,
            fg="#ffffff",
            bg="#b91c1c",
            activebackground="#991b1b",
            activeforeground="#ffffff",
            padx=14,
            pady=6,
        ).pack(side="right", expand=True, padx=3)

    def _build_dashboard(self):
        """Create a live table containing every detected tick from left to right."""
        self.dashboard = tk.Toplevel(self.root)
        self.dashboard.title("刻度數值儀表板（左 → 右）")
        self.dashboard.geometry("920x720")
        self.dashboard.minsize(820, 560)
        self.dashboard.configure(bg="#f8fafc")

        tk.Label(
            self.dashboard,
            textvariable=self.dashboard_summary,
            font=("Noto Sans CJK TC", 14, "bold"),
            fg="#111827",
            bg="#f8fafc",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 3))
        tk.Label(
            self.dashboard,
            text=(
                "GAP←：與左邊上一條線的中心距離；"
                "LENGTH：分析帶內單欄最大黑色高度；"
                "AREA：該刻度的黑色像素總數。"
            ),
            font=("Noto Sans CJK TC", 10),
            fg="#475569",
            bg="#f8fafc",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        table_frame = tk.Frame(self.dashboard, bg="#f8fafc")
        table_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        columns = (
            "order",
            "side",
            "tick_id",
            "x",
            "width",
            "gap",
            "length",
            "area",
            "state",
        )
        style = ttk.Style(self.dashboard)
        style.configure(
            "Dashboard.Treeview",
            font=("Noto Sans CJK TC", 11),
            rowheight=23,
        )
        style.configure(
            "Dashboard.Treeview.Heading",
            font=("Noto Sans CJK TC", 11, "bold"),
        )
        self.dashboard_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Dashboard.Treeview",
            height=27,
        )
        headings = {
            "order": "左→右",
            "side": "側邊",
            "tick_id": "ID",
            "x": "X (px)",
            "width": "粗細 (px)",
            "gap": "GAP← (px)",
            "length": "LENGTH (px)",
            "area": "AREA (px)",
            "state": "狀態",
        }
        widths = {
            "order": 70,
            "side": 55,
            "tick_id": 48,
            "x": 82,
            "width": 78,
            "gap": 90,
            "length": 100,
            "area": 90,
            "state": 190,
        }
        for name in columns:
            self.dashboard_tree.heading(name, text=headings[name])
            self.dashboard_tree.column(
                name,
                width=widths[name],
                minwidth=45,
                anchor="w" if name == "state" else "center",
                stretch=name == "state",
            )
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.dashboard_tree.yview,
        )
        self.dashboard_tree.configure(yscrollcommand=scrollbar.set)
        self.dashboard_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.dashboard_tree.tag_configure("valid", foreground="#1d4ed8")
        self.dashboard_tree.tag_configure(
            "reference", foreground="#854d0e", background="#fef9c3"
        )
        self.dashboard_tree.tag_configure("invalid", foreground="#b91c1c")
        self.dashboard.protocol("WM_DELETE_WINDOW", self.hide_dashboard)

    def hide_dashboard(self):
        if self.dashboard is not None:
            self.dashboard.withdraw()

    def show_dashboard(self):
        if self.dashboard is not None:
            self.dashboard.deiconify()
            self.dashboard.lift()

    @staticmethod
    def _format_dashboard_number(value, digits=2):
        if value is None:
            return "—"
        return f"{float(value):.{digits}f}"

    def update_measurement_dashboard(self, measurement):
        """Refresh the dashboard at a Pi-friendly maximum rate of 5 Hz."""
        current_time = time.monotonic()
        if current_time - self.dashboard_last_update < 0.2:
            return
        self.dashboard_last_update = current_time
        if self.dashboard_tree is None:
            return

        candidates = sorted(
            measurement.get("candidates", []),
            key=lambda item: item.get("x_full_centroid", 0.0),
        )
        existing_rows = self.dashboard_tree.get_children()
        if existing_rows:
            self.dashboard_tree.delete(*existing_rows)

        previous_x = None
        for order, candidate in enumerate(candidates, start=1):
            x_value = candidate.get("x_at_axis")
            if x_value is None:
                x_value = candidate.get("x_full_centroid")
            gap = (
                None
                if previous_x is None or x_value is None
                else float(x_value) - float(previous_x)
            )
            if x_value is not None:
                previous_x = x_value

            valid = bool(candidate.get("valid"))
            selected_reference = bool(candidate.get("selected_reference"))
            if not valid:
                state = candidate.get("invalid_reason") or "INVALID"
                tag = "invalid"
            elif selected_reference:
                state = "REFERENCE"
                tag = "reference"
            else:
                state = "VALID"
                tag = "valid"
            tick_id = candidate.get("tick_id")
            self.dashboard_tree.insert(
                "",
                "end",
                values=(
                    order,
                    "L" if candidate.get("side") == "left" else "R",
                    "—" if tick_id is None else tick_id,
                    self._format_dashboard_number(x_value),
                    candidate.get("width", "—"),
                    self._format_dashboard_number(gap),
                    candidate.get("vertical_score", "—"),
                    candidate.get("total_dark_pixels", "—"),
                    state,
                ),
                tags=(tag,),
            )

        pitch = measurement.get("global_pitch", {}).get("pitch_px")
        zero_x = measurement.get("reference_midpoint_x")
        valid_count = sum(bool(item.get("valid")) for item in candidates)
        self.dashboard_summary.set(
            f"{measurement.get('status', 'FAIL')}  |  "
            f"總線數 {len(candidates)}  |  有效 {valid_count}  |  "
            f"L/R {measurement.get('left_candidate_count', 0)}/"
            f"{measurement.get('right_candidate_count', 0)}  |  "
            f"PITCH {self._format_dashboard_number(pitch)} px/div  |  "
            f"ZERO {self._format_dashboard_number(zero_x)} px"
        )

    def read_parameters(self):
        return {
            "block_size": self.block_size.get(),
            "c_value": self.c_value.get(),
            "morph_kernel": self.morph_kernel.get(),
            "use_roi": self.use_roi.get(),
        }

    def _camera_control_changed(self, _value=None):
        self.camera_settings_changed = True

    def reset_exposure_controls(self):
        self.exposure_time.set(
            self.initial_camera_settings["exposure_time"]
        )
        self.analogue_gain.set(
            self.initial_camera_settings["analogue_gain"]
        )
        self.brightness.set(self.initial_camera_settings["brightness"])
        self.contrast.set(self.initial_camera_settings["contrast"])
        self.camera_settings_changed = True

    def reset_color_controls(self):
        self.white_balance_mode.set(
            self.initial_camera_settings["white_balance_mode"]
        )
        self.lens_position.set(
            self.initial_camera_settings["lens_position"]
        )
        self.camera_settings_changed = True

    def read_camera_settings(self):
        return {
            "AeEnable": False,
            "AwbEnable": True,
            "AwbMode": WHITE_BALANCE_MODES[self.white_balance_mode.get()],
            "FrameDurationLimits": (33333, 33333),
            "ExposureTime": int(self.exposure_time.get()),
            "AnalogueGain": round(self.analogue_gain.get(), 2),
            "Brightness": round(self.brightness.get(), 2),
            "Contrast": round(self.contrast.get(), 2),
            "LensPosition": round(self.lens_position.get(), 2),
        }

    def consume_camera_settings(self):
        if not self.camera_settings_changed:
            return None
        self.camera_settings_changed = False
        return self.read_camera_settings()

    def update(self):
        if self.quit_requested:
            return False
        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            return False
        return not self.quit_requested

    def request_save(self):
        self.save_requested = True

    def consume_save_request(self):
        requested = self.save_requested
        self.save_requested = False
        return requested

    def request_quit(self):
        self.quit_requested = True

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def select_image(frame, use_roi):
    if not use_roi:
        return frame
    return frame[
        config.ROI_Y1 : config.ROI_Y2,
        config.ROI_X1 : config.ROI_X2,
    ]


def binarize(frame, parameters):
    """Apply the same Adaptive Gaussian and morphology flow as t.py."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    block_size = max(3, parameters["block_size"])
    if block_size % 2 == 0:
        block_size += 1

    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size,
        parameters["c_value"],
    )

    kernel_size = parameters["morph_kernel"]
    if kernel_size > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (kernel_size, kernel_size)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    return binary


def measure_ticks(binary):
    """Measure tick positions and pitch using tick_scale_calibration.py."""
    result, annotated, _mask = calibrate_ticks(
        binary,
        threshold_mode="binary",
        logo_x1=304,
        logo_x2=441,
        logo_margin=0,
    )
    return result, annotated


def make_measurement_preview(annotated, measurement):
    """Add an unobtrusive measurement header above the annotated image."""
    preview = cv2.copyMakeBorder(
        annotated,
        58,
        0,
        0,
        0,
        cv2.BORDER_CONSTANT,
        value=(28, 28, 28),
    )
    status = measurement["status"]
    status_color = {
        "PASS": (0, 220, 0),
        "WARN": (0, 220, 255),
        "FAIL": (0, 0, 255),
    }.get(status, (255, 255, 255))

    pitch = measurement["global_pitch"]["pitch_px"]
    zero_x = measurement["reference_midpoint_x"]
    pitch_text = "N/A" if pitch is None else f"{pitch:.2f} px/div"
    zero_text = "N/A" if zero_x is None else f"{zero_x:.2f} px"

    cv2.putText(
        preview,
        status,
        (10, 22),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        status_color,
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        preview,
        (
            f"TICKS L/R: {measurement['left_candidate_count']}/"
            f"{measurement['right_candidate_count']}   "
            f"PITCH: {pitch_text}   ZERO: {zero_text}"
        ),
        (90, 22),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (240, 240, 240),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        preview,
        "BLUE valid   YELLOW reference   RED invalid   MAGENTA zero",
        (10, 47),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.46,
        (210, 210, 210),
        1,
        cv2.LINE_AA,
    )
    return preview


def save_capture(binary, parameters, measurement, annotated):
    CAPTURE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    image_path = CAPTURE_DIRECTORY / f"binary_{timestamp}.png"
    settings_path = CAPTURE_DIRECTORY / f"binary_{timestamp}.json"
    measurement_path = (
        CAPTURE_DIRECTORY / f"binary_{timestamp}_tick_measurement.json"
    )
    analysis_path = (
        CAPTURE_DIRECTORY / f"binary_{timestamp}_tick_analysis.png"
    )

    cv2.imwrite(str(image_path), binary)
    with open(settings_path, "w", encoding="utf-8") as settings_file:
        json.dump(parameters, settings_file, ensure_ascii=False, indent=2)
    with open(measurement_path, "w", encoding="utf-8") as measurement_file:
        json.dump(measurement, measurement_file, ensure_ascii=False, indent=2)
    cv2.imwrite(str(analysis_path), annotated)

    print(f"已儲存影像：{image_path}")
    print(f"已儲存參數：{settings_path}")
    print(f"已儲存刻度量測：{measurement_path}")
    print(f"已儲存刻度標註：{analysis_path}")


def apply_camera_settings(camera, settings):
    """Apply only controls advertised by the active camera."""
    settings = normalize_camera_controls(settings)
    available = getattr(camera, "camera_controls", {})
    supported = {
        name: value for name, value in settings.items() if name in available
    }
    if not supported:
        return
    try:
        camera.set_controls(supported)
    except RuntimeError as error:
        print(f"無法套用相機畫面參數：{error}")


def run():
    camera = None
    controls = None
    measurement_logger = None
    last_measurement_log_time = None
    frame_id = 0
    try:
        initial_settings = load_saved_settings()
        controls = ControlPanel(initial_settings)
        measurement_logger = CircularMeasurementCsvLogger()
        cv2.namedWindow(ORIGINAL_WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(ORIGINAL_WINDOW, 900, 280)
        cv2.namedWindow(BINARY_WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(BINARY_WINDOW, 900, 280)
        cv2.namedWindow(PREVIEW_WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(PREVIEW_WINDOW, 900, 340)
        initial_camera_settings = controls.consume_camera_settings()
        camera = create_camera(initial_camera_settings)
        image_geometry = describe_image_geometry(camera)
        (measurement_logger.run_directory / "image_geometry.json").write_text(
            json.dumps(image_geometry, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"已載入設定：{SETTINGS_PATH}")
        print("即時刻度量測已啟動。")
        print(f"量測CSV：{measurement_logger.path.resolve()}")

        while controls.update():
            frame_id += 1
            loop_start = time.perf_counter()
            camera_settings = controls.consume_camera_settings()
            if camera_settings is not None:
                apply_camera_settings(camera, camera_settings)

            capture_start = time.perf_counter()
            frame = capture_frame(camera)
            capture_ms = (time.perf_counter() - capture_start) * 1000.0

            binary_start = time.perf_counter()
            parameters = controls.read_parameters()
            source = select_image(frame, parameters["use_roi"])
            binary = binarize(source, parameters)
            binary_ms = (time.perf_counter() - binary_start) * 1000.0

            measurement_start = time.perf_counter()
            measurement, annotated = measure_ticks(binary)
            measurement["image_geometry"] = {
                **image_geometry,
                "roi_origin": ([config.ROI_X1, config.ROI_Y1]
                               if parameters["use_roi"] else [0, 0]),
            }
            measurement_ms = (
                time.perf_counter() - measurement_start
            ) * 1000.0
            controls.update_measurement_dashboard(measurement)
            current_time = time.monotonic()
            should_log = (
                last_measurement_log_time is None
                or current_time - last_measurement_log_time
                >= config.MEASUREMENT_LOG_INTERVAL_SECONDS
            )
            if should_log:
                frame_metadata = {
                    "run_id": measurement_logger.run_id,
                    "frame_id": frame_id,
                    "datetime": datetime.now().isoformat(
                        timespec="milliseconds"
                    ),
                    "capture_ms": capture_ms,
                    "binary_ms": binary_ms,
                    "measurement_ms": measurement_ms,
                    "total_processing_ms": (
                        time.perf_counter() - loop_start
                    )
                    * 1000.0,
                    "record_mode": "interval",
                    "camera_settings": controls.read_camera_settings(),
                }
                measurement_index = measurement_logger.write(
                    measurement,
                    frame_metadata,
                )
                print_measurement(measurement_index, measurement)
                last_measurement_log_time = current_time
            preview = make_measurement_preview(annotated, measurement)
            cv2.imshow(ORIGINAL_WINDOW, source)
            cv2.imshow(BINARY_WINDOW, binary)
            cv2.imshow(PREVIEW_WINDOW, preview)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                controls.request_quit()
            save_requested = controls.consume_save_request()
            if key == ord("s") or save_requested:
                saved_parameters = {
                    **parameters,
                    "camera_controls": controls.read_camera_settings(),
                    "image_geometry": measurement["image_geometry"],
                }
                save_capture(
                    binary,
                    saved_parameters,
                    measurement,
                    annotated,
                )

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止二值化串流。")
    finally:
        if measurement_logger is not None:
            measurement_logger.close()
        if camera is not None:
            close_camera(camera)
        if controls is not None:
            controls.close()
        cv2.destroyAllWindows()
        print("二值化串流已結束。")


if __name__ == "__main__":
    run()
