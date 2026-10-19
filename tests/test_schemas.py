"""The JSON Schema is a contract, not documentation: validate a real payload."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tests.helpers import make_event

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts" / "event.schema.json").read_text(encoding="utf-8"))


def test_generated_schema_matches_runtime_payload():
    payload = json.loads(make_event().model_dump_json())
    Draft202012Validator(SCHEMA).validate(payload)


def test_wire_schema_requires_identity_and_timestamp():
    required = set(SCHEMA["required"])
    assert {"event_id", "ts", "schema_version"} <= required


def test_schema_rejects_extra_image_field():
    payload = json.loads(make_event().model_dump_json())
    payload["image_b64"] = "AAAA"
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMA).validate(payload)
