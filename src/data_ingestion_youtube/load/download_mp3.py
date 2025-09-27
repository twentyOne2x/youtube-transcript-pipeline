#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os

# --- Force UTF-8 early (before other imports) ---
if sys.version_info[0] >= 3:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['LANG'] = 'en_US.UTF-8'

# --- Now the rest of imports ---
import io
import asyncio
import time
import json
import argparse
import random
from typing import List, Optional, Dict, Tuple
from dotenv import load_dotenv
import pandas as pd
import yt_dlp as ydlp
from yt_dlp import DownloadError
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from src import root_directory, YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.utils import get_channel_id, get_video_info
from src.utils.utils import authenticate_service_account


# ---------------------------
# UTF-8 I/O
# ---------------------------
def ensure_utf8_stdio():
    for stream_name in ("stdout", "stderr"):
        s = getattr(sys, stream_name)
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                wrapped = io.TextIOWrapper(getattr(s, "buffer", s), encoding="utf-8", errors="replace")
                setattr(sys, stream_name, wrapped)
        except Exception:
            pass

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")
    os.environ.setdefault("LANG", "en_US.UTF-8")


ensure_utf8_stdio()

# ---------------------------
# Config / env
# ---------------------------
load_dotenv()
api_key = os.environ.get('YOUTUBE_API_KEY')
if not api_key:
    raise ValueError("No API key provided. Please provide an API key via command line argument or .env file.")

DOWNLOAD_AUDIO = os.environ.get('DOWNLOAD_AUDIO', 'True').lower() == 'true'
MAX_PER_CHANNEL = int(os.environ.get('MAX_PER_CHANNEL', '50'))  # soft cap to avoid 2000+ reprocessing at once
SKIP_IF_MP3_EXISTS = os.environ.get('SKIP_IF_MP3_EXISTS', 'True').lower() == 'true'
BROWSER = os.environ.get("BROWSER", "brave")
PROFILE = os.environ.get("PROFILE", "Default")

# NEW: global concurrency limiter across all channels
GLOBAL_MAX_DOWNLOADS = int(os.environ.get("GLOBAL_MAX_DOWNLOADS", "2"))
_SEM: Optional[asyncio.Semaphore] = None  # created lazily when the loop exists


def _get_global_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(GLOBAL_MAX_DOWNLOADS)
        logging.info(f"Global concurrency set to {GLOBAL_MAX_DOWNLOADS} (GLOBAL_MAX_DOWNLOADS).")
    return _SEM


