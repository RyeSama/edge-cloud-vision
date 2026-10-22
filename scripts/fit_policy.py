#!/usr/bin/env python3
"""Fit a per-condition acceptance policy from evaluation predictions.

Input is ``predictions.csv`` produced by ``eval_dataset.py``. The policy is fit
on the **calibration split** only; evaluate it on the held-out test split with
``eval_dataset.py --policy``.

Selection rule: for each condition with at least ``--min-samples`` rows, choose
the lowest calibrated threshold whose observed false-accept rate is within
``--false-accept-budget``. Sparse conditions fall back to the global threshold.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.instrument.acceptance import fit_policy, policy_summary  # noqa: E402


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="calibration predictions.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "acceptance-policy.json")
    parser.add_argument("--false-accept-budget", type=float, default=0.02)
    parser.add_argument(
        "--risk-control",
        choices=("empirical", "wilson"),
        default="empirical",
        help="empirical FAR, or the one-sided Wilson upper bound (v0.6.1)",
    )
    parser.add_argument("--delta", type=float, default=0.05)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--condition-column", default="degradation")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="fit on raw confidence even if calibrated_confidence is present",
    )
    parser.add_argument("--dataset-id", default="local-calibration")
    parser.add_argument("--calibration-profile-version", default=None)
    args = parser.parse_args()

    if args.min_samples < 1:
        parser.error("--min-samples must be >= 1")
    if not 0.0 <= args.false_accept_budget <= 1.0:
        parser.error("--false-accept-budget must be in [0, 1]")
    if not 0.0 < args.delta < 1.0:
        parser.error("--delta must be in (0, 1)")

    with args.input.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        parser.error("no rows found")

    records = []
    for index, row in enumerate(rows, start=1):
        key = (
            "confidence"
            if args.raw or not row.get("calibrated_confidence")
            else "calibrated_confidence"
        )
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            parser.error(f"row {index}: missing numeric {key}")
        records.append(
            {
                "confidence": value,
                "correct": _bool(row.get("correct", "")),
                "condition": (row.get(args.condition_column) or "default").strip() or "default",
            }
        )

    policy = fit_policy(
        records,
        false_accept_budget=args.false_accept_budget,
        risk_control=args.risk_control,
        delta=args.delta,
        min_samples=args.min_samples,
        condition_key="condition",
        dataset_id=args.dataset_id,
        calibration_profile_version=args.calibration_profile_version,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(policy.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "dataset_id": policy.dataset_id,
                "n": policy.default.n,
                "false_accept_budget": policy.false_accept_budget,
                "risk_control": policy.risk_control,
                "delta": policy.delta,
                "min_samples": policy.min_samples,
                "conditions": [row["condition"] for row in policy_summary(policy)],
                "notes": policy.notes,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
