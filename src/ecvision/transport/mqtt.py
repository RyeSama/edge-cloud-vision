"""MQTT publisher with the three things a demo usually forgets:

1. Last Will and Testament: the broker publishes ``offline`` if the edge dies.
2. A retained ``online`` status + periodic heartbeat: liveness is observable.
3. A bounded offline buffer: a broker restart does not lose review events.

Delivery is at-least-once (QoS 1). The cloud deduplicates on ``event_id``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from ecvision.config import MqttConfig
from ecvision.contracts import Event
from ecvision.transport.base import encode_event
from ecvision.transport.topics import event_topic, status_topic

log = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, config: MqttConfig, device_id: str) -> None:
        self._config = config
        self._device_id = device_id
        self._topic = event_topic(device_id)
        self._status_topic = status_topic(device_id)
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._buffer: deque[bytes] = deque(maxlen=config.buffer_size)
        self._published = 0
        self._dropped = 0
        self._payload_bytes = 0
        self._started_at = time.monotonic()

        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv311,
            clean_session=False,
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.will_set(
            self._status_topic,
            payload=self._status_payload("offline"),
            qos=1,
            retain=True,
        )

    def _status_payload(self, state: str) -> bytes:
        payload = {
            "schema_version": "1.0",
            "device_id": self._device_id,
            "state": state,
            "ts": datetime.now(timezone.utc).isoformat(),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "events_published": self._published,
            "buffered": len(self._buffer),
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self._config.host, self._config.port)
        self._connected.set()
        client.publish(self._status_topic, self._status_payload("online"), qos=1, retain=True)
        self._flush()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()
        if not self._stopping.is_set():
            log.warning("MQTT disconnected (%s); paho will retry with backoff", reason_code)

    def start(self) -> None:
        self._client.connect_async(self._config.host, self._config.port, self._config.keepalive_s)
        self._client.loop_start()
        threading.Thread(target=self._heartbeat_loop, name="mqtt-heartbeat", daemon=True).start()

    def _heartbeat_loop(self) -> None:
        interval = max(1.0, self._config.heartbeat_s)
        while not self._stopping.wait(interval):
            if self._connected.is_set():
                self._client.publish(
                    self._status_topic, self._status_payload("online"), qos=1, retain=True
                )

    def _buffer_payload(self, payload: bytes) -> None:
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                self._dropped += 1
                log.warning("offline buffer full; dropping oldest event")
            self._buffer.append(payload)

    def _flush(self) -> None:
        while self._connected.is_set():
            with self._lock:
                if not self._buffer:
                    return
                payload = self._buffer[0]
            info = self._client.publish(self._topic, payload, qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return
            with self._lock:
                self._buffer.popleft()
                self._published += 1
                self._payload_bytes += len(payload)

    def publish(self, event: Event) -> bool:
        payload = encode_event(event)
        if not self._connected.is_set():
            self._buffer_payload(payload)
            return False

        info = self._client.publish(self._topic, payload, qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self._buffer_payload(payload)
            return False
        with self._lock:
            self._published += 1
            self._payload_bytes += len(payload)
        return True

    def stop(self) -> None:
        self._stopping.set()
        if self._connected.is_set():
            info = self._client.publish(
                self._status_topic, self._status_payload("offline"), qos=1, retain=True
            )
            try:
                info.wait_for_publish(timeout=1.0)
            except (ValueError, RuntimeError):
                pass
        self._client.disconnect()
        self._client.loop_stop()

    @property
    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "connected": self._connected.is_set(),
                "published": self._published,
                "buffered": len(self._buffer),
                "dropped": self._dropped,
                "payload_bytes": self._payload_bytes,
            }
