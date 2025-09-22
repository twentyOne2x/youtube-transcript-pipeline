#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os

# Fix encoding BEFORE any other imports
if sys.version_info[0] >= 3:
    # Force UTF-8 for Python 3
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

# Set environment to UTF-8
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['LANG'] = 'en_US.UTF-8'

# Now do other imports
import asyncio
import subprocess
import time
import itertools
import json
import argparse
from typing import List, Optional, Dict
from dotenv import load_dotenv
import pandas as pd
import yt_dlp as ydlp
from yt_dlp import DownloadError
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from src import root_directory, YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.utils import get_channel_id, get_video_info
from src.utils.utils import authenticate_service_account, move_remaining_mp3_to_their_subdirs, \
    clean_fullwidth_characters, merge_directories, delete_mp3_if_text_or_json_exists, start_logging

import sys, os, io


def ensure_utf8_stdio():
    # Best effort to make stdout/stderr UTF-8 even if locale is Latin-1
    for stream_name in ("stdout", "stderr"):
        s = getattr(sys, stream_name)
        try:
            # Python 3.7+: reconfigure available on real text streams
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                # Fall back to wrapping the underlying buffer
                wrapped = io.TextIOWrapper(getattr(s, "buffer", s), encoding="utf-8", errors="replace")
                setattr(sys, stream_name, wrapped)
        except Exception:
            # Last resort: leave it as-is
            pass

    # Also hint to child processes and libraries
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")
    os.environ.setdefault("LANG", "en_US.UTF-8")

ensure_utf8_stdio()

load_dotenv()
api_key = os.environ.get('YOUTUBE_API_KEY')
if not api_key:
    raise ValueError("No API key provided. Please provide an API key via command line argument or .env file.")

# Force UTF-8 encoding - simplified approach
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Set environment variable for Python
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Load environment variables from the .env file
DOWNLOAD_AUDIO = os.environ.get('DOWNLOAD_AUDIO', 'True').lower() == 'true'

def start_logging(name: str, level=logging.INFO, log_dir="logs"):
    os.makedirs(log_dir, exist_ok=True)

    # Build a clean root logger
    root = logging.getLogger()
    root.setLevel(level)
    # Remove any pre-existing handlers (avoid duplicates on reload)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # Stream handler to stderr, force UTF-8 (with graceful fallback)
    try:
        sh = logging.StreamHandler(sys.stderr)
        if hasattr(sh.stream, "reconfigure"):
            sh.stream.reconfigure(encoding="utf-8", errors="replace")
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    except Exception:
        # Ultimate fallback: write to stdout
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # File handler, explicit UTF-8
    fh = logging.FileHandler(os.path.join(log_dir, f"{name}.log"), encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Quiet down noisy libs if desired
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

# Now start logging (this will set up its own handlers)
start_logging(f"download_mp3s")
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.WARNING)

def sanitize_filename(filename):
    """Remove or replace characters that might cause filesystem issues"""
    # Replace common problematic characters
    replacements = {
        ''': "'", ''': "'", '"': '', '"': '', '"': '',
        '—': '-', '–': '-', '…': '...',
        '：': '-', '|': '-', '\\': '-', '/': '-',
        '<': '', '>': '', '*': '', '?': ''
    }
    for old, new in replacements.items():
        filename = filename.replace(old, new)

    # Remove any remaining non-ASCII characters
    filename = unicodedata.normalize('NFKD', filename)
    filename = ''.join(c for c in filename if ord(c) < 128)

    return filename.strip()


def setup_cookies():
    """Setup YouTube cookies for yt-dlp - REQUIRED for downloads"""
    logging.info("Setting up YouTube cookies...")

    # Define the exact path you want to use
    cookie_paths = [
        'src/data_ingestion_youtube/load/youtube_cookies.txt',
        os.path.join(root_directory(), 'src/data_ingestion_youtube/load/youtube_cookies.txt'),
        'youtube_cookies.txt',
        os.path.join(root_directory(), 'youtube_cookies.txt'),
    ]

    # Check each path
    for cookie_file in cookie_paths:
        if os.path.exists(cookie_file):
            logging.info(f"✓ Found cookie file at: {sanitize_filename(cookie_file)}")
            # Verify it's not empty
            if os.path.getsize(cookie_file) > 0:
                return os.path.abspath(cookie_file)  # Return absolute path
            else:
                logging.error(f"Cookie file is empty: {cookie_file}")

    # If we get here, no valid cookie file was found - CRASH
    error_msg = f"""
    ❌ CRITICAL ERROR: No valid YouTube cookie file found!

    Searched in these locations:
    {chr(10).join(f'  - {path}' for path in cookie_paths)}

    To fix this:
    1. Export cookies from your browser using a cookies.txt extension
    2. Save the file as 'youtube_cookies.txt' 
    3. Place it in: {cookie_paths[0]}

    See: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp
    """
    logging.error(error_msg)
    raise FileNotFoundError(error_msg)


