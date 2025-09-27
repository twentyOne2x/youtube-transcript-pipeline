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
