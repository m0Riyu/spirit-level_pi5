"""Picamera2 setup and frame capture."""

from picamera2 import Picamera2
from libcamera import controls

import config
from camera_undistortion import FullFrameUndistorter, load_calibration


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
    image_size = (config.FRAME_WIDTH, config.FRAME_HEIGHT)
    matrix, distortion = load_calibration(config.CAMERA_CALIBRATION_NPZ, image_size)
    undistorter = FullFrameUndistorter(
        matrix, distortion, image_size, alpha=config.UNDISTORT_ALPHA,
    )
    camera = Picamera2()
    try:
        camera.frame_undistorter = undistorter
        return start_camera(camera, initial_controls)
    except Exception:
        camera.close()
        raise


def start_camera(camera, initial_controls):
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
    print(f"全畫面去畸變已啟用：{config.CAMERA_CALIBRATION_NPZ}")
    return camera


def capture_frame(camera):
    """Rectify the complete frame before ROI selection or thresholding."""
    return camera.frame_undistorter.process(camera.capture_array("main"))


def capture_roi(camera):
    """Return the rectified full frame together with its centered ROI."""
    frame = capture_frame(camera)
    roi = frame[
        config.ROI_Y1 : config.ROI_Y2,
        config.ROI_X1 : config.ROI_X2,
    ].copy()
    return frame, roi


def describe_image_geometry(camera):
    """Record the actual remap parameters alongside saved images and CSVs."""
    undistorter = camera.frame_undistorter
    return {
        "coordinate_system": "undistorted",
        "calibration_npz": str(config.CAMERA_CALIBRATION_NPZ.resolve()),
        "alpha": config.UNDISTORT_ALPHA,
        "frame_size": list(undistorter.image_size),
        "roi_bounds": [config.ROI_X1, config.ROI_Y1, config.ROI_X2, config.ROI_Y2],
        "original_camera_matrix": undistorter.original_camera_matrix.tolist(),
        "dist_coeffs": undistorter.distortion.tolist(),
        "new_camera_matrix": undistorter.camera_matrix.tolist(),
    }


def close_camera(camera):
    camera.stop()
    camera.close()
