# src/data_ingestion_youtube/load/download_mp3/cookies.py
import os
import time
import logging
from contextlib import suppress
import multiprocessing as mp
import yt_dlp as ydlp

PKG_DIR = os.path.dirname(__file__)
DEFAULT_COOKIE_PATH = os.path.join(PKG_DIR, "youtube_cookies.txt")
MAX_COOKIE_AGE_DAYS = int(os.environ.get("MAX_COOKIE_AGE_DAYS", "7"))
BROWSER = os.environ.get("BROWSER", "brave")
PROFILE = os.environ.get("PROFILE", "Default")
COOKIE_EXPORT_TIMEOUT = int(os.environ.get("COOKIE_EXPORT_TIMEOUT", "20"))  # seconds
ATTEMPT_ORDER = tuple((os.environ.get("COOKIE_ATTEMPT_ORDER") or "keyring,plain").split(","))  # e.g. "plain,keyring"

def netscape_cookiefile_looks_ok(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines or not lines[0].startswith("# Netscape HTTP Cookie File"):
            return False
        for ln in lines[1:]:
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) != 7:
                return False
            exp = int(parts[4])
            if not (exp == 0 or (1_000_000_000 <= exp < 4_102_444_800)):
                return False
            return True
        return False
    except Exception:
        return False

def _export_worker(out_path: str, browser: str, profile: str, use_keyring: bool, retq: mp.Queue):
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        logging.info(f"Exporting YouTube cookies from {browser} ({profile}) keyring={use_keyring} → {out_path}")
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "cookiesfrombrowser": (browser, profile, None, use_keyring),
            "cookiefile": out_path,       # initialize jar
            "socket_timeout": 10,
            "retries": 1,
        }
        with ydlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info("https://www.youtube.com/", download=False)
            ydl.save_cookies()
        if not netscape_cookiefile_looks_ok(out_path):
            raise RuntimeError("Cookie export produced an invalid Netscape cookie file.")
        retq.put(("ok", None))
    except Exception as e:
        with suppress(Exception):
            if os.path.exists(out_path):
                os.remove(out_path)
        retq.put(("err", str(e)))

def _attempt_with_timeout(out_path: str, browser: str, profile: str, use_keyring: bool) -> str:
    ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_export_worker, args=(out_path, browser, profile, use_keyring, q))
    p.start()
    p.join(COOKIE_EXPORT_TIMEOUT)
    if p.is_alive():
        # Hard kill the stuck exporter
        p.terminate()
        p.join(5)
        with suppress(Exception):
            if os.path.exists(out_path):
                os.remove(out_path)
        raise TimeoutError(f"cookie export timed out after {COOKIE_EXPORT_TIMEOUT}s (keyring={use_keyring})")
    try:
        status, msg = q.get_nowait()
    except Exception:
        status, msg = ("err", "cookie exporter exited without a status")
    if status == "ok":
        return out_path
    raise RuntimeError(msg or "cookie export failed")

def _export_with_timeout(out_path: str, browser: str, profile: str) -> str:
    # Build attempt list from env order
    attempts = []
    for tag in ATTEMPT_ORDER:
        tag = tag.strip().lower()
        if tag == "keyring":
            attempts.append(True)
        elif tag in ("plain", "nokeyring", "no_keyring"):
            attempts.append(False)
    # Fallback if mis-specified
    if not attempts:
        attempts = [True, False]

    last_err: Exception | None = None
    for use_keyring in attempts:
        try:
            return _attempt_with_timeout(out_path, browser, profile, use_keyring)
        except Exception as e:
            last_err = e
            logging.warning(f"Cookie export attempt keyring={use_keyring} failed: {e}")

    raise RuntimeError(str(last_err) if last_err else "Cookie export failed")

def ensure_cached_cookiefile(out_path: str = DEFAULT_COOKIE_PATH,
                             browser: str = BROWSER,
                             profile: str = PROFILE,
                             max_age_days: int = MAX_COOKIE_AGE_DAYS) -> str:
    if os.path.exists(out_path) and netscape_cookiefile_looks_ok(out_path):
        if max_age_days > 0:
            age_days = (time.time() - os.path.getmtime(out_path)) / 86400.0
            if age_days > max_age_days:
                logging.info("Cookie cache is old; refreshing…")
                return _export_with_timeout(out_path, browser, profile)
        logging.info(f"✓ Using cached cookies file: {out_path}")
        return out_path
    return _export_with_timeout(out_path, browser, profile)

def setup_cookies() -> str | None:
    logging.info("Setting up YouTube cookies (cached)…")
    if os.environ.get("USE_COOKIE_CACHE", "1").lower() in ("0", "false", "no"):
        logging.info("Skipping cookie cache; will use runtime/no cookies.")
        return None
    try:
        return ensure_cached_cookiefile()
    except Exception as e:
        logging.warning(f"Cookie export failed or timed out ({e}). Continuing without cookies.")
        return None
