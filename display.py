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
