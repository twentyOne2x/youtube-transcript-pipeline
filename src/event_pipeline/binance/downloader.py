from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Optional

import requests

from src.data_ingestion_binance.config import BinanceSettings
from src.data_ingestion_binance.course import CourseVideo, parse_course
from src.data_ingestion_binance.downloader import download_video
from src.event_pipeline.schemas import BinanceCourseEvent, Mp3ReadyEvent
from src.utils.gcs import maybe_upload

LOG = logging.getLogger(__name__)


def _ffmpeg_bin() -> str:
    return "ffmpeg"


def _convert_to_mp3(source: Path, bitrate: str = "192k") -> Path:
    target = source.with_suffix(".mp3")
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-loglevel",
        "error",
        "-hide_banner",
        "-i",
        source.as_posix(),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-b:a",
        bitrate,
        target.as_posix(),
    ]
    LOG.info("Converting %s -> %s", source.name, target.name)
    subprocess.run(cmd, check=True)
    return target


def _iter_videos(
    event: BinanceCourseEvent,
    settings: BinanceSettings,
    session: requests.Session,
) -> Iterable[CourseVideo]:
    videos = parse_course(
        event.course_url,
        language=event.language,
        timeout=settings.request_timeout,
        session=session,
    )
    for video in videos:
        yield video


def _relative_key(path: Path, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.name


def _build_video_id(video: CourseVideo) -> str:
    slug = Path(video.video_link.rstrip("/")).name
    return f"binance_{video.language}_{slug}"[:120]


def _upload_outputs(
    mp3_path: Path,
    metadata_path: Path,
    settings: BinanceSettings,
    *,
    base_dir: Path,
) -> tuple[str, Optional[str]]:
    relative_mp3 = _relative_key(mp3_path, base_dir)
    relative_metadata = _relative_key(metadata_path, base_dir)
    mp3_uri = maybe_upload(
        mp3_path,
        bucket=settings.gcs_bucket,
        prefix=settings.gcs_prefix,
        relative_key=relative_mp3,
    )
    metadata_uri = maybe_upload(
        metadata_path,
        bucket=settings.gcs_bucket,
        prefix=settings.gcs_prefix,
        relative_key=relative_metadata,
    )
    if not mp3_uri:
        raise RuntimeError("Failed to upload Binance mp3 to GCS.")
    return mp3_uri, metadata_uri


def process_course_event(event: BinanceCourseEvent) -> List[Mp3ReadyEvent]:
    settings = BinanceSettings()
    # Ensure files remain until we've finished post-processing.
    working_settings = replace(settings, keep_local_files=True)
    session = requests.Session()
    events: List[Mp3ReadyEvent] = []
    base_dir = working_settings.ensure_output_dir()
    LOG.info("Processing Binance course %s (%s)", event.course_url, event.language)

    videos = list(_iter_videos(event, working_settings, session))
    if not videos:
        LOG.info("No videos for course %s", event.course_url)
        return events

    for video in videos:
        try:
            mp4_path = download_video(video, working_settings, session=session)
        except Exception as exc:
            LOG.exception("Failed to download %s: %s", video.video_link, exc)
            continue
        if not mp4_path:
            continue

        metadata_path = mp4_path.with_name("metadata.json")
        try:
            mp3_path = _convert_to_mp3(mp4_path)
        except subprocess.CalledProcessError as exc:
            LOG.error("Failed to convert %s to mp3: %s", mp4_path, exc)
            continue

        try:
            mp3_uri, metadata_uri = _upload_outputs(
                mp3_path,
                metadata_path,
                working_settings,
                base_dir=base_dir,
            )
        except Exception as exc:
            LOG.error("Failed to upload Binance artefacts for %s: %s", video.video_link, exc)
            continue

        events.append(
            Mp3ReadyEvent(
                gcs_uri=mp3_uri,
                metadata_uri=metadata_uri,
                video_id=_build_video_id(video),
            )
        )

        if not settings.keep_local_files:
            clip_dir = mp4_path.parent
            try:
                shutil.rmtree(clip_dir)
            except OSError as exc:
                LOG.warning("Failed to remove %s: %s", clip_dir, exc)

    return events
