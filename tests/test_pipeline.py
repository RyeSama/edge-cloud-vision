from __future__ import annotations

import numpy as np
import pytest

from ecvision.contracts import GateDecision, GateReason
from ecvision.edge.classifier import Candidate, Prediction
from ecvision.edge.gate import GateConfig
from ecvision.edge.pipeline import EdgeRunner, RunnerConfig


class OneFrameSource:
    description = "test:one-frame"

    def __init__(self) -> None:
        self._sent = False

    def read(self):
        if self._sent:
            return None
        self._sent = True
        return np.zeros((240, 320, 3), dtype=np.uint8)

    def close(self) -> None:
        return None


class FakeClassifier:
    model_name = "fake-int8"

    def predict(self, frame, top_k: int = 3) -> Prediction:
        return Prediction(
            candidates=(
                Candidate(1, "person", 0.95),
                Candidate(2, "bicycle", 0.03),
            ),
            preprocess_ms=1.0,
            inference_ms=2.0,
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.events = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def publish(self, event) -> bool:
        self.events.append(event)
        return True

    def stop(self) -> None:
        return None

    @property
    def stats(self):
        return {"connected": True, "published": len(self.events)}


def test_runner_emits_structured_accept_event():
    publisher = RecordingPublisher()
    runner = EdgeRunner(
        source=OneFrameSource(),
        classifier=FakeClassifier(),
        publisher=publisher,
        config=RunnerConfig(device_id="dev-1", fps=15, gate=GateConfig(), top_k=3),
    )
    metrics = runner.run(max_frames=1)

    assert publisher.started
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event.inference.label == "person"
    assert event.inference.latency_ms == pytest.approx(3.0)
    assert event.gate.decision == GateDecision.ACCEPT
    assert event.gate.reason == GateReason.HIGH_CONFIDENCE
    assert metrics.accepted == 1
    assert metrics.review == 0
