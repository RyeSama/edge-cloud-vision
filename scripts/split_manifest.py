#!/usr/bin/env python3
"""Split a manifest into calibration/test sets without group leakage.

If a row has ``group_id``, that is used. Otherwise the group is derived from
``condition_id`` before ``__`` (e.g. ``laptop__blur`` -> ``laptop``) or from the
image stem. All variants of one base image stay in the same split.

The split is deterministic (``--seed``) and written as two CSV files with a
``split`` column set to ``calibration`` or ``test``.
"""

from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path


def _group(row: dict[str, str]) -> str:
    explicit = (row.get("group_id") or "").strip()
    if explicit:
        return explicit
    condition = (row.get("condition_id") or "").strip()
    if "__" in condition:
        return condition.split("__", 1)[0]
    return Path(row.get("image_path", "")).stem or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--calibration-out", type=Path, required=True)
    parser.add_argument("--test-out", type=Path, required=True)
    parser.add_argument("--calibration-fraction", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    if not 0.0 < args.calibration_fraction < 1.0:
        parser.error("--calibration-fraction must be in (0, 1)")

    with args.input.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            parser.error("empty manifest")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if not rows:
        parser.error("no rows found")

    if "split" not in fieldnames:
        fieldnames.append("split")

    groups = sorted({_group(row) for row in rows})
    if len(groups) < 2:
        parser.error("need at least two groups for a group-aware split")
    random.Random(args.seed).shuffle(groups)
    calibration_count = min(
        len(groups) - 1,
        max(1, round(len(groups) * args.calibration_fraction)),
    )
    calibration_groups = set(groups[:calibration_count])

    calibration_rows: list[dict[str, str]] = []
    test_rows: list[dict[str, str]] = []
    for row in rows:
        target = calibration_rows if _group(row) in calibration_groups else test_rows
        item = {key: row.get(key, "") for key in fieldnames}
        item["split"] = "calibration" if target is calibration_rows else "test"
        target.append(item)

    for path, data in (
        (args.calibration_out, calibration_rows),
        (args.test_out, test_rows),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)

    print(
        f"groups={len(groups)} calibration_groups={len(calibration_groups)} "
        f"calibration_rows={len(calibration_rows)} test_rows={len(test_rows)}"
    )
    print(f"wrote {args.calibration_out} and {args.test_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
