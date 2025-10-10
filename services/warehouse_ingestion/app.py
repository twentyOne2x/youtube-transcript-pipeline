from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response

from src.event_pipeline.schemas import DiarizationReadyEvent, decode_pubsub_message

LOG = logging.getLogger(__name__)
app = FastAPI(title="Warehouse Ingestion")


def _warehouse_dir() -> Path:
    root = os.getenv("WAREHOUSE_BUFFER_DIR", "/tmp/warehouse_ingestion")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=DiarizationReadyEvent)
    buffer_dir = _warehouse_dir()
    outfile = buffer_dir / f"{event.mp3_uri.replace('gs://', '').replace('/', '_')}.json"
    outfile.write_text(json.dumps(event.to_message(), indent=2), encoding="utf-8")
    LOG.info("Buffered diarization-ready event at %s", outfile)
    return Response(status_code=204)
