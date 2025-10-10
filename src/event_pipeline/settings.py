from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas import TopicName


class PipelineSettings(BaseSettings):
    """Environment-driven configuration for event pipeline services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gcp_project: str = Field(..., validation_alias="GCP_PROJECT")
    yt_new_video_topic: str = Field(default=TopicName.YT_NEW_VIDEO.value, validation_alias="YT_NEW_VIDEO_TOPIC")
    mp3_download_topic: str = Field(default=TopicName.MP3_DOWNLOAD.value, validation_alias="MP3_DOWNLOAD_TOPIC")
    mp3_ready_topic: str = Field(default=TopicName.MP3_READY.value, validation_alias="MP3_READY_TOPIC")
    diarization_ready_topic: str = Field(default=TopicName.DIARIZATION_READY.value, validation_alias="DIARIZATION_READY_TOPIC")
    youtube_api_key: Optional[str] = Field(default=None, validation_alias="YOUTUBE_API_KEY")
    youtube_api_key_secret: Optional[str] = Field(default=None, validation_alias="YOUTUBE_API_KEY_SECRET")
    assembly_ai_api_key: Optional[str] = Field(default=None, validation_alias="ASSEMBLY_AI_API_KEY")
    assembly_ai_secret: Optional[str] = Field(default=None, validation_alias="ASSEMBLY_AI_SECRET")
    media_bucket: str = Field(..., validation_alias="MEDIA_BUCKET")


@lru_cache(maxsize=1)
def get_settings() -> PipelineSettings:
    return PipelineSettings()  # type: ignore[arg-type]
