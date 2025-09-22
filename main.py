#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.index_youtube.fetch_youtube_video_details_from_handles import run as fetch_videos
from src.data_ingestion_youtube.load.download_mp3 import main as download_main
from src.data_ingestion_youtube.load.create_transcripts_from_raw_json_utterances import run as create_transcripts
from src.data_ingestion_youtube.load.load import load_video_transcripts

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='YouTube Transcript Pipeline')
    parser.add_argument('--action', choices=['fetch', 'download', 'transcribe', 'process', 'all'],
                        default='all', help='Which action to perform')

    args = parser.parse_args()

    if args.action in ['fetch', 'all']:
        print("Fetching YouTube video metadata...")
        fetch_videos()

    if args.action in ['download', 'all']:
        print("Downloading YouTube videos...")
        download_main()

    if args.action in ['transcribe', 'all']:
        print("Creating transcripts from raw JSON...")
        create_transcripts()

    if args.action in ['process', 'all']:
        print("Loading and processing video transcripts...")
        from pathlib import Path
        from src import YOUTUBE_VIDEO_DIRECTORY

        load_video_transcripts(directory_path=Path(YOUTUBE_VIDEO_DIRECTORY))