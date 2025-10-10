from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import List, Optional

from src import root_directory


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class BinanceSettings:
    """Runtime configuration for Binance Academy ingestion."""

    languages: List[str] = field(
        default_factory=lambda: _split_csv(os.environ.get("BINANCE_LANGUAGE_CODES", "en"))
    )
    output_dir: Path = Path(
        os.environ.get(
            "BINANCE_OUTPUT_DIR",
            root_directory() / "datasets" / "evaluation_data" / "binance_academy",
        )
    )
    request_timeout: int = int(os.environ.get("BINANCE_REQUEST_TIMEOUT", "30"))

    gcs_bucket: Optional[str] = os.environ.get("BINANCE_GCS_BUCKET") or None
    gcs_prefix: str = os.environ.get("BINANCE_GCS_PREFIX", "binance_academy")
    keep_local_files: bool = _as_bool(os.environ.get("BINANCE_KEEP_LOCAL"), True)

    max_consecutive_errors: int = int(os.environ.get("BINANCE_MAX_ERRORS", "5"))

    sitemap_base: str = os.environ.get(
        "BINANCE_SITEMAP_BASE", "https://academy.binance.com/learnAndEarn"
    )

    wistia_timeout: int = int(os.environ.get("BINANCE_WISTIA_TIMEOUT", "30"))

    def ensure_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir
