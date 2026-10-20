from __future__ import annotations

import numpy as np

from ecvision.bench.stimulus import (
    BenchSample,
    StimulusCase,
    render_stimulus,
    run_benchmark,
    summarize,
    write_csv,
    write_markdown,
)
from ecvision.edge.classifier import Candidate, Prediction


class _FixedClassifier:
    model_name = "fixed"

    def predict(self, frame: np.ndarray, top_k: int = 3) -> Prediction:
        return Prediction(
            candidates=(Candidate(label_id=0, label="red_target", confidence=0.95),),
            preprocess_ms=1.0,
            inference_ms=2.0,
            probabilities=(0.95, 0.03, 0.02),
        )


def test_render_is_deterministic_and_occlusion_darkens_right_side() -> None:
    case = StimulusCase("blur-occlusion", blur_sigma=2.0, occlusion_ratio=0.4)
    first = render_stimulus(case, width=96, height=72)
    second = render_stimulus(case, width=96, height=72)
    assert np.array_equal(first, second)
    assert float(first[:, 70:].mean()) < float(first[:, :20].mean())


def test_blur_reduces_edges() -> None:
    sharp = render_stimulus(StimulusCase("sharp"), width=96, height=72)
    blurred = render_stimulus(StimulusCase("blur", blur_sigma=3.0), width=96, height=72)
    assert float(np.abs(np.diff(blurred.astype(float), axis=1)).mean()) < float(
        np.abs(np.diff(sharp.astype(float), axis=1)).mean()
    )


def test_benchmark_summary_counts_repeats() -> None:
    cases = (StimulusCase("a"), StimulusCase("b", lux=100))
    samples = run_benchmark(_FixedClassifier(), cases, repeats=2, width=64, height=48)
    assert len(samples) == 4
    summary = summarize(samples)
    assert summary["sample_count"] == 4
    rows = {row["case"]: row for row in summary["cases"]}  # type: ignore[index]
    assert rows["a"]["n"] == 2
    assert rows["a"]["accept_rate"] == 1.0
    assert rows["a"]["latency_p50_ms"] >= 0.0
    assert rows["a"]["accuracy"] is None


def test_image_mode_reports_accuracy() -> None:
    base = np.full((48, 64, 3), 90, dtype=np.uint8)
    case = StimulusCase("image-case", ground_truth="red_target")
    samples = run_benchmark(
        _FixedClassifier(),
        (case,),
        repeats=2,
        width=64,
        height=48,
        base_image=base,
    )
    assert all(sample.correct is True for sample in samples)
    summary = summarize(samples)
    assert summary["cases"][0]["accuracy"] == 1.0  # type: ignore[index]


def test_csv_and_markdown_outputs(tmp_path) -> None:
    samples = run_benchmark(_FixedClassifier(), (StimulusCase("case"),), repeats=1)
    csv_path = tmp_path / "out.csv"
    md_path = tmp_path / "out.md"
    write_csv(samples, csv_path)
    write_markdown(summarize(samples), md_path, model="fixed")
    assert "case,repeat,label" in csv_path.read_text(encoding="utf-8")
    assert "Stimulus-response bench" in md_path.read_text(encoding="utf-8")


def test_empty_summary_is_safe() -> None:
    assert summarize([]) == {"cases": [], "sample_count": 0}
    assert BenchSample.__dataclass_fields__["case"].name == "case"
