"""Picamera2 setup and frame capture, shared by the service and debug tools.

Every entry point opens the camera through create_camera(), so all of them use
the same V4L2 focus, undistortion and ROI. A per-process lock file makes a
second camera program fail fast instead of fighting over the sensor.
"""

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from picamera2 import Picamera2
from libcamera import controls

import config
from camera_undistortion import FullFrameUndistorter, load_calibration


# Focus is owned by V4L2 (AK7375); libcamera focus controls must not move it.
FOCUS_CONTROLS = ("AfMode", "LensPosition")
_process_lock = None


class CameraBusyError(RuntimeError):
    pass


def acquire_camera_lock(path=None):
    """Hold the camera lock for the rest of this process; idempotent."""
    global _process_lock
    if _process_lock is not None:
        return _process_lock
    path = Path(path or config.CAMERA_LOCK_PATH)
    file = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.seek(0)
        owner = file.read().strip() or "未知程式"
        file.close()
        raise CameraBusyError(f"相機正被其他程式使用（{owner}）。請先停止 levelsvc 或其他相機程式。") from None
    file.seek(0)
    file.truncate()
    file.write(f"pid={os.getpid()} {Path(sys.argv[0]).name}\n")
    file.flush()
    _process_lock = file
    return file


def without_focus_controls(camera_controls):
    return {name: value for name, value in (camera_controls or {}).items() if name not in FOCUS_CONTROLS}


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


def create_camera(initial_controls=None, *, focus=None):
    acquire_camera_lock()
    camera = Picamera2()
    try:
        return start_camera(camera, initial_controls, focus)
    except Exception:
        # The caller cannot close a camera that failed before being returned.
        camera.close()
        raise


def start_camera(camera, initial_controls, focus=None):
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

    requested_controls = normalize_camera_controls(without_focus_controls(initial_controls))
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
        device, config.VCM_FOCUS_ABSOLUTE if focus is None else focus, minimum, maximum, step
    )
    camera.focus_absolute = actual
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


def describe_image_geometry(camera):
    """Record the actual remap parameters alongside saved images and CSVs."""
    undistorter = camera.frame_undistorter
    return {
        "coordinate_system": "undistorted",
        "calibration_npz": str(Path(config.CAMERA_CALIBRATION_NPZ).resolve()),
        "alpha": config.UNDISTORT_ALPHA,
        "frame_size": list(undistorter.image_size),
        "roi_bounds": [config.ROI_X1, config.ROI_Y1, config.ROI_X2, config.ROI_Y2],
        "roi_origin": [config.ROI_X1, config.ROI_Y1],
        "original_camera_matrix": undistorter.original_camera_matrix.tolist(),
        "dist_coeffs": undistorter.distortion.reshape(-1).tolist(),
        "new_camera_matrix": undistorter.camera_matrix.tolist(),
        "focus_absolute": getattr(camera, "focus_absolute", None),
    }


def close_camera(camera):
    camera.stop()
    camera.close()
