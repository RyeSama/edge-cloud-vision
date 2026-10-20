#!/usr/bin/env python3
"""Evaluate a labelled image manifest and write an auditable measurement report.

The manifest is CSV with at least ``image_path`` and ``label``. Optional columns
(``condition_id``, ``distance_m``, ``lux``, ``degradation``, ``repeat``, ``split``)
are carried into per-condition breakdowns. Paths are resolved relative to the
manifest file unless absolute.

This is a *measurement* script, not a leaderboard: it reports top-1 accuracy,
gate coverage/review, false-accept accounting, calibration error, conformal
coverage, and latency percentiles with the sample count attached.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.contracts import GateDecision  # noqa: E402
from ecvision.edge.classifier import TFLiteClassifier  # noqa: E402
from ecvision.edge.gate import GateConfig, evaluate  # noqa: E402
from ecvision.instrument.acceptance import (  # noqa: E402
    AcceptancePolicy,
    policy_summary,
)
from ecvision.instrument.calibration import (  # noqa: E402
    CalibrationGate,
    CalibrationProfile,
    ConformalProfile,
    brier_score,
    expected_calibration_error,
    reliability_bins,
)

CONDITION_KEYS = ("distance_m", "lux", "degradation")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[rank], 3)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _fmt(value: Any) -> Any:
    return "—" if value is None else value


def _load_manifest(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("manifest is empty")
        missing = {"image_path", "label"} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"manifest missing columns: {sorted(missing)}")
        for line_number, row in enumerate(reader, start=2):
            image_path = (row.get("image_path") or "").strip()
            label = (row.get("label") or "").strip()
            if not image_path and not label:
                continue
            if not image_path or not label:
                raise ValueError(f"line {line_number}: image_path and label are required")
            clean = {key: (value or "").strip() for key, value in row.items() if key}
            clean["image_path"] = image_path
            clean["label"] = label
            rows.append(clean)
    if not rows:
        raise ValueError("manifest has no usable rows")
    return rows


def _resolve(path_text: str, manifest: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else manifest.parent / path


def _load_calibration(
    profile_path: Path | None, conformal_path: Path | None
) -> CalibrationGate | None:
    if profile_path is None and conformal_path is None:
        return None
    profile = (
        CalibrationProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        if profile_path is not None
        else None
    )
    conformal = (
        ConformalProfile.model_validate_json(conformal_path.read_text(encoding="utf-8"))
        if conformal_path is not None
        else None
    )
    return CalibrationGate(profile=profile, conformal=conformal)


def _group_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(key, "") or "") for key in CONDITION_KEYS)


def _summarize(records: list[dict[str, Any]], model_name: str, gate: GateConfig) -> dict[str, Any]:
    total = len(records)
    accepted = [record for record in records if record["accepted"]]
    accepted_correct = [record for record in accepted if record["correct"]]
    accepted_wrong = [record for record in accepted if not record["correct"]]
    reviewed_correct = [
        record for record in records if not record["accepted"] and record["correct"]
    ]

    confidences = [record["calibrated_confidence"] for record in records]
    truths = [record["correct"] for record in records]
    latencies = [record["latency_ms"] for record in records]

    conformal_values = [
        record["conformal_hit"] for record in records if record["conformal_hit"] is not None
    ]
    conformal_coverage = (
        _mean(conformal_values) if len(conformal_values) == len(records) and records else None
    )
    summary: dict[str, Any] = {
        "dataset": {
            "n": total,
            "model": model_name,
            "gate": {
                "accept_threshold": gate.accept_threshold,
                "review_threshold": gate.review_threshold,
            },
        },
        "headline": {
            "accuracy": _mean([1.0 if record["correct"] else 0.0 for record in records]),
            "coverage": round(len(accepted) / total, 6),
            "review_rate": round(1.0 - len(accepted) / total, 6),
            "selective_accuracy": round(len(accepted_correct) / len(accepted), 6)
            if accepted
            else None,
            "false_accept_rate": round(len(accepted_wrong) / total, 6),
            "false_accept_among_accepted": round(len(accepted_wrong) / len(accepted), 6)
            if accepted
            else None,
            "false_review_rate": round(len(reviewed_correct) / total, 6),
            "conformal_coverage": conformal_coverage,
            "ece": expected_calibration_error(confidences, truths),
            "brier": brier_score(confidences, truths),
            "latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "p99": _percentile(latencies, 0.99),
                "max": round(max(latencies), 3) if latencies else None,
            },
        },
        "gate_reasons": dict(Counter(record["reason"] for record in records)),
        "conditions": [],
        "confusion": {},
        "warnings": sorted({warning for record in records for warning in record["warnings"]}),
    }

    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[_group_key(record)].append(record)
    for key, group in sorted(grouped.items()):
        group_accepted = [record for record in group if record["accepted"]]
        summary["conditions"].append(
            {
                "distance_m": key[0],
                "lux": key[1],
                "degradation": key[2],
                "n": len(group),
                "accuracy": _mean([1.0 if record["correct"] else 0.0 for record in group]),
                "coverage": round(len(group_accepted) / len(group), 6),
                "mean_confidence": _mean([record["confidence"] for record in group]),
                "accept_threshold": group[0].get("accept_threshold"),
                "policy_source": group[0].get("policy_source"),
            }
        )

    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        confusion[record["label"]][record["predicted"]] += 1
    summary["confusion"] = {
        truth: dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
        for truth, counts in sorted(confusion.items())
    }
    summary["reliability_bins"] = [
        bin_.model_dump() for bin_ in reliability_bins(confidences, truths, bins=10)
    ]
    return summary


def _write_report(summary: dict[str, Any], records: list[dict[str, Any]], path: Path) -> None:
    headline = summary["headline"]
    lines = [
        "# 数据集验证报告",
        "",
        f"- 模型：`{summary['dataset']['model']}`",
        f"- 样本数：{summary['dataset']['n']}",
        f"- 全局门限（policy 可按条件覆盖）：accept ≥ {summary['dataset']['gate']['accept_threshold']}，"
        f"review ≥ {summary['dataset']['gate']['review_threshold']}",
    ]
    if summary.get("policy"):
        policy = summary["policy"]
        lines.append(
            f"- 验收策略：`{Path(policy['path']).name}`，"
            f"false-accept 预算 ≤ {policy['false_accept_budget']}，"
            f"min_samples={policy['min_samples']}"
        )
    lines += [
        "",
        "## 总览",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---:|---|",
        f"| top-1 accuracy | {_fmt(headline['accuracy'])} | 全部样本 |",
        f"| coverage（accept） | {_fmt(headline['coverage'])} | 被直接接受的比例 |",
        f"| review rate | {_fmt(headline['review_rate'])} | 进入复核的比例 |",
        f"| selective accuracy | {_fmt(headline['selective_accuracy'])} | accept 中正确的比例 |",
        f"| false accept rate | {_fmt(headline['false_accept_rate'])} | accept 且错误 / 全部 |",
        f"| false accept among accepted | {_fmt(headline['false_accept_among_accepted'])} | accept 中错误比例 |",
        f"| ECE（10 箱） | {_fmt(headline['ece'])} | 越低越好 |",
        f"| Brier | {_fmt(headline['brier'])} | 越低越好 |",
        f"| conformal coverage | {_fmt(headline['conformal_coverage'])} | 无 conformal 时为 null |",
        f"| latency p50 / p95 / p99 | {_fmt(headline['latency_ms']['p50'])} / "
        f"{_fmt(headline['latency_ms']['p95'])} / {_fmt(headline['latency_ms']['p99'])} ms | "
        "预处理+推理，不含采集等待 |",
        "",
        "> accuracy 的分母是全部样本；coverage 的分母也是全部样本。"
        "不要把 selective accuracy 当 accuracy 报。",
        "",
        "## 分条件结果",
        "",
        "| distance_m | lux | degradation | n | accuracy | coverage | mean confidence | accept_threshold |",
        "|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary["conditions"]:
        lines.append(
            f"| {row['distance_m']} | {row['lux']} | {row['degradation']} | {row['n']} | "
            f"{row['accuracy']} | {row['coverage']} | {row['mean_confidence']} | "
            f"{_fmt(row.get('accept_threshold'))} |"
        )
    if summary.get("policy"):
        lines += [
            "",
            "## 验收策略（在标定集上选择）",
            "",
            "| condition | accept_threshold | n | coverage | false accept rate | source |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for row in summary["policy"]["conditions"]:
            lines.append(
                f"| {row['condition']} | {row['accept_threshold']} | {row['n']} | "
                f"{row['coverage']} | {row['false_accept_rate']} | {row['source']} |"
            )
        for note in summary["policy"].get("notes", []):
            lines.append(f"\n> {note}")
    lines += [
        "",
        "## 混淆（truth → predicted）",
        "",
        "| truth | predicted | count |",
        "|---|---|---:|",
    ]
    for truth, counts in summary["confusion"].items():
        for predicted, count in counts.items():
            lines.append(f"| {truth} | {predicted} | {count} |")
    if summary["warnings"]:
        lines += ["", "## 警告", ""]
        lines += [f"- {warning}" for warning in summary["warnings"]]
    lines += [
        "",
        "## 口径与边界",
        "",
        "- 本报告只评估 gate 行为与标定，不证明模型在工业现场的泛化精度。",
        "- 延迟是 `preprocess_ms + inference_ms`，不含相机曝光、USB、MQTT、网络。",
        "- 样本量小的时候，ECE 和 coverage 的方差很大；请同时看每箱样本数。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--model", type=Path, default=ROOT / "models" / "mobilenet_v1_1.0_224_quant.tflite"
    )
    parser.add_argument("--labels", type=Path, default=ROOT / "models" / "labels.txt")
    parser.add_argument("--calibration-profile", type=Path, default=None)
    parser.add_argument("--conformal-profile", type=Path, default=None)
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "docs" / "measurements" / "validation"
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="AcceptancePolicy JSON; selects accept threshold per condition",
    )
    parser.add_argument(
        "--condition-column",
        default="degradation",
        help="manifest column used to select a per-condition policy (default: degradation)",
    )
    parser.add_argument(
        "--group-column",
        default="group_id",
        help="manifest column carried into predictions for group-aware calibration",
    )
    parser.add_argument("--accept-threshold", type=float, default=0.90)
    parser.add_argument("--review-threshold", type=float, default=0.50)
    parser.add_argument("--split", default=None, help="only evaluate this split value")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--num-threads", type=int, default=2)
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    gate = GateConfig(
        accept_threshold=args.accept_threshold, review_threshold=args.review_threshold
    )
    rows = _load_manifest(args.manifest)
    if args.split is not None:
        rows = [row for row in rows if row.get("split") == args.split]
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        parser.error("no rows selected")

    classifier = TFLiteClassifier(args.model, args.labels, num_threads=args.num_threads)
    calibration = _load_calibration(args.calibration_profile, args.conformal_profile)
    policy = (
        AcceptancePolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
        if args.policy is not None
        else None
    )
    policy_notes: list[str] = []
    for row in rows:
        condition = (row.get(args.condition_column) or "default").strip() or "default"
        if policy is not None and condition not in policy.conditions and condition != "default":
            policy_notes.append(f"condition '{condition}' is not in policy; using default")
    policy_notes = sorted(set(policy_notes))
    records: list[dict[str, Any]] = []
    for row in rows:
        image_path = _resolve(row["image_path"], args.manifest)
        frame = cv2.imread(str(image_path))
        if frame is None:
            parser.error(f"could not read image: {image_path}")
        prediction = classifier.predict(frame, top_k=3)
        top1 = prediction.top1
        condition = (row.get(args.condition_column) or "default").strip() or "default"
        threshold = policy.threshold_for(condition) if policy is not None else None
        gate_config = threshold.gate_config() if threshold is not None else gate
        if calibration is not None:
            decision = calibration.evaluate(
                prediction.candidates,
                probabilities=prediction.probabilities,
                labels=classifier.labels,
                config=gate_config,
            )
            gate_result = decision.gate
            calibrated = decision.calibrated_confidence
            prediction_set = list(decision.prediction_set)
            warnings = list(decision.warnings)
        else:
            gate_result = evaluate(top1.confidence, gate_config)
            calibrated = top1.confidence
            prediction_set = [top1.label]
            warnings = []
        accepted = gate_result.decision is GateDecision.ACCEPT
        correct = top1.label == row["label"]
        records.append(
            {
                "image_path": str(image_path),
                "condition_id": row.get("condition_id", ""),
                "condition": condition,
                "group_id": row.get(args.group_column, ""),
                "distance_m": row.get("distance_m", ""),
                "lux": row.get("lux", ""),
                "degradation": row.get("degradation", ""),
                "repeat": row.get("repeat", ""),
                "split": row.get("split", ""),
                "label": row["label"],
                "predicted": top1.label,
                "confidence": round(top1.confidence, 6),
                "calibrated_confidence": round(calibrated, 6),
                "accept_threshold": gate_config.accept_threshold,
                "policy_source": threshold.source if threshold is not None else "global",
                "decision": gate_result.decision.value,
                "reason": gate_result.reason.value,
                "prediction_set": "|".join(prediction_set),
                "conformal_hit": (row["label"] in prediction_set)
                if calibration is not None and calibration.conformal is not None
                else None,
                "correct": correct,
                "accepted": accepted,
                "latency_ms": round(prediction.preprocess_ms + prediction.inference_ms, 3),
                "warnings": warnings,
            }
        )

    summary = _summarize(records, classifier.model_name, gate)
    summary["dataset"]["manifest"] = str(args.manifest)
    summary["dataset"]["split"] = args.split
    summary["dataset"]["condition_column"] = args.condition_column
    summary["calibration"] = {
        "profile": str(args.calibration_profile) if args.calibration_profile else None,
        "conformal": str(args.conformal_profile) if args.conformal_profile else None,
    }
    summary["policy"] = (
        {
            "path": str(args.policy),
            "version": policy.version,
            "false_accept_budget": policy.false_accept_budget,
            "min_samples": policy.min_samples,
            "conditions": policy_summary(policy),
            "notes": policy.notes,
        }
        if policy is not None
        else None
    )
    if policy_notes:
        summary["warnings"] = sorted(set(summary["warnings"]) | set(policy_notes))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fieldnames = list(records[0].keys())
    with (args.out_dir / "predictions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in fieldnames})
    _write_report(summary, records, args.out_dir / "validation.md")
    print(json.dumps(summary["headline"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
