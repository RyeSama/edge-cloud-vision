from __future__ import annotations

import asyncio

import pytest

from ecvision.cloud.decision import CloudDecisionService, fallback_decision
from ecvision.cloud.settings import CloudSettings
from ecvision.config import MqttConfig
from tests.helpers import make_event


def make_settings(api_key: str = "") -> CloudSettings:
    return CloudSettings(
        mqtt=MqttConfig(),
        llm_base_url="https://example.invalid/v1",
        llm_api_key=api_key,
        llm_model="test-model",
        llm_timeout_s=1.0,
        store_size=10,
        queue_size=10,
    )


def test_fallback_is_conservative():
    decision = fallback_decision("evt-1", "timeout")
    assert decision.risk_level == "unknown"
    assert decision.action == "request_human_review"
    assert decision.source == "rule_fallback"


def test_no_api_key_degrades_without_network():
    service = CloudDecisionService(make_settings())
    decision = asyncio.run(service.decide(make_event()))
    asyncio.run(service.aclose())
    assert decision.source == "rule_fallback"
    assert decision.risk_level == "unknown"


def test_parse_accepts_fenced_json():
    service = CloudDecisionService(make_settings())
    event = make_event()
    content = (
        '```json\n{"risk_level":"high","action":"stop_conveyor","rationale":"person in cell"}\n```'
    )
    decision = service._parse(event, content, started=0.0)
    assert decision.risk_level == "high"
    assert decision.source == "llm"
    assert decision.action == "stop_conveyor"


def test_parse_rejects_invalid_risk():
    service = CloudDecisionService(make_settings())
    with pytest.raises(ValueError):
        service._parse(
            make_event(),
            '{"risk_level":"critical","action":"stop","rationale":"x"}',
            started=0.0,
        )
