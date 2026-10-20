from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from ecvision.contracts import GateDecision, GateReason
from ecvision.edge.classifier import Candidate, Prediction
from ecvision.edge.gate import GateConfig
from ecvision.edge.pipeline import EdgeRunner, RunnerConfig
from ecvision.instrument.clock import SamplingClock


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


def test_event_ts_is_capture_time_not_emit_time():
    wall = 1_700_000_000_000_000_000
    clock = SamplingClock(
        source="ntp",
        mono_ns_fn=lambda: 5_000_000_000,
        wall_ns_fn=lambda: wall,
    )
    publisher = RecordingPublisher()
    runner = EdgeRunner(
        source=OneFrameSource(),
        classifier=FakeClassifier(),
        publisher=publisher,
        config=RunnerConfig(device_id="dev-1", fps=15, gate=GateConfig(), top_k=3),
        clock=clock,
    )
    runner.run(max_frames=1)
    event = publisher.events[0]
    assert event.ts == datetime.fromtimestamp(wall / 1_000_000_000, tz=timezone.utc)
    assert event.sample is not None
    assert event.sample.capture_wall_ns == wall
    assert event.sample.clock.source == "ntp"
