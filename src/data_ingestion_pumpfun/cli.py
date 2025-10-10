from __future__ import annotations

import argparse
from dataclasses import replace
import logging
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import requests

from .api import fetch_coin_metadata, iter_clips
from .config import PumpfunSettings
from .downloader import ClipDownloadResult, download_clip
from .room_config import RoomEntry, hydrate_room_labels, load_room_config


LOG = logging.getLogger("pumpfun")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _load_room_entries(
    args: argparse.Namespace,
    settings: PumpfunSettings,
    session: requests.Session,
) -> Tuple[List[RoomEntry], dict]:
    config_defaults: dict = {}
    entries: List[RoomEntry] = []

    if args.config:
        config_entries, config_defaults = load_room_config(args.config)
        entries.extend(config_entries)

    if args.rooms:
        for room in args.rooms:
            entries.append(RoomEntry(room=room, max_clips=args.max_clips))

    if args.rooms_file:
        path = Path(args.rooms_file)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                entries.append(RoomEntry(room=value, max_clips=args.max_clips))

    if not entries:
        return [], config_defaults

    # Deduplicate while preserving the earliest declaration.
    deduped: dict[str, RoomEntry] = {}
    for entry in entries:
        existing = deduped.get(entry.room)
        if existing is None:
            deduped[entry.room] = entry
        else:
            if not existing.label and entry.label:
                existing.label = entry.label
            if existing.max_clips is None and entry.max_clips is not None:
                existing.max_clips = entry.max_clips

    final_entries = [entry for entry in deduped.values()]
    hydrate_room_labels(final_entries, settings, session=session)
    return final_entries, config_defaults


def _maybe_override_settings(
    settings: PumpfunSettings,
    args: argparse.Namespace,
    config_defaults: Optional[dict] = None,
) -> PumpfunSettings:
    updates = {}
    if config_defaults and config_defaults.get("max_clips") is not None:
        updates["max_clips_per_room"] = int(config_defaults["max_clips"])
    if args.output_dir:
        updates["output_dir"] = Path(args.output_dir)
    if args.max_clips is not None:
        updates["max_clips_per_room"] = args.max_clips
    if args.no_mp4:
        updates["download_mp4"] = False
    if args.mp3_only:
        updates["download_mp4"] = False
        updates["download_mp3"] = True
    if args.mp4_only:
        updates["download_mp4"] = True
        updates["download_mp3"] = False
    if not updates:
        return settings
    return replace(settings, **updates)


def _download_for_room(entry: RoomEntry, settings: PumpfunSettings, session: requests.Session) -> List[ClipDownloadResult]:
    results: List[ClipDownloadResult] = []
    log_label = f"{entry.label} ({entry.room})" if entry.label else entry.room
    LOG.info("Processing Pump.fun room: %s", log_label)

    coin = fetch_coin_metadata(entry.room, settings, session=session)
    if not entry.label:
        entry.label = coin.get("name") or entry.label

    max_results = entry.max_clips
    if max_results is None:
        max_results = settings.max_clips_per_room
    if max_results is not None and max_results <= 0:
        max_results = None

    for clip in iter_clips(entry.room, settings, max_results=max_results, session=session):
        playlist_url = clip.get("playlistUrl")
        if not playlist_url:
            LOG.warning("Clip %s missing playlist URL, skipping", clip.get("clipId"))
            continue
        try:
            result = download_clip(
                clip=clip,
                room_name=entry.room,
                playlist_url=playlist_url,
                coin=coin,
                settings=settings,
            )
            results.append(result)
        except Exception as exc:
            LOG.exception("Failed to download clip %s: %s", clip.get("clipId"), exc)
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Pump.fun livestream clips as mp4/mp3.")
    parser.add_argument(
        "--config",
        help="Path to a Pump.fun rooms config (JSON). Defaults to data/links/pumpfun/pumpfun_rooms.json if present.",
    )
    parser.add_argument(
        "--rooms",
        nargs="+",
        help="Pump.fun room identifiers (coin mint addresses).",
    )
    parser.add_argument(
        "--rooms-file",
        help="Path to a newline-delimited list of Pump.fun room identifiers.",
    )
    parser.add_argument(
        "--output-dir",
        help="Destination directory (defaults to datasets/evaluation_data/pumpfun_streams).",
    )
    parser.add_argument(
        "--max-clips",
        type=int,
        help="Limit the number of clips per room (defaults to PUMPFUN_MAX_CLIPS_PER_ROOM).",
    )
    parser.add_argument("--no-mp4", action="store_true", help="Skip mp4 downloads (mp3 only).")
    parser.add_argument("--mp3-only", action="store_true", help="Alias for --no-mp4 but keep mp3 downloads.")
    parser.add_argument("--mp4-only", action="store_true", help="Download mp4 only.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if (args.mp3_only and args.mp4_only) or (args.mp4_only and args.no_mp4):
        parser.error("Conflicting output options specified.")

    _configure_logging(args.verbose)
    settings = PumpfunSettings()

    # Implicit default config path if none provided and file exists.
    if not args.config:
        default_config = Path("data/links/pumpfun/pumpfun_rooms.json")
        if default_config.exists():
            args.config = str(default_config)

    session = requests.Session()
    entries, config_defaults = _load_room_entries(args, settings, session)
    if not entries:
        parser.error("No Pump.fun rooms provided. Use --config, --rooms, or --rooms-file.")

    settings = _maybe_override_settings(settings, args, config_defaults)

    summary: List[ClipDownloadResult] = []
    for entry in entries:
        summary.extend(_download_for_room(entry, settings, session))

    skipped = sum(1 for r in summary if r.skipped)
    LOG.info("Download complete. %d clips processed (%d skipped).", len(summary), skipped)


if __name__ == "__main__":
    main()