def chunked_iterable(iterable, size):
    """Splits an iterable into chunks of a specified size."""
    iterator = iter(iterable)
    while True:
        chunk = list(itertools.islice(iterator, size))
        if not chunk:
            break
        yield chunk


def get_ydl_opts(video_dir_path: str, video_title: str, cookie_file: str | None = None) -> dict:
    use_cookies_file = bool(cookie_file and os.path.exists(cookie_file))

    player_clients = ['web'] if use_cookies_file else ['android', 'web']

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': f'{video_dir_path}/{video_title}.%(ext)s',
        'quiet': False,
        'no_warnings': False,
        'retries': 10,
        'fragment_retries': 10,
        'concurrent_fragments': 5,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'referer': 'https://www.youtube.com/',
        'extractor_args': {'youtube': {'player_client': player_clients}},
        # Prefer a cookies file; if you sometimes want to run without a file, add the fallback below
        'cookiefile': cookie_file if use_cookies_file else None,
        # Optional: fallback to Brave profile if no file (comment out if you don't want runtime browser access)
        # 'cookiesfrombrowser': ('brave', 'Default', None) if not use_cookies_file else None,
        'progress_with_newline': True,  # nicer logs
        'noprogress': False,
        # 'keepvideo': False,  # default; you already see it deleting the .mp4 after extracting audio
    }
    # remove None keys to avoid confusing yt-dlp
    ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}

    logging.debug(f"yt-dlp options configured. Cookie file: {cookie_file if use_cookies_file else 'none'}; clients={player_clients}")
    return ydl_opts

def download_video(url: str, ydl_opts: dict, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            with ydlp.YoutubeDL(ydl_opts) as ydl:
                # Check if already downloaded
                info = ydl.extract_info(url, download=False)
                filename = ydl.prepare_filename(info)
                if os.path.exists(filename.replace('.webm', '.mp3').replace('.m4a', '.mp3')):
                    logging.info(f"Already exists: {filename}")
                    return True

                # Download
                ydl.download([url])
                return True

        except DownloadError as e:
            if "Video unavailable" in str(e):
                logging.error(f"Video unavailable: {url}")
                return False
            elif "429" in str(e):
                wait_time = (attempt + 1) * 30
                logging.warning(f"Rate limited. Waiting {wait_time}s...")
                time.sleep(wait_time)
            else:
                logging.warning(f"Attempt {attempt + 1} failed: {e}")

    return False

async def download_audio_batch(video_infos: List[dict], base_dir: str, cookie_file: str = None):
    """
    Download a batch of videos in parallel
    """
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=5)  # Reduced workers to avoid rate limits

    futures = []



    for info in video_infos:
        # Create individual directories and options for each video
        video_title = sanitize_filename(info['title']).replace('/', '_')
        published_at = info.get('published_date', '')[:10]  # yyyy-mm-dd format
        video_title_with_date = f"{published_at}_{video_title}" if published_at else video_title

        video_dir_path = os.path.join(base_dir, video_title_with_date)
        os.makedirs(video_dir_path, exist_ok=True)

        ydl_opts = get_ydl_opts(video_dir_path, video_title_with_date, cookie_file)

        future = loop.run_in_executor(
            executor,
            download_video,
            info['url'],
            ydl_opts
        )
        futures.append(future)

    results = await asyncio.gather(*futures, return_exceptions=True)

    success_count = sum(1 for r in results if r is True)
    logging.info(f"Batch complete: {success_count}/{len(video_infos)} downloaded successfully")

