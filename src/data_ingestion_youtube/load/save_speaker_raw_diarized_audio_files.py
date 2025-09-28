# src/data_ingestion_youtube/load/save_speaker_raw_diarized_audio_files.py
import os
import re
import io
import sys
import json
import time
import math
import pickle
import random
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

from src import YOUTUBE_VIDEO_DIRECTORY, root_directory

load_dotenv()

# ---------------------------------------------------------------------
# API keys (comma-separated)
# ---------------------------------------------------------------------
_api_keys = os.environ.get('ASSEMBLY_AI_API_KEYS')
if not _api_keys:
    raise EnvironmentError("ASSEMBLY_AI_API_KEYS environment variable not found.")
api_keys = [k.strip() for k in _api_keys.split(',') if k.strip()]

# Cache file lives next to the audio dataset
CACHE_FILE = os.path.join(YOUTUBE_VIDEO_DIRECTORY, '.diarization_cache.pkl')

# =========================
# Tunable runtime settings
# =========================

# Worker concurrency
MAX_CONCURRENT_TRANSCRIPTIONS = int(os.getenv("AAI_MAX_CONCURRENT_TRANSCRIPTIONS", "5"))
MAX_UPLOADS_PER_KEY = int(os.getenv("AAI_MAX_UPLOADS_PER_KEY", "1"))  # upload is the bottleneck

# Timeouts / retries
TRANSCRIBE_TIMEOUT_SEC = int(os.getenv("AAI_TRANSCRIBE_TIMEOUT_SEC", "1800"))  # 30 min
UPLOAD_RETRY_MAX = int(os.getenv("AAI_UPLOAD_RETRY_MAX", "6"))
BACKOFF_BASE = float(os.getenv("AAI_BACKOFF_BASE", "1.8"))
BACKOFF_CAP = float(os.getenv("AAI_BACKOFF_CAP", "60"))

# Upload chunk size (bytes)
AAI_UPLOAD_CHUNK_SIZE = int(os.getenv("AAI_UPLOAD_CHUNK_SIZE", str(5 * 1024 * 1024)))

# Planning / prioritization
PRIORITIZE_SMALL_CHANNELS = os.getenv("PRIORITIZE_SMALL_CHANNELS", "1") not in ("0", "false", "False")
GROUP_BY_YEAR = os.getenv("GROUP_BY_YEAR", "0") in ("1", "true", "True")
CHANNELS_PER_PASS = max(1, int(os.getenv("CHANNELS_PER_PASS", "1")))
MAX_PER_CHANNEL = int(os.getenv("MAX_PER_CHANNEL", "0"))  # 0 = no cap

# Plan CSV output
PLAN_CSV = Path(root_directory()) / "data" / "links" / "youtube" / "planned_transcriptions_summary.csv"

# =========================
# Filename parsing helpers
# =========================

_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')
_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')


def extract_video_id_from_path(file_path: str) -> Optional[str]:
    """Extract 11-char video ID from basename '{YYYY-MM-DD}_{ID}_{title}.ext'."""
    base = os.path.basename(file_path)
    name, _ext = os.path.splitext(base)
    parts = name.split("_", 2)
    if len(parts) < 2:
        return None
    vid = parts[1]
    if _ID_RE.match(vid):
        return vid
    return None


def parse_filename(file_path: str) -> Tuple[str, str, str]:
    """
    Return (date_iso, video_id, title) parsed from '{YYYY-MM-DD}_{ID}_{title}.ext'.
    Unknowns returned as ('unknown-date', 'unknown-id', 'untitled').
    """
    base = os.path.basename(file_path)
    name, _ext = os.path.splitext(base)
    parts = name.split("_", 2)
    if len(parts) < 3:
        return "unknown-date", "unknown-id", name
    date_iso, vid, title = parts[0], parts[1], parts[2]
    if not _DATE_RE.match(date_iso):
        date_iso = "unknown-date"
    if not _ID_RE.match(vid):
        vid = "unknown-id"
    title = title.replace('"', "").strip() or "untitled"
    return date_iso, vid, title


