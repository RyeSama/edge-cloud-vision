"""Statistical process control for edge quality signals.

An instrument is expected to report not only a value but whether the process is
still in a known state. This module watches confidence, latency, and the label
distribution with two complementary charts:

* EWMA detects small persistent shifts.
* CUSUM detects a sustained drift after a change point.

Alerts are emitted once per cooldown window; a chart that fires on every sample
is noise, not observability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class SpcConfig:
    alpha: float = 0.2
    l_sigma: float = 3.0
    cusum_k: float = 0.5
    cusum_h: float = 5.0
    warmup: int = 8
    cooldown: int = 20
    min_sigma: float = 1e-6
    js_threshold: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        if self.l_sigma <= 0 or self.cusum_h <= 0:
            raise ValueError("control limits must be positive")
        if self.warmup < 2:
            raise ValueError("warmup must be >= 2")


@dataclass(frozen=True, slots=True)
class SpcAlert:
    metric: str
    kind: str
    value: float
    limit: float
    message: str
    ts: datetime


@dataclass(slots=True)
class _Chart:
    target: float
    ewma: float
    cusum_hi: float = 0.0
    cusum_lo: float = 0.0
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    last_alert_n: int = -10_000

    def sigma(self, floor: float) -> float:
        if self.n < 2:
            return floor
        return max(floor, math.sqrt(self.m2 / (self.n - 1)))


class SpcMonitor:
    """Monitor confidence and latency; label drift is updated separately."""

    def __init__(self, config: SpcConfig | None = None) -> None:
        self.config = config or SpcConfig()
        self._charts: dict[str, _Chart] = {}
        self._labels: dict[str, float] | None = None
        self.alerts: list[SpcAlert] = []

    def observe(self, metric: str, value: float, *, ts: datetime | None = None) -> list[SpcAlert]:
        if not math.isfinite(value):
            return []
        stamp = ts or datetime.now(timezone.utc)
        chart = self._charts.get(metric)
        if chart is None:
            chart = _Chart(target=value, ewma=value)
            self._charts[metric] = chart
            return []

        chart.n += 1
        delta = value - chart.mean
        chart.mean += delta / chart.n
        chart.m2 += delta * (value - chart.mean)
        chart.ewma = self.config.alpha * value + (1.0 - self.config.alpha) * chart.ewma
        sigma = chart.sigma(self.config.min_sigma)

        alerts: list[SpcAlert] = []
        if chart.n >= self.config.warmup:
            denominator = sigma * math.sqrt(self.config.alpha / (2.0 - self.config.alpha))
            z_value = (chart.ewma - chart.target) / max(denominator, self.config.min_sigma)
            if abs(z_value) > self.config.l_sigma and self._can_alert(chart):
                chart.last_alert_n = chart.n
                alerts.append(
                    SpcAlert(
                        metric=metric,
                        kind="ewma",
                        value=round(z_value, 3),
                        limit=self.config.l_sigma,
                        message=f"{metric} EWMA z={z_value:.2f} exceeds {self.config.l_sigma:.1f} sigma",
                        ts=stamp,
                    )
                )

            shift = value - chart.target
            chart.cusum_hi = max(0.0, chart.cusum_hi + shift - self.config.cusum_k * sigma)
            chart.cusum_lo = max(0.0, chart.cusum_lo - shift - self.config.cusum_k * sigma)
            if max(
                chart.cusum_hi, chart.cusum_lo
            ) > self.config.cusum_h * sigma and self._can_alert(chart):
                chart.last_alert_n = chart.n
                direction = "up" if chart.cusum_hi >= chart.cusum_lo else "down"
                alerts.append(
                    SpcAlert(
                        metric=metric,
                        kind="cusum",
                        value=round(max(chart.cusum_hi, chart.cusum_lo), 3),
                        limit=round(self.config.cusum_h * sigma, 3),
                        message=f"{metric} CUSUM {direction} shift exceeds h",
                        ts=stamp,
                    )
                )
        self.alerts.extend(alerts)
        return alerts

    def update_distribution(
        self, counts: dict[str, int], *, ts: datetime | None = None
    ) -> list[SpcAlert]:
        total = sum(max(0, value) for value in counts.values())
        if total <= 0:
            return []
        current = {key: value / total for key, value in counts.items()}
        if self._labels is None:
            self._labels = current
            return []
        keys = set(self._labels) | set(current)
        divergence = js_divergence(
            [self._labels.get(key, 0.0) for key in keys],
            [current.get(key, 0.0) for key in keys],
        )
        if divergence <= self.config.js_threshold:
            return []
        alert = SpcAlert(
            metric="label_distribution",
            kind="js",
            value=round(divergence, 4),
            limit=self.config.js_threshold,
            message=f"label distribution JS={divergence:.3f} exceeds threshold",
            ts=ts or datetime.now(timezone.utc),
        )
        self.alerts.append(alert)
        return [alert]

    def _can_alert(self, chart: _Chart) -> bool:
        return chart.n - chart.last_alert_n >= self.config.cooldown

    def summary(self) -> dict[str, object]:
        charts = {
            name: {
                "n": chart.n,
                "ewma": round(chart.ewma, 6),
                "target": round(chart.target, 6),
                "cusum_hi": round(chart.cusum_hi, 6),
                "cusum_lo": round(chart.cusum_lo, 6),
            }
            for name, chart in self._charts.items()
        }
        return {
            "charts": charts,
            "alerts": [
                {
                    "metric": alert.metric,
                    "kind": alert.kind,
                    "value": alert.value,
                    "limit": alert.limit,
                    "message": alert.message,
                    "ts": alert.ts.isoformat(),
                }
                for alert in self.alerts[-20:]
            ],
            "alert_count": len(self.alerts),
            "label_baseline": self._labels is not None,
        }


def js_divergence(left: list[float], right: list[float]) -> float:
    """Jensen-Shannon divergence with base 2, in [0, 1]."""
    if len(left) != len(right):
        raise ValueError("distributions must have equal length")
    if not left:
        return 0.0
    midpoint = [(a + b) / 2.0 for a, b in zip(left, right, strict=False)]
    return max(0.0, min(1.0, 0.5 * _kl(left, midpoint) + 0.5 * _kl(right, midpoint)))


def _kl(left: list[float], right: list[float]) -> float:
    total = 0.0
    for a, b in zip(left, right, strict=False):
        if a > 0.0 and b > 0.0:
            total += a * math.log2(a / b)
    return total
