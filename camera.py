"""Picamera2 setup and frame capture."""

from picamera2 import Picamera2
from libcamera import controls

import config


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
    camera_config = camera.create_preview_configuration(
        main={
            "size": (config.FRAME_WIDTH, config.FRAME_HEIGHT),
            "format": "RGB888",
        },
        raw={"size": (config.RAW_WIDTH, config.RAW_HEIGHT)},
    )
    camera.configure(camera_config)

    requested_controls = {
        "AfMode": controls.AfModeEnum.Manual,
        "LensPosition": config.LENS_POSITION,
    }
    if initial_controls:
        requested_controls.update(normalize_camera_controls(initial_controls))
    supported_controls = {
        name: value
        for name, value in requested_controls.items()
        if name in camera.camera_controls
    }
    if supported_controls:
        camera.set_controls(supported_controls)

    camera.start()
    return camera


def capture_roi(camera):
    """Capture a full frame and return it together with the centered ROI."""
    frame = camera.capture_array("main")
    roi = frame[
        config.ROI_Y1 : config.ROI_Y2,
        config.ROI_X1 : config.ROI_X2,
    ].copy()
    return frame, roi


def close_camera(camera):
    camera.stop()
    camera.close()
