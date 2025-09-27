import json
import logging
import os
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dotenv import load_dotenv
import pickle
from datetime import datetime
from queue import Queue
import threading
import concurrent.futures as cf


from src import YOUTUBE_VIDEO_DIRECTORY

load_dotenv()
api_keys = os.environ.get('ASSEMBLY_AI_API_KEYS')

if not api_keys:
    raise EnvironmentError("ASSEMBLY_AI_API_KEYS environment variable not found.")

api_keys = [k.strip() for k in api_keys.split(',') if k.strip()]

# Cache file path
CACHE_FILE = os.path.join(YOUTUBE_VIDEO_DIRECTORY, '.diarization_cache.pkl')

# =========================
# Tunable runtime settings
# =========================

# Concurrency limits (uploads are the bottleneck)
MAX_CONCURRENT_TRANSCRIPTIONS = int(os.getenv("AAI_MAX_CONCURRENT_TRANSCRIPTIONS", "5"))
MAX_UPLOADS_PER_KEY = int(os.getenv("AAI_MAX_UPLOADS_PER_KEY", "1"))  # keep this low!

# Timeouts / retries
TRANSCRIBE_TIMEOUT_SEC = int(os.getenv("AAI_TRANSCRIBE_TIMEOUT_SEC", "1800"))  # 30 min
UPLOAD_RETRY_MAX = int(os.getenv("AAI_UPLOAD_RETRY_MAX", "6"))
BACKOFF_BASE = float(os.getenv("AAI_BACKOFF_BASE", "1.8"))
BACKOFF_CAP = float(os.getenv("AAI_BACKOFF_CAP", "60"))  # max sleep between retries

# Upload chunk size (bytes). 5–10MB is a sweet spot.
AAI_UPLOAD_CHUNK_SIZE = int(os.getenv("AAI_UPLOAD_CHUNK_SIZE", str(5 * 1024 * 1024)))

# =========================


def extract_video_id_from_path(file_path):
    """Extract video ID from file path - video ID comes after date"""
    filename = os.path.basename(file_path)
    # New format: {date}_{video_id}_{title}.mp3
    # Date is YYYY-MM-DD (10 chars) + underscore, then video ID
    if len(filename) >= 22:  # At least date + _ + video_id
        potential_id = filename[11:22]  # Skip date and underscore, get 11-char ID
        if re.match(r'^[a-zA-Z0-9_-]{11}$', potential_id):
            return potential_id
    return None


