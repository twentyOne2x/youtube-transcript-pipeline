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
