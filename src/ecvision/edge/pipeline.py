"""The edge pipeline: capture -> infer -> gate -> publish.

This is intentionally a straight line with no framework. The same order maps to
C++ on an MCU: ``camera.read()`` -> ``model.invoke()`` -> threshold -> UART/MQTT.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2

from ecvision.contracts import Event, GateDecision, InferenceInfo, SourceInfo, TopK
from ecvision.edge.classifier import Prediction
from ecvision.edge.gate import GateConfig, evaluate
from ecvision.edge.metrics import RunMetrics
from ecvision.edge.sources import FrameSource
from ecvision.transport.base import EventPublisher, encode_event

log = logging.getLogger(__name__)


class Classifier(Protocol):
    model_name: str

    def predict(self, frame, top_k: int = 3) -> Prediction: ...


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    device_id: str
    fps: float
    gate: GateConfig
    top_k: int = 3
    log_every: int = 30


class EdgeRunner:
    def __init__(
        self,
        source: FrameSource,
        classifier: Classifier,
        publisher: EventPublisher,
        config: RunnerConfig,
    ) -> None:
        self._source = source
        self._classifier = classifier
        self._publisher = publisher
        self._config = config

    def run(
        self,
        *,
        max_frames: int | None = None,
        preview: bool = False,
        metrics_out: Path | None = None,
    ) -> RunMetrics:
        metrics = RunMetrics()
        seq = 0
        last_width = last_height = 0
        transport_stats: dict[str, int | bool] = {}
        self._publisher.start()
        try:
            while max_frames is None or seq < max_frames:
                frame = self._source.read()
                if frame is None:
                    log.info("source exhausted after %d frames", seq)
                    break
                last_width, last_height = int(frame.shape[1]), int(frame.shape[0])
                prediction = self._classifier.predict(frame, top_k=self._config.top_k)
                top1 = prediction.top1
                gate = evaluate(top1.confidence, self._config.gate)

                event = Event(
                    device_id=self._config.device_id,
                    seq=seq,
                    source=SourceInfo(
                        width=int(frame.shape[1]),
                        height=int(frame.shape[0]),
                        fps=self._config.fps,
                    ),
                    inference=InferenceInfo(
                        model=self._classifier.model_name,
                        label=top1.label,
                        label_id=top1.label_id,
                        confidence=round(top1.confidence, 6),
                        latency_ms=round(prediction.preprocess_ms + prediction.inference_ms, 3),
                        top_k=[
                            TopK(label=c.label, confidence=round(c.confidence, 6))
                            for c in prediction.candidates
                        ],
                    ),
                    gate=gate.to_info(self._config.gate),
                )

                payload_size = len(encode_event(event))
                self._publisher.publish(event)
                metrics.record(gate.decision.value, payload_size)
                seq += 1

                if preview:
                    if not self._show(frame, event, prediction):
                        break
                if seq % self._config.log_every == 0:
                    log.info(
                        "frames=%d accept=%d review=%d top1=%s %.1f%% gate=%s",
                        metrics.frames,
                        metrics.accepted,
                        metrics.review,
                        top1.label,
                        top1.confidence * 100,
                        gate.decision.value,
                    )
        finally:
            # Snapshot before stop(): stop() disconnects and would otherwise
            # report connected=false in the run summary.
            transport_stats = dict(self._publisher.stats)
            self._publisher.stop()
            self._source.close()

        summary = metrics.summary(width=last_width, height=last_height, fps=self._config.fps)
        summary["transport"] = transport_stats  # type: ignore[assignment]
        log.info("run summary: %s", json.dumps(summary, ensure_ascii=False))
        if metrics_out:
            metrics_out.parent.mkdir(parents=True, exist_ok=True)
            metrics_out.write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return metrics

    @staticmethod
    def _show(frame, event: Event, prediction: Prediction) -> bool:
        """Returns False when the user pressed q. Never lets GUI errors kill a run."""
        if event.gate.decision == GateDecision.ACCEPT:
            color = (80, 200, 80)
        elif event.gate.reason.value == "ambiguous":
            color = (0, 180, 255)
        else:
            color = (60, 60, 220)
        canvas = frame.copy()
        label = f"{prediction.top1.label} {prediction.top1.confidence:.0%} [{event.gate.decision.value}]"
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 30), (20, 20, 20), -1)
        cv2.putText(canvas, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
        try:
            cv2.imshow("edge-cloud-vision (q to quit)", canvas)
            return (cv2.waitKey(1) & 0xFF) != ord("q")
        except cv2.error as exc:
            log.warning("preview disabled (no GUI backend): %s", exc)
            return True
