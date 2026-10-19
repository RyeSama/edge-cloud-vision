from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ecvision.contracts import Event
from ecvision.transport.base import encode_event
from tests.helpers import make_event


def test_event_round_trip_and_compact_payload():
    event = make_event()
    encoded = encode_event(event)

    assert len(encoded) < 2048
    assert b"image" not in encoded.lower()
    assert b"jpeg" not in encoded.lower()
    parsed = Event.model_validate(json.loads(encoded))
    assert parsed.event_id == event.event_id
    assert parsed.gate.decision.value == "accept"


def test_event_forbids_raw_pixels():
    payload = json.loads(make_event().model_dump_json())
    payload["image_b64"] = "AAAA"
    with pytest.raises(ValidationError):
        Event.model_validate(payload)
