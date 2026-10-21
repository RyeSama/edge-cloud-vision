#!/usr/bin/env python3
"""Read the board's NDJSON over UART, or replay a captured file.

Examples:

    # No hardware: replay the sample
    python scripts/serial_monitor.py --replay docs/examples/serial-lines.jsonl

    # ESP32 on COM5
    python scripts/serial_monitor.py --port COM5 --baud 115200

    # Print raw JSON instead of a table
    python scripts/serial_monitor.py --replay docs/examples/serial-lines.jsonl --json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ecvision.transport.serial_json import (
    SensorSequenceTracker,
    SerialParseError,
    iter_sensor_lines,
    iter_serial,
)


def _fmt(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read sensor-board NDJSON over serial or replay.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyUSB0")
    source.add_argument("--replay", type=Path, help="captured NDJSON file")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--limit", type=int, default=0, help="stop after N readings (0 = no limit)")
    parser.add_argument("--json", action="store_true", help="print compact JSON, one per line")
    args = parser.parse_args(argv)

    errors = 0

    def on_error(exc: SerialParseError, raw: str) -> None:
        nonlocal errors
        errors += 1
        print(f"! parse error: {exc}; raw={raw.strip()[:120]!r}", file=sys.stderr)

    if args.replay:
        if not args.replay.exists():
            parser.error(f"replay file not found: {args.replay}")
        lines = args.replay.read_text(encoding="utf-8").splitlines()
        readings = iter_sensor_lines(lines, on_error=on_error)
        source_label = str(args.replay)
    else:
        try:
            readings = iter_sensor_lines(iter_serial(args.port, baud=args.baud), on_error=on_error)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        source_label = f"{args.port} @ {args.baud}"

    tracker = SensorSequenceTracker()
    count = 0
    if not args.json:
        print(f"source: {source_label}")
        print("  t_s    seq   lux     distance_mm  trigger  note")
        print("  -----  ----  ------  -----------  -------  ----")

    try:
        for reading in readings:
            missing = tracker.observe(reading)
            count += 1
            if args.json:
                print(reading.model_dump_json())
            else:
                note = ""
                if missing > 0:
                    note = f"MISSING {missing}"
                elif missing < 0:
                    note = "duplicate/out-of-order"
                print(
                    f"  {reading.t_us / 1e6:7.3f}  {reading.seq:4d}  "
                    f"{_fmt(reading.lux):>6}  {_fmt(reading.distance_mm):>11}  "
                    f"{'YES' if reading.trigger else '-':>7}  {note}"
                )
            if args.limit and count >= args.limit:
                break
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.json:
        print(
            f"\nreadings={count} errors={errors} gaps={tracker.gaps} "
            f"duplicates={tracker.duplicates} reboots={tracker.reboots}"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
