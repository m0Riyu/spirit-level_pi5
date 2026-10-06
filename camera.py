"""Picamera2 setup and frame capture."""

import re
import subprocess
import time
from pathlib import Path

from picamera2 import Picamera2
from libcamera import controls

import config
from camera_undistortion import FullFrameUndistorter, load_calibration


def find_vcm_device():
    for name_path in sorted(Path("/sys/class/video4linux").glob("v4l-subdev*/name")):
        try:
            name = name_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if "ak7375" in name:
            return Path("/dev") / name_path.parent.name
    raise RuntimeError("找不到 AK7375 V4L2 subdevice。")


def run_v4l2(device, *arguments):
    result = subprocess.run(
        ["v4l2-ctl", "-d", str(device), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"v4l2-ctl 失敗：{detail}")
    return result.stdout


def query_focus_control(device):
    output = run_v4l2(device, "--list-ctrls")
    match = re.search(
        r"focus_absolute[^:]*:\s*"
        r"min=(-?\d+)\s+max=(-?\d+)\s+step=(\d+)\s+"
        r"default=(-?\d+)\s+value=(-?\d+)",
        output,
    )
    if not match:
        raise RuntimeError(f"{device} 沒有可用的 focus_absolute 控制。\n{output}")
    return tuple(map(int, match.groups()))


def clamp_focus(value, minimum, maximum, step):
    value = max(minimum, min(maximum, int(round(value))))
    if step > 1:
        value = minimum + round((value - minimum) / step) * step
    return max(minimum, min(maximum, value))


def set_focus(device, value, minimum, maximum, step):
    value = clamp_focus(value, minimum, maximum, step)
    output = run_v4l2(
        device,
        "--set-ctrl",
        f"focus_absolute={value}",
        "--get-ctrl",
        "focus_absolute",
    )
    match = re.search(r"focus_absolute:\s*(-?\d+)", output)
    if not match:
        raise RuntimeError(f"無法讀回 focus_absolute：{output}")
    actual = int(match.group(1))
    if actual != value:
        raise RuntimeError(f"VCM 對焦值不符：requested={value}, actual={actual}")
    return value, actual


def normalize_camera_controls(camera_controls):
    """Convert serializable UI values into libcamera control values."""
    normalized = dict(camera_controls)
    awb_mode = normalized.get("AwbMode")
    if isinstance(awb_mode, str):
        normalized["AwbMode"] = getattr(
            controls.AwbModeEnum, awb_mode, controls.AwbModeEnum.Daylight
        )
    return normalized


def create_camera(initial_controls=None):
    camera = Picamera2()
    try:
        return start_camera(camera, initial_controls)
    except Exception:
        # The caller cannot close a camera that failed before being returned.
        camera.close()
        raise


def start_camera(camera, initial_controls):
    image_size = (config.FRAME_WIDTH, config.FRAME_HEIGHT)
    matrix, distortion = load_calibration(config.CAMERA_CALIBRATION_NPZ, image_size)
    camera.frame_undistorter = FullFrameUndistorter(
        matrix, distortion, image_size, alpha=config.UNDISTORT_ALPHA,
    )
    camera_config = camera.create_preview_configuration(
        main={
            "size": (config.FRAME_WIDTH, config.FRAME_HEIGHT),
            "format": "RGB888",
        },
        raw={"size": (config.RAW_WIDTH, config.RAW_HEIGHT)},
    )
    camera.configure(camera_config)

    requested_controls = normalize_camera_controls(initial_controls or {})
    # Focus is owned by V4L2; libcamera focus controls must not override it.
    requested_controls.pop("AfMode", None)
    requested_controls.pop("LensPosition", None)
    supported_controls = {
        name: value
        for name, value in requested_controls.items()
        if name in camera.camera_controls
    }
    if supported_controls:
        camera.set_controls(supported_controls)

    camera.start()

    device = find_vcm_device()
    minimum, maximum, step, _, _ = query_focus_control(device)
    requested, actual = set_focus(
        device, config.VCM_FOCUS_ABSOLUTE, minimum, maximum, step
    )
    # Drain frames captured while the actuator is settling.
    deadline = time.monotonic() + max(0.0, config.VCM_FOCUS_SETTLE_SECONDS)
    while time.monotonic() < deadline:
        camera.capture_array("main")
    print(f"VCM device：{device}；focus_absolute requested/actual：{requested}/{actual}")
    print(f"全畫面去畸變已啟用：{config.CAMERA_CALIBRATION_NPZ}")
    return camera


def capture_frames(camera):
    """Return (raw, rectified, roi); rectify BEFORE cropping the shared ROI.

    raw is the sensor frame before undistortion, kept for lossless captures.
    """
    raw = camera.capture_array("main")
    frame = camera.frame_undistorter.process(raw)
    roi = frame[
        config.ROI_Y1 : config.ROI_Y2,
        config.ROI_X1 : config.ROI_X2,
    ].copy()
    return raw, frame, roi


def capture_roi(camera):
    _, frame, roi = capture_frames(camera)
    return frame, roi


def close_camera(camera):
    camera.stop()
    camera.close()
