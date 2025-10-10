from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional
import json
import logging
import shutil

import requests

from src.utils.gcs import maybe_upload

from .config import BinanceSettings
from .course import CourseVideo
from .utils import sanitize_component
from .wistia import extract_video_id, fetch_media, pick_best_asset

LOG = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20  # 1 MiB


def _write_metadata(path: Path, metadata: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _download_file(url: str, dest: Path, timeout: int = 60) -> None:
    LOG.info("Downloading %s -> %s", url, dest)
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)


def download_video(video: CourseVideo, settings: BinanceSettings, session: Optional[requests.Session] = None) -> Optional[Path]:
    video_id = extract_video_id(video.video_link)
    if not video_id:
        LOG.warning("Unable to extract video id from %s", video.video_link)
        return None

    sess = session or requests.Session()
    media = fetch_media(video_id, timeout=settings.wistia_timeout, session=sess)
    best_asset = pick_best_asset(media.assets)
    if not best_asset:
        LOG.warning("No downloadable mp4 asset for %s", video.video_link)
        return None

    base_dir = settings.ensure_output_dir() / sanitize_component(video.language) / sanitize_component(video.course_slug)
    safe_name = sanitize_component(media.name or video_id) or video_id
    clip_dir = base_dir / safe_name
    clip_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = clip_dir / "metadata.json"
    metadata = {
        "course_slug": video.course_slug,
        "course_title": video.course_title,
        "language": video.language,
        "translation_id": video.translation_id,
        "video_link": video.video_link,
        "article_slug": video.article_slug,
        "wistia_id": media.id,
        "wistia_name": media.name,
        "wistia_description": media.description,
        "wistia_thumbnail": media.thumbnail_url,
        "available_assets": [asdict(asset) for asset in media.assets],
        "selected_asset": asdict(best_asset),
    }
    _write_metadata(metadata_path, metadata)

    media_path = clip_dir / f"{safe_name}.mp4"
    if not media_path.exists():
        _download_file(best_asset.url, media_path, timeout=settings.wistia_timeout)

    # Upload metadata
    rel_metadata = media_path.relative_to(settings.ensure_output_dir()).with_suffix("").parent / metadata_path.name
    metadata_uri = maybe_upload(
        metadata_path,
        bucket=settings.gcs_bucket,
        prefix=settings.gcs_prefix,
        relative_key=str(rel_metadata),
    )
    mp4_uri = maybe_upload(
        media_path,
        bucket=settings.gcs_bucket,
        prefix=settings.gcs_prefix,
        relative_key=str(media_path.relative_to(settings.ensure_output_dir())),
    )

    if settings.gcs_bucket and not settings.keep_local_files:
        try:
            shutil.rmtree(clip_dir)
            LOG.debug("Removed local directory %s", clip_dir)
        except OSError as exc:
            LOG.warning("Failed to remove %s: %s", clip_dir, exc)
        return None

    if mp4_uri:
        LOG.info("Uploaded to %s", mp4_uri)
    if metadata_uri:
        LOG.debug("Metadata uploaded to %s", metadata_uri)
    return media_path
