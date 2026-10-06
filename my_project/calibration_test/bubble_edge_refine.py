"""Sub-pixel bubble center refinement from the bright meniscus ring.

YOLO gives a coarse box. The bubble ends are a faint bright ring that is
often crossed by dark tick lines, so ticks are first removed with a
horizontal grayscale closing. Each end is then fitted with a circle using
radial ridge samples and RANSAC; the bubble center is the midpoint of the
two ring tips.
"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class EndFit:
    center_x: float
    center_y: float
    radius: float
    tip_x: float
    inliers: int
    samples: int
    rms_px: float


@dataclass(frozen=True)
class RefinedBubble:
    valid: bool
    center_x: float
    length_px: float
    left: EndFit | None
    right: EndFit | None
    error: str = ""


def ridge_image(roi_bgr):
    """Return an image where the bright ring is a ridge and ticks are gone."""
    green = roi_bgr[:, :, 1]
    # Dark ticks are about 2-4 px wide; a 1x9 closing fills them in.
    no_ticks = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((1, 9), np.uint8))
    smooth = cv2.GaussianBlur(no_ticks, (0, 0), 1.0).astype(np.float32)
    background = cv2.GaussianBlur(smooth, (0, 0), 6.0)
    return smooth - background


def _sample_bilinear(image, xs, ys):
    return cv2.remap(image, xs.astype(np.float32), ys.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _fit_circle(points):
    x, y = points[:, 0], points[:, 1]
    a = np.c_[2 * x, 2 * y, np.ones(len(x))]
    b = x * x + y * y
    (cx, cy, c), *_ = np.linalg.lstsq(a, b, rcond=None)
    return cx, cy, float(np.sqrt(max(c + cx * cx + cy * cy, 0.0)))


def _ransac_circle(points, threshold=1.0, iterations=300, seed=0):
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(iterations):
        sample = points[rng.choice(len(points), 3, replace=False)]
        cx, cy, r = _fit_circle(sample)
        distance = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - r)
        inliers = distance < threshold
        if best is None or inliers.sum() > best.sum():
            best = inliers
    cx, cy, r = _fit_circle(points[best])
    for _ in range(2):  # refine on inliers of the refit circle
        distance = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - r)
        best = distance < threshold
        cx, cy, r = _fit_circle(points[best])
    residual = np.hypot(points[best, 0] - cx, points[best, 1] - cy) - r
    return cx, cy, r, best, float(np.sqrt(np.mean(residual ** 2)))


def fit_end(ridge, box_edge_x, center_y, half_height, side,
            angle_span_deg=70, radius_search=(0.55, 1.15)):
    """Fit the ring at one bubble end. side is 'left' or 'right'."""
    radius0 = half_height
    direction = -1.0 if side == "left" else 1.0
    # Ring tip sits a few pixels inside the YOLO box edge.
    cx0 = box_edge_x - direction * radius0
    angles = np.radians(np.arange(-angle_span_deg, angle_span_deg + 1, 2.0))
    radii = np.arange(radius0 * radius_search[0], radius0 * radius_search[1], 0.25)
    points = []
    for angle in angles:
        dx, dy = direction * np.cos(angle), np.sin(angle)
        xs = cx0 + radii * dx
        ys = center_y + radii * dy
        profile = _sample_bilinear(ridge, xs[None, :], ys[None, :])[0]
        i = int(np.argmax(profile))
        if i == 0 or i == len(profile) - 1 or profile[i] <= 1.5:
            continue
        a, b, c = profile[i - 1], profile[i], profile[i + 1]
        denominator = a - 2 * b + c
        offset = 0.5 * (a - c) / denominator if denominator != 0 else 0.0
        r = radii[i] + offset * (radii[1] - radii[0])
        points.append((cx0 + r * dx, center_y + r * dy))
    points = np.asarray(points, dtype=float)
    if len(points) < 12:
        return None, len(points)
    cx, cy, r, inliers, rms = _ransac_circle(points)
    return EndFit(cx, cy, r, cx + direction * r, int(inliers.sum()), len(points), rms), len(points)


def refine_bubble(roi_bgr, x1, y1, x2, y2, min_inliers=20):
    ridge = ridge_image(roi_bgr)
    center_y = (y1 + y2) / 2
    half_height = (y2 - y1) / 2
    left, _ = fit_end(ridge, x1, center_y, half_height, "left")
    right, _ = fit_end(ridge, x2, center_y, half_height, "right")
    for name, end in (("left", left), ("right", right)):
        if end is None or end.inliers < min_inliers:
            return RefinedBubble(False, (x1 + x2) / 2, x2 - x1, left, right,
                                 f"{name}_ring_fit_failed")
    return RefinedBubble(True, (left.tip_x + right.tip_x) / 2,
                         right.tip_x - left.tip_x, left, right)
