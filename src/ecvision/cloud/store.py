"""In-memory event store for the demo.

Swap this class for SQLite/PostgreSQL in week 2; callers only use add/attach/get.
The cloud keeps the raw JPEG out of its database by construction, which is a
privacy feature, not a missing feature.
"""

from __future__ import annotations

from collections import OrderedDict

from ecvision.contracts import Decision, Event, EventRecord


class EventStore:
    def __init__(self, maxlen: int = 500) -> None:
        self._maxlen = max(1, maxlen)
        self._records: OrderedDict[str, EventRecord] = OrderedDict()

    def add(self, event: Event) -> EventRecord:
        existing = self._records.get(event.event_id)
        if existing is not None:
            # At-least-once delivery: never overwrite an existing decision.
            return existing
        record = EventRecord(event=event)
        self._records[event.event_id] = record
        while len(self._records) > self._maxlen:
            self._records.popitem(last=False)
        return record

    def attach_decision(self, event_id: str, decision: Decision) -> bool:
        record = self._records.get(event_id)
        if record is None:
            return False
        record.decision = decision
        return True

    def get(self, event_id: str) -> EventRecord | None:
        return self._records.get(event_id)

    def recent(self, limit: int = 20) -> list[EventRecord]:
        items = list(self._records.values())
        return list(reversed(items[-max(0, limit) :]))

    def __len__(self) -> int:
        return len(self._records)
