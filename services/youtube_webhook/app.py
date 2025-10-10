from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Iterable, List

from fastapi import FastAPI, HTTPException, Query, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import TopicName, YouTubeNewVideoEvent
from src.event_pipeline.settings import get_settings

LOG = logging.getLogger(__name__)

app = FastAPI(title="YouTube PubSubHubbub Webhook")


def _extract_events(xml_body: str) -> Iterable[YouTubeNewVideoEvent]:
    if not xml_body.strip():
        return []
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="Invalid XML payload") from exc

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    events: List[YouTubeNewVideoEvent] = []
    for entry in root.findall("atom:entry", ns):
        video_id_elem = entry.find("yt:videoId", ns)
        channel_id_elem = entry.find("yt:channelId", ns)
        if video_id_elem is None or channel_id_elem is None:
            continue
        events.append(
            YouTubeNewVideoEvent(
                video_id=video_id_elem.text or "",
                channel_id=channel_id_elem.text or "",
            )
        )
    return events


@app.get("/hub")
async def verify_subscription(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="Unsupported hub.mode")
    LOG.info("Responding to hub.challenge for mode=%s", hub_mode)
    return Response(content=hub_challenge, media_type="text/plain")


@app.post("/hub")
async def handle_notification(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    events = _extract_events(body.decode("utf-8"))
    if not events:
        LOG.info("No events found in webhook payload.")
        return Response(status_code=204)
    for event in events:
        LOG.info("Publishing new video event %s", event.model_dump())
        publish_event(settings.yt_new_video_topic, event, attributes={"source": "youtube-webhook"})
    return Response(status_code=202)
