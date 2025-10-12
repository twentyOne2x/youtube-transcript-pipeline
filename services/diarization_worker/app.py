from __future__ import annotations

import logging

from itertools import cycle
from threading import Lock

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import Mp3ReadyEvent, decode_pubsub_message
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.diarization import build_ready_event, run_diarization

LOG = logging.getLogger(__name__)
app = FastAPI(title="Diarization Worker")

_KEY_LOCK = Lock()
_KEY_CYCLE = None
_KEY_CYCLE_KEYS: tuple[str, ...] = ()


def _select_api_key(settings) -> str:
    keys = settings.assembly_ai_keys
    if keys:
        if len(keys) == 1:
            return keys[0]
        global _KEY_CYCLE, _KEY_CYCLE_KEYS
        with _KEY_LOCK:
            key_tuple = tuple(keys)
            if _KEY_CYCLE is None or key_tuple != _KEY_CYCLE_KEYS:
                _KEY_CYCLE = cycle(keys)
                _KEY_CYCLE_KEYS = key_tuple
            try:
                return next(_KEY_CYCLE)
            except StopIteration:  # pragma: no cover - cycle should not exhaust
                _KEY_CYCLE = cycle(keys)
                _KEY_CYCLE_KEYS = key_tuple
                return next(_KEY_CYCLE)
    single_key = (settings.assembly_ai_api_key or "").strip()
    if single_key:
        return single_key
    raise HTTPException(status_code=500, detail="Missing ASSEMBLY_AI_API_KEY(S)")


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=Mp3ReadyEvent)
    settings = get_settings()

    api_key = _select_api_key(settings)
    result = run_diarization(event, api_key=api_key, bucket=settings.media_bucket)
    ready_event = build_ready_event(event, result)
    publish_event(
        settings.diarization_ready_topic,
        ready_event,
        attributes={"source": "diarization-worker"},
    )
    LOG.info("Published diarization-ready event for %s", event.video_id)
    return Response(status_code=204)
