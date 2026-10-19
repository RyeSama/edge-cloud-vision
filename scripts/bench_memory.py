#!/usr/bin/env python3
"""Reproducible Edge-Mem micro-benchmark.

This is not a scientific benchmark. It exists so the README can quote measured
numbers for a fixed workload instead of adjectives:

    python scripts/bench_memory.py --events 1000

It writes ``docs/measurements/edge-mem.json`` with compression counts and query
latency percentiles. The SQLite file is temporary and removed on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.contracts import (  # noqa: E402
    Event,
    GateDecision,
    GateInfo,
    GateReason,
    InferenceInfo,
    SourceInfo,
    TopK,
)
from ecvision.memory.models import MemoryQuery  # noqa: E402
from ecvision.memory.service import MemoryService  # noqa: E402

LABELS = ("person", "person", "forklift", "helmet", "unknown")
DEVICES = ("edge-cam-01", "edge-cam-02")


def build_settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        llm_enabled=False,
        memory_db_path=str(db_path),
        memory_timezone="Asia/Shanghai",
        memory_hot_minutes=10,
        memory_warm_hours=2,
        memory_bucket_minutes=15,
        memory_vector_backend="memory",
        memory_chroma_path=str(db_path.parent / "chroma-bench"),
        memory_vector_dim=256,
        memory_auto_compress=False,
    )


def build_event(index: int, now: datetime) -> Event:
    # Spread 1000 events over the last 3 hours, with a deterministic pattern.
    ts = now - timedelta(seconds=(index * 10.8) % (3 * 3600))
    confidence = 0.55 + ((index * 37) % 43) / 100.0
    decision = GateDecision.ACCEPT if confidence >= 0.90 else GateDecision.REVIEW
    reason = GateReason.HIGH_CONFIDENCE if decision is GateDecision.ACCEPT else GateReason.AMBIGUOUS
    label = LABELS[index % len(LABELS)]
    return Event(
        event_id=str(uuid4()),
        device_id=DEVICES[index % len(DEVICES)],
        seq=index,
        ts=ts,
        source=SourceInfo(width=320, height=240, fps=15.0),
        inference=InferenceInfo(
            model="mobilenet_v1_1.0_224_quant",
            label=label,
            label_id=index % 1000,
            confidence=round(confidence, 4),
            latency_ms=round(10.0 + (index % 17) * 0.4, 2),
            top_k=[
                TopK(label=label, confidence=round(confidence, 4)),
                TopK(label="background", confidence=round(1.0 - confidence, 4)),
            ],
        ),
        gate=GateInfo(
            decision=decision,
            reason=reason,
            accept_threshold=0.90,
            review_threshold=0.50,
        ),
    )


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--out", type=Path, default=ROOT / "docs/measurements/edge-mem.json")
    args = parser.parse_args()

    db_path = ROOT / "data" / "bench-edge-mem.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = MemoryService.from_settings(build_settings(db_path))
    try:
        started = time.perf_counter()
        for index in range(args.events):
            service.ingest(build_event(index, now))
        ingest_ms = (time.perf_counter() - started) * 1000

        before = service.stats()
        compress_started = time.perf_counter()
        report = service.compress(now)
        compress_ms = (time.perf_counter() - compress_started) * 1000
        after = service.stats()

        query = MemoryQuery(
            question="过去 1 小时有哪些 person 事件？",
            limit=10,
            token_budget=2000,
            format="markdown",
            now=now,
        )
        latencies: list[float] = []
        last = None
        for _ in range(args.runs):
            query_started = time.perf_counter()
            last = service.query(query)
            latencies.append((time.perf_counter() - query_started) * 1000)

        result = {
            "measured_at": now.isoformat(),
            "events": args.events,
            "runs": args.runs,
            "ingest_ms": round(ingest_ms, 2),
            "compress_ms": round(compress_ms, 2),
            "stats_before": before.model_dump(mode="json"),
            "stats_after": after.model_dump(mode="json"),
            "compression": report.model_dump(mode="json"),
            "query": {
                "p50_ms": percentile(latencies, 0.50),
                "p95_ms": percentile(latencies, 0.95),
                "mean_ms": round(statistics.fmean(latencies), 3),
                "max_ms": round(max(latencies), 3),
                "total_candidates": last.total_candidates if last else 0,
                "returned_items": len(last.items) if last else 0,
                "context_tokens": last.context.token_count if last else 0,
                "context_truncated": last.context.truncated if last else False,
                "degraded": last.degraded if last else False,
                "warnings": last.warnings if last else [],
            },
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(json.dumps(result["query"], ensure_ascii=False))
        print(f"wrote {args.out}")
        return 0
    finally:
        await service.aclose()
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
