from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

from src import PUMPFUN_STREAM_DIRECTORY


def _as_bool(flag: str | None, default: bool) -> bool:
    if flag is None:
        return default
    return flag.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PumpfunSettings:
    """Runtime configuration for Pump.fun downloads."""

    api_base: str = os.environ.get("PUMPFUN_API_BASE", "https://livestream-api.pump.fun")
    coin_api_base: str = os.environ.get("PUMPFUN_COIN_API_BASE", "https://frontend-api-v3.pump.fun")
    clips_cdn_base: str = os.environ.get("PUMPFUN_CLIPS_BASE", "https://clips.pump.fun")
    output_dir: Path = Path(os.environ.get("PUMPFUN_OUTPUT_DIR", PUMPFUN_STREAM_DIRECTORY))

    download_mp4: bool = _as_bool(os.environ.get("PUMPFUN_DOWNLOAD_MP4"), True)
    download_mp3: bool = _as_bool(os.environ.get("PUMPFUN_DOWNLOAD_MP3"), True)
    skip_existing: bool = _as_bool(os.environ.get("PUMPFUN_SKIP_EXISTING"), True)
    keep_local_files: bool = _as_bool(os.environ.get("PUMPFUN_KEEP_LOCAL"), True)

    max_clips_per_room: int = int(os.environ.get("PUMPFUN_MAX_CLIPS_PER_ROOM", "25"))
    request_timeout: int = int(os.environ.get("PUMPFUN_REQUEST_TIMEOUT", "30"))

    ffmpeg_bin: str = os.environ.get("FFMPEG_BIN", "ffmpeg")
    mp3_bitrate: str = os.environ.get("PUMPFUN_MP3_BITRATE", "192k")

    gcs_bucket: Optional[str] = os.environ.get("PUMPFUN_GCS_BUCKET")  # type: ignore[name-defined]
    gcs_prefix: str = os.environ.get("PUMPFUN_GCS_PREFIX", "pumpfun_streams")

    def ensure_output_dir(self) -> Path:
        """Create and return the base output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir
