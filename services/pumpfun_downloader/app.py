from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pumpfun import process_clip_event
from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import PumpfunClipEvent, decode_pubsub_message
from src.event_pipeline.settings import get_settings

LOG = logging.getLogger(__name__)
app = FastAPI(title="Pumpfun Clip Downloader")


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    event = decode_pubsub_message(body, model=PumpfunClipEvent)
    ready_event = process_clip_event(event)
    if ready_event is None:
        LOG.info("Pump.fun clip %s already processed; acking message.", event.clip_id)
        return Response(status_code=204)

    settings = get_settings()
    publish_event(
        settings.mp3_ready_topic,
        ready_event,
        attributes={"source": "pumpfun-downloader"},
    )
    LOG.info("Published mp3-ready for Pump.fun clip %s", event.clip_id)
    return Response(status_code=204)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