def filter_videos_in_dataframe(video_info_list, youtube_videos_df):
    # Normalize titles in the dataframe
    youtube_videos_df['title'] = youtube_videos_df['title'].str.replace(' +', ' ', regex=True).str.replace('"', '', regex=False)

    # Extract titles from the video info list
    titles = [video['title'] for video in video_info_list]

    # Create a mask for videos that are in the DataFrame
    mask = youtube_videos_df['title'].isin(titles)

    # Get the titles present in the DataFrame
    titles_in_df = youtube_videos_df[mask]

    # Filter and return videos that are in DataFrame
    return titles_in_df


async def video_valid_for_processing(channel_name, video_title, dir_path):
    try:
        normalized_video_title = video_title.replace('/', '_')
        titles_to_avoid = ['livestream', 'live stream', 'live']
        for title in titles_to_avoid:
            if title in normalized_video_title.lower():
                return False

        # Function to check for the title's existence in files
        def title_exists_in_files(directory, suffix):
            for filename in os.listdir(directory):
                if normalized_video_title in filename and filename.endswith(suffix):
                    return True
            return False

        # Recursively check in dir_path and its subdirectories
        for root, dirs, files in os.walk(dir_path):
            if (
                    title_exists_in_files(root, ".mp3") or
                    title_exists_in_files(root, "_diarized_content.json") or
                    title_exists_in_files(root, "_diarized_content_processed_diarized.txt") or
                    title_exists_in_files(root, "_content_processed_diarized.txt")
            ):
                # logging.info(f"video_valid_for_processing: {video_title} is already processed")
                return False
        logging.info(f"[{channel_name}] video_valid_for_processing: [{sanitize_filename(video_title)}] is not processed yet, adding to the list!")
        return True
    except Exception as e:
        logging.warning(f"Exception in video_valid_for_processing: {e}")
        return False


async def prepare_download_info(video_info, dir_path, video_title):
    strlen = len("yyyy-mm-dd")
    published_at = video_info['publishedAt'].replace(':', '-').replace('.', '-')[:strlen]
    video_title = f"{published_at}_{video_title}"

    # Create a specific directory for this video if it doesn't exist
    video_dir_path = os.path.join(dir_path, video_title)
    if not os.path.exists(video_dir_path):
        os.makedirs(video_dir_path)

    audio_file_path = os.path.join(video_dir_path, f'{video_title}.mp3')
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': f'{video_dir_path}/{video_title}.%(ext)s',
    }
    return ydl_opts, audio_file_path


async def process_video_batches(channel_name: str, video_info_list: List[dict],
                                dir_path: str, youtube_videos_df, batch_size: int = 10, cookie_file: str = None):
    """
    Process videos in smaller batches to avoid rate limiting
    """
    # Filter and validate videos (keep your existing logic)
    filtered_videos = filter_videos_in_dataframe(video_info_list, youtube_videos_df)

    valid_videos = []
    for index, row in filtered_videos.iterrows():
        video_dict = row.to_dict()
        is_valid = await video_valid_for_processing(channel_name, video_dict['title'], dir_path)
        if is_valid:
            valid_videos.append(video_dict)

    if not valid_videos:
        logging.info(f"[{channel_name}] No new videos to download")
        return

    logging.info(f"[{channel_name}] Downloading {len(valid_videos)} videos")

    # Process in smaller batches with delays
    for i in range(0, len(valid_videos), batch_size):
        batch = valid_videos[i:i + batch_size]
        logging.info(f"[{channel_name}] Processing batch {i // batch_size + 1}")

        await download_audio_batch(batch, dir_path, cookie_file)

        # Add delay between batches to avoid rate limiting
        if i + batch_size < len(valid_videos):
            await asyncio.sleep(5)


async def process_video_batches_async(channel_id, channel_name, credentials, youtube_videos_df, cookie_file: str = None):
    logging.info(f"Processing channel: {channel_name}")
    dir_path = YOUTUBE_VIDEO_DIRECTORY
    # Get video information from the channel
    video_info_list = get_video_info(credentials, api_key, channel_id)

    # Create a 'data' directory if it does not exist
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    # Create a subdirectory for the current channel if it does not exist
    dir_path = os.path.join(dir_path, channel_name)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    await process_video_batches(channel_name, video_info_list, dir_path, youtube_videos_df, cookie_file=cookie_file)


