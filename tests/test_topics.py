from __future__ import annotations

from ecvision.transport.topics import (
    device_from_event_topic,
    event_subscription,
    event_topic,
    status_topic,
)


def test_topic_round_trip():
    topic = event_topic("edge-cam-01")
    assert topic == "ecv/v1/edge-cam-01/event"
    assert device_from_event_topic(topic) == "edge-cam-01"
    assert event_subscription() == "ecv/v1/+/event"
    assert status_topic("edge-cam-01") == "ecv/v1/edge-cam-01/status"


def test_topic_rejects_wrong_shapes():
    assert device_from_event_topic("ecv/v1/edge-cam-01/status") is None
    assert device_from_event_topic("ecv/v2/edge-cam-01/event") is None
    assert device_from_event_topic("other/v1/x/event") is None
