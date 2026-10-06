"""Independent Pi version of JetsonNano_PTZ/test_apriltag_only.py."""

import argparse
import csv
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

import apriltag_config as config
from camera_undistortion import FullFrameUndistorter, load_calibration


FAMILIES = {
    "tag16h5": "DICT_APRILTAG_16h5", "tag25h9": "DICT_APRILTAG_25h9",
    "tag36h10": "DICT_APRILTAG_36h10", "tag36h11": "DICT_APRILTAG_36h11",
}
CSV_FIELDS = (
    "timestamp_epoch_ms", "frame_id", "detected", "pose_valid", "tag_id",
    "center_x_px", "center_y_px", "corners_px", "pitch_deg", "yaw_deg",
    "roll_deg", "distance_mm", "x_mm", "y_mm", "z_mm", "reprojection_rmse_px",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=FAMILIES, default=config.TAG_FAMILY)
    parser.add_argument("--tag-size-mm", type=float, default=config.TAG_SIZE_METER * 1000)
    parser.add_argument("--calibration", type=Path, default=config.CALIBRATION_NPZ)
    parser.add_argument("--focus", type=int, default=config.VCM_FOCUS_ABSOLUTE)
    parser.add_argument("--image", type=Path, help="Measure a full-size saved image without opening the camera")
    parser.add_argument("--headless", action="store_true", help="No GUI windows")
    parser.add_argument("--frames", type=int, default=0, help="Stop after N frames; 0 means unlimited")
    parser.add_argument("--csv", type=Path, help="Write a new CSV file; do not overwrite an existing file")
    args = parser.parse_args(argv)
    if not math.isfinite(args.tag_size_mm) or args.tag_size_mm <= 0 or args.frames < 0:
        parser.error("Tag size must be finite and positive; frames must be nonnegative.")
    if not hasattr(cv2, "aruco"):
        parser.error("OpenCV must include cv2.aruco (opencv-contrib-python).")
    return args


def create_detector(family):
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, FAMILIES[family]))
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(dictionary, parameters)


def pose_angles(rotation):
    """Jetson labels: Pitch about X, Yaw about Y, Roll about Z (degrees)."""
    return {
        "pitch_deg": math.degrees(math.atan2(rotation[2, 1], rotation[2, 2])),
        "yaw_deg": math.degrees(math.atan2(-rotation[2, 0], math.hypot(rotation[2, 1], rotation[2, 2]))),
        "roll_deg": math.degrees(math.atan2(rotation[1, 0], rotation[0, 0])),
    }


def estimate_tag_pose(corners, camera_matrix, tag_size_m, rotation_correction_deg=0):
    """Estimate on undistorted pixels with NEW K and zero distortion."""
    half = tag_size_m / 2
    objects = np.array([[-half, half, 0], [half, half, 0],
                        [half, -half, 0], [-half, -half, 0]], dtype=np.float64)
    pixels = np.asarray(corners, dtype=np.float64).reshape(4, 2)
    count, rotations, translations, _ = cv2.solvePnPGeneric(
        objects, pixels, camera_matrix, np.zeros(5), flags=cv2.SOLVEPNP_IPPE_SQUARE,
    )
    candidates = []
    for rvec, tvec in zip(rotations, translations):
        if not np.isfinite(rvec).all() or not np.isfinite(tvec).all():
            continue
        rotation, _ = cv2.Rodrigues(rvec)
        transformed = (rotation @ objects.T).T + tvec.reshape(1, 3)
        if np.any(transformed[:, 2] <= 0):
            continue
        projected, _ = cv2.projectPoints(objects, rvec, tvec, camera_matrix, np.zeros(5))
        residual = projected.reshape(4, 2) - pixels
        error = float(np.sqrt(np.mean(np.sum(residual ** 2, axis=1))))
        candidates.append((error, rotation, tvec))
    if not count or not candidates:
        return None
    error, opencv_rotation, tvec = min(candidates, key=lambda candidate: candidate[0])
    # OpenCV's square frame is X-right/Y-up/Z-out. The Jetson apriltag
    # frame is X-right/Y-down/Z-in; convert before its Euler formulas.
    detected_rotation = opencv_rotation @ np.diag([1., -1., -1.])
    theta = math.radians(rotation_correction_deg)
    c, s = math.cos(theta), math.sin(theta)
    correction = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.]])
    rotation = detected_rotation @ correction
    xyz_mm = tvec.reshape(3) * 1000
    return {
        "pose_valid": True, **pose_angles(rotation),
        "distance_mm": float(np.linalg.norm(xyz_mm)),
        "x_mm": float(xyz_mm[0]), "y_mm": float(xyz_mm[1]), "z_mm": float(xyz_mm[2]),
        "reprojection_rmse_px": error, "rotation": rotation, "tvec_m": tvec,
    }


