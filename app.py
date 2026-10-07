"""levelsvc: one service, one camera, one processor per mode.

CameraService opens the camera once; ModeManager hands each frame to the
active processor (① measure, ② align, ③ ticks); the web server exposes
telemetry, captures, /api/state and /api/mode.
"""

import config
from calibration_runtime import CalibrationRuntime
from calibration_store import CalibrationStore, calibration_info
from camera import create_camera
from camera_service import CameraService
from csv_logger import CsvLogger
from detector import YoloDetector
from display import close_windows
from manual_capture import CaptureManager
from mode_manager import ModeManager
from preview import PreviewBuffer
from processors.align import AlignProcessor
from processors.measure import MeasureProcessor
from processors.ticks import TickProcessor
from system_controller import PinGuard, SystemController, capture_sessions, session_zip, system_summary
from telemetry_server import FileDownload, TelemetryServer


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


def api_routes(manager, ticks, runtime, align=None, controller=None, state=None):
    """(method, path regex, handler(params, body) -> (status, json)) for the web server."""
    def measure_ticks(params, body):
        if manager.mode != "ticks":
            return 409, {"status": "error", "error_code": "WRONG_MODE", "message": "請先切換到刻度檢查模式"}
        return 202, ticks.request_measure()

    def tick_result(params, body):
        result = ticks.result(params["id"])
        return (200, result) if result else (404, {"status": "error", "error_code": "NOT_FOUND",
                                                    "message": "measurement not found"})

    def activate(params, body):
        try:
            return 200, runtime.activate(params["kind"], params["version"])
        except FileNotFoundError as error:
            return 404, {"status": "error", "error_code": "NOT_FOUND", "message": str(error)}

    def in_align(handler):
        def run(params, body):
            if manager.mode != "align":
                return 409, {"status": "error", "error_code": "WRONG_MODE", "message": "請先切換到相機對位模式"}
            return handler(params, body)
        return run

    kinds, version = r"(?P<kind>geometry|vial|alignment)", r"(?P<version>[0-9A-Za-z_]{1,64})"
    align_routes = [] if align is None else [
        ("GET", r"/api/align", lambda params, body: (200, align.status())),
        ("POST", r"/api/align/teach/(?P<screw>[AB])/(?P<step>start|finish)",
         in_align(lambda params, body: align.teach(params["screw"], params["step"]))),
        ("POST", r"/api/align/complete", in_align(lambda params, body: align.complete())),
    ]
    session = r"(?P<session>\d{8}_\d{6}_[0-9a-f]{8})"
    system_routes = [] if controller is None else [
        ("GET", r"/api/system/status", lambda params, body: (200, state()["system"])),
        ("POST", r"/api/system/(?P<action>restart-service|reboot|shutdown)",
         lambda params, body: controller.request(params["action"]), True),
        ("GET", r"/api/logs/sessions", lambda params, body: (200, {"sessions": capture_sessions()})),
        ("GET", rf"/api/logs/sessions/{session}\.zip",
         lambda params, body: (200, FileDownload(session_zip(params["session"]), f"{params['session']}.zip"))),
    ]
    return align_routes + system_routes + [
        ("POST", r"/api/ticks/measure", measure_ticks),
        ("GET", r"/api/ticks/(?P<id>[0-9a-f]{12})", tick_result),
        ("POST", r"/api/ticks/(?P<id>[0-9a-f]{12})/apply",
         lambda params, body: ticks.apply(params["id"], confirm=body.get("confirm") is True)),
        ("GET", rf"/api/calibration/{kinds}", lambda params, body: (200, runtime.history(params["kind"]))),
        ("POST", rf"/api/calibration/{kinds}/{version}/activate", activate, True),
    ]


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

    measure = MeasureProcessor(detector=detector, captures=captures, publish=publish, logger=logger, camera=camera)
    runtime = CalibrationRuntime(store, measure, lambda: camera.undistorter if camera.camera is not None else None)
    preview = PreviewBuffer()
    ticks = TickProcessor(runtime=runtime, camera=camera, publish=publish, preview=preview)
    align = AlignProcessor(store=store, runtime=runtime, camera=camera, publish=publish, preview=preview)
    manager = ModeManager(
        {"measure": measure, "align": align, "ticks": ticks},
        on_change=lambda mode_state: publish({"type": "state", "schema_version": 1, **mode_state}),
    )

    controller = SystemController(captures)
    pin = PinGuard()

    def service_state():
        return {"type": "state", "schema_version": 1, **manager.state(),
                "calibration": runtime.status(),
                "camera": {"open": camera.camera is not None, "open_count": camera.open_count,
                           "frame_id": camera.frame_id,
                           "focus_absolute": getattr(camera.camera, "focus_absolute", None)},
                "system": {**system_summary(), "storage": captures.storage_estimate(),
                           "pin_configured": bool(pin.pin), "dry_run": config.SYSTEM_DRY_RUN}}

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
                routes=api_routes(manager, ticks, runtime, align, controller, service_state),
                preview=preview,
                pin_guard=pin,
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
        status = runtime.reload()
        geometry, vial = measure.geometry, measure.vial
        if geometry is not None:
            scale_center = geometry.zero_x_roi()
            print(
                f"幾何校正 {geometry.version}（{geometry.polynomial_degree} 次）："
                f"center={scale_center:.3f}px, pitch={geometry.px_per_div(scale_center):.3f}px/div；"
                f"水平儀校正 {vial.version}：{vial.mm_per_m_per_div:.5f} mm/m/div，"
                f"零點 {vial.zero_offset_div:+.3f} div"
            )
            if status["status"] == "pending":
                print("注意：幾何校正待確認（相機可能動過），請重做刻度檢查。")
        elif config.ENABLE_BUBBLE_MEASUREMENT:
            print(f"警告：校正檔無法使用（{status['error']}），僅執行YOLO。")
        print("按q或Ctrl+C結束。")

        # A safe shutdown/reboot request ends the loop between two frames.
        while not manager.step(camera) and not controller.stop_requested.is_set():
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
            ("系統控制", controller.camera_has_closed),
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

        controller.wait_for_power_command()
        print("程式已結束。")