def year_of(date_iso: str) -> str:
    return date_iso[:4] if _DATE_RE.match(date_iso or "") else "unknown"


def date_to_int(date_iso: str) -> int:
    """YYYY-MM-DD -> yyyymmdd; unknown -> -1."""
    m = _DATE_RE.match(date_iso or "")
    if not m:
        return -1
    y, mm, dd = map(int, m.groups())
    return y * 10000 + mm * 100 + dd


def is_valid_filename(filename: str) -> bool:
    """Verify expected new naming pattern."""
    return re.match(r'^\d{4}-\d{2}-\d{2}_[a-zA-Z0-9_-]{11}_', filename) is not None


def truncate(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


# =========================
# Cache
# =========================

class DiarizationCache:
    """Simple persistent set of processed video IDs."""
    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self.processed_video_ids = self.load_cache()

    def load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    data = pickle.load(f)
                # Migrate old path-based caches
                if data and isinstance(next(iter(data), ""), str):
                    sample = next(iter(data), "")
                    if "/" in sample or "\\" in sample:
                        logging.info("Migrating cache from paths → video IDs…")
                        vids = set()
                        for p in data:
                            vid = extract_video_id_from_path(p)
                            if vid:
                                vids.add(vid)
                        logging.info("Migrated %d IDs from %d paths", len(vids), len(data))
                        return vids
                logging.info("Loaded cache with %d processed video IDs", len(data))
                return data
            except Exception as e:
                logging.warning("Could not load cache: %s. Starting fresh.", e)
                return set()
        return set()

    def save_cache(self):
        try:
            with open(self.cache_file, 'wb') as f:
                pickle.dump(self.processed_video_ids, f)
        except Exception as e:
            logging.error("Could not save cache: %s", e)

    def is_processed(self, file_path: str) -> bool:
        vid = extract_video_id_from_path(file_path)
        if vid and vid in self.processed_video_ids:
            return True
        # belt & suspenders: check json sidecar
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        if os.path.exists(transcript_file):
            if vid:
                self.processed_video_ids.add(vid)
                self.save_cache()
            return True
        return False

    def mark_processed(self, file_path: str):
        vid = extract_video_id_from_path(file_path)
        if vid:
            self.processed_video_ids.add(vid)
            self.save_cache()

    def get_unprocessed_files(self, files: List[str]) -> List[str]:
        out = []
        for p in files:
            if not self.is_processed(p):
                out.append(p)
        logging.info("Found %d unprocessed files out of %d total", len(out), len(files))
        return out


class ProcessSafeCache:
    """Cache wrapper for worker processes (checks file or loads set on each call)."""
    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._lock = threading.Lock()

    def _load_ids(self) -> set:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                return set()
        return set()

    def is_processed(self, file_path):
        # sidecar present?
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        if os.path.exists(transcript_file):
            return True
        vid = extract_video_id_from_path(file_path)
        if not vid:
            return False
        with self._lock:
            ids = self._load_ids()
            return vid in ids

    def mark_processed(self, file_path):
        vid = extract_video_id_from_path(file_path)
        if not vid:
            return
        with self._lock:
            ids = self._load_ids()
            ids.add(vid)
            try:
                with open(self.cache_file, 'wb') as f:
                    pickle.dump(ids, f)
            except Exception as e:
                logging.error("Could not update cache: %s", e)


# =========================
# Logging helpers
# =========================

class No200HTTPFilter(logging.Filter):
    def filter(self, record):
        return "HTTP/1.1 200 OK" not in record.getMessage()


class ThreadLogger:
    @staticmethod
    def get_logger(api_key_index: int) -> logging.Logger:
        name = f"Worker-{api_key_index}"
        logger = logging.getLogger(name)
        if not logger.handlers:
            h = logging.StreamHandler()
            fmt = logging.Formatter(f"%(asctime)s - [API-{api_key_index}] - %(levelname)s - %(message)s")
            h.setFormatter(fmt)
            logger.addHandler(h)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        return logger


# =========================
# AssemblyAI upload + transcribe
# =========================

def set_api_key(api_key):
    import assemblyai as aai
    aai.settings.api_key = api_key


class PerKeyLimiter:
    def __init__(self, max_uploads: int):
        self.sem = threading.Semaphore(max_uploads)

    def acquire(self):
        self.sem.acquire()

    def release(self):
        self.sem.release()


def backoff_sleep(attempt, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    delay = min(cap, (base ** attempt))
    time.sleep(random.uniform(0, delay))


def _stream_file(path, chunk_size):
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            yield buf


def _requests_session_with_retry() -> requests.Session:
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=0.5,
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _raw_upload_with_requests(file_path: str, api_key: str, logger: logging.Logger) -> str:
    """
    Direct streaming upload to /v2/upload with chunked transfer.
    Adds Content-Type header and uses a retriable requests session.
    """
    url = "https://api.assemblyai.com/v2/upload"
    headers = {
        "authorization": api_key,
        "Content-Type": "application/octet-stream",
    }
    sess = _requests_session_with_retry()
    resp = sess.post(
        url,
        headers=headers,
        data=_stream_file(file_path, AAI_UPLOAD_CHUNK_SIZE),
        timeout=900,  # allow big files
    )
    if 200 <= resp.status_code < 300:
        try:
            js = resp.json()
        except Exception:
            js = {}
        return js.get("upload_url") or js.get("url") or js.get("uploadUrl")
    raise RuntimeError(f"Upload HTTP {resp.status_code}: {resp.text[:300]}")


def upload_with_retries(file_path, logger, api_key):
    """
    Robust uploader: try SDK surfaces; on failure, fallback to raw requests with retries.
    Treat 422 during upload as transient (often momentary ingestion glitch).
    """
    import assemblyai as aai
    attempt = 0
    last_err = None

    has_files_upload = hasattr(aai, "files") and hasattr(getattr(aai, "files"), "upload")
    has_upload_file = hasattr(aai, "upload_file")

    while attempt < UPLOAD_RETRY_MAX:
        try:
            if has_files_upload:
                return aai.files.upload(file_path, chunk_size=AAI_UPLOAD_CHUNK_SIZE)
            if has_upload_file:
                return aai.upload_file(file_path)
            return _raw_upload_with_requests(file_path, api_key, logger)
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            last_err = e
            transient_bits = ("502", "503", "504", "Bad Gateway", "Service Unavailable",
                              "Gateway", "timed out", "Timeout", "Temporary failure",
                              "Connection reset", "aborted", "ECONNRESET", "429",
                              "SSLEOFError", "SSL", "EOF occurred", "ChunkedEncodingError")
            is_422 = "422" in msg or "Upload HTTP 422" in msg
            if is_422 or any(bit in msg for bit in transient_bits):
                attempt += 1
                logger.warning(f"Upload failed (attempt {attempt}/{UPLOAD_RETRY_MAX}) for {os.path.basename(file_path)}: {msg}")
                backoff_sleep(attempt)
                continue
            raise
    raise RuntimeError(f"Upload failed after {UPLOAD_RETRY_MAX} attempts: {last_err}")


def utterance_to_safe_dict(u) -> dict:
    words = getattr(u, "words", None) or []
    return {
        "text": getattr(u, "text", ""),
        "start": getattr(u, "start", None),
        "end": getattr(u, "end", None),
        "confidence": getattr(u, "confidence", None),
        "channel": getattr(u, "channel", None),
        "speaker": getattr(u, "speaker", None),
        "words": [{
            "text": getattr(w, "text", ""),
            "start": getattr(w, "start", None),
            "end": getattr(w, "end", None),
            "confidence": getattr(w, "confidence", None),
            "channel": getattr(w, "channel", None),
            "speaker": getattr(w, "speaker", None),
        } for w in words]
    }


def transcribe_single_file(api_key, file_path, cache_file, logger):
    """Transcribe one file; guarded by per-key upload limiter."""
    cache = ProcessSafeCache(cache_file)
    try:
        transcript_out = os.path.splitext(file_path)[0] + "_diarized_content.json"

        if cache.is_processed(file_path):
            logger.debug(f"Already processed: {os.path.basename(file_path)}")
            return {"status": "SKIPPED", "file": file_path}

        if not os.path.exists(file_path):
            logger.warning(f"Missing file: {file_path}")
            return {"status": "FAILED", "file": file_path, "reason": "missing_file"}

        # For logs
        path_segs = file_path.split(os.sep)
        channel_name = path_segs[-3] if len(path_segs) >= 3 else "unknown"
        fname = path_segs[-1]

        logger.info(f"Starting diarization: [{channel_name}/{fname}]")

        # Upload with per-key limiter
        import assemblyai as aai
        config = aai.TranscriptionConfig(speaker_labels=True)
        transcriber = aai.Transcriber()

        if not hasattr(transcribe_single_file, "_limiters"):
            transcribe_single_file._limiters = {}
        limiter = transcribe_single_file._limiters.setdefault(api_key, PerKeyLimiter(MAX_UPLOADS_PER_KEY))

        limiter.acquire()
        try:
            up_start = time.time()
            audio_url = upload_with_retries(file_path, logger, api_key)
            logger.debug(f"Uploaded in {time.time() - up_start:.1f}s -> {audio_url}")
        finally:
            limiter.release()

        # Kick and wait with a real timeout
        start_time = time.time()
        job = transcriber.submit(audio_url, config=config)

        # New SDK uses futures underneath; give it a deadline
        import concurrent.futures as _cf
        try:
            transcript = job.wait_for_completion_async().result(timeout=TRANSCRIBE_TIMEOUT_SEC)
        except _cf.TimeoutError:
            logger.error(f"Timed out after {TRANSCRIBE_TIMEOUT_SEC}s: [{channel_name}/{fname}]")
            try:
                if getattr(job, "id", None):
                    aai.Transcript.delete_by_id(job.id)
            except Exception:
                pass
            return {"status": "FAILED", "file": file_path, "reason": "timeout"}

        duration = time.time() - start_time
        if transcript is None:
            logger.error(f"Transcript was None: [{channel_name}/{fname}]")
            return {"status": "FAILED", "file": file_path, "reason": "transcript_none"}

        status = getattr(transcript, "status", None)
        err = getattr(transcript, "error", None)
        if status == "error" or err:
            logger.error(f"AssemblyAI error for [{channel_name}/{fname}]: {err or status}")
            return {"status": "FAILED", "file": file_path, "reason": err or status}

        utterances = getattr(transcript, "utterances", None)

        # Save
        if not utterances:
            # Fallback single blob of text
            text = getattr(transcript, "text", "") or ""
            payload = [{
                "text": text,
                "start": None, "end": None,
                "confidence": getattr(transcript, "confidence", None),
                "channel": None, "speaker": None,
                "words": []
            }]
        else:
            payload = [utterance_to_safe_dict(u) for u in utterances]

        with open(transcript_out, 'w') as f:
            json.dump(payload, f, indent=4)

        cache.mark_processed(file_path)
        logger.info(f"SUCCESS [{channel_name}/{fname}] in {duration:.1f}s")
        return {"status": "SUCCESS", "file": file_path, "duration": duration}

    except Exception as e:
        logger.error(f"Error transcribing {file_path}: {e}")
        return {"status": "FAILED", "file": file_path, "reason": str(e)}


# =========================
# Planning / summary table
# =========================

def scan_all_mp3() -> List[str]:
    data_path = YOUTUBE_VIDEO_DIRECTORY
    return [
        os.path.join(root, f)
        for root, _dirs, files in os.walk(data_path)
        for f in files
        if f.endswith(".mp3") and is_valid_filename(f)
    ]


def group_unprocessed_by_channel(unprocessed_files: List[str]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {}
    for p in unprocessed_files:
        # path structure: .../<channel>/<YYYY-...>_<id>_<title>/<same>.mp3
        parts = Path(p).parts
        channel = parts[-3] if len(parts) >= 3 else "unknown"
        buckets.setdefault(channel, []).append(p)
    # sort each channel newest→oldest
    for ch in list(buckets.keys()):
        buckets[ch].sort(key=lambda path: date_to_int(parse_filename(path)[0]), reverse=True)
        # cap per-channel if requested
        if MAX_PER_CHANNEL > 0:
            buckets[ch] = buckets[ch][:MAX_PER_CHANNEL]
    return buckets


def plan_order(buckets: Dict[str, List[str]]) -> List[str]:
    """
    Return global ordered list of files to process:
    - order channels by FEWEST remaining first (tie: alphabetical)
    - within channel: newest→oldest (already sorted)
    - optional year grouping could be added here (kept off by default)
    """
    channels = list(buckets.keys())
    if PRIORITIZE_SMALL_CHANNELS:
        channels.sort(key=lambda c: (len(buckets[c]), c.lower()))
    else:
        channels.sort(key=lambda c: c.lower())

    # Flatten in channel priority order
    ordered: List[str] = []
    if GROUP_BY_YEAR:
        # collect all years
        all_years = set()
        per_ch_years: Dict[str, Dict[str, List[str]]] = {}
        for c in channels:
            ys: Dict[str, List[str]] = {}
            for p in buckets[c]:
                y = year_of(parse_filename(p)[0])
                ys.setdefault(y, []).append(p)
            # keep newest→oldest in each bucket
            for yk in ys:
                ys[yk].sort(key=lambda path: date_to_int(parse_filename(path)[0]), reverse=True)
            per_ch_years[c] = ys
            all_years.update(ys.keys())
        years_sorted = sorted([y for y in all_years if y != "unknown"], reverse=True)
        if "unknown" in all_years:
            years_sorted.append("unknown")

        for y in years_sorted:
            ch_for_y = [c for c in channels if y in per_ch_years[c] and per_ch_years[c][y]]
            if PRIORITIZE_SMALL_CHANNELS:
                ch_for_y.sort(key=lambda c: (len(per_ch_years[c][y]), c.lower()))
            for i in range(0, len(ch_for_y), CHANNELS_PER_PASS):
                group = ch_for_y[i:i + CHANNELS_PER_PASS]
                for c in group:
                    ordered.extend(per_ch_years[c][y])
    else:
        for i in range(0, len(channels), CHANNELS_PER_PASS):
            group = channels[i:i + CHANNELS_PER_PASS]
            for c in group:
                ordered.extend(buckets[c])

    return ordered


def print_plan_summary(buckets: Dict[str, List[str]]) -> None:
    if not buckets:
        print("\nNo transcriptions needed. ✅\n")
        return

    ch_width = 32
    cnt_width = 8
    top_width = 90

    print("\n=== Transcription Plan Summary (newest → oldest within channel) ===")
    print(f"{'Channel':{ch_width}} {'Missing':>{cnt_width}}  Top-3 most recent to transcribe")
    print("-" * (ch_width + cnt_width + 2 + 64))

    total = 0
    # fewest missing first
    ordered_items = sorted(buckets.items(), key=lambda kv: (len(kv[1]), kv[0].lower()))
    rows_for_csv: List[Dict] = []

    for channel, files in ordered_items:
        total += len(files)
        top3 = files[:3]
        triples = []
        for p in top3:
            d, vid, title = parse_filename(p)
            triples.append(f"{d} — {truncate(title, top_width)}")
        top3_str = " | ".join(triples) if triples else "-"
        print(f"{channel:{ch_width}} {len(files):>{cnt_width}}  {top3_str}")

        # CSV rows
        for p in files:
            d, vid, title = parse_filename(p)
            rows_for_csv.append({
                "channel": channel,
                "video_id": vid,
                "date": d,
                "year": year_of(d),
                "title": title,
                "path": p,
            })

    print("-" * (ch_width + cnt_width + 2 + 64))
    print(f"TOTAL files to transcribe: {total}\n")

    # Persist plan CSV
    try:
        import pandas as pd
        PLAN_CSV.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows_for_csv).to_csv(PLAN_CSV, index=False)
        print(f"Saved detailed plan to: {PLAN_CSV}")
    except Exception as e:
        logging.debug("Could not write plan CSV: %s", e)


# =========================
# Workers
# =========================

def worker_with_backlog(api_key_index, api_key, file_paths, cache):
    set_api_key(api_key)
    logger = ThreadLogger.get_logger(api_key_index)

    total = len(file_paths)
    logger.info(f"Starting worker with {total} files to process")
    if total == 0:
        logger.info("No files assigned to this worker")
        return

    completed = {"SUCCESS": 0, "FALLBACK": 0, "SKIPPED": 0, "FAILED": 0}

    # Keep batches small; uploads are limited per key
    batch_size = min(MAX_CONCURRENT_TRANSCRIPTIONS, 3)

    for i in range(0, total, batch_size):
        batch = file_paths[i:i + batch_size]
        logger.info(f"Processing batch {i // batch_size + 1}: files {i + 1}-{i + len(batch)} of {total}")
        logger.info(f"Queue status: Completed={completed}, Remaining={total - (i)}")

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = []
            for file_path in batch:
                # tiny jitter to avoid synchronized POSTs
                time.sleep(random.uniform(0.1, 0.3))
                futures.append(executor.submit(
                    transcribe_single_file, api_key, file_path, cache.cache_file, logger
                ))

            # Wait for batch
            for fut in futures:
                try:
                    result = fut.result(timeout=TRANSCRIBE_TIMEOUT_SEC + 180)
                    status = (result or {}).get("status", "FAILED")
                    completed[status] = completed.get(status, 0) + 1
                except Exception as e:
                    logger.error(f"Batch processing error: {e}")
                    completed["FAILED"] += 1

        logger.info(
            f"Batch complete. Total done: {sum(completed.values())}/{total} "
            f"(SUCCESS: {completed['SUCCESS']}, FALLBACK: {completed['FALLBACK']}, "
            f"SKIPPED: {completed['SKIPPED']}, FAILED: {completed['FAILED']})"
        )

        if i + batch_size < total:
            sleep_s = random.uniform(2, 5)
            logger.info(f"Waiting {sleep_s:.1f}s before next batch…")
            time.sleep(sleep_s)

    logger.info(
        f"Worker completed! Processed {sum(completed.values())} files "
        f"(SUCCESS: {completed['SUCCESS']}, FALLBACK: {completed['FALLBACK']}, "
        f"SKIPPED: {completed['SKIPPED']}, FAILED: {completed['FAILED']})"
    )


# =========================
# Main
# =========================

def main():
    # Main logger
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MAIN] - %(levelname)s - %(message)s')
    logging.getLogger().addFilter(No200HTTPFilter())

    cache = DiarizationCache()

    # Discover audio
    all_mp3_files = scan_all_mp3()
    if not all_mp3_files:
        logging.warning("No MP3 files found to transcribe.")
        return

    logging.info(f"Found {len(all_mp3_files)} total MP3 files")

    # Filter unprocessed
    mp3_files = cache.get_unprocessed_files(all_mp3_files)
    if not mp3_files:
        logging.info("All files have been processed. Nothing to do.")
        return

    # Group by channel & sort
    buckets = group_unprocessed_by_channel(mp3_files)

    # Print concise plan and save CSV
    print_plan_summary(buckets)

    # Build global ordered list from plan
    ordered_files = plan_order(buckets)

    # Log a few samples (global order)
    sample_sz = min(5, len(ordered_files))
    for i in range(sample_sz):
        d, vid, title = parse_filename(ordered_files[i])
        logging.info(f"Sample {i+1}: {d} {vid} — {title}")

    # Distribute ordered files across API keys round-robin (preserves priority)
    nkeys = len(api_keys)
    chunks: List[List[str]] = [[] for _ in range(nkeys)]
    for idx, path in enumerate(ordered_files):
        chunks[idx % nkeys].append(path)

    for i, chunk in enumerate(chunks):
        logging.info(f"API Key {i}: {len(chunk)} files assigned")

    # Launch workers
    with ProcessPoolExecutor(max_workers=nkeys) as executor:
        futures = [
            executor.submit(worker_with_backlog, i, api_key, chunks[i], cache)
            for i, api_key in enumerate(api_keys)
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logging.error(f"Worker process failed: {e}")

    logging.info("All workers completed!")


if __name__ == "__main__":
    main()
