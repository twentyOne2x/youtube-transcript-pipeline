from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.indexer import IndexerSettings, process_event
from src.event_pipeline.indexer.settings import get_indexer_settings
from src.event_pipeline.schemas import DiarizationReadyEvent, decode_pubsub_message

LOG = logging.getLogger(__name__)
app = FastAPI(title="Diarization Indexer")


def _settings() -> IndexerSettings:
    return get_indexer_settings()


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    try:
        event = decode_pubsub_message(body, model=DiarizationReadyEvent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = process_event(event, _settings())
    except Exception as exc:
        LOG.exception("Failed to index diarization payload for %s: %s", event.video_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    LOG.info(
        "Indexed diarization payload for %s (%d chunks, %d vectors).",
        event.video_id,
        result.chunk_count,
        result.vector_count,
    )
    return Response(status_code=204)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
