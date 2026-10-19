"""Transport boundary.

The edge pipeline depends on this Protocol, not on MQTT. A future Socket or
serial adapter can be dropped in without touching capture/inference/gating.
"""

from __future__ import annotations

import json
import sys
from typing import Protocol, runtime_checkable

from ecvision.contracts import Event


@runtime_checkable
class EventPublisher(Protocol):
    def start(self) -> None: ...

    def publish(self, event: Event) -> bool: ...

    def stop(self) -> None: ...

    @property
    def stats(self) -> dict[str, int | bool]: ...


def encode_event(event: Event) -> bytes:
    """Compact UTF-8 JSON: the only payload that crosses the network."""
    return json.dumps(
        event.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class DryRunPublisher:
    """Writes JSONL to stdout; useful before a broker exists."""

    def __init__(self) -> None:
        self._published = 0
        self._bytes = 0

    def start(self) -> None:
        return None

    def publish(self, event: Event) -> bool:
        payload = encode_event(event)
        sys.stdout.write(payload.decode("utf-8") + "\n")
        sys.stdout.flush()
        self._published += 1
        self._bytes += len(payload)
        return True

    def stop(self) -> None:
        return None

    @property
    def stats(self) -> dict[str, int | bool]:
        return {"connected": True, "published": self._published, "payload_bytes": self._bytes}
