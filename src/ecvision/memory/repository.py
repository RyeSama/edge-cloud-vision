"""SQLite-backed metadata store for Edge-Mem.

SQLite is deliberate: it is embedded, transactional, easy to inspect with any
SQL browser, and has no server to lose during an edge demo. The repository
interface keeps the retrieval layer independent of SQLite, so a production
deployment can swap in PostgreSQL or a TSDB without touching the retriever.

Storage tiers:
- ``events``: hot raw events, queried for the last 10 minutes.
- ``summaries``: warm aggregates, 10 minutes to 2 hours.
- ``cold_events``: minimal cold index beyond 2 hours; raw details are dropped.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ecvision.contracts import Decision, Event, EventRecord
from ecvision.memory.embedder import event_text
from ecvision.memory.models import EventSummary, MemoryItem, MemoryTier

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    ts            TEXT NOT NULL,
    label         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    decision_json TEXT,
    risk_level    TEXT,
    action        TEXT,
    event_json    TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'hot',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_label_ts ON events(label, ts);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id    TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    label         TEXT NOT NULL,
    bucket_start  TEXT NOT NULL,
    bucket_end    TEXT NOT NULL,
    count         INTEGER NOT NULL,
    first_ts      TEXT NOT NULL,
    last_ts       TEXT NOT NULL,
    avg_confidence REAL NOT NULL,
    max_confidence REAL NOT NULL,
    risk_counts   TEXT NOT NULL,
    action_counts TEXT NOT NULL,
    text          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_last_ts ON summaries(last_ts);
CREATE INDEX IF NOT EXISTS idx_summaries_device_ts ON summaries(device_id, last_ts);

CREATE TABLE IF NOT EXISTS cold_events (
    event_id      TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    ts            TEXT NOT NULL,
    label         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    risk_level    TEXT,
    action        TEXT,
    summary_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_cold_ts ON cold_events(ts);
"""


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return to_utc(value).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EventRepository(Protocol):
    def add_event(self, event: Event, decision: Decision | None = None) -> bool: ...

    def attach_decision(self, event_id: str, decision: Decision) -> bool: ...

    def get_event(self, event_id: str) -> EventRecord | None: ...

    def list_events(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]: ...

    def list_summaries(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]: ...

    def list_cold(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]: ...

    def search_text(
        self,
        term: str,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]: ...

    def add_summary(self, summary: EventSummary) -> bool: ...

    def archive_cold(self, item: MemoryItem) -> bool: ...

    def delete_events(self, ids: Iterable[str]) -> int: ...

    def all_hot(self, limit: int = 10000) -> list[MemoryItem]: ...

    def all_summaries(self, limit: int = 10000) -> list[MemoryItem]: ...

    def counts(self) -> dict[str, int]: ...

    def close(self) -> None: ...


