"""YOLO model loading, inference, and detection parsing."""

import time
from dataclasses import dataclass

from ultralytics import YOLO

import config


@dataclass
class Detection:
    detected: int = 0
    detection_count: int = 0
    class_id: object = ""
    class_name: str = ""
    confidence: object = ""
    all_confidences: str = ""
    x1_roi: object = ""
    y1_roi: object = ""
    x2_roi: object = ""
    y2_roi: object = ""
    center_x_roi: object = ""
    center_y_roi: object = ""
    x1_full: object = ""
    y1_full: object = ""
    x2_full: object = ""
    y2_full: object = ""
    center_x_full: object = ""
    center_y_full: object = ""
    box_width: object = ""
    box_height: object = ""

    def as_dict(self):
        return vars(self).copy()


@dataclass
class Prediction:
    result: object
    detection: Detection
    predict_ms: float
    preprocess_ms: float
    inference_ms: float
    postprocess_ms: float


class YoloDetector:
    def __init__(self):
        print(f"正在載入模型：{config.MODEL_PATH}")
        self.model = YOLO(str(config.MODEL_PATH))
        print("模型載入完成。")

    def predict(self, roi):
        predict_start = time.perf_counter()
        results = self.model.predict(
            source=roi,
            imgsz=config.MODEL_IMAGE_SIZE,
            conf=config.CONFIDENCE_THRESHOLD,
            verbose=False,
        )
        predict_ms = (time.perf_counter() - predict_start) * 1000.0

        result = results[0]
        speed = result.speed or {}
        detection = self._get_best_detection(result.boxes)

        return Prediction(
            result=result,
            detection=detection,
            predict_ms=predict_ms,
            preprocess_ms=speed.get("preprocess", 0.0),
            inference_ms=speed.get("inference", 0.0),
            postprocess_ms=speed.get("postprocess", 0.0),
        )

    def _get_best_detection(self, boxes):
        if boxes is None or len(boxes) == 0:
            return Detection()

        confidences = boxes.conf.cpu().numpy()
        best_index = int(confidences.argmax())
        coordinates = boxes.xyxy[best_index].cpu().numpy()

        x1_roi = float(coordinates[0])
        y1_roi = float(coordinates[1])
        x2_roi = float(coordinates[2])
        y2_roi = float(coordinates[3])
        center_x_roi = (x1_roi + x2_roi) / 2.0
        center_y_roi = (y1_roi + y2_roi) / 2.0
        class_id = int(boxes.cls[best_index].item())

        return Detection(
            detected=1,
            detection_count=len(boxes),
            class_id=class_id,
            class_name=self.model.names[class_id],
            confidence=float(confidences[best_index]),
            all_confidences=";".join(f"{value:.4f}" for value in confidences),
            x1_roi=x1_roi,
            y1_roi=y1_roi,
            x2_roi=x2_roi,
            y2_roi=y2_roi,
            center_x_roi=center_x_roi,
            center_y_roi=center_y_roi,
            x1_full=x1_roi + config.ROI_X1,
            y1_full=y1_roi + config.ROI_Y1,
            x2_full=x2_roi + config.ROI_X1,
            y2_full=y2_roi + config.ROI_Y1,
            center_x_full=center_x_roi + config.ROI_X1,
            center_y_full=center_y_roi + config.ROI_Y1,
            box_width=x2_roi - x1_roi,
            box_height=y2_roi - y1_roi,
        )
