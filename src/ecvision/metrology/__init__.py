"""Metrology layer: camera calibration, planar measurement, uncertainty, Gauge R&R."""

from ecvision.metrology.calibration import (
    calibrate_from_corners,
    calibrate_from_images,
    checkerboard_object_points,
    detect_checkerboard,
)
from ecvision.metrology.camera import CalibrationResult, CameraIntrinsics
from ecvision.metrology.gauge import GaugeMeasurement, GaugeResult, analyze_gauge, gauge_rows
from ecvision.metrology.measurement import (
    MeasurementResult,
    PlanarMeasurement,
    PlanarTarget,
    backproject_to_plane,
    backproject_to_target_frame,
    solve_plane_pose,
)
from ecvision.metrology.report import render_report, write_report
from ecvision.metrology.synthetic import (
    SyntheticMeasurement,
    SyntheticView,
    calibration_views,
    make_view,
    measurement_scene,
    synthetic_camera,
)
from ecvision.metrology.uncertainty import (
    UncertaintyComponent,
    budget_rows,
    combined_standard_uncertainty,
    expanded_uncertainty,
)

__all__ = [
    "CalibrationResult",
    "CameraIntrinsics",
    "GaugeMeasurement",
    "GaugeResult",
    "MeasurementResult",
    "PlanarMeasurement",
    "PlanarTarget",
    "SyntheticMeasurement",
    "SyntheticView",
    "UncertaintyComponent",
    "analyze_gauge",
    "backproject_to_plane",
    "backproject_to_target_frame",
    "budget_rows",
    "calibrate_from_corners",
    "calibrate_from_images",
    "calibration_views",
    "checkerboard_object_points",
    "combined_standard_uncertainty",
    "detect_checkerboard",
    "expanded_uncertainty",
    "gauge_rows",
    "make_view",
    "measurement_scene",
    "render_report",
    "solve_plane_pose",
    "synthetic_camera",
    "write_report",
]
