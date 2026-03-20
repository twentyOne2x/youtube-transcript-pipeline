from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
from typing import Dict, Optional
import shutil

from .config import PumpfunSettings
from src.utils.gcs import blob_exists, maybe_upload


LOG = logging.getLogger(__name__)


@dataclass
class ClipDownloadResult:
    room_name: str
    clip_id: str
    mp4_path: Optional[Path]
    mp3_path: Optional[Path]
    skipped: bool = False
    metadata_path: Optional[Path] = None
    gcs_mp4_uri: Optional[str] = None
    gcs_mp3_uri: Optional[str] = None
    gcs_metadata_uri: Optional[str] = None


def sanitize_filename(value: str) -> str:
    replacements = {
        '"': "",
        "'": "",
        "/": "-",
        "\\": "-",
        "|": "-",
        ":": "-",
        "?": "",
        "*": "",
        "<": "",
        ">": "",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    # Replace unicode dashes and ellipsis
    value = value.replace("—", "-").replace("–", "-").replace("…", "...")
    normalized = "".join(ch for ch in value if 32 <= ord(ch) < 127)
    return normalized.strip() or "untitled"


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _build_clip_base_name(clip: Dict) -> str:
    timestamp = clip.get("startTime") or clip.get("createdAt")
    if timestamp:
        try:
            dt = _parse_timestamp(timestamp)
            prefix = dt.strftime("%Y-%m-%d_%H-%M-%S")
        except Exception:
            prefix = "unknown"
    else:
        prefix = "unknown"
    clip_id = clip.get("clipId") or "clip"
    clip_id = clip_id.replace(":", "-")
    return sanitize_filename(f"{prefix}_{clip_id}")


def _run_ffmpeg(args: list[str]) -> None:
    LOG.debug("Running command: %s", " ".join(args))
    completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 0:
        LOG.error("ffmpeg failed: %s", completed.stderr.strip())
        raise RuntimeError(f"ffmpeg exited with code {completed.returncode}")


def _write_metadata(path: Path, clip: Dict, coin: Dict | None) -> None:
    data = {
        "clip": clip,
        "coin": coin or {},
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def download_clip(
    *,
    clip: Dict,
    room_name: str,
    playlist_url: str,
    coin: Dict | None,
    settings: PumpfunSettings,
) -> ClipDownloadResult:
    base_dir = settings.ensure_output_dir()
    coin_label = coin.get("name") if coin else None
    room_label = coin_label or room_name
    room_dir = base_dir / sanitize_filename(f"{room_label}_{room_name[:8]}")
    room_dir.mkdir(parents=True, exist_ok=True)

    base_name = _build_clip_base_name(clip)
    clip_dir = room_dir / base_name
    clip_dir.mkdir(parents=True, exist_ok=True)

    mp4_path = clip_dir / f"{base_name}.mp4"
    mp3_path = clip_dir / f"{base_name}.mp3"
    metadata_path = clip_dir / "metadata.json"

    try:
        relative_dir = clip_dir.relative_to(base_dir)
    except ValueError:
        relative_dir = Path(clip_dir.name)

    metadata_relative = (relative_dir / metadata_path.name).as_posix()
    mp4_relative = (relative_dir / mp4_path.name).as_posix()
    mp3_relative = (relative_dir / mp3_path.name).as_posix()

    def _build_gcs_uri(relative_key: str) -> Optional[str]:
        if not settings.gcs_bucket:
            return None
        parts = [p for p in (settings.gcs_prefix, relative_key) if p]
        return f"gs://{settings.gcs_bucket}/{'/'.join(parts)}"

    if settings.skip_existing and settings.gcs_bucket:
        metadata_blob = "/".join(p for p in (settings.gcs_prefix, metadata_relative) if p)
        mp4_blob = "/".join(p for p in (settings.gcs_prefix, mp4_relative) if p)
        mp3_blob = "/".join(p for p in (settings.gcs_prefix, mp3_relative) if p)

        metadata_exists = blob_exists(settings.gcs_bucket, metadata_blob)
        mp4_exists = True if not settings.download_mp4 else blob_exists(settings.gcs_bucket, mp4_blob)
        mp3_exists = True if not settings.download_mp3 else blob_exists(settings.gcs_bucket, mp3_blob)

        if metadata_exists and mp4_exists and mp3_exists:
            LOG.info("Skipping clip %s/%s (artifacts already uploaded)", room_name, clip.get("clipId"))
            return ClipDownloadResult(
                room_name=room_name,
                clip_id=clip.get("clipId", ""),
                mp4_path=None,
                mp3_path=None,
                skipped=True,
                metadata_path=None,
                gcs_mp4_uri=_build_gcs_uri(mp4_relative) if settings.download_mp4 else None,
                gcs_mp3_uri=_build_gcs_uri(mp3_relative) if settings.download_mp3 else None,
                gcs_metadata_uri=_build_gcs_uri(metadata_relative),
            )

    if settings.skip_existing and mp4_path.exists() and (not settings.download_mp3 or mp3_path.exists()):
        LOG.info("Skipping clip %s/%s (files already exist)", room_name, clip.get("clipId"))
        if not metadata_path.exists():
            _write_metadata(metadata_path, clip, coin)
        return ClipDownloadResult(
            room_name,
            clip.get("clipId", ""),
            mp4_path if mp4_path.exists() else None,
            mp3_path if mp3_path.exists() else None,
            skipped=True,
            metadata_path=metadata_path if metadata_path.exists() else None,
            gcs_mp4_uri=_build_gcs_uri(mp4_relative) if settings.download_mp4 else None,
            gcs_mp3_uri=_build_gcs_uri(mp3_relative) if settings.download_mp3 else None,
            gcs_metadata_uri=_build_gcs_uri(metadata_relative),
        )

    _write_metadata(metadata_path, clip, coin)

    mp4_result_path: Optional[Path] = None
    if settings.download_mp4:
        if not mp4_path.exists() or not settings.skip_existing:
            LOG.info("Downloading mp4 for %s/%s", room_name, clip.get("clipId"))
            _run_ffmpeg(
                [
                    settings.ffmpeg_bin,
                    "-y",
                    "-loglevel",
                    "error",
                    "-hide_banner",
                    "-i",
                    playlist_url,
                    "-c",
                    "copy",
                    str(mp4_path),
                ]
            )
        mp4_result_path = mp4_path

    mp3_result_path: Optional[Path] = None
    if settings.download_mp3:
        if not mp3_path.exists() or not settings.skip_existing:
            LOG.info("Extracting mp3 for %s/%s", room_name, clip.get("clipId"))
            source = str(mp4_path if mp4_result_path else playlist_url)
            args = [
                settings.ffmpeg_bin,
                "-y",
                "-loglevel",
                "error",
                "-hide_banner",
                "-i",
                source,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-b:a",
                settings.mp3_bitrate,
                str(mp3_path),
            ]
            try:
                _run_ffmpeg(args)
            except RuntimeError:
                # If a previous run was interrupted (Ctrl-C/timeout), ffmpeg may have left a
                # corrupt/truncated mp4 on disk. In that case the mp3 extraction fails (e.g.
                # "moov atom not found") and the "skip existing" logic would prevent a re-download
                # forever. Recover by deleting the mp4 and retrying once.
                if settings.download_mp4 and mp4_result_path and mp4_path.exists() and settings.skip_existing:
                    try:
                        mp4_path.unlink()
                    except OSError:
                        pass
                    LOG.warning("mp3 extraction failed; re-downloading mp4 and retrying once: %s", mp4_path)
                    _run_ffmpeg(
                        [
                            settings.ffmpeg_bin,
                            "-y",
                            "-loglevel",
                            "error",
                            "-hide_banner",
                            "-i",
                            playlist_url,
                            "-c",
                            "copy",
                            str(mp4_path),
                        ]
                    )
                    _run_ffmpeg(args)
                else:
                    raise
        mp3_result_path = mp3_path

    if not settings.download_mp4 and mp4_path.exists() and not settings.download_mp3:
        # No outputs require the mp4 artefact.
        try:
            mp4_path.unlink()
        except OSError:
            pass

    gcs_metadata_uri = None
    gcs_mp4_uri = None
    gcs_mp3_uri = None

    rel_mp4 = mp4_relative if mp4_result_path else None
    rel_mp3 = mp3_relative if mp3_result_path else None

    gcs_metadata_uri = maybe_upload(metadata_path, bucket=settings.gcs_bucket, prefix=settings.gcs_prefix, relative_key=metadata_relative) or gcs_metadata_uri
    if mp4_result_path:
        gcs_mp4_uri = maybe_upload(mp4_result_path, bucket=settings.gcs_bucket, prefix=settings.gcs_prefix, relative_key=rel_mp4 or mp4_result_path.name) or gcs_mp4_uri
    if mp3_result_path:
        gcs_mp3_uri = maybe_upload(mp3_result_path, bucket=settings.gcs_bucket, prefix=settings.gcs_prefix, relative_key=rel_mp3 or mp3_result_path.name) or gcs_mp3_uri

    if settings.gcs_bucket and not settings.keep_local_files:
        try:
            shutil.rmtree(clip_dir)
            LOG.debug("Removed local copy after upload: %s", clip_dir)
        except OSError as exc:
            LOG.warning("Failed to remove local directory %s: %s", clip_dir, exc)
        else:
            mp4_result_path = None
            mp3_result_path = None
            metadata_path = None

    return ClipDownloadResult(
        room_name=room_name,
        clip_id=clip.get("clipId", ""),
        mp4_path=mp4_result_path,
        mp3_path=mp3_result_path,
        skipped=False,
        metadata_path=metadata_path,
        gcs_mp4_uri=gcs_mp4_uri,
        gcs_mp3_uri=gcs_mp3_uri,
        gcs_metadata_uri=gcs_metadata_uri,
    )