def measure_frame(frame, detector, camera_matrix, tag_size_m,
                  default_correction=None, corrections=None):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = detector.detectMarkers(gray)  # Full frame, no slicing.
    if ids is None:
        return []
    if default_correction is None:
        default_correction = config.POSE_ROTATION_CORRECTION_DEG
    if corrections is None:
        corrections = config.TAG_ROTATION_CORRECTIONS_DEG
    measurements = []
    for tag_id, corners_for_tag in zip(ids.reshape(-1), corners):
        tag_id = int(tag_id)
        points = corners_for_tag.reshape(4, 2)
        homogeneous = np.column_stack((points, np.ones(4)))
        intersection = np.cross(np.cross(homogeneous[0], homogeneous[2]),
                                np.cross(homogeneous[1], homogeneous[3]))
        center = intersection[:2] / intersection[2] if abs(intersection[2]) > 1e-12 else points.mean(axis=0)
        result = {
            "tag_id": tag_id, "center_x_px": float(center[0]), "center_y_px": float(center[1]),
            "corners_px": points.tolist(), "pose_valid": False,
        }
        correction = float(corrections.get(tag_id, default_correction))
        if not math.isfinite(correction):
            raise ValueError(f"Tag {tag_id}: rotation correction must be finite.")
        pose = estimate_tag_pose(points, camera_matrix, tag_size_m, correction)
        if pose is not None:
            result.update(pose)
        measurements.append(result)
    return sorted(measurements, key=lambda result: result["tag_id"])


def circular_mean_degrees(values):
    if not values:
        return None
    angles = np.radians(values)
    sine, cosine = float(np.sin(angles).mean()), float(np.cos(angles).mean())
    if math.hypot(sine, cosine) < 1e-10:
        return None
    return math.degrees(math.atan2(sine, cosine))


def summarize(measurements):
    valid = [result for result in measurements if result["pose_valid"]]
    result = {"tag_count": len(measurements), "pose_count": len(valid)}
    for name in ("pitch_deg", "yaw_deg", "roll_deg"):
        result[name] = circular_mean_degrees([item[name] for item in valid])
    result["distance_mm"] = float(np.mean([item["distance_mm"] for item in valid])) if valid else None
    return result


class PixelRuler:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.points = []

    def mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and 0 <= x < self.width and 0 <= y < self.height:
            if len(self.points) == 2:
                self.points.clear()
            self.points.append((x, y))
            print(f"Pixel ruler point: ({x}, {y})")

    @property
    def distance_x(self):
        return abs(self.points[0][0] - self.points[1][0]) if len(self.points) == 2 else None


def draw_3d_axis(frame, camera_matrix, tag_size_m, pose):
    # Jetson draws -Z outward from the face of the tag.
    points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, -1]], dtype=np.float64) * tag_size_m
    rvec, _ = cv2.Rodrigues(pose["rotation"])
    pixels, _ = cv2.projectPoints(points, rvec, pose["tvec_m"], camera_matrix, np.zeros(5))
    pixels = pixels.reshape(-1, 2)
    if not np.isfinite(pixels).all():
        return
    pixels = np.clip(np.rint(pixels), -1000000, 1000000).astype(int)
    origin = tuple(pixels[0])
    for point, color in zip(pixels[1:], ((0, 0, 255), (0, 255, 0), (255, 0, 0))):
        cv2.line(frame, origin, tuple(point), color, 2)


