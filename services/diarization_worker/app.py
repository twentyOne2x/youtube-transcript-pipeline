from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import Mp3ReadyEvent, decode_pubsub_message
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.diarization import build_ready_event, run_diarization

LOG = logging.getLogger(__name__)
app = FastAPI(title="Diarization Worker")


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=Mp3ReadyEvent)
    settings = get_settings()
    if not settings.assembly_ai_api_key:
        raise HTTPException(status_code=500, detail="Missing ASSEMBLY_AI_API_KEY")

    result = run_diarization(event, api_key=settings.assembly_ai_api_key, bucket=settings.media_bucket)
    ready_event = build_ready_event(event, result)
    publish_event(
        settings.diarization_ready_topic,
        ready_event,
        attributes={"source": "diarization-worker"},
    )
    LOG.info("Published diarization-ready event for %s", event.video_id)
    return Response(status_code=204)
