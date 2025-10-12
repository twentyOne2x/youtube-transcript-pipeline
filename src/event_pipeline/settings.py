from __future__ import annotations

import json
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
    binance_course_topic: str = Field(default=TopicName.BINANCE_COURSE.value, validation_alias="BINANCE_COURSE_TOPIC")
    pumpfun_clip_topic: str = Field(default=TopicName.PUMPFUN_CLIP.value, validation_alias="PUMPFUN_CLIP_TOPIC")
    youtube_cookie_topic: str = Field(default=TopicName.YOUTUBE_COOKIE.value, validation_alias="YOUTUBE_COOKIE_TOPIC")
    youtube_api_key: Optional[str] = Field(default=None, validation_alias="YOUTUBE_API_KEY")
    youtube_api_key_secret: Optional[str] = Field(default=None, validation_alias="YOUTUBE_API_KEY_SECRET")
    assembly_ai_api_key: Optional[str] = Field(default=None, validation_alias="ASSEMBLY_AI_API_KEY")
    assembly_ai_secret: Optional[str] = Field(default=None, validation_alias="ASSEMBLY_AI_SECRET")
    assembly_ai_api_keys_raw: Optional[str] = Field(default=None, validation_alias="ASSEMBLY_AI_API_KEYS")
    media_bucket: str = Field(..., validation_alias="MEDIA_BUCKET")
    ingestion_topic: str = Field(default="ingestion-diarization-ready", validation_alias="INGESTION_TOPIC")

    @property
    def assembly_ai_keys(self) -> list[str]:
        keys: list[str] = []
        raw = self.assembly_ai_api_keys_raw
        if raw:
            parsed = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                keys.extend(str(item).strip() for item in parsed if str(item).strip())
            else:
                parts = [part.strip() for part in raw.split(",")]
                keys.extend(part for part in parts if part)
        if self.assembly_ai_api_key:
            keys.append(self.assembly_ai_api_key.strip())
        seen: set[str] = set()
        deduped: list[str] = []
        for key in keys:
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped


@lru_cache(maxsize=1)
def get_settings() -> PipelineSettings:
    return PipelineSettings()  # type: ignore[arg-type]
