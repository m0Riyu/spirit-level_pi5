"""One-time creation of the first versioned calibration files.

    python migrate_calibration.py geometry [--source TICK_JSON]
    python migrate_calibration.py vial --summary ../calibration_test/output/summary.json

geometry: legacy binary_*_tick_measurement.json -> degree-1 geometry whose
          output equals the old (x - reference_midpoint_x) / pitch.
vial:     gain and zero offset fitted by calibration_test/run_log_test.py.
Both write a new version and make it active; existing files are never edited.
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import config
from calibration_store import TIMEZONE, CalibrationStore, new_version
from geometry_calibration import legacy_tick_geometry

LEGACY_TICK_MEASUREMENT = (
    config.PROJECT_DIRECTORY / "binary_stream_tuner_project" / "binary_captures"
    / "binary_20261002_072923_517806_tick_measurement.json"
)


def geometry_record(source, previous_version=None):
    source = Path(source).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    match = re.search(r"(\d{8})_(\d{6})", source.name)
    if match:
        stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=TIMEZONE)
    else:
        stamp = datetime.fromisoformat(data["created_utc"]).astimezone(TIMEZONE)
    return legacy_tick_geometry(
        data, version=new_version("geometry", stamp), created_at_iso=stamp.isoformat(timespec="seconds"),
        source_path=source, previous_version=previous_version,
    )


def vial_record(summary_path, previous_version=None, now=None):
    summary_path = Path(summary_path).resolve()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    fit = summary["calibration_all_points"]["yolo"]
    now = now or datetime.now(TIMEZONE)
    return {
        "schema_version": 1,
        "version": new_version("vial", now),
        "created_at_iso": now.isoformat(timespec="seconds"),
        "mm_per_m_per_div": fit["mm_per_m_per_div"],
        "zero_offset_div": fit["zero_offset_div"],
        "fit_method": "least squares: reference_mm_per_m = mm_per_m_per_div * (offset_div - zero_offset_div); "
                      "offset_div from YOLO box center, degree-1 geometry",
        "reference_instrument": "LEVELNIC DL-S4W (CNC A axis, 1:1)",
        "source_session": summary["session"],
        "source_summary": str(summary_path),
        "labeled_captures": summary["labeled_captures"],
        "reference_angles": summary["reference_angles"],
        "cross_validation_leave_one_angle_out": summary["method_B_yolo_calibrated_cv"],
        "nominal_system_error": summary["method_A_current_system"],
        "previous_version": previous_version,
    }


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("geometry", "vial"))
    parser.add_argument("--source", default=LEGACY_TICK_MEASUREMENT, help="legacy tick measurement JSON")
    parser.add_argument("--summary", help="run_log_test.py output/summary.json")
    parser.add_argument("--root", default=config.CALIBRATION_DIRECTORY, help="calibration directory")
    options = parser.parse_args(arguments)
    store = CalibrationStore(options.root)
    previous = store.active_state(options.kind)["version"]
    if options.kind == "geometry":
        record = geometry_record(options.source, previous)
    else:
        if not options.summary:
            parser.error("vial needs --summary")
        record = vial_record(options.summary, previous)
    path = store.save(options.kind, record)
    print(f"已建立並啟用 {options.kind} 版本 {record['version']}：{path}")
    return path


if __name__ == "__main__":
    main()
