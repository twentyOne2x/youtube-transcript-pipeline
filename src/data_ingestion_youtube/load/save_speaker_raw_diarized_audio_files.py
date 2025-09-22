import json
import logging
import os
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from dotenv import load_dotenv
import pickle
from datetime import datetime

from src import YOUTUBE_VIDEO_DIRECTORY

load_dotenv()
api_keys = os.environ.get('ASSEMBLY_AI_API_KEYS')

if not api_keys:
    raise EnvironmentError("ASSEMBLY_AI_API_KEYS environment variable not found.")

api_keys = api_keys.split(',')

# Cache file path
CACHE_FILE = os.path.join(YOUTUBE_VIDEO_DIRECTORY, '.diarization_cache.pkl')


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


class No200HTTPFilter(logging.Filter):
    def filter(self, record):
        if "HTTP/1.1 200 OK" in record.getMessage():
            return False
        return True


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


def transcribe_and_save(api_key_file_path_cache):
    api_key, file_path, cache = api_key_file_path_cache
    set_api_key(api_key)
    random_sleep()

    try:
        transcript_file_path = os.path.splitext(file_path)[0] + "_diarized_content.json"

        # Double-check in case of race condition
        if cache.is_processed(file_path):
            logging.debug(f"Already processed: {os.path.basename(file_path)}")
            return

        if not os.path.exists(file_path):
            logging.warning(f"File {file_path} not found.")
            return

        # Extract channel and file info for logging
        path_segments = file_path.split('/')
        channel_name = path_segments[-3]
        file_name = path_segments[-1]

        logging.info(f"Diarization started for [{channel_name}/{file_name}]")

        import assemblyai as aai
        config = aai.TranscriptionConfig(speaker_labels=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(file_path, config=config)

        if transcript is None:
            logging.error(f"Transcription returned None for file: [{file_path}]")
            return

        utterances_dicts = [utterance_to_dict(utterance) for utterance in transcript.utterances]

        with open(transcript_file_path, 'w') as file:
            json.dump(utterances_dicts, file, indent=4)

        # Mark as processed in cache
        cache.mark_processed(file_path)

        transcript_file_name = os.path.basename(transcript_file_path)
        logging.info(f"Transcript for [{channel_name}/{file_name}] saved to [{channel_name}/{transcript_file_name}]")

    except Exception as e:
        logging.error(f"Error transcribing {file_path}: {e}")


def worker(api_key, file_paths, cache):
    set_api_key(api_key)
    with ThreadPoolExecutor(max_workers=3) as executor:
        api_key_file_paths = [(api_key, file_path, cache) for file_path in file_paths]
        list(executor.map(transcribe_and_save, api_key_file_paths))


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

    # Split files evenly among API keys
    files_per_key = len(mp3_files) // len(api_keys) + 1
    file_chunks = [mp3_files[i:i + files_per_key] for i in range(0, len(mp3_files), files_per_key)]

    # Ensure we don't have more chunks than API keys
    file_chunks = file_chunks[:len(api_keys)]

    with ProcessPoolExecutor(max_workers=len(api_keys)) as executor:
        futures = [
            executor.submit(worker, api_key, file_chunks[i] if i < len(file_chunks) else [], cache)
            for i, api_key in enumerate(api_keys)
        ]
        for future in futures:
            future.result()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger()
    logger.addFilter(No200HTTPFilter())

    main()