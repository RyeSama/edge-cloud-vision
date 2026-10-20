"""Run the deterministic stimulus-response bench (P1-1).

This is a test report generator, not a training script. It never downloads a
model. With no ``--source-image`` it uses synthetic targets, so the report only
claims timing, gate stability, and repeatability; pass ``--source-image`` and
``--ground-truth`` for label accuracy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.bench.stimulus import (  # noqa: E402
    DEFAULT_CASES,
    run_benchmark,
    summarize,
    write_csv,
    write_markdown,
)
from ecvision.edge.classifier import TFLiteClassifier  # noqa: E402
from ecvision.edge.gate import GateConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", type=Path, default=ROOT / "models" / "mobilenet_v1_1.0_224_quant.tflite"
    )
    parser.add_argument("--labels", type=Path, default=ROOT / "models" / "labels.txt")
    parser.add_argument(
        "--source-image", type=Path, default=None, help="real image; requires --ground-truth"
    )
    parser.add_argument("--ground-truth", default=None, help="label for --source-image")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "measurements" / "stimulus.md")
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--num-threads", type=int, default=2)
    args = parser.parse_args()

    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.source_image is not None and not args.ground_truth:
        parser.error("--source-image requires --ground-truth")

    base_image = None
    cases = DEFAULT_CASES
    if args.source_image is not None:
        base_image = cv2.imread(str(args.source_image))
        if base_image is None:
            parser.error(f"could not read image: {args.source_image}")
        cases = tuple(
            case.__class__(
                name=case.name,
                distance_m=case.distance_m,
                lux=case.lux,
                blur_sigma=case.blur_sigma,
                occlusion_ratio=case.occlusion_ratio,
                seed=case.seed,
                ground_truth=args.ground_truth,
            )
            for case in DEFAULT_CASES
        )

    classifier = TFLiteClassifier(args.model, args.labels, num_threads=args.num_threads)
    samples = run_benchmark(
        classifier,
        cases,
        repeats=args.repeats,
        width=args.width,
        height=args.height,
        gate=GateConfig(),
        base_image=base_image,
    )
    summary = summarize(samples)
    summary["model"] = classifier.model_name
    summary["source"] = "image" if base_image is not None else "synthetic"
    summary["ground_truth"] = args.ground_truth

    write_markdown(summary, args.out, model=classifier.model_name)
    if args.csv is not None:
        write_csv(samples, args.csv)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
