"""FastAPI edge-gateway service.

Run with:
    python -m ecvision.cloud
    # or: uvicorn ecvision.cloud.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ecvision.cloud.dashboard import DASHBOARD_HTML
from ecvision.cloud.decision import CloudDecisionService
from ecvision.cloud.settings import CloudSettings
from ecvision.cloud.store import EventStore
from ecvision.cloud.subscriber import MqttEventSubscriber
from ecvision.contracts import Event, GateDecision
from ecvision.memory.models import CompressRequest, MemoryQuery, MemoryQueryResult
from ecvision.memory.service import MemoryService

log = logging.getLogger(__name__)


async def _consume(
    queue: asyncio.Queue[Event],
    store: EventStore,
    service: CloudDecisionService,
    stats: dict[str, int],
    memory: MemoryService | None,
) -> None:
    """Single consumer gives natural backpressure; LLM calls stay serialized.

    Week 2 can add a worker pool + per-device ordering. For a demo, serial is
    easier to reason about and prevents a burst of review events from exhausting
    the LLM rate limit.
    """
    while True:
        event = await queue.get()
        try:
            stats["received"] += 1
            duplicate = store.get(event.event_id) is not None
            record = store.add(event)
            if duplicate:
                stats["duplicates"] += 1
                # QoS1 is at-least-once. Never pay for a second LLM call, and
                # never overwrite the decision already attached to this event.
                if record.decision is not None:
                    if memory is not None:
                        memory.ingest(event, record.decision)
                    continue
            if memory is not None:
                memory.ingest(event)
            if event.gate.decision == GateDecision.REVIEW:
                stats["review"] += 1
                decision = await service.decide(event)
                store.attach_decision(event.event_id, decision)
                if memory is not None:
                    memory.attach_decision(event.event_id, decision)
                stats["llm" if decision.source == "llm" else "fallback"] += 1
                log.info(
                    "review %s label=%s conf=%.2f -> risk=%s action=%s (%s, %.0f ms)",
                    event.event_id,
                    event.inference.label,
                    event.inference.confidence,
                    decision.risk_level,
                    decision.action,
                    decision.source,
                    decision.latency_ms,
                )
        except Exception:
            stats["processing_errors"] += 1
            log.exception("failed to process event %s", event.event_id)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    settings = CloudSettings.from_env()
    store = EventStore(settings.store_size)
    service = CloudDecisionService(settings)
    memory: MemoryService | None
    memory_error: str | None = None
    try:
        memory = MemoryService.from_settings(settings)
    except Exception as exc:  # noqa: BLE001 - v0.1 ingest must survive a broken memory DB
        memory = None
        memory_error = f"{type(exc).__name__}: {exc}"
        log.exception("Edge-Mem initialization failed; memory API will be unavailable")
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=settings.queue_size)
    stats = {
        "received": 0,
        "review": 0,
        "llm": 0,
        "fallback": 0,
        "dropped": 0,
        "duplicates": 0,
        "processing_errors": 0,
    }
    loop = asyncio.get_running_loop()

    def enqueue(event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            stats["dropped"] += 1
            log.warning("cloud queue full; dropping event %s", event.event_id)

    def enqueue_from_mqtt_thread(event: Event) -> None:
        try:
            loop.call_soon_threadsafe(enqueue, event)
        except RuntimeError:  # loop already closed during shutdown
            log.debug("event %s arrived after shutdown", event.event_id)

    subscriber = MqttEventSubscriber(settings.mqtt, enqueue_from_mqtt_thread)
    subscriber.start()
    consumer = asyncio.create_task(_consume(queue, store, service, stats, memory))

    app.state.settings = settings
    app.state.store = store
    app.state.service = service
    app.state.memory = memory
    app.state.memory_error = memory_error
    app.state.subscriber = subscriber
    app.state.queue = queue
    app.state.stats = stats
    log.info(
        "cloud ready: mqtt=%s:%s llm=%s model=%s",
        settings.mqtt.host,
        settings.mqtt.port,
        "enabled" if settings.llm_enabled else "fallback-only",
        settings.llm_model,
    )
    try:
        yield
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        subscriber.stop()
        await service.aclose()
        if memory is not None:
            await memory.aclose()


app = FastAPI(
    title="Edge-Cloud Vision Gateway",
    version="0.6.0",
    description="Receives edge JSON events, escalates low-confidence cases to an LLM, degrades safely.",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "edge-cloud-vision",
        "dashboard": "/dashboard",
        "docs": "/docs",
        "health": "/healthz",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """Browser HMI for a first run; no build step, no external assets."""
    return DASHBOARD_HTML


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    store: EventStore = request.app.state.store
    subscriber: MqttEventSubscriber = request.app.state.subscriber
    settings: CloudSettings = request.app.state.settings
    queue: asyncio.Queue[Event] = request.app.state.queue
    stats: dict[str, int] = request.app.state.stats
    memory: MemoryService | None = request.app.state.memory
    memory_stats = memory.stats().model_dump(mode="json") if memory is not None else None
    return {
        "status": "ok" if memory is not None and memory.last_storage_error is None else "degraded",
        "mqtt_connected": subscriber.connected,
        "mqtt": subscriber.stats,
        "queue_depth": queue.qsize(),
        "stored_events": len(store),
        "llm_enabled": settings.llm_enabled,
        "llm_last_error": request.app.state.service.last_error,
        "stats": stats,
        "memory": memory_stats,
        "memory_error": request.app.state.memory_error,
        "memory_warnings": memory.startup_warnings if memory is not None else [],
        "memory_vector_error": memory.last_vector_error if memory is not None else None,
        "memory_storage_error": memory.last_storage_error if memory is not None else None,
    }


@app.get("/api/v1/events")
async def list_events(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    store: EventStore = request.app.state.store
    return [record.model_dump(mode="json") for record in store.recent(limit)]


@app.get("/api/v1/events/{event_id}")
async def get_event(request: Request, event_id: str) -> dict:
    store: EventStore = request.app.state.store
    record = store.get(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail="event not found")
    return record.model_dump(mode="json")


@app.post("/api/v1/events", status_code=202)
async def inject_event(request: Request, event: Event) -> dict:
    """Local test hook; in production this endpoint would require device auth."""
    queue: asyncio.Queue[Event] = request.app.state.queue
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull as exc:
        raise HTTPException(status_code=429, detail="queue full") from exc
    return {"queued": True, "event_id": event.event_id}


def _require_memory(request: Request) -> MemoryService:
    memory: MemoryService | None = request.app.state.memory
    if memory is None:
        raise HTTPException(
            status_code=503,
            detail=f"Edge-Mem unavailable: {request.app.state.memory_error}",
        )
    return memory


@app.get("/api/v1/memory/stats")
async def memory_stats(request: Request) -> dict:
    return _require_memory(request).stats().model_dump(mode="json")


@app.post("/api/v1/memory/query", response_model=MemoryQueryResult)
async def memory_query(request: Request, query: MemoryQuery) -> MemoryQueryResult:
    memory = _require_memory(request)
    return await asyncio.to_thread(memory.query, query)


@app.post("/api/v1/memory/ask")
async def memory_ask(request: Request, query: MemoryQuery) -> dict:
    memory = _require_memory(request)
    answer = await memory.ask(query)
    return answer.model_dump(mode="json")


@app.post("/api/v1/memory/compress")
async def memory_compress(request: Request, body: CompressRequest) -> dict:
    memory = _require_memory(request)
    report = await asyncio.to_thread(memory.compress, body.now)
    return report.model_dump(mode="json")


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    store: EventStore = request.app.state.store
    subscriber: MqttEventSubscriber = request.app.state.subscriber
    stats: dict[str, int] = request.app.state.stats
    memory: MemoryService | None = request.app.state.memory
    memory_stats = memory.stats() if memory is not None else None
    lines = [
        "# HELP ecv_mqtt_connected MQTT connection state (1=up).",
        "# TYPE ecv_mqtt_connected gauge",
        f"ecv_mqtt_connected {int(subscriber.connected)}",
        "# HELP ecv_events_received_total Valid events received from MQTT.",
        "# TYPE ecv_events_received_total counter",
        f"ecv_events_received_total {stats['received']}",
        "# HELP ecv_events_review_total Events escalated to the cloud decision layer.",
        "# TYPE ecv_events_review_total counter",
        f"ecv_events_review_total {stats['review']}",
        "# HELP ecv_llm_calls_total Successful LLM decisions.",
        "# TYPE ecv_llm_calls_total counter",
        f"ecv_llm_calls_total {stats['llm']}",
        "# HELP ecv_llm_fallbacks_total Decisions served by the rule fallback.",
        "# TYPE ecv_llm_fallbacks_total counter",
        f"ecv_llm_fallbacks_total {stats['fallback']}",
        "# HELP ecv_events_dropped_total Events dropped before processing.",
        "# TYPE ecv_events_dropped_total counter",
        f"ecv_events_dropped_total {stats['dropped']}",
        "# HELP ecv_events_duplicates_total Duplicate deliveries suppressed (QoS1).",
        "# TYPE ecv_events_duplicates_total counter",
        f"ecv_events_duplicates_total {stats['duplicates']}",
        "# HELP ecv_events_stored Number of events in the in-memory store.",
        "# TYPE ecv_events_stored gauge",
        f"ecv_events_stored {len(store)}",
    ]
    if memory_stats is not None:
        lines.extend(
            [
                "# HELP ecv_memory_events Hot raw events in Edge-Mem SQLite.",
                "# TYPE ecv_memory_events gauge",
                f"ecv_memory_events {memory_stats.events}",
                "# HELP ecv_memory_summaries Warm aggregate rows.",
                "# TYPE ecv_memory_summaries gauge",
                f"ecv_memory_summaries {memory_stats.summaries}",
                "# HELP ecv_memory_cold_events Cold index rows.",
                "# TYPE ecv_memory_cold_events gauge",
                f"ecv_memory_cold_events {memory_stats.cold_events}",
                "# HELP ecv_memory_vectors Vectors currently indexed.",
                "# TYPE ecv_memory_vectors gauge",
                f"ecv_memory_vectors {memory_stats.vectors}",
                "# HELP ecv_memory_vector_healthy Vector backend health (1=up).",
                "# TYPE ecv_memory_vector_healthy gauge",
                f"ecv_memory_vector_healthy {int(memory_stats.vector_healthy)}",
            ]
        )
    return "\n".join(lines) + "\n"
