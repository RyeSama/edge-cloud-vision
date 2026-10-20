#!/usr/bin/env python3
"""Measure the distance between two pixels on a checkerboard plane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.metrology import (  # noqa: E402
    CalibrationResult,
    CameraIntrinsics,
    PlanarMeasurement,
    PlanarTarget,
    checkerboard_object_points,
    detect_checkerboard,
)


def _point(text: str) -> np.ndarray:
    try:
        x_text, y_text = text.split(",", 1)
        return np.asarray([float(x_text), float(y_text)], dtype=np.float64)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected 'x,y' pixel coordinates") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, help="calibration JSON or camera JSON")
    parser.add_argument("--image", required=True, help="image containing the checkerboard")
    parser.add_argument("--cols", type=int, default=9)
    parser.add_argument("--rows", type=int, default=6)
    parser.add_argument("--square-mm", type=float, default=25.0)
    parser.add_argument("--a", required=True, type=_point, help="first pixel: x,y")
    parser.add_argument("--b", required=True, type=_point, help="second pixel: x,y")
    parser.add_argument("--pixel-sigma", type=float, default=0.5)
    parser.add_argument("--scale-sigma-rel", type=float, default=0.001)
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    try:
        camera = CalibrationResult.load(args.camera).camera
    except Exception:
        camera = CameraIntrinsics.load(args.camera)

    image = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if image is None:
        parser.error(f"cannot read {args.image}")
    corners = detect_checkerboard(image, pattern_cols=args.cols, pattern_rows=args.rows)
    if corners is None:
        parser.error("checkerboard not found")

    object_points = checkerboard_object_points(args.cols, args.rows, args.square_mm)
    target = PlanarTarget(
        object_points_mm=object_points.tolist(),
        image_points_px=corners.tolist(),
        label=Path(args.image).name,
    )
    measurement = PlanarMeasurement(camera, target)
    result = measurement.measure_with_uncertainty(
        args.a,
        args.b,
        pixel_sigma_px=args.pixel_sigma,
        calibration_scale_sigma_rel=args.scale_sigma_rel,
        samples=args.samples,
    )
    payload = {
        "image": str(Path(args.image)),
        "checkerboard_reprojection_rms_px": measurement.pose.reprojection_rms_px,
        "value_mm": result.value_mm,
        "sigma_mm": result.sigma_mm,
        "ci95_low_mm": result.ci95_low_mm,
        "ci95_high_mm": result.ci95_high_mm,
        "pixel_sigma_px": result.pixel_sigma_px,
        "calibration_scale_sigma_rel": result.calibration_scale_sigma_rel,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
