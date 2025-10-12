from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.binance import process_course_event
from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import BinanceCourseEvent, decode_pubsub_message
from src.event_pipeline.settings import get_settings

LOG = logging.getLogger(__name__)
app = FastAPI(title="Binance Course Downloader")


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = decode_pubsub_message(body, model=BinanceCourseEvent)
    ready_events = process_course_event(event)
    if not ready_events:
        LOG.info("No media generated for %s", event.course_url)
        return Response(status_code=204)

    settings = get_settings()
    for ready_event in ready_events:
        publish_event(
            settings.mp3_ready_topic,
            ready_event,
            attributes={"source": "binance-downloader"},
        )
        LOG.info("Published mp3-ready for Binance clip %s", ready_event.video_id)
    return Response(status_code=204)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
