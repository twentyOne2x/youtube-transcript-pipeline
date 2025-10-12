from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Mapping, Optional

try:
    from google.cloud import pubsub_v1  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    pubsub_v1 = None  # type: ignore

from .schemas import BaseEvent
from .settings import get_settings

LOG = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _publisher_client() -> pubsub_v1.PublisherClient:
    if pubsub_v1 is None:  # pragma: no cover - requires google-cloud-pubsub
        raise ImportError("google-cloud-pubsub is required for publish operations")
    return pubsub_v1.PublisherClient()


def topic_path(topic_name: str) -> str:
    settings = get_settings()
    return _publisher_client().topic_path(settings.gcp_project, topic_name)


def publish_event(
    topic_name: str,
    event: BaseEvent,
    *,
    attributes: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Publish an event to a Pub/Sub topic.

    Args:
        topic_name: Short topic name (auto-expanded using project).
        event: Payload to serialize.
        attributes: Optional Pub/Sub attributes.

    Returns:
        Server-generated message ID.
    """
    publisher = _publisher_client()
    full_topic = topic_path(topic_name)
    data = event.to_json().encode("utf-8")
    attrs = {**(attributes or {}), "event_version": event.event_version}
    LOG.debug("Publishing to %s with attributes %s", full_topic, attrs)
    future = publisher.publish(full_topic, data=data, **attrs)
    return future.result()
