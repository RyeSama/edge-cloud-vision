"""Image preprocessing kept independent from the TFLite runtime."""

from __future__ import annotations

import cv2
import numpy as np


def center_crop_square(frame: np.ndarray) -> np.ndarray:
    """Preserve aspect ratio; a plain resize distorts objects and hurts accuracy."""
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(f"expected HxWx3 BGR frame, got {frame.shape}")
    height, width = frame.shape[:2]
    side = min(height, width)
    top = (height - side) // 2
    left = (width - side) // 2
    return frame[top : top + side, left : left + side]


def to_rgb_input(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Return uint8 RGB at ``size=(width, height)``.

    Quantized MobileNet takes raw uint8 RGB. Float models need a separate
    normalization policy, applied by the classifier based on tensor dtype.
    """
    target_w, target_h = size
    cropped = center_crop_square(frame)
    interpolation = cv2.INTER_AREA if cropped.shape[1] > target_w else cv2.INTER_LINEAR
    resized = cv2.resize(cropped, (target_w, target_h), interpolation=interpolation)
    return cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
