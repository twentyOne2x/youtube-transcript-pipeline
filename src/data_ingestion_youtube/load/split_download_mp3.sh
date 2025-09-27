#!/usr/bin/env bash
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$PKG_DIR/tests"
touch "$PKG_DIR/__init__.py"

# -----------------------------
# config.py
# -----------------------------
cat > "$PKG_DIR/config.py" <<'PY'
from dataclasses import dataclass
from enum import Enum
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

    global_max_downloads: int = int(os.environ.get("GLOBAL_MAX_DOWNLOADS", "5"))

    debug_list_formats: bool = os.environ.get("DEBUG_LIST_FORMATS", "false").lower() == "true"

    use_browser_cookies: bool = os.environ.get("USE_BROWSER_COOKIES", "true").lower() == "true"
PY

# -----------------------------
# logging_setup.py
# -----------------------------
cat > "$PKG_DIR/logging_setup.py" <<'PY'
import os, sys, logging

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
PY

# -----------------------------
# cookies.py
# -----------------------------
cat > "$PKG_DIR/cookies.py" <<'PY'
import os, logging
from src import root_directory

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

def setup_cookies() -> str | None:
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
PY

# -----------------------------
# clients.py
# -----------------------------
cat > "$PKG_DIR/clients.py" <<'PY'
from typing import List
def set_client(opts: dict, client: str):
    y = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
    if client == "web":
        y["player_client"] = ["web_safari"]
    elif client in ("web_safari", "web_creator", "web_embedded", "android", "ios", "tv"):
        y["player_client"] = [client]
    else:
        y["player_client"] = ["web_safari"]

    have_cookiefile = bool(opts.get("_fallback_cookie_file"))
    have_browser_cookies = "_browser" in opts and "_profile" in opts
    using_cookies = have_cookiefile or have_browser_cookies

    if using_cookies and y["player_client"][0].startswith("web"):
        if have_cookiefile:
            opts["cookiefile"] = opts["_fallback_cookie_file"]
            opts.pop("cookiesfrombrowser", None)
        else:
            opts["cookiesfrombrowser"] = (
                opts.get("_browser", "brave"),
                opts.get("_profile", "Default"),
                None,
                True
            )
            opts.pop("cookiefile", None)
    else:
        opts.pop("cookiefile", None)
        opts.pop("cookiesfrombrowser", None)

class ClientRotator:
    def __init__(self, using_cookies: bool):
        self.order: List[str] = (["web_safari", "web_creator", "web_embedded", "android", "ios", "tv"]
                                 if using_cookies else ["android", "ios", "tv", "web_safari"])
        self.idx = 0

    def current(self) -> str:
        return self.order[self.idx]

    def next(self) -> str:
        self.idx = (self.idx + 1) % len(self.order)
        return self.current()

    def force_web_with_cookies(self):
        # reset to first web client
        if "web_safari" in self.order:
            self.idx = self.order.index("web_safari")
        else:
            self.idx = 0
PY

# -----------------------------
# formats.py
# -----------------------------
cat > "$PKG_DIR/formats.py" <<'PY'
from typing import Dict, List
import logging
from .config import Settings

def debug_log_formats(info: Dict, url: str, settings: Settings):
    if not settings.debug_list_formats:
        return
    fmts = info.get('formats') or []
    logging.info(f"--- Available formats for {url} ({len(fmts)}) ---")
    for f in fmts:
        logging.info(
            "id=%s ext=%s vcodec=%s acodec=%s abr=%s tbr=%s asr=%s proto=%s has_url=%s note=%s",
            f.get('format_id'), f.get('ext'), f.get('vcodec'), f.get('acodec'), f.get('abr'),
            f.get('tbr'), f.get('asr'), f.get('protocol'), bool(f.get('url')), f.get('format_note')
        )

def ranked_audio_format_ids(info: Dict, settings: Settings) -> List[str]:
    fmts = info.get('formats') or []

    def is_audio_only(f):
        return (f.get('vcodec') in (None, 'none')) and (f.get('acodec') not in (None, 'none'))

    def score(f):
        ext = (f.get('ext') or '').lower()
        ac = (f.get('acodec') or '').lower()
        proto = (f.get('protocol') or '')
        abr = int(f.get('abr') or 0)
        s = 0
        if f.get('url'): s += 1000
        if 'm3u8' not in proto: s += 300

        af = settings.audio_format
        if af == 'm4a':
            if ext == 'm4a' or 'mp4a' in ac: s += 150
        elif af == 'opus':
            if ext in ('webm',) or 'opus' in ac: s += 150
        elif af == 'mp3':
            if ext == 'm4a' or 'mp4a' in ac: s += 150
            elif 'opus' in ac: s += 120

        s += abr
        return s

    candidates = [f for f in fmts if is_audio_only(f)]
    candidates.sort(key=score, reverse=True)
    return [f.get('format_id') for f in candidates if f.get('format_id')]
