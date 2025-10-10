from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
import os

class DlStatus(str, Enum):
    OK = "ok"
    RATE = "rate_limited"
    SABR = "sabr"
    NOAUDIO = "no_audio_only"
    ERR = "error"

@dataclass(frozen=True)
class Settings:
    audio_format: str = os.environ.get("AUDIO_FORMAT", "mp3").lower()  # m4a|opus|mp3|source
    require_audio_only: bool = os.environ.get("REQUIRE_AUDIO_ONLY", "True").lower() == "true"
    allow_sabr_fallback: bool = os.environ.get("ALLOW_SABR_FALLBACK", "False").lower() == "true"

    adaptive_max_cap: int = int(os.environ.get("ADAPTIVE_MAX_CAP", "6"))
    adaptive_min_cap: int = int(os.environ.get("ADAPTIVE_MIN_CAP", "2"))

    download_audio: bool = os.environ.get("DOWNLOAD_AUDIO", "True").lower() == "true"
    max_per_channel: int = int(os.environ.get("MAX_PER_CHANNEL", "50"))
    skip_if_exists: bool = os.environ.get("SKIP_IF_MP3_EXISTS", "True").lower() == "true"

    browser: str = os.environ.get("BROWSER", "brave")
    profile: str = os.environ.get("PROFILE", "Default")

    global_max_downloads: int = int(os.environ.get("GLOBAL_MAX_DOWNLOADS", "6"))

    debug_list_formats: bool = os.environ.get("DEBUG_LIST_FORMATS", "false").lower() == "true"

    use_browser_cookies: bool = os.environ.get("USE_BROWSER_COOKIES", "true").lower() == "true"

    default_client: str = os.environ.get("DEFAULT_CLIENT", "").strip()
    preferred_itags: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.environ.get("PREFERRED_ITAGS", "").split(",") if s.strip()
    ])

    gcs_bucket: Optional[str] = os.environ.get("YOUTUBE_GCS_BUCKET")
    gcs_prefix: str = os.environ.get("YOUTUBE_GCS_PREFIX", "youtube_audio")
    keep_local_files: bool = os.environ.get("YOUTUBE_KEEP_LOCAL", "true").lower() == "true"
