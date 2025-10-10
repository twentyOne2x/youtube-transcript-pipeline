import os
from pathlib import Path

def root_directory():
    """Get the root directory of the project"""
    return Path(__file__).parent.parent.absolute()

# Define the YouTube video directory (overridable via env for new pipeline stages)
YOUTUBE_VIDEO_DIRECTORY = os.environ.get(
    "YOUTUBE_VIDEO_DIRECTORY",
    str(root_directory() / "datasets" / "evaluation_data" / "diarized_youtube_content_2023-10-06/"),
)

# Base directory for Pump.fun livestream captures
PUMPFUN_STREAM_DIRECTORY = str(root_directory() / "datasets" / "evaluation_data" / "pumpfun_streams")

# Export for backwards compatibility
root_dir = root_directory()
