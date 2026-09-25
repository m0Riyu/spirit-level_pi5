"""Main capture, inference, display, and logging loop."""

import time

import config
from bubble_measurement import BubbleCalibration, BubbleMeasurement
from camera import capture_roi, close_camera, create_camera
from csv_logger import CsvLogger
from detector import YoloDetector
from display import close_windows, create_annotated_frame, show_frame
from telemetry_server import TelemetryServer, build_telemetry_payload


def print_metrics(frame_id, detection, measurement, timings):
    if frame_id % config.PRINT_EVERY != 0:
        return

    common = (
        f"capture={timings['capture_ms']:.1f}ms | "
        f"predict={timings['predict_ms']:.1f}ms | "
        f"process={timings['process_ms']:.1f}ms | "
        f"fps={timings['fps']:.1f}"
    )
    if detection.detected:
        measurement_text = (
            f"offset={measurement.offset_px:+.2f}px | "
            f"div={measurement.offset_div:+.3f} | "
            f"direction={measurement.direction} | "
            if measurement.valid
            else f"measurement={measurement.error} | "
        )
        print(
            f"frame={frame_id} | "
            f"conf={detection.confidence:.4f} | "
            f"count={detection.detection_count} | "
            f"center_x_roi={detection.center_x_roi:.2f}px | "
            f"{measurement_text}"
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
    calibration = None
    telemetry = None

    if config.ENABLE_BUBBLE_MEASUREMENT:
        try:
            calibration = BubbleCalibration.from_json(
                config.BUBBLE_CALIBRATION_PATH,
                expected_size=(config.ROI_WIDTH, config.ROI_HEIGHT),
            )
            print(
                "氣泡量測校正已載入："
                f"center={calibration.center_x_roi:.3f}px, "
                f"pitch={calibration.pitch_px_per_div:.3f}px/div"
            )
        except (OSError, KeyError, TypeError, ValueError) as error:
            print(f"警告：無法載入氣泡量測校正，僅執行YOLO：{error}")

    if config.ENABLE_WEBSOCKET:
        try:
            telemetry = TelemetryServer(
                websocket_host=config.WEBSOCKET_HOST,
                websocket_port=config.WEBSOCKET_PORT,
                dashboard_host=config.DASHBOARD_HOST,
                dashboard_port=config.DASHBOARD_PORT,
                dashboard_directory=config.APP_DIRECTORY / "dashboard",
            )
            telemetry.start()
            print("WebSocket遙測已啟動：")
            for url in telemetry.dashboard_urls():
                print(f"  {url}")
        except (OSError, RuntimeError) as error:
            telemetry = None
            print(f"警告：WebSocket遙測無法啟動，主程式繼續執行：{error}")

    print(f"CSV預定儲存位置：{logger.path.resolve()}")

    try:
        camera = create_camera()
        print("相機已啟動。")
        print("按q或Ctrl+C結束。")

        frame_id = 0
        while True:
            frame_id += 1
            frame_started_at_epoch_ms = time.time() * 1000.0
            loop_start = time.perf_counter()

            capture_start = time.perf_counter()
            _, bubble_roi = capture_roi(camera)
            capture_ms = (time.perf_counter() - capture_start) * 1000.0
            capture_completed_at_epoch_ms = time.time() * 1000.0

            prediction = detector.predict(bubble_roi)
            prediction_completed_at_epoch_ms = time.time() * 1000.0
            if calibration is None:
                measurement = BubbleMeasurement.disabled(
                    "calibration_unavailable"
                    if config.ENABLE_BUBBLE_MEASUREMENT
                    else "measurement_disabled"
                )
            else:
                measurement = calibration.measure(
                    prediction.detection.center_x_roi
                    if prediction.detection.detected
                    else None
                )

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

            logger.write(frame_id, prediction.detection, measurement, timings)
            print_metrics(frame_id, prediction.detection, measurement, timings)

            if (
                telemetry is not None
                and frame_id % config.TELEMETRY_SEND_EVERY == 0
            ):
                payload = build_telemetry_payload(
                    frame_id,
                    prediction.detection,
                    measurement,
                    timings,
                    mm_per_m_per_div=config.MM_PER_M_PER_DIV,
                    level_tolerance_mm_per_m=(
                        config.LEVEL_TOLERANCE_MM_PER_M
                    ),
                    max_measurable_slope_mm_per_m=(
                        config.MAX_MEASURABLE_SLOPE_MM_PER_M
                    ),
                )
                payload["frame_started_at_epoch_ms"] = frame_started_at_epoch_ms
                payload["capture_completed_at_epoch_ms"] = (
                    capture_completed_at_epoch_ms
                )
                payload["prediction_completed_at_epoch_ms"] = (
                    prediction_completed_at_epoch_ms
                )
                telemetry.publish(payload)

            if config.ENABLE_IMAGE_STREAM and show_frame(
                annotated_frame, prediction.detection, measurement, fps
            ):
                break

    except KeyboardInterrupt:
        print("\n收到Ctrl+C，停止紀錄。")
    finally:
        logger.close()
        if camera is not None:
            close_camera(camera)
        if telemetry is not None:
            telemetry.stop()
        close_windows()

        if ask_to_save_csv():
            saved_path = logger.save()
            print(f"CSV已儲存：{saved_path.resolve()}")
        else:
            logger.discard()
            print("已放棄此次CSV紀錄。")

        print("程式已結束。")
