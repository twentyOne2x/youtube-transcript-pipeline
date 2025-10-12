from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fastapi import FastAPI, HTTPException, Request

from src.event_pipeline.binance import discover_courses
from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import BinanceCourseEvent
from src.event_pipeline.settings import get_settings

LOG = logging.getLogger(__name__)
app = FastAPI(title="Binance Course Publisher")


def _env_languages() -> Optional[List[str]]:
    raw = os.getenv("BINANCE_LANGUAGE_CODES")
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_course_overrides() -> Optional[List[str]]:
    raw = os.getenv("BINANCE_COURSES")
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    payload = body or {}
    for key in ("languages", "courses"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], (list, tuple)):
            raise HTTPException(status_code=400, detail=f"{key} must be an array of strings")
        if isinstance(payload.get(key), (list, tuple)):
            payload[key] = [str(item).strip() for item in payload[key] if str(item).strip()]
    if payload.get("limit_per_language") is not None:
        try:
            payload["limit_per_language"] = int(payload["limit_per_language"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="limit_per_language must be an integer")
    return payload


def _discover_courses(payload: Dict[str, Any]) -> Sequence[BinanceCourseEvent]:
    languages = payload.get("languages") or _env_languages()
    include_courses = payload.get("courses") or _env_course_overrides()
    limit_per_language = payload.get("limit_per_language")

    events = discover_courses(
        languages=languages,
        include_courses=include_courses,
        limit_per_language=limit_per_language,
    )
    LOG.info("Discovered %d Binance courses", len(events))
    return events


def _publish(events: Iterable[BinanceCourseEvent]) -> int:
    settings = get_settings()
    count = 0
    for event in events:
        publish_event(
            settings.binance_course_topic,
            event,
            attributes={"source": "binance-crawler"},
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
    events = _discover_courses(payload)
    published = _publish(events)
    return {"published": published}


@app.get("/healthz")
async def health() -> Dict[str, str]:
    return {"status": "ok"}
