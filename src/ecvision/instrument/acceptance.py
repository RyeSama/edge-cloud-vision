"""Per-condition acceptance policy: turn calibration data into thresholds.

The v0.2/v0.3 gate used one global pair of thresholds (0.90/0.50). That is a
convenience, not a measurement result: blur, low light and occlusion do not have
the same score distribution. This module fits *per-condition* accept thresholds
under an explicit false-accept budget, and falls back to a global policy when a
condition has too few samples.

The policy is deliberately offline-first. It is used by
``scripts/eval_dataset.py`` and by the v0.4 acceptance report; the runtime edge
loop keeps its single default gate unless a condition is known and supplied by
the caller.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from ecvision.contracts import StrictModel
from ecvision.edge.gate import GateConfig


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    """Wilson lower bound, used only for reporting, never for threshold choice."""
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


class ConditionThreshold(StrictModel):
    condition: str = Field(default="default", min_length=1, max_length=64)
    accept_threshold: float = Field(ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    n: int = Field(default=0, ge=0)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    false_accept_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    false_accept_among_accepted: float | None = Field(default=None, ge=0.0, le=1.0)
    selective_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    source: Literal["fitted", "default", "fallback"] = "fitted"

    @model_validator(mode="after")
    def check_thresholds(self) -> ConditionThreshold:
        if self.review_threshold > self.accept_threshold:
            raise ValueError("review_threshold must be <= accept_threshold")
        return self

    def gate_config(self) -> GateConfig:
        return GateConfig(
            accept_threshold=self.accept_threshold,
            review_threshold=self.review_threshold,
        )


class AcceptancePolicy(StrictModel):
    version: str = Field(default="policy-v1", min_length=1, max_length=64)
    dataset_id: str = Field(default="unknown", min_length=1, max_length=200)
    calibration_profile_version: str | None = Field(default=None, max_length=64)
    false_accept_budget: float = Field(ge=0.0, le=1.0)
    min_samples: int = Field(default=20, ge=1)
    default: ConditionThreshold
    conditions: dict[str, ConditionThreshold] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    def threshold_for(self, condition: str | None) -> ConditionThreshold:
        if condition and condition in self.conditions:
            return self.conditions[condition]
        return self.default


def select_threshold(
    confidences: Iterable[float],
    correct: Iterable[bool],
    *,
    condition: str = "default",
    false_accept_budget: float = 0.02,
    review_threshold: float = 0.5,
) -> ConditionThreshold:
    """Lowest threshold whose observed false-accept rate stays in budget.

    Lowering a threshold can only add accepted samples, so the false-accept
    rate is monotonically non-decreasing as the threshold falls. We therefore
    walk candidates from high to low and stop at the first violation; the last
    valid candidate is the maximum-coverage policy under the budget.
    """
    if not 0.0 <= false_accept_budget <= 1.0:
        raise ValueError("false_accept_budget must be in [0, 1]")
    pairs = [(float(c), bool(y)) for c, y in zip(confidences, correct, strict=False)]
    if not pairs:
        raise ValueError("at least one calibration point is required")
    if not 0.0 <= review_threshold <= 1.0:
        raise ValueError("review_threshold must be in [0, 1]")

    total = len(pairs)
    candidates = sorted({c for c, _ in pairs}, reverse=True)
    best: ConditionThreshold | None = None
    for threshold in candidates:
        accepted = [(c, y) for c, y in pairs if c >= threshold]
        false_accepts = sum(1 for _c, y in accepted if not y)
        far = false_accepts / total
        if far <= false_accept_budget:
            correct_accepted = sum(1 for _c, y in accepted if y)
            best = ConditionThreshold(
                condition=condition,
                accept_threshold=threshold,
                review_threshold=min(review_threshold, threshold),
                n=total,
                coverage=len(accepted) / total,
                false_accept_rate=far,
                false_accept_among_accepted=(false_accepts / len(accepted)) if accepted else None,
                selective_accuracy=(correct_accepted / len(accepted)) if accepted else None,
                source="fitted",
            )
        else:
            break

    if best is None:
        return ConditionThreshold(
            condition=condition,
            accept_threshold=1.0,
            review_threshold=min(review_threshold, 1.0),
            n=total,
            coverage=0.0,
            false_accept_rate=0.0,
            source="fallback",
        )
    return best


def fit_policy(
    records: Iterable[Mapping[str, object]],
    *,
    false_accept_budget: float = 0.02,
    min_samples: int = 20,
    review_threshold: float = 0.5,
    condition_key: str = "condition",
    dataset_id: str = "unknown",
    calibration_profile_version: str | None = None,
) -> AcceptancePolicy:
    """Fit a global default plus per-condition overrides.

    A condition is only given its own threshold when it has at least
    ``min_samples`` rows. Sparse conditions fall back to the global default;
    that is safer than fitting a threshold on five points and pretending it is
    a measurement.
    """
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    rows = []
    for index, record in enumerate(records):
        try:
            confidence = float(record["confidence"])
            correct = record["correct"]
            if not isinstance(correct, bool):
                if isinstance(correct, str):
                    correct = correct.strip().lower() in {"1", "true", "yes", "y"}
                else:
                    correct = bool(correct)
            condition = str(record.get(condition_key) or "default")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"row {index}: invalid calibration record ({exc})") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"row {index}: confidence must be in [0, 1]")
        rows.append((confidence, correct, condition))

    if not rows:
        raise ValueError("at least one calibration record is required")

    default = select_threshold(
        [row[0] for row in rows],
        [row[1] for row in rows],
        condition="default",
        false_accept_budget=false_accept_budget,
        review_threshold=review_threshold,
    )
    default = default.model_copy(update={"source": "default"})

    grouped: dict[str, list[tuple[float, bool, str]]] = {}
    for row in rows:
        grouped.setdefault(row[2], []).append(row)

    conditions: dict[str, ConditionThreshold] = {}
    notes: list[str] = []
    for condition, group in sorted(grouped.items()):
        if len(group) < min_samples:
            notes.append(
                f"condition '{condition}' has {len(group)} samples < min_samples={min_samples}; "
                "using default threshold"
            )
            continue
        threshold = select_threshold(
            [row[0] for row in group],
            [row[1] for row in group],
            condition=condition,
            false_accept_budget=false_accept_budget,
            review_threshold=review_threshold,
        )
        if threshold.source == "fallback":
            notes.append(
                f"condition '{condition}' cannot satisfy false_accept_budget="
                f"{false_accept_budget}; using default threshold"
            )
            continue
        conditions[condition] = threshold

    return AcceptancePolicy(
        dataset_id=dataset_id,
        calibration_profile_version=calibration_profile_version,
        false_accept_budget=false_accept_budget,
        min_samples=min_samples,
        default=default,
        conditions=conditions,
        notes=notes,
    )


def policy_summary(policy: AcceptancePolicy) -> list[dict[str, object]]:
    """Flat rows for reports and JSON output."""
    rows: list[dict[str, object]] = []
    for condition, threshold in sorted(
        {**{"default": policy.default}, **policy.conditions}.items()
    ):
        rows.append(
            {
                "condition": condition,
                "accept_threshold": threshold.accept_threshold,
                "review_threshold": threshold.review_threshold,
                "n": threshold.n,
                "coverage": threshold.coverage,
                "false_accept_rate": threshold.false_accept_rate,
                "false_accept_among_accepted": threshold.false_accept_among_accepted,
                "selective_accuracy": threshold.selective_accuracy,
                "wilson_lower_false_accept": _wilson_lower(
                    round(threshold.false_accept_rate * threshold.n), threshold.n
                )
                if threshold.n
                else 0.0,
                "source": threshold.source,
            }
        )
    return rows
