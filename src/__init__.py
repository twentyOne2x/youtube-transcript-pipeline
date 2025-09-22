import os
from pathlib import Path

def root_directory():
    """Get the root directory of the project"""
    return Path(__file__).parent.parent.absolute()

# Define the YouTube video directory
YOUTUBE_VIDEO_DIRECTORY = str(root_directory() / "datasets" / "evaluation_data" / "diarized_youtube_content_2023-10-06/")

# Export for backwards compatibility
root_dir = root_directory()

