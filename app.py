"""levelsvc: one service, one camera, one processor per mode.

CameraService opens the camera once; ModeManager hands each frame to the
active processor (① measure, ② align, ③ ticks); the web server exposes
telemetry, captures, /api/state and /api/mode.
"""

import config
from bubble_measurement import check_undistorted_geometry
from calibration_store import CalibrationStore
from camera import create_camera
from camera_service import CameraService
from csv_logger import CsvLogger
from detector import YoloDetector
from display import close_windows
from manual_capture import CaptureManager
from mode_manager import ModeManager
from processors.align import AlignProcessor
from processors.measure import MeasureProcessor
from processors.ticks import TickProcessor
from system_controller import system_summary
from telemetry_server import TelemetryServer


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


def calibration_info(store, geometry, vial):
    """Versions and vial constants recorded in telemetry and every LOG row."""
    state = store.summary()
    return {
        "geometry_version": geometry.version,
        "geometry_pending_confirmation": state["geometry"]["pending_confirmation"],
        "vial_version": vial.version,
        "alignment_version": state["alignment"]["version"] or "",
        "mm_per_m_per_div": vial.mm_per_m_per_div,
        "zero_offset_div": vial.zero_offset_div,
    }


def calibration_status(store, measure):
    status = "unavailable" if measure.geometry is None else (
        "pending" if measure.calibration.get("geometry_pending_confirmation") else "ok")
    return {"status": status, **store.summary()}


def run():
    detector = YoloDetector()
    logger = CsvLogger() if config.ENABLE_CONTINUOUS_CSV else None
    store = CalibrationStore()
    geometry = vial = None
    calibration = {}
    if config.ENABLE_BUBBLE_MEASUREMENT:
        try:
            geometry, vial = store.load_geometry(), store.load_vial()
            calibration = calibration_info(store, geometry, vial)
        except (OSError, KeyError, TypeError, ValueError) as error:
            geometry = vial = None
            print(f"警告：無法載入校正檔（{store.root}），僅執行YOLO：{error}")
    captures = CaptureManager(calibration={
        **calibration, "geometry_source": geometry.source_path if geometry else "",
        "vial_source": vial.source_path if vial else ""})
    camera = CameraService(create_camera)
    telemetry = None

    def publish(payload):
        if telemetry is not None:
            telemetry.publish(payload)

    measure = MeasureProcessor(detector=detector, captures=captures, publish=publish, logger=logger)
    manager = ModeManager(
        {"measure": measure, "align": AlignProcessor(publish=publish), "ticks": TickProcessor(publish=publish)},
        on_change=lambda mode_state: publish({"type": "state", "schema_version": 1, **mode_state}),
    )

    def service_state():
        return {"type": "state", "schema_version": 1, **manager.state(),
                "calibration": calibration_status(store, measure),
                "camera": {"open": camera.camera is not None, "open_count": camera.open_count,
                           "frame_id": camera.frame_id,
                           "focus_absolute": getattr(camera.camera, "focus_absolute", None)},
                "system": system_summary()}

    if config.ENABLE_WEBSOCKET:
        try:
            telemetry = TelemetryServer(
                websocket_host=config.WEBSOCKET_HOST,
                websocket_port=config.WEBSOCKET_PORT,
                dashboard_host=config.DASHBOARD_HOST,
                dashboard_port=config.DASHBOARD_PORT,
                dashboard_directory=config.APP_DIRECTORY / "dashboard",
                capture_manager=captures,
                state_provider=service_state,
                mode_handler=manager.request,
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
        camera.open()
        print("相機已啟動。")
        if geometry is not None:
            try:
                check_undistorted_geometry(geometry.image_geometry, camera.undistorter,
                                           (config.ROI_X1, config.ROI_Y1))
                scale_center = geometry.zero_x_roi()
                print(
                    f"幾何校正 {geometry.version}（{geometry.polynomial_degree} 次）："
                    f"center={scale_center:.3f}px, pitch={geometry.px_per_div(scale_center):.3f}px/div；"
                    f"水平儀校正 {vial.version}：{vial.mm_per_m_per_div:.5f} mm/m/div，"
                    f"零點 {vial.zero_offset_div:+.3f} div"
                )
                if calibration["geometry_pending_confirmation"]:
                    print("注意：幾何校正待確認（相機可能動過），請重做刻度檢查。")
                measure.set_calibration(geometry, vial, calibration)
            except (KeyError, TypeError, ValueError) as error:
                print(f"警告：幾何校正與相機參數不符，僅執行YOLO：{error}")
        print("按q或Ctrl+C結束。")

        while not manager.step(camera):
            pass

    except KeyboardInterrupt:
        print("\n收到Ctrl+C，停止紀錄。")
    finally:
        # A preview/network/camera cleanup failure must not strand a non-daemon
        # writer or lose already frozen captures.
        for label, callback in (
            ("模式", manager.close),
            ("CSV", logger.close if logger is not None else None),
            ("相機", camera.close),
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
