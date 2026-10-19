"""API-level tests with the MQTT adapter stubbed out.

These cover the FastAPI lifespan, queue/consumer wiring, the local injection
hook, and the "no API key -> conservative fallback" path without needing a
broker or network.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ecvision.cloud import main
from tests.helpers import make_event


class FakeSubscriber:
    connected = False
    stats = {"connected": False, "received": 0, "invalid": 0, "last_error": None}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "MqttEventSubscriber", FakeSubscriber)
    monkeypatch.setenv("ECV_LLM_API_KEY", "")
    return TestClient(main.app)


def _wait_for_decision(client: TestClient, event_id: str) -> dict:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/events/{event_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["decision"] is not None:
            return payload
        time.sleep(0.02)
    raise AssertionError("decision was not attached within 3 s")


def test_injected_review_event_gets_conservative_fallback(monkeypatch):
    with _client(monkeypatch) as client:
        event = make_event(confidence=0.62, decision=main.GateDecision.REVIEW)
        response = client.post("/api/v1/events", json=event.model_dump(mode="json"))
        assert response.status_code == 202

        record = _wait_for_decision(client, event.event_id)
        assert record["decision"]["source"] == "rule_fallback"
        assert record["decision"]["risk_level"] == "unknown"
        assert record["decision"]["action"] == "request_human_review"

        health = client.get("/healthz").json()
        assert health["llm_enabled"] is False
        assert health["stats"]["fallback"] == 1


def test_duplicate_event_is_idempotent_and_not_charged_twice(monkeypatch):
    with _client(monkeypatch) as client:
        event = make_event(confidence=0.62, decision=main.GateDecision.REVIEW)
        payload = event.model_dump(mode="json")

        assert client.post("/api/v1/events", json=payload).status_code == 202
        _wait_for_decision(client, event.event_id)
        assert client.post("/api/v1/events", json=payload).status_code == 202
        time.sleep(0.1)

        health = client.get("/healthz").json()
        assert health["stats"]["received"] == 2
        assert health["stats"]["review"] == 1
        assert health["stats"]["duplicates"] == 1
        assert health["stored_events"] == 1


def test_raw_image_field_is_rejected(monkeypatch):
    with _client(monkeypatch) as client:
        payload = make_event().model_dump(mode="json")
        payload["image_b64"] = "AAAA"
        assert client.post("/api/v1/events", json=payload).status_code == 422