PY

# -----------------------------
# downloader.py
# -----------------------------
cat > "$PKG_DIR/downloader.py" <<'PY'
import os, time, logging
from typing import Optional
import yt_dlp as ydlp
from yt_dlp import DownloadError
from .config import Settings, DlStatus
from .clients import set_client, ClientRotator
from .formats import ranked_audio_format_ids, debug_log_formats

def make_ydl_opts(video_dir_path: str, base_filename: str, cookie_file: Optional[str], settings: Settings) -> dict:
    audio_pref = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/140/251/bestaudio"
    ydl_opts = {
        "format": audio_pref,
        "outtmpl": f"{video_dir_path}/{base_filename}.%(ext)s",
        "noplaylist": True,
        "progress_with_newline": True,
        "quiet": False,
        "no_warnings": False,

        "sleep_interval_requests": 1.5,
        "max_sleep_interval_requests": 3.0,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragments": 1,

        "_fallback_cookie_file": cookie_file if cookie_file else None,
        "_browser": settings.browser,
        "_profile": settings.profile,

        "continuedl": True,
        "nopart": True,
        "restrictfilenames": True,
    }
    if settings.audio_format == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        ydl_opts["merge_output_format"] = "mp3"
    return ydl_opts

def download_one(url: str, ydl_opts: dict, target_path: Optional[str], settings: Settings, retries: int = 4) -> str:
    using_cookies = bool(ydl_opts.get("_fallback_cookie_file")) or ("_browser" in ydl_opts and "_profile" in ydl_opts)
    rot = ClientRotator(using_cookies)
    set_client(ydl_opts, rot.current())

    for attempt in range(1, retries + 1):
        try:
            with ydlp.YoutubeDL({**ydl_opts, "format": "bestaudio/best"}) as ydl:
                info = ydl.extract_info(url, download=False)
            debug_log_formats(info, url, settings)

            if settings.download_audio and target_path and os.path.exists(target_path):
                logging.info(f"Already exists: {target_path}")
                return DlStatus.OK

            tried_any = False
            for fmt_id in ranked_audio_format_ids(info, settings):
                tried_any = True
                try:
                    with ydlp.YoutubeDL({**ydl_opts, "format": fmt_id}) as ydl:
                        ydl.download([url])
                    return DlStatus.OK
                except DownloadError as fe:
                    logging.warning(f"Format {fmt_id} failed: {fe}. Trying next candidate…")
                    continue

            if attempt < retries:
                prev = rot.current()
                nxt = rot.next()
                logging.warning(f"{'No viable audio formats' if tried_any else 'No audio-only formats'} for client={prev}. Rotating → {nxt}")
                set_client(ydl_opts, nxt)
                continue

            fallback = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/140/251/bestaudio"
            with ydlp.YoutubeDL({**ydl_opts, "format": fallback}) as ydl:
                ydl.download([url])
            return DlStatus.OK

        except DownloadError as e:
            s = str(e)
            if ("Sign in to confirm" in s or "HTTP Error 429" in s) and using_cookies:
                wait = 30 if attempt == 1 else min(90, 20 * attempt)
                logging.warning(f"{s} — sleeping {wait}s; forcing web client with cookies and retrying")
                time.sleep(wait)
                rot.force_web_with_cookies()
                set_client(ydl_opts, rot.current())
                if attempt < retries:
                    continue

            if any(k in s for k in ("Requested format is not available","Only images are available","SABR","missing a url","nsig")) or ("m3u8" in s and "403" in s):
                if attempt < retries:
                    prev = rot.current()
                    nxt = rot.next()
                    logging.warning(f"{s} — rotating client {prev} → {nxt}")
                    set_client(ydl_opts, nxt)
                    continue
                if "Only images" in s or "SABR" in s:
                    return DlStatus.SABR
                if "Requested format is not available" in s:
                    return DlStatus.NOAUDIO
                return DlStatus.ERR

            logging.warning(f"Attempt {attempt} failed: {s}. Backing off…")
            time.sleep(5 * attempt)
            if attempt < retries:
                prev = rot.current()
                nxt = rot.next()
                logging.info(f"Switching client {prev} → {nxt}")
                set_client(ydl_opts, nxt)
                continue

            if "HTTP Error 429" in s:
                return DlStatus.RATE
            return DlStatus.ERR

        except Exception as e:
            logging.exception(f"Unexpected error on attempt {attempt}: {e}")
            time.sleep(5 * attempt)
            if attempt < retries:
                prev = rot.current()
                nxt = rot.next()
                logging.info(f"Switching client {prev} → {nxt}")
                set_client(ydl_opts, nxt)
                continue
            return DlStatus.ERR

    return DlStatus.ERR
