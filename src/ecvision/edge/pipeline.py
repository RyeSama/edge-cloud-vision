"""The edge pipeline: capture -> infer -> gate -> publish.

This is intentionally a straight line with no framework. The same order maps to
C++ on an MCU: ``camera.read()`` -> ``model.invoke()`` -> threshold -> UART/MQTT.

Timing rule: monotonic time measures every interval (read, infer, publish,
end-to-end); wall time is recorded only to join the event with other
instruments. The event payload carries both, plus the clock quality state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2

from ecvision.contracts import (
    ClockInfo,
    Event,
    GateDecision,
    GateInfo,
    InferenceInfo,
    SampleInfo,
    SourceInfo,
    TopK,
)
from ecvision.control.interlock import InterlockController, InterlockInput
from ecvision.edge.classifier import Prediction
from ecvision.edge.gate import GateConfig, GateResult, evaluate
from ecvision.edge.metrics import RunMetrics
from ecvision.edge.sources import FrameSource
from ecvision.instrument.calibration import CalibrationGate
from ecvision.instrument.clock import SamplingClock
from ecvision.instrument.spc import SpcMonitor
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
    deadline_ms: float | None = None


class EdgeRunner:
    def __init__(
        self,
        source: FrameSource,
        classifier: Classifier,
        publisher: EventPublisher,
        config: RunnerConfig,
        *,
        clock: SamplingClock | None = None,
        calibration: CalibrationGate | None = None,
        spc: SpcMonitor | None = None,
        interlock: InterlockController | None = None,
        observer: Callable[[Event], None] | None = None,
    ) -> None:
        self._source = source
        self._classifier = classifier
        self._publisher = publisher
        self._config = config
        self._clock = clock or SamplingClock()
        self._calibration = calibration
        self._spc = spc
        self._interlock = interlock
        self._observer = observer
        self._label_counts: dict[str, int] = {}

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
        last_capture_mono: int | None = None
        period_us = 1_000_000.0 / self._config.fps if self._config.fps > 0 else 0.0
        deadline_ms = self._config.deadline_ms or (1000.0 / self._config.fps)
        transport_stats: dict[str, int | bool] = {}
        self._publisher.start()
        try:
            while max_frames is None or seq < max_frames:
                # Timestamp the frame only after read() returns: that is when the
                # frame is actually available. The time spent in read() is the
                # acquisition wait (pacing/blocking), not capture->publish latency.
                loop_start = self._clock.read()
                frame = self._source.read()
                capture = self._clock.read()
                read_done = capture
                if frame is None:
                    log.info("source exhausted after %d frames", seq)
                    break
                last_width, last_height = int(frame.shape[1]), int(frame.shape[0])

                infer_start = self._clock.read()
                prediction = self._classifier.predict(frame, top_k=self._config.top_k)
                infer_done = self._clock.read()

                top1 = prediction.top1
                gate_result, gate_info = self._gate(prediction)

                emit = self._clock.read()
                capture_dt_us = (
                    None
                    if last_capture_mono is None
                    else (capture.mono_ns - last_capture_mono) / 1000.0
                )
                dropped = _estimate_dropped(capture_dt_us, period_us)
                end_to_end_ms = (emit.mono_ns - capture.mono_ns) / 1_000_000.0

                event = Event(
                    device_id=self._config.device_id,
                    seq=seq,
                    ts=capture.wall_datetime,
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
                    gate=gate_info,
                    sample=SampleInfo(
                        capture_mono_ns=capture.mono_ns,
                        capture_wall_ns=capture.wall_ns,
                        read_done_mono_ns=read_done.mono_ns,
                        infer_start_mono_ns=infer_start.mono_ns,
                        infer_done_mono_ns=infer_done.mono_ns,
                        emit_wall_ns=emit.wall_ns,
                        capture_dt_us=round(capture_dt_us, 3)
                        if capture_dt_us is not None
                        else None,
                        dropped_since_last=dropped,
                        end_to_end_ms=round(end_to_end_ms, 3),
                        deadline_ms=round(deadline_ms, 3),
                        deadline_missed=end_to_end_ms > deadline_ms,
                        clock=ClockInfo(
                            source=capture.source,
                            boot_id=capture.boot_id,
                            offset_ms=capture.offset_ms,
                            synced=capture.synced,
                        ),
                    ),
                )

                payload_size = len(encode_event(event))
                self._publisher.publish(event)
                publish_done = self._clock.read()
                total_ms = (publish_done.mono_ns - capture.mono_ns) / 1_000_000.0
                metrics.record_sample(
                    gate_result.decision.value,
                    payload_size,
                    latency_ms=total_ms,
                    read_ms=(capture.mono_ns - loop_start.mono_ns) / 1_000_000.0,
                    infer_ms=(infer_done.mono_ns - infer_start.mono_ns) / 1_000_000.0,
                    publish_ms=(publish_done.mono_ns - emit.mono_ns) / 1_000_000.0,
                    capture_dt_us=capture_dt_us,
                    dropped_since_last=dropped,
                    deadline_missed=total_ms > deadline_ms,
                    clock_synced=capture.synced,
                    clock_source=capture.source,
                )
                if self._observer is not None:
                    self._observer(event)
                if self._spc is not None:
                    self._spc.observe("confidence", top1.confidence, ts=event.ts)
                    self._spc.observe("latency_ms", total_ms, ts=event.ts)
                    self._label_counts[top1.label] = self._label_counts.get(top1.label, 0) + 1
                    if seq % max(1, self._config.log_every) == 0:
                        for alert in self._spc.update_distribution(self._label_counts, ts=event.ts):
                            log.warning("SPC %s: %s", alert.metric, alert.message)
                if self._interlock is not None:
                    output = self._interlock.update(
                        InterlockInput(
                            seq=seq,
                            detected=gate_result.decision == GateDecision.ACCEPT,
                            label=top1.label,
                            label_id=top1.label_id,
                            confidence=top1.confidence,
                            latency_ms=total_ms,
                            ts_mono_s=emit.mono_ns / 1_000_000_000.0,
                        )
                    )
                    if output.alarm:
                        log.warning("interlock ALARM seq=%d class=%s", seq, top1.label)
                seq += 1
                last_capture_mono = capture.mono_ns

                if preview:
                    if not self._show(frame, event, prediction):
                        break
                if seq % self._config.log_every == 0:
                    log.info(
                        "frames=%d accept=%d review=%d top1=%s %.1f%% gate=%s p99=%.1fms",
                        metrics.frames,
                        metrics.accepted,
                        metrics.review,
                        top1.label,
                        top1.confidence * 100,
                        gate_result.decision.value,
                        _p99(metrics.latencies_ms),
                    )
        finally:
            # Snapshot before stop(): stop() disconnects and would otherwise
            # report connected=false in the run summary.
            transport_stats = dict(self._publisher.stats)
            self._publisher.stop()
            self._source.close()

        summary = metrics.summary(width=last_width, height=last_height, fps=self._config.fps)
        summary["transport"] = transport_stats  # type: ignore[assignment]
        if self._spc is not None:
            summary["spc"] = self._spc.summary()
        if self._interlock is not None and self._interlock.last is not None:
            last = self._interlock.last
            summary["control"] = {
                "state": last.state.value,
                "alarm": last.alarm,
                "fail_safe": last.fail_safe,
                "alarm_count": last.alarm_count,
                "consecutive": last.consecutive,
            }
        log.info("run summary: %s", json.dumps(summary, ensure_ascii=False))
        if metrics_out:
            metrics_out.parent.mkdir(parents=True, exist_ok=True)
            metrics_out.write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return metrics

    def _gate(self, prediction: Prediction) -> tuple[GateResult, GateInfo]:
        if self._calibration is None:
            gate = evaluate(prediction.top1.confidence, self._config.gate)
            return gate, gate.to_info(self._config.gate)
        decision = self._calibration.evaluate(
            prediction.candidates,
            probabilities=prediction.probabilities,
            labels=getattr(self._classifier, "labels", None),
        )
        info = decision.gate.to_info(
            self._config.gate,
            calibrated_confidence=round(decision.calibrated_confidence, 6),
            calibration_version=decision.profile_version,
            conformal_qhat=decision.qhat,
            conformal_set=list(decision.prediction_set),
        )
        return decision.gate, info

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


def _estimate_dropped(capture_dt_us: float | None, period_us: float) -> int:
    if capture_dt_us is None or period_us <= 0:
        return 0
    # Only claim a drop when the gap is more than 1.5 periods; otherwise normal
    # scheduler jitter would inflate the count.
    return (
        max(0, int(round(capture_dt_us / period_us)) - 1) if capture_dt_us > 1.5 * period_us else 0
    )


def _p99(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.99 * (len(ordered) - 1))))]
