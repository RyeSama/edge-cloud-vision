#!/usr/bin/env python3
"""Deterministic metrology demo: calibration, planar measurement, uncertainty, Gauge R&R.

This is the v0.5 evidence script. It uses a synthetic pinhole camera whose
ground truth is known, so the output checks the math rather than a physical rig.
Replace ``calibration_views`` and ``measurement_scene`` with real checkerboard
images for a real camera study.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.metrology import (  # noqa: E402
    GaugeMeasurement,
    PlanarMeasurement,
    PlanarTarget,
    UncertaintyComponent,
    analyze_gauge,
    budget_rows,
    calibrate_from_corners,
    calibration_views,
    combined_standard_uncertainty,
    expanded_uncertainty,
    measurement_scene,
    render_report,
    synthetic_camera,
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/measurements/v0.5", help="output directory")
    parser.add_argument("--views", type=int, default=15)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--pixel-noise", type=float, default=0.1)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    camera = synthetic_camera()
    views = calibration_views(camera, views=args.views, pixel_noise=args.pixel_noise, seed=11)
    calibration = calibrate_from_corners(
        [view.object_points for view in views],
        [view.image_points for view in views],
        (camera.image_width, camera.image_height),
        pattern_cols=9,
        pattern_rows=6,
        square_mm=25.0,
    )
    calibration.save(out / "camera.json")
    (out / "calibration-summary.json").write_text(
        json.dumps(
            {
                "ground_truth": {
                    "fx": camera.fx,
                    "fy": camera.fy,
                    "cx": camera.cx,
                    "cy": camera.cy,
                },
                "fitted": calibration.camera.model_dump(mode="json"),
                "rms_reprojection_px": calibration.rms_reprojection_px,
                "per_view_rms_px": calibration.per_view_rms_px,
                "n_views": calibration.n_views,
                "n_points": calibration.n_points,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    measurement_rows: list[dict[str, object]] = []
    first_measurement = None
    for index in range(args.repeats):
        scene = measurement_scene(camera, pixel_noise=args.pixel_noise, seed=100 + index)
        target = PlanarTarget(
            object_points_mm=scene.target_object_points.tolist(),
            image_points_px=scene.target_image_points.tolist(),
        )
        measurement = PlanarMeasurement(calibration.camera, target)
        result = measurement.measure_with_uncertainty(
            scene.point_a_px,
            scene.point_b_px,
            pixel_sigma_px=0.5,
            calibration_scale_sigma_rel=0.001,
            samples=160,
            seed=index,
        )
        if first_measurement is None:
            first_measurement = result
        measurement_rows.append(
            {
                "repeat": index + 1,
                "truth_mm": round(scene.true_mm, 6),
                "measured_mm": round(result.value_mm, 6),
                "error_mm": round(result.value_mm - scene.true_mm, 6),
                "sigma_mm": round(result.sigma_mm, 6),
                "ci95_low_mm": round(result.ci95_low_mm, 6),
                "ci95_high_mm": round(result.ci95_high_mm, 6),
                "pose_rms_px": round(measurement.pose.reprojection_rms_px, 6),
            }
        )
    _write_csv(out / "measurements.csv", measurement_rows)

    errors = np.asarray([float(row["error_mm"]) for row in measurement_rows])
    assert first_measurement is not None
    components = [
        UncertaintyComponent(
            name="pixel localization + plane pose",
            standard_uncertainty_mm=first_measurement.sigma_mm,
            distribution="normal",
            note="Monte Carlo over checkerboard corners and measurement endpoints",
        ),
        UncertaintyComponent(
            name="calibration scale",
            standard_uncertainty_mm=0.100,
            distribution="normal",
            note="0.1% of a 100 mm measurand; replace with the real calibration covariance",
        ),
        UncertaintyComponent(
            name="target flatness / placement",
            standard_uncertainty_mm=0.030,
            distribution="rectangular",
            note="assumed 0.03 mm standard uncertainty for a flat printed target",
        ),
    ]
    _write_csv(out / "uncertainty-budget.csv", budget_rows(components))
    combined = combined_standard_uncertainty(components)
    expanded = expanded_uncertainty(combined, k=2.0)

    gauge_measurements: list[GaugeMeasurement] = []
    rng = random.Random(4)
    for part in range(5):
        base = 20.0 + 0.1 * part
        for operator, bias in (("op-A", 0.0), ("op-B", 0.03)):
            for trial in range(1, 4):
                gauge_measurements.append(
                    GaugeMeasurement(
                        part=f"P{part}",
                        operator=operator,
                        trial=trial,
                        value_mm=base + bias + rng.gauss(0.0, 0.01),
                    )
                )
    gauge = analyze_gauge(gauge_measurements, tolerance_mm=1.0)
    (out / "gauge-summary.json").write_text(
        gauge.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _write_csv(
        out / "gauge.csv",
        [
            {
                "part": item.part,
                "operator": item.operator,
                "trial": item.trial,
                "value_mm": round(item.value_mm, 6),
            }
            for item in gauge_measurements
        ],
    )

    report = render_report(
        title="v0.5 planar metrology validation (synthetic camera)",
        calibration=calibration,
        measurement=first_measurement,
        components=components,
        gauge=gauge,
        notes=[
            "Synthetic pinhole camera: ground truth fx/fy/cx/cy is known; this validates projection, pose, back-projection and uncertainty propagation.",
            "The repeatability column is the spread of repeated scene noise, not a physical repeatability study.",
            f"Repeat error mean = {errors.mean():.4f} mm, RMS = {np.sqrt(np.mean(errors**2)):.4f} mm.",
            "Real deployment needs real checkerboard images, a flat target, and a traceable reference (caliper/gauge block).",
            "The Gauge R&R study uses its own synthetic measurement values to exercise the estimator; it is not the camera run above.",
        ],
    )
    (out / "report.md").write_text(report, encoding="utf-8")

    summary = {
        "calibration_rms_px": calibration.rms_reprojection_px,
        "fx": calibration.camera.fx,
        "fy": calibration.camera.fy,
        "cx": calibration.camera.cx,
        "cy": calibration.camera.cy,
        "measurement_value_mm": first_measurement.value_mm,
        "measurement_sigma_mm": first_measurement.sigma_mm,
        "error_mean_mm": float(errors.mean()),
        "error_rms_mm": float(np.sqrt(np.mean(errors**2))),
        "combined_standard_uncertainty_mm": combined,
        "expanded_uncertainty_mm": expanded,
        "gauge_grr_mm": gauge.grr_std_mm,
        "gauge_pct_tolerance": gauge.grr_pct_tolerance,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