PY

# -----------------------------
# channels.py
# -----------------------------
cat > "$PKG_DIR/channels.py" <<'PY'
import os, unicodedata, logging, asyncio, random
from typing import Dict, List, Optional, Tuple
from .config import Settings, DlStatus
from .downloader import make_ydl_opts, download_one

def sanitize_filename(filename: str) -> str:
    replacements = {
        "'": "'", '"': '', '—': '-', '–': '-', '…': '...',
        '：': '-', '|': '-', '\\': '-', '/': '-', '<': '', '>': '', '*': '', '?': ''
    }
    for old, new in replacements.items():
        filename = filename.replace(old, new)
    filename = unicodedata.normalize('NFKD', filename)
    filename = ''.join(c for c in filename if ord(c) < 128)
    return filename.strip()

def extract_video_id(url: str) -> str | None:
    if 'v=' in url:
        return url.split('v=')[1].split('&')[0]
    if 'youtu.be/' in url:
        return url.split('youtu.be/')[1].split('?')[0]
    if 'youtube.com/watch/' in url:
        return url.split('watch/')[1].split('?')[0]
    return None

def build_paths(base_dir: str, info: Dict, audio_format: str) -> Tuple[str, str, str]:
    video_id = extract_video_id(info['url']) or "unknown"
    title = sanitize_filename(info['title']).replace('/', '_')
    published_at = (info.get('published_date') or info.get('publishedAt') or '')[:10]
    base_filename = f"{published_at}_{video_id}_{title}" if published_at else f"{video_id}_{title}"

    video_dir_path = os.path.join(base_dir, base_filename)
    os.makedirs(video_dir_path, exist_ok=True)

    if audio_format == 'mp3':
        target_ext = 'mp3'
    elif audio_format == 'opus':
        target_ext = 'webm'
    else:
        target_ext = 'm4a'
    out_path = os.path.join(video_dir_path, f"{base_filename}.{target_ext}")
    return video_dir_path, base_filename, out_path

def pick_recent_subset(video_info_list: List[Dict], limit: int) -> List[Dict]:
    def key_dt(v):
        date_str = (v.get('published_date') or v.get('publishedAt') or '')[:19]
        return date_str
    try:
        sorted_list = sorted(video_info_list, key=key_dt, reverse=True)
        return sorted_list[:max(0, limit)]
    except Exception:
        return video_info_list[-limit:]

async def _guarded_download(loop, executor, url, ydl_opts, out_path, settings: Settings):
    return await loop.run_in_executor(executor, download_one, url, ydl_opts, out_path, settings, 4)

