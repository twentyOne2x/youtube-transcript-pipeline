from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src import YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.download_mp3.config import DlStatus, Settings
from src.data_ingestion_youtube.load.download_mp3.downloader import download_one, make_ydl_opts
from src.event_pipeline.schemas import Mp3DownloadEvent, Mp3ReadyEvent

LOG = logging.getLogger(__name__)


def _sanitize(value: Optional[str]) -> str:
    if not value:
        return "untitled"
    value = value.strip()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"\s+", "_", value)
    return value[:120] or "untitled"


def _date_prefix(metadata: dict) -> str:
    published = metadata.get("publishedAt") or metadata.get("published_date")
    if not published:
        return "unknown-date"
    return str(published)[:10]


@dataclass
class DownloadArtifact:
    local_path: Path
    gcs_uri: Optional[str]


def _build_paths(event: Mp3DownloadEvent) -> tuple[Path, str]:
    base_dir = Path(os.environ.get("YOUTUBE_VIDEO_DIRECTORY", YOUTUBE_VIDEO_DIRECTORY)).resolve()
    channel_dir = base_dir / event.channel_id
    slug = _sanitize(event.metadata.get("title"))
    date_prefix = _date_prefix(event.metadata)
    file_stem = f"{date_prefix}_{event.video_id}_{slug}"
    channel_dir.mkdir(parents=True, exist_ok=True)
    return channel_dir, file_stem


def _relative_key(local_path: Path) -> str:
    base_dir = Path(os.environ.get("YOUTUBE_VIDEO_DIRECTORY", YOUTUBE_VIDEO_DIRECTORY)).resolve()
    return local_path.resolve().relative_to(base_dir).as_posix()


def download_mp3(event: Mp3DownloadEvent, settings: Optional[Settings] = None) -> DownloadArtifact:
    settings = settings or Settings()
    channel_dir, file_stem = _build_paths(event)
    work_dir = channel_dir / file_stem
    work_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = make_ydl_opts(str(work_dir), file_stem, None, settings)
    target_path = os.path.join(str(work_dir), f"{file_stem}.mp3")
    url = f"https://www.youtube.com/watch?v={event.video_id}"
    LOG.info("Downloading %s → %s", url, target_path)
    status = download_one(url, ydl_opts, target_path, settings)
    if status != DlStatus.OK:
        raise RuntimeError(f"Failed to download {event.video_id}: status={status}")

    local_path = Path(target_path)
    gcs_uri = None
    if settings.gcs_bucket:
        rel = _relative_key(local_path)
        prefix = "/".join(p for p in (settings.gcs_prefix, rel) if p)
        gcs_uri = f"gs://{settings.gcs_bucket}/{prefix}"
    return DownloadArtifact(local_path=local_path, gcs_uri=gcs_uri)


def build_ready_event(artifact: DownloadArtifact, event: Mp3DownloadEvent) -> Mp3ReadyEvent:
    if not artifact.gcs_uri:
        raise ValueError("MP3 downloader requires GCS upload to be configured.")
    metadata_uri = None
    if event.metadata:
        metadata_uri = event.metadata.get("metadata_uri")
    return Mp3ReadyEvent(
        gcs_uri=artifact.gcs_uri,
        metadata_uri=metadata_uri,
        video_id=event.video_id,
    )
