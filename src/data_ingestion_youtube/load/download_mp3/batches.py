import asyncio, logging, random
from typing import Dict, List, Optional, Tuple
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

def _video_sort_key(v: Dict) -> str:
    # Sort on ISO-like prefix YYYY-MM-DDTHH:MM:SS (handles both 'published_date' and 'publishedAt')
    return (v.get('published_date') or v.get('publishedAt') or '')[:19]

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

async def process_global(
    items: List[Tuple[str, str, List[Dict]]],  # [(channel_name, channel_dir, videos), ...]
    settings: Settings,
    cookie_file: Optional[str],
    wave_size: int = 10
):
    """
    Flatten all channel videos, sort newest→oldest globally, and download in waves.
    Each wave is regrouped per channel and dispatched to the existing download_batch,
    which already honors the global semaphore and adaptive tuning.
    """
    # Flatten (channel_name, channel_dir, video_info) triplets
    flat: List[Tuple[str, str, Dict]] = []
    for cname, cdir, vids in items:
        for v in vids:
            flat.append((cname, cdir, v))

    # Global newest → oldest
    flat.sort(key=lambda x: _video_sort_key(x[2]), reverse=True)

    sem = get_global_sem(settings)
    total = len(flat)
    if total == 0:
        logging.info("Global queue is empty. Nothing to do.")
        return

    logging.info(f"Global scheduler: {total} videos across {len(items)} channel(s) "
                 f"(wave_size={wave_size}, global_max={sem._value}).")

    i = 0
    while i < total:
        wave = flat[i:i + wave_size]

        # Group a wave by channel_dir so we can reuse download_batch unchanged
        grouped: Dict[str, List[Dict]] = {}
        for _, cdir, vid in wave:
            grouped.setdefault(cdir, []).append(vid)

        tasks = [
            _download_batch(vs, base_dir=cdir, settings=settings, cookie_file=cookie_file, sem=sem)
            for cdir, vs in grouped.items()
        ]
        statuses_lists = await asyncio.gather(*tasks, return_exceptions=True)

        # Adaptive tuning, same logic as process_channel
        statuses: List[DlStatus] = []
        for sl in statuses_lists:
            if isinstance(sl, list):
                statuses.extend(sl)
            elif isinstance(sl, Exception):
                logging.exception("Global wave batch raised exception", exc_info=sl)

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

        i += wave_size
        if i < total:
            await asyncio.sleep(5 + random.random() * 2.0)