async def download_batch(video_infos: List[Dict], base_dir: str, settings: Settings, cookie_file: Optional[str] = None,
                         sem: Optional[asyncio.Semaphore] = None):
    loop = asyncio.get_event_loop()
    executor = None
    try:
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=min(max(2, settings.global_max_downloads), 4))
        tasks = []

        for info in video_infos:
            await asyncio.sleep(0.5 + random.random() * 0.75)
            video_id = extract_video_id(info.get('url', ''))
            if not video_id:
                logging.error(f"Could not extract video ID from URL: {info.get('url')}")
                continue
            video_dir_path, base_filename, out_path = build_paths(base_dir, info, settings.audio_format)
            if settings.download_audio and settings.skip_if_exists and os.path.exists(out_path):
                logging.info(f"Skip (exists): {out_path}")
                continue
            ydl_opts = make_ydl_opts(video_dir_path, base_filename, cookie_file, settings)
            if sem is None:
                tasks.append(asyncio.create_task(_guarded_download(loop, executor, info['url'], ydl_opts, out_path, settings)))
            else:
                async def guarded(u=info['url'], o=ydl_opts, p=out_path):
                    async with sem:
                        return await _guarded_download(loop, executor, u, o, p, settings)
                tasks.append(asyncio.create_task(guarded()))

        if not tasks:
            logging.info("Nothing to download in this batch.")
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        statuses: list[DlStatus] = []
        for r in results:
            if isinstance(r, Exception):
                logging.exception("Task raised unhandled exception", exc_info=r)
                statuses.append(DlStatus.ERR)
            elif isinstance(r, DlStatus):
                statuses.append(r)
            elif isinstance(r, str):
                try:
                    statuses.append(DlStatus(r))
                except Exception:
                    statuses.append(DlStatus.ERR)
            else:
                statuses.append(DlStatus.ERR)

        ok = sum(1 for s in statuses if s == DlStatus.OK)
        logging.info(f"Batch complete: {ok}/{len(statuses)} succeeded "
                     f"(rate={statuses.count(DlStatus.RATE)}, sabr={statuses.count(DlStatus.SABR)}, "
                     f"no_audio={statuses.count(DlStatus.NOAUDIO)}, err={statuses.count(DlStatus.ERR)})")
        return statuses
    finally:
        if executor:
            executor.shutdown(wait=False)
PY

# -----------------------------
# batches.py
# -----------------------------
cat > "$PKG_DIR/batches.py" <<'PY'
import asyncio, logging, random
from typing import Dict, List, Optional
from .config import Settings, DlStatus
from .channels import pick_recent_subset, download_batch as _download_batch

_GLOBAL_SEM: asyncio.Semaphore | None = None

def get_global_sem(settings: Settings) -> asyncio.Semaphore:
    global _GLOBAL_SEM
    if _GLOBAL_SEM is None:
        _GLOBAL_SEM = asyncio.Semaphore(settings.global_max_downloads)
        logging.info(f"Global concurrency set to {settings.global_max_downloads} (GLOBAL_MAX_DOWNLOADS).")
    return _GLOBAL_SEM

def rebuild_global_sem(settings: Settings, new_limit: int):
    global _GLOBAL_SEM
    new_limit = max(settings.adaptive_min_cap, min(settings.adaptive_max_cap, int(new_limit)))
    if _GLOBAL_SEM is None or _GLOBAL_SEM._value != new_limit:  # type: ignore
        _GLOBAL_SEM = asyncio.Semaphore(new_limit)
        logging.info(f"Adjusted global concurrency → {new_limit}")

