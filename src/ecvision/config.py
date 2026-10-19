"""Small, dependency-free configuration objects.

Environment variables are read once at process start. Edge-only knobs live in
the CLI; deployment-wide knobs (broker/LLM) live here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    keepalive_s: int = 30
    heartbeat_s: float = 10.0
    client_id: str = "ecv-edge"
    username: str | None = None
    password: str | None = None
    buffer_size: int = 500

    @classmethod
    def from_env(cls, client_id: str = "ecv-edge") -> MqttConfig:
        return cls(
            host=os.getenv("ECV_MQTT_HOST", "127.0.0.1"),
            port=int(os.getenv("ECV_MQTT_PORT", "1883")),
            heartbeat_s=float(os.getenv("ECV_MQTT_HEARTBEAT_S", "10")),
            client_id=client_id,
            username=os.getenv("ECV_MQTT_USERNAME") or None,
            password=os.getenv("ECV_MQTT_PASSWORD") or None,
        )


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)
