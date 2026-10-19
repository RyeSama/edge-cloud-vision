"""MQTT -> application boundary for the cloud.

Paho callbacks run on a network thread. The callback only validates and hands the
event to the asyncio loop; it never performs I/O or LLM calls. That single rule
prevents the classic "blocked MQTT thread stalls all heartbeats" bug.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from ecvision.config import MqttConfig
from ecvision.contracts import Event
from ecvision.transport.topics import device_from_event_topic, event_subscription

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024


class MqttEventSubscriber:
    def __init__(self, config: MqttConfig, on_event: Callable[[Event], None]) -> None:
        self._config = config
        self._on_event = on_event
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv311,
            # Persistent session: while the cloud is briefly down, the broker
            # queues QoS1 events for this stable client id. Duplicates are
            # suppressed by event_id, so at-least-once is safe here.
            clean_session=False,
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self.received = 0
        self.invalid = 0
        self.last_error: str | None = None
        self.connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.last_error = f"connect refused: {reason_code}"
            log.error("MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        client.subscribe(event_subscription(), qos=1)
        log.info(
            "MQTT connected to %s:%s; subscribed to %s",
            self._config.host,
            self._config.port,
            event_subscription(),
        )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        log.warning("MQTT disconnected (%s); paho will retry", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        if len(message.payload) > MAX_MESSAGE_BYTES:
            self.invalid += 1
            self.last_error = f"message too large: {len(message.payload)} bytes"
            log.warning(self.last_error)
            return
        try:
            event = Event.model_validate(json.loads(message.payload))
        except Exception as exc:
            self.invalid += 1
            self.last_error = f"invalid event: {type(exc).__name__}: {exc}"[:500]
            log.warning("dropping malformed event on %s: %s", message.topic, exc)
            return

        topic_device = device_from_event_topic(message.topic)
        if topic_device != event.device_id:
            self.invalid += 1
            self.last_error = f"device/topic mismatch: {topic_device!r} != {event.device_id!r}"
            log.warning(self.last_error)
            return

        self.received += 1
        try:
            self._on_event(event)
        except Exception:
            log.exception("application callback failed for %s", event.event_id)

    def start(self) -> None:
        self._client.connect_async(self._config.host, self._config.port, self._config.keepalive_s)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    @property
    def stats(self) -> dict[str, int | bool | str | None]:
        return {
            "connected": self.connected,
            "received": self.received,
            "invalid": self.invalid,
            "last_error": self.last_error,
        }
