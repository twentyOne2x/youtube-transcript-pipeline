#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.index_youtube.fetch_youtube_video_details_from_handles import run as fetch_videos
from src.data_ingestion_youtube.load.download_mp3 import main as download_main
from src.data_ingestion_youtube.load.save_speaker_raw_diarized_audio_files import main as diarize_audio

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='YouTube Transcript Pipeline')
    parser.add_argument('--action', choices=['fetch', 'download', 'diarize', 'process', 'all'],
                        default='all', help='Which action to perform')

    args = parser.parse_args()

    if args.action in ['fetch', 'all']:
        print("Fetching YouTube video metadata...")
        fetch_videos()

    if args.action in ['download', 'all']:
        print("Downloading YouTube videos as MP3...")
        download_main()

    if args.action in ['diarize', 'all']:
        print("Diarizing audio files (MP3 to JSON)...")
        diarize_audio()  # This creates _diarized_content.json files
