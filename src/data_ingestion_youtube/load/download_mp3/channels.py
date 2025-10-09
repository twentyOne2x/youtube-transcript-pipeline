import os, unicodedata, logging, asyncio, random
from typing import Dict, List, Optional, Tuple
from .config import Settings, DlStatus
from .downloader import make_ydl_opts, download_one
from src.utils.global_thread_guard import get_global_thread_limiter

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
    prepared: List[Tuple[str, Dict, str]] = []

    for info in video_infos:
        await asyncio.sleep(0.5 + random.random() * 0.75)
        video_id = extract_video_id(info.get('url', ''))
        if not video_id:
            logging.error(f"Could not extract video ID from URL: {info.get('url')}")
            continue
        video_dir_path, base_filename, out_path = build_paths(base_dir, info, settings.audio_format)
        if settings.download_audio and settings.skip_if_exists and os.path.exists(out_path):
            logging.debug(f"Skip (exists): {out_path}")  # was logging.info
            continue
        ydl_opts = make_ydl_opts(video_dir_path, base_filename, cookie_file, settings)
        prepared.append((info['url'], ydl_opts, out_path))

    if not prepared:
        logging.info("Nothing to download in this batch.")
        return []

    from concurrent.futures import ThreadPoolExecutor

    pool_size = min(max(2, settings.global_max_downloads), 4)
    limiter = get_global_thread_limiter()
    token = None
    executor = None

    try:
        token = await asyncio.to_thread(limiter.acquire, pool_size, label="download_mp3")
        executor = ThreadPoolExecutor(max_workers=pool_size)

        tasks = []
        for url, ydl_opts, out_path in prepared:
            if sem is None:
                tasks.append(asyncio.create_task(_guarded_download(loop, executor, url, ydl_opts, out_path, settings)))
            else:
                async def guarded(u=url, o=ydl_opts, p=out_path):
                    async with sem:
                        return await _guarded_download(loop, executor, u, o, p, settings)
                tasks.append(asyncio.create_task(guarded()))

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
        logging.info(
            f"Batch complete: {ok}/{len(statuses)} succeeded "
            f"(rate={statuses.count(DlStatus.RATE)}, sabr={statuses.count(DlStatus.SABR)}, "
            f"no_audio={statuses.count(DlStatus.NOAUDIO)}, err={statuses.count(DlStatus.ERR)})"
        )
        return statuses
    finally:
        if executor:
            executor.shutdown(wait=False)
        if token:
            await asyncio.to_thread(limiter.release, token)
