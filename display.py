"""OpenCV annotation and preview-window handling."""

import time

import cv2

import config


def create_annotated_frame(result):
    plot_start = time.perf_counter()
    annotated_frame = result.plot()
    plot_ms = (time.perf_counter() - plot_start) * 1000.0
    return annotated_frame, plot_ms


def show_frame(annotated_frame, detection, measurement, fps):
    if detection.detected:
        confidence_text = f"Conf: {detection.confidence:.3f}"
    else:
        confidence_text = "No detection"

    cv2.putText(
        annotated_frame,
        confidence_text,
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2,
    )
    cv2.putText(
        annotated_frame,
        f"FPS: {fps:.1f}",
        (10, 60),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2,
    )
    if measurement.valid:
        offset_text = (
            f"Offset: {measurement.offset_div:+.3f} div "
            f"({measurement.offset_px:+.2f} px)"
        )
        color = (0, 255, 0)
    else:
        offset_text = "Offset: unavailable"
        color = (0, 0, 255)
    cv2.putText(
        annotated_frame,
        offset_text,
        (10, 90),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        color,
        2,
    )

    if measurement.scale_center_x_roi not in (None, ""):
        center_x = int(round(float(measurement.scale_center_x_roi)))
        cv2.line(
            annotated_frame,
            (center_x, 0),
            (center_x, annotated_frame.shape[0] - 1),
            (255, 0, 255),
            1,
        )
    cv2.imshow(config.WINDOW_TITLE, annotated_frame)
    return cv2.waitKey(1) & 0xFF == ord("q")


def close_windows():
    cv2.destroyAllWindows()


def show_clean_frame(frame, detection, measurement, fps):
    """Optional local preview without creating/painting an annotated image."""
    cv2.imshow(config.WINDOW_TITLE, frame)
    return cv2.waitKey(1) & 0xFF == ord("q")


def annotate_capture_roi(clean_roi, row):
    """Annotate only a triggered snapshot; never touch the clean input.

    Picamera2 RGB888's byte array is BGR on this Pi, as consumed by the existing
    OpenCV/Ultralytics pipeline. Keep that order for cv2.imencode (no RGB swap).
    """
    from stability import finite_number

    image = clean_roi.copy()
    height, width = image.shape[:2]
    # Short three-line band; keep the middle of the tube unobstructed.
    band_height = 43
    image[:band_height] = (image[:band_height].astype("float32") * .35).astype("uint8")

    def text_value(key, digits=3):
        number = finite_number(row.get(key))
        return "NA" if number is None else f"{number:+.{digits}f}"

    lines = [
        f"{row['record_id']}  Frame {row['frame_id']}",
        f"Conf {text_value('confidence', 3)}  Offset {text_value('bubble_offset_px', 2)} px / {text_value('bubble_offset_div')} div",
        f"Slope {text_value('slope_mm_per_m', 5)} mm/m  Angle {text_value('angle_degrees', 6)} deg  {row['system_state']} / {row['stability_state']}",
    ]
    for index, line in enumerate(lines):
        scale = .36
        text_width = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0]
        if text_width > width - 10:
            scale *= (width - 10) / text_width
        cv2.putText(image, line, (5, 12 + 14 * index), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (255, 255, 255), 1, cv2.LINE_AA)
    scale_center = finite_number(row.get("scale_center_x_roi"))
    if scale_center is not None:
        x = max(0, min(width - 1, round(scale_center)))
        cv2.line(image, (x, band_height), (x, height - 1), (255, 0, 255), 1)
    if row.get("detected"):
        coordinates = [finite_number(row.get(name)) for name in ("x1_roi", "y1_roi", "x2_roi", "y2_roi")]
        if all(value is not None for value in coordinates):
            x1, y1, x2, y2 = [round(value) for value in coordinates]
            cv2.rectangle(image, (x1, y1), (x2, y2), (255, 160, 0), 1)
        center = [finite_number(row.get(name)) for name in ("center_x_roi", "center_y_roi")]
        if all(value is not None for value in center):
            cv2.circle(image, tuple(round(value) for value in center), 3, (0, 255, 255), -1)
    return image
