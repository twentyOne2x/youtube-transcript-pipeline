from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from fastapi import FastAPI, HTTPException, Request

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.settings import get_settings
from src.event_pipeline.pumpfun import discover_clip_events
from src.event_pipeline.schemas import PumpfunClipEvent

LOG = logging.getLogger(__name__)
app = FastAPI(title="Pumpfun Clip Publisher")


def _env_rooms() -> Optional[List[str]]:
    raw = os.getenv("PUMPFUN_ROOMS")
    if not raw:
        return None
    return [value.strip() for value in raw.split(",") if value.strip()]


def _env_config_path() -> Optional[str]:
    return os.getenv("PUMPFUN_ROOMS_CONFIG")


def _parse_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    payload = body or {}
    if "rooms" in payload and payload["rooms"] is not None:
        if not isinstance(payload["rooms"], (list, tuple)):
            raise HTTPException(status_code=400, detail="rooms must be an array of strings")
        payload["rooms"] = [str(item).strip() for item in payload["rooms"] if str(item).strip()]
    if "max_clips" in payload and payload["max_clips"] is not None:
        try:
            payload["max_clips"] = int(payload["max_clips"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_clips must be an integer")
    return payload


def _discover(payload: Dict[str, Any]) -> Sequence[PumpfunClipEvent]:
    config_path = payload.get("config_path") or _env_config_path()
    rooms = payload.get("rooms") or _env_rooms()
    max_clips = payload.get("max_clips")
    events = discover_clip_events(
        config_path=config_path,
        rooms=rooms,
        max_results_override=max_clips,
    )
    LOG.info("Discovered %d Pump.fun clips", len(events))
    return events


def _publish(events: Sequence[PumpfunClipEvent]) -> int:
    settings = get_settings()
    count = 0
    for event in events:
        publish_event(
            settings.pumpfun_clip_topic,
            event,
            attributes={"source": "pumpfun-publisher"},
        )
        count += 1
    return count


@app.post("/trigger")
async def trigger(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    payload = _parse_payload(body)
    events = _discover(payload)
    published = _publish(events)
    return {"published": published}


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}
