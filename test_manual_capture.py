"""Real writer/API tests using fake ROI arrays; no Picamera2 hardware."""
import contextlib
import csv
import errno
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

import app
import calibration_runtime
import camera_service
import config
import manual_capture
import processors.measure
from bubble_measurement import BubbleCalibration, BubbleMeasurement
from calibration_store import VialCalibration
from geometry_calibration import GeometryCalibration
from detector import Detection, Prediction
from display import annotate_capture_roi
from manual_capture import (CaptureFailure, CaptureManager, PI_LOG_FIELDS, SAVED_RESPONSE_FIELDS,
                            parse_capture_options)
from stability import StabilityTracker
from telemetry_server import TelemetryServer, build_telemetry_payload


def frame_data(value=60, detected=True):
    roi = np.full((160, 740, 3), value, np.uint8)
    calibration = BubbleCalibration(370., 18., 740, 160, "PASS", "", "fake.json")
    detection = Detection(detected=1, detection_count=1, class_id=0, class_name="bubble",
                          confidence=.95, x1_roi=340., y1_roi=45., x2_roi=436., y2_roi=145.,
                          center_x_roi=388., center_y_roi=95., box_width=96., box_height=100.) if detected else Detection()
    measurement = calibration.measure(388. if detected else None)
    timings = dict(capture_ms=2., predict_ms=80., yolo_preprocess_ms=2., yolo_inference_ms=75.,
                   yolo_postprocess_ms=3., process_ms=84., fps=1000 / 84, plot_ms=0.)
    payload = build_telemetry_payload(42, detection, measurement, timings,
        mm_per_m_per_div=.02, level_tolerance_mm_per_m=.01, max_measurable_slope_mm_per_m=.12)
    tracker = StabilityTracker(hold_seconds=0)
    for i in range(20):
        stability = tracker.update(detected, .02, 1., .95, now=i)
    return roi, detection, measurement, timings, payload, stability


def freeze(manager, requests, *, frame_id=42, value=60, detected=True, data=None, raw_frame=None):
    roi, detection, measurement, timings, payload, stability = data or frame_data(value, detected)
    epoch = time.time() * 1000
    manager.freeze_frame(requests, roi, frame_id, detection, measurement, timings, payload, stability,
                         frame_started_epoch_ms=epoch, capture_completed_epoch_ms=epoch + 2,
                         prediction_completed_epoch_ms=epoch + 82, frame_completed_monotonic=time.monotonic(),
                         raw_frame=raw_frame)
    return roi


