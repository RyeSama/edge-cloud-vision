"""Confidence calibration and split-conformal review.

The model's softmax score is not a probability of being correct. This module
turns a held-out calibration set into:

* an isotonic mapping from raw confidence to calibrated confidence;
* ECE/Brier/reliability diagnostics so the mapping is auditable;
* a split-conformal threshold that produces a prediction *set* rather than a
  single label.

The gate accepts only when the calibrated score clears the threshold **and**
the conformal set contains one label. Otherwise the event is reviewed. If a
profile is absent, the v0.1 raw-threshold gate is used unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from ecvision.contracts import GateDecision, GateReason, StrictModel
from ecvision.edge.classifier import Candidate
from ecvision.edge.gate import GateConfig, GateResult


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CalibrationPoint(StrictModel):
    confidence: float = Field(ge=0.0, le=1.0)
    correct: bool
    weight: float = Field(default=1.0, gt=0.0)


class ReliabilityBin(StrictModel):
    lower: float = Field(ge=0.0, le=1.0)
    upper: float = Field(gt=0.0, le=1.0)
    count: int = Field(ge=0)
    avg_confidence: float = Field(ge=0.0, le=1.0)
    accuracy: float = Field(ge=0.0, le=1.0)


class CalibrationProfile(StrictModel):
    """Calibration map plus its held-out diagnostics.

    ``method="temperature"`` applies a single-parameter logistic rescaling to
    the top-1 confidence. It is a binary-confidence calibration (correct vs
    incorrect), not a re-run of multiclass softmax; use isotonic when the
    reliability curve is clearly non-monotone and the calibration set is large.
    """

    version: str = Field(default="cal-v1", min_length=1, max_length=64)
    method: Literal["identity", "isotonic", "temperature"] = "identity"
    temperature: float = Field(default=1.0, gt=0.0, le=20.0)
    x: list[float] = Field(default_factory=list, max_length=256)
    y: list[float] = Field(default_factory=list, max_length=256)
    ece: float = Field(default=0.0, ge=0.0, le=1.0)
    brier: float = Field(default=0.0, ge=0.0, le=1.0)
    nll: float = Field(default=0.0, ge=0.0)
    n: int = Field(default=0, ge=0)
    bins: int = Field(default=10, ge=2, le=50)
    dataset_id: str = Field(default="unknown", min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def check_curve(self) -> CalibrationProfile:
        if len(self.x) != len(self.y):
            raise ValueError("x and y must have the same length")
        if any(not 0.0 <= value <= 1.0 for value in self.x + self.y):
            raise ValueError("calibration curve values must be in [0, 1]")
        if any(b <= a for a, b in zip(self.x, self.x[1:], strict=False)):
            raise ValueError("calibration x values must be strictly increasing")
        if any(b < a for a, b in zip(self.y, self.y[1:], strict=False)):
            raise ValueError("isotonic y values must be non-decreasing")
        if self.method == "temperature" and self.x:
            raise ValueError("temperature profiles must not carry an isotonic curve")
        return self

    def apply(self, confidence: float) -> float:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.method == "temperature":
            return _temperature_scale(confidence, self.temperature)
        if self.method == "identity" or not self.x:
            return confidence
        if confidence <= self.x[0]:
            return self.y[0]
        if confidence >= self.x[-1]:
            return self.y[-1]
        for index in range(1, len(self.x)):
            if confidence <= self.x[index]:
                left, right = self.x[index - 1], self.x[index]
                if right == left:
                    return self.y[index]
                ratio = (confidence - left) / (right - left)
                return self.y[index - 1] + ratio * (self.y[index] - self.y[index - 1])
        return self.y[-1]


class ConformalProfile(StrictModel):
    version: str = Field(default="conformal-v1", min_length=1, max_length=64)
    alpha: float = Field(gt=0.0, lt=1.0)
    qhat: float = Field(ge=0.0, le=1.0)
    n: int = Field(ge=1)
    dataset_id: str = Field(default="unknown", min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)


class ConformalResult(StrictModel):
    qhat: float = Field(ge=0.0, le=1.0)
    labels: list[str] = Field(min_length=1, max_length=10)
    accepted: bool


def expected_calibration_error(
    confidences: Sequence[float], correct: Sequence[bool], *, bins: int = 10
) -> float:
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have equal length")
    if not confidences:
        return 0.0
    total = len(confidences)
    error = 0.0
    for _lower, _upper, count, avg_conf, accuracy in _bin_stats(confidences, correct, bins):
        if count:
            error += (count / total) * abs(accuracy - avg_conf)
    return round(error, 6)


def reliability_bins(
    confidences: Sequence[float], correct: Sequence[bool], *, bins: int = 10
) -> list[ReliabilityBin]:
    return [
        ReliabilityBin(
            lower=lower,
            upper=upper,
            count=count,
            avg_confidence=round(avg_conf, 6),
            accuracy=round(accuracy, 6),
        )
        for lower, upper, count, avg_conf, accuracy in _bin_stats(confidences, correct, bins)
        if count
    ]


def brier_score(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have equal length")
    if not confidences:
        return 0.0
    return round(
        sum(
            (confidence - float(truth)) ** 2
            for confidence, truth in zip(confidences, correct, strict=False)
        )
        / len(confidences),
        6,
    )


def negative_log_likelihood(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    """Binary NLL of the confidence as a probability of being correct."""
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have equal length")
    if not confidences:
        return 0.0
    epsilon = 1e-9
    total = 0.0
    for confidence, truth in zip(confidences, correct, strict=False):
        p = min(1.0 - epsilon, max(epsilon, confidence))
        total -= math.log(p) if truth else math.log(1.0 - p)
    return round(total / len(confidences), 6)


def _logit(confidence: float) -> float:
    p = min(1.0 - 1e-9, max(1e-9, confidence))
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _temperature_scale(confidence: float, temperature: float) -> float:
    if temperature <= 0:
        raise ValueError("temperature must be > 0")
    return _sigmoid(_logit(confidence) / temperature)


def fit_temperature(
    points: Sequence[CalibrationPoint],
    *,
    dataset_id: str = "unknown",
    bins: int = 10,
    version: str = "cal-v1",
    minimum: float = 0.5,
    maximum: float = 5.0,
    step: float = 0.01,
) -> CalibrationProfile:
    """Fit a single temperature by grid search on binary NLL.

    No scipy dependency: the objective is one-dimensional and smooth enough
    that a 0.01 grid is adequate for a reportable engineering baseline. The
    fitted value is diagnostic, not a claim of state-of-the-art calibration.
    """
    if minimum <= 0 or maximum <= minimum or step <= 0:
        raise ValueError("require 0 < minimum < maximum and step > 0")
    usable = [p for p in points if 0.0 <= p.confidence <= 1.0]
    if not usable:
        return CalibrationProfile(
            version=version, method="identity", dataset_id=dataset_id, bins=bins
        )

    best_temperature = 1.0
    best_nll = float("inf")
    steps = int(round((maximum - minimum) / step))
    for index in range(steps + 1):
        temperature = minimum + index * step
        calibrated = [_temperature_scale(point.confidence, temperature) for point in usable]
        value = negative_log_likelihood(calibrated, [point.correct for point in usable])
        if value < best_nll:
            best_nll = value
            best_temperature = temperature

    calibrated = [_temperature_scale(point.confidence, best_temperature) for point in usable]
    truth = [point.correct for point in usable]
    return CalibrationProfile(
        version=version,
        method="temperature",
        temperature=round(best_temperature, 4),
        ece=expected_calibration_error(calibrated, truth, bins=bins),
        brier=brier_score(calibrated, truth),
        nll=negative_log_likelihood(calibrated, truth),
        n=len(usable),
        bins=bins,
        dataset_id=dataset_id,
    )


def fit_calibration(
    points: Sequence[CalibrationPoint],
    *,
    method: str = "isotonic",
    dataset_id: str = "unknown",
    bins: int = 10,
    version: str = "cal-v1",
) -> CalibrationProfile:
    """Dispatch to isotonic or temperature calibration."""
    if method == "temperature":
        return fit_temperature(points, dataset_id=dataset_id, bins=bins, version=version)
    if method == "isotonic":
        return fit_isotonic(points, dataset_id=dataset_id, bins=bins, version=version)
    if method == "identity":
        usable = [p for p in points if 0.0 <= p.confidence <= 1.0]
        truth = [p.correct for p in usable]
        confidences = [p.confidence for p in usable]
        return CalibrationProfile(
            version=version,
            method="identity",
            ece=expected_calibration_error(confidences, truth, bins=bins),
            brier=brier_score(confidences, truth),
            nll=negative_log_likelihood(confidences, truth),
            n=len(usable),
            bins=bins,
            dataset_id=dataset_id,
        )
    raise ValueError(f"unknown calibration method: {method}")


def fit_isotonic(
    points: Sequence[CalibrationPoint],
    *,
    dataset_id: str = "unknown",
    bins: int = 10,
    version: str = "cal-v1",
) -> CalibrationProfile:
    """Fit a monotone piecewise-linear map with pool-adjacent-violators.

    Duplicate confidences are merged first. A flat region is represented by its
    two endpoints with the same y, so interpolation does not invent a slope.
    """
    usable = sorted((p for p in points if 0.0 <= p.confidence <= 1.0), key=lambda p: p.confidence)
    if not usable:
        return CalibrationProfile(
            version=version, method="identity", dataset_id=dataset_id, bins=bins
        )

    grouped: list[list[float]] = []
    for point in usable:
        if grouped and grouped[-1][0] == point.confidence:
            block = grouped[-1]
            block[1] = (block[1] * block[2] + float(point.correct) * point.weight) / (
                block[2] + point.weight
            )
            block[2] += point.weight
        else:
            grouped.append([point.confidence, float(point.correct), point.weight])

    blocks: list[list[float]] = []
    for x_value, y_value, weight in grouped:
        blocks.append([x_value, x_value, y_value, weight])
        while len(blocks) >= 2 and blocks[-2][2] >= blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            total_weight = left[3] + right[3]
            merged_y = (left[2] * left[3] + right[2] * right[3]) / total_weight
            blocks.append([left[0], right[1], merged_y, total_weight])

    x_values: list[float] = []
    y_values: list[float] = []
    for left_x, right_x, y_value, _weight in blocks:
        if not x_values or x_values[-1] != left_x:
            x_values.append(left_x)
            y_values.append(max(0.0, min(1.0, y_value)))
        if right_x != x_values[-1]:
            x_values.append(right_x)
            y_values.append(max(0.0, min(1.0, y_value)))

    profile = CalibrationProfile(
        version=version,
        method="isotonic",
        x=x_values,
        y=y_values,
        n=len(usable),
        bins=bins,
        dataset_id=dataset_id,
    )
    calibrated = [profile.apply(point.confidence) for point in usable]
    truth = [point.correct for point in usable]
    return profile.model_copy(
        update={
            "ece": expected_calibration_error(calibrated, truth, bins=bins),
            "brier": brier_score(calibrated, truth),
            "nll": negative_log_likelihood(calibrated, truth),
        }
    )


def fit_conformal(
    nonconformity_scores: Sequence[float],
    *,
    alpha: float = 0.1,
    dataset_id: str = "unknown",
    version: str = "conformal-v1",
) -> ConformalProfile:
    """Split-conformal quantile (finite-sample corrected)."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    scores = sorted(max(0.0, min(1.0, float(score))) for score in nonconformity_scores)
    if not scores:
        raise ValueError("at least one calibration score is required")
    rank = math.ceil((len(scores) + 1) * (1.0 - alpha))
    qhat = scores[min(len(scores) - 1, max(0, rank - 1))]
    return ConformalProfile(
        version=version, alpha=alpha, qhat=qhat, n=len(scores), dataset_id=dataset_id
    )


