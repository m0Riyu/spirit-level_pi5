#!/usr/bin/env python3
"""Offline statistics for per-run tick measurement CSV datasets.

Examples:
    python3 analyze_tick_measurements.py logs/tick_measurement_runs

    python3 analyze_tick_measurements.py run_dir_a run_dir_b \
        --manual-annotations manual_ticks.csv --output-dir analysis_output

Manual annotation CSV columns:
    run_id,frame_id,tick_id,region,manual_x
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


RUN_FILES = (
    "frame_summary.csv",
    "tick_positions.csv",
    "tick_gaps.csv",
    "pair_centers.csv",
)


def as_float(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> Optional[int]:
    number = as_float(value)
    return None if number is None else int(number)


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def describe(values: Iterable[float]) -> dict[str, Any]:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "std": None,
            "min": None,
            "max": None,
            "range": None,
            "coefficient_of_variation": None,
        }
    mean = statistics.fmean(numbers)
    minimum = min(numbers)
    maximum = max(numbers)
    std = statistics.pstdev(numbers)
    return {
        "count": len(numbers),
        "mean": mean,
        "median": statistics.median(numbers),
        "std": std,
        "min": minimum,
        "max": maximum,
        "range": maximum - minimum,
        "coefficient_of_variation": std / mean if mean != 0 else None,
    }


def prefixed(stats: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {f"{prefix}_{name}": value for name, value in stats.items()}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def discover_run_directories(inputs: list[Path]) -> list[Path]:
    run_directories: set[Path] = set()
    for input_path in inputs:
        path = input_path.expanduser().resolve()
        if path.is_file() and path.name == "frame_summary.csv":
            run_directories.add(path.parent)
        elif path.is_dir() and all((path / name).is_file() for name in RUN_FILES):
            run_directories.add(path)
        elif path.is_dir():
            for summary_path in path.rglob("frame_summary.csv"):
                candidate = summary_path.parent
                if all((candidate / name).is_file() for name in RUN_FILES):
                    run_directories.add(candidate)
        else:
            raise FileNotFoundError(f"Input path does not exist: {path}")
    return sorted(run_directories)


def load_runs(run_directories: list[Path]):
    runs = []
    for directory in run_directories:
        metadata_path = directory / "session_metadata.json"
        metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata_path.is_file()
            else {}
        )
        frames = read_csv(directory / "frame_summary.csv")
        run_id = metadata.get("run_id")
        if not run_id and frames:
            run_id = frames[0].get("run_id")
        run_id = run_id or directory.name
        runs.append(
            {
                "run_id": run_id,
                "directory": directory,
                "metadata": metadata,
                "frames": frames,
                "ticks": read_csv(directory / "tick_positions.csv"),
                "gaps": read_csv(directory / "tick_gaps.csv"),
                "pairs": read_csv(directory / "pair_centers.csv"),
            }
        )
    return runs


def gap_time_stability(runs):
    groups = defaultdict(list)
    for run in runs:
        for row in run["gaps"]:
            gap_px = as_float(row.get("gap_px"))
            if is_true(row.get("valid")) and gap_px is not None:
                key = (run["run_id"], row["region"], int(row["gap_id"]))
                groups[key].append(gap_px)

    output = []
    for (run_id, region, gap_id), values in sorted(groups.items()):
        output.append(
            {
                "run_id": run_id,
                "region": region,
                "gap_id": gap_id,
                **prefixed(describe(values), "gap_px"),
            }
        )
    return output


def gap_spatial_consistency(runs):
    groups = defaultdict(list)
    datetimes = {}
    for run in runs:
        for row in run["gaps"]:
            gap_px = as_float(row.get("gap_px"))
            if is_true(row.get("valid")) and gap_px is not None:
                key = (run["run_id"], int(row["frame_id"]), row["region"])
                groups[key].append(gap_px)
                datetimes[key] = row["datetime"]

    output = []
    for (run_id, frame_id, region), values in sorted(groups.items()):
        output.append(
            {
                "run_id": run_id,
                "frame_id": frame_id,
                "datetime": datetimes[(run_id, frame_id, region)],
                "region": region,
                **prefixed(describe(values), "gap_px"),
            }
        )
    return output


def left_right_gap_consistency(runs):
    matched = defaultdict(dict)
    for run in runs:
        for row in run["gaps"]:
            if not is_true(row.get("valid")):
                continue
            gap_px = as_float(row.get("gap_px"))
            if gap_px is None:
                continue
            key = (run["run_id"], int(row["frame_id"]), int(row["gap_id"]))
            matched[key][row["region"]] = gap_px

    groups = defaultdict(list)
    for (run_id, _frame_id, gap_id), sides in matched.items():
        if "left" in sides and "right" in sides:
            difference = sides["left"] - sides["right"]
            groups[(run_id, gap_id)].append(
                (sides["left"], sides["right"], difference)
            )

    output = []
    for (run_id, gap_id), values in sorted(groups.items()):
        left_values = [item[0] for item in values]
        right_values = [item[1] for item in values]
        differences = [item[2] for item in values]
        output.append(
            {
                "run_id": run_id,
                "gap_id": gap_id,
                "sample_count": len(values),
                "left_mean_px": statistics.fmean(left_values),
                "right_mean_px": statistics.fmean(right_values),
                "signed_difference_mean_px": statistics.fmean(differences),
                "signed_difference_std_px": statistics.pstdev(differences),
                "absolute_difference_mean_px": statistics.fmean(
                    abs(value) for value in differences
                ),
                "absolute_difference_max_px": max(
                    abs(value) for value in differences
                ),
            }
        )
    return output


def pair_center_time_stability(runs):
    groups = defaultdict(list)
    for run in runs:
        for row in run["pairs"]:
            center = as_float(row.get("pair_center_x"))
            if is_true(row.get("valid")) and center is not None:
                groups[(run["run_id"], int(row["tick_id"]))].append(center)

    return [
        {
            "run_id": run_id,
            "tick_id": tick_id,
            **prefixed(describe(values), "pair_center_x"),
        }
        for (run_id, tick_id), values in sorted(groups.items())
    ]


def pair_center_spatial_stability(runs):
    groups = defaultdict(list)
    datetimes = {}
    for run in runs:
        for row in run["pairs"]:
            center = as_float(row.get("pair_center_x"))
            if is_true(row.get("valid")) and center is not None:
                key = (run["run_id"], int(row["frame_id"]))
                groups[key].append(center)
                datetimes[key] = row["datetime"]

    return [
        {
            "run_id": run_id,
            "frame_id": frame_id,
            "datetime": datetimes[(run_id, frame_id)],
            **prefixed(describe(values), "pair_center_x"),
        }
        for (run_id, frame_id), values in sorted(groups.items())
    ]


def run_comparison(runs):
    output = []
    for run in runs:
        frames = run["frames"]
        statuses = [row.get("status") for row in frames]
        metadata = run["metadata"]
        row = {
            "run_id": run["run_id"],
            "run_directory": str(run["directory"]),
            "experiment_label": metadata.get("experiment_label", ""),
            "setup_label": metadata.get("setup_label", ""),
            "lighting_label": metadata.get("lighting_label", ""),
            "started_datetime": metadata.get("started_datetime", ""),
            "ended_datetime": metadata.get("ended_datetime", ""),
            "frame_count": len(frames),
            "pass_count": statuses.count("PASS"),
            "warn_count": statuses.count("WARN"),
            "fail_count": statuses.count("FAIL"),
        }
        for column, prefix in (
            ("global_pitch_px_per_div", "global_pitch_px_per_div"),
            ("all_pairs_center_x", "all_pairs_center_x"),
            ("pair_center_std_px", "pair_center_std_px"),
            ("left_gap_mean_px", "left_gap_mean_px"),
            ("right_gap_mean_px", "right_gap_mean_px"),
            ("total_processing_ms", "total_processing_ms"),
        ):
            values = [as_float(frame.get(column)) for frame in frames]
            row.update(prefixed(describe(values), prefix))
        output.append(row)
    return output


def condition_comparison(runs):
    groups = defaultdict(list)
    for run in runs:
        metadata = run["metadata"]
        key = (
            metadata.get("experiment_label", ""),
            metadata.get("setup_label", ""),
            metadata.get("lighting_label", ""),
        )
        groups[key].append(run)

    output = []
    for labels, grouped_runs in sorted(groups.items()):
        frames = [frame for run in grouped_runs for frame in run["frames"]]
        row = {
            "experiment_label": labels[0],
            "setup_label": labels[1],
            "lighting_label": labels[2],
            "run_count": len(grouped_runs),
            "frame_count": len(frames),
            "run_ids": ";".join(run["run_id"] for run in grouped_runs),
        }
        for column in (
            "global_pitch_px_per_div",
            "all_pairs_center_x",
            "pair_center_std_px",
        ):
            row.update(
                prefixed(
                    describe(as_float(frame.get(column)) for frame in frames),
                    column,
                )
            )
        output.append(row)
    return output


def compare_manual_annotations(runs, annotation_path: Optional[Path]):
    if annotation_path is None:
        return []
    annotations = read_csv(annotation_path.expanduser().resolve())
    required = {"run_id", "frame_id", "tick_id", "region", "manual_x"}
    if annotations and not required.issubset(annotations[0]):
        missing = sorted(required.difference(annotations[0]))
        raise ValueError(f"Manual annotation CSV missing columns: {missing}")

    detected = {}
    for run in runs:
        for row in run["ticks"]:
            tick_id = as_int(row.get("tick_id"))
            if tick_id is None or not is_true(row.get("valid")):
                continue
            key = (
                run["run_id"],
                int(row["frame_id"]),
                tick_id,
                row["region"],
            )
            detected[key] = as_float(row.get("x_at_axis"))

    output = []
    for annotation in annotations:
        key = (
            annotation["run_id"],
            int(annotation["frame_id"]),
            int(annotation["tick_id"]),
            annotation["region"],
        )
        manual_x = as_float(annotation["manual_x"])
        detected_x = detected.get(key)
        error = (
            detected_x - manual_x
            if detected_x is not None and manual_x is not None
            else None
        )
        output.append(
            {
                "run_id": key[0],
                "frame_id": key[1],
                "tick_id": key[2],
                "region": key[3],
                "manual_x": manual_x,
                "x_at_axis": detected_x,
                "error_px": error,
                "absolute_error_px": None if error is None else abs(error),
                "matched": detected_x is not None,
            }
        )
    return output


def summarize_manual_annotations(comparisons):
    groups = defaultdict(list)
    for row in comparisons:
        error = as_float(row.get("error_px"))
        if error is not None:
            groups[(row["run_id"], row["region"], row["tick_id"])].append(
                error
            )
    return [
        {
            "run_id": run_id,
            "region": region,
            "tick_id": tick_id,
            **prefixed(describe(errors), "signed_error_px"),
            **prefixed(describe(abs(error) for error in errors), "absolute_error_px"),
        }
        for (run_id, region, tick_id), errors in sorted(groups.items())
    ]


def write_rows(path: Path, rows: list[dict[str, Any]], fieldnames):
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fieldnames_for(rows, fallback):
    if not rows:
        return fallback
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    return fields


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Offline stability analysis for tick measurement runs."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        default=[Path("logs/tick_measurement_runs")],
        help="Run directories or a parent directory containing runs",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("tick_analysis"))
    parser.add_argument("--manual-annotations", type=Path, default=None)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    run_directories = discover_run_directories(args.inputs)
    if not run_directories:
        raise SystemExit("No measurement run directories were found.")
    runs = load_runs(run_directories)

    outputs = {
        "gap_time_stability.csv": gap_time_stability(runs),
        "gap_spatial_consistency.csv": gap_spatial_consistency(runs),
        "left_right_gap_consistency.csv": left_right_gap_consistency(runs),
        "pair_center_time_stability.csv": pair_center_time_stability(runs),
        "pair_center_spatial_stability.csv": pair_center_spatial_stability(runs),
        "run_comparison.csv": run_comparison(runs),
        "condition_comparison.csv": condition_comparison(runs),
    }
    manual_rows = compare_manual_annotations(runs, args.manual_annotations)
    if args.manual_annotations is not None:
        outputs["manual_annotation_comparison.csv"] = manual_rows
        outputs["manual_annotation_summary.csv"] = summarize_manual_annotations(
            manual_rows
        )

    fallback_fields = {
        "gap_time_stability.csv": ["run_id", "region", "gap_id"],
        "gap_spatial_consistency.csv": [
            "run_id", "frame_id", "datetime", "region"
        ],
        "left_right_gap_consistency.csv": ["run_id", "gap_id"],
        "pair_center_time_stability.csv": ["run_id", "tick_id"],
        "pair_center_spatial_stability.csv": [
            "run_id", "frame_id", "datetime"
        ],
        "run_comparison.csv": ["run_id"],
        "condition_comparison.csv": [
            "experiment_label", "setup_label", "lighting_label"
        ],
        "manual_annotation_comparison.csv": [
            "run_id", "frame_id", "tick_id", "region", "manual_x",
            "x_at_axis", "error_px", "absolute_error_px", "matched"
        ],
        "manual_annotation_summary.csv": [
            "run_id", "region", "tick_id"
        ],
    }

    output_directory = args.output_dir.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, rows in outputs.items():
        write_rows(
            output_directory / name,
            rows,
            fieldnames_for(rows, fallback_fields[name]),
        )

    summary = {
        "run_count": len(runs),
        "run_ids": [run["run_id"] for run in runs],
        "output_directory": str(output_directory),
        "row_counts": {name: len(rows) for name, rows in outputs.items()},
    }
    (output_directory / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Analyzed runs: {len(runs)}")
    for run in runs:
        print(f"  {run['run_id']}: {run['directory']}")
    print(f"Output: {output_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