class CaptureTests(unittest.TestCase):
    """Single-frame triggers (burst_frames=1) keep the original one-row contract."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = CaptureManager(self.directory.name, default_burst_frames=1)
        self.manager.set_ready()

    def tearDown(self):
        self.manager.close()
        self.directory.cleanup()

    def trigger(self):
        request_id = str(uuid.uuid4())
        self.assertEqual(self.manager.submit(request_id)[0], 202)
        return request_id

    def complete(self, request_id, **kwargs):
        freeze(self.manager, self.manager.begin_frame(), **kwargs)
        self.manager.jobs.join()
        return self.manager.get_status(request_id)

    def rows(self):
        with self.manager.csv_path.open(newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def test_log_timezone_is_taipei_even_when_pi_system_timezone_is_utc(self):
        try:
            with patch.dict(os.environ, {"TZ": "UTC"}):
                time.tzset()
                iso, epoch = manual_capture.wall_time(1767225600000)
                self.assertEqual(iso, "2026-01-01T08:00:00.000+08:00")
                self.assertEqual(epoch, 1767225600000)
        finally:
            time.tzset()
        self.assertEqual(manual_capture.session_metadata("test")["log_timezone"], "Asia/Taipei")

    def test_one_trigger_is_one_row_two_same_frame_images(self):
        request_id = self.trigger()
        result = self.complete(request_id)
        self.assertEqual(result["status"], "saved")
        row, = self.rows()
        self.assertEqual(set(row), set(PI_LOG_FIELDS))
        self.assertEqual(row["frame_id"], str(result["frame_id"]))
        self.assertEqual(row["request_id"], request_id)
        self.assertEqual(row["record_id"], result["record_id"])
        clean = cv2.imread(str(self.manager.session_directory / row["clean_image_path"]))
        annotated = cv2.imread(str(self.manager.session_directory / row["annotated_image_path"]))
        self.assertEqual(clean.shape, (160, 740, 3))
        self.assertEqual(annotated.shape, clean.shape)
        np.testing.assert_allclose(clean, 60, atol=2)
        self.assertGreater(np.abs(annotated.astype(float) - clean).sum(), 0)
        self.assertIn("+", row["saved_at_iso"])
        for name in ("queue_wait_ms", "annotation_ms", "image_write_ms", "csv_write_ms", "request_to_saved_ms"):
            self.assertGreaterEqual(float(row[name]), 0)

    def test_idempotency_before_and_after_save(self):
        request_id = self.trigger()
        self.assertEqual(self.manager.submit(request_id)[1]["status"], "pending")
        self.assertEqual(self.manager.requests.qsize(), 1)
        first = self.complete(request_id)
        self.assertEqual(self.manager.submit(request_id), (200, first))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.manager.requests.qsize(), 0)

    def test_concurrent_duplicate_posts_enqueue_once(self):
        request_id = str(uuid.uuid4())
        threads = [threading.Thread(target=self.manager.submit, args=(request_id,)) for _ in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(self.manager.requests.qsize(), 1)
        self.complete(request_id)
        self.assertEqual(len(self.rows()), 1)

    def test_queue_full_is_429(self):
        for _ in range(self.manager.requests.maxsize): self.trigger()
        status, response = self.manager.submit(str(uuid.uuid4()))
        self.assertEqual(status, 429)
        self.assertEqual(response["error_code"], "QUEUE_FULL")

    def test_active_limit_is_409(self):
        for _ in range(self.manager.max_active): self.trigger()
        self.manager.begin_frame()
        self.assertEqual(self.manager.submit(str(uuid.uuid4()))[0], 409)

    def test_request_after_frame_started_waits_for_next_frame(self):
        accepted_for_old_frame = self.manager.begin_frame()
        request_id = self.trigger()  # Simulate HTTP arrival during inference.
        freeze(self.manager, accepted_for_old_frame, frame_id=10, value=15)
        self.assertEqual(self.manager.get_status(request_id)["status"], "pending")
        self.assertEqual(self.rows(), [])
        accepted = self.manager.begin_frame()
        self.assertGreaterEqual(accepted[0].accepted_monotonic, accepted[0].received_monotonic)
        freeze(self.manager, accepted, frame_id=11, value=180)
        self.manager.jobs.join()
        row, = self.rows()
        self.assertEqual(row["frame_id"], "11")
        clean = cv2.imread(str(self.manager.session_directory / row["clean_image_path"]))
        np.testing.assert_allclose(clean, 180, atol=2)

    def test_clean_roi_not_modified_by_annotation_or_later_camera_buffer(self):
        data = frame_data()
        request_id = self.trigger()
        original = data[0].copy()
        freeze(self.manager, self.manager.begin_frame(), data=data)
        data[0][:] = 220
        self.manager.jobs.join()
        row, = self.rows()
        clean = cv2.imread(str(self.manager.session_directory / row["clean_image_path"]))
        np.testing.assert_allclose(clean, original, atol=2)
        self.assertEqual(self.manager.get_status(request_id)["status"], "saved")
        output = annotate_capture_roi(original, row)
        self.assertEqual(output.shape, original.shape)
        np.testing.assert_array_equal(original, 60)

    def test_no_request_no_images_no_rows_no_encoding(self):
        with patch.object(manual_capture.cv2, "imencode") as encode, patch.object(manual_capture, "annotate_capture_roi") as annotate:
            for frame_id in range(20): freeze(self.manager, [], frame_id=frame_id)
            encode.assert_not_called()
            annotate.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.manager.images_directory.iterdir()), [])

    def test_annotated_failure_cleans_current_files_and_later_request_succeeds(self):
        request_id = self.trigger()
        original = self.manager._write_image
        def write(path, image):
            if "annotated" in path.name: raise CaptureFailure("IMAGE_WRITE_FAILED", "annotated ROI could not be saved")
            original(path, image)
        with patch.object(self.manager, "_write_image", side_effect=write), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["error_code"], "IMAGE_WRITE_FAILED")
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.manager.images_directory.iterdir()), [])
        self.assertTrue(self.manager._worker.is_alive())
        self.assertEqual(self.complete(self.trigger())["status"], "saved")

    def test_jpeg_encode_failure_is_error(self):
        request_id = self.trigger()
        with patch.object(manual_capture.cv2, "imencode", return_value=(False, None)), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["error_code"], "JPEG_ENCODE_FAILED")
        self.assertEqual(self.rows(), [])

    def test_disk_full_is_explicit_error(self):
        request_id = self.trigger()
        with patch.object(self.manager, "_write_image", side_effect=OSError(errno.ENOSPC, "disk full")), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["error_code"], "DISK_FULL")

    def test_csv_failure_does_not_remove_other_successful_samples(self):
        first = self.complete(self.trigger())
        request_id = self.trigger()
        with patch.object(self.manager, "_append_csv", side_effect=OSError("CSV unavailable")), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["error_code"], "CSV_WRITE_FAILED")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(list(self.manager.images_directory.glob("*.jpg"))), 2)
        self.assertEqual(self.rows()[0]["record_id"], first["record_id"])

    def test_csv_partial_write_is_rolled_back(self):
        request_id = self.trigger()
        calls = 0
        original = manual_capture.os.fsync
        def fsync(fd):
            nonlocal calls
            calls += 1
            # Two JPEG syncs + directory sync; fail first CSV fsync once.
            if calls == 4: raise OSError("CSV sync failed")
            original(fd)
        with patch.object(manual_capture.os, "fsync", side_effect=fsync), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.rows(), [])

    def test_writer_exception_does_not_stop_worker(self):
        request_id = self.trigger()
        with patch.object(manual_capture, "annotate_capture_roi", side_effect=RuntimeError("bad annotation")), self.assertLogs("manual_capture", level="ERROR"):
            result = self.complete(request_id)
        self.assertEqual(result["error_code"], "WRITER_FAILED")
        self.assertTrue(self.manager._worker.is_alive())

    def test_no_detection_uses_empty_coordinates_and_can_be_recorded(self):
        result = self.complete(self.trigger(), detected=False)
        self.assertEqual(result["status"], "saved")
        row, = self.rows()
        self.assertEqual(row["system_state"], "SEARCHING")
        self.assertEqual(row["x1_roi"], "")
        self.assertEqual(row["center_x_roi"], "")
        self.assertEqual(row["stability_state"], "NO_MEASUREMENT")

    def test_calibration_unavailable_is_recorded_as_invalid(self):
        roi, detection, _, timings, _, stability = frame_data()
        measurement = BubbleMeasurement.disabled("calibration_unavailable")
        payload = build_telemetry_payload(42, detection, measurement, timings,
            mm_per_m_per_div=.02, level_tolerance_mm_per_m=.01, max_measurable_slope_mm_per_m=.12)
        result = self.complete(self.trigger(), data=(roi, detection, measurement, timings, payload, stability))
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.rows()[0]["measurement_error"], "calibration_unavailable")

    def test_backend_checks_stability_and_rejects_without_images(self):
        self.manager.require_stable = True
        result = self.complete(self.trigger(), detected=False)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], "NOT_STABLE")
        self.assertEqual(self.rows(), [])

    def test_unstable_allowed_by_default_and_faithfully_logged(self):
        data = list(frame_data())
        data[-1] = StabilityTracker().update(True, .02, 1, .95)
        self.assertEqual(self.complete(self.trigger(), data=data)["status"], "saved")
        self.assertEqual(self.rows()[0]["stability_state"], "WARMING_UP")

    def test_system_information_failure_is_nonfatal(self):
        request_id = self.trigger()
        with patch.object(manual_capture.os, "getloadavg", side_effect=OSError()), patch.object(manual_capture.shutil, "disk_usage", side_effect=OSError()):
            self.assertEqual(self.complete(request_id)["status"], "saved")
        self.assertEqual(self.rows()[0]["disk_free_mb"], "")

    def test_minimal_success_response_and_bounded_status_memory(self):
        for _ in range(8):
            result = self.complete(self.trigger())
            self.assertEqual(set(result), set(SAVED_RESPONSE_FIELDS))
        self.assertEqual(self.manager._active, {})

    def test_idempotency_survives_pi_restart(self):
        request_id = self.trigger()
        previous = self.complete(request_id)
        self.manager.close()
        self.manager = CaptureManager(self.directory.name, default_burst_frames=1)
        self.manager.set_ready()
        self.assertEqual(self.manager.submit(request_id), (200, previous))
        self.assertEqual(self.rows(), [])

    def test_shutdown_finishes_writer_and_errors_pending_requests(self):
        saved_id = self.trigger()
        freeze(self.manager, self.manager.begin_frame())
        pending_id = self.trigger()
        with self.assertLogs("manual_capture", level="ERROR"):
            self.manager.close()
        self.assertFalse(self.manager._worker.is_alive())
        with contextlib.closing(sqlite3.connect(self.manager.root / "capture_requests.sqlite3")) as database:
            statuses = {key: json.loads(raw) for key, raw in database.execute("SELECT request_id, response FROM requests")}
        self.assertEqual(statuses[saved_id]["status"], "saved")
        self.assertEqual(statuses[pending_id]["error_code"], "SERVER_SHUTDOWN")

    def test_writer_queue_is_bounded_and_full_job_fails(self):
        self.manager.close()
        self.manager = CaptureManager(self.directory.name, writer_queue_size=1, default_burst_frames=1)
        self.manager.set_ready()
        entered, release = threading.Event(), threading.Event()
        original = self.manager._save
        def blocked(job):
            entered.set()
            release.wait(3)
            return original(job)
        try:
            with patch.object(self.manager, "_save", side_effect=blocked):
                first = self.trigger()
                freeze(self.manager, self.manager.begin_frame())
                self.assertTrue(entered.wait(1))
                self.trigger()
                freeze(self.manager, self.manager.begin_frame())
                third = self.trigger()
                with self.assertLogs("manual_capture", level="ERROR"):
                    freeze(self.manager, self.manager.begin_frame())
                self.assertEqual(self.manager.get_status(third)["error_code"], "WRITER_QUEUE_FULL")
                release.set()
                self.manager.jobs.join()
                self.assertEqual(self.manager.get_status(first)["status"], "saved")
        finally:
            release.set()


CALIBRATION = {"geometry_version": "20261002T072923_geometry", "geometry_pending_confirmation": False,
               "vial_version": "20261006T200000_vial", "alignment_version": "",
               "mm_per_m_per_div": .02283, "zero_offset_div": .427}


class BurstCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = CaptureManager(self.directory.name, calibration={**CALIBRATION, "geometry_source": "g.json"})
        self.manager.set_ready()

    def tearDown(self):
        self.manager.close()
        self.directory.cleanup()

    def rows(self):
        with self.manager.csv_path.open(newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def data(self, center):
        roi, detection, _, timings, _, stability = frame_data()
        measurement = BubbleCalibration(370., 18., 740, 160, "PASS", "", "fake.json").measure(center)
        payload = build_telemetry_payload(42, detection, measurement, timings, mm_per_m_per_div=.02283,
            zero_offset_div=.427, level_tolerance_mm_per_m=.01, max_measurable_slope_mm_per_m=.12,
            calibration=CALIBRATION)
        return roi, detection, measurement, timings, payload, stability

    def test_default_burst_size_comes_from_config(self):
        self.assertEqual(self.manager.default_burst_frames, config.CAPTURE_BURST_FRAMES)

    def test_one_trigger_records_consecutive_frames_summary_and_lossless_raw_frame(self):
        request_id = str(uuid.uuid4())
        options = parse_capture_options({"request_id": request_id, "reference_deg": -.004, "a_axis_deg": -.012,
                                         "sweep_direction": "backward", "note": "pt 2", "burst_frames": 3})
        self.assertEqual(self.manager.submit(request_id, options)[0], 202)
        raw = np.random.default_rng(3).integers(0, 256, (540, 960, 3), dtype=np.uint8)
        for frame_id, center in zip((10, 11, 12), (388., 389., 393.)):
            freeze(self.manager, self.manager.begin_frame(), frame_id=frame_id, data=self.data(center),
                   raw_frame=raw if frame_id < 12 else raw * 0)
            if frame_id < 12:
                self.assertEqual(self.manager.get_status(request_id)["status"], "processing")
        self.manager.jobs.join()
        result = self.manager.get_status(request_id)
        self.assertEqual((result["status"], result["frame_id"], result["sample_id"]), ("saved", 10, 1))
        rows = self.rows()
        self.assertEqual([row["row_type"] for row in rows], ["frame", "frame", "frame", "summary"])
        self.assertEqual({row["burst_id"] for row in rows}, {result["record_id"]})
        self.assertEqual([row["burst_index"] for row in rows], ["1", "2", "3", ""])
        self.assertEqual([row["frame_id"] for row in rows], ["10", "11", "12", ""])
        for row in rows:
            self.assertEqual((row["sample_id"], row["burst_size"], row["reference_deg"], row["a_axis_deg"],
                              row["sweep_direction"], row["note"]), ("1", "3", "-0.004", "-0.012", "backward", "pt 2"))
            self.assertEqual((row["geometry_version"], row["vial_version"], row["zero_offset_div"]),
                             (CALIBRATION["geometry_version"], CALIBRATION["vial_version"], "0.427"))
            self.assertEqual(row["raw_image_path"], rows[0]["raw_image_path"])
        offsets = [(center - 370) / 18 for center in (388., 389., 393.)]
        slopes = [(offset - .427) * .02283 for offset in offsets]
        summary = rows[-1]
        self.assertEqual(summary["burst_valid_count"], "3")
        self.assertAlmostEqual(float(summary["burst_median_slope_mm_per_m"]), slopes[1], places=12)
        self.assertAlmostEqual(float(summary["burst_std_slope_mm_per_m"]), float(np.std(slopes)), places=12)
        self.assertAlmostEqual(float(summary["burst_median_offset_div"]), offsets[1], places=12)
        self.assertAlmostEqual(float(rows[0]["slope_mm_per_m"]), slopes[0], places=12)
        images = self.manager.images_directory
        self.assertEqual(len(list(images.glob("*.jpg"))), 6)
        self.assertEqual(len({row["clean_image_path"] for row in rows[:3]}), 3)
        saved_raw = cv2.imread(str(self.manager.session_directory / rows[0]["raw_image_path"]), cv2.IMREAD_UNCHANGED)
        np.testing.assert_array_equal(saved_raw, raw)  # first burst frame, before undistortion, lossless
        self.assertEqual(rows[0]["raw_frame_id"], "10")
        estimate = self.manager.storage_estimate()
        self.assertEqual(estimate["bytes_per_capture"], sum(path.stat().st_size for path in images.iterdir())
                         + self.manager.csv_path.stat().st_size - len(",".join(PI_LOG_FIELDS)) - 2)

    def test_bursts_overlap_and_a_late_request_starts_at_its_own_next_frame(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.manager.submit(first, {"burst_frames": 2})
        freeze(self.manager, self.manager.begin_frame(), frame_id=1)
        self.manager.submit(second, {"burst_frames": 2})
        for frame_id in (2, 3):
            freeze(self.manager, self.manager.begin_frame(), frame_id=frame_id)
        self.manager.jobs.join()
        rows = [row for row in self.rows() if row["row_type"] == "frame"]
        frames = {request: [row["frame_id"] for row in rows if row["request_id"] == request] for request in (first, second)}
        self.assertEqual(frames, {first: ["1", "2"], second: ["2", "3"]})

    def test_shutdown_mid_burst_saves_nothing_and_reports_error(self):
        request_id = str(uuid.uuid4())
        self.manager.submit(request_id, {"burst_frames": 3})
        freeze(self.manager, self.manager.begin_frame())
        with self.assertLogs("manual_capture", level="ERROR"):
            self.manager.close()
        self.assertEqual(self.rows(), [])
        with contextlib.closing(sqlite3.connect(self.manager.root / "capture_requests.sqlite3")) as database:
            response = json.loads(database.execute("SELECT response FROM requests WHERE request_id=?", (request_id,)).fetchone()[0])
        self.assertEqual(response["error_code"], "SERVER_SHUTDOWN")

    def test_failed_frame_ends_burst_and_frees_later_frames(self):
        request_id = str(uuid.uuid4())
        self.manager.submit(request_id, {"burst_frames": 3})
        freeze(self.manager, self.manager.begin_frame())
        bad = list(frame_data())
        bad[0] = np.zeros((10, 10, 3), np.uint8)
        with self.assertLogs("manual_capture", level="ERROR"):
            freeze(self.manager, self.manager.begin_frame(), data=bad)
        self.assertEqual(self.manager.get_status(request_id)["error_code"], "ROI_SIZE_INVALID")
        self.assertEqual(self.manager.begin_frame(), [])

    def test_metadata_records_calibration_versions(self):
        metadata = json.loads((self.manager.session_directory / "session_metadata.json").read_text())
        self.assertEqual(metadata["calibration"]["geometry_version"], CALIBRATION["geometry_version"])
        self.assertEqual(metadata["calibration_source"], "g.json")
        self.assertEqual((metadata["mm_per_m_per_div"], metadata["zero_offset_div"]), (.02283, .427))
        self.assertEqual(metadata["capture_burst_frames_default"], config.CAPTURE_BURST_FRAMES)

    def test_capture_options_are_validated_and_normalized(self):
        self.assertEqual(parse_capture_options({"request_id": "x"}), {})
        self.assertEqual(parse_capture_options({"reference_deg": 0, "sweep_direction": "", "note": " a\n b "}),
                         {"reference_deg": 0.0, "note": "a b"})
        for bad in ({"reference_deg": float("inf")}, {"a_axis_deg": 91}, {"burst_frames": config.CAPTURE_BURST_MAX_FRAMES + 1},
                    {"note": 5}, {"extra": 1}):
            with self.assertRaises(ValueError):
                parse_capture_options(bad)


class CaptureApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = CaptureManager(self.directory.name, default_burst_frames=1)
        self.manager.set_ready()
        self.server = TelemetryServer(websocket_host="127.0.0.1", websocket_port=0,
            dashboard_host="127.0.0.1", dashboard_port=0,
            dashboard_directory=config.APP_DIRECTORY / "dashboard", capture_manager=self.manager)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server._http_server.server_address[1]}"
        self.request_id = str(uuid.uuid4())

    def tearDown(self):
        self.server.stop()
        self.manager.close()
        self.directory.cleanup()

    def call(self, path, body=None):
        request = Request(self.url + path, data=body, headers={"Content-Type": "application/json"})
        try: response = urlopen(request, timeout=3)
        except HTTPError as error: response = error
        with response:
            return response.code, json.loads(response.read())

    def post(self, request_id=None):
        return self.call("/api/captures", json.dumps({"request_id": request_id or self.request_id}).encode())

    def test_post_creates_pending_and_get_reads_pending(self):
        self.assertEqual(self.post(), (202, {"status": "pending", "request_id": self.request_id}))
        self.assertEqual(self.call("/api/captures/" + self.request_id)[1]["status"], "pending")

    def test_get_saved_has_only_minimal_fields(self):
        self.post()
        freeze(self.manager, self.manager.begin_frame())
        self.manager.jobs.join()
        status, result = self.call("/api/captures/" + self.request_id)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(set(result), set(SAVED_RESPONSE_FIELDS))
        self.assertNotIn("confidence", result)

    def test_invalid_json_is_400(self):
        for body in (b"{", b"[]", b"null", b"{}", b'{"request_id": "x", "metrics": 12}'):
            self.assertEqual(self.call("/api/captures", body)[0], 400)

    def test_invalid_uuid_is_400(self):
        self.assertEqual(self.post("not-uuid")[0], 400)
        self.assertEqual(self.call("/api/captures/bad")[0], 400)

    def test_unknown_request_is_404(self):
        self.assertEqual(self.call("/api/captures/" + self.request_id)[0], 404)

    def test_duplicate_post_is_idempotent(self):
        self.post()
        self.post()
        self.assertEqual(self.manager.requests.qsize(), 1)
        freeze(self.manager, self.manager.begin_frame())
        self.manager.jobs.join()
        self.assertEqual(self.post()[1]["status"], "saved")
        self.assertEqual(self.manager.requests.qsize(), 0)

    def test_not_ready_is_503(self):
        self.manager.set_ready(False)
        self.assertEqual(self.post()[0], 503)

    def test_full_queue_is_429(self):
        for _ in range(self.manager.requests.maxsize): self.post(str(uuid.uuid4()))
        self.assertEqual(self.post()[0], 429)

    def test_readiness_exposes_capture_policy_and_storage_only(self):
        status, result = self.call("/api/captures/ready")
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"ready", "require_stable_for_capture", "burst_frames_default", "storage"})
        self.assertEqual((result["ready"], result["require_stable_for_capture"], result["burst_frames_default"]), (True, False, 1))
        self.assertGreater(result["storage"]["disk_free_mb"], 0)
        self.assertGreaterEqual(result["storage"]["estimated_remaining_captures"], 0)

    def test_post_accepts_capture_options_and_rejects_bad_values(self):
        body = {"request_id": self.request_id, "reference_deg": -0.004, "a_axis_deg": -0.012,
                "sweep_direction": "forward", "note": "point 3", "burst_frames": 1}
        self.assertEqual(self.call("/api/captures", json.dumps(body).encode())[0], 202)
        freeze(self.manager, self.manager.begin_frame())
        self.manager.jobs.join()
        with self.manager.csv_path.open(newline="", encoding="utf-8") as file:
            row, = csv.DictReader(file)
        self.assertEqual((row["reference_deg"], row["a_axis_deg"], row["sweep_direction"], row["note"]),
                         ("-0.004", "-0.012", "forward", "point 3"))
        for bad in ({"reference_deg": "x"}, {"reference_deg": True}, {"sweep_direction": "up"},
                    {"burst_frames": 0}, {"burst_frames": 1.5}, {"note": "n" * 201}, {"metrics": 12}):
            status, result = self.call("/api/captures", json.dumps({"request_id": str(uuid.uuid4()), **bad}).encode())
            self.assertEqual((status, result["error_code"]), (400, "INVALID_CAPTURE_OPTIONS"), bad)
        status, _ = self.call("/api/captures", b'{"request_id": "%s", "reference_deg": NaN}' % str(uuid.uuid4()).encode())
        self.assertEqual(status, 400)

    def test_processing_can_be_queried(self):
        self.post()
        self.manager.begin_frame()
        self.assertEqual(self.call("/api/captures/" + self.request_id)[1]["status"], "processing")


class ManualMainLoopTests(unittest.TestCase):
    def run_fake(self, trigger=False, writer_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            manager = CaptureManager(directory, default_burst_frames=1)
            store = Mock()
            store.load_geometry.return_value = GeometryCalibration("g_geometry", 1, 370., (0., 1 / 18))
            store.load_vial.return_value = VialCalibration("v_vial", .02, 0.)
            store.summary.return_value = {kind: {"version": None, "pending_confirmation": False}
                                          for kind in ("geometry", "vial", "alignment")}
            request_id = str(uuid.uuid4())
            frames = 0
            detector = Mock()
            roi, detection, _, _, _, _ = frame_data()
            def predict(frame):
                nonlocal frames
                frames += 1
                if frames == 1 and trigger: manager.submit(request_id)
                if frames == 3: raise KeyboardInterrupt
                return Prediction(Mock(), detection, 80., 2., 75., 3.)
            detector.predict.side_effect = predict
            server = Mock()
            server.dashboard_urls.return_value = []
            with contextlib.ExitStack() as stack:
                for name, value in (("ENABLE_CONTINUOUS_CSV", False), ("ENABLE_IMAGE_STREAM", False),
                                    ("ENABLE_WEBSOCKET", True), ("TELEMETRY_SEND_EVERY", 2)):
                    stack.enter_context(patch.object(config, name, value))
                stack.enter_context(patch.object(app, "CaptureManager", return_value=manager))
                stack.enter_context(patch.object(app, "YoloDetector", return_value=detector))
                stack.enter_context(patch.object(app, "create_camera", return_value=Mock()))
                stack.enter_context(patch.object(camera_service, "capture_frames", return_value=(roi, None, roi)))
                stack.enter_context(patch.object(app, "CalibrationStore", return_value=store))
                stack.enter_context(patch.object(calibration_runtime, "check_undistorted_geometry"))
                stack.enter_context(patch.object(app, "TelemetryServer", return_value=server))
                stack.enter_context(patch.object(app, "close_windows"))
                logger = stack.enter_context(patch.object(app, "CsvLogger"))
                ask = stack.enter_context(patch.object(app, "ask_to_save_csv"))
                show = stack.enter_context(patch.object(processors.measure, "show_frame"))
                if writer_failure:
                    stack.enter_context(patch.object(manager, "_write_image", side_effect=CaptureFailure("IMAGE_WRITE_FAILED", "injected")))
                    stack.enter_context(self.assertLogs("manual_capture", level="ERROR"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                app.run()
                logger.assert_not_called()
                ask.assert_not_called()
                show.assert_not_called()
            with manager.csv_path.open(newline="") as file: rows = list(csv.DictReader(file))
            self.assertEqual(frames, 3)
            self.assertEqual(server.publish.call_count, 1)
            self.assertIn("stability", server.publish.call_args.args[0])
            self.assertEqual(server.publish.call_args.args[0]["frame_id"], 2)
            self.assertFalse(manager._worker.is_alive())
            return rows

    def test_preview_off_still_saves_next_frame(self):
        rows = self.run_fake(trigger=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frame_id"], "2")

    def test_default_no_request_creates_no_sample_or_legacy_csv(self):
        self.assertEqual(self.run_fake(), [])

    def test_image_failure_does_not_stop_inference(self):
        self.assertEqual(self.run_fake(trigger=True, writer_failure=True), [])


if __name__ == "__main__":
    unittest.main()