def prediction_set(
    probabilities: Sequence[float], labels: Sequence[str], qhat: float
) -> ConformalResult:
    """Labels with ``1 - p(label) <= qhat``; at least the argmax is returned."""
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have equal length")
    if not probabilities:
        raise ValueError("empty probability vector")
    ranked = sorted(
        ((float(prob), str(label)) for prob, label in zip(probabilities, labels, strict=False)),
        reverse=True,
    )
    selected = [label for prob, label in ranked if 1.0 - prob <= qhat]
    if not selected:
        selected = [ranked[0][1]]
    return ConformalResult(qhat=qhat, labels=selected[:10], accepted=len(selected) == 1)


@dataclass(frozen=True, slots=True)
class CalibrationDecision:
    gate: GateResult
    calibrated_confidence: float
    prediction_set: tuple[str, ...]
    qhat: float | None
    profile_version: str | None
    warnings: tuple[str, ...] = ()


class CalibrationGate:
    """Combine a calibrated threshold with a conformal prediction set."""

    def __init__(
        self,
        *,
        profile: CalibrationProfile | None = None,
        conformal: ConformalProfile | None = None,
        config: GateConfig | None = None,
    ) -> None:
        self._profile = profile
        self._conformal = conformal
        self._config = config or GateConfig()

    @property
    def profile(self) -> CalibrationProfile | None:
        return self._profile

    @property
    def conformal(self) -> ConformalProfile | None:
        return self._conformal

    def evaluate(
        self,
        candidates: Sequence[Candidate],
        *,
        probabilities: Sequence[float] | None = None,
        labels: Sequence[str] | None = None,
        config: GateConfig | None = None,
    ) -> CalibrationDecision:
        if not candidates:
            raise ValueError("at least one candidate is required")
        cfg = config or self._config
        top1 = candidates[0]
        calibrated = (
            self._profile.apply(top1.confidence) if self._profile is not None else top1.confidence
        )
        warnings: list[str] = []
        selected: tuple[str, ...] = (top1.label,)
        qhat: float | None = None

        if self._conformal is not None:
            qhat = self._conformal.qhat
            if probabilities is None:
                warnings.append(
                    "conformal prediction requires a full probability vector; "
                    "only the top-1 score was available"
                )
            elif labels is not None and len(labels) == len(probabilities):
                result = prediction_set(probabilities, labels, qhat)
                selected = tuple(result.labels)
            elif len(probabilities) == len(candidates):
                result = prediction_set(
                    probabilities, [candidate.label for candidate in candidates], qhat
                )
                selected = tuple(result.labels)
            else:
                warnings.append(
                    "conformal prediction requires labels for the full probability "
                    "vector; only top-k candidates were available"
                )

        if len(selected) > 1:
            gate = GateResult(GateDecision.REVIEW, GateReason.AMBIGUOUS)
        elif calibrated >= cfg.accept_threshold:
            gate = GateResult(GateDecision.ACCEPT, GateReason.HIGH_CONFIDENCE)
        elif calibrated >= cfg.review_threshold:
            gate = GateResult(GateDecision.REVIEW, GateReason.AMBIGUOUS)
        else:
            gate = GateResult(GateDecision.REVIEW, GateReason.LOW_CONFIDENCE)

        return CalibrationDecision(
            gate=gate,
            calibrated_confidence=calibrated,
            prediction_set=selected,
            qhat=qhat,
            profile_version=self._profile.version if self._profile is not None else None,
            warnings=tuple(warnings),
        )

    def evaluate_confidence(
        self, confidence: float, *, config: GateConfig | None = None
    ) -> CalibrationDecision:
        """Convenience path for callers that only have the top-1 score."""
        return self.evaluate(
            [Candidate(label_id=0, label="top1", confidence=confidence)],
            probabilities=None,
            config=config,
        )


def _bin_stats(
    confidences: Sequence[float], correct: Sequence[bool], bins: int
) -> list[tuple[float, float, int, float, float]]:
    if bins < 2:
        raise ValueError("bins must be >= 2")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, truth in zip(confidences, correct, strict=False):
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        index = min(bins - 1, int(confidence * bins))
        buckets[index].append((confidence, truth))
    stats: list[tuple[float, float, int, float, float]] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        lower = index / bins
        upper = (index + 1) / bins
        avg_conf = sum(value for value, _ in bucket) / len(bucket)
        accuracy = sum(1.0 for _, truth in bucket if truth) / len(bucket)
        stats.append((lower, upper, len(bucket), avg_conf, accuracy))
    return stats
