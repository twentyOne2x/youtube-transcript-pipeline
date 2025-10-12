from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
import httpx

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import DiarizationReadyEvent, decode_pubsub_message
from src.event_pipeline.settings import get_settings

LOG = logging.getLogger(__name__)
app = FastAPI(title="Warehouse Ingestion")


def _warehouse_dir() -> Path:
    root = os.getenv("WAREHOUSE_BUFFER_DIR", "/tmp/warehouse_ingestion")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _forward_to_ingestion(event: DiarizationReadyEvent) -> None:
    settings = get_settings()
    try:
        publish_event(settings.ingestion_topic, event, attributes={"source": "warehouse-ingestion"})
    except Exception as exc:
        LOG.warning("Failed to publish diarization-ready to ingestion topic: %s", exc)

    endpoint = os.getenv("INGESTION_ENDPOINT")
    if not endpoint:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, json=event.to_message())
            resp.raise_for_status()
        LOG.info("Forwarded diarization-ready to ingestion endpoint %s", endpoint)
    except Exception as exc:
        LOG.error("Failed to POST diarization-ready to ingestion endpoint: %s", exc)


def _persist_event(event: DiarizationReadyEvent) -> Path:
    buffer_dir = _warehouse_dir()
    outfile = buffer_dir / f"{event.mp3_uri.replace('gs://', '').replace('/', '_')}.json"
    outfile.write_text(json.dumps(event.to_message(), indent=2), encoding="utf-8")
    return outfile


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=DiarizationReadyEvent)
    outfile = _persist_event(event)
    LOG.info("Buffered diarization-ready event at %s", outfile)
    await _forward_to_ingestion(event)
    return Response(status_code=204)