async def run(api_key: str, yt_channels: Optional[List[str]] = None, yt_playlists: Optional[List[str]] = None, cookie_file: str = None):
    """
    Run function that takes a YouTube Data API key and a list of YouTube channel names, fetches video transcripts,
    and saves them as .txt files in a data directory.

    Args:
        yt_playlists:
        api_key (str): Your YouTube Data API key.
        yt_channels (List[str]): A list of YouTube channel names.
    """
    clean_mp3s()
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = None

    if service_account_file:
        credentials = authenticate_service_account(service_account_file)
        logging.info("Service account file found. Proceeding with public channels, playlists, or private videos if accessible via Google Service Account.")
    else:
        logging.info("No service account file found. Proceeding with public channels or playlists.")

    # Create a dictionary with channel IDs as keys and channel names as values
    # Define the path for storing the mapping between channel names and their IDs
    channel_mapping_filepath = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"

    # Load existing mappings if the file exists, or initialize an empty dictionary
    channel_name_to_id = {}  # Initialize regardless
    if os.path.exists(channel_mapping_filepath):
        with open(channel_mapping_filepath, 'r', encoding='utf-8') as file:
            channel_name_to_id = json.load(file)

    yt_id_name = {get_channel_id(credentials=credentials, api_key=api_key, channel_name=name, channel_name_to_id=channel_name_to_id): name for name in yt_channels}

    videos_path = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
    youtube_videos_df = pd.read_csv(videos_path)

    # Iterate through the dictionary of channel IDs and channel names
    await asyncio.gather(*(process_video_batches_async(channel_id, channel_name, credentials, youtube_videos_df, cookie_file)
                           for channel_id, channel_name in yt_id_name.items()))

    # Iterate through the dictionary of channel IDs and channel names

    # if yt_playlists:
    #     await asyncio.gather(*(process_video_batches_async(channel_id, channel_name, credentials, youtube_videos_df)
    #                            for channel_id, channel_name in yt_id_name.items()))
    #     for playlist_id in yt_playlists:
    #         playlist_title = get_playlist_title(credentials, api_key, playlist_id)
    #         # Ensure the title is filesystem-friendly (replacing slashes, for example)
    #         playlist_title = playlist_title.replace('/', '_') if playlist_title else f"playlist_{playlist_id}"
#
    #         video_info_list = get_videos_from_playlist(credentials, api_key, playlist_id)
#
    #         if not os.path.exists(dir_path):
    #             os.makedirs(dir_path)
#
    #         dir_path += f'/{playlist_title}'
    #         if not os.path.exists(dir_path):
    #             os.makedirs(dir_path)
#
    #         await process_video_batches(channel_name, video_info_list, dir_path, youtube_videos_df)

    # clean up because downloaded file names have full-width characters instead of ASCII
    clean_mp3s()


def clean_mp3s():
    directory = f"{root_directory()}/datasets/evaluation_data/diarized_youtube_content_2023-10-06"
    clean_fullwidth_characters(directory)
    move_remaining_mp3_to_their_subdirs()
    merge_directories(directory)
    delete_mp3_if_text_or_json_exists(directory)


def get_youtube_channels_from_file(file_path):
    with open(file_path, 'r') as file:
        channels = file.read().split(',')
    return channels


def main():
    parser = argparse.ArgumentParser(description='Fetch YouTube video transcripts.')
    parser.add_argument('--api_key', type=str, help='YouTube Data API key')
    parser.add_argument('--channels', nargs='+', type=str, help='YouTube channel names or IDs')
    parser.add_argument('--playlists', nargs='+', type=str, help='YouTube playlist IDs')

    args = parser.parse_args()

    # Setup cookies FIRST and crash if not found
    try:
        cookie_file = setup_cookies()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)  # Exit with error code

    yt_channels_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
    yt_channels = get_youtube_channels_from_file(yt_channels_file)

    yt_playlists = args.playlists or os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists:
        yt_playlists = [playlist.strip() for playlist in yt_playlists.split(',')]

    if not yt_channels and not yt_playlists:
        raise ValueError("No channels or playlists provided.")

    asyncio.run(run(api_key, yt_channels, yt_playlists, cookie_file))

if __name__ == '__main__':
    main()