def build_canvas(frame, measurements, camera_matrix, tag_size_m, ruler):
    height, width = frame.shape[:2]
    canvas = np.zeros((height, width + config.SIDEBAR_WIDTH, 3), dtype=np.uint8)
    canvas[:, :width] = frame
    view = canvas[:, :width]  # Entire camera image, not a detection ROI.
    cv2.line(view, (width // 2, 0), (width // 2, height - 1), (80, 80, 80), 1)
    cv2.line(view, (0, height // 2), (width - 1, height // 2), (80, 80, 80), 1)
    for result in measurements:
        points = np.asarray(result["corners_px"], dtype=np.int32)
        cv2.polylines(view, [points], True, (0, 255, 0), 1)
        center = (round(result["center_x_px"]), round(result["center_y_px"]))
        cv2.circle(view, center, 2, (0, 255, 255), -1)
        cv2.putText(view, str(result["tag_id"]), center, cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        if result["pose_valid"]:
            draw_3d_axis(view, camera_matrix, tag_size_m, result)
    for point in ruler.points:
        cv2.circle(view, point, 3, (0, 0, 255), -1)
    if len(ruler.points) == 2:
        cv2.line(view, ruler.points[0], ruler.points[1], (0, 255, 255), 1)

    text_x = width + 20
    font = cv2.FONT_HERSHEY_SIMPLEX

    def text(value, y, color=(255, 255, 255), scale=0.6):
        cv2.putText(canvas, value, (text_x, y), font, scale, color, 1, cv2.LINE_AA)

    cv2.line(canvas, (width, 0), (width, height - 1), (200, 200, 200), 1)
    text("1. PIXEL RULER", 30, (0, 255, 255))
    text(f"Dist X: {ruler.distance_x} px" if ruler.distance_x is not None else "Click two image points", 60)
    cv2.line(canvas, (text_x, 80), (canvas.shape[1] - 10, 80), (100, 100, 100), 1)
    text("2. MEAN TAG POSE", 110, (0, 255, 255))
    summary = summarize(measurements)
    if summary["pose_count"]:
        for label, key, y in (("Pitch", "pitch_deg", 140), ("Yaw", "yaw_deg", 170), ("Roll", "roll_deg", 200)):
            value = summary[key]
            color = (255, 255, 255)
            if key == "roll_deg" and value is not None:
                color = (0, 255, 0) if abs(value) < config.ROLL_LEVEL_TOLERANCE_DEG else (0, 0, 255)
            text(f"{label}: {value:.1f} deg" if value is not None else f"{label}: ambiguous", y, color)
        text(f"Dist : {summary['distance_mm']:.1f} mm", 240, (255, 255, 0))
    else:
        text("No valid Tag pose", 140, (0, 0, 255))
    text(f"Tags: {summary['tag_count']} / poses: {summary['pose_count']}", 280)
    text(f"Tag side: {tag_size_m * 1000:.2f} mm", 310)
    text("3. INDIVIDUAL TAGS", 345, (0, 255, 255))
    for index, result in enumerate(measurements):
        y = 375 + index * 25
        if y > height - 45:
            break
        label = f"ID {result['tag_id']}: no valid pose"
        if result["pose_valid"]:
            label = f"ID {result['tag_id']}: {result['distance_mm']:.1f}mm err={result['reprojection_rmse_px']:.2f}px"
        text(label, y, scale=0.45)
    text("Undistorted full frame | Q: quit", height - 15, (180, 180, 180), scale=0.45)
    return canvas


def set_vcm_focus(value):
    for name_path in sorted(Path("/sys/class/video4linux").glob("v4l-subdev*/name")):
        try:
            if "ak7375" not in name_path.read_text().lower():
                continue
        except OSError:
            continue
        device = Path("/dev") / name_path.parent.name
        break
    else:
        raise RuntimeError("Cannot find the AK7375 V4L2 focus device.")

    def control(*arguments):
        result = subprocess.run(["v4l2-ctl", "-d", str(device), *arguments],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout

    match = re.search(r"focus_absolute[^:]*:\s*min=(-?\d+)\s+max=(-?\d+)\s+step=(\d+)",
                      control("--list-ctrls"))
    if not match:
        raise RuntimeError("Device has no focus_absolute control.")
    minimum, maximum, step = map(int, match.groups())
    requested = max(minimum, min(maximum, value))
    if step > 1:
        requested = minimum + round((requested - minimum) / step) * step
        requested = max(minimum, min(maximum, requested))
    readback = control("--set-ctrl", f"focus_absolute={requested}", "--get-ctrl", "focus_absolute")
    match = re.search(r"focus_absolute:\s*(-?\d+)", readback)
    if not match or int(match.group(1)) != requested:
        raise RuntimeError("Could not verify the requested focus value.")
    print(f"VCM {device}: focus_absolute={requested}")


def create_camera(focus):
    from picamera2 import Picamera2

    camera = Picamera2()
    try:
        settings = camera.create_preview_configuration(
            main={"size": (config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT), "format": "RGB888"},
            raw={"size": (config.RAW_WIDTH, config.RAW_HEIGHT)},
        )
        camera.configure(settings)
        camera.start()
        set_vcm_focus(focus)
        deadline = time.monotonic() + config.VCM_SETTLE_SECONDS
        while time.monotonic() < deadline:
            camera.capture_array("main")
        return camera
    except BaseException:
        camera.close()
        raise


def serializable_measurements(measurements):
    return [{key: value for key, value in result.items() if key not in ("rotation", "tvec_m")}
            for result in measurements]


def run(args):
    image_size = (config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT)
    matrix, distortion = load_calibration(args.calibration, image_size)
    undistorter = FullFrameUndistorter(matrix, distortion, image_size, config.UNDISTORT_ALPHA)
    detector = create_detector(args.family)
    tag_size_m = args.tag_size_mm / 1000
    ruler = PixelRuler(*image_size)
    image = None
    if args.image is not None:
        image = cv2.imread(str(args.image))
        if image is None:
            raise ValueError(f"Cannot read image: {args.image}")
        if (image.shape[1], image.shape[0]) != image_size:
            raise ValueError(f"Image must be the full calibrated size {image_size}.")
    print(f"AprilTag {args.family}; side={args.tag_size_mm:g} mm; full-frame undistortion={image_size}")
    print(f"Calibration: {args.calibration}")
    print(f"Pose correction: {config.POSE_ROTATION_CORRECTION_DEG:g} deg; overrides={config.TAG_ROTATION_CORRECTIONS_DEG}")
    camera = None
    csv_file = None
    next_print = 0.0
    frame_id = 0
    try:
        if args.csv is not None:
            csv_file = args.csv.open("x", encoding="utf-8-sig", newline="")
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
        if not args.headless:
            os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
            fonts = Path("/usr/share/fonts/truetype/dejavu")
            if fonts.is_dir():
                os.environ["QT_QPA_FONTDIR"] = str(fonts)
            cv2.namedWindow(config.WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.setMouseCallback(config.WINDOW_NAME, ruler.mouse_callback)
            if config.SHOW_RAW_WINDOW:
                cv2.namedWindow(config.RAW_WINDOW_NAME, cv2.WINDOW_NORMAL)
        if image is None:
            camera = create_camera(args.focus)
        while True:
            raw_frame = image if image is not None else camera.capture_array("main")
            clean_frame = undistorter.process(raw_frame)
            frame_id += 1
            timestamp = time.time() * 1000
            measurements = measure_frame(clean_frame, detector, undistorter.camera_matrix, tag_size_m)
            public = serializable_measurements(measurements)
            if csv_file is not None:
                for result in public or [{}]:
                    row = dict(result)
                    if "corners_px" in row:
                        row["corners_px"] = json.dumps(row["corners_px"], separators=(",", ":"))
                    writer.writerow({"timestamp_epoch_ms": timestamp, "frame_id": frame_id,
                                     "detected": int(bool(result)), **row})
                if frame_id % 30 == 0:
                    csv_file.flush()
            if time.monotonic() >= next_print:
                print(f"frame={frame_id}: {json.dumps(summarize(measurements))}", flush=True)
                print(f"tags: {json.dumps(public)}", flush=True)
                next_print = time.monotonic() + config.PRINT_INTERVAL_SECONDS
            if not args.headless:
                canvas = build_canvas(clean_frame, measurements, undistorter.camera_matrix, tag_size_m, ruler)
                cv2.imshow(config.WINDOW_NAME, canvas)
                if config.SHOW_RAW_WINDOW:
                    cv2.imshow(config.RAW_WINDOW_NAME, raw_frame)
                if cv2.waitKey(0 if image is not None else 1) & 0xFF in (27, ord("q"), ord("Q")):
                    break
            if image is not None or (args.frames and frame_id >= args.frames):
                break
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if camera is not None:
            camera.close()
        if csv_file is not None:
            csv_file.close()
        if not args.headless:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    try:
        run(parse_args())
    except (OSError, RuntimeError, ValueError, KeyError, cv2.error, subprocess.SubprocessError) as error:
        raise SystemExit(f"AprilTag measurement failed: {error}") from error
