"""Geometry (pixel -> division) layer and its regression against the old formula."""

import json
import math
import unittest

import numpy as np

import migrate_calibration
from bubble_measurement import BubbleCalibration
from calibration_store import NOMINAL_VIAL
from detector import Detection
from geometry_calibration import GeometryCalibration, legacy_tick_geometry
from telemetry_server import build_telemetry_payload

TIMINGS = dict(capture_ms=1., predict_ms=1., yolo_inference_ms=1., process_ms=1., fps=1.)


def payload(measurement, vial):
    detection = Detection(detected=1, detection_count=1, confidence=.9)
    return build_telemetry_payload(1, detection, measurement, TIMINGS, mm_per_m_per_div=vial.mm_per_m_per_div,
                                   zero_offset_div=vial.zero_offset_div, level_tolerance_mm_per_m=.01,
                                   max_measurable_slope_mm_per_m=.12)


class GeometryTests(unittest.TestCase):
    def test_degree_one_matches_old_center_and_pitch_formula(self):
        geometry = GeometryCalibration("g_geometry", 1, 370.25, (0., 1 / 18.))
        for x in np.linspace(0, 740, 2961):
            self.assertLess(abs(geometry.divisions(x) - (x - 370.25) / 18.), 1e-6)
        self.assertAlmostEqual(geometry.zero_x_roi(), 370.25, places=12)
        self.assertAlmostEqual(geometry.px_per_div(100.), 18., places=12)

    def test_nominal_vial_reproduces_previous_telemetry(self):
        old = BubbleCalibration(370.25, 18., 740, 160, "PASS", "", "old.json")
        new = GeometryCalibration("g_geometry", 1, 370.25, (0., 1 / 18.))
        for x in np.linspace(250, 490, 241):
            before, after = payload(old.measure(x), NOMINAL_VIAL)["measurement"], payload(new.measure(x), NOMINAL_VIAL)["measurement"]
            self.assertEqual(before.keys(), after.keys())
            for name, value in before.items():
                if isinstance(value, float):
                    self.assertAlmostEqual(after[name], value, places=9, msg=name)
                else:
                    self.assertEqual(after[name], value, name)

    def test_vial_gain_and_zero_offset_change_only_slope(self):
        geometry = GeometryCalibration("g_geometry", 1, 370.25, (0., 1 / 18.))
        measurement = geometry.measure(370.25 + 18 * 2.427)
        result = payload(measurement, type(NOMINAL_VIAL)("v_vial", .02283, .427))["measurement"]
        self.assertAlmostEqual(result["offset_div"], 2.427, places=9)
        self.assertAlmostEqual(result["slope_mm_per_m"], 2 * .02283, places=12)
        self.assertAlmostEqual(result["angle_degrees"], math.degrees(math.atan(2 * .02283 / 1000)), places=15)

    def test_quadratic_geometry_center_pitch_and_direction(self):
        geometry = GeometryCalibration("g_geometry", 2, 370., (.1, 1 / 18., 1e-5))
        zero = geometry.zero_x_roi()
        self.assertAlmostEqual(geometry.divisions(zero), 0., places=12)
        measurement = geometry.measure(zero - 30)
        self.assertEqual((measurement.valid, measurement.direction), (1, "left"))
        self.assertAlmostEqual(measurement.offset_div, geometry.divisions(zero - 30), places=12)
        self.assertAlmostEqual(measurement.pitch_px_per_div, 1 / geometry.derivative(zero), places=12)
        self.assertEqual(geometry.measure(None).error, "bubble_not_detected")

    def test_rejects_bad_coefficients(self):
        for degree, coefficients in ((1, (0., -1 / 18)), (2, (0., 1 / 18)), (1, (0., math.nan))):
            with self.assertRaises(ValueError):
                GeometryCalibration("g_geometry", degree, 370., coefficients)


class LegacyConversionTests(unittest.TestCase):
    def legacy(self, **changes):
        candidates = [{"side": side, "tick_id": k, "x_at_axis": 370.25 + sign * (4.5 + k) * 18 + (.5 if k == 3 else 0),
                       "x_full_centroid": 370.25 + sign * (4.5 + k) * 18, "valid": True}
                      for side, sign in (("left", -1), ("right", 1)) for k in range(13)]
        data = {"status": "PASS", "created_utc": "2026-10-01T23:29:23+00:00", "reference_midpoint_x": 370.25,
                "global_pitch": {"pitch_px": 18.}, "parameters": {"expected_ticks_per_side": 13}, "candidates": candidates,
                "image_geometry": {"coordinate_system": "undistorted", "frame_size": [960, 540], "roi_origin": [110, 190]}}
        data.update(changes)
        return data

    def test_conversion_is_degree_one_and_fits_inner_half_gap(self):
        record = legacy_tick_geometry(self.legacy(), version="20261002T072923_geometry",
                                      created_at_iso="2026-10-02T07:29:23+08:00", source_path="ticks.json")
        self.assertEqual((record["polynomial_degree"], record["x_center_px"]), (1, 370.25))
        self.assertEqual(record["coefficients_x_to_div"], [0., 1 / 18.])
        self.assertAlmostEqual(record["inner_half_gap_div"], 4.5, places=6)
        self.assertEqual((record["checks"]["tick_count"], record["checks"]["passed"]), (26, True))
        self.assertGreater(record["fit_residual_max_px"], .4)
        geometry = GeometryCalibration.from_dict(json.loads(json.dumps(record)))
        self.assertEqual(geometry.divisions(388.25), 1.)

    def test_conversion_rejects_original_coordinates_or_failed_status(self):
        for changes in ({"status": "FAIL"}, {"image_geometry": {"coordinate_system": "original"}}):
            with self.assertRaises(ValueError):
                legacy_tick_geometry(self.legacy(**changes), version="v_geometry", created_at_iso="", source_path="")

    @unittest.skipUnless(migrate_calibration.LEGACY_TICK_MEASUREMENT.is_file(), "legacy tick JSON not present")
    def test_real_legacy_json_matches_old_bubble_calibration(self):
        record = migrate_calibration.geometry_record(migrate_calibration.LEGACY_TICK_MEASUREMENT)
        self.assertEqual(record["version"], "20261002T072923_geometry")
        self.assertEqual(record["created_at_iso"], "2026-10-02T07:29:23+08:00")
        self.assertEqual(record["checks"]["tick_count"], 26)
        self.assertLess(record["fit_residual_rms_px"], .5)
        geometry = GeometryCalibration.from_dict(record)
        old = BubbleCalibration.from_json(migrate_calibration.LEGACY_TICK_MEASUREMENT, expected_size=(740, 160))
        for x in np.linspace(0, 740, 741):
            self.assertLess(abs(geometry.divisions(x) - old.measure(x).offset_div), 1e-6)


if __name__ == "__main__":
    unittest.main()
