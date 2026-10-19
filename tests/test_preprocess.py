from __future__ import annotations

import numpy as np

from ecvision.edge.preprocess import center_crop_square, to_rgb_input


def test_center_crop_preserves_square():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cropped = center_crop_square(frame)
    assert cropped.shape == (480, 480, 3)


def test_rgb_conversion_and_size():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:, :, 2] = 255  # BGR red
    rgb = to_rgb_input(frame, (224, 224))
    assert rgb.shape == (224, 224, 3)
    assert rgb.dtype == np.uint8
    assert tuple(rgb[0, 0]) == (255, 0, 0)
