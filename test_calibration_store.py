"""Versioned calibration files: new file per update, active pointer, rollback."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import migrate_calibration
from calibration_store import TIMEZONE, CalibrationStore, VialCalibration, new_version

GEOMETRY = {"version": "20261002T072923_geometry", "polynomial_degree": 1, "x_center_px": 370.25,
            "coefficients_x_to_div": [0., 1 / 18.]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CalibrationStore(Path(self.directory.name) / "calibration")

    def tearDown(self):
        self.directory.cleanup()

    def test_empty_store_has_no_active_version(self):
        self.assertEqual(self.store.active_state("vial"), {"version": None, "pending_confirmation": False})
        with self.assertRaises(FileNotFoundError):
            self.store.load_vial()

    def test_save_activates_and_links_previous_version(self):
        self.store.save("vial", {"version": "20261002T155000_vial", "mm_per_m_per_div": .02, "zero_offset_div": 0.})
        self.store.save("vial", {"version": "20261006T200000_vial", "mm_per_m_per_div": .02283, "zero_offset_div": .427})
        vial = self.store.load_vial()
        self.assertEqual((vial.version, vial.mm_per_m_per_div, vial.zero_offset_div), ("20261006T200000_vial", .02283, .427))
        self.assertAlmostEqual(vial.slope_mm_per_m(2.427), 2 * .02283, places=12)
        data, _ = self.store.load("vial")
        self.assertEqual(data["previous_version"], "20261002T155000_vial")
        self.assertEqual([item["active"] for item in self.store.versions("vial")], [False, True])

    def test_rollback_points_active_to_old_file_without_editing_it(self):
        self.store.save("geometry", GEOMETRY)
        old_path = self.store.path("geometry", GEOMETRY["version"])
        old_bytes = old_path.read_bytes()
        self.store.save("geometry", {**GEOMETRY, "version": "20261006T184000_geometry"}, pending_confirmation=True)
        self.assertTrue(self.store.active_state("geometry")["pending_confirmation"])
        self.store.activate("geometry", GEOMETRY["version"])
        self.assertEqual(self.store.load_geometry().version, GEOMETRY["version"])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(self.store.summary()["geometry"], {"version": GEOMETRY["version"], "pending_confirmation": False})

    def test_existing_version_is_never_overwritten(self):
        self.store.save("geometry", GEOMETRY)
        with self.assertRaises(FileExistsError):
            self.store.save("geometry", {**GEOMETRY, "x_center_px": 1.})
        self.assertEqual(self.store.load_geometry().x_center_px, 370.25)

    def test_rejects_unknown_kinds_bad_names_and_missing_versions(self):
        for kind, version in (("lens", "x_lens"), ("vial", "x_geometry"), ("vial", "../x_vial")):
            with self.assertRaises(ValueError):
                self.store.path(kind, version)
        with self.assertRaises(FileNotFoundError):
            self.store.activate("vial", "20260101T000000_vial")

    def test_file_must_record_its_own_version(self):
        path = self.store.path("vial", "20261006T200000_vial")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": "other_vial", "mm_per_m_per_div": .02, "zero_offset_div": 0}))
        with self.assertRaises(ValueError):
            self.store.load("vial", "20261006T200000_vial")

    def test_vial_values_are_validated(self):
        for gain, zero in ((0., 0.), (-.02, 0.), (.02, float("nan"))):
            with self.assertRaises(ValueError):
                VialCalibration("v_vial", gain, zero)

    def test_version_names_use_taipei_time(self):
        stamp = datetime(2026, 10, 6, 10, 40, tzinfo=TIMEZONE)
        self.assertEqual(new_version("geometry", stamp), "20261006T104000_geometry")


class MigrationTests(unittest.TestCase):
    def test_vial_migration_records_fit_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.json"
            summary.write_text(json.dumps({
                "session": "20261002_145946_64bcd440", "labeled_captures": 13, "reference_angles": 10,
                "method_A_current_system": {"max_abs_deg": .0011}, "method_B_yolo_calibrated_cv": {"max_abs_deg": .00024},
                "calibration_all_points": {"yolo": {"mm_per_m_per_div": .0228336, "zero_offset_div": .4267}}}))
            root = Path(directory) / "calibration"
            with contextlib.redirect_stdout(io.StringIO()):
                migrate_calibration.main(["vial", "--summary", str(summary), "--root", str(root)])
            store = CalibrationStore(root)
            vial = store.load_vial()
            self.assertEqual((vial.mm_per_m_per_div, vial.zero_offset_div), (.0228336, .4267))
            data, _ = store.load("vial")
            self.assertEqual(data["source_session"], "20261002_145946_64bcd440")
            self.assertEqual(data["cross_validation_leave_one_angle_out"], {"max_abs_deg": .00024})

    @unittest.skipUnless(migrate_calibration.LEGACY_TICK_MEASUREMENT.is_file(), "legacy tick JSON not present")
    def test_geometry_migration_creates_first_version_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "calibration"
            with contextlib.redirect_stdout(io.StringIO()):
                migrate_calibration.main(["geometry", "--root", str(root)])
                with self.assertRaises(FileExistsError):
                    migrate_calibration.main(["geometry", "--root", str(root)])
            self.assertEqual(CalibrationStore(root).load_geometry().version, "20261002T072923_geometry")


if __name__ == "__main__":
    unittest.main()
