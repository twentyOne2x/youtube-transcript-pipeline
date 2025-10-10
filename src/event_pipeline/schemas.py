from __future__ import annotations

import base64
import json
from enum import Enum
from typing import Any, Dict, Optional, Type, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

T = TypeVar("T", bound="BaseEvent")


class TopicName(str, Enum):
    YT_NEW_VIDEO = "yt-new-video"
    MP3_DOWNLOAD = "mp3-download"
    MP3_READY = "mp3-ready"
    DIARIZATION_READY = "diarization-ready"
    BINANCE_COURSE = "binance-course"
    PUMPFUN_CLIP = "pumpfun-clip"


class BaseEvent(BaseModel):
    """Base schema for Pub/Sub events produced by the media pipeline."""

    event_version: str = Field(default="v1", frozen=True)

    def to_message(self) -> Dict[str, Any]:
        """Serialize to a Pub/Sub compatible message body."""
        payload = self.model_dump()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_message())

    def to_base64_json(self) -> str:
        return base64.b64encode(self.to_json().encode("utf-8")).decode("utf-8")

    @classmethod
    def from_payload(cls: Type[T], payload: Dict[str, Any]) -> T:
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Invalid payload for {cls.__name__}: {exc}") from exc

    @classmethod
    def from_base64(cls: Type[T], data: str) -> T:
        decoded = base64.b64decode(data).decode("utf-8")
        return cls.from_payload(json.loads(decoded))


class YouTubeNewVideoEvent(BaseEvent):
    video_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)


class Mp3DownloadEvent(BaseEvent):
    video_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Mp3ReadyEvent(BaseEvent):
    gcs_uri: str = Field(min_length=1)
    metadata_uri: Optional[str] = None
    video_id: str = Field(min_length=1)

    @field_validator("gcs_uri", "metadata_uri")
    @classmethod
    def _validate_gcs_uri(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.startswith("gs://"):
            raise ValueError("GCS URIs must start with 'gs://'")
        return value


class DiarizationReadyEvent(BaseEvent):
    mp3_uri: str = Field(min_length=1)
    diarized_uri: str = Field(min_length=1)
    entities_uri: Optional[str] = None

    @field_validator("mp3_uri", "diarized_uri", "entities_uri")
    @classmethod
    def _validate_uri(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.startswith("gs://"):
            raise ValueError("GCS URIs must start with 'gs://'")
        return value


def decode_pubsub_message(
    body: Dict[str, Any],
    *,
    model: Type[T],
) -> T:
    """
    Decode a Pub/Sub push payload into an event model.

    Args:
        body: Flask-style JSON payload that wraps `message.data`.
        model: Target event model.
    """
    message = body.get("message")
    if not message:
        raise ValueError("Missing 'message' in Pub/Sub request.")
    data = message.get("data")
    if not data:
        raise ValueError("Missing 'data' in Pub/Sub message.")
    return model.from_base64(data)
