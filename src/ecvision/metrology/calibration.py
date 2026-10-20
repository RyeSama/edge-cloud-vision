"""Checkerboard calibration for the metrology layer.

Two entry points:

* :func:`calibrate_from_corners` takes already-detected correspondences. This is
  what unit tests and synthetic validation use; it keeps the calibration math
  independent of image decoding.
* :func:`calibrate_from_images` detects an inner-corner checkerboard in image
  files and then calls the same routine.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from ecvision.metrology.camera import CalibrationResult, CameraIntrinsics


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def checkerboard_object_points(cols: int, rows: int, square_mm: float) -> np.ndarray:
    """3D object points of an inner-corner grid, z=0, in millimetres."""
    if cols < 2 or rows < 2:
        raise ValueError("checkerboard needs at least 2x2 inner corners")
    if square_mm <= 0:
        raise ValueError("square_mm must be > 0")
    grid = np.zeros((cols * rows, 3), dtype=np.float64)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * float(square_mm)
    return grid


def calibrate_from_corners(
    object_points: Sequence[np.ndarray],
    image_points: Sequence[np.ndarray],
    image_size: tuple[int, int],
    *,
    pattern_cols: int,
    pattern_rows: int,
    square_mm: float,
    flags: int = 0,
) -> CalibrationResult:
    """Run OpenCV's pinhole calibration on known 3D/2D correspondences."""
    if len(object_points) != len(image_points):
        raise ValueError("object_points and image_points must have equal length")
    if len(object_points) < 3:
        raise ValueError("at least 3 calibration views are required")
    if image_size[0] <= 0 or image_size[1] <= 0:
        raise ValueError("image_size must be positive")

    objects = [np.asarray(points, dtype=np.float32).reshape(-1, 3) for points in object_points]
    images = [np.asarray(points, dtype=np.float32).reshape(-1, 2) for points in image_points]
    for index, (obj, img) in enumerate(zip(objects, images, strict=True)):
        if len(obj) < 4 or len(obj) != len(img):
            raise ValueError(f"view {index}: need matching object/image points, at least 4")

    rms, matrix, distortion, rvecs, tvecs = cv2.calibrateCamera(
        objects,
        images,
        image_size,
        None,
        None,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 100, 1e-9),
    )
    if not np.isfinite(rms) or matrix is None:
        raise ValueError("OpenCV calibration failed")

    per_view: list[float] = []
    for obj, img, rvec, tvec in zip(objects, images, rvecs, tvecs, strict=True):
        projected, _ = cv2.projectPoints(obj, rvec, tvec, matrix, distortion)
        residual = projected.reshape(-1, 2) - img
        per_view.append(float(np.sqrt(np.mean(np.sum(residual * residual, axis=1)))))

    camera = CameraIntrinsics(
        fx=float(matrix[0, 0]),
        fy=float(matrix[1, 1]),
        cx=float(matrix[0, 2]),
        cy=float(matrix[1, 2]),
        image_width=int(image_size[0]),
        image_height=int(image_size[1]),
        distortion=[float(value) for value in np.asarray(distortion).reshape(-1)[:5]],
    )
    return CalibrationResult(
        camera=camera,
        rms_reprojection_px=float(rms),
        per_view_rms_px=per_view,
        n_views=len(objects),
        n_points=sum(len(points) for points in objects),
        pattern_cols=pattern_cols,
        pattern_rows=pattern_rows,
        square_mm=float(square_mm),
        created_at=utc_now(),
    )


def detect_checkerboard(
    image: np.ndarray,
    *,
    pattern_cols: int,
    pattern_rows: int,
) -> np.ndarray | None:
    """Detect inner corners with sub-pixel refinement where available."""
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    pattern = (pattern_cols, pattern_rows)
    found, corners = cv2.findChessboardCornersSB(
        gray,
        pattern,
        flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY,
    )
    if not found:
        found, corners = cv2.findChessboardCorners(
            gray,
            pattern,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE,
        )
        if found:
            corners = cv2.cornerSubPix(
                gray,
                corners,
                winSize=(5, 5),
                zeroZone=(-1, -1),
                criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 40, 0.001),
            )
    if not found:
        return None
    return np.asarray(corners, dtype=np.float64).reshape(-1, 2)


def calibrate_from_images(
    paths: Iterable[str | Path],
    *,
    pattern_cols: int,
    pattern_rows: int,
    square_mm: float,
) -> CalibrationResult:
    """Detect a checkerboard in each image and calibrate the camera."""
    object_grid = checkerboard_object_points(pattern_cols, pattern_rows, square_mm)
    objects: list[np.ndarray] = []
    images: list[np.ndarray] = []
    image_size: tuple[int, int] | None = None
    failures: list[str] = []

    for path in paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            failures.append(f"{path}: unreadable")
            continue
        height, width = image.shape[:2]
        if image_size is None:
            image_size = (width, height)
        elif image_size != (width, height):
            failures.append(f"{path}: size {width}x{height} != {image_size[0]}x{image_size[1]}")
            continue
        corners = detect_checkerboard(image, pattern_cols=pattern_cols, pattern_rows=pattern_rows)
        if corners is None:
            failures.append(f"{path}: checkerboard not found")
            continue
        objects.append(object_grid.copy())
        images.append(corners)

    if image_size is None or len(objects) < 3:
        detail = "; ".join(failures[:5]) or "no readable images"
        raise ValueError(f"need at least 3 valid checkerboard views ({detail})")

    result = calibrate_from_corners(
        objects,
        images,
        image_size,
        pattern_cols=pattern_cols,
        pattern_rows=pattern_rows,
        square_mm=square_mm,
    )
    return result
