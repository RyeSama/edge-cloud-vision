"""Planar measurement: back-project two pixels onto a calibrated plane.

The target is a flat checkerboard whose metric geometry is known. Solving the
plane pose from the detected corners gives a homography between the board and
the image; two arbitrary pixels can then be back-projected as rays and
intersected with that plane.

This is intentionally a *planar* measurement. It does not claim 3D metrology,
and it assumes the object being measured lies on the same plane as the board.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np
from pydantic import Field, model_validator

from ecvision.contracts import StrictModel
from ecvision.metrology.camera import CameraIntrinsics


class PlanarTarget(StrictModel):
    """Known 3D points on a plane and their pixel correspondences."""

    object_points_mm: list[list[float]] = Field(min_length=4)
    image_points_px: list[list[float]] = Field(min_length=4)
    label: str = Field(default="planar-target", min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_lengths(self) -> PlanarTarget:
        if len(self.object_points_mm) != len(self.image_points_px):
            raise ValueError("object_points_mm and image_points_px must have equal length")
        for point in self.object_points_mm:
            if len(point) != 3:
                raise ValueError("each object point must be [x, y, z]")
            if abs(point[2]) > 1e-6:
                raise ValueError("planar target object points must have z=0")
        for point in self.image_points_px:
            if len(point) != 2:
                raise ValueError("each image point must be [x, y]")
        return self


class MeasurementResult(StrictModel):
    value_mm: float = Field(ge=0.0)
    sigma_mm: float = Field(ge=0.0)
    ci95_low_mm: float = Field(ge=0.0)
    ci95_high_mm: float = Field(ge=0.0)
    n_samples: int = Field(ge=0)
    pixel_sigma_px: float = Field(ge=0.0)
    calibration_scale_sigma_rel: float = Field(ge=0.0)
    method: Literal["monte-carlo", "analytic"] = "monte-carlo"

    @model_validator(mode="after")
    def check_interval(self) -> MeasurementResult:
        if self.ci95_low_mm > self.ci95_high_mm:
            raise ValueError("ci95_low_mm must be <= ci95_high_mm")
        return self


@dataclass(frozen=True, slots=True)
class PlanePose:
    rvec: np.ndarray
    tvec: np.ndarray
    reprojection_rms_px: float
    plane_normal: np.ndarray
    plane_offset: float


def solve_plane_pose(
    camera: CameraIntrinsics,
    target: PlanarTarget,
) -> PlanePose:
    object_points = np.asarray(target.object_points_mm, dtype=np.float64).reshape(-1, 3)
    image_points = np.asarray(target.image_points_px, dtype=np.float64).reshape(-1, 2)
    if len(object_points) < 4:
        raise ValueError("at least four coplanar correspondences are required")

    ok, rvec, tvec = cv2.solvePnP(
        object_points,
        image_points,
        camera.matrix(),
        camera.dist_coeffs(),
        flags=cv2.SOLVEPNP_IPPE,
    )
    if not ok:
        ok, rvec, tvec = cv2.solvePnP(
            object_points,
            image_points,
            camera.matrix(),
            camera.dist_coeffs(),
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
    if not ok:
        raise ValueError("solvePnP failed for the planar target")

    projected, _ = cv2.projectPoints(
        object_points, rvec, tvec, camera.matrix(), camera.dist_coeffs()
    )
    residual = projected.reshape(-1, 2) - image_points
    rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))

    rotation, _ = cv2.Rodrigues(rvec)
    normal = rotation[:, 2]
    norm = float(np.linalg.norm(normal))
    if norm == 0:
        raise ValueError("degenerate plane normal")
    normal = normal / norm
    offset = -float(normal @ tvec.reshape(3))

    return PlanePose(
        rvec=rvec.reshape(3),
        tvec=tvec.reshape(3),
        reprojection_rms_px=rms,
        plane_normal=normal,
        plane_offset=offset,
    )


def backproject_to_plane(
    camera: CameraIntrinsics,
    pose: PlanePose,
    points_px: np.ndarray,
) -> np.ndarray:
    """Intersect pixel rays with the calibrated target plane (millimetres)."""
    rays = camera.pixel_rays(points_px)
    denominator = rays @ pose.plane_normal
    if np.any(np.abs(denominator) < 1e-12):
        raise ValueError("pixel ray is parallel to the target plane")
    depths = -pose.plane_offset / denominator
    if np.any(depths <= 0):
        raise ValueError("pixel ray points away from the target plane")
    return rays * depths[:, None]


def backproject_to_target_frame(
    camera: CameraIntrinsics,
    pose: PlanePose,
    points_px: np.ndarray,
) -> np.ndarray:
    """Back-project pixels, then express the points in the target's frame.

    The returned points have the same coordinate system as
    ``PlanarTarget.object_points_mm``, so a known target can be compared
    directly with what the camera recovered.
    """
    camera_points = backproject_to_plane(camera, pose, points_px)
    rotation, _ = cv2.Rodrigues(pose.rvec)
    return (camera_points - pose.tvec.reshape(1, 3)) @ rotation


class PlanarMeasurement:
    """Pose plus two-point measurement on a planar target."""

    def __init__(self, camera: CameraIntrinsics, target: PlanarTarget) -> None:
        self.camera = camera
        self.target = target
        self.pose = solve_plane_pose(camera, target)

    def measure(self, point_a_px: np.ndarray, point_b_px: np.ndarray) -> float:
        points = backproject_to_plane(
            self.camera,
            self.pose,
            np.asarray([point_a_px, point_b_px], dtype=np.float64),
        )
        return float(np.linalg.norm(points[0] - points[1]))

    def measure_with_uncertainty(
        self,
        point_a_px: np.ndarray,
        point_b_px: np.ndarray,
        *,
        pixel_sigma_px: float = 0.5,
        calibration_scale_sigma_rel: float = 0.001,
        samples: int = 400,
        seed: int = 7,
    ) -> MeasurementResult:
        if pixel_sigma_px < 0 or calibration_scale_sigma_rel < 0:
            raise ValueError("uncertainty inputs must be non-negative")
        if samples < 20:
            raise ValueError("at least 20 Monte Carlo samples are required")

        base_a = np.asarray(point_a_px, dtype=np.float64)
        base_b = np.asarray(point_b_px, dtype=np.float64)
        object_points = np.asarray(self.target.object_points_mm, dtype=np.float64)
        image_points = np.asarray(self.target.image_points_px, dtype=np.float64)

        rng = np.random.default_rng(seed)
        values: list[float] = []
        matrix = self.camera.matrix()
        dist = self.camera.dist_coeffs()
        for _ in range(samples):
            jitter = rng.normal(0.0, pixel_sigma_px, size=image_points.shape)
            noisy_reference = image_points + jitter
            noisy_a = base_a + rng.normal(0.0, pixel_sigma_px, size=2)
            noisy_b = base_b + rng.normal(0.0, pixel_sigma_px, size=2)
            ok, rvec, tvec = cv2.solvePnP(
                object_points,
                noisy_reference,
                matrix,
                dist,
                flags=cv2.SOLVEPNP_IPPE,
            )
            if not ok:
                continue
            rotation, _ = cv2.Rodrigues(rvec)
            normal = rotation[:, 2]
            norm = float(np.linalg.norm(normal))
            if norm == 0:
                continue
            normal = normal / norm
            offset = -float(normal @ tvec.reshape(3))
            rays = self.camera.pixel_rays(np.asarray([noisy_a, noisy_b]))
            denominator = rays @ normal
            if np.any(np.abs(denominator) < 1e-12):
                continue
            depths = -offset / denominator
            if np.any(depths <= 0):
                continue
            points = rays * depths[:, None]
            value = float(np.linalg.norm(points[0] - points[1]))
            value *= float(1.0 + rng.normal(0.0, calibration_scale_sigma_rel))
            values.append(value)

        if len(values) < 10:
            raise ValueError("Monte Carlo produced too few valid samples")
        array = np.asarray(values, dtype=np.float64)
        return MeasurementResult(
            value_mm=float(np.median(array)),
            sigma_mm=float(np.std(array, ddof=1)),
            ci95_low_mm=float(np.percentile(array, 2.5)),
            ci95_high_mm=float(np.percentile(array, 97.5)),
            n_samples=len(values),
            pixel_sigma_px=float(pixel_sigma_px),
            calibration_scale_sigma_rel=float(calibration_scale_sigma_rel),
            method="monte-carlo",
        )
