"""Serial NDJSON contract: valid lines, rejection, and sequence tracking."""

from __future__ import annotations

import json

import pytest

from ecvision.contracts import SensorReading
from ecvision.transport.serial_json import (
    SensorSequenceTracker,
    SerialParseError,
    iter_sensor_lines,
    parse_sensor_line,
    read_replay,
)


def _line(**overrides) -> str:
    payload = {
        "schema_version": "1.0",
        "kind": "sensor",
        "device_id": "esp32-01",
        "boot_id": "a1b2c3d4",
        "seq": 1,
        "t_us": 100_000,
        "lux": 512.3,
        "distance_mm": 803.0,
        "trigger": False,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_parse_sensor_line_accepts_compact_json():
    reading = parse_sensor_line(_line())
    assert isinstance(reading, SensorReading)
    assert reading.device_id == "esp32-01"
    assert reading.distance_mm == 803.0
    assert reading.trigger is False


def test_parse_sensor_line_ignores_unknown_fields():
    with pytest.raises(SerialParseError):
        parse_sensor_line(_line(image_b64="AAAA"))


def test_parse_sensor_line_rejects_bad_json_and_out_of_range():
    with pytest.raises(SerialParseError):
        parse_sensor_line('{"device_id":')
    with pytest.raises(SerialParseError):
        parse_sensor_line(_line(distance_mm=-1))


def test_iter_lines_skips_blank_and_comment_lines():
    lines = ["", "  ", "# boot log", _line(seq=1), _line(seq=2)]
    assert [reading.seq for reading in iter_sensor_lines(lines)] == [1, 2]


def test_iter_lines_reports_errors_without_stopping():
    errors: list[tuple[str, str]] = []

    def on_error(exc, raw):
        errors.append((type(exc).__name__, raw.strip()))

    lines = [_line(seq=1), "not json", _line(seq=2)]
    assert [reading.seq for reading in iter_sensor_lines(lines, on_error=on_error)] == [1, 2]
    assert errors == [("SerialParseError", "not json")]


def test_sequence_tracker_detects_gap_duplicate_and_reboot():
    tracker = SensorSequenceTracker()
    assert tracker.observe(parse_sensor_line(_line(seq=1))) == 0
    assert tracker.observe(parse_sensor_line(_line(seq=2))) == 0
    assert tracker.observe(parse_sensor_line(_line(seq=5))) == 2
    assert tracker.observe(parse_sensor_line(_line(seq=5))) == -1
    assert tracker.observe(parse_sensor_line(_line(seq=6, boot_id="ffff0000"))) == 0
    assert tracker.gaps == 2
    assert tracker.duplicates == 1
    assert tracker.reboots == 1


def test_read_replay_tolerates_utf8_bom(tmp_path):
    path = tmp_path / "sensor.ndjson"
    path.write_bytes(b"\xef\xbb\xbf" + _line(seq=42).encode("utf-8") + b"\n")
    readings = list(read_replay(path))
    assert [reading.seq for reading in readings] == [42]