async def process_channel(channel_name: str, video_info_list: List[Dict],
                          channel_dir: str, batch_size: int,
                          settings: Settings, cookie_file: Optional[str]):
    to_process = pick_recent_subset(video_info_list, settings.max_per_channel)
    total = len(to_process)
    if total == 0:
        logging.info(f"[{channel_name}] No videos to process.")
        return

    logging.info(f"[{channel_name}] Downloading up to {total} videos (cap={settings.max_per_channel}, batch_size={batch_size})")
    sem = get_global_sem(settings)

    for i in range(0, total, batch_size):
        batch = to_process[i:i + batch_size]
        logging.info(f"[{channel_name}] Processing batch {i // batch_size + 1} ({len(batch)} items)")
        statuses = await _download_batch(batch, channel_dir, settings, cookie_file, sem)

        if statuses:
            ok = statuses.count(DlStatus.OK)
            rate = statuses.count(DlStatus.RATE)
            sabr = statuses.count(DlStatus.SABR) + statuses.count(DlStatus.NOAUDIO)
            err = statuses.count(DlStatus.ERR)
            n = len(statuses)

            if rate > 0 or err > n * 0.25 or sabr > n * 0.25:
                rebuild_global_sem(settings, max(sem._value // 2, settings.adaptive_min_cap))  # type: ignore
            elif ok == n:
                rebuild_global_sem(settings, sem._value + 1)  # type: ignore

        if i + batch_size < total:
            await asyncio.sleep(5 + random.random() * 2.0)
PY

# -----------------------------
# run.py
# -----------------------------
cat > "$PKG_DIR/run.py" <<'PY'
import os, json, asyncio, logging, pandas as pd
from typing import List, Optional
from dotenv import load_dotenv
from src import root_directory, YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.utils import get_channel_id, get_video_info
from src.utils.utils import authenticate_service_account
from .config import Settings
from .batches import process_channel

async def process_channel_async(channel_id: str, channel_name: str,
                                credentials, youtube_videos_df: pd.DataFrame,
                                settings: Settings, cookie_file: Optional[str],
                                batch_size: int = 10):
    logging.info(f"Processing channel: {channel_name}")
    base_dir = YOUTUBE_VIDEO_DIRECTORY
    os.makedirs(base_dir, exist_ok=True)
    channel_dir = os.path.join(base_dir, channel_name)
    os.makedirs(channel_dir, exist_ok=True)

    video_info_list = get_video_info(credentials, os.environ.get("YOUTUBE_API_KEY"), channel_id)

    titles_in_csv = set(youtube_videos_df['title'].str.replace(' +', ' ', regex=True).str.replace('"', '', regex=False))
    filtered = [v for v in video_info_list if v.get('title') in titles_in_csv]
    videos = filtered if filtered else video_info_list

    await process_channel(channel_name, videos, channel_dir, batch_size, settings, cookie_file)

async def run(api_key: str,
              settings: Settings,
              yt_channels: Optional[List[str]] = None,
              yt_playlists: Optional[List[str]] = None,
              cookie_file: Optional[str] = None,
              batch_size: int = 10):
    load_dotenv()
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = authenticate_service_account(service_account_file) if service_account_file else None
    if credentials:
        logging.info("Service account file found. Proceeding with public channels, playlists, or private videos if accessible via Google Service Account.")
    else:
        logging.info("No service account file found. Proceeding with public channels or playlists.")

    mapping_path = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"
    channel_name_to_id = {}
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8') as f:
            channel_name_to_id = json.load(f)

    yt_id_name = {
        get_channel_id(credentials=credentials, api_key=api_key, channel_name=name,
                       channel_name_to_id=channel_name_to_id): name
        for name in (yt_channels or [])
    }

    videos_csv = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
    youtube_videos_df = pd.read_csv(videos_csv)

    await asyncio.gather(
        *(process_channel_async(cid, cname, credentials, youtube_videos_df, settings, cookie_file, batch_size)
          for cid, cname in yt_id_name.items())
    )
PY

# -----------------------------
# cli.py
# -----------------------------
cat > "$PKG_DIR/cli.py" <<'PY'
import sys, os, io, argparse, asyncio, logging
from dotenv import load_dotenv
from .config import Settings
from .cookies import setup_cookies
from .logging_setup import start_logging
from .run import run
from src import root_directory

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

def main():
    ensure_utf8_stdio()
    load_dotenv()
    start_logging("download_mp3s")

    try:
        import yt_dlp as ydlp
        logging.info(f"{sys.executable} yt-dlp {ydlp.version.__version__}")
    except Exception:
        pass

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        raise ValueError("No API key provided. Please provide an API key via command line argument or .env file.")

    parser = argparse.ArgumentParser(description='Batch download YouTube audio via yt-dlp with cookie/client rotation.')
    parser.add_argument('--api_key', type=str, help='YouTube Data API key (overrides .env)')
    parser.add_argument('--channels', nargs='+', type=str, help='YouTube channel names or IDs')
    parser.add_argument('--playlists', nargs='+', type=str, help='YouTube playlist IDs (unused here)')
    parser.add_argument('--batch_size', type=int, default=int(os.environ.get('BATCH_SIZE', '10')),
                        help='Items per batch per channel')
    args = parser.parse_args()

    try:
        cookie_file = setup_cookies()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    yt_channels = args.channels
    if not yt_channels:
        handles_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
        if os.path.exists(handles_file):
            with open(handles_file, 'r') as f:
                yt_channels = [c.strip() for c in f.read().split(',') if c.strip()]

    yt_playlists = args.playlists or os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists and isinstance(yt_playlists, str):
        yt_playlists = [p.strip() for p in yt_playlists.split(',') if p.strip()]

    if not yt_channels and not yt_playlists:
        raise ValueError("No channels or playlists provided.")

    key = args.api_key or api_key
    settings = Settings()
    asyncio.run(run(key, settings, yt_channels, yt_playlists, cookie_file, batch_size=args.batch_size))

if __name__ == '__main__':
    main()
PY

# -----------------------------
# rewrite download_mp3.py -> thin shim
# -----------------------------
cat > "$PKG_DIR/download_mp3.py" <<'PY'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from .cli import main

if __name__ == '__main__':
    main()
PY

echo "✅ Split complete. Files created in $PKG_DIR"
