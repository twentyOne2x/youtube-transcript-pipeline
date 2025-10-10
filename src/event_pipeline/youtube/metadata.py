from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.event_pipeline.schemas import Mp3DownloadEvent, YouTubeNewVideoEvent

LOG = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    raw: Dict[str, Any]

    def to_download_payload(self, event: YouTubeNewVideoEvent) -> Mp3DownloadEvent:
        snippet = self.raw.get("snippet", {})
        return Mp3DownloadEvent(
            video_id=event.video_id,
            channel_id=event.channel_id,
            metadata={
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "publishedAt": snippet.get("publishedAt"),
            },
        )

    def to_json(self) -> str:
        return json.dumps(self.raw, indent=2)


class YouTubeMetadataClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    @lru_cache(maxsize=1)
    def _youtube(self):
        return build("youtube", "v3", developerKey=self.api_key)

    def fetch_video(self, video_id: str) -> VideoMetadata:
        LOG.info("Fetching metadata for video %s", video_id)
        try:
            response = (
                self._youtube()
                .videos()
                .list(part="snippet,contentDetails,status", id=video_id)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"YouTube API error: {exc}") from exc
        items = response.get("items")
        if not items:
            raise ValueError(f"No metadata for video {video_id}")
        return VideoMetadata(raw=items[0])


def persist_metadata(metadata: VideoMetadata, directory: Path, video_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{video_id}.video_metadata.json"
    path.write_text(metadata.to_json(), encoding="utf-8")
    return path


def build_download_event(event: YouTubeNewVideoEvent, metadata: VideoMetadata) -> Mp3DownloadEvent:
    return metadata.to_download_payload(event)
