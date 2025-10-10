from __future__ import annotations

from typing import Dict, Iterable, Iterator, List, Optional
import logging

import requests

from .config import PumpfunSettings


LOG = logging.getLogger(__name__)


def _build_url(base: str, *parts: str) -> str:
    base = base.rstrip("/")
    suffix = "/".join(part.strip("/") for part in parts)
    return f"{base}/{suffix}"


def fetch_coin_metadata(
    room_name: str,
    settings: PumpfunSettings,
    session: Optional[requests.Session] = None,
) -> Dict:
    """Return metadata about the Pump.fun coin/channel."""
    url = _build_url(settings.coin_api_base, "coins", room_name)
    sess = session or requests.Session()
    resp = sess.get(url, timeout=settings.request_timeout)
    if resp.status_code == 404:
        LOG.warning("Coin metadata not found for %s", room_name)
        return {}
    resp.raise_for_status()
    return resp.json()


def fetch_clip(
    room_name: str,
    clip_id: str,
    settings: PumpfunSettings,
    session: Optional[requests.Session] = None,
) -> Dict:
    """Fetch a single clip record."""
    url = _build_url(settings.api_base, "clips", room_name, clip_id)
    sess = session or requests.Session()
    resp = sess.get(url, timeout=settings.request_timeout)
    resp.raise_for_status()
    return resp.json()


def iter_clips(
    room_name: str,
    settings: PumpfunSettings,
    *,
    max_results: Optional[int] = None,
    include_user_hidden: bool = False,
    clip_type: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Iterator[Dict]:
    """
    Yield clip metadata for a Pump.fun room.

    The endpoint paginates using ``lastEvaluatedKey``. We keep fetching until
    no additional pages remain or ``max_results`` clips have been produced.
    """
    sess = session or requests.Session()
    fetched = 0
    last_key = None

    while True:
        params: Dict[str, str] = {"limit": "50"}
        if last_key:
            params["lastEvaluatedKey"] = last_key
        if include_user_hidden:
            params["includeUserHidden"] = "true"
        if clip_type:
            params["clipType"] = clip_type
        if max_results is not None:
            remaining = max_results - fetched
            if remaining <= 0:
                break
            params["limit"] = str(min(remaining, 50))

        url = _build_url(settings.api_base, "clips", room_name)
        LOG.debug("Fetching Pump.fun clips: %s %s", url, params)
        resp = sess.get(url, params=params, timeout=settings.request_timeout)
        resp.raise_for_status()
        payload = resp.json()

        clips: List[Dict] = payload.get("clips", [])
        for clip in clips:
            yield clip
            fetched += 1
            if max_results is not None and fetched >= max_results:
                return

        if not payload.get("hasMore"):
            break
        last_key = payload.get("lastEvaluatedKey")
        if not last_key:
            break
