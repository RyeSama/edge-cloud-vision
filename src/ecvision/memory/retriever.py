"""Hybrid temporal-semantic retrieval with reciprocal rank fusion.

Pipeline:
1. Parse a natural-language time expression into a half-open UTC window.
2. Hard-filter SQLite by time, device, and label.
3. Rank candidates by recency.
4. Rank the same candidate set by vector similarity.
5. Fuse the two rankings with weighted RRF.
6. Build a token-bounded context.

If the vector backend fails, step 4 is replaced by SQLite text search and the
result is marked ``degraded``; the caller still gets a usable timeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timezone

from ecvision.contracts import utc_now
from ecvision.memory.context import ContextBuilder
from ecvision.memory.embedder import Embedder
from ecvision.memory.models import MemoryItem, MemoryQuery, MemoryQueryResult
from ecvision.memory.repository import EventRepository
from ecvision.memory.timeparse import DEFAULT_TIMEZONE, parse_time_window
from ecvision.memory.vector import VectorIndex


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    candidate_limit: int = 300
    rrf_k: int = 60
    time_weight: float = 0.4
    semantic_weight: float = 0.6

    def __post_init__(self) -> None:
        if self.candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        if self.time_weight < 0 or self.semantic_weight < 0:
            raise ValueError("RRF weights must be non-negative")
        if self.time_weight + self.semantic_weight == 0:
            raise ValueError("at least one RRF weight must be positive")


def rrf_fuse(
    time_ranked_ids: list[str],
    semantic_ranked_ids: list[str],
    *,
    k: int = 60,
    time_weight: float = 0.4,
    semantic_weight: float = 0.6,
) -> dict[str, float]:
    """Weighted reciprocal rank fusion.

    ``k`` damps the contribution of top ranks. The function is pure so the
    weighting policy can be tested without a database or an embedding model.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    scores: dict[str, float] = {}
    for rank, event_id in enumerate(time_ranked_ids, start=1):
        scores[event_id] = scores.get(event_id, 0.0) + time_weight / (k + rank)
    for rank, event_id in enumerate(semantic_ranked_ids, start=1):
        scores[event_id] = scores.get(event_id, 0.0) + semantic_weight / (k + rank)
    return scores


class HybridRetriever:
    def __init__(
        self,
        repository: EventRepository,
        vector_index: VectorIndex,
        embedder: Embedder,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        config: RetrievalConfig | None = None,
        llm_enabled: bool = False,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self._repo = repository
        self._vector = vector_index
        self._embedder = embedder
        self._timezone_name = timezone_name
        self.config = config or RetrievalConfig()
        self._llm_enabled = llm_enabled
        self._context = context_builder or ContextBuilder()

    def retrieve(self, query: MemoryQuery) -> MemoryQueryResult:
        started = time.perf_counter()
        now = query.now or utc_now()
        window = parse_time_window(query.question, now, timezone_name=self._timezone_name)
        start = window.start.astimezone(timezone.utc)
        end = window.end.astimezone(timezone.utc)
        limit = self.config.candidate_limit

        items_by_id: dict[str, MemoryItem] = {}
        for item in self._repo.list_events(
            start, end, device_id=query.device_id, label=query.label, limit=limit
        ):
            items_by_id[item.event_id] = item
        for item in self._repo.list_summaries(
            start, end, device_id=query.device_id, label=query.label, limit=limit
        ):
            items_by_id[item.event_id] = item
        for item in self._repo.list_cold(
            start, end, device_id=query.device_id, label=query.label, limit=limit
        ):
            items_by_id[item.event_id] = item

        degraded = False
        warnings: list[str] = []
        time_ids = [
            item.event_id
            for item in sorted(items_by_id.values(), key=lambda item: item.ts, reverse=True)
        ]

        semantic_ids: list[str] = []
        if items_by_id:
            try:
                query_vector = self._embedder.embed_text(query.question)
                hits = self._vector.query(
                    query_vector,
                    top_k=min(max(query.limit * 4, 20), len(items_by_id)),
                    allowed_ids=set(items_by_id),
                )
                semantic_ids = [event_id for event_id, _ in hits if event_id in items_by_id]
                if not semantic_ids:
                    warnings.append("向量索引没有命中，已仅按时间排序")
            except Exception as exc:  # noqa: BLE001 - degrade, never fail the query
                degraded = True
                warnings.append(
                    f"向量检索失败，回退到 SQLite 模糊查询：{type(exc).__name__}: {exc}"
                )
                fallback = self._repo.search_text(
                    query.label or query.question,
                    start,
                    end,
                    device_id=query.device_id,
                    label=query.label,
                    limit=max(query.limit * 2, 20),
                )
                for item in fallback:
                    items_by_id.setdefault(item.event_id, item)
                semantic_ids = [item.event_id for item in fallback]
                time_ids = [
                    item.event_id
                    for item in sorted(items_by_id.values(), key=lambda item: item.ts, reverse=True)
                ]

        scores = rrf_fuse(
            time_ids,
            semantic_ids,
            k=self.config.rrf_k,
            time_weight=self.config.time_weight,
            semantic_weight=self.config.semantic_weight,
        )
        time_rank = {event_id: rank for rank, event_id in enumerate(time_ids, start=1)}
        semantic_rank = {event_id: rank for rank, event_id in enumerate(semantic_ids, start=1)}

        ranked = sorted(
            items_by_id.values(),
            key=lambda item: (scores.get(item.event_id, 0.0), item.ts),
            reverse=True,
        )[: query.limit]
        ranked = [
            item.model_copy(
                update={
                    "score": round(scores.get(item.event_id, 0.0), 8),
                    "time_rank": time_rank.get(item.event_id),
                    "semantic_rank": semantic_rank.get(item.event_id),
                }
            )
            for item in ranked
        ]

        context_format = query.format
        if context_format == "auto":
            context_format = "markdown" if self._llm_enabled else "json"
        context = self._context.build(
            ranked,
            token_budget=query.token_budget,
            format=context_format,
        )
        return MemoryQueryResult(
            question=query.question,
            window=window,
            items=ranked,
            context=context,
            degraded=degraded,
            warnings=warnings,
            total_candidates=len(items_by_id),
            retrieval_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
