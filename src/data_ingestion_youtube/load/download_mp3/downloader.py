import os, time, logging
from pathlib import Path
from typing import Optional

try:
    import yt_dlp as ydlp  # type: ignore
    from yt_dlp import DownloadError  # type: ignore
except ImportError:  # pragma: no cover - optional during unit tests
    ydlp = None

    class DownloadError(Exception):
        """Fallback DownloadError when yt-dlp is unavailable."""

        pass
from .config import Settings, DlStatus
from .clients import set_client, ClientRotator
from .formats import ranked_audio_format_ids, debug_log_formats
from src import YOUTUBE_VIDEO_DIRECTORY
from src.utils.gcs import maybe_upload

def _yt_base_path() -> Path:
    override = os.environ.get("YOUTUBE_VIDEO_DIRECTORY")
    if override:
        return Path(override).resolve()
    return Path(YOUTUBE_VIDEO_DIRECTORY).resolve()

def _relative_key(path: Path) -> str:
    base = _yt_base_path()
    resolved = path.resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return resolved.name

def _post_download(target_path: Optional[str], settings: Settings) -> None:
    if not target_path:
        return
    path = Path(target_path)
    if not path.exists():
        return
    gcs_uri = maybe_upload(
        path,
        bucket=settings.gcs_bucket,
        prefix=settings.gcs_prefix,
        relative_key=_relative_key(path),
    )
    if gcs_uri:
        logging.info("Uploaded to GCS: %s", gcs_uri)
        if settings.gcs_bucket and not settings.keep_local_files:
            try:
                path.unlink()
                logging.debug("Removed local copy after upload: %s", path)
            except OSError as exc:
                logging.warning("Failed to remove local file %s: %s", path, exc)

def make_ydl_opts(video_dir_path: str, base_filename: str, cookie_file: Optional[str], settings: Settings) -> dict:
    audio_pref = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/140/251/bestaudio"
    ydl_opts = {
        "format": audio_pref,
        "outtmpl": f"{video_dir_path}/{base_filename}.%(ext)s",
        "noplaylist": True,
        "progress_with_newline": True,

        # Keep logs readable; yt-dlp will still print warnings/errors
        "quiet": True,
        "no_warnings": False,

        # Be gentle on network
        "sleep_interval_requests": 1.5,
        "max_sleep_interval_requests": 3.0,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragments": 1,

        # ✅ Key changes to kill 416s and stale resumes
        "continuedl": False,          # never resume — start clean
        "overwrites": True,           # overwrite any partial from previous attempt
        "nopart": True,               # write directly to final file

        # Cookies
        "_fallback_cookie_file": cookie_file if cookie_file else None,

        "restrictfilenames": True,
        "prefer_ffmpeg": True,
    }

    if settings.use_browser_cookies:
        ydl_opts["_browser"] = settings.browser
        ydl_opts["_profile"] = settings.profile
    else:
        ydl_opts.pop("_browser", None)
        ydl_opts.pop("_profile", None)

    # Limit FFmpeg CPU threads (helps VM responsiveness)
    ff_threads = int(os.environ.get("FFMPEG_THREADS", "1"))
    if ff_threads > 0:
        ydl_opts.setdefault("postprocessor_args", []).extend(["-threads", str(ff_threads)])

    if settings.audio_format == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        ydl_opts["merge_output_format"] = "mp3"
    return ydl_opts

