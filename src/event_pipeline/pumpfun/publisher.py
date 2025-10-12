from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from src.data_ingestion_pumpfun.api import fetch_coin_metadata, iter_clips
from src.data_ingestion_pumpfun.config import PumpfunSettings
from src.data_ingestion_pumpfun.room_config import RoomEntry, hydrate_room_labels, load_room_config
from src.event_pipeline.schemas import PumpfunClipEvent

LOG = logging.getLogger(__name__)


def _dedupe_entries(entries: Iterable[RoomEntry]) -> List[RoomEntry]:
    deduped: Dict[str, RoomEntry] = {}
    for entry in entries:
        existing = deduped.get(entry.room)
        if existing is None:
            deduped[entry.room] = entry
            continue
        if not existing.label and entry.label:
            existing.label = entry.label
        if existing.max_clips is None and entry.max_clips is not None:
            existing.max_clips = entry.max_clips
    return list(deduped.values())


def _resolve_entries(
    settings: PumpfunSettings,
    *,
    config_path: Optional[str],
    rooms: Optional[Sequence[str]],
    session: requests.Session,
) -> Tuple[List[RoomEntry], Dict]:
    resolved: List[RoomEntry] = []
    defaults: Dict = {}

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Pump.fun rooms config not found: {config_path}")
        cfg_entries, defaults = load_room_config(path)
        resolved.extend(cfg_entries)

    if rooms:
        resolved.extend(RoomEntry(room=value).normalized() for value in rooms if value.strip())

    if not resolved:
        raise ValueError("No Pump.fun rooms provided.")

    deduped = _dedupe_entries(resolved)
    hydrate_room_labels(deduped, settings, session=session)
    return deduped, defaults


def _room_limit(
    entry: RoomEntry,
    settings: PumpfunSettings,
    *,
    override: Optional[int],
) -> Optional[int]:
    if override is not None:
        return override if override > 0 else None
    if entry.max_clips is not None:
        return entry.max_clips if entry.max_clips > 0 else None
    if settings.max_clips_per_room is not None and settings.max_clips_per_room > 0:
        return settings.max_clips_per_room
    return None


def discover_clip_events(
    *,
    config_path: Optional[str],
    rooms: Optional[Sequence[str]],
    max_results_override: Optional[int],
    settings: Optional[PumpfunSettings] = None,
    session: Optional[requests.Session] = None,
) -> List[PumpfunClipEvent]:
    """
    Build Pump.fun clip events for downstream processing.

    Args:
        config_path: Optional JSON config describing rooms.
        rooms: Optional explicit list of room identifiers (mint addresses).
        max_results_override: Optional per-room clip limit override.
        settings: Optional PumpfunSettings; when omitted a default instance is created.
        session: Optional requests session for reuse.
    """
    sess = session or requests.Session()
    base_settings = settings or PumpfunSettings()

    # Ensure we do not mutate the caller-provided settings.
    effective_settings = replace(base_settings)

    entries, _ = _resolve_entries(
        effective_settings,
        config_path=config_path,
        rooms=rooms,
        session=sess,
    )

    events: List[PumpfunClipEvent] = []
    for entry in entries:
        before = len(events)
        limit = _room_limit(entry, effective_settings, override=max_results_override)
        try:
            coin = fetch_coin_metadata(entry.room, effective_settings, session=sess)
        except requests.HTTPError as exc:
            LOG.warning("Failed to fetch coin metadata for %s: %s", entry.room, exc)
            coin = {}

        clip_iter = iter_clips(
            entry.room,
            effective_settings,
            max_results=limit,
            session=sess,
        )
        for clip in clip_iter:
            clip_id = str(clip.get("clipId") or clip.get("id") or clip.get("startTime") or "")
            playlist_url = clip.get("playlistUrl") or clip.get("clipUrl")
            if not clip_id or not playlist_url:
                LOG.debug("Skipping clip without id or playlist: %s", clip)
                continue
            events.append(
                PumpfunClipEvent(
                    room=entry.room,
                    clip_id=clip_id,
                    playlist_url=playlist_url,
                    clip=clip,
                    coin=coin or {},
                )
            )
        after = len(events)
        LOG.info("Discovered %d Pump.fun clips for %s", after - before, entry.room)
    return events
