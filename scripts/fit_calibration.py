#!/usr/bin/env python3
"""Fit a calibration profile and optional conformal profile.

Input can be JSONL or CSV. JSONL lines look like::

    {"confidence": 0.93, "correct": true}
    {"confidence": 0.62, "correct": false, "score": 0.78}

CSV must contain ``confidence`` and ``correct``; ``score`` is optional. ``score``
is the nonconformity score for conformal calibration; when omitted,
``1 - confidence`` is used. The output is a strict ``CalibrationProfile`` /
``ConformalProfile`` that ``CalibrationGate`` can load.

Fit on a held-out calibration split, never on the frames used to tune the
model or choose the acceptance policy.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.instrument.calibration import (  # noqa: E402
    CalibrationPoint,
    fit_calibration,
    fit_conformal,
)

_TRUE = {"1", "true", "yes", "y"}


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_number}: invalid JSON ({exc})") from exc
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL or CSV calibration set")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "calibration.json")
    parser.add_argument("--conformal-out", type=Path, default=None)
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument(
        "--method",
        choices=("isotonic", "temperature", "identity"),
        default="isotonic",
        help="isotonic for a non-monotone curve, temperature for a single-parameter baseline",
    )
    parser.add_argument("--dataset-id", default="local-calibration")
    args = parser.parse_args()

    rows = _read_csv(args.input) if args.input.suffix.lower() == ".csv" else _read_jsonl(args.input)
    points: list[CalibrationPoint] = []
    scores: list[float] = []
    for line_number, row in enumerate(rows, start=1):
        try:
            confidence = float(row["confidence"])
            correct = _parse_bool(row["correct"])
            weight = float(row.get("weight") or 1.0)
        except (KeyError, TypeError, ValueError) as exc:
            parser.error(f"row {line_number}: invalid record ({exc})")
        points.append(CalibrationPoint(confidence=confidence, correct=correct, weight=weight))
        score = row.get("score")
        scores.append(float(score) if score not in (None, "") else 1.0 - confidence)

    if not points:
        parser.error("no calibration rows found")

    profile = fit_calibration(points, method=args.method, dataset_id=args.dataset_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "n": profile.n,
                "method": profile.method,
                "temperature": profile.temperature,
                "ece": profile.ece,
                "brier": profile.brier,
                "nll": profile.nll,
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