def download_one(url: str, ydl_opts: dict, target_path: Optional[str], settings: Settings, retries: int = 4) -> str:
    if ydlp is None:  # pragma: no cover - requires yt-dlp runtime
        raise ImportError("yt_dlp is required to download videos")
    using_cookies = bool(ydl_opts.get("_fallback_cookie_file")) or ("_browser" in ydl_opts and "_profile" in ydl_opts)
    rot = ClientRotator(using_cookies, settings)
    set_client(ydl_opts, rot.current())

    for attempt in range(1, retries + 1):
        try:
            # Probe info first (no download) so we can rank formats
            with ydlp.YoutubeDL({**ydl_opts, "format": "bestaudio/best"}) as ydl:
                info = ydl.extract_info(url, download=False)
            debug_log_formats(info, url, settings)

            if settings.download_audio and target_path and os.path.exists(target_path):
                logging.info(f"Already exists: {target_path}")
                _post_download(target_path, settings)
                return DlStatus.OK

            candidates = ranked_audio_format_ids(info, settings)
            if not candidates:
                if attempt < retries:
                    prev, nxt = rot.current(), rot.next()
                    logging.warning(f"No audio-only formats for client={prev}. Rotating → {nxt}")
                    set_client(ydl_opts, nxt)
                    continue
                return DlStatus.NOAUDIO

            rotate_next_client = False

            for fmt_id in candidates:
                try:
                    with ydlp.YoutubeDL({**ydl_opts, "format": fmt_id}) as ydl:
                        ydl.download([url])
                    if target_path and os.path.exists(target_path):
                        logging.info(f"Saved: {target_path}")
                        _post_download(target_path, settings)
                    else:
                        logging.warning(f"Download reported success but file not found at expected path: {target_path}")
                    return DlStatus.OK

                except DownloadError as fe:
                    s = str(fe)

                    # Clean retry for 416 on this SAME format
                    if "HTTP Error 416" in s and target_path and os.path.exists(target_path):
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass
                        logging.warning("416 on resume; removed existing file; retrying this format clean once…")
                        try:
                            with ydlp.YoutubeDL({**ydl_opts, "format": fmt_id, "continuedl": False, "overwrites": True}) as ydl:
                                ydl.download([url])
                            if target_path and os.path.exists(target_path):
                                logging.info(f"Saved: {target_path}")
                                _post_download(target_path, settings)
                                return DlStatus.OK
                        except DownloadError:
                            # fall through to try the next candidate
                            pass

                    # Client-sensitive failures → rotate on next outer attempt
                    if ("m3u8" in s and "403" in s) or any(k in s for k in ("Requested format is not available", "Only images are available", "SABR", "missing a url", "nsig")):
                        rotate_next_client = True
                        break

                    logging.warning(f"Format {fmt_id} failed: {fe}. Trying next candidate…")

            if rotate_next_client and attempt < retries:
                prev, nxt = rot.current(), rot.next()
                logging.warning(f"Rotating client {prev} → {nxt}")
                set_client(ydl_opts, nxt)
                continue

            if attempt < retries:
                prev, nxt = rot.current(), rot.next()
                logging.warning(f"No viable audio formats for client={prev}. Rotating → {nxt}")
                set_client(ydl_opts, nxt)
                continue

            # Final fallback
            fallback = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/140/251/bestaudio"
            with ydlp.YoutubeDL({**ydl_opts, "format": fallback}) as ydl:
                ydl.download([url])
            if target_path and os.path.exists(target_path):
                logging.info(f"Saved: {target_path}")
                _post_download(target_path, settings)
            else:
                logging.warning(f"Download reported success but file not found at expected path: {target_path}")
            return DlStatus.OK

        except DownloadError as e:
            s = str(e)

            # Auth/rate limiting paths
            if ("Sign in to confirm" in s or "HTTP Error 429" in s) and using_cookies:
                wait = 30 if attempt == 1 else min(90, 20 * attempt)
                logging.warning(f"{s} — sleeping {wait}s; forcing web client with cookies and retrying")
                time.sleep(wait)
                rot.force_web_with_cookies()
                set_client(ydl_opts, rot.current())
                if attempt < retries:
                    continue

            if "HTTP Error 429" in s:
                return DlStatus.RATE

            # Hard failures that won't improve with retries
            if any(msg in s for msg in ("Video unavailable", "Private video", "This video is no longer available")):
                logging.error(s)
                return DlStatus.ERR

            logging.warning(f"Attempt {attempt} failed during info extraction: {s}. Backing off…")
            time.sleep(5 * attempt)
            if attempt < retries:
                prev, nxt = rot.current(), rot.next()
                logging.info(f"Switching client {prev} → {nxt}")
                set_client(ydl_opts, nxt)
                continue
            return DlStatus.ERR

        except Exception as e:
            logging.exception(f"Unexpected error on attempt {attempt}: {e}")
            time.sleep(5 * attempt)
            if attempt < retries:
                prev, nxt = rot.current(), rot.next()
                logging.info(f"Switching client {prev} → {nxt}")
                set_client(ydl_opts, nxt)
                continue
            return DlStatus.ERR

    return DlStatus.ERR
