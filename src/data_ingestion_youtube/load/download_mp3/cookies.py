import os
import time
import logging
import yt_dlp as ydlp
from contextlib import suppress

PKG_DIR = os.path.dirname(__file__)
DEFAULT_COOKIE_PATH = os.path.join(PKG_DIR, "youtube_cookies.txt")
MAX_COOKIE_AGE_DAYS = int(os.environ.get("MAX_COOKIE_AGE_DAYS", "7"))
BROWSER = os.environ.get("BROWSER", "brave")
PROFILE = os.environ.get("PROFILE", "Default")
COOKIE_EXPORT_TIMEOUT = int(os.environ.get("COOKIE_EXPORT_TIMEOUT", "20"))  # seconds


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


def _export_from_browser(out_path: str, browser: str = BROWSER, profile: str = PROFILE) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    logging.info(f"Exporting YouTube cookies from {browser} ({profile}) → {out_path}")

    for use_keyring in (True, False):
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "cookiesfrombrowser": (browser, profile, None, use_keyring),
                "socket_timeout": 10,
            }
            with ydlp.YoutubeDL(ydl_opts) as ydl:
                # Light touch: open homepage to load cookies; don't invoke heavy extractors
                ydl.urlopen("https://www.youtube.com/")
                ydl.params["cookiefile"] = out_path
                ydl.save_cookies()

            if not netscape_cookiefile_looks_ok(out_path):
                raise RuntimeError("Cookie export produced an invalid Netscape cookie file.")
            logging.info(f"✓ Cookies cached at: {out_path}")
            return out_path
        except Exception as e:
            logging.warning(f"Cookie export attempt (keyring={use_keyring}) failed: {e}")
            if os.path.exists(out_path):
                try: os.remove(out_path)
                except Exception: pass

    raise RuntimeError("All cookie export attempts failed")


def ensure_cached_cookiefile(out_path: str = DEFAULT_COOKIE_PATH,
                             browser: str = BROWSER,
                             profile: str = PROFILE,
                             max_age_days: int = MAX_COOKIE_AGE_DAYS) -> str:
    if os.path.exists(out_path) and netscape_cookiefile_looks_ok(out_path):
        if max_age_days > 0:
            age_days = (time.time() - os.path.getmtime(out_path)) / 86400.0
            if age_days > max_age_days:
                logging.info("Cookie cache is old; refreshing…")
                return _export_from_browser(out_path, browser, profile)
        logging.info(f"✓ Using cached cookies file: {out_path}")
        return out_path
    return _export_from_browser(out_path, browser, profile)


def setup_cookies() -> str | None:
    logging.info("Setting up YouTube cookies (cached)…")
    # Allow skipping export entirely
    if os.environ.get("USE_COOKIE_CACHE", "1") in ("0", "false", "False"):
        logging.info("Skipping cookie cache; will use runtime browser cookies.")
        return None
    try:
        return ensure_cached_cookiefile()
    except Exception as e:
        logging.warning(f"Cookie export failed ({e}). Continuing without cookies.")
        return None
