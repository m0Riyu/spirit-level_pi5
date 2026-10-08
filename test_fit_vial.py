"""fit_vial: vial fit from reference captures, checks, cross-validation, not activated by default."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

import fit_vial
from calibration_store import CalibrationStore

GAIN, ZERO = .0232, .14
FIELDS = ("row_type", "burst_id", "reference_deg", "burst_median_offset_div", "burst_median_angle_degrees",
          "geometry_version", "vial_version")


def write_session(root, name, references, *, geometry="20261008T144936_geometry", noise=()):
    directory = Path(root) / name
    directory.mkdir()
    with (directory / fit_vial.CSV_NAME).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, FIELDS)
        writer.writeheader()
        for index, reference in enumerate(references, 1):
            div = fit_vial.deg_to_mm_per_m(reference) / GAIN + ZERO + (noise[index - 1] if noise else 0)
            row = {"burst_id": f"{name}_{index:06d}", "reference_deg": reference, "burst_median_offset_div": div,
                   "burst_median_angle_degrees": 0, "geometry_version": geometry, "vial_version": "old_vial"}
            writer.writerow(dict(row, row_type="frame"))
            writer.writerow(dict(row, row_type="summary"))
        writer.writerow({"row_type": "summary", "burst_id": f"{name}_999999", "reference_deg": "",
                         "burst_median_offset_div": 1.0, "geometry_version": geometry})  # no reference: skipped
    return directory


class FitVialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = CalibrationStore(self.root / "calibration")
        self.store.save("geometry", {"version": "20261008T144936_geometry", "polynomial_degree": 1,
                                     "x_center_px": 370., "coefficients_x_to_div": [0., 1 / 18.]})
        self.store.save("vial", {"version": "20261007T001334_vial", "mm_per_m_per_div": .02285, "zero_offset_div": .317})
        self.references = [round(-.006 + .001 * i, 4) for i in range(13)]

    def tearDown(self):
        self.directory.cleanup()

    def run_main(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            record = fit_vial.main([*map(str, arguments), "--calibration", str(self.store.root)])
        return record, output.getvalue()

    def test_recovers_gain_and_zero_and_does_not_activate(self):
        sessions = [write_session(self.root, f"s{i}", self.references) for i in range(3)]
        record, output = self.run_main(*sessions)
        self.assertAlmostEqual(record["mm_per_m_per_div"], GAIN, places=9)
        self.assertAlmostEqual(record["zero_offset_div"], ZERO, places=6)
        self.assertEqual(record["labeled_captures"], 39)  # frame rows and rows without reference ignored
        self.assertEqual(record["reference_angles"], 13)
        self.assertLess(record["fit_residual"]["max_abs_deg"], 1e-9)
        self.assertGreater(record["previous_vial_error"]["rms_deg"], 1e-5)
        self.assertEqual(record["previous_version"], "20261007T001334_vial")
        self.assertEqual(self.store.active_state("vial")["version"], "20261007T001334_vial")  # not activated
        self.assertEqual(self.store.load_vial(record["version"]).mm_per_m_per_div, record["mm_per_m_per_div"])
        self.assertIn("未套用", output)

    def test_activate_option(self):
        record, _ = self.run_main(write_session(self.root, "s", self.references), "--activate")
        self.assertEqual(self.store.active_state("vial")["version"], record["version"])

    def test_exclude_and_unknown_exclude(self):
        session = write_session(self.root, "s", self.references)
        record, _ = self.run_main(session, "--exclude", "s_000001")
        self.assertEqual((record["labeled_captures"], record["excluded_bursts"]), (12, ["s_000001"]))
        with self.assertRaises(ValueError):
            fit_vial.read_points([session], ["s_000077"])

    def test_cross_validation_per_session(self):
        noise = [.05 * (-1) ** i for i in range(13)]
        sessions = [write_session(self.root, "a", self.references), write_session(self.root, "b", self.references, noise=noise)]
        record, _ = self.run_main(*sessions)
        cv = record["cross_validation_leave_one_session_out"]
        self.assertEqual(set(cv["per_session"]), {"a", "b"})
        self.assertGreater(cv["overall"]["rms_deg"], record["fit_residual"]["rms_deg"])
        self.assertIsNone(fit_vial.leave_one_session_out(fit_vial.read_points([sessions[0]])))

    def test_refuses_geometry_mismatch(self):
        with self.assertRaisesRegex(ValueError, "is active"):
            self.run_main(write_session(self.root, "s", self.references, geometry="20261007T232759_geometry"))
        mixed = [write_session(self.root, "a", self.references),
                 write_session(self.root, "b", self.references, geometry="20261007T232759_geometry")]
        with self.assertRaisesRegex(ValueError, "different geometry"):
            self.run_main(*mixed)
        self.assertEqual([item["version"] for item in self.store.versions("vial")], ["20261007T001334_vial"])

    def test_degenerate_data_is_rejected(self):
        with self.assertRaises(ValueError):
            fit_vial.fit([{"offset_div": 1., "reference_deg": .001}] * 3)


if __name__ == "__main__":
    unittest.main()
