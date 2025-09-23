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

from src import YOUTUBE_VIDEO_DIRECTORY

load_dotenv()
api_keys = os.environ.get('ASSEMBLY_AI_API_KEYS')

if not api_keys:
    raise EnvironmentError("ASSEMBLY_AI_API_KEYS environment variable not found.")

api_keys = api_keys.split(',')

# Cache file path
CACHE_FILE = os.path.join(YOUTUBE_VIDEO_DIRECTORY, '.diarization_cache.pkl')

# Maximum concurrent transcriptions per API key
MAX_CONCURRENT_TRANSCRIPTIONS = 5


class DiarizationCache:
    """Simple cache to track processed files"""

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self.processed_files = self.load_cache()

    def load_cache(self):
        """Load cache from disk"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                    logging.info(f"Loaded cache with {len(cache_data)} processed files")
                    return cache_data
            except Exception as e:
                logging.warning(f"Could not load cache: {e}. Starting fresh.")
                return set()
        return set()

    def save_cache(self):
        """Save cache to disk"""
        try:
            with open(self.cache_file, 'wb') as f:
                pickle.dump(self.processed_files, f)
            logging.debug(f"Cache saved with {len(self.processed_files)} entries")
        except Exception as e:
            logging.error(f"Could not save cache: {e}")

    def is_processed(self, file_path):
        """Check if file has been processed"""
        # Also check if the actual JSON file exists
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        return file_path in self.processed_files or os.path.exists(transcript_file)

    def mark_processed(self, file_path):
        """Mark file as processed"""
        self.processed_files.add(file_path)
        self.save_cache()

    def get_unprocessed_files(self, file_list):
        """Filter list to only unprocessed files"""
        unprocessed = []
        for file_path in file_list:
            if not self.is_processed(file_path):
                unprocessed.append(file_path)

        logging.info(f"Found {len(unprocessed)} unprocessed files out of {len(file_list)} total")
        return unprocessed

    def rebuild_from_disk(self):
        """Rebuild cache by scanning for existing JSON files"""
        logging.info("Rebuilding cache from existing diarized files...")
        self.processed_files = set()

        for root, _, files in os.walk(YOUTUBE_VIDEO_DIRECTORY):
            for file in files:
                if file.endswith("_diarized_content.json"):
                    # Reconstruct the original mp3 path
                    mp3_path = os.path.join(root, file.replace("_diarized_content.json", ".mp3"))
                    self.processed_files.add(mp3_path)

        self.save_cache()
        logging.info(f"Cache rebuilt with {len(self.processed_files)} processed files")


class ProcessSafeCache:
    """Cache wrapper for use in worker processes"""

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._lock = threading.Lock()

    def is_processed(self, file_path):
        """Check if file has been processed by checking the filesystem"""
        transcript_file = os.path.splitext(file_path)[0] + "_diarized_content.json"
        return os.path.exists(transcript_file)

    def mark_processed(self, file_path):
        """Mark file as processed by updating the cache file"""
        with self._lock:
            # Load current cache
            processed_files = set()
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, 'rb') as f:
                        processed_files = pickle.load(f)
                except:
                    pass

            # Add new file
            processed_files.add(file_path)

            # Save updated cache
            try:
                with open(self.cache_file, 'wb') as f:
                    pickle.dump(processed_files, f)
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
    return re.match(r'^\d{4}-\d{2}-\d{2}_', filename)


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


def transcribe_single_file(api_key, file_path, cache_file, logger):
    cache = ProcessSafeCache(cache_file)
    try:
        transcript_file_path = os.path.splitext(file_path)[0] + "_diarized_content.json"

        if cache.is_processed(file_path):
            logger.debug(f"Already processed: {os.path.basename(file_path)}")
            return {"status": "SKIPPED", "file": file_path}

        if not os.path.exists(file_path):
            logger.warning(f"File {file_path} not found.")
            return {"status": "FAILED", "file": file_path, "reason": "missing_file"}

        path_segments = file_path.split(os.sep)
        channel_name = path_segments[-3] if len(path_segments) >= 3 else "unknown"
        file_name = path_segments[-1]

        logger.info(f"Starting diarization: [{channel_name}/{file_name}]")

        import assemblyai as aai
        config = aai.TranscriptionConfig(speaker_labels=True)
        transcriber = aai.Transcriber()

        start_time = time.time()
        transcript = transcriber.transcribe(file_path, config=config)
        duration = time.time() - start_time

        if transcript is None:
            logger.error(f"Transcription returned None for: [{channel_name}/{file_name}]")
            return {"status": "FAILED", "file": file_path, "reason": "transcript_none"}

        status = getattr(transcript, "status", None)
        error_msg = getattr(transcript, "error", None)
        if status == "error" or error_msg:
            logger.error(f"AssemblyAI error for [{channel_name}/{file_name}]: {error_msg or status}")
            return {"status": "FAILED", "file": file_path, "reason": error_msg or status}

        utterances = getattr(transcript, "utterances", None)

        if not utterances:
            logger.warning(f"No utterances returned for [{channel_name}/{file_name}] "
                           f"(diarization may have failed or found no speech).")
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
            logger.info(f"FALLBACK [{channel_name}/{file_name}] in {duration:.1f}s")
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
        logger.info(f"SUCCESS [{channel_name}/{file_name}] in {duration:.1f}s")
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

    # Process files in batches
    batch_size = MAX_CONCURRENT_TRANSCRIPTIONS

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
                path_segments = file_path.split('/')
                channel_name = path_segments[-3] if len(path_segments) >= 3 else "unknown"
                file_name = path_segments[-1]

                logger.debug(f"Submitting: [{channel_name}/{file_name}]")

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
                    result = future.result(timeout=600)
                    status = (result or {}).get("status", "FAILED")
                    completed[status] = completed.get(status, 0) + 1
                except Exception as e:
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

    logger.info(f"Worker completed! Processed {completed + failed} files "
                f"(Success: {completed}, Failed: {failed})")


def main():
    # Initialize cache
    cache = DiarizationCache()

    # Optional: Rebuild cache from disk if needed
    # cache.rebuild_from_disk()

    data_path = YOUTUBE_VIDEO_DIRECTORY

    # Get all MP3 files
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
        f"Using {len(api_keys)} API keys with max {MAX_CONCURRENT_TRANSCRIPTIONS} concurrent transcriptions each")

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