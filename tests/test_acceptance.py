"""Tests for per-condition acceptance policies and temperature calibration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ecvision.contracts import GateDecision
from ecvision.edge.classifier import Candidate
from ecvision.edge.gate import GateConfig
from ecvision.instrument.acceptance import (
    AcceptancePolicy,
    ConditionThreshold,
    fit_policy,
    policy_summary,
    select_threshold,
)
from ecvision.instrument.calibration import (
    CalibrationGate,
    CalibrationPoint,
    fit_calibration,
    fit_temperature,
    negative_log_likelihood,
)


def test_select_threshold_respects_false_accept_budget() -> None:
    """The lowest threshold under budget should win, not the highest."""
    result = select_threshold(
        [0.99, 0.95, 0.90, 0.85, 0.60],
        [True, True, False, True, False],
        false_accept_budget=0.20,
    )
    assert result.accept_threshold == pytest.approx(0.85)
    assert result.coverage == pytest.approx(0.8)
    assert result.false_accept_rate == pytest.approx(0.2)
    assert result.false_accept_among_accepted == pytest.approx(0.25)
    assert result.selective_accuracy == pytest.approx(0.75)
    assert result.source == "fitted"


def test_select_threshold_falls_back_when_budget_is_impossible() -> None:
    result = select_threshold(
        [0.99, 0.98, 0.97],
        [False, False, False],
        false_accept_budget=0.0,
    )
    assert result.accept_threshold == 1.0
    assert result.coverage == 0.0
    assert result.source == "fallback"


def test_fit_policy_keeps_sparse_conditions_on_default() -> None:
    records = [
        {"confidence": 0.95, "correct": True, "condition": "clean"},
        {"confidence": 0.55, "correct": False, "condition": "clean"},
        {"confidence": 0.80, "correct": True, "condition": "blur"},
        {"confidence": 0.70, "correct": False, "condition": "blur"},
    ]
    policy = fit_policy(
        records,
        false_accept_budget=0.25,
        min_samples=3,
        dataset_id="unit-test",
    )
    assert policy.default.source == "default"
    assert "blur" not in policy.conditions
    assert any("blur" in note and "min_samples" in note for note in policy.notes)
    assert policy.threshold_for("blur") is policy.default


def test_condition_threshold_rejects_inverted_limits() -> None:
    with pytest.raises(ValidationError):
        ConditionThreshold(accept_threshold=0.5, review_threshold=0.6)


def test_acceptance_policy_roundtrip_and_summary() -> None:
    policy = AcceptancePolicy(
        false_accept_budget=0.02,
        default=ConditionThreshold(accept_threshold=0.91),
        conditions={"blur": ConditionThreshold(accept_threshold=0.97, n=10, source="fitted")},
        dataset_id="unit-test",
    )
    again = AcceptancePolicy.model_validate_json(policy.model_dump_json())
    assert again.threshold_for("blur").accept_threshold == pytest.approx(0.97)
    assert again.threshold_for("missing") is again.default
    rows = {row["condition"]: row for row in policy_summary(again)}
    assert rows["blur"]["accept_threshold"] == pytest.approx(0.97)
    assert "wilson_lower_false_accept" in rows["default"]


def test_temperature_fit_reduces_nll_on_overconfident_wrong_predictions() -> None:
    points = [CalibrationPoint(confidence=0.99, correct=index % 2 == 0) for index in range(40)]
    identity_nll = negative_log_likelihood(
        [point.confidence for point in points], [point.correct for point in points]
    )
    profile = fit_temperature(points, dataset_id="unit-test")
    assert profile.method == "temperature"
    assert profile.temperature > 1.0
    assert profile.nll < identity_nll
    assert 0.0 <= profile.ece <= 1.0
    assert 0.0 <= profile.brier <= 1.0


def test_calibration_gate_accepts_per_condition_config_override() -> None:
    gate = CalibrationGate()
    candidates = [Candidate(label_id=1, label="laptop", confidence=0.86)]
    default = gate.evaluate(candidates)
    assert default.gate.decision is GateDecision.REVIEW
    tuned = gate.evaluate(candidates, config=GateConfig(accept_threshold=0.85))
    assert tuned.gate.decision is GateDecision.ACCEPT
    assert tuned.prediction_set == ("laptop",)


def test_fit_calibration_dispatch_temperature() -> None:
    points = [CalibrationPoint(confidence=0.8, correct=True) for _ in range(5)]
    profile = fit_calibration(points, method="temperature")
    assert profile.method == "temperature"
    assert profile.n == 5