# ---------------------------
# Logging
# ---------------------------
def start_logging(name: str, level=logging.INFO, log_dir="logs"):
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    try:
        sh = logging.StreamHandler(sys.stderr)
        if hasattr(sh.stream, "reconfigure"):
            sh.stream.reconfigure(encoding="utf-8", errors="replace")
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    except Exception:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    fh = logging.FileHandler(os.path.join(log_dir, f"{name}.log"), encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


start_logging("download_mp3s")
try:
    logging.info(f"{sys.executable} yt-dlp {ydlp.version.__version__}")
except Exception:
    pass


# ---------------------------
# Helpers
# ---------------------------
def sanitize_filename(filename: str) -> str:
    replacements = {
        ''': "'", ''': "'", '"': '', '"': '', '"': '',
        '—': '-', '–': '-', '…': '...',
        '：': '-', '|': '-', '\\': '-', '/': '-',
        '<': '', '>': '', '*': '', '?': ''
    }
    for old, new in replacements.items():
        filename = filename.replace(old, new)
    filename = unicodedata.normalize('NFKD', filename)
    filename = ''.join(c for c in filename if ord(c) < 128)
    return filename.strip()


def extract_video_id(url: str) -> Optional[str]:
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    if 'youtu.be/' in url:
        return url.split('youtu.be/')[1].split('?')[0]
    if 'youtube.com/watch/' in url:
        return url.split('watch/')[1].split('?')[0]
    return None


def netscape_cookiefile_looks_ok(path: str) -> bool:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
        if not lines or not lines[0].startswith('# Netscape HTTP Cookie File'):
            return False
        for ln in lines[1:]:
            if not ln or ln.startswith('#'):
                continue
            parts = ln.split('\t')
            if len(parts) != 7:
                return False
            exp = int(parts[4])
            if not (exp == 0 or (1_000_000_000 <= exp < 4_102_444_800)):
                return False
            return True
        return False
    except Exception:
        return False


def setup_cookies() -> Optional[str]:
    logging.info("Setting up YouTube cookies...")
    candidates = [
        os.path.join(root_directory(), 'src/data_ingestion_youtube/load/youtube_cookies.txt'),
        'src/data_ingestion_youtube/load/youtube_cookies.txt',
        os.path.join(root_directory(), 'youtube_cookies.txt'),
        'youtube_cookies.txt',
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 0 and netscape_cookiefile_looks_ok(p):
            abspath = os.path.abspath(p)
            logging.info(f"✓ Using cookies file: {abspath}")
            return abspath
        elif os.path.exists(p):
            logging.warning(f"Cookie file exists but is invalid: {os.path.abspath(p)}")
    logging.warning("No valid cookies file found; will try reading cookies from Brave at runtime.")
    return None


def build_paths(base_dir: str, info: Dict) -> Tuple[str, str, str]:
    """
    Returns (video_dir_path, base_filename, mp3_path)
    """
    video_id = extract_video_id(info['url']) or "unknown"
    title = sanitize_filename(info['title']).replace('/', '_')
    published_at = (info.get('published_date') or info.get('publishedAt') or '')[:10]  # yyyy-mm-dd
    base_filename = f"{published_at}_{video_id}_{title}" if published_at else f"{video_id}_{title}"

    video_dir_path = os.path.join(base_dir, base_filename)
    os.makedirs(video_dir_path, exist_ok=True)

    mp3_path = os.path.join(video_dir_path, f"{base_filename}.mp3")
    return video_dir_path, base_filename, mp3_path


# ---------------------------
# yt-dlp opts + client rotation
# ---------------------------
def make_ydl_opts(video_dir_path: str, base_filename: str, cookie_file: Optional[str]) -> dict:
    # Prefer m4a first to avoid SABR'd webm/HLS; then webm, then anything
    base_format = "140/bestaudio[ext=m4a]/251/bestaudio/best" if DOWNLOAD_AUDIO else "bestvideo*+bestaudio/best"

    return {
        "format": base_format,
        "postprocessors": ([{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }] if DOWNLOAD_AUDIO else []),
        "outtmpl": f"{video_dir_path}/{base_filename}.%(ext)s",

        # stability
        "sleep_interval_requests": 1.5,
        "max_sleep_interval_requests": 3.0,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragments": 1,
        "noplaylist": True,
        "progress_with_newline": True,

        # stash for switching logic
        "_fallback_cookie_file": cookie_file if cookie_file else None,
        "_browser": BROWSER,
        "_profile": PROFILE,
    }


def download_one(url: str, ydl_opts: dict, target_mp3: Optional[str], retries: int = 4) -> bool:
    def set_client(opts, client: str):
        y = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
        # Use web_safari variant when "web"
        if client == "web":
            y["player_client"] = ["web_safari"]
        else:
            y["player_client"] = [client]
        # cookies only for web
        if client != "web":
            opts.pop("cookiefile", None)
            opts.pop("cookiesfrombrowser", None)

    def use_web_with_cookies(opts, force_browser: bool = False):
        cookie_file = None if force_browser else opts.get("_fallback_cookie_file")
        set_client(opts, "web")
        if cookie_file:
            opts["cookiefile"] = cookie_file
            opts.pop("cookiesfrombrowser", None)
        else:
            opts["cookiesfrombrowser"] = (
                opts.get("_browser", "brave"),
                opts.get("_profile", "Default"),
                None,
                True
            )
            opts.pop("cookiefile", None)

    # NEW: Start with Android (then iOS → TV → Web)
    rotation = ["android", "ios", "tv", "web"]
    rot_idx = 0
    set_client(ydl_opts, rotation[rot_idx])

    for attempt in range(1, retries + 1):
        try:
            with ydlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if DOWNLOAD_AUDIO and target_mp3 and os.path.exists(target_mp3):
                    logging.info(f"Already exists: {target_mp3}")
                    return True
                ydl.download([url])
                return True

        except DownloadError as e:
            msg = str(e)

            # SABR / 403 / missing url / nsig -> rotate among non-web first, then web
            if ("Some web client https formats have been skipped" in msg) or \
               ("missing a url" in msg) or ("HTTP Error 403" in msg) or \
               ("Only images are available" in msg) or ("nsig extraction failed" in msg) or \
               ("m3u8" in msg and "403" in msg):
                if rotation[rot_idx] != "web":
                    prev = rotation[rot_idx]
                    rot_idx = (rot_idx + 1) % len(rotation)
                    nxt = rotation[rot_idx]
                    logging.warning(f"{prev} failed (SABR/403); trying {nxt}...")
                    if nxt == "web":
                        use_web_with_cookies(ydl_opts, force_browser=True)
                    else:
                        set_client(ydl_opts, nxt)
                    continue

            # Auth/rate limit -> go web with fresh browser cookies
            if ("Sign in to confirm you’re not a bot" in msg) or ("HTTP Error 429" in msg):
                wait_time = 45 if attempt == 1 else min(90, 15 * attempt)
                logging.warning(f"Auth/429; waiting {wait_time}s then using web+cookies...")
                time.sleep(wait_time)
                use_web_with_cookies(ydl_opts, force_browser=True)
                rot_idx = rotation.index("web")
                continue

            # Bad/empty cookies.txt on web
            if ("does not look like a Netscape format cookies file" in msg) or ("cookies file is empty" in msg):
                logging.warning("Invalid cookiefile; switching to cookies-from-browser (web).")
                ydl_opts["_fallback_cookie_file"] = None
                use_web_with_cookies(ydl_opts, force_browser=True)
                rot_idx = rotation.index("web")
                continue

            # generic retry
            wait_time = 5 * attempt
            logging.warning(f"Attempt {attempt} failed: {msg}. Retrying in {wait_time}s...")
            time.sleep(wait_time)

    logging.error("Exhausted retries.")
    return False


# ---------------------------
# Batch downloading
# ---------------------------
async def _guarded_download(loop, executor, url, ydl_opts, mp3_path):
    # NEW: global semaphore to cap total parallel downloads across all channels
    async with _get_global_sem():
        return await loop.run_in_executor(executor, download_one, url, ydl_opts, mp3_path, 4)


async def download_batch(video_infos: List[Dict], base_dir: str, cookie_file: Optional[str] = None):
    """
    Low concurrency + jitter; optional skip-if-exists; pure batching (no deep FS scan).
    """
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=2)  # gentle on YouTube anti-bot
    tasks = []

    for info in video_infos:
        await asyncio.sleep(0.5 + random.random() * 0.75)  # tiny jitter

        video_id = extract_video_id(info.get('url', ''))
        if not video_id:
            logging.error(f"Could not extract video ID from URL: {info.get('url')}")
            continue

        video_dir_path, base_filename, mp3_path = build_paths(base_dir, info)
        if DOWNLOAD_AUDIO and SKIP_IF_MP3_EXISTS and os.path.exists(mp3_path):
            logging.info(f"Skip (exists): {mp3_path}")
            continue

        ydl_opts = make_ydl_opts(video_dir_path, base_filename, cookie_file)

        # NEW: use guarded task (global semaphore) instead of launching all at once
        tasks.append(asyncio.create_task(_guarded_download(loop, executor, info['url'], ydl_opts, mp3_path)))

    if not tasks:
        logging.info("Nothing to download in this batch.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = sum(1 for r in results if r is True)
    logging.info(f"Batch complete: {success_count}/{len(results)} succeeded")


def pick_recent_subset(video_info_list: List[Dict], limit: int) -> List[Dict]:
    """
    Return at most `limit` newest-looking entries.
    Assumes items have 'published_date' or 'publishedAt' in ISO-ish form.
    If not, just take the last `limit` items as-is.
    """
    def key_dt(v):
        date_str = (v.get('published_date') or v.get('publishedAt') or '')[:19]
        return date_str

    try:
        sorted_list = sorted(video_info_list, key=key_dt, reverse=True)
        return sorted_list[:max(0, limit)]
    except Exception:
        return video_info_list[-limit:]


async def process_channel(channel_name: str, video_info_list: List[Dict],
                          channel_dir: str, batch_size: int, cookie_file: Optional[str]):
    # Keep only newest MAX_PER_CHANNEL to avoid scanning thousands at once
    to_process = pick_recent_subset(video_info_list, MAX_PER_CHANNEL)
    total = len(to_process)
    if total == 0:
        logging.info(f"[{channel_name}] No videos to process.")
        return

    logging.info(f"[{channel_name}] Downloading up to {total} videos (cap={MAX_PER_CHANNEL}, batch_size={batch_size})")

    for i in range(0, total, batch_size):
        batch = to_process[i:i + batch_size]
        logging.info(f"[{channel_name}] Processing batch {i // batch_size + 1} ({len(batch)} items)")
        await download_batch(batch, channel_dir, cookie_file)
        if i + batch_size < total:
            await asyncio.sleep(5 + random.random() * 2.0)


# ---------------------------
# Orchestration
# ---------------------------
async def process_channel_async(channel_id: str, channel_name: str,
                                credentials, youtube_videos_df: pd.DataFrame,
                                cookie_file: Optional[str] = None,
                                batch_size: int = 10):
    logging.info(f"Processing channel: {channel_name}")
    base_dir = YOUTUBE_VIDEO_DIRECTORY
    os.makedirs(base_dir, exist_ok=True)
    channel_dir = os.path.join(base_dir, channel_name)
    os.makedirs(channel_dir, exist_ok=True)

    # Pull fresh list of videos for the channel
    video_info_list = get_video_info(credentials, api_key, channel_id)

    # Optionally intersect with your CSV (if that's your intended source of truth)
    titles_in_csv = set(youtube_videos_df['title'].str.replace(' +', ' ', regex=True).str.replace('"', '', regex=False))
    filtered = [v for v in video_info_list if v.get('title') in titles_in_csv]

    videos = filtered if filtered else video_info_list
    await process_channel(channel_name, videos, channel_dir, batch_size, cookie_file)


async def run(api_key: str,
              yt_channels: Optional[List[str]] = None,
              yt_playlists: Optional[List[str]] = None,
              cookie_file: Optional[str] = None,
              batch_size: int = 10):
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = authenticate_service_account(service_account_file) if service_account_file else None
    if credentials:
        logging.info("Service account file found. Proceeding with public channels, playlists, or private videos if accessible via Google Service Account.")
    else:
        logging.info("No service account file found. Proceeding with public channels or playlists.")

    # Load channel handle -> id cache if present
    mapping_path = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"
    channel_name_to_id = {}
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8') as f:
            channel_name_to_id = json.load(f)

    # Resolve ids
    yt_id_name = {
        get_channel_id(credentials=credentials, api_key=api_key, channel_name=name,
                       channel_name_to_id=channel_name_to_id): name
        for name in (yt_channels or [])
    }

    videos_csv = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
    youtube_videos_df = pd.read_csv(videos_csv)

    await asyncio.gather(
        *(process_channel_async(cid, cname, credentials, youtube_videos_df, cookie_file, batch_size)
          for cid, cname in yt_id_name.items())
    )


# ---------------------------
# CLI
# ---------------------------
def get_youtube_channels_from_file(file_path: str) -> List[str]:
    with open(file_path, 'r') as f:
        return [c.strip() for c in f.read().split(',') if c.strip()]


def main():
    parser = argparse.ArgumentParser(description='Batch download YouTube audio via yt-dlp with cookie/client rotation.')
    parser.add_argument('--api_key', type=str, help='YouTube Data API key (overrides .env)')
    parser.add_argument('--channels', nargs='+', type=str, help='YouTube channel names or IDs')
    parser.add_argument('--playlists', nargs='+', type=str, help='YouTube playlist IDs (unused here)')
    parser.add_argument('--batch_size', type=int, default=int(os.environ.get('BATCH_SIZE', '10')),
                        help='Items per batch per channel')
    args = parser.parse_args()

    # cookie source
    try:
        cookie_file = setup_cookies()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    # channels source
    yt_channels = args.channels
    if not yt_channels:
        handles_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
        if os.path.exists(handles_file):
            yt_channels = get_youtube_channels_from_file(handles_file)

    yt_playlists = args.playlists or os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists and isinstance(yt_playlists, str):
        yt_playlists = [p.strip() for p in yt_playlists.split(',') if p.strip()]

    if not yt_channels and not yt_playlists:
        raise ValueError("No channels or playlists provided.")

    # prefer CLI api_key if provided
    key = args.api_key or api_key

    asyncio.run(run(key, yt_channels, yt_playlists, cookie_file, batch_size=args.batch_size))


if __name__ == '__main__':
    main()
