"""Deterministic stimulus-response bench for the acquisition chain.

The bench is deliberately model-agnostic: it takes any classifier that implements
``predict(frame, top_k)``. Synthetic targets exercise timing, gate stability, and
repeatability; ``--source-image`` + ``--ground-truth`` switches to a controlled
image perturbation matrix for a real model.

It is not an accuracy benchmark by itself. With synthetic targets there is no
ground truth label, so the report only claims stability/latency, not recognition
accuracy.
"""

from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from ecvision.edge.classifier import Prediction
from ecvision.edge.gate import GateConfig, GateResult, evaluate


class BenchClassifier(Protocol):
    model_name: str

    def predict(self, frame: np.ndarray, top_k: int = 3) -> Prediction: ...


@dataclass(frozen=True, slots=True)
class StimulusCase:
    """One controlled operating point.

    ``distance_m`` scales the target, ``lux`` scales brightness, ``blur_sigma``
    is a Gaussian blur in pixels, and ``occlusion_ratio`` covers the target from
    the right. ``ground_truth`` is optional; omit it for synthetic targets.
    """

    name: str
    distance_m: float = 1.0
    lux: int = 500
    blur_sigma: float = 0.0
    occlusion_ratio: float = 0.0
    seed: int = 7
    ground_truth: str | None = None

    def __post_init__(self) -> None:
        if self.distance_m <= 0 or self.lux <= 0:
            raise ValueError("distance and lux must be positive")
        if not 0.0 <= self.occlusion_ratio < 1.0:
            raise ValueError("occlusion_ratio must be in [0, 1)")
        if self.blur_sigma < 0:
            raise ValueError("blur_sigma must be >= 0")


@dataclass(frozen=True, slots=True)
class BenchSample:
    case: str
    repeat: int
    label: str
    confidence: float
    latency_ms: float
    decision: str
    ground_truth: str | None
    correct: bool | None


DEFAULT_CASES: tuple[StimulusCase, ...] = (
    StimulusCase("baseline-1m-500lux", distance_m=1.0, lux=500),
    StimulusCase("far-3m", distance_m=3.0, lux=500),
    StimulusCase("near-0.5m", distance_m=0.5, lux=500),
    StimulusCase("dim-80lux", distance_m=1.0, lux=80),
    StimulusCase("bright-1500lux", distance_m=1.0, lux=1500),
    StimulusCase("blur-2px", distance_m=1.0, lux=500, blur_sigma=2.0),
    StimulusCase("occlusion-30pct", distance_m=1.0, lux=500, occlusion_ratio=0.3),
    StimulusCase("far-dim-blur", distance_m=2.5, lux=120, blur_sigma=1.5),
)


