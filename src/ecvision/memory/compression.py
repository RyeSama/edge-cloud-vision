"""Temporal decay: hot detail -> warm summary -> cold index.

The transition is data-destructive by design. Keeping every raw event forever
would make the "compression" claim meaningless, so warm raw rows are replaced by
one aggregate row and cold raw rows by a minimal index row. The original event
IDs are retained in the cold index for audit, but labels/confidence/action are
the only searchable fields left.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ecvision.memory.embedder import Embedder, summary_text
from ecvision.memory.models import (
    CompressionReport,
    EventSummary,
    MemoryItem,
    VectorRecord,
)
from ecvision.memory.repository import EventRepository, iso
from ecvision.memory.vector import VectorIndex


@dataclass(frozen=True, slots=True)
class TemporalDecayPolicy:
    hot_window: timedelta = timedelta(minutes=10)
    warm_window: timedelta = timedelta(hours=2)
    bucket: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.hot_window <= timedelta(0):
            raise ValueError("hot_window must be positive")
        if self.warm_window <= self.hot_window:
            raise ValueError("warm_window must be greater than hot_window")
        if self.bucket <= timedelta(0):
            raise ValueError("bucket must be positive")


class TemporalCompressor:
    def __init__(
        self,
        repository: EventRepository,
        *,
        embedder: Embedder,
        vector_index: VectorIndex,
        policy: TemporalDecayPolicy | None = None,
    ) -> None:
        self._repo = repository
        self._embedder = embedder
        self._vector = vector_index
        self.policy = policy or TemporalDecayPolicy()

    def compress(self, now: datetime | None = None) -> CompressionReport:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        warm_end = current - self.policy.hot_window
        warm_start = current - self.policy.warm_window
        errors: list[str] = []
        summaries = 0
        warm_removed = 0
        vectors_deleted = 0

        warm_items = self._repo.list_events(warm_start, warm_end, limit=10000)
        for group in _group_by_bucket(warm_items, self.policy.bucket):
            summary = self._build_summary(group, self.policy.bucket)
            added = self._repo.add_summary(summary)
            if added:
                summaries += 1
            vectors_deleted += self._safe_delete_vectors([summary.summary_id], errors, "summary")
            try:
                self._vector.upsert([self._summary_vector(summary)])
            except Exception as exc:  # noqa: BLE001 - optional backend must not break ingest
                errors.append(f"summary vector upsert failed: {type(exc).__name__}: {exc}")
            ids = [item.event_id for item in group]
            warm_removed += self._repo.delete_events(ids)
            vectors_deleted += self._safe_delete_vectors(ids, errors, "warm")

        cold_items = self._repo.list_events(None, warm_start, limit=10000)
        cold_archived = 0
        for item in cold_items:
            if self._repo.archive_cold(item):
                cold_archived += 1
            vectors_deleted += self._safe_delete_vectors([item.event_id], errors, "cold")
        if cold_items:
            self._repo.delete_events([item.event_id for item in cold_items])

        return CompressionReport(
            now=current,
            warm_summaries=summaries,
            warm_events_removed=warm_removed,
            cold_archived=cold_archived,
            vectors_deleted=vectors_deleted,
            errors=errors,
        )

    def _build_summary(self, items: list[MemoryItem], bucket: timedelta) -> EventSummary:
        first = min(items, key=lambda item: item.ts)
        last = max(items, key=lambda item: item.ts)
        bucket_start = _floor(first.ts, bucket)
        bucket_end = bucket_start + bucket
        risk_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {}
        for item in items:
            risk = item.risk_level or "unknown"
            action = item.action or "unrecorded"
            risk_counts[risk] = risk_counts.get(risk, 0) + 1
            action_counts[action] = action_counts.get(action, 0) + 1
        digest = hashlib.sha1(
            f"{first.device_id}|{first.label}|{iso(bucket_start)}".encode()
        ).hexdigest()[:12]
        summary = EventSummary(
            summary_id=f"sum:{first.device_id[:24]}:{digest}",
            device_id=first.device_id,
            label=first.label,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            count=len(items),
            first_ts=first.ts,
            last_ts=last.ts,
            avg_confidence=sum(item.confidence for item in items) / len(items),
            max_confidence=max(item.confidence for item in items),
            risk_counts=risk_counts,
            action_counts=action_counts,
            text="pending",
        )
        return summary.model_copy(update={"text": summary_text(summary)})

    def _summary_vector(self, summary: EventSummary) -> VectorRecord:
        return VectorRecord(
            id=summary.summary_id,
            vector=self._embedder.embed_text(summary.text),
            text=summary.text,
            metadata={
                "tier": "warm",
                "device_id": summary.device_id,
                "label": summary.label,
                "count": summary.count,
            },
        )

    def _safe_delete_vectors(self, ids: list[str], errors: list[str], stage: str) -> int:
        if not ids:
            return 0
        try:
            return self._vector.delete(ids)
        except Exception as exc:  # noqa: BLE001 - report and continue compression
            errors.append(f"{stage} vector delete failed: {type(exc).__name__}: {exc}")
            return 0


def _group_by_bucket(items: list[MemoryItem], bucket: timedelta) -> list[list[MemoryItem]]:
    groups: dict[tuple[str, str, int], list[MemoryItem]] = {}
    for item in items:
        bucket_start = _floor(item.ts, bucket)
        key = (item.device_id, item.label, int(bucket_start.timestamp()))
        groups.setdefault(key, []).append(item)
    return [sorted(group, key=lambda item: item.ts) for group in groups.values()]


def _floor(value: datetime, bucket: timedelta) -> datetime:
    seconds = int(bucket.total_seconds())
    if seconds <= 0:
        raise ValueError("bucket must be positive")
    stamp = int(value.timestamp())
    return datetime.fromtimestamp(stamp - stamp % seconds, tz=timezone.utc)
