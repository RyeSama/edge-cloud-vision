from __future__ import annotations

from ecvision.contracts import (
    Event,
    GateDecision,
    GateInfo,
    GateReason,
    InferenceInfo,
    SourceInfo,
    TopK,
)


def make_event(confidence: float = 0.93, decision: GateDecision = GateDecision.ACCEPT) -> Event:
    reason = GateReason.HIGH_CONFIDENCE if decision == GateDecision.ACCEPT else GateReason.AMBIGUOUS
    return Event(
        device_id="edge-cam-01",
        seq=7,
        source=SourceInfo(width=320, height=240, fps=15),
        inference=InferenceInfo(
            model="mobilenet_v1_1.0_224_quant",
            label="person",
            label_id=1,
            confidence=confidence,
            latency_ms=18.5,
            top_k=[
                TopK(label="person", confidence=confidence),
                TopK(label="bicycle", confidence=max(0.0, 1.0 - confidence - 0.05)),
            ],
        ),
        gate=GateInfo(
            decision=decision,
            reason=reason,
            accept_threshold=0.90,
            review_threshold=0.50,
        ),
    )
