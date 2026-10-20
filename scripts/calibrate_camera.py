#!/usr/bin/env python3
"""Calibrate a camera from checkerboard images."""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.metrology import calibrate_from_images  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", required=True, help="glob for checkerboard images")
    parser.add_argument("--cols", type=int, default=9, help="inner corners per row")
    parser.add_argument("--rows", type=int, default=6, help="inner corners per column")
    parser.add_argument("--square-mm", type=float, default=25.0)
    parser.add_argument("--out", default="data/camera.json")
    args = parser.parse_args()

    paths = sorted(Path(path) for path in glob.glob(args.images, recursive=True))
    if not paths:
        parser.error(f"no images matched {args.images!r}")
    result = calibrate_from_images(
        paths,
        pattern_cols=args.cols,
        pattern_rows=args.rows,
        square_mm=args.square_mm,
    )
    result.save(args.out)
    print(f"views={result.n_views} points={result.n_points}")
    print(f"rms_reprojection_px={result.rms_reprojection_px:.4f}")
    print(
        f"fx={result.camera.fx:.3f} fy={result.camera.fy:.3f} "
        f"cx={result.camera.cx:.3f} cy={result.camera.cy:.3f}"
    )
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
