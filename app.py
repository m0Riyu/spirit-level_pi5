"""Main capture, inference, display, and logging loop."""

import time

import config
from bubble_measurement import BubbleCalibration, BubbleMeasurement
from camera import capture_roi, close_camera, create_camera
from csv_logger import CsvLogger
from detector import YoloDetector
from display import close_windows, show_clean_frame as show_frame
from manual_capture import CaptureManager
from stability import StabilityTracker
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
    stability_tracker = StabilityTracker(
        config.STABILITY_WINDOW_SIZE, config.STABILITY_MIN_VALID_RATIO,
        config.STABILITY_MAX_STD_MM_PER_M, config.STABILITY_MAX_RANGE_MM_PER_M,
        config.STABILITY_HOLD_SECONDS,
    )
    logger = CsvLogger() if config.ENABLE_CONTINUOUS_CSV else None
    captures = CaptureManager()
    camera = None
    calibration = None
    telemetry = None

    if config.ENABLE_WEBSOCKET:
        try:
            telemetry = TelemetryServer(
                websocket_host=config.WEBSOCKET_HOST,
                websocket_port=config.WEBSOCKET_PORT,
                dashboard_host=config.DASHBOARD_HOST,
                dashboard_port=config.DASHBOARD_PORT,
                dashboard_directory=config.APP_DIRECTORY / "dashboard",
                capture_manager=captures,
            )
            telemetry.start()
            print("WebSocket遙測已啟動：")
            for url in telemetry.dashboard_urls():
                print(f"  {url}")
        except (OSError, RuntimeError) as error:
            telemetry = None
            print(f"警告：WebSocket遙測無法啟動，主程式繼續執行：{error}")

    if logger is not None:
        print(f"CSV預定儲存位置：{logger.path.resolve()}")
    print(f"手動拍攝紀錄：{captures.session_directory.resolve()}")

    try:
        camera = create_camera()
        print("相機已啟動。")
        if config.ENABLE_BUBBLE_MEASUREMENT:
            try:
                calibration = BubbleCalibration.from_json(
                    config.BUBBLE_CALIBRATION_PATH,
                    expected_size=(config.ROI_WIDTH, config.ROI_HEIGHT),
                    undistorter=camera.frame_undistorter,
                    roi_origin=(config.ROI_X1, config.ROI_Y1),
                )
                print(
                    "氣泡量測校正（去畸變座標）已載入："
                    f"center={calibration.center_x_roi:.3f}px, "
                    f"center pitch={calibration.pitch_px_per_div:.3f}px/div"
                )
            except (OSError, KeyError, TypeError, ValueError) as error:
                print(f"警告：無法載入氣泡量測校正，僅執行YOLO：{error}")
        print("按q或Ctrl+C結束。")
        captures.set_ready()

        frame_id = 0
        while True:
            frame_id += 1
            # Only requests already queued here can use this frame. A trigger
            # arriving during capture/YOLO must wait for the following frame.
            frame_requests = captures.begin_frame()
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
                    else None,
                    prediction.detection.center_y_roi
                    if prediction.detection.detected
                    else None,
                )

            plot_ms = 0.0

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

            payload = build_telemetry_payload(
                frame_id, prediction.detection, measurement, timings,
                mm_per_m_per_div=config.MM_PER_M_PER_DIV,
                level_tolerance_mm_per_m=config.LEVEL_TOLERANCE_MM_PER_M,
                max_measurable_slope_mm_per_m=config.MAX_MEASURABLE_SLOPE_MM_PER_M,
            )
            stability = stability_tracker.update(
                payload["measurement"]["valid"], payload["measurement"]["slope_mm_per_m"],
                payload["measurement"]["offset_div"], payload["confidence"],
            )
            # Include per-frame stability work in process/FPS metrics.
            timings["process_ms"] = (time.perf_counter() - loop_start) * 1000
            timings["fps"] = 1000 / timings["process_ms"] if timings["process_ms"] > 0 else 0.
            fps = timings["fps"]
            payload["performance"]["processing_ms"] = timings["process_ms"]
            payload["performance"]["fps"] = fps
            if logger is not None:
                logger.write(frame_id, prediction.detection, measurement, timings)
            print_metrics(frame_id, prediction.detection, measurement, timings)
            payload["stability"] = stability.as_dict()
            payload["capture"] = {"ready": captures.ready, "require_stable_for_capture": captures.require_stable}
            payload["frame_started_at_epoch_ms"] = frame_started_at_epoch_ms
            payload["capture_completed_at_epoch_ms"] = capture_completed_at_epoch_ms
            payload["prediction_completed_at_epoch_ms"] = prediction_completed_at_epoch_ms
            if frame_requests:
                captures.freeze_frame(
                    frame_requests, bubble_roi, frame_id, prediction.detection, measurement,
                    timings, payload, stability, frame_started_epoch_ms=frame_started_at_epoch_ms,
                    capture_completed_epoch_ms=capture_completed_at_epoch_ms,
                    prediction_completed_epoch_ms=prediction_completed_at_epoch_ms,
                    frame_completed_monotonic=time.monotonic(),
                )

            if (
                telemetry is not None
                and frame_id % config.TELEMETRY_SEND_EVERY == 0
            ):
                telemetry.publish(payload)

            if config.ENABLE_IMAGE_STREAM and show_frame(
                bubble_roi, prediction.detection, measurement, fps
            ):
                break

    except KeyboardInterrupt:
        print("\n收到Ctrl+C，停止紀錄。")
    finally:
        captures.set_ready(False)
        # A preview/network/camera cleanup failure must not strand a non-daemon
        # writer or lose already frozen captures.
        for label, callback in (
            ("CSV", logger.close if logger is not None else None),
            ("相機", (lambda: close_camera(camera)) if camera is not None else None),
            ("HTTP/WebSocket", telemetry.stop if telemetry is not None else None),
            ("拍攝 writer", captures.close), ("預覽", close_windows),
        ):
            if callback is not None:
                try:
                    callback()
                except Exception as error:
                    print(f"警告：{label}關閉失敗：{error}")

        if logger is not None and ask_to_save_csv():
            saved_path = logger.save()
            print(f"CSV已儲存：{saved_path.resolve()}")
        elif logger is not None:
            logger.discard()
            print("已放棄此次CSV紀錄。")

        print("程式已結束。")
