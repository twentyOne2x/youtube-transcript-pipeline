from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .config import PumpfunSettings
from .api import fetch_coin_metadata


LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class RoomEntry:
    room: str
    label: Optional[str] = None
    max_clips: Optional[int] = None

    def normalized(self) -> "RoomEntry":
        return replace(
            self,
            room=self.room.strip(),
            label=self.label.strip() if isinstance(self.label, str) else self.label,
        )


def _ensure_path(path: Path | str) -> Path:
    return path if isinstance(path, Path) else Path(path)


def load_room_config(path: Path | str) -> Tuple[List[RoomEntry], Dict]:
    """
    Load Pump.fun room configuration from JSON.

    The structure can either be a list of room objects or an object with
    ``{"rooms": [...], "defaults": {...}}``.
    """
    config_path = _ensure_path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if isinstance(raw, dict):
        rooms_data = raw.get("rooms", [])
        defaults = raw.get("defaults", {})
    elif isinstance(raw, list):
        rooms_data = raw
        defaults = {}
    else:
        raise ValueError(f"Unsupported rooms config shape in {config_path}")

    entries: List[RoomEntry] = []
    for item in rooms_data:
        room = item.get("room") or item.get("mint")
        if not room:
            raise ValueError(f"Invalid room entry missing 'room': {item}")
        label = item.get("label") or item.get("name")
        max_clips = item.get("max_clips", defaults.get("max_clips"))
        entries.append(RoomEntry(room=room, label=label, max_clips=max_clips).normalized())
    return entries, defaults


def save_room_config(path: Path | str, entries: Sequence[RoomEntry], defaults: Optional[Dict] = None) -> None:
    """Persist room configuration JSON."""
    config_path = _ensure_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    payload: Dict[str, object] = {"rooms": [asdict(entry) for entry in entries]}
    if defaults:
        payload["defaults"] = defaults

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def hydrate_room_labels(
    entries: Iterable[RoomEntry],
    settings: PumpfunSettings,
    session: Optional[requests.Session] = None,
) -> bool:
    """
    Fill in missing labels by fetching metadata from Pump.fun.

    Returns True if any labels were updated.
    """
    sess = session or requests.Session()
    updated = False
    for entry in entries:
        if entry.label:
            continue
        try:
            coin = fetch_coin_metadata(entry.room, settings, session=sess)
        except requests.HTTPError as exc:
            LOG.warning("Failed to fetch coin metadata for %s: %s", entry.room, exc)
            continue
        name = coin.get("name") or coin.get("symbol")
        if name:
            entry.label = name
            updated = True
    return updated


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Utilities for managing Pump.fun room configuration files."
    )
    parser.add_argument("config", help="Path to pumpfun_rooms.json.")
    parser.add_argument(
        "--refresh-labels",
        action="store_true",
        help="Fetch channel names for entries missing the 'label' field.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    settings = PumpfunSettings()
    entries, defaults = load_room_config(args.config)

    LOG.info("Loaded %d Pump.fun room entries.", len(entries))
    session = requests.Session()

    if args.refresh_labels:
        if hydrate_room_labels(entries, settings, session=session):
            save_room_config(args.config, entries, defaults)
            LOG.info("Labels refreshed and configuration updated.")
        else:
            LOG.info("No missing labels; configuration unchanged.")


if __name__ == "__main__":
    main()
