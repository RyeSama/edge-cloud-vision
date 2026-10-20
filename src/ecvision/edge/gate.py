"""Confidence gate: the first of the two filters.

Policy (default, matching the project spec):
  confidence >= 0.90                 -> accept
  0.50 <= confidence < 0.90          -> review (ambiguous)
  confidence < 0.50                  -> review (low_confidence), not silently
                                        dropped: a missed hazard costs more
                                        than one extra LLM call.

The gate is pure and unit-testable; no model or network objects cross it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ecvision.contracts import GateDecision, GateInfo, GateReason


@dataclass(frozen=True, slots=True)
class GateConfig:
    accept_threshold: float = 0.90
    review_threshold: float = 0.50

    def __post_init__(self) -> None:
        if not 0.0 <= self.review_threshold <= self.accept_threshold <= 1.0:
            raise ValueError("require 0 <= review_threshold <= accept_threshold <= 1")


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: GateDecision
    reason: GateReason

    def to_info(
        self,
        config: GateConfig,
        *,
        calibrated_confidence: float | None = None,
        calibration_version: str | None = None,
        conformal_qhat: float | None = None,
        conformal_set: list[str] | None = None,
    ) -> GateInfo:
        return GateInfo(
            decision=self.decision,
            reason=self.reason,
            accept_threshold=config.accept_threshold,
            review_threshold=config.review_threshold,
            calibrated_confidence=calibrated_confidence,
            calibration_version=calibration_version,
            conformal_qhat=conformal_qhat,
            conformal_set=conformal_set or [],
        )


def evaluate(confidence: float, config: GateConfig | None = None) -> GateResult:
    cfg = config or GateConfig()
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    if confidence >= cfg.accept_threshold:
        return GateResult(GateDecision.ACCEPT, GateReason.HIGH_CONFIDENCE)
    if confidence >= cfg.review_threshold:
        return GateResult(GateDecision.REVIEW, GateReason.AMBIGUOUS)
    return GateResult(GateDecision.REVIEW, GateReason.LOW_CONFIDENCE)
