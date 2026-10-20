#!/usr/bin/env python3
"""Fit calibration and conformal profiles from a JSONL calibration set.

Input lines (one JSON object per line)::

    {"confidence": 0.93, "correct": true}
    {"confidence": 0.62, "correct": false, "score": 0.78}

``score`` is optional; when omitted, ``1 - confidence`` is used as the
nonconformity score for conformal calibration. The output is a strict
``CalibrationProfile`` / ``ConformalProfile`` that ``CalibrationGate`` can load.

This is intentionally dataset-agnostic: the repo does not ship a labelled
industrial dataset, so the calibration set is user-supplied. Do not fit on the
same frames used to tune the model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.instrument.calibration import (  # noqa: E402
    CalibrationPoint,
    fit_conformal,
    fit_isotonic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL calibration set")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "calibration.json")
    parser.add_argument("--conformal-out", type=Path, default=None)
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument("--dataset-id", default="local-calibration")
    args = parser.parse_args()

    points: list[CalibrationPoint] = []
    scores: list[float] = []
    for line_number, raw in enumerate(args.input.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
            confidence = float(row["confidence"])
            correct = bool(row["correct"])
            weight = float(row.get("weight", 1.0))
        except (KeyError, TypeError, ValueError) as exc:
            parser.error(f"line {line_number}: invalid record ({exc})")
        points.append(CalibrationPoint(confidence=confidence, correct=correct, weight=weight))
        score = row.get("score", 1.0 - confidence)
        scores.append(float(score))

    if not points:
        parser.error("no calibration rows found")

    profile = fit_isotonic(points, dataset_id=args.dataset_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "n": profile.n,
                "ece": profile.ece,
                "brier": profile.brier,
                "method": profile.method,
            },
            ensure_ascii=False,
        )
    )

    if args.conformal_out is not None:
        conformal = fit_conformal(scores, alpha=args.alpha, dataset_id=args.dataset_id)
        args.conformal_out.parent.mkdir(parents=True, exist_ok=True)
        args.conformal_out.write_text(conformal.model_dump_json(indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "conformal_out": str(args.conformal_out),
                    "alpha": conformal.alpha,
                    "qhat": conformal.qhat,
                    "n": conformal.n,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
