"""Main capture, inference, display, and logging loop."""

import time

import config
from camera import capture_roi, close_camera, create_camera
from csv_logger import CsvLogger
from detector import YoloDetector
from display import close_windows, create_annotated_frame, show_frame


def print_metrics(frame_id, detection, timings):
    if frame_id % config.PRINT_EVERY != 0:
        return

    common = (
        f"capture={timings['capture_ms']:.1f}ms | "
        f"predict={timings['predict_ms']:.1f}ms | "
        f"process={timings['process_ms']:.1f}ms | "
        f"fps={timings['fps']:.1f}"
    )
    if detection.detected:
        print(
            f"frame={frame_id} | "
            f"conf={detection.confidence:.4f} | "
            f"count={detection.detection_count} | "
            f"center_x={detection.center_x_full:.2f}px | "
            f"{common}"
        )
    else:
        print(f"frame={frame_id} | 未偵測 | {common}")


def ask_to_save_csv():
    """Ask until a valid answer is entered; default to save for data safety."""
    while True:
        try:
            answer = input("是否要儲存此次 CSV 紀錄？[Y/n]：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n無法取得輸入，將保留 CSV 紀錄。")
            return True

        if answer in ("", "y", "yes", "是"):
            return True
        if answer in ("n", "no", "否"):
            return False
        print("請輸入 y（儲存）或 n（不儲存）。")


def run():
    detector = YoloDetector()
    logger = CsvLogger()
    camera = None

    print(f"CSV預定儲存位置：{logger.path.resolve()}")

    try:
        camera = create_camera()
        print("相機已啟動。")
        print("按q或Ctrl+C結束。")

        frame_id = 0
        while True:
            frame_id += 1
            loop_start = time.perf_counter()

            capture_start = time.perf_counter()
            _, bubble_roi = capture_roi(camera)
            capture_ms = (time.perf_counter() - capture_start) * 1000.0

            prediction = detector.predict(bubble_roi)

            annotated_frame = None
            plot_ms = 0.0
            if config.ENABLE_IMAGE_STREAM:
                annotated_frame, plot_ms = create_annotated_frame(prediction.result)

            process_ms = (time.perf_counter() - loop_start) * 1000.0
            fps = 1000.0 / process_ms if process_ms > 0 else 0.0
            timings = {
                "capture_ms": capture_ms,
                "predict_ms": prediction.predict_ms,
                "yolo_preprocess_ms": prediction.preprocess_ms,
                "yolo_inference_ms": prediction.inference_ms,
                "yolo_postprocess_ms": prediction.postprocess_ms,
                "plot_ms": plot_ms,
                "process_ms": process_ms,
                "fps": fps,
            }

            logger.write(frame_id, prediction.detection, timings)
            print_metrics(frame_id, prediction.detection, timings)

            if config.ENABLE_IMAGE_STREAM and show_frame(
                annotated_frame, prediction.detection, fps
            ):
                break

    except KeyboardInterrupt:
        print("\n收到Ctrl+C，停止紀錄。")
    finally:
        logger.close()
        if camera is not None:
            close_camera(camera)
        close_windows()

        if ask_to_save_csv():
            saved_path = logger.save()
            print(f"CSV已儲存：{saved_path.resolve()}")
        else:
            logger.discard()
            print("已放棄此次CSV紀錄。")

        print("程式已結束。")
