"""MQTT topic layout, versioned from day one."""

from __future__ import annotations

PREFIX = "ecv/v1"


def event_topic(device_id: str) -> str:
    return f"{PREFIX}/{device_id}/event"


def status_topic(device_id: str) -> str:
    return f"{PREFIX}/{device_id}/status"


def command_topic(device_id: str) -> str:
    return f"{PREFIX}/{device_id}/cmd"


def event_subscription() -> str:
    return f"{PREFIX}/+/event"


def device_from_event_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "ecv" or parts[1] != "v1" or parts[3] != "event":
        return None
    return parts[2] or None
