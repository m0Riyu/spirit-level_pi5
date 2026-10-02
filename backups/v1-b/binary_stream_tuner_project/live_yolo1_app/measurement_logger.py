"""Per-run CSV logging for live tick measurements.

The logger only serializes completed frame results. Camera capture, binary
processing, tick detection, and offline statistics remain in their own modules.
"""

import csv
import json
from datetime import datetime
from pathlib import Path

import config


MAX_MEASUREMENT_ROWS = 10000
VALID_MODES = {"append-only", "circular"}

COMMON_FIELDS = ["run_id", "frame_id", "measurement_index", "datetime"]

FRAME_SUMMARY_FIELDS = [
    *COMMON_FIELDS,
    "status",
    "failure_reason",
    "warning_text",
    "capture_ms",
    "binary_ms",
    "measurement_ms",
    "total_processing_ms",
    "record_mode",
    "logger_mode",
    "image_width",
    "image_height",
    "axis_y",
    "axis_half_height",
    "tick_band_y1",
    "tick_band_y2",
    "logo_x1",
    "logo_x2",
    "logo_source",
    "candidate_count",
    "left_candidate_count",
    "right_candidate_count",
    "valid_tick_count",
    "invalid_tick_count",
    "left_reference_x",
    "right_reference_x",
    "reference_midpoint_x",
    "reference_span_px",
    "inner_center_x",
    "all_pairs_center_x",
    "pair_center_std_px",
    "left_pitch_px_per_div",
    "right_pitch_px_per_div",
    "global_pitch_px_per_div",
    "left_gap_mean_px",
    "left_gap_std_px",
    "left_gap_cv",
    "right_gap_mean_px",
    "right_gap_std_px",
    "right_gap_cv",
    "all_gap_mean_px",
    "all_gap_std_px",
    "left_right_gap_abs_diff_mean_px",
    "left_right_gap_abs_diff_max_px",
    "axis_centroid_offset_mean_px",
    "axis_centroid_offset_std_px",
    "camera_ae_enable",
    "camera_awb_enable",
    "camera_awb_mode",
    "camera_frame_duration_limits",
    "camera_exposure_time",
    "camera_analogue_gain",
    "camera_brightness",
    "camera_contrast",
    "camera_lens_position",
    "camera_settings_json",
]

TICK_POSITION_FIELDS = [
    *COMMON_FIELDS,
    "tick_id",
    "region",
    "x_at_axis",
    "x_full_centroid",
    "pair_center_x",
    "x_start",
    "x_end",
    "width",
    "vertical_score",
    "total_dark_pixels",
    "is_long",
    "selected_reference",
    "valid",
    "invalid_reason",
]

TICK_GAP_FIELDS = [
    *COMMON_FIELDS,
    "gap_id",
    "region",
    "inner_tick_id",
    "outer_tick_id",
    "gap_px",
    "valid",
    "invalid_reason",
]

PAIR_CENTER_FIELDS = [
    *COMMON_FIELDS,
    "tick_id",
    "left_x_at_axis",
    "right_x_at_axis",
    "pair_center_x",
    "valid",
    "invalid_reason",
]

CSV_SCHEMAS = {
    "frame_summary.csv": FRAME_SUMMARY_FIELDS,
    "tick_positions.csv": TICK_POSITION_FIELDS,
    "tick_gaps.csv": TICK_GAP_FIELDS,
    "pair_centers.csv": PAIR_CENTER_FIELDS,
}


def _pitch(measurement, region):
    return measurement.get(f"{region}_pitch", {}).get("pitch_px")


def _stat(measurement, section, field):
    return (
        measurement.get("spatial_consistency", {})
        .get(section, {})
        .get(field)
    )


def _common_row(frame_metadata, measurement_index):
    return {
        "run_id": frame_metadata["run_id"],
        "frame_id": frame_metadata["frame_id"],
        "measurement_index": measurement_index,
        "datetime": frame_metadata["datetime"],
    }


