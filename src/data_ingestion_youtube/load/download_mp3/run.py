# src/data_ingestion_youtube/load/download_mp3/run.py
import os
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import pandas as pd
from dotenv import load_dotenv
from googleapiclient.discovery import build

from src import root_directory, YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.utils import get_channel_id, get_video_info
from src.utils.utils import authenticate_service_account
from src.data_ingestion_youtube.load.download_mp3.config import Settings
from src.data_ingestion_youtube.load.download_mp3.batches import process_global

# ------------------------------------------------------------------------------
# Logging (quiet about per-item skips; summary-only)
# ------------------------------------------------------------------------------
LOG = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
_INVALID_FS_CHARS = re.compile(r'[\\/:\*\?"<>\|\x00-\x1F]+')


def _sanitize_filename(name: str, max_len: int = 180) -> str:
    """Conservative filename sanitizer (Windows-safe)."""
    name = name.strip()
    name = _INVALID_FS_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len]


def _to_yyyy_mm_dd(date_val: Optional[str]) -> str:
    """
    Normalize various date shapes to 'YYYY-MM-DD'.
    Accepts: 'YYYYMMDD', 'YYYY-MM-DD', ISO timestamps like '2023-07-01T12:34:56Z', or None.
    """
    if not date_val:
        return "unknown-date"
    date_val = str(date_val)
    if re.fullmatch(r"\d{8}", date_val):
        return f"{date_val[0:4]}-{date_val[4:6]}-{date_val[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_val):
        return date_val
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_val)
    if m:
        return m.group(1)
    return "unknown-date"


def _extract_video_fields(v: Dict) -> Tuple[str, str, str, Optional[str]]:
    """
    Extract (video_id, title, date_iso, url) from a heterogeneous video dict.
    """
    vid = (
        v.get("video_id")
        or v.get("id")
        or v.get("resourceId", {}).get("videoId")
        or v.get("contentDetails", {}).get("videoId")
        or v.get("url")
    )
    title = (
        v.get("title")
        or v.get("name")
        or v.get("snippet", {}).get("title")
        or "untitled"
    )
    date_raw = (
        v.get("upload_date")
        or v.get("published_date")
        or v.get("publishedAt")
        or v.get("published_at")
        or v.get("publish_date")
        or v.get("date")
        or v.get("snippet", {}).get("publishedAt")
    )
    url = v.get("url")
    return str(vid) if vid is not None else "unknown-id", str(title), _to_yyyy_mm_dd(date_raw), url


def _expected_leaf_name(date_iso: str, video_id: str, title: str) -> str:
    """Build the directory/file leaf name: YYYY-MM-DD_<id>_<title> (sanitized)."""
    title_for_path = title.replace('"', "")
    leaf = f"{date_iso}_{video_id}_{title_for_path}"
    return _sanitize_filename(leaf)


def _find_existing_download_dir(channel_dir: Path, video_id: str) -> Optional[Path]:
    """
    Robust "already downloaded?" check that does NOT depend on exact title.
    Finds any subdir containing '_<video_id>_' in its name.
    """
    if not channel_dir.exists():
        return None
    with os.scandir(channel_dir) as it:
        for entry in it:
            if entry.is_dir() and f"_{video_id}_" in entry.name:
                return Path(entry.path)
    return None


def _mp3_exists_in_dir(d: Path) -> bool:
    if not d or not d.exists():
        return False
    with os.scandir(d) as it:
        for entry in it:
            if entry.is_file() and entry.name.lower().endswith(".mp3"):
                return True
    return False


def _purge_stale_part_files(d: Optional[Path]) -> None:
    """If the final mp3 exists, clear out *.part crumbs to avoid bad resumes (HTTP 416)."""
    if not d or not d.exists():
        return
    with os.scandir(d) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith(".part"):
                try:
                    os.remove(entry.path)
                except Exception:
                    pass


