# run.py (top of file)
import os, json, asyncio, logging, pandas as pd
from typing import List, Optional
from dotenv import load_dotenv
from src import root_directory, YOUTUBE_VIDEO_DIRECTORY
from src.data_ingestion_youtube.load.utils import get_channel_id, get_video_info
from src.utils.utils import authenticate_service_account
# 🔽 change these two lines
from src.data_ingestion_youtube.load.download_mp3.config import Settings
from src.data_ingestion_youtube.load.download_mp3.batches import process_channel

async def process_channel_async(channel_id: str, channel_name: str,
                                credentials, youtube_videos_df: pd.DataFrame,
                                settings: Settings, cookie_file: Optional[str],
                                batch_size: int = 10):
    logging.info(f"Processing channel: {channel_name}")
    base_dir = YOUTUBE_VIDEO_DIRECTORY
    os.makedirs(base_dir, exist_ok=True)
    channel_dir = os.path.join(base_dir, channel_name)
    os.makedirs(channel_dir, exist_ok=True)

    video_info_list = get_video_info(credentials, os.environ.get("YOUTUBE_API_KEY"), channel_id)

    titles_in_csv = set(youtube_videos_df['title'].str.replace(' +', ' ', regex=True).str.replace('"', '', regex=False))
    filtered = [v for v in video_info_list if v.get('title') in titles_in_csv]
    videos = filtered if filtered else video_info_list

    await process_channel(channel_name, videos, channel_dir, batch_size, settings, cookie_file)

async def run(api_key: str,
              settings: Settings,
              yt_channels: Optional[List[str]] = None,
              yt_playlists: Optional[List[str]] = None,
              cookie_file: Optional[str] = None,
              batch_size: int = 10):
    load_dotenv()
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = authenticate_service_account(service_account_file) if service_account_file else None
    if credentials:
        logging.info("Service account file found. Proceeding with public channels, playlists, or private videos if accessible via Google Service Account.")
    else:
        logging.info("No service account file found. Proceeding with public channels or playlists.")

    mapping_path = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"
    channel_name_to_id = {}
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8') as f:
            channel_name_to_id = json.load(f)

    yt_id_name = {
        get_channel_id(credentials=credentials, api_key=api_key, channel_name=name,
                       channel_name_to_id=channel_name_to_id): name
        for name in (yt_channels or [])
    }

    videos_csv = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
    youtube_videos_df = pd.read_csv(videos_csv)

    await asyncio.gather(
        *(process_channel_async(cid, cname, credentials, youtube_videos_df, settings, cookie_file, batch_size)
          for cid, cname in yt_id_name.items())
    )


if __name__ == "__main__":
    import argparse, sys
    from pathlib import Path

    # --- make absolute imports work when running this file directly ---
    if __package__ is None:
        # add project root: <repo root>/ so `import src...` works
        here = Path(__file__).resolve()
        project_root = here.parents[3]  # .../youtube-transcript-pipeline/
        sys.path.insert(0, str(project_root))

    # --- absolute imports only (no leading dots) ---
    from src.data_ingestion_youtube.load.download_mp3.logging_setup import start_logging
    from src.data_ingestion_youtube.load.download_mp3.config import Settings
    from src.data_ingestion_youtube.load.download_mp3.cookies import setup_cookies
    from src import root_directory
    from dotenv import load_dotenv

    # logging
    try:
        start_logging("download_mp3s")
    except Exception:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s")

    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument('--api_key', type=str)
    parser.add_argument('--channels', nargs='+', type=str)
    parser.add_argument('--playlists', nargs='+', type=str)
    parser.add_argument('--batch_size', type=int, default=10)
    args = parser.parse_args()

    # fall back to channels file if none provided
    yt_channels = args.channels
    if not yt_channels:
        handles_file = Path(root_directory()) / "data/links/youtube/youtube_channel_handles.txt"
        if handles_file.exists():
            yt_channels = [c.strip() for c in handles_file.read_text().split(',') if c.strip()]
            logging.info(f"Loaded {len(yt_channels)} channel handle(s) from {handles_file}")
        else:
            logging.error("No --channels provided and channels file not found; nothing to do.")
            sys.exit(1)

    settings = Settings.from_env() if hasattr(Settings, "from_env") else Settings()

    if args.api_key:
        os.environ["YOUTUBE_API_KEY"] = args.api_key

    cookie_file = setup_cookies()

    asyncio.run(
        run(
            api_key=os.environ.get("YOUTUBE_API_KEY", ""),
            settings=settings,
            yt_channels=yt_channels,
            yt_playlists=args.playlists,
            cookie_file=cookie_file,
            batch_size=args.batch_size,
        )
    )
