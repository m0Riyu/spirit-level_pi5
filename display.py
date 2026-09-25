"""OpenCV annotation and preview-window handling."""

import time

import cv2

import config


def create_annotated_frame(result):
    plot_start = time.perf_counter()
    annotated_frame = result.plot()
    plot_ms = (time.perf_counter() - plot_start) * 1000.0
    return annotated_frame, plot_ms


def show_frame(annotated_frame, detection, fps):
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
    cv2.imshow(config.WINDOW_TITLE, annotated_frame)
    return cv2.waitKey(1) & 0xFF == ord("q")


def close_windows():
    cv2.destroyAllWindows()