class DiarizationCache:
    """Simple cache to track processed files by video ID"""

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self.processed_video_ids = self.load_cache()

    def load_cache(self):
        """Load cache from disk - now stores video IDs instead of full paths"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                    # Handle both old format (full paths) and new format (video IDs)
                    if cache_data and isinstance(next(iter(cache_data), ""), str):
                        # Check if it's old format (full paths) or new format (video IDs)
                        sample = next(iter(cache_data), "")
                        if "/" in sample or "\\" in sample:  # Old format with paths
                            logging.info("Migrating cache from paths to video IDs...")
                            video_ids = set()
                            for path in cache_data:
                                video_id = extract_video_id_from_path(path)
                                if video_id:
                                    video_ids.add(video_id)
                            logging.info(f"Migrated {len(video_ids)} video IDs from {len(cache_data)} paths")
                            return video_ids
                    logging.info(f"Loaded cache with {len(cache_data)} processed video IDs")
                    return cache_data
            except Exception as e:
                logging.warning(f"Could not load cache: {e}. Starting fresh.")
                return set()
        return set()

    def save_cache(self):
        """Save cache to disk"""
        try:
            with open(self.cache_file, 'wb') as f:
                pickle.dump(self.processed_video_ids, f)
            logging.debug(f"Cache saved with {len(self.processed_video_ids)} video IDs")
        except Exception as e:
            logging.error(f"Could not save cache: {e}")

    def is_processed(self, file_path):
        """Check if file has been processed by video ID"""
        video_id = extract_video_id_from_path(file_path)
        if not video_id:
            # Fallback to checking if JSON file exists
            transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
            return os.path.exists(transcript_file)

        # Check if video ID is in cache
        if video_id in self.processed_video_ids:
            return True

        # Also check if the actual JSON file exists (belt and suspenders)
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        if os.path.exists(transcript_file):
            # Add to cache if found but not in cache
            self.processed_video_ids.add(video_id)
            self.save_cache()
            return True

        return False

    def mark_processed(self, file_path):
        """Mark file as processed by video ID"""
        video_id = extract_video_id_from_path(file_path)
        if video_id:
            self.processed_video_ids.add(video_id)
            self.save_cache()
        else:
            logging.warning(f"Could not extract video ID from {file_path}")

    def get_unprocessed_files(self, file_list):
        """Filter list to only unprocessed files"""
        unprocessed = []
        for file_path in file_list:
            if not self.is_processed(file_path):
                unprocessed.append(file_path)

        logging.info(f"Found {len(unprocessed)} unprocessed files out of {len(file_list)} total")
        return unprocessed

    def rebuild_from_disk(self):
        """Rebuild cache by scanning for existing JSON files and extracting video IDs"""
        logging.info("Rebuilding cache from existing diarized files...")
        self.processed_video_ids = set()

        for root, _, files in os.walk(YOUTUBE_VIDEO_DIRECTORY):
            for file in files:
                if file.endswith("_diarized_content.json"):
                    # Extract video ID from the JSON filename
                    video_id = extract_video_id_from_path(file)
                    if video_id:
                        self.processed_video_ids.add(video_id)
                    else:
                        # Try to get from the corresponding mp3 file
                        mp3_file = file.replace("_diarized_content.json", ".mp3")
                        video_id = extract_video_id_from_path(mp3_file)
                        if video_id:
                            self.processed_video_ids.add(video_id)

        self.save_cache()
        logging.info(f"Cache rebuilt with {len(self.processed_video_ids)} video IDs")


class ProcessSafeCache:
    """Cache wrapper for use in worker processes"""

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._lock = threading.Lock()

    def is_processed(self, file_path):
        """Check if file has been processed by checking video ID and filesystem"""
        video_id = extract_video_id_from_path(file_path)

        # First check if transcript file exists
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        if os.path.exists(transcript_file):
            return True

        if not video_id:
            return False

        # Check cache for video ID
        with self._lock:
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, 'rb') as f:
                        processed_video_ids = pickle.load(f)
                        return video_id in processed_video_ids
                except:
                    pass
        return False

    def mark_processed(self, file_path):
        """Mark file as processed by updating the cache with video ID"""
        video_id = extract_video_id_from_path(file_path)
        if not video_id:
            logging.warning(f"Could not extract video ID from {file_path}")
            return

        with self._lock:
            # Load current cache
            processed_video_ids = set()
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, 'rb') as f:
                        processed_video_ids = pickle.load(f)
                except:
                    pass

            # Add new video ID
            processed_video_ids.add(video_id)

            # Save updated cache
            try:
                with open(self.cache_file, 'wb') as f:
                    pickle.dump(processed_video_ids, f)
            except Exception as e:
                logging.error(f"Could not update cache: {e}")


class No200HTTPFilter(logging.Filter):
    def filter(self, record):
        if "HTTP/1.1 200 OK" in record.getMessage():
            return False
        return True


class ThreadLogger:
    """Helper class to create thread-specific loggers"""

    @staticmethod
    def get_logger(api_key_index):
        """Get a logger with thread-specific formatting"""
        logger_name = f"Worker-{api_key_index}"
        logger = logging.getLogger(logger_name)

        # Only add handler if it doesn't exist
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                f'%(asctime)s - [API-{api_key_index}] - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False

        return logger


def set_api_key(api_key):
    import assemblyai as aai
    aai.settings.api_key = api_key


def random_sleep(min_seconds=0.5, max_seconds=2.5):
    time.sleep(random.uniform(min_seconds, max_seconds))


def is_valid_filename(filename):
    """Check if filename starts with video ID pattern"""
    # Updated to check for video ID at the start after the date
    # Video IDs are 11 characters of alphanumeric, dash, underscore
    return re.match(r'^\d{4}-\d{2}-\d{2}_[a-zA-Z0-9_-]{11}_', filename) is not None


def utterance_to_dict(utterance) -> dict:
    return {
        'text': utterance.text,
        'start': utterance.start,
        'end': utterance.end,
        'confidence': utterance.confidence,
        'channel': utterance.channel,
        'speaker': utterance.speaker,
        'words': [{
            'text': word.text,
            'start': word.start,
            'end': word.end,
            'confidence': word.confidence,
            'channel': word.channel,
            'speaker': word.speaker
        } for word in utterance.words]
    }


# =========================
# Upload throttling helpers
# =========================

class PerKeyLimiter:
    def __init__(self, max_uploads: int):
        self.sem = threading.Semaphore(max_uploads)

    def acquire(self):
        self.sem.acquire()

    def release(self):
        self.sem.release()


def backoff_sleep(attempt, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """Exponential backoff with full jitter"""
    import random as _random
    delay = min(cap, (base ** attempt))
    time.sleep(_random.uniform(0, delay))


def _stream_file(path, chunk_size):
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            yield data


def _raw_upload_with_requests(file_path, api_key, logger):
    import requests
    url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": api_key}
    # streaming generator triggers chunked transfer; no Content-Length needed
    resp = requests.post(
        url,
        headers=headers,
        data=_stream_file(file_path, AAI_UPLOAD_CHUNK_SIZE),
        timeout=300,
    )
    if 200 <= resp.status_code < 300:
        js = {}
        try:
            js = resp.json()
        except Exception:
            pass
        # SDK returns 'upload_url'; be liberal just in case
        return js.get("upload_url") or js.get("url") or js.get("uploadUrl")
    raise RuntimeError(f"Upload HTTP {resp.status_code}: {resp.text[:300]}")


def upload_with_retries(file_path, logger, api_key):
    """
    Robust uploader that works across SDK versions:
    - New SDKs: aai.files.upload
    - Older SDKs: aai.upload_file
    - Fallback: direct streaming to /v2/upload with requests
    """
    import assemblyai as aai
    attempt = 0
    last_err = None

    # Detect SDK surface
    has_files_upload = hasattr(aai, "files") and hasattr(getattr(aai, "files"), "upload")
    has_upload_file = hasattr(aai, "upload_file")

    while attempt < UPLOAD_RETRY_MAX:
        try:
            if has_files_upload:
                # New SDK supports chunk_size
                return aai.files.upload(file_path, chunk_size=AAI_UPLOAD_CHUNK_SIZE)
            if has_upload_file:
                # Older SDK; still chunked internally
                return aai.upload_file(file_path)
            # Fallback: direct HTTP
            return _raw_upload_with_requests(file_path, api_key, logger)

        except Exception as e:
            msg = str(e) or e.__class__.__name__
            last_err = e
            # Errors we treat as transient (upload is the flaky step)
            transient = any(s in msg for s in [
                "502", "503", "504", "Bad Gateway", "Service Unavailable", "Gateway",
                "timed out", "Timeout", "Temporary failure", "Connection reset",
                "Upload failed", "Read timed out", "Connection aborted", "ECONNRESET",
                "429", "Too Many Requests"
            ])
            code422 = ("422" in msg)  # treat 422 during upload as transient here
            if transient or code422:
                attempt += 1
                logger.warning(
                    f"Upload failed (attempt {attempt}/{UPLOAD_RETRY_MAX}) for {os.path.basename(file_path)}: {msg}"
                )
                backoff_sleep(attempt)
                continue
            # Non-transient: bail out immediately
            raise
    raise RuntimeError(f"Upload failed after {UPLOAD_RETRY_MAX} attempts: {last_err}")


def transcribe_single_file(api_key, file_path, cache_file, logger):
    """Transcribe one file with explicit upload + polling, guarded by per-key upload limiter."""
    cache = ProcessSafeCache(cache_file)
    try:
        transcript_file_path = os.path.splitext(file_path)[0] + "_diarized_content.json"

        if cache.is_processed(file_path):
            video_id = extract_video_id_from_path(file_path)
            logger.debug(f"Already processed: {video_id or os.path.basename(file_path)}")
            return {"status": "SKIPPED", "file": file_path}

        if not os.path.exists(file_path):
            logger.warning(f"File {file_path} not found.")
            return {"status": "FAILED", "file": file_path, "reason": "missing_file"}

        path_segments = file_path.split(os.sep)
        channel_name = path_segments[-3] if len(path_segments) >= 3 else "unknown"
        file_name = path_segments[-1]
        video_id = extract_video_id_from_path(file_path)

        logger.info(f"Starting diarization: [{channel_name}/{file_name or video_id}]")

        # ================
        # Upload (limited)
        # ================
        import assemblyai as aai
        config = aai.TranscriptionConfig(speaker_labels=True)
        transcriber = aai.Transcriber()

        # Acquire an upload slot for this API key before pushing bytes
        if not hasattr(transcribe_single_file, "_limiters"):
            transcribe_single_file._limiters = {}
        limiter = transcribe_single_file._limiters.setdefault(api_key, PerKeyLimiter(MAX_UPLOADS_PER_KEY))

        limiter.acquire()
        try:
            upload_start = time.time()
            audio_url = upload_with_retries(file_path, logger, api_key)
            logger.debug(f"Uploaded in {time.time() - upload_start:.1f}s -> {audio_url}")
        finally:
            limiter.release()

        # ======================
        # Kick off + wait safely
        # ======================
        start_time = time.time()

        # Newer SDKs: submit() returns a job handle; wait with a real timeout
        job = transcriber.submit(audio_url, config=config)
        try:
            transcript = job.wait_for_completion_async().result(timeout=TRANSCRIBE_TIMEOUT_SEC)
        except cf.TimeoutError:
            logger.error(
                f"Timed out after {TRANSCRIBE_TIMEOUT_SEC}s waiting for: "
                f"[{channel_name}/{file_name or video_id}]"
            )
            # best-effort cleanup
            try:
                if getattr(job, "id", None):
                    import assemblyai as aai
                    aai.Transcript.delete_by_id(job.id)
            except Exception:
                pass
            return {"status": "FAILED", "file": file_path, "reason": "timeout"}

        duration = time.time() - start_time

        if transcript is None:
            logger.error(f"Transcription returned None for: [{channel_name}/{file_name or video_id}]")
            return {"status": "FAILED", "file": file_path, "reason": "transcript_none"}

        status = getattr(transcript, "status", None)
        error_msg = getattr(transcript, "error", None)
        if status == "error" or error_msg:
            logger.error(f"AssemblyAI error for [{channel_name}/{file_name or video_id}]: {error_msg or status}")
            return {"status": "FAILED", "file": file_path, "reason": error_msg or status}

        utterances = getattr(transcript, "utterances", None)

        if not utterances:
            logger.warning(
                f"No utterances returned for [{channel_name}/{file_name or video_id}] "
                f"(diarization may have failed or found no speech)."
            )
            text = getattr(transcript, "text", "") or ""
            fallback = [{
                "text": text,
                "start": None,
                "end": None,
                "confidence": getattr(transcript, "confidence", None),
                "channel": None,
                "speaker": None,
                "words": []
            }]
            with open(transcript_file_path, 'w') as f:
                json.dump(fallback, f, indent=4)
            cache.mark_processed(file_path)
            logger.info(f"FALLBACK [{channel_name}/{file_name or video_id}] in {duration:.1f}s")
            return {"status": "FALLBACK", "file": file_path, "duration": duration, "reason": "no_utterances"}

        def safe_utterance_to_dict(u):
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

        utterances_dicts = [safe_utterance_to_dict(u) for u in utterances]
        with open(transcript_file_path, 'w') as f:
            json.dump(utterances_dicts, f, indent=4)

        cache.mark_processed(file_path)
        logger.info(f"SUCCESS [{channel_name}/{file_name or video_id}] in {duration:.1f}s")
        return {"status": "SUCCESS", "file": file_path, "duration": duration}

    except Exception as e:
        logger.error(f"Error transcribing {file_path}: {e}")
        return {"status": "FAILED", "file": file_path, "reason": str(e)}


def worker_with_backlog(api_key_index, api_key, file_paths, cache):
    """Worker that processes files with a maximum backlog"""
    set_api_key(api_key)
    logger = ThreadLogger.get_logger(api_key_index)

    total_files = len(file_paths)
    logger.info(f"Starting worker with {total_files} files to process")

    if total_files == 0:
        logger.info("No files assigned to this worker")
        return

    completed = {"SUCCESS": 0, "FALLBACK": 0, "SKIPPED": 0, "FAILED": 0}
    failed = 0

    # Process files in smaller batches to avoid saturating upload endpoints
    batch_size = min(MAX_CONCURRENT_TRANSCRIPTIONS, 3)  # safer default

    for i in range(0, total_files, batch_size):
        batch = file_paths[i:i + batch_size]
        batch_end = min(i + batch_size, total_files)

        logger.info(f"Processing batch {i // batch_size + 1}: files {i + 1}-{batch_end} of {total_files}")
        logger.info(f"Queue status: Completed={completed}, Failed={failed}, Remaining={total_files - i}")

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = []

            for file_path in batch:
                # Add small random delay between submissions
                random_sleep(0.1, 0.3)

                # Extract file info for logging
                path_segments = file_path.split(os.sep)
                channel_name = path_segments[-3] if len(path_segments) >= 3 else "unknown"
                video_id = extract_video_id_from_path(file_path)
                file_name = path_segments[-1]

                logger.debug(f"Submitting: [{channel_name}/{file_name or video_id}]")

                future = executor.submit(
                    transcribe_single_file,
                    api_key,
                    file_path,
                    cache.cache_file,  # pass a path, not the object
                    logger
                )

                futures.append((future, file_path))

            # Wait for batch to complete
            logger.info(f"Waiting for batch of {len(futures)} transcriptions to complete...")

            for future, file_path in futures:
                try:
                    # longer future timeout (transcription can legitimately take a while)
                    result = future.result(timeout=TRANSCRIBE_TIMEOUT_SEC + 120)
                    status = (result or {}).get("status", "FAILED")
                    completed[status] = completed.get(status, 0) + 1
                except Exception as e:
                    msg = str(e)
                    if "TimeoutError" in msg or "timeout" in msg.lower():
                        logger.warning(
                            f"Timeout waiting for {os.path.basename(file_path)}; retrying once synchronously"
                        )
                        # Retry once synchronously (no extra thread)
                        try:
                            result = transcribe_single_file(api_key, file_path, cache.cache_file, logger)
                            status = (result or {}).get("status", "FAILED")
                            completed[status] = completed.get(status, 0) + 1
                        except Exception as e2:
                            logger.error(f"Retry failed for {file_path}: {e2}")
                            completed["FAILED"] += 1
                    else:
                        logger.error(f"Batch processing error for {file_path}: {e}")
                        completed["FAILED"] += 1

            logger.info(
                f"Batch complete. Total: {sum(completed.values())}/{total_files} "
                f"(SUCCESS: {completed['SUCCESS']}, FALLBACK: {completed['FALLBACK']}, "
                f"SKIPPED: {completed['SKIPPED']}, FAILED: {completed['FAILED']})"
            )

            # Add delay between batches to avoid overwhelming the API
            if i + batch_size < total_files:
                wait_time = random.uniform(2, 5)
                logger.info(f"Waiting {wait_time:.1f}s before next batch...")
                time.sleep(wait_time)

    logger.info(f"Worker completed! Processed {sum(completed.values())} files "
                f"(SUCCESS: {completed['SUCCESS']}, FALLBACK: {completed['FALLBACK']}, "
                f"SKIPPED: {completed['SKIPPED']}, FAILED: {completed['FAILED']})")


def main():
    # Initialize cache
    cache = DiarizationCache()

    # Optional: Rebuild cache from disk if needed
    # cache.rebuild_from_disk()

    data_path = YOUTUBE_VIDEO_DIRECTORY

    # Get all MP3 files with the new naming pattern
    all_mp3_files = [
        os.path.join(root, file)
        for root, _, files in os.walk(data_path)
        for file in files
        if file.endswith(".mp3") and is_valid_filename(file)
    ]

    if not all_mp3_files:
        logging.warning("No MP3 files found to transcribe.")
        return

    logging.info(f"Found {len(all_mp3_files)} total MP3 files")

    # Filter to only unprocessed files using cache
    mp3_files = cache.get_unprocessed_files(all_mp3_files)

    if not mp3_files:
        logging.info("All files have been processed. Nothing to do.")
        return

    # Show some stats
    logging.info(f"Processing {len(mp3_files)} new files...")
    logging.info(f"Skipping {len(all_mp3_files) - len(mp3_files)} already processed files")
    logging.info(
        f"Using {len(api_keys)} API keys with max {MAX_CONCURRENT_TRANSCRIPTIONS} concurrent transcriptions each; "
        f"max uploads per key: {MAX_UPLOADS_PER_KEY}"
    )

    # Log some sample video IDs being processed
    sample_size = min(5, len(mp3_files))
    for i in range(sample_size):
        video_id = extract_video_id_from_path(mp3_files[i])
        logging.info(f"Sample file {i + 1}: Video ID = {video_id}, Path = {os.path.basename(mp3_files[i])}")

    # Shuffle files for better distribution
    random.shuffle(mp3_files)

    # Split files evenly among API keys
    files_per_key = len(mp3_files) // len(api_keys)
    remainder = len(mp3_files) % len(api_keys)

    file_chunks = []
    start_idx = 0

    for i in range(len(api_keys)):
        # Add one extra file to the first 'remainder' workers
        chunk_size = files_per_key + (1 if i < remainder else 0)
        file_chunks.append(mp3_files[start_idx:start_idx + chunk_size])
        start_idx += chunk_size

    # Log distribution
    for i, chunk in enumerate(file_chunks):
        logging.info(f"API Key {i}: {len(chunk)} files assigned")

    with ProcessPoolExecutor(max_workers=len(api_keys)) as executor:
        futures = [
            executor.submit(worker_with_backlog, i, api_key, file_chunks[i], cache)
            for i, api_key in enumerate(api_keys)
        ]

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Worker process failed: {e}")

    logging.info("All workers completed!")


if __name__ == "__main__":
    # Set up main logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - [MAIN] - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger()
    logger.addFilter(No200HTTPFilter())

    main()