def _date_to_int(date_iso: str) -> int:
    """Convert 'YYYY-MM-DD' → yyyymmdd int for sorting; unknown → -1."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date_iso or "")
    if not m:
        return -1
    y, mm, dd = map(int, m.groups())
    return y * 10000 + mm * 100 + dd


def _year_of(date_iso: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso or ""):
        return date_iso[:4]
    return "unknown"


def _truncate(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _sort_desc_by_date(videos: List[Dict]) -> List[Dict]:
    """Sort newest→oldest using normalized dates; unknown dates go last."""
    def key(v: Dict) -> int:
        _, _, date_iso, _ = _extract_video_fields(v)
        return _date_to_int(date_iso)
    return sorted(videos, key=key, reverse=True)


def _print_plan_summary_table(plan_per_channel: Dict[str, List[Dict]]) -> None:
    """
    Pretty print a concise summary table:
      Channel | Missing | Top-3 most recent missing (YYYY-MM-DD — title)
    Ordered by smallest Missing count first.
    """
    if not plan_per_channel:
        print("\nNo downloads needed. ✅\n")
        return

    ch_width = 32
    cnt_width = 8
    top_width = 90

    print("\n=== Download Plan Summary (newest -> oldest) ===")
    print(f"{'Channel':{ch_width}} {'Missing':>{cnt_width}}  Top-3 most recent missing")
    print("-" * (ch_width + cnt_width + 2 + 64))

    total = 0
    for channel, vids in sorted(
        plan_per_channel.items(),
        key=lambda kv: (len(kv[1]), kv[0].lower())
    ):
        total += len(vids)
        top3 = vids[:3]
        triples = []
        for v in top3:
            vid, title, date_iso, _ = _extract_video_fields(v)
            triples.append(f"{date_iso} — {_truncate(title, top_width)}")
        top3_str = " | ".join(triples) if triples else "-"
        print(f"{channel:{ch_width}} {len(vids):>{cnt_width}}  {top3_str}")

    print("-" * (ch_width + cnt_width + 2 + 64))
    print(f"TOTAL videos to download: {total}\n")

    # Optional: persist the full plan as CSV
    try:
        rows = []
        for channel, vids in plan_per_channel.items():
            for v in vids:
                vid, title, date_iso, url = _extract_video_fields(v)
                rows.append(
                    {
                        "channel": channel,
                        "video_id": vid,
                        "date": date_iso,
                        "year": _year_of(date_iso),
                        "title": title,
                        "url": url or (f"https://youtube.com/watch?v={vid}" if vid and vid != "unknown-id" else ""),
                    }
                )
        if rows:
            df = pd.DataFrame(rows)
            out_csv = Path(root_directory()) / "data" / "links" / "youtube" / "planned_downloads_summary.csv"
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out_csv, index=False)
            print(f"Saved detailed plan to: {out_csv}")
    except Exception as e:
        LOG.debug("Could not write plan CSV: %s", e)


def _group_by_year(videos: List[Dict]) -> Dict[str, List[Dict]]:
    buckets: Dict[str, List[Dict]] = {}
    for v in videos:
        _, _, date_iso, _ = _extract_video_fields(v)
        buckets.setdefault(_year_of(date_iso), []).append(v)
    for y in list(buckets.keys()):
        buckets[y] = _sort_desc_by_date(buckets[y])
    return buckets


def _chunks(seq: List[str], size: int) -> List[List[str]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _parse_id_list_env(var: str) -> List[str]:
    raw = os.getenv(var, "") or ""
    return [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]


# ---------- ID-first helpers (CSV reuse + API fallback) ------------------------
def _load_csv_rows_by_ids(ids: List[str]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    if not ids:
        return out
    csv_path = Path(root_directory()) / "data" / "links" / "youtube" / "youtube_videos.csv"
    if not csv_path.exists():
        return out
    try:
        df = pd.read_csv(csv_path, encoding="utf-8")
    except Exception:
        return out
    id_set = set(ids)
    for _, r in df.iterrows():
        vid = str(r.get("video_id", "")).strip()
        if vid and vid in id_set:
            out[vid] = {
                "video_id": vid,
                "title": r.get("title", ""),
                "published_date": r.get("published_date", ""),
                "channel_name": r.get("channel_name", ""),
                "channel_id": r.get("channel_id", ""),
                "url": r.get("url", f"https://www.youtube.com/watch?v={vid}"),
            }
    return out


def _invert_mapping(d: Dict[str, str]) -> Dict[str, str]:
    # returns value->key; if duplicates, last wins
    return {v: k for k, v in (d or {}).items() if v}


def _derive_channel_folder(channel_title: str, channel_id: Optional[str]) -> str:
    """
    Try to return an @handle folder if we can resolve it from mappings.
    Fallback to the readable channel title otherwise.
    """
    # Load mappings
    base = Path(root_directory()) / "data" / "links"
    handle_to_id_path = base / "channel_handle_to_id_mapping.json"
    handle_to_name_path = base / "channel_handle_to_name_mapping.json"

    handle_to_id: Dict[str, str] = {}
    handle_to_name: Dict[str, str] = {}
    try:
        if handle_to_id_path.exists():
            handle_to_id = json.loads(handle_to_id_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        if handle_to_name_path.exists():
            handle_to_name = json.loads(handle_to_name_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Invert for lookups
    id_to_handle = {v: k for k, v in handle_to_id.items() if v}
    name_to_handle = _invert_mapping(handle_to_name)

    # 1) By channel_id
    if channel_id and channel_id in id_to_handle:
        return id_to_handle[channel_id]

    # 2) By channel_title
    if channel_title and channel_title in name_to_handle:
        return name_to_handle[channel_title]

    # 3) Already a handle?
    if channel_title and channel_title.startswith("@"):
        return channel_title

    # Fallback
    return channel_title or "UnknownChannel"


def _fetch_video_snippets(api_key: str, ids: List[str]) -> Dict[str, Dict]:
    """
    Call YouTube Data API for a list of video IDs and return
    id -> {title, published_date, channel_title, channel_id, url}
    """
    if not ids:
        return {}
    youtube = build("youtube", "v3", developerKey=api_key)
    out: Dict[str, Dict] = {}
    MAX = 50
    for i in range(0, len(ids), MAX):
        batch = ids[i:i + MAX]
        try:
            resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
            items = resp.get("items", [])
        except Exception as e:
            LOG.warning("YouTube API failed for batch %s..%s: %s", i, i + len(batch) - 1, e)
            continue
        for it in items:
            vid = it.get("id")
            sn = it.get("snippet", {}) or {}
            title = sn.get("title", "") or ""
            published_at = sn.get("publishedAt", "") or ""
            ch_title = sn.get("channelTitle", "") or ""
            ch_id = sn.get("channelId", "") or ""
            out[vid] = {
                "video_id": vid,
                "title": title,
                "published_date": _to_yyyy_mm_dd(published_at),
                "channel_name": ch_title,
                "channel_id": ch_id,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
    return out


def _merge_unique_by_id(existing: List[Dict], add: List[Dict]) -> List[Dict]:
    seen = {str(_extract_video_fields(v)[0]) for v in existing}
    out = list(existing)
    for v in add:
        vid = str(_extract_video_fields(v)[0])
        if vid and vid not in seen:
            out.append(v)
            seen.add(vid)
    return out


def _plan_channel_videos(channel_name: str, channel_dir: Path, videos: List[Dict]) -> List[Dict]:
    """
    Return only the videos that still need downloading for this channel.
    Existence is determined by presence of any .mp3 in a directory whose name contains _<id>_.
    (Quiet: no per-item 'Skip' logs.)
    """
    to_download: List[Dict] = []

    for v in videos:
        video_id, title, date_iso, _ = _extract_video_fields(v)
        if not video_id or video_id == "unknown-id":
            to_download.append(v)
            continue

        # Prefer robust ID-based match first
        existing_dir = _find_existing_download_dir(channel_dir, video_id)
        if existing_dir and _mp3_exists_in_dir(existing_dir):
            _purge_stale_part_files(existing_dir)
            continue

        # Fallback exact-expected path
        leaf = _expected_leaf_name(date_iso, video_id, title)
        leaf_dir = channel_dir / leaf
        leaf_mp3 = leaf_dir / f"{leaf}.mp3"
        if leaf_mp3.exists():
            _purge_stale_part_files(leaf_dir)
            continue

        to_download.append(v)

    return to_download


# ------------------------------------------------------------------------------
# Main async entrypoint
# ------------------------------------------------------------------------------
async def run(api_key: str,
              settings: Settings,
              yt_channels: Optional[List[str]] = None,
              yt_playlists: Optional[List[str]] = None,
              cookie_file: Optional[str] = None,
              batch_size: int = 10,
              prioritize_small_channels: bool = True,
              group_by_year: bool = False,
              channels_per_pass: int = 1):
    """
    prioritize_small_channels: when True, download channels with FEWEST missing items first.
    group_by_year:           when True, process most recent year first (2025, then 2024, ...) and
                             still prioritize small channels WITHIN each year.
    channels_per_pass:       number of channels to batch together per call to process_global.
                             1 = strict per-channel; >1 lets several small channels run together.
    """
    load_dotenv()

    # Allow env var overrides for non-CLI usage
    prioritize_small_channels = bool(int(os.environ.get("PRIORITIZE_SMALL_CHANNELS", "1" if prioritize_small_channels else "0")))
    group_by_year = bool(int(os.environ.get("GROUP_BY_YEAR", "1" if group_by_year else "0")))
    channels_per_pass = max(1, int(os.environ.get("CHANNELS_PER_PASS", str(channels_per_pass))))

    # Optional Google Service Account for private vids
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = authenticate_service_account(service_account_file) if service_account_file else None
    if credentials:
        LOG.info("Service account detected (private videos may be accessible).")
    else:
        LOG.info("No service account detected (public channels/playlists only).")

    # Build channel name → id mapping cache (if present)
    mapping_path = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"
    channel_name_to_id: Dict[str, str] = {}
    if os.path.exists(mapping_path):
        try:
            with open(mapping_path, 'r', encoding='utf-8') as f:
                channel_name_to_id = json.load(f)
        except Exception:
            channel_name_to_id = {}

    # CSV filter (if present) — used for channel scans only; NOT for ID-first items
    titles_in_csv = None
    videos_csv = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
    try:
        youtube_videos_df = pd.read_csv(videos_csv)
        titles_in_csv = set(
            youtube_videos_df['title']
            .astype(str)
            .str.replace(' +', ' ', regex=True)
            .str.replace('"', '', regex=False)
        )
        LOG.info("Loaded title filter CSV with %d titles.", len(titles_in_csv))
    except Exception:
        LOG.info("No title filter CSV found; proceeding without title restrictions.")
        titles_in_csv = None

    # Build plan per channel (sorted newest→oldest)
    base_dir = Path(YOUTUBE_VIDEO_DIRECTORY)
    base_dir.mkdir(parents=True, exist_ok=True)

    items_per_channel: Dict[str, Tuple[str, List[Dict]]] = {}  # cname -> (channel_dir, vids_to_dl)
    plan_per_channel: Dict[str, List[Dict]] = {}
    total_candidates = 0
    total_planned = 0

    # ----------------- NEW: ID-first path (YOUTUBE_VIDEO_IDS) ------------------
    specific_ids = _parse_id_list_env("YOUTUBE_VIDEO_IDS")
    if specific_ids:
        # 1) Reuse CSV rows if present
        by_id_csv = _load_csv_rows_by_ids(specific_ids)

        # 2) For any IDs missing from CSV, call API
        missing_ids = [vid for vid in specific_ids if vid not in by_id_csv]
        by_id_api = _fetch_video_snippets(api_key, missing_ids) if missing_ids else {}

        # 3) Combine and group by derived channel folder (prefer @handle)
        id_items_by_channel: Dict[str, List[Dict]] = {}
        for vid in specific_ids:
            row = by_id_csv.get(vid) or by_id_api.get(vid)
            if not row:
                LOG.warning("YOUTUBE_VIDEO_IDS: could not resolve %s (no CSV row, API miss).", vid)
                continue
            ch_folder = _derive_channel_folder(row.get("channel_name", ""), row.get("channel_id"))
            channel_dir = base_dir / ch_folder
            channel_dir.mkdir(parents=True, exist_ok=True)

            # Shape this dict to look like playlist/video_info entries the rest of the code expects
            vdict = {
                "video_id": row["video_id"],
                "title": row.get("title", ""),
                "published_date": row.get("published_date", ""),
                "url": row.get("url", f"https://www.youtube.com/watch?v={row['video_id']}"),
            }
            id_items_by_channel.setdefault(ch_folder, []).append(vdict)

        # 4) Plan these per channel (bypass title CSV filter; we intentionally download these)
        for ch_name, vids in id_items_by_channel.items():
            channel_dir = base_dir / ch_name
            to_download = _plan_channel_videos(ch_name, channel_dir, vids)
            to_download = _sort_desc_by_date(to_download)
            if to_download:
                # If the same channel is also scanned below, we'll merge later
                existing = plan_per_channel.get(ch_name, [])
                merged = _merge_unique_by_id(existing, to_download)
                plan_per_channel[ch_name] = merged
                items_per_channel[ch_name] = (str(channel_dir), merged)
                total_planned += len(to_download)

        total_candidates += len(specific_ids)

    # ---------------------- Normal channel-scan path ---------------------------
    # Resolve channel IDs for handles/names provided to the downloader
    yt_id_name: Dict[str, str] = {}
    for name in (yt_channels or []):
        ch_id = get_channel_id(
            credentials=credentials,
            api_key=api_key,
            channel_name=name,
            channel_name_to_id=channel_name_to_id
        )
        if ch_id:
            yt_id_name[ch_id] = name
        else:
            LOG.warning("Could not resolve channel id for handle/name: %s", name)

    for cid, cname in yt_id_name.items():
        LOG.info("Collecting videos for channel: %s", cname)
        channel_dir = base_dir / cname
        channel_dir.mkdir(parents=True, exist_ok=True)

        # Gather video info
        video_info_list = get_video_info(credentials, os.environ.get("YOUTUBE_API_KEY"), cid)
        total_candidates += len(video_info_list)

        # Optional CSV filter (only for channel scans; ID-first items were already selected)
        if titles_in_csv is not None:
            filtered = [v for v in video_info_list if str(v.get('title')) in titles_in_csv]
            videos = filtered if filtered else video_info_list
        else:
            videos = video_info_list

        # Plan (quiet) and sort newest→oldest
        videos_to_download = _plan_channel_videos(cname, channel_dir, videos)
        videos_to_download = _sort_desc_by_date(videos_to_download)

        if videos_to_download:
            if cname in plan_per_channel:
                prev_len = len(plan_per_channel[cname])
                merged = _merge_unique_by_id(plan_per_channel[cname], videos_to_download)
                plan_per_channel[cname] = merged
                items_per_channel[cname] = (str(channel_dir), merged)
                total_planned += len(merged) - prev_len
            else:
                plan_per_channel[cname] = videos_to_download
                items_per_channel[cname] = (str(channel_dir), videos_to_download)
                total_planned += len(videos_to_download)

    # Print a concise, actionable summary table
    _print_plan_summary_table(plan_per_channel)

    # Lazily set up cookies right before downloads so planning logs appear immediately.
    if cookie_file is None:
        try:
            from .cookies import setup_cookies
            cookie_file = setup_cookies()
        except Exception as e:
            LOG.warning("Cookie setup failed (%s); proceeding without cookies.", e)
            cookie_file = None

    if not items_per_channel:
        LOG.info(
            "Global planner: %d candidate(s) across %d channel(s) → nothing to download. Exiting.",
            total_candidates, len(yt_id_name),
        )
        return

    LOG.info(
        "Global planner summary: %d candidate(s) → %d to download across %d channel(s).",
        total_candidates, total_planned, len(items_per_channel),
    )

    # ------------------------------
    # PRIORITIZATION STRATEGY
    # ------------------------------
    if prioritize_small_channels:
        ordered_channels = sorted(plan_per_channel.keys(), key=lambda c: len(plan_per_channel[c]))
    else:
        ordered_channels = sorted(plan_per_channel.keys(), key=lambda c: c.lower())

    if group_by_year:
        # Build per-channel year buckets
        per_channel_years: Dict[str, Dict[str, List[Dict]]] = {}
        all_years = set()
        for c in ordered_channels:
            _, vids = items_per_channel[c]
            buckets = _group_by_year(vids)
            per_channel_years[c] = buckets
            all_years.update(buckets.keys())

        years_sorted = sorted([y for y in all_years if y != "unknown"], reverse=True)
        if "unknown" in all_years:
            years_sorted.append("unknown")

        print("Year buckets (most recent first):", ", ".join(years_sorted))

        for y in years_sorted:
            ch_for_year = [c for c in ordered_channels if y in per_channel_years[c] and per_channel_years[c][y]]
            if not ch_for_year:
                continue
            ch_for_year = sorted(ch_for_year, key=lambda c: len(per_channel_years[c][y])) if prioritize_small_channels else ch_for_year

            for group in _chunks(ch_for_year, channels_per_pass):
                grouped_items: List[Tuple[str, str, List[Dict]]] = []
                for c in group:
                    cdir, _ = items_per_channel[c]
                    grouped_items.append((c, cdir, per_channel_years[c][y]))
                LOG.info("Dispatching year %s, channels: %s", y, ", ".join(group))
                await process_global(grouped_items, settings=settings, cookie_file=cookie_file, wave_size=batch_size)
    else:
        for group in _chunks(ordered_channels, channels_per_pass):
            grouped_items: List[Tuple[str, str, List[Dict]]] = []
            for c in group:
                cdir, vids = items_per_channel[c]
                grouped_items.append((c, cdir, vids))
            LOG.info("Dispatching channels: %s", ", ".join(group))
            await process_global(grouped_items, settings=settings, cookie_file=cookie_file, wave_size=batch_size)


# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    # --- make absolute imports work when running this file directly ---
    if __package__ is None:
        here = Path(__file__).resolve()
        project_root = here.parents[4]  # .../youtube-transcript-pipeline/
        sys.path.insert(0, str(project_root))

    # --- absolute imports only (no leading dots) ---
    from src.data_ingestion_youtube.load.download_mp3.logging_setup import start_logging
    from src.data_ingestion_youtube.load.download_mp3.config import Settings
    from src.data_ingestion_youtube.load.download_mp3.cookies import setup_cookies
    from src import root_directory
    from dotenv import load_dotenv

    # logging
    try:
        start_logging("download_mp3s")
    except Exception:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s")

    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument('--api_key', type=str)
    parser.add_argument('--channels', nargs='+', type=str)
    parser.add_argument('--playlists', nargs='+', type=str)
    parser.add_argument('--batch_size', type=int, default=10)

    # NEW prioritization controls
    parser.add_argument('--no-prioritize-small-channels', action='store_true',
                        help='Disable prioritizing channels with the fewest missing items first.')
    parser.add_argument('--group-by-year', action='store_true',
                        help='Process 2025 first, then 2024, etc. Still prioritizes small channels within each year.')
    parser.add_argument('--channels-per-pass', type=int, default=int(os.environ.get("CHANNELS_PER_PASS", "1")),
                        help='How many channels to dispatch together per pass (default 1).')

    args = parser.parse_args()

    # fall back to channels file if none provided
    yt_channels = args.channels
    if not yt_channels:
        handles_file = Path(root_directory()) / "data/links/youtube/youtube_channel_handles.txt"
        if handles_file.exists():
            yt_channels = [c.strip() for c in handles_file.read_text().split(',') if c.strip()]
            LOG.info("Loaded %d channel handle(s) from %s", len(yt_channels), handles_file)
        else:
            LOG.error("No --channels provided and channels file not found; nothing to do.")
            sys.exit(1)

    settings = Settings.from_env() if hasattr(Settings, "from_env") else Settings()

    if args.api_key:
        os.environ["YOUTUBE_API_KEY"] = args.api_key

    cookie_file = None

    # Run cleanup to merge/convert any lingering files before planning
    import subprocess
    from src import YOUTUBE_VIDEO_DIRECTORY as _YVD

    subprocess.run([
        sys.executable, "-m",
        "src.data_ingestion_youtube.load.download_mp3.cleanup",
        "--root", str(Path(_YVD)),
        "--no-dry-run"
    ], check=True)

    asyncio.run(
        run(
            api_key=os.environ.get("YOUTUBE_API_KEY", ""),
            settings=settings,
            yt_channels=yt_channels,
            yt_playlists=args.playlists,
            cookie_file=cookie_file,
            batch_size=args.batch_size,
            prioritize_small_channels=not args.no_prioritize_small_channels,
            group_by_year=args.group_by_year,
            channels_per_pass=max(1, args.channels_per_pass),
        )
    )
