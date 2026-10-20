from __future__ import annotations

import random

import numpy as np
import pytest

from ecvision.metrology import (
    GaugeMeasurement,
    PlanarMeasurement,
    PlanarTarget,
    UncertaintyComponent,
    analyze_gauge,
    backproject_to_target_frame,
    budget_rows,
    calibrate_from_corners,
    calibration_views,
    combined_standard_uncertainty,
    expanded_uncertainty,
    measurement_scene,
    render_report,
    synthetic_camera,
)


def _calibrated():
    camera = synthetic_camera()
    views = calibration_views(camera, views=12, pixel_noise=0.1, seed=11)
    result = calibrate_from_corners(
        [view.object_points for view in views],
        [view.image_points for view in views],
        (camera.image_width, camera.image_height),
        pattern_cols=9,
        pattern_rows=6,
        square_mm=25.0,
    )
    return camera, result


def test_calibration_recovers_synthetic_intrinsics() -> None:
    camera, result = _calibrated()
    assert result.rms_reprojection_px < 0.5
    assert result.camera.fx == pytest.approx(camera.fx, rel=0.03)
    assert result.camera.fy == pytest.approx(camera.fy, rel=0.03)
    assert result.camera.cx == pytest.approx(camera.cx, abs=10.0)
    assert result.camera.cy == pytest.approx(camera.cy, abs=10.0)
    assert result.n_views == 12
    assert result.n_points == 12 * 54


def test_backprojection_recovers_known_plane_points() -> None:
    camera, result = _calibrated()
    scene = measurement_scene(camera)
    target = PlanarTarget(
        object_points_mm=scene.target_object_points.tolist(),
        image_points_px=scene.target_image_points.tolist(),
    )
    measurement = PlanarMeasurement(result.camera, target)
    recovered = backproject_to_target_frame(
        result.camera, measurement.pose, scene.target_image_points
    )
    error = np.linalg.norm(recovered - scene.target_object_points, axis=1)
    assert float(error.max()) < 0.4


def test_planar_measurement_matches_known_distance() -> None:
    camera, result = _calibrated()
    scene = measurement_scene(camera)
    target = PlanarTarget(
        object_points_mm=scene.target_object_points.tolist(),
        image_points_px=scene.target_image_points.tolist(),
    )
    measurement = PlanarMeasurement(result.camera, target)
    value = measurement.measure(scene.point_a_px, scene.point_b_px)
    assert value == pytest.approx(scene.true_mm, abs=0.3)
    assert measurement.pose.reprojection_rms_px < 0.5


def test_monte_carlo_uncertainty_is_bounded_and_ordered() -> None:
    camera, result = _calibrated()
    scene = measurement_scene(camera)
    target = PlanarTarget(
        object_points_mm=scene.target_object_points.tolist(),
        image_points_px=scene.target_image_points.tolist(),
    )
    measurement = PlanarMeasurement(result.camera, target)
    estimate = measurement.measure_with_uncertainty(
        scene.point_a_px, scene.point_b_px, samples=120, seed=4
    )
    assert estimate.n_samples >= 100
    assert 0.0 < estimate.sigma_mm < 2.0
    assert estimate.ci95_low_mm < estimate.value_mm < estimate.ci95_high_mm


def test_uncertainty_budget_combines_in_quadrature() -> None:
    components = [
        UncertaintyComponent(name="a", standard_uncertainty_mm=0.1),
        UncertaintyComponent(name="b", standard_uncertainty_mm=0.2, sensitivity=2.0),
    ]
    combined = combined_standard_uncertainty(components)
    assert combined == pytest.approx((0.1**2 + 0.4**2) ** 0.5)
    assert expanded_uncertainty(combined, k=2.0) == pytest.approx(2.0 * combined)
    rows = budget_rows(components)
    assert rows[-1]["name"] == "combined"
    assert sum(float(row["share_pct"]) for row in rows[:-1]) == pytest.approx(100.0)


def test_gauge_rr_separates_repeatability_and_operator_bias() -> None:
    rng = random.Random(3)
    measurements: list[GaugeMeasurement] = []
    for part in range(5):
        base = 20.0 + 10.0 * part
        for operator, bias in (("A", 0.0), ("B", 0.03)):
            for trial in range(1, 4):
                measurements.append(
                    GaugeMeasurement(
                        part=f"P{part}",
                        operator=operator,
                        trial=trial,
                        value_mm=base + bias + rng.gauss(0.0, 0.01),
                    )
                )
    result = analyze_gauge(measurements, tolerance_mm=1.0)
    assert result.n == 30
    assert result.repeatability_std_mm == pytest.approx(0.01, rel=0.5)
    assert 0.005 < result.reproducibility_std_mm < 0.05
    assert result.grr_std_mm > result.repeatability_std_mm
    assert result.grr_pct_tolerance is not None
    assert result.grr_pct_tolerance < 20.0


def test_report_contains_measurement_and_budget() -> None:
    camera, result = _calibrated()
    scene = measurement_scene(camera)
    target = PlanarTarget(
        object_points_mm=scene.target_object_points.tolist(),
        image_points_px=scene.target_image_points.tolist(),
    )
    measurement = PlanarMeasurement(result.camera, target)
    estimate = measurement.measure_with_uncertainty(
        scene.point_a_px, scene.point_b_px, samples=60, seed=2
    )
    report = render_report(
        title="test report",
        calibration=result,
        measurement=estimate,
        components=[UncertaintyComponent(name="pixel", standard_uncertainty_mm=0.5)],
        notes=["synthetic"],
    )
    assert "test report" in report
    assert "Uncertainty budget" in report
    assert f"{estimate.value_mm:.4f}" in report
    assert "synthetic" in report
