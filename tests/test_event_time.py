from __future__ import annotations

from datetime import timedelta

from ecvision.contracts import utc_now
from ecvision.memory.compression import TemporalCompressor
from ecvision.memory.embedder import HashingEmbedder
from ecvision.memory.models import MemoryQuery
from ecvision.memory.repository import SQLiteEventRepository
from ecvision.memory.retriever import HybridRetriever
from ecvision.memory.service import MemoryService
from ecvision.memory.vector import InMemoryVectorIndex
from tests.helpers import make_event


def _service(allowed_lateness_s: int = 60) -> tuple[MemoryService, SQLiteEventRepository]:
    repo = SQLiteEventRepository(":memory:")
    embedder = HashingEmbedder(32)
    vector = InMemoryVectorIndex(32)
    compressor = TemporalCompressor(repo, embedder=embedder, vector_index=vector)
    retriever = HybridRetriever(repo, vector, embedder)
    service = MemoryService(
        repo,
        vector,
        embedder,
        compressor=compressor,
        retriever=retriever,
        llm_enabled=False,
        allowed_lateness_s=allowed_lateness_s,
    )
    return service, repo


def test_late_events_are_flagged_and_counted() -> None:
    service, repo = _service(allowed_lateness_s=60)
    try:
        now = utc_now()
        first = make_event(confidence=0.95).model_copy(
            update={"event_id": "e1", "ts": now - timedelta(minutes=10)}
        )
        second = make_event(confidence=0.95).model_copy(
            update={"event_id": "e2", "ts": now - timedelta(seconds=1)}
        )
        late = make_event(confidence=0.95).model_copy(
            update={"event_id": "e3", "ts": now - timedelta(minutes=10)}
        )
        assert service.ingest(first, ingest_time=now - timedelta(minutes=9)) is True
        assert service.ingest(second, ingest_time=now) is True
        assert service.ingest(late, ingest_time=now) is True

        stats = service.stats()
        assert stats.events == 3
        assert stats.late_events == 1
        assert stats.allowed_lateness_s == 60
        assert stats.watermark is not None
        assert stats.watermark >= now - timedelta(seconds=1)

        result = service.query(MemoryQuery(question="person", now=now, format="json"))
        flags = {item.event_id: item.late for item in result.items}
        assert flags["e1"] is False
        assert flags["e2"] is False
        assert flags["e3"] is True
        late_item = next(item for item in result.items if item.event_id == "e3")
        assert late_item.capture_time is not None
        assert late_item.ingest_time is not None
    finally:
        repo.close()
