from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from src.data_ingestion_pumpfun.config import PumpfunSettings
from src.data_ingestion_pumpfun.downloader import ClipDownloadResult, download_clip, sanitize_filename
from src.event_pipeline.schemas import Mp3ReadyEvent, PumpfunClipEvent

LOG = logging.getLogger(__name__)


def _ensure_settings() -> PumpfunSettings:
    settings = PumpfunSettings()
    if not settings.gcs_bucket:
        raise ValueError("PUMPFUN_GCS_BUCKET must be configured for Cloud Run downloads.")
    # Always keep local files disabled for stateless Cloud Run executions.
    if settings.keep_local_files:
        settings = replace(settings, keep_local_files=False)
    if not settings.skip_existing:
        settings = replace(settings, skip_existing=True)
    if settings.download_mp4:
        settings = replace(settings, download_mp4=False)
    if not settings.download_mp3:
        settings = replace(settings, download_mp3=True)
    return settings


def _result_to_event(result: ClipDownloadResult, event: PumpfunClipEvent) -> Mp3ReadyEvent:
    if not result.gcs_mp3_uri:
        raise RuntimeError(f"Pump.fun downloader missing MP3 URI for clip {event.clip_id}")
    video_id = sanitize_filename(f"pumpfun_{event.room}_{event.clip_id}")[:120]
    metadata_uri: Optional[str] = result.gcs_metadata_uri
    return Mp3ReadyEvent(
        gcs_uri=result.gcs_mp3_uri,
        metadata_uri=metadata_uri,
        video_id=video_id,
    )


def process_clip_event(event: PumpfunClipEvent) -> Optional[Mp3ReadyEvent]:
    settings = _ensure_settings()
    result = download_clip(
        clip=event.clip,
        room_name=event.room,
        playlist_url=event.playlist_url,
        coin=event.coin,
        settings=settings,
    )
    if result.skipped:
        LOG.info("Pump.fun clip %s/%s already processed; skipping.", event.room, event.clip_id)
        return None
    ready_event = _result_to_event(result, event)
    LOG.info("Pump.fun clip processed: %s -> %s", event.clip_id, ready_event.gcs_uri)
    return ready_event
