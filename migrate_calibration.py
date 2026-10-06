"""One-time creation of the first versioned calibration files.

    python migrate_calibration.py geometry [--source TICK_JSON]
    python migrate_calibration.py geometry-images IMAGE... --stamp YYYYMMDDTHHMMSS [--activate]
    python migrate_calibration.py vial --summary ../calibration_test/output/summary.json [--fit NAME]

geometry:        legacy binary_*_tick_measurement.json -> degree-1 geometry whose
                 output equals the old (x - reference_midpoint_x) / pitch.
geometry-images: ③ fit from saved rectified ROI images (e.g. a LOG session),
                 stored inactive unless --activate.
vial:            gain and zero offset fitted by calibration_test/run_log_test.py
                 (--fit yolo_ends_session_geometry: P3 conversion, relative to
                 the session's own tick center; stored inactive unless --activate).
Existing files are never edited.
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import cv2

import config
from calibration_store import TIMEZONE, CalibrationStore, new_version
from camera_undistortion import FullFrameUndistorter, load_calibration
from geometry_calibration import fit_geometry, legacy_tick_geometry
from tick_detection import combine_frames, detect_ticks

LEGACY_TICK_MEASUREMENT = (
    config.PROJECT_DIRECTORY / "binary_stream_tuner_project" / "binary_captures"
    / "binary_20261002_072923_517806_tick_measurement.json"
)


def geometry_record(source, previous_version=None):
    source = Path(source).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    match = re.search(r"(\d{8})_(\d{6})", source.name)
    if match:
        stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=TIMEZONE)
    else:
        stamp = datetime.fromisoformat(data["created_utc"]).astimezone(TIMEZONE)
    return legacy_tick_geometry(
        data, version=new_version("geometry", stamp), created_at_iso=stamp.isoformat(timespec="seconds"),
        source_path=source, previous_version=previous_version,
    )


def offline_image_geometry(focus_absolute=None):
    """image_geometry for frames rectified with the configured camera calibration."""
    size = (config.FRAME_WIDTH, config.FRAME_HEIGHT)
    undistorter = FullFrameUndistorter(*load_calibration(config.CAMERA_CALIBRATION_NPZ, size), size,
                                       alpha=config.UNDISTORT_ALPHA)
    return {"coordinate_system": "undistorted", "calibration_npz": str(Path(config.CAMERA_CALIBRATION_NPZ).resolve()),
            "alpha": config.UNDISTORT_ALPHA, "frame_size": list(size), "roi_origin": [config.ROI_X1, config.ROI_Y1],
            "original_camera_matrix": undistorter.original_camera_matrix.tolist(),
            "dist_coeffs": undistorter.distortion.reshape(-1).tolist(),
            "new_camera_matrix": undistorter.camera_matrix.tolist(), "focus_absolute": focus_absolute}


def images_geometry_record(images, stamp, previous=None, focus_absolute=None):
    frames = []
    for path in images:
        roi = cv2.imread(str(path))
        if roi is None or roi.shape[:2] != (config.ROI_HEIGHT, config.ROI_WIDTH):
            raise ValueError(f"not a {config.ROI_WIDTH}x{config.ROI_HEIGHT} ROI image: {path}")
        prior = (previous.zero_x_roi(), previous.px_per_div(previous.zero_x_roi())) if previous else (None, None)
        frames.append(detect_ticks(roi, *prior))
    ticks, roll = combine_frames(frames)
    stamp = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=TIMEZONE)
    record = fit_geometry(
        ticks, degree=config.GEOMETRY_POLYNOMIAL_DEGREE, version=new_version("geometry", stamp),
        created_at_iso=stamp.isoformat(timespec="seconds"), frames_used=len(frames), roll_deg=roll,
        focus_absolute=focus_absolute, image_geometry=offline_image_geometry(focus_absolute), previous=previous,
        expected_ticks=2 * config.TICK_EXPECTED_PER_SIDE, max_residual_rms_px=config.TICK_MAX_RESIDUAL_RMS_PX,
        max_pitch_change=config.TICK_MAX_PITCH_CHANGE)
    record["source_images"] = [str(Path(path).resolve()) for path in images]
    return record


def vial_record(summary_path, previous_version=None, now=None, fit_name="yolo", store=None):
    summary_path = Path(summary_path).resolve()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    fit = summary["calibration_all_points"][fit_name]
    now = now or datetime.now(TIMEZONE)
    method = ("YOLO box ends with the geometry re-measured from the session's own ticks"
              if fit_name == "yolo_ends_session_geometry" else "YOLO box center, degree-1 geometry")
    cross_validation = summary["method_E_session_geometry_ends_cv" if fit_name == "yolo_ends_session_geometry"
                               else "method_B_yolo_calibrated_cv"]
    return {
        "schema_version": 1,
        "version": store.new_version("vial", now) if store else new_version("vial", now),
        "created_at_iso": now.isoformat(timespec="seconds"),
        "mm_per_m_per_div": fit["mm_per_m_per_div"],
        "zero_offset_div": fit["zero_offset_div"],
        "fit_method": "least squares: reference_mm_per_m = mm_per_m_per_div * (offset_div - zero_offset_div); "
                      f"offset_div from {method}",
        "fit_name": fit_name,
        "reference_instrument": "LEVELNIC DL-S4W (CNC A axis, 1:1)",
        "source_session": summary["session"],
        "source_summary": str(summary_path),
        "labeled_captures": summary["labeled_captures"],
        "reference_angles": summary["reference_angles"],
        "cross_validation_leave_one_angle_out": cross_validation,
        "geometry_basis": summary.get("session_geometry", {}).get("fits", {}).get(
            str(config.GEOMETRY_POLYNOMIAL_DEGREE)) if fit_name == "yolo_ends_session_geometry" else None,
        "nominal_system_error": summary["method_A_current_system"],
        "previous_version": previous_version,
    }


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("geometry", "geometry-images", "vial"))
    parser.add_argument("images", nargs="*", help="ROI images for geometry-images")
    parser.add_argument("--source", default=LEGACY_TICK_MEASUREMENT, help="legacy tick measurement JSON")
    parser.add_argument("--summary", help="run_log_test.py output/summary.json")
    parser.add_argument("--fit", default="yolo", help="calibration_all_points entry of the summary")
    parser.add_argument("--stamp", help="version time for geometry-images, e.g. 20261002T145946")
    parser.add_argument("--focus", type=int, help="focus_absolute the images were taken with")
    parser.add_argument("--activate", action="store_true", help="activate geometry-images / ends-fit vial")
    parser.add_argument("--root", default=config.CALIBRATION_DIRECTORY, help="calibration directory")
    options = parser.parse_args(arguments)
    store = CalibrationStore(options.root)
    kind = "geometry" if options.kind.startswith("geometry") else "vial"
    previous = store.active_state(kind)["version"]
    activate = True
    if options.kind == "geometry":
        record = geometry_record(options.source, previous)
    elif options.kind == "geometry-images":
        if not options.images or not options.stamp:
            parser.error("geometry-images needs IMAGE... and --stamp")
        active = store.load_geometry() if previous else None
        record = images_geometry_record(options.images, options.stamp, active, options.focus)
        if not record["checks"]["passed"]:
            parser.error(f"tick checks failed: {record['checks']}")
        activate = options.activate
    else:
        if not options.summary:
            parser.error("vial needs --summary")
        record = vial_record(options.summary, previous, fit_name=options.fit, store=store)
        activate = options.fit == "yolo" or options.activate
    path = store.save(kind, record, activate=activate)
    print(f"已建立{'並啟用' if activate else '（未啟用）'} {kind} 版本 {record['version']}：{path}")
    return path


if __name__ == "__main__":
    main()
