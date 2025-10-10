from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import (
    TopicName,
    YouTubeNewVideoEvent,
    decode_pubsub_message,
)
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.metadata import (
    YouTubeMetadataClient,
    build_download_event,
    persist_metadata,
)

LOG = logging.getLogger(__name__)
app = FastAPI(title="YouTube Metadata Enricher")


def _metadata_cache_dir() -> Path:
    return Path(os.getenv("METADATA_CACHE_DIR", "/tmp/youtube_metadata"))


def _client() -> YouTubeMetadataClient:
    settings = get_settings()
    api_key = settings.youtube_api_key
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing YOUTUBE_API_KEY")
    return YouTubeMetadataClient(api_key=api_key)


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=YouTubeNewVideoEvent)
    metadata = _client().fetch_video(event.video_id)
    cache_dir = _metadata_cache_dir()
    saved_path = persist_metadata(metadata, cache_dir, event.video_id)
    LOG.info("Saved metadata to %s", saved_path)

    download_event = build_download_event(event, metadata)
    publish_event(
        get_settings().mp3_download_topic,
        download_event,
        attributes={"source": "metadata-enricher", "metadata_path": str(saved_path)},
    )
    return Response(status_code=204)
