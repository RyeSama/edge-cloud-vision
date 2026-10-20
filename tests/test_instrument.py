from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ecvision.edge.classifier import Candidate
from ecvision.edge.gate import GateConfig
from ecvision.instrument.calibration import (
    CalibrationGate,
    CalibrationPoint,
    CalibrationProfile,
    ConformalProfile,
    brier_score,
    expected_calibration_error,
    fit_isotonic,
    prediction_set,
    reliability_bins,
)
from ecvision.instrument.clock import SamplingClock
from ecvision.instrument.spc import SpcConfig, SpcMonitor, js_divergence


def test_clock_injects_offset_and_marks_quality() -> None:
    base_wall = 1_700_000_000_000_000_000
    clock = SamplingClock(
        source="ntp",
        offset_ms=50.0,
        mono_ns_fn=lambda: 1_000_000_000,
        wall_ns_fn=lambda: base_wall,
    )
    reading = clock.read(2_000_000_000)
    assert reading.mono_ns == 2_000_000_000
    assert reading.wall_ns == base_wall + 1_050_000_000
    assert reading.source == "ntp"
    assert reading.synced is True
    assert reading.wall_datetime.tzinfo is not None


def test_system_clock_is_unsynced_until_sync() -> None:
    clock = SamplingClock(mono_ns_fn=lambda: 10, wall_ns_fn=lambda: 20)
    assert clock.synced is False
    clock.sync(offset_ms=10.0)
    assert clock.synced is True
    assert clock.read(10).offset_ms == 10.0
    clock.mark_unsynced()
    assert clock.synced is False


def test_clock_rejects_implausible_offset() -> None:
    with pytest.raises(ValueError):
        SamplingClock(offset_ms=2_000_000)


def test_ece_and_brier_for_perfect_and_biased_sets() -> None:
    assert expected_calibration_error([0.9, 0.1], [True, False]) == pytest.approx(0.1)
    assert brier_score([0.9, 0.1], [True, False]) == pytest.approx(0.01)
    bins = reliability_bins([0.9, 0.1], [True, False])
    assert len(bins) == 2
    assert sum(bin.count for bin in bins) == 2


def test_isotonic_profile_is_monotone() -> None:
    points = [
        CalibrationPoint(confidence=0.2, correct=False),
        CalibrationPoint(confidence=0.4, correct=True),
        CalibrationPoint(confidence=0.6, correct=True),
        CalibrationPoint(confidence=0.8, correct=True),
    ]
    profile = fit_isotonic(points, dataset_id="unit")
    assert isinstance(profile, CalibrationProfile)
    assert profile.method == "isotonic"
    assert profile.x == sorted(profile.x)
    assert all(a <= b for a, b in zip(profile.y, profile.y[1:], strict=False))
    assert 0.0 <= profile.apply(0.5) <= 1.0


def test_conformal_set_size_controls_acceptance() -> None:
    profile = ConformalProfile(alpha=0.1, qhat=0.2, n=100)
    result = prediction_set([0.9, 0.05, 0.05], ["a", "b", "c"], profile.qhat)
    assert result.labels == ["a"]
    assert result.accepted is True

    ambiguous = prediction_set([0.55, 0.44, 0.01], ["a", "b", "c"], 0.6)
    assert set(ambiguous.labels) == {"a", "b"}
    assert ambiguous.accepted is False


def test_calibration_gate_combines_profile_and_conformal() -> None:
    gate = CalibrationGate(
        profile=CalibrationProfile(method="identity"),
        conformal=ConformalProfile(alpha=0.1, qhat=0.2, n=50),
        config=GateConfig(accept_threshold=0.9, review_threshold=0.5),
    )
    accepted = gate.evaluate(
        [Candidate(0, "person", 0.95)],
        probabilities=[0.95, 0.03, 0.02],
        labels=["person", "bicycle", "car"],
    )
    assert accepted.gate.decision.value == "accept"
    assert accepted.prediction_set == ("person",)
    assert accepted.qhat == 0.2

    ambiguous_gate = CalibrationGate(
        profile=CalibrationProfile(method="identity"),
        conformal=ConformalProfile(alpha=0.1, qhat=0.5, n=50),
        config=GateConfig(accept_threshold=0.9, review_threshold=0.5),
    )
    ambiguous = ambiguous_gate.evaluate(
        [Candidate(0, "person", 0.95)],
        probabilities=[0.55, 0.50, 0.01],
        labels=["person", "bicycle", "car"],
    )
    assert ambiguous.gate.decision.value == "review"
    assert len(ambiguous.prediction_set) == 2


def test_conformal_without_full_labels_warns_and_falls_back_to_top1() -> None:
    gate = CalibrationGate(conformal=ConformalProfile(alpha=0.1, qhat=0.1, n=10))
    decision = gate.evaluate([Candidate(0, "person", 0.99)], probabilities=[0.99, 0.01])
    assert decision.gate.decision.value == "accept"
    assert decision.warnings


def test_spc_detects_shift_and_is_quiet_when_stable() -> None:
    monitor = SpcMonitor(SpcConfig(warmup=5, cooldown=3, js_threshold=0.2))
    stamp = datetime.now(timezone.utc)
    for _ in range(20):
        assert monitor.observe("confidence", 0.90, ts=stamp) == []

    alerts = []
    for _ in range(20):
        alerts.extend(monitor.observe("confidence", 0.40, ts=stamp))
    assert alerts
    assert any(alert.kind in {"ewma", "cusum"} for alert in alerts)
    assert monitor.summary()["alert_count"] >= 1


def test_spc_label_drift() -> None:
    monitor = SpcMonitor(SpcConfig(js_threshold=0.1))
    assert monitor.update_distribution({"person": 9, "car": 1}) == []
    alerts = monitor.update_distribution({"car": 9, "bicycle": 1})
    assert alerts
    assert alerts[0].kind == "js"
    assert js_divergence([1.0, 0.0], [0.0, 1.0]) > 0.9


def test_spc_rejects_mismatched_distribution_shapes() -> None:
    with pytest.raises(ValueError):
        js_divergence([0.5, 0.5], [1.0])


def test_spc_timestamp_is_timezone_aware() -> None:
    monitor = SpcMonitor()
    monitor.observe("latency_ms", 10.0)
    monitor.observe("latency_ms", 10.1, ts=datetime.now(timezone.utc) - timedelta(seconds=1))
    summary = monitor.summary()
    assert summary["charts"]["latency_ms"]["n"] == 1
