from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ecvision.memory.compression import TemporalCompressor, TemporalDecayPolicy
from ecvision.memory.context import ContextBuilder, estimate_tokens
from ecvision.memory.embedder import HashingEmbedder
from ecvision.memory.models import MemoryQuery
from ecvision.memory.repository import SQLiteEventRepository
from ecvision.memory.retriever import HybridRetriever, RetrievalConfig, rrf_fuse
from ecvision.memory.service import MemoryService
from ecvision.memory.timeparse import parse_time_window
from ecvision.memory.vector import InMemoryVectorIndex
from tests.helpers import make_event

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _service(*, llm_enabled: bool = False) -> tuple[MemoryService, SQLiteEventRepository]:
    repo = SQLiteEventRepository(":memory:")
    embedder = HashingEmbedder(64)
    vector = InMemoryVectorIndex(64)
    policy = TemporalDecayPolicy(
        hot_window=timedelta(minutes=10),
        warm_window=timedelta(hours=2),
        bucket=timedelta(minutes=15),
    )
    compressor = TemporalCompressor(repo, embedder=embedder, vector_index=vector, policy=policy)
    retriever = HybridRetriever(
        repo,
        vector,
        embedder,
        config=RetrievalConfig(candidate_limit=100, rrf_k=60),
        llm_enabled=llm_enabled,
    )
    return (
        MemoryService(
            repo,
            vector,
            embedder,
            compressor=compressor,
            retriever=retriever,
            llm_enabled=llm_enabled,
        ),
        repo,
    )


def _at(event_id: str, ts: datetime, confidence: float = 0.95):
    event = make_event(confidence=confidence)
    return event.model_copy(update={"event_id": event_id, "ts": ts})


def test_temporal_decay_moves_events_through_tiers() -> None:
    service, repo = _service()
    try:
        service.ingest(_at("hot", NOW - timedelta(minutes=2), 0.96))
        service.ingest(_at("warm", NOW - timedelta(minutes=30), 0.86))
        service.ingest(_at("cold", NOW - timedelta(hours=3), 0.76))

        report = service.compress(NOW)

        assert report.warm_summaries == 1
        assert report.warm_events_removed == 1
        assert report.cold_archived == 1
        assert report.errors == []
        stats = service.stats()
        assert stats.events == 1
        assert stats.summaries == 1
        assert stats.cold_events == 1
        assert stats.vectors == 2
    finally:
        repo.close()


def test_time_window_day_period_and_relative() -> None:
    local_now = datetime(2026, 10, 19, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    yesterday_afternoon = parse_time_window("昨天下午穿红衣服的人", local_now)
    assert yesterday_afternoon.start.date().isoformat() == "2026-10-18"
    assert (yesterday_afternoon.start.hour, yesterday_afternoon.end.hour) == (12, 18)
    assert yesterday_afternoon.end > yesterday_afternoon.start

    recent = parse_time_window("过去 30 分钟有哪些人", local_now)
    assert recent.start == local_now - timedelta(minutes=30)
    assert recent.end == local_now

    explicit = parse_time_window("2026-10-18 14:30 的告警", local_now)
    assert explicit.start.hour == 14
    assert explicit.start.minute == 30
    assert explicit.end - explicit.start == timedelta(hours=1)


def test_rrf_prefers_items_ranked_by_both_signals() -> None:
    scores = rrf_fuse(["a", "b"], ["b", "c"], k=60, time_weight=0.4, semantic_weight=0.6)

    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]
    assert set(scores) == {"a", "b", "c"}


def test_context_builder_stays_within_token_budget() -> None:
    items = [_memory_item(f"e{i}", NOW - timedelta(minutes=i), confidence=0.9) for i in range(20)]
    context = ContextBuilder().build(items, token_budget=180, format="markdown")

    assert context.item_count < len(items)
    assert context.truncated is True
    assert context.token_count <= 180
    assert estimate_tokens(context.text) <= 180


def test_vector_failure_degrades_to_sqlite_search() -> None:
    class BrokenVector(InMemoryVectorIndex):
        def query(self, *args, **kwargs):
            raise RuntimeError("vector backend down")

    repo = SQLiteEventRepository(":memory:")
    embedder = HashingEmbedder(64)
    vector = BrokenVector(64)
    compressor = TemporalCompressor(repo, embedder=embedder, vector_index=vector)
    retriever = HybridRetriever(repo, vector, embedder)
    service = MemoryService(
        repo,
        vector,
        embedder,
        compressor=compressor,
        retriever=retriever,
        llm_enabled=False,
    )
    try:
        service.ingest(_at("person-1", NOW - timedelta(minutes=2), 0.91))
        result = service.query(MemoryQuery(question="person", now=NOW, label="person"))
        assert result.degraded is True
        assert any("向量检索失败" in warning for warning in result.warnings)
        assert [item.event_id for item in result.items] == ["person-1"]
    finally:
        repo.close()


def test_no_llm_returns_json_timeline() -> None:
    service, repo = _service(llm_enabled=False)
    try:
        service.ingest(_at("e1", NOW - timedelta(minutes=1), 0.97))
        answer = asyncio.run(service.ask(MemoryQuery(question="过去 1 小时", now=NOW)))
        assert answer.mode == "json_timeline"
        assert answer.answer is None
        assert answer.context.format == "json"
    finally:
        repo.close()


def _memory_item(event_id: str, ts: datetime, confidence: float):
    from ecvision.memory.models import MemoryItem, MemoryTier

    return MemoryItem(
        event_id=event_id,
        device_id="edge-cam-01",
        ts=ts,
        label="person",
        confidence=confidence,
        tier=MemoryTier.HOT,
        text=f"person event {event_id}",
    )
