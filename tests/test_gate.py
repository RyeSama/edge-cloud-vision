from __future__ import annotations

import pytest

from ecvision.contracts import GateDecision, GateReason
from ecvision.edge.gate import GateConfig, evaluate


@pytest.mark.parametrize(
    ("confidence", "decision", "reason"),
    [
        (1.0, GateDecision.ACCEPT, GateReason.HIGH_CONFIDENCE),
        (0.90, GateDecision.ACCEPT, GateReason.HIGH_CONFIDENCE),
        (0.899, GateDecision.REVIEW, GateReason.AMBIGUOUS),
        (0.50, GateDecision.REVIEW, GateReason.AMBIGUOUS),
        (0.499, GateDecision.REVIEW, GateReason.LOW_CONFIDENCE),
        (0.0, GateDecision.REVIEW, GateReason.LOW_CONFIDENCE),
    ],
)
def test_gate_boundaries(confidence, decision, reason):
    result = evaluate(confidence)
    assert result.decision == decision
    assert result.reason == reason


def test_gate_rejects_invalid_config():
    with pytest.raises(ValueError):
        GateConfig(accept_threshold=0.4, review_threshold=0.6)


def test_gate_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        evaluate(1.01)
