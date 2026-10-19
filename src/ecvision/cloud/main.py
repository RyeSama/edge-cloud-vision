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
from fastapi.responses import PlainTextResponse

from ecvision.cloud.decision import CloudDecisionService
from ecvision.cloud.settings import CloudSettings
from ecvision.cloud.store import EventStore
from ecvision.cloud.subscriber import MqttEventSubscriber
from ecvision.contracts import Event, GateDecision

log = logging.getLogger(__name__)


async def _consume(
    queue: asyncio.Queue[Event],
    store: EventStore,
    service: CloudDecisionService,
    stats: dict[str, int],
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
                    continue
            if event.gate.decision == GateDecision.REVIEW:
                stats["review"] += 1
                decision = await service.decide(event)
                store.attach_decision(event.event_id, decision)
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
    consumer = asyncio.create_task(_consume(queue, store, service, stats))

    app.state.settings = settings
    app.state.store = store
    app.state.service = service
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


app = FastAPI(
    title="Edge-Cloud Vision Gateway",
    version="0.1.0",
    description="Receives edge JSON events, escalates low-confidence cases to an LLM, degrades safely.",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "edge-cloud-vision", "docs": "/docs", "health": "/healthz"}


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    store: EventStore = request.app.state.store
    subscriber: MqttEventSubscriber = request.app.state.subscriber
    settings: CloudSettings = request.app.state.settings
    queue: asyncio.Queue[Event] = request.app.state.queue
    stats: dict[str, int] = request.app.state.stats
    return {
        "status": "ok",
        "mqtt_connected": subscriber.connected,
        "mqtt": subscriber.stats,
        "queue_depth": queue.qsize(),
        "stored_events": len(store),
        "llm_enabled": settings.llm_enabled,
        "llm_last_error": request.app.state.service.last_error,
        "stats": stats,
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


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    store: EventStore = request.app.state.store
    subscriber: MqttEventSubscriber = request.app.state.subscriber
    stats: dict[str, int] = request.app.state.stats
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
    return "\n".join(lines) + "\n"
