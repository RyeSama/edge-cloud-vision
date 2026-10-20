"""A small Gauge R&R (repeatability and reproducibility) study.

This is the one- and multi-operator average-and-range style analysis, reduced
to the numbers a project report actually needs: repeatability, reproducibility,
combined GRR, percent of tolerance, and number of distinct categories.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from math import floor, sqrt
from statistics import mean, variance

from pydantic import Field

from ecvision.contracts import StrictModel


class GaugeMeasurement(StrictModel):
    part: str = Field(min_length=1, max_length=100)
    operator: str = Field(default="op-1", min_length=1, max_length=100)
    trial: int = Field(ge=1)
    value_mm: float


class GaugeResult(StrictModel):
    n: int = Field(ge=1)
    parts: int = Field(ge=1)
    operators: int = Field(ge=1)
    trials: int = Field(ge=1)
    mean_mm: float
    repeatability_std_mm: float = Field(ge=0.0)
    reproducibility_std_mm: float = Field(ge=0.0)
    grr_std_mm: float = Field(ge=0.0)
    part_std_mm: float = Field(ge=0.0)
    ndc: int = Field(ge=0)
    tolerance_mm: float | None = Field(default=None, gt=0.0)
    grr_pct_tolerance: float | None = Field(default=None, ge=0.0)


def analyze_gauge(
    measurements: Sequence[GaugeMeasurement],
    *,
    tolerance_mm: float | None = None,
) -> GaugeResult:
    """Compute repeatability/reproducibility from a balanced or near-balanced study."""
    if not measurements:
        raise ValueError("at least one gauge measurement is required")
    if tolerance_mm is not None and tolerance_mm <= 0:
        raise ValueError("tolerance_mm must be > 0")

    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    parts: set[str] = set()
    operators: set[str] = set()
    trials: set[int] = set()
    for item in measurements:
        groups[(item.part, item.operator)].append(item.value_mm)
        parts.add(item.part)
        operators.add(item.operator)
        trials.add(item.trial)

    group_sizes = [len(values) for values in groups.values()]
    if max(group_sizes) < 2:
        raise ValueError("each part/operator group needs at least 2 trials")

    within_terms: list[float] = []
    within_dof = 0
    for values in groups.values():
        if len(values) >= 2:
            within_terms.append((len(values) - 1) * variance(values))
            within_dof += len(values) - 1
    repeatability_var = sum(within_terms) / within_dof if within_dof else 0.0
    repeatability = sqrt(max(0.0, repeatability_var))

    # Operator means average over the same parts, so the part-to-part spread
    # cancels. This is the simple balanced-design reproducibility estimate.
    operator_means = [
        mean(
            value
            for (_part, operator), values in groups.items()
            if operator == operator_id
            for value in values
        )
        for operator_id in sorted(operators)
    ]
    reproducibility = sqrt(variance(operator_means)) if len(operator_means) > 1 else 0.0
    grr = sqrt(repeatability_var + reproducibility**2)

    part_means = [
        mean(
            value
            for (part, _operator), values in groups.items()
            if part == part_id
            for value in values
        )
        for part_id in sorted(parts)
    ]
    part_std = (
        sqrt(max(0.0, variance(part_means) - repeatability_var / (len(operators) * len(trials))))
        if len(part_means) > 1
        else 0.0
    )
    ndc = floor(1.41 * part_std / grr) if grr > 0 else 0

    return GaugeResult(
        n=len(measurements),
        parts=len(parts),
        operators=len(operators),
        trials=len(trials),
        mean_mm=mean(item.value_mm for item in measurements),
        repeatability_std_mm=repeatability,
        reproducibility_std_mm=reproducibility,
        grr_std_mm=grr,
        part_std_mm=part_std,
        ndc=max(0, ndc),
        tolerance_mm=tolerance_mm,
        grr_pct_tolerance=(6.0 * grr / tolerance_mm * 100.0) if tolerance_mm else None,
    )


def gauge_rows(result: GaugeResult) -> list[dict[str, object]]:
    return [
        {"metric": "n", "value": result.n, "unit": "measurements"},
        {"metric": "parts", "value": result.parts, "unit": "parts"},
        {"metric": "operators", "value": result.operators, "unit": "operators"},
        {"metric": "repeatability", "value": result.repeatability_std_mm, "unit": "mm"},
        {"metric": "reproducibility", "value": result.reproducibility_std_mm, "unit": "mm"},
        {"metric": "GRR", "value": result.grr_std_mm, "unit": "mm"},
        {"metric": "part_std", "value": result.part_std_mm, "unit": "mm"},
        {"metric": "ndc", "value": result.ndc, "unit": "categories"},
        {
            "metric": "%GRR/tolerance",
            "value": result.grr_pct_tolerance,
            "unit": "%",
        },
    ]