def _frame_summary_row(measurement, frame_metadata, measurement_index, mode):
    common = _common_row(frame_metadata, measurement_index)
    logo = measurement.get("logo_exclusion", {})
    tick_band = measurement.get("tick_band", {})
    consistency = measurement.get("spatial_consistency", {})
    camera = frame_metadata.get("camera_settings", {})
    gap_difference = consistency.get(
        "left_right_gap_absolute_difference", {}
    )

    return {
        **common,
        "status": measurement.get("status", "FAIL"),
        "failure_reason": "; ".join(measurement.get("errors", [])),
        "warning_text": "; ".join(measurement.get("warnings", [])),
        "capture_ms": frame_metadata.get("capture_ms"),
        "binary_ms": frame_metadata.get("binary_ms"),
        "measurement_ms": frame_metadata.get("measurement_ms"),
        "total_processing_ms": frame_metadata.get("total_processing_ms"),
        "record_mode": frame_metadata.get("record_mode"),
        "logger_mode": mode,
        "image_width": measurement.get("image_width"),
        "image_height": measurement.get("image_height"),
        "axis_y": measurement.get("axis_y"),
        "axis_half_height": measurement.get("axis_half_height"),
        "tick_band_y1": tick_band.get("y1"),
        "tick_band_y2": tick_band.get("y2"),
        "logo_x1": logo.get("x1"),
        "logo_x2": logo.get("x2"),
        "logo_source": logo.get("source"),
        "candidate_count": measurement.get("candidate_count"),
        "left_candidate_count": measurement.get("left_candidate_count"),
        "right_candidate_count": measurement.get("right_candidate_count"),
        "valid_tick_count": consistency.get("valid_tick_count"),
        "invalid_tick_count": consistency.get("invalid_tick_count"),
        "left_reference_x": measurement.get("left_reference_x"),
        "right_reference_x": measurement.get("right_reference_x"),
        "reference_midpoint_x": measurement.get("reference_midpoint_x"),
        "reference_span_px": measurement.get("reference_span_px"),
        "inner_center_x": measurement.get("inner_center_x"),
        "all_pairs_center_x": measurement.get("all_pairs_center_x"),
        "pair_center_std_px": measurement.get("pair_center_std_px"),
        "left_pitch_px_per_div": _pitch(measurement, "left"),
        "right_pitch_px_per_div": _pitch(measurement, "right"),
        "global_pitch_px_per_div": _pitch(measurement, "global"),
        "left_gap_mean_px": _stat(measurement, "left_gaps", "mean_px"),
        "left_gap_std_px": _stat(measurement, "left_gaps", "std_px"),
        "left_gap_cv": _stat(
            measurement, "left_gaps", "coefficient_of_variation"
        ),
        "right_gap_mean_px": _stat(measurement, "right_gaps", "mean_px"),
        "right_gap_std_px": _stat(measurement, "right_gaps", "std_px"),
        "right_gap_cv": _stat(
            measurement, "right_gaps", "coefficient_of_variation"
        ),
        "all_gap_mean_px": _stat(measurement, "all_gaps", "mean_px"),
        "all_gap_std_px": _stat(measurement, "all_gaps", "std_px"),
        "left_right_gap_abs_diff_mean_px": gap_difference.get("mean_px"),
        "left_right_gap_abs_diff_max_px": gap_difference.get("max_px"),
        "axis_centroid_offset_mean_px": _stat(
            measurement, "axis_minus_full_centroid", "mean_px"
        ),
        "axis_centroid_offset_std_px": _stat(
            measurement, "axis_minus_full_centroid", "std_px"
        ),
        "camera_ae_enable": camera.get("AeEnable"),
        "camera_awb_enable": camera.get("AwbEnable"),
        "camera_awb_mode": camera.get("AwbMode"),
        "camera_frame_duration_limits": json.dumps(
            camera.get("FrameDurationLimits"), ensure_ascii=False
        ),
        "camera_exposure_time": camera.get("ExposureTime"),
        "camera_analogue_gain": camera.get("AnalogueGain"),
        "camera_brightness": camera.get("Brightness"),
        "camera_contrast": camera.get("Contrast"),
        "camera_lens_position": camera.get("LensPosition"),
        "camera_settings_json": json.dumps(camera, ensure_ascii=False),
    }


