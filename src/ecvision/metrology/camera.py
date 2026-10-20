"""Pinhole camera model and pixel/ray helpers.

This is the geometric layer under the v0.5 metrology module. It deliberately
does not depend on the edge classifier: a camera calibration is a property of
the lens/sensor, not of the model that happens to consume the frames.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from pydantic import Field, model_validator

from ecvision.contracts import StrictModel


class CameraIntrinsics(StrictModel):
    """Pinhole intrinsics plus an optional 5-parameter distortion vector."""

    fx: float = Field(gt=0)
    fy: float = Field(gt=0)
    cx: float = Field(ge=0)
    cy: float = Field(ge=0)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    distortion: list[float] = Field(default_factory=lambda: [0.0] * 5, max_length=5)
    model: str = Field(default="pinhole", min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_principal_point(self) -> CameraIntrinsics:
        if not 0.0 <= self.cx <= self.image_width:
            raise ValueError("cx must be inside the image width")
        if not 0.0 <= self.cy <= self.image_height:
            raise ValueError("cy must be inside the image height")
        return self

    def matrix(self) -> np.ndarray:
        return np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )

    def dist_coeffs(self) -> np.ndarray:
        return np.asarray(self.distortion, dtype=np.float64).reshape(1, -1)

    def project(self, points_mm: np.ndarray) -> np.ndarray:
        """Project points in the camera frame (millimetres, z forward) to pixels."""
        points = np.asarray(points_mm, dtype=np.float64).reshape(-1, 3)
        if points.size == 0:
            return np.empty((0, 2), dtype=np.float64)
        image_points, _ = cv2.projectPoints(
            points.reshape(-1, 1, 3),
            np.zeros(3, dtype=np.float64),
            np.zeros(3, dtype=np.float64),
            self.matrix(),
            self.dist_coeffs(),
        )
        return image_points.reshape(-1, 2)

    def undistort_pixels(self, points_px: np.ndarray) -> np.ndarray:
        """Undistort pixels while keeping them in pixel coordinates."""
        points = np.asarray(points_px, dtype=np.float64).reshape(-1, 2)
        if points.size == 0:
            return np.empty((0, 2), dtype=np.float64)
        corrected = cv2.undistortPoints(
            points.reshape(-1, 1, 2),
            self.matrix(),
            self.dist_coeffs(),
            P=self.matrix(),
        )
        return corrected.reshape(-1, 2)

    def pixel_rays(self, points_px: np.ndarray) -> np.ndarray:
        """Unit-depth rays in the camera frame: ``[x/z, y/z, 1]``."""
        points = np.asarray(points_px, dtype=np.float64).reshape(-1, 2)
        if points.size == 0:
            return np.empty((0, 3), dtype=np.float64)
        normalized = cv2.undistortPoints(
            points.reshape(-1, 1, 2),
            self.matrix(),
            self.dist_coeffs(),
        )
        xy = normalized.reshape(-1, 2)
        return np.column_stack([xy, np.ones(len(xy), dtype=np.float64)])

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> CameraIntrinsics:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class CalibrationResult(StrictModel):
    """Camera calibration plus the diagnostics needed to judge it."""

    camera: CameraIntrinsics
    rms_reprojection_px: float = Field(ge=0.0)
    per_view_rms_px: list[float] = Field(default_factory=list)
    n_views: int = Field(ge=3)
    n_points: int = Field(ge=4)
    pattern_cols: int = Field(ge=2)
    pattern_rows: int = Field(ge=2)
    square_mm: float = Field(gt=0.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> CalibrationResult:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_json(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
