#!/usr/bin/env python3
"""Export the Python contract to language-neutral JSON Schema.

Run after changing ``ecvision.contracts``:

    python scripts/export_schemas.py

The generated files in ``contracts/`` are the reference for the future C++
edge implementation. ``tests/test_schemas.py`` keeps them in sync.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from ecvision.contracts import Decision, Event
from ecvision.memory.models import MemoryQuery, MemoryQueryResult

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = ROOT / "contracts"


def export(model: type[BaseModel], name: str, *, require: tuple[str, ...] = ()) -> None:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    # Python fills these for convenience (uuid4 / utcnow), but a wire message
    # must carry them explicitly. Keep the cross-language schema strict.
    required = schema.setdefault("required", [])
    required.extend(field for field in require if field not in required)
    path = CONTRACTS_DIR / f"{name}.schema.json"
    path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> int:
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    export(Event, "event", require=("event_id", "ts", "schema_version"))
    export(Decision, "decision", require=("created_at",))
    export(MemoryQuery, "memory-query")
    export(MemoryQueryResult, "memory-result")
    return 0


if __name__ == "__main__":
    sys.exit(main())
