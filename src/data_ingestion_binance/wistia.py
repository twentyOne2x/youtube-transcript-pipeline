from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
import logging
import re

import requests

LOG = logging.getLogger(__name__)

WISTIA_MEDIA_URL = "https://fast.wistia.com/embed/medias/{video_id}.json"

VIDEO_ID_PATTERN = re.compile(r"/embed/(?:iframe|medias)/([a-zA-Z0-9]+)")


@dataclass
class WistiaAsset:
    url: str
    ext: str
    width: Optional[int]
    height: Optional[int]
    display_name: Optional[str]
    bitrate: Optional[float]


@dataclass
class WistiaMedia:
    id: str
    name: str
    description: Optional[str]
    assets: List[WistiaAsset]
    thumbnail_url: Optional[str]


def extract_video_id(link: str) -> Optional[str]:
    match = VIDEO_ID_PATTERN.search(link)
    if match:
        return match.group(1)
    # Accept bare ID
    if link.isalnum():
        return link
    return None


def fetch_media(video_id: str, timeout: int = 30, session: Optional[requests.Session] = None) -> WistiaMedia:
    sess = session or requests.Session()
    url = WISTIA_MEDIA_URL.format(video_id=video_id)
    LOG.debug("Fetching Wistia metadata %s", url)
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    media = payload.get("media", {})
    assets = []
    for asset in media.get("assets", []):
        assets.append(
            WistiaAsset(
                url=asset.get("url"),
                ext=asset.get("ext"),
                width=asset.get("width"),
                height=asset.get("height"),
                display_name=asset.get("display_name"),
                bitrate=asset.get("bitrate"),
            )
        )
    return WistiaMedia(
        id=media.get("hashed_id") or video_id,
        name=media.get("name") or video_id,
        description=media.get("description"),
        assets=assets,
        thumbnail_url=media.get("thumbnail", {}).get("url") if isinstance(media.get("thumbnail"), dict) else None,
    )


def pick_best_asset(assets: List[WistiaAsset]) -> Optional[WistiaAsset]:
    mp4_assets = [asset for asset in assets if asset.ext == "mp4" and asset.url]
    if not mp4_assets:
        return None
    mp4_assets.sort(key=lambda asset: (asset.height or 0, asset.bitrate or 0))
    return mp4_assets[-1]
