"""Full-frame camera calibration shared by the independent entry points."""

import math

import cv2
import numpy as np


def load_calibration(path, image_size):
    with np.load(path, allow_pickle=False) as data:
        matrix = np.asarray(data["camera_matrix"], dtype=np.float64)
        distortion = np.asarray(data["dist_coeffs"], dtype=np.float64).reshape(-1)
        if "image_width" in data and "image_height" in data:
            calibrated_size = (int(data["image_width"].item()), int(data["image_height"].item()))
        elif "image_size" in data:
            calibrated_size = tuple(int(value) for value in data["image_size"].reshape(-1))
        else:
            raise ValueError("Calibration NPZ must record its image width and height.")
    if calibrated_size != tuple(image_size):
        raise ValueError(f"Calibration size {calibrated_size} differs from full frame {image_size}.")
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or matrix[0, 0] <= 0 or matrix[1, 1] <= 0
            or not np.allclose(matrix[2], [0, 0, 1])
            or distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(distortion).all()):
        raise ValueError("Invalid pinhole calibration matrix or distortion coefficients.")
    return matrix, distortion


class FullFrameUndistorter:
    """Build remap once and keep the full output size, without the crop ROI."""

    def __init__(self, matrix, distortion, image_size, alpha=1.0):
        if not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise ValueError("Undistortion alpha must be between 0 and 1.")
        self.image_size = tuple(image_size)
        self.original_camera_matrix = np.array(matrix, dtype=np.float64, copy=True)
        self.distortion = np.array(distortion, dtype=np.float64, copy=True)
        self.camera_matrix, _ = cv2.getOptimalNewCameraMatrix(
            self.original_camera_matrix, self.distortion,
            self.image_size, alpha, self.image_size,
        )
        self.inverse_camera_matrix = np.linalg.inv(self.camera_matrix)
        self.map_x, self.map_y = cv2.initUndistortRectifyMap(
            self.original_camera_matrix, self.distortion, None,
            self.camera_matrix, self.image_size, cv2.CV_32FC1,
        )

    def process(self, frame):
        if (frame.shape[1], frame.shape[0]) != self.image_size:
            raise ValueError("Frame size differs from calibrated full-frame size.")
        return cv2.remap(frame, self.map_x, self.map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def undistort_points(self, points):
        """Convert ORIGINAL full-frame pixels to the same pixels as process()."""
        points = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        # OpenCV 5 moved the criteria argument into undistortPoints itself.
        transform = getattr(cv2, "undistortPointsIter", cv2.undistortPoints)
        return transform(
            points, self.original_camera_matrix, self.distortion,
            R=None, P=self.camera_matrix,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 1e-10),
        ).reshape(-1, 2)

    def distort_points(self, points):
        """Map rectified full-frame pixels back to the source calibration."""
        points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        rays = np.column_stack((points, np.ones(len(points)))) @ self.inverse_camera_matrix.T
        pixels, _ = cv2.projectPoints(
            rays, np.zeros(3), np.zeros(3),
            self.original_camera_matrix, self.distortion,
        )
        return pixels.reshape(-1, 2)