class SQLiteEventRepository:
    def __init__(self, path: str | Path = "data/edge_mem.sqlite3") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writing
    def add_event(self, event: Event, decision: Decision | None = None) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, device_id, ts, label, confidence, decision_json,
                     risk_level, action, event_json, tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'hot', ?)
                """,
                (
                    event.event_id,
                    event.device_id,
                    iso(event.ts),
                    event.inference.label,
                    event.inference.confidence,
                    decision.model_dump_json() if decision else None,
                    decision.risk_level if decision else None,
                    decision.action if decision else None,
                    event.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def attach_decision(self, event_id: str, decision: Decision) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE events
                   SET decision_json = ?, risk_level = ?, action = ?
                 WHERE event_id = ?
                """,
                (
                    decision.model_dump_json(),
                    decision.risk_level,
                    decision.action,
                    event_id,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def add_summary(self, summary: EventSummary) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO summaries
                    (summary_id, device_id, label, bucket_start, bucket_end, count,
                     first_ts, last_ts, avg_confidence, max_confidence,
                     risk_counts, action_counts, text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.summary_id,
                    summary.device_id,
                    summary.label,
                    iso(summary.bucket_start),
                    iso(summary.bucket_end),
                    summary.count,
                    iso(summary.first_ts),
                    iso(summary.last_ts),
                    summary.avg_confidence,
                    summary.max_confidence,
                    _json_dict(summary.risk_counts),
                    _json_dict(summary.action_counts),
                    summary.text,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def archive_cold(self, item: MemoryItem) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO cold_events
                    (event_id, device_id, ts, label, confidence, risk_level, action, summary_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.event_id,
                    item.device_id,
                    iso(item.ts),
                    item.label,
                    item.confidence,
                    item.risk_level,
                    item.action,
                    item.summary_count,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_events(self, ids: Iterable[str]) -> int:
        values = list(dict.fromkeys(ids))
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM events WHERE event_id IN ({placeholders})", values
            )
            self._conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------ reading
    def get_event(self, event_id: str) -> EventRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT event_json, decision_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        event = Event.model_validate_json(row["event_json"])
        decision = (
            Decision.model_validate_json(row["decision_json"]) if row["decision_json"] else None
        )
        return EventRecord(event=event, decision=decision)

    def list_events(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]:
        clauses = ["ts < ?"]
        params: list[object] = [iso(end)]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(iso(start))
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if label:
            clauses.append("label = ?")
            params.append(label)
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT event_id, device_id, ts, label, confidence, decision_json,
                       risk_level, action, event_json
                  FROM events
                 WHERE {" AND ".join(clauses)}
                 ORDER BY ts DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [_item_from_event_row(row) for row in rows]

    def list_summaries(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]:
        clauses = ["last_ts >= ?", "first_ts < ?"]
        params: list[object] = [] if start is None else [iso(start)]
        params.append(iso(end))
        if start is None:
            clauses = ["first_ts < ?"]
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if label:
            clauses.append("label = ?")
            params.append(label)
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT summary_id, device_id, label, count, first_ts, last_ts,
                       avg_confidence, max_confidence, risk_counts, action_counts, text
                  FROM summaries
                 WHERE {" AND ".join(clauses)}
                 ORDER BY last_ts DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [_item_from_summary_row(row) for row in rows]

    def list_cold(
        self,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[MemoryItem]:
        clauses = ["ts < ?"]
        params: list[object] = [iso(end)]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(iso(start))
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if label:
            clauses.append("label = ?")
            params.append(label)
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT event_id, device_id, ts, label, confidence, risk_level, action, summary_count
                  FROM cold_events
                 WHERE {" AND ".join(clauses)}
                 ORDER BY ts DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [_item_from_cold_row(row) for row in rows]

    def search_text(
        self,
        term: str,
        start: datetime | None,
        end: datetime,
        *,
        device_id: str | None = None,
        label: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        if not term.strip():
            return []
        results: list[MemoryItem] = []
        results.extend(self.list_events(start, end, device_id=device_id, label=label, limit=limit))
        results.extend(
            self.list_summaries(start, end, device_id=device_id, label=label, limit=limit)
        )
        results.extend(self.list_cold(start, end, device_id=device_id, label=label, limit=limit))
        # SQL LIKE is applied here rather than in every tier query so the
        # fallback stays one small, testable code path.
        filtered = [
            item
            for item in results
            if term.lower() in item.text.lower()
            or term.lower() in item.label.lower()
            or (item.action and term.lower() in item.action.lower())
        ]
        filtered.sort(key=lambda item: item.ts, reverse=True)
        return filtered[: max(1, limit)]

    def all_hot(self, limit: int = 10000) -> list[MemoryItem]:
        return self.list_events(None, datetime.max.replace(tzinfo=timezone.utc), limit=limit)

    def all_summaries(self, limit: int = 10000) -> list[MemoryItem]:
        return self.list_summaries(None, datetime.max.replace(tzinfo=timezone.utc), limit=limit)

    def counts(self) -> dict[str, int]:
        with self._lock:
            events = int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            summaries = int(self._conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0])
            cold = int(self._conn.execute("SELECT COUNT(*) FROM cold_events").fetchone()[0])
        return {"events": events, "summaries": summaries, "cold_events": cold}


def _json_dict(values: dict[str, int]) -> str:
    import json

    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def _from_json_dict(value: str) -> dict[str, int]:
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return (
        {str(key): int(count) for key, count in parsed.items()} if isinstance(parsed, dict) else {}
    )


def _item_from_event_row(row: sqlite3.Row) -> MemoryItem:
    event = Event.model_validate_json(row["event_json"])
    decision = Decision.model_validate_json(row["decision_json"]) if row["decision_json"] else None
    return MemoryItem(
        event_id=row["event_id"],
        device_id=row["device_id"],
        ts=from_iso(row["ts"]),
        label=row["label"],
        confidence=float(row["confidence"]),
        tier=MemoryTier.HOT,
        risk_level=row["risk_level"],
        action=row["action"],
        text=event_text(event, decision),
    )


def _item_from_summary_row(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        event_id=row["summary_id"],
        device_id=row["device_id"],
        ts=from_iso(row["last_ts"]),
        label=row["label"],
        confidence=float(row["avg_confidence"]),
        tier=MemoryTier.WARM,
        summary_count=int(row["count"]),
        text=row["text"],
    )


def _item_from_cold_row(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        event_id=row["event_id"],
        device_id=row["device_id"],
        ts=from_iso(row["ts"]),
        label=row["label"],
        confidence=float(row["confidence"]),
        tier=MemoryTier.COLD,
        summary_count=int(row["summary_count"]),
        risk_level=row["risk_level"],
        action=row["action"],
        text=(
            f"{row['label']} 冷归档事件，设备 {row['device_id']}，"
            f"置信度 {float(row['confidence']):.3f}，风险 {row['risk_level'] or 'unknown'}"
        ),
    )
