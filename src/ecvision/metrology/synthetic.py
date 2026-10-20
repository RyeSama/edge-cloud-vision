"""Deterministic synthetic camera scenes for metrology tests and demos.

The point is not to fake a real calibration. It is to have a scene where the
ground truth is exactly known, so projection, pose, back-projection and the
uncertainty propagation can be checked independently of a physical rig.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ecvision.metrology.calibration import checkerboard_object_points
from ecvision.metrology.camera import CameraIntrinsics


@dataclass(frozen=True, slots=True)
class SyntheticView:
    object_points: np.ndarray
    image_points: np.ndarray
    rvec: np.ndarray
    tvec: np.ndarray


@dataclass(frozen=True, slots=True)
class SyntheticMeasurement:
    target_object_points: np.ndarray
    target_image_points: np.ndarray
    point_a_px: np.ndarray
    point_b_px: np.ndarray
    true_mm: float


def synthetic_camera(
    *, width: int = 1280, height: int = 720, fx: float = 900.0, fy: float = 900.0
) -> CameraIntrinsics:
    return CameraIntrinsics(
        fx=fx,
        fy=fy,
        cx=width / 2.0,
        cy=height / 2.0,
        image_width=width,
        image_height=height,
        distortion=[-0.12, 0.05, 0.001, -0.0005, 0.0],
    )


def make_view(
    camera: CameraIntrinsics,
    *,
    cols: int = 9,
    rows: int = 6,
    square_mm: float = 25.0,
    rvec: tuple[float, float, float] = (0.0, 0.0, 0.0),
    tvec: tuple[float, float, float] = (0.0, 0.0, 800.0),
    pixel_noise: float = 0.0,
    seed: int = 0,
) -> SyntheticView:
    object_points = checkerboard_object_points(cols, rows, square_mm)
    rvec_array = np.asarray(rvec, dtype=np.float64)
    tvec_array = np.asarray(tvec, dtype=np.float64)
    image_points, _ = cv2.projectPoints(
        object_points.reshape(-1, 1, 3),
        rvec_array,
        tvec_array,
        camera.matrix(),
        camera.dist_coeffs(),
    )
    image_points = image_points.reshape(-1, 2)
    if pixel_noise > 0:
        rng = np.random.default_rng(seed)
        image_points = image_points + rng.normal(0.0, pixel_noise, image_points.shape)
    return SyntheticView(object_points, image_points, rvec_array, tvec_array)


def calibration_views(
    camera: CameraIntrinsics | None = None,
    *,
    cols: int = 9,
    rows: int = 6,
    square_mm: float = 25.0,
    views: int = 10,
    pixel_noise: float = 0.1,
    seed: int = 7,
) -> list[SyntheticView]:
    camera = camera or synthetic_camera()
    if views < 3:
        raise ValueError("at least 3 views are required")
    output: list[SyntheticView] = []
    rng = np.random.default_rng(seed)
    for index in range(views):
        rvec = (
            float(rng.uniform(-0.25, 0.25)),
            float(rng.uniform(-0.25, 0.25)),
            float(rng.uniform(-0.10, 0.10)),
        )
        tvec = (
            float(rng.uniform(-40.0, 40.0)),
            float(rng.uniform(-30.0, 30.0)),
            float(760.0 + 12.0 * index),
        )
        output.append(
            make_view(
                camera,
                cols=cols,
                rows=rows,
                square_mm=square_mm,
                rvec=rvec,
                tvec=tvec,
                pixel_noise=pixel_noise,
                seed=seed + index,
            )
        )
    return output


def measurement_scene(
    camera: CameraIntrinsics | None = None,
    *,
    cols: int = 9,
    rows: int = 6,
    square_mm: float = 25.0,
    pixel_noise: float = 0.1,
    seed: int = 21,
) -> SyntheticMeasurement:
    camera = camera or synthetic_camera()
    view = make_view(
        camera,
        cols=cols,
        rows=rows,
        square_mm=square_mm,
        rvec=(0.05, -0.03, 0.02),
        tvec=(0.0, 0.0, 800.0),
        pixel_noise=pixel_noise,
        seed=seed,
    )
    true_a = np.asarray([50.0, 50.0, 0.0])
    true_b = np.asarray([150.0, 50.0, 0.0])
    points, _ = cv2.projectPoints(
        np.asarray([true_a, true_b]).reshape(-1, 1, 3),
        view.rvec,
        view.tvec,
        camera.matrix(),
        camera.dist_coeffs(),
    )
    points = points.reshape(-1, 2)
    rng = np.random.default_rng(seed + 1)
    if pixel_noise > 0:
        points = points + rng.normal(0.0, pixel_noise, points.shape)
    return SyntheticMeasurement(
        target_object_points=view.object_points,
        target_image_points=view.image_points,
        point_a_px=points[0],
        point_b_px=points[1],
        true_mm=float(np.linalg.norm(true_b - true_a)),
    )
