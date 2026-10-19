"""Deterministic, dependency-light embeddings and text projections.

The default embedder is a hashed bag-of-words/character model. It is not a
replacement for a real sentence encoder, but it makes the memory layer fully
offline, reproducible in tests, and swappable: ``Embedder`` is the seam where a
sentence-transformers or API embedding model can be plugged in later.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from ecvision.contracts import Decision, Event
from ecvision.memory.models import EventSummary, MemoryItem

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


class Embedder(Protocol):
    dimension: int

    def embed_text(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def event_text(event: Event, decision: Decision | None = None) -> str:
    top = " ".join(f"{item.label}:{item.confidence:.2f}" for item in event.inference.top_k[:3])
    parts = [
        f"label={event.inference.label}",
        f"device={event.device_id}",
        f"gate={event.gate.decision.value}",
        f"reason={event.gate.reason.value}",
        f"confidence={event.inference.confidence:.3f}",
        f"top={top}",
    ]
    if decision is not None:
        parts.extend(
            [
                f"risk={decision.risk_level}",
                f"action={decision.action}",
                f"rationale={decision.rationale}",
            ]
        )
    return " ".join(parts)


def summary_text(summary: EventSummary) -> str:
    risks = ",".join(f"{key}:{value}" for key, value in sorted(summary.risk_counts.items()))
    actions = ",".join(f"{key}:{value}" for key, value in sorted(summary.action_counts.items()))
    return (
        f"{summary.count} 次 {summary.label} 事件，设备 {summary.device_id}，"
        f"时间 {summary.first_ts.isoformat()} 至 {summary.last_ts.isoformat()}，"
        f"平均置信度 {summary.avg_confidence:.3f}，最高 {summary.max_confidence:.3f}，"
        f"风险 {risks or 'none'}，动作 {actions or 'none'}"
    )


def item_text(item: MemoryItem) -> str:
    return item.text


class HashingEmbedder:
    """Hashed character/word n-grams with a stable hash.

    ``hashlib`` is used instead of Python's ``hash()`` because the latter is
    salted per process and would make persisted vectors unusable after restart.
    """

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 16:
            raise ValueError("dimension must be >= 16")
        self.dimension = dimension

    def _features(self, text: str) -> list[tuple[str, float]]:
        normalized = text.lower().strip()
        tokens = _TOKEN_RE.findall(normalized)
        features: list[tuple[str, float]] = [(token, 1.0) for token in tokens]
        # Character bigrams give Chinese phrases a little more signal without
        # requiring a tokenizer or a downloaded model.
        cjk = "".join(
            token for token in tokens if len(token) == 1 and "\u4e00" <= token <= "\u9fff"
        )
        for index in range(len(cjk) - 1):
            features.append((cjk[index : index + 2], 0.5))
        return features

    def embed_text(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]
