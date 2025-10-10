from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import (
    Mp3DownloadEvent,
    decode_pubsub_message,
)
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.downloader import build_ready_event, download_mp3

LOG = logging.getLogger(__name__)
app = FastAPI(title="YouTube MP3 Downloader")

DEFAULT_LOCAL_ROOT = "/tmp/youtube_audio"


def _ensure_env():
    settings = get_settings()
    os.environ.setdefault("YOUTUBE_VIDEO_DIRECTORY", DEFAULT_LOCAL_ROOT)
    os.environ.setdefault("YOUTUBE_GCS_BUCKET", settings.media_bucket)
    os.environ.setdefault("YOUTUBE_GCS_PREFIX", "youtube_audio")
    return settings


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=Mp3DownloadEvent)
    settings = _ensure_env()
    artifact = download_mp3(event)
    ready_event = build_ready_event(artifact, event)
    publish_event(
        settings.mp3_ready_topic,
        ready_event,
        attributes={"source": "mp3-downloader"},
    )
    LOG.info("Published mp3-ready event for %s", event.video_id)
    return Response(status_code=204)
