"""Edge-Mem orchestration service.

This is the only object the FastAPI layer needs: it owns the durable store, the
vector index, the decay policy, and the optional answer generator. Every
external dependency is injected, which is what makes the fallback paths testable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ecvision.contracts import Decision, Event
from ecvision.memory.answer import Answerer, OpenAIMemoryAnswerer
from ecvision.memory.compression import TemporalCompressor, TemporalDecayPolicy
from ecvision.memory.embedder import Embedder, HashingEmbedder, event_text
from ecvision.memory.models import (
    CompressionReport,
    MemoryAnswer,
    MemoryQuery,
    MemoryQueryResult,
    MemoryStats,
    VectorRecord,
)
from ecvision.memory.repository import EventRepository, SQLiteEventRepository
from ecvision.memory.retriever import HybridRetriever
from ecvision.memory.vector import (
    VectorIndex,
    build_vector_index,
)

log = logging.getLogger(__name__)


class MemorySettings(Protocol):
    """Structural settings so memory does not import the cloud package."""

    llm_enabled: bool
    memory_db_path: str
    memory_timezone: str
    memory_hot_minutes: int
    memory_warm_hours: int
    memory_bucket_minutes: int
    memory_vector_backend: str
    memory_chroma_path: str
    memory_vector_dim: int
    memory_auto_compress: bool
    memory_late_seconds: int


class MemoryService:
    def __init__(
        self,
        repository: EventRepository,
        vector_index: VectorIndex,
        embedder: Embedder,
        *,
        compressor: TemporalCompressor,
        retriever: HybridRetriever,
        llm_enabled: bool,
        answerer: Answerer | None = None,
        startup_warnings: list[str] | None = None,
        allowed_lateness_s: int = 300,
    ) -> None:
        self._repo = repository
        self._vector = vector_index
        self._embedder = embedder
        self._compressor = compressor
        self._retriever = retriever
        self._llm_enabled = llm_enabled
        self._answerer = answerer
        self.allowed_lateness_s = max(0, int(allowed_lateness_s))
        self.last_vector_error: str | None = None
        self.last_storage_error: str | None = None
        self.startup_warnings = startup_warnings or []
        self._watermark = None
        self._late_count = 0

    @classmethod
    def from_settings(cls, settings: MemorySettings) -> MemoryService:
        repository = SQLiteEventRepository(settings.memory_db_path)
        embedder = HashingEmbedder(settings.memory_vector_dim)
        vector, warning = build_vector_index(
            settings.memory_vector_backend,
            dimension=settings.memory_vector_dim,
            persist_dir=settings.memory_chroma_path,
        )
        policy = TemporalDecayPolicy(
            hot_window=timedelta(minutes=settings.memory_hot_minutes),
            warm_window=timedelta(hours=settings.memory_warm_hours),
            bucket=timedelta(minutes=settings.memory_bucket_minutes),
        )
        compressor = TemporalCompressor(
            repository,
            embedder=embedder,
            vector_index=vector,
            policy=policy,
        )
        retriever = HybridRetriever(
            repository,
            vector,
            embedder,
            timezone_name=settings.memory_timezone,
            llm_enabled=settings.llm_enabled,
        )
        service = cls(
            repository,
            vector,
            embedder,
            compressor=compressor,
            retriever=retriever,
            llm_enabled=settings.llm_enabled,
            answerer=OpenAIMemoryAnswerer(settings) if settings.llm_enabled else None,
            startup_warnings=[warning] if warning else [],
            allowed_lateness_s=getattr(settings, "memory_late_seconds", 300),
        )
        service.reindex()
        if settings.memory_auto_compress:
            try:
                service.compress()
            except Exception as exc:  # noqa: BLE001 - startup should still serve queries
                log.warning("startup compression failed: %s", exc)
                service.startup_warnings.append(f"startup compression failed: {exc}")
        return service

    # ------------------------------------------------------------------ writes
    def ingest(
        self,
        event: Event,
        decision: Decision | None = None,
        *,
        ingest_time: datetime | None = None,
    ) -> bool:
        ingest_time = ingest_time or datetime.now(timezone.utc)
        if ingest_time.tzinfo is None:
            raise ValueError("ingest_time must include a timezone")
        late = False
        if self._watermark is None or event.ts > self._watermark:
            self._watermark = event.ts
        else:
            late = event.ts < self._watermark - timedelta(seconds=self.allowed_lateness_s)
        try:
            added = self._repo.add_event(event, decision, ingest_time=ingest_time, late=late)
        except Exception as exc:  # noqa: BLE001 - memory must not break ingest
            self.last_storage_error = f"{type(exc).__name__}: {exc}"
            log.error("memory ingest failed for %s: %s", event.event_id, self.last_storage_error)
            return False
        if added:
            if late:
                self._late_count += 1
            self._index_event(event, decision)
        return added

    def attach_decision(self, event_id: str, decision: Decision) -> bool:
        try:
            attached = self._repo.attach_decision(event_id, decision)
        except Exception as exc:  # noqa: BLE001 - v0.1 mainline must continue
            self.last_storage_error = f"{type(exc).__name__}: {exc}"
            log.error("memory attach failed for %s: %s", event_id, self.last_storage_error)
            return False
        if attached:
            record = self._repo.get_event(event_id)
            if record is not None:
                self._index_event(record.event, decision)
        return attached

    def _index_event(self, event: Event, decision: Decision | None) -> None:
        try:
            self._vector.upsert(
                [
                    VectorRecord(
                        id=event.event_id,
                        vector=self._embedder.embed_text(event_text(event, decision)),
                        text=event_text(event, decision),
                        metadata={
                            "tier": "hot",
                            "device_id": event.device_id,
                            "label": event.inference.label,
                        },
                    )
                ]
            )
        except Exception as exc:  # noqa: BLE001 - indexing is best-effort
            self.last_vector_error = f"{type(exc).__name__}: {exc}"
            log.warning("vector ingest failed for %s: %s", event.event_id, self.last_vector_error)

    def compress(self, now=None) -> CompressionReport:
        return self._compressor.compress(now)

    def reindex(self) -> int:
        """Rebuild the in-memory index after a restart.

        Chroma persistence already has vectors, but upsert is idempotent, so the
        same path works for both backends.
        """
        records: list[VectorRecord] = []
        try:
            for item in self._repo.all_hot(limit=10000):
                records.append(
                    VectorRecord(
                        id=item.event_id,
                        vector=self._embedder.embed_text(item.text),
                        text=item.text,
                        metadata={
                            "tier": "hot",
                            "device_id": item.device_id,
                            "label": item.label,
                        },
                    )
                )
            for item in self._repo.all_summaries(limit=10000):
                records.append(
                    VectorRecord(
                        id=item.event_id,
                        vector=self._embedder.embed_text(item.text),
                        text=item.text,
                        metadata={
                            "tier": "warm",
                            "device_id": item.device_id,
                            "label": item.label,
                        },
                    )
                )
            if records:
                self._vector.upsert(records)
            return len(records)
        except Exception as exc:  # noqa: BLE001 - reindex cannot break startup
            self.last_vector_error = f"{type(exc).__name__}: {exc}"
            log.warning("reindex failed: %s", self.last_vector_error)
            return 0

    # ------------------------------------------------------------------ reads
    def query(self, query: MemoryQuery) -> MemoryQueryResult:
        if query.compress:
            try:
                self.compress(query.now)
            except Exception as exc:  # noqa: BLE001 - query should still run
                log.warning("pre-query compression failed: %s", exc)
        return self._retriever.retrieve(query)

    async def ask(self, query: MemoryQuery) -> MemoryAnswer:
        result = await asyncio.to_thread(self.query, query)
        if not self._llm_enabled or self._answerer is None:
            return MemoryAnswer(
                question=query.question,
                answer=None,
                context=result.context,
                mode="json_timeline",
                warnings=result.warnings,
            )
        answer = await self._answerer.answer(query.question, result.context)
        if answer is None:
            return MemoryAnswer(
                question=query.question,
                answer=None,
                context=result.context,
                mode="degraded",
                warnings=[*result.warnings, "LLM 未返回有效答案，已保留检索时间轴"],
            )
        return MemoryAnswer(
            question=query.question,
            answer=answer,
            context=result.context,
            mode="llm",
            warnings=result.warnings,
        )

    def stats(self) -> MemoryStats:
        try:
            counts = self._repo.counts()
        except Exception as exc:  # noqa: BLE001 - stats must not fail healthz
            self.last_storage_error = f"{type(exc).__name__}: {exc}"
            counts = {"events": 0, "summaries": 0, "cold_events": 0}
        try:
            vector_count = self._vector.count()
            healthy = self._vector.healthy()
        except Exception as exc:  # noqa: BLE001 - stats must not fail healthz
            self.last_vector_error = f"{type(exc).__name__}: {exc}"
            vector_count = 0
            healthy = False
        return MemoryStats(
            events=counts["events"],
            summaries=counts["summaries"],
            cold_events=counts["cold_events"],
            vectors=vector_count,
            vector_backend=self._vector.backend,
            vector_healthy=healthy,
            last_vector_error=self.last_vector_error,
            late_events=self._late_count,
            watermark=self._watermark,
            allowed_lateness_s=self.allowed_lateness_s,
        )

    async def aclose(self) -> None:
        if self._answerer is not None:
            await self._answerer.aclose()
        self._repo.close()
