"""Edge-side metrics, including measurement-chain timing.

Only application payload bytes are counted here. That is the number the project
actually controls; TCP/TLS/MQTT framing overhead is measured separately with a
packet capture if needed. Reporting both raw-RGB and JPEG baselines prevents the
"99% saving" claim from being an artifact of an unfair baseline.

Timing is reported as distributions, not a single average: p50 hides tail
latency, and an instrument that only reports FPS cannot prove a deadline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class RunMetrics:
    frames: int = 0
    accepted: int = 0
    review: int = 0
    json_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)
    latencies_ms: list[float] = field(default_factory=list)
    read_ms: list[float] = field(default_factory=list)
    infer_ms: list[float] = field(default_factory=list)
    publish_ms: list[float] = field(default_factory=list)
    capture_dt_us: list[float] = field(default_factory=list)
    deadline_misses: int = 0
    dropped_frames: int = 0
    unsynced_frames: int = 0
    clock_source: str = "unknown"

    def record(self, decision: str, payload_bytes: int) -> None:
        """Compatibility path for callers that only have a decision and size."""
        self.frames += 1
        if decision == "accept":
            self.accepted += 1
        else:
            self.review += 1
        self.json_bytes += payload_bytes

    def record_sample(
        self,
        decision: str,
        payload_bytes: int,
        *,
        latency_ms: float,
        read_ms: float,
        infer_ms: float,
        publish_ms: float,
        capture_dt_us: float | None,
        dropped_since_last: int,
        deadline_missed: bool,
        clock_synced: bool,
        clock_source: str,
    ) -> None:
        self.record(decision, payload_bytes)
        self.latencies_ms.append(max(0.0, latency_ms))
        self.read_ms.append(max(0.0, read_ms))
        self.infer_ms.append(max(0.0, infer_ms))
        self.publish_ms.append(max(0.0, publish_ms))
        if capture_dt_us is not None:
            self.capture_dt_us.append(max(0.0, capture_dt_us))
        self.deadline_misses += int(deadline_missed)
        self.dropped_frames += max(0, int(dropped_since_last))
        self.unsynced_frames += int(not clock_synced)
        self.clock_source = clock_source

    def summary(self, width: int, height: int, fps: float) -> dict[str, object]:
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        raw_rgb_bytes = self.frames * width * height * 3
        # A typical MJPEG q=80 320x240 frame is ~10-20 KB; 15 KB is a labelled
        # reference value, not a measurement from this run.
        jpeg_reference_bytes = self.frames * 15_000
        period_us = 1_000_000.0 / fps if fps > 0 else 0.0
        jitter_us = [abs(value - period_us) for value in self.capture_dt_us] if period_us else []
        expected = self.frames + self.dropped_frames
        return {
            "frames": self.frames,
            "accepted": self.accepted,
            "review": self.review,
            "elapsed_s": round(elapsed, 3),
            "fps": round(self.frames / elapsed, 2),
            "configured_fps": fps,
            "json_payload_bytes": self.json_bytes,
            "raw_rgb_equivalent_bytes": raw_rgb_bytes,
            "jpeg_15kb_reference_bytes": jpeg_reference_bytes,
            "reduction_vs_raw_rgb": _ratio(self.json_bytes, raw_rgb_bytes),
            "reduction_vs_jpeg_15kb": _ratio(self.json_bytes, jpeg_reference_bytes),
            "latency_ms": _distribution(self.latencies_ms),
            "stage_ms": {
                "read": _distribution(self.read_ms),
                "infer": _distribution(self.infer_ms),
                "publish": _distribution(self.publish_ms),
            },
            "capture_dt_us": _distribution(self.capture_dt_us),
            "jitter_us": _distribution(jitter_us),
            "deadline_ms": round(1000.0 / fps, 3) if fps > 0 else 0.0,
            "deadline_misses": self.deadline_misses,
            "deadline_miss_rate": _rate(self.deadline_misses, self.frames),
            "dropped_frames": self.dropped_frames,
            "drop_rate": _rate(self.dropped_frames, expected),
            "clock": {
                "source": self.clock_source,
                "synced_frames": self.frames - self.unsynced_frames,
                "unsynced_frames": self.unsynced_frames,
            },
        }


def _distribution(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "p99": round(_percentile(ordered, 0.99), 3),
        "max": round(ordered[-1], 3),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered: list[float], quantile: float) -> float:
    if not ordered:
        return 0.0
    # Nearest-rank is intentionally conservative and reproducible.
    index = min(len(ordered) - 1, max(0, int(round(quantile * (len(ordered) - 1)))))
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(1.0 - numerator / denominator, 6)
