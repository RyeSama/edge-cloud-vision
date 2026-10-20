"""Uncertainty budget helpers following the GUM combination rule.

Each component is already expressed as a standard uncertainty in millimetres.
The sensitivity coefficient lets a caller express a component in its own unit
(e.g. 0.01 mm/px times 0.5 px) without pre-multiplying by hand.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ecvision.contracts import StrictModel


class UncertaintyComponent(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    standard_uncertainty_mm: float = Field(ge=0.0)
    sensitivity: float = Field(default=1.0)
    distribution: Literal["normal", "rectangular", "triangular"] = "normal"
    note: str = Field(default="", max_length=300)

    @property
    def contribution_mm(self) -> float:
        return abs(self.sensitivity) * self.standard_uncertainty_mm


def combined_standard_uncertainty(components: Sequence[UncertaintyComponent]) -> float:
    if not components:
        return 0.0
    return math.sqrt(sum(component.contribution_mm**2 for component in components))


def expanded_uncertainty(standard_uncertainty_mm: float, *, k: float = 2.0) -> float:
    if standard_uncertainty_mm < 0:
        raise ValueError("standard uncertainty must be >= 0")
    if k <= 0:
        raise ValueError("coverage factor k must be > 0")
    return standard_uncertainty_mm * k


def budget_rows(
    components: Sequence[UncertaintyComponent], *, k: float = 2.0
) -> list[dict[str, object]]:
    combined = combined_standard_uncertainty(components)
    total_variance = combined**2
    rows: list[dict[str, object]] = []
    for component in components:
        contribution = component.contribution_mm
        share = (contribution**2 / total_variance * 100.0) if total_variance else 0.0
        rows.append(
            {
                "name": component.name,
                "standard_uncertainty_mm": component.standard_uncertainty_mm,
                "sensitivity": component.sensitivity,
                "contribution_mm": contribution,
                "share_pct": share,
                "distribution": component.distribution,
                "note": component.note,
            }
        )
    rows.append(
        {
            "name": "combined",
            "standard_uncertainty_mm": combined,
            "sensitivity": 1.0,
            "contribution_mm": combined,
            "share_pct": 100.0 if total_variance else 0.0,
            "distribution": "normal",
            "note": f"expanded U(k={k}) = {expanded_uncertainty(combined, k=k):.6f} mm",
        }
    )
    return rows
