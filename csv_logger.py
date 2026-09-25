"""CSV file creation and buffered metric logging."""

import csv
from datetime import datetime

import config


CSV_FIELDS = [
    "frame_id",
    "datetime",
    "detected",
    "detection_count",
    "class_id",
    "class_name",
    "confidence",
    "all_confidences",
    "x1_roi",
    "y1_roi",
    "x2_roi",
    "y2_roi",
    "center_x_roi",
    "center_y_roi",
    "x1_full",
    "y1_full",
    "x2_full",
    "y2_full",
    "center_x_full",
    "center_y_full",
    "box_width",
    "box_height",
    "measurement_valid",
    "measurement_error",
    "bubble_center_x_roi",
    "scale_center_x_roi",
    "pitch_px_per_div",
    "bubble_offset_px",
    "bubble_offset_div",
    "bubble_absolute_offset_div",
    "bubble_direction",
    "calibration_status",
    "calibration_created_utc",
    "calibration_source",
    "capture_ms",
    "predict_ms",
    "yolo_preprocess_ms",
    "yolo_inference_ms",
    "yolo_postprocess_ms",
    "plot_ms",
    "process_ms",
    "fps",
    "confidence_threshold",
    "model_height",
    "model_width",
    "roi_x1",
    "roi_y1",
    "roi_x2",
    "roi_y2",
]


class CsvLogger:
    def __init__(self):
        config.LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        log_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = config.LOG_DIRECTORY / f"yolo_128x608_metrics_{log_time}.csv"
        self.temporary_path = self.path.with_suffix(".csv.part")
        self.file = open(
            self.temporary_path, mode="w", newline="", encoding="utf-8"
        )
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_FIELDS)
        self.writer.writeheader()
        self.closed = False

    def write(self, frame_id, detection, measurement, timings):
        row = {
            "frame_id": frame_id,
            "datetime": datetime.now().isoformat(timespec="milliseconds"),
            **detection.as_dict(),
            **measurement.as_dict(),
            **timings,
            "confidence_threshold": config.CONFIDENCE_THRESHOLD,
            "model_height": config.MODEL_IMAGE_SIZE[0],
            "model_width": config.MODEL_IMAGE_SIZE[1],
            "roi_x1": config.ROI_X1,
            "roi_y1": config.ROI_Y1,
            "roi_x2": config.ROI_X2,
            "roi_y2": config.ROI_Y2,
        }
        self.writer.writerow(row)

        if frame_id % config.FLUSH_EVERY == 0:
            self.file.flush()

    def close(self):
        if self.closed:
            return
        self.file.flush()
        self.file.close()
        self.closed = True

    def save(self):
        """Close the temporary log and promote it to the final CSV path."""
        self.close()
        self.temporary_path.replace(self.path)
        return self.path

    def discard(self):
        """Close and remove the temporary log."""
        self.close()
        self.temporary_path.unlink(missing_ok=True)
