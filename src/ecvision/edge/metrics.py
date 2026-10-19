"""Edge-side metrics.

Only application payload bytes are counted here. That is the number the project
actually controls; TCP/TLS/MQTT framing overhead is measured separately with a
packet capture if needed. Reporting both raw-RGB and JPEG baselines prevents the
"99% saving" claim from being an artifact of an unfair baseline.
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

    def record(self, decision: str, payload_bytes: int) -> None:
        self.frames += 1
        if decision == "accept":
            self.accepted += 1
        else:
            self.review += 1
        self.json_bytes += payload_bytes

    def summary(self, width: int, height: int, fps: float) -> dict[str, float | int]:
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        raw_rgb_bytes = self.frames * width * height * 3
        # A typical MJPEG q=80 320x240 frame is ~10-20 KB; 15 KB is a labelled
        # reference value, not a measurement from this run.
        jpeg_reference_bytes = self.frames * 15_000
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
        }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(1.0 - numerator / denominator, 6)