def render_stimulus(
    case: StimulusCase,
    *,
    width: int = 320,
    height: int = 240,
    base_image: np.ndarray | None = None,
) -> np.ndarray:
    """Render a deterministic BGR frame for one case."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    rng = np.random.default_rng(case.seed)

    if base_image is None:
        canvas = np.full((height, width, 3), 32, dtype=np.uint8)
        target_w = max(8, int(width * 0.16 / max(0.2, case.distance_m)))
        target_h = max(8, int(height * 0.20 / max(0.2, case.distance_m)))
        cx, cy = width // 2, height // 2
        cv2.rectangle(
            canvas,
            (cx - target_w // 2, cy - target_h // 2),
            (cx + target_w // 2, cy + target_h // 2),
            (40, 40, 200),
            -1,
        )
        cv2.circle(canvas, (cx, cy), max(3, target_w // 6), (220, 220, 220), -1)
    else:
        canvas = _fit(base_image, width, height)
        target_w = max(8, int(width * 0.16 / max(0.2, case.distance_m)))
        target_h = max(8, int(height * 0.20 / max(0.2, case.distance_m)))
        # Scale the whole scene around the center to simulate distance.
        scale = min(1.0, target_w / max(1, width * 0.16))
        if scale < 0.98:
            small = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            canvas = np.full((height, width, 3), 24, dtype=np.uint8)
            y0 = (height - small.shape[0]) // 2
            x0 = (width - small.shape[1]) // 2
            canvas[y0 : y0 + small.shape[0], x0 : x0 + small.shape[1]] = small

    gain = min(1.6, max(0.25, case.lux / 500.0))
    canvas = np.clip(canvas.astype(np.float32) * gain, 0, 255).astype(np.uint8)

    if case.blur_sigma > 0:
        kernel = _odd_kernel(case.blur_sigma)
        canvas = cv2.GaussianBlur(canvas, (kernel, kernel), case.blur_sigma)

    if case.occlusion_ratio > 0:
        cover = int(width * case.occlusion_ratio)
        canvas[:, width - cover :] = (8, 8, 8)

    noise = rng.normal(0.0, 1.5, canvas.shape).astype(np.float32)
    return np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def run_benchmark(
    classifier: BenchClassifier,
    cases: tuple[StimulusCase, ...] = DEFAULT_CASES,
    *,
    repeats: int = 3,
    width: int = 320,
    height: int = 240,
    gate: GateConfig | None = None,
    base_image: np.ndarray | None = None,
) -> list[BenchSample]:
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    cfg = gate or GateConfig()
    samples: list[BenchSample] = []
    for case in cases:
        for repeat in range(repeats):
            frame = render_stimulus(case, width=width, height=height, base_image=base_image)
            started = time.perf_counter()
            prediction = classifier.predict(frame, top_k=3)
            latency_ms = (time.perf_counter() - started) * 1000.0
            top1 = prediction.top1
            result: GateResult = evaluate(top1.confidence, cfg)
            correct = None if case.ground_truth is None else top1.label == case.ground_truth
            samples.append(
                BenchSample(
                    case=case.name,
                    repeat=repeat,
                    label=top1.label,
                    confidence=top1.confidence,
                    latency_ms=round(latency_ms, 3),
                    decision=result.decision.value,
                    ground_truth=case.ground_truth,
                    correct=correct,
                )
            )
    return samples


def summarize(samples: list[BenchSample]) -> dict[str, object]:
    if not samples:
        return {"cases": [], "sample_count": 0}
    grouped: dict[str, list[BenchSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.case, []).append(sample)
    cases: list[dict[str, object]] = []
    for name, group in grouped.items():
        confidences = [sample.confidence for sample in group]
        latencies = [sample.latency_ms for sample in group]
        accuracies = [sample.correct for sample in group if sample.correct is not None]
        cases.append(
            {
                "case": name,
                "n": len(group),
                "accept_rate": round(
                    sum(sample.decision == "accept" for sample in group) / len(group), 4
                ),
                "confidence_mean": round(statistics.fmean(confidences), 4),
                "confidence_std": round(statistics.pstdev(confidences), 4)
                if len(confidences) > 1
                else 0.0,
                "latency_p50_ms": round(_percentile(latencies, 0.50), 3),
                "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
                "accuracy": round(sum(1 for value in accuracies if value) / len(accuracies), 4)
                if accuracies
                else None,
                "labels": sorted({sample.label for sample in group}),
            }
        )
    cases.sort(key=lambda row: str(row["case"]))
    return {"sample_count": len(samples), "cases": cases}


def write_csv(samples: list[BenchSample], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(BenchSample.__dataclass_fields__))
        writer.writeheader()
        for sample in samples:
            writer.writerow(asdict(sample))


def write_markdown(summary: dict[str, object], path: str | Path, *, model: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = summary.get("cases", [])
    lines = [
        "# Stimulus-response bench",
        "",
        f"Model: `{model}`",
        f"Samples: {summary.get('sample_count', 0)}",
        "",
        "| case | n | accept rate | confidence mean | confidence std | p50 ms | p95 ms | accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        accuracy = "n/a" if row["accuracy"] is None else f"{row['accuracy']:.3f}"
        lines.append(
            f"| {row['case']} | {row['n']} | {row['accept_rate']:.3f} | "
            f"{row['confidence_mean']:.3f} | {row['confidence_std']:.3f} | "
            f"{row['latency_p50_ms']:.2f} | {row['latency_p95_ms']:.2f} | {accuracy} |"
        )
    lines.extend(
        [
            "",
            "Synthetic cases have no ground-truth label, so `accuracy` is `n/a`;",
            "they measure timing, gate stability, and repeatability, not recognition accuracy.",
        ]
    )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fit(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if image.shape[1] == width and image.shape[0] == height:
        return image.copy()
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def _odd_kernel(sigma: float) -> int:
    return max(3, int(math.ceil(sigma * 4)) | 1)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(quantile * (len(ordered) - 1)))))
    return ordered[index]