class CircularMeasurementCsvLogger:
    """Write four per-run CSV datasets in append-only or circular mode."""

    def __init__(
        self,
        run_id=None,
        base_directory=None,
        mode="append-only",
        max_rows=MAX_MEASUREMENT_ROWS,
        flush_every=None,
    ):
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
        self.mode = mode
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"Unsupported logger mode {mode!r}; choose {sorted(VALID_MODES)}"
            )
        self.max_rows = int(max_rows)
        if self.max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self.flush_every = int(
            flush_every or config.MEASUREMENT_FLUSH_EVERY
        )
        if self.flush_every <= 0:
            raise ValueError("flush_every must be positive")

        root = Path(base_directory or config.MEASUREMENT_RUN_DIRECTORY)
        self.run_directory = root / self.run_id
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.paths = {
            name: self.run_directory / name for name in CSV_SCHEMAS
        }
        # Backward-compatible attribute: the former logger exposed .path.
        self.path = self.paths["frame_summary.csv"]

        self.files = {}
        self.writers = {}
        self.measurement_index = 0
        if self.mode == "append-only" and self.path.is_file():
            with self.path.open(newline="", encoding="utf-8") as existing_file:
                self.measurement_index = sum(
                    1 for _row in csv.DictReader(existing_file)
                )
        self.write_count = self.measurement_index
        self.circular_cycle = 1
        self.closed = False
        self._open_files(mode="a" if self.mode == "append-only" else "w")

    def _open_files(self, mode):
        for name, fields in CSV_SCHEMAS.items():
            path = self.paths[name]
            file_exists_with_data = path.exists() and path.stat().st_size > 0
            file = open(path, mode, newline="", encoding="utf-8")
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            if mode == "w" or not file_exists_with_data:
                writer.writeheader()
            self.files[name] = file
            self.writers[name] = writer
        self.flush()

    def _restart_circular_cycle(self):
        self._close_files()
        self.circular_cycle += 1
        self.measurement_index = 0
        self._open_files(mode="w")

    def write(self, measurement, frame_metadata):
        if self.closed:
            raise ValueError("Cannot write to a closed measurement logger")
        if self.mode == "circular" and self.measurement_index >= self.max_rows:
            self._restart_circular_cycle()

        self.measurement_index += 1
        common = _common_row(frame_metadata, self.measurement_index)
        self.writers["frame_summary.csv"].writerow(
            _frame_summary_row(
                measurement,
                frame_metadata,
                self.measurement_index,
                self.mode,
            )
        )

        for candidate in measurement.get("candidates", []):
            self.writers["tick_positions.csv"].writerow(
                {
                    **common,
                    "tick_id": candidate.get("tick_id"),
                    "region": candidate.get("side"),
                    "x_at_axis": candidate.get("x_at_axis"),
                    "x_full_centroid": candidate.get("x_full_centroid"),
                    "pair_center_x": candidate.get("pair_center_x"),
                    "x_start": candidate.get("x_start"),
                    "x_end": candidate.get("x_end"),
                    "width": candidate.get("width"),
                    "vertical_score": candidate.get("vertical_score"),
                    "total_dark_pixels": candidate.get("total_dark_pixels"),
                    "is_long": candidate.get("is_long"),
                    "selected_reference": candidate.get("selected_reference"),
                    "valid": candidate.get("valid"),
                    "invalid_reason": candidate.get("invalid_reason"),
                }
            )

        for region in ("left", "right"):
            for gap in measurement.get("gaps", {}).get(region, []):
                self.writers["tick_gaps.csv"].writerow({**common, **gap})

        for pair in measurement.get("pair_centers", []):
            self.writers["pair_centers.csv"].writerow({**common, **pair})

        self.write_count += 1
        if self.write_count % self.flush_every == 0:
            self.flush()
        return self.measurement_index

    def flush(self):
        for file in self.files.values():
            file.flush()

    def _close_files(self):
        self.flush()
        for file in self.files.values():
            file.close()
        self.files.clear()
        self.writers.clear()

    def close(self):
        if self.closed:
            return
        self._close_files()
        self.closed = True


def _format_number(value):
    return "N/A" if value is None else f"{value:.3f}"


def print_measurement(frame_id, measurement):
    """Print the concise values exposed by tick_scale_calibration.py."""
    logo = measurement.get("logo_exclusion", {})
    print(
        f"frame={frame_id:06d} | "
        f"status={measurement.get('status', 'FAIL')} | "
        f"logo={logo.get('x1')}..{logo.get('x2')}"
        f"({logo.get('source', 'unknown')}) | "
        f"ticks=L{measurement.get('left_candidate_count', 0)}/"
        f"R{measurement.get('right_candidate_count', 0)} | "
        f"ref=L{_format_number(measurement.get('left_reference_x'))}/"
        f"R{_format_number(measurement.get('right_reference_x'))} | "
        f"inner={_format_number(measurement.get('inner_center_x'))} | "
        f"all_center={_format_number(measurement.get('all_pairs_center_x'))} | "
        f"pair_std={_format_number(measurement.get('pair_center_std_px'))} | "
        f"pitch=L{_format_number(_pitch(measurement, 'left'))}/"
        f"R{_format_number(_pitch(measurement, 'right'))}/"
        f"G{_format_number(_pitch(measurement, 'global'))} px/div"
    )
    for warning in measurement.get("warnings", []):
        print(f"frame={frame_id:06d} | WARNING: {warning}")
    for error in measurement.get("errors", []):
        print(f"frame={frame_id:06d} | ERROR: {error}")
