import sys, os, io, argparse, asyncio, logging
from dotenv import load_dotenv
from .config import Settings
from .cookies import setup_cookies
from .logging_setup import start_logging
from .run import run
from src import root_directory

def ensure_utf8_stdio():
    for stream_name in ("stdout", "stderr"):
        s = getattr(sys, stream_name)
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                wrapped = io.TextIOWrapper(getattr(s, "buffer", s), encoding="utf-8", errors="replace")
                setattr(sys, stream_name, wrapped)
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("LC_ALL", "en_US.UTF-8")
    os.environ.setdefault("LANG", "en_US.UTF-8")

def main():
    ensure_utf8_stdio()
    load_dotenv()
    start_logging("download_mp3s")

    try:
        import yt_dlp as ydlp
        logging.info(f"{sys.executable} yt-dlp {ydlp.version.__version__}")
    except Exception:
        pass

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        raise ValueError("No API key provided. Please provide an API key via command line argument or .env file.")

    parser = argparse.ArgumentParser(description='Batch download YouTube audio via yt-dlp with cookie/client rotation.')
    parser.add_argument('--api_key', type=str, help='YouTube Data API key (overrides .env)')
    parser.add_argument('--channels', nargs='+', type=str, help='YouTube channel names or IDs')
    parser.add_argument('--playlists', nargs='+', type=str, help='YouTube playlist IDs (unused here)')
    parser.add_argument('--batch_size', type=int, default=int(os.environ.get('BATCH_SIZE', '10')),
                        help='Items per batch per channel')
    args = parser.parse_args()

    try:
        cookie_file = setup_cookies()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    yt_channels = args.channels
    if not yt_channels:
        handles_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
        if os.path.exists(handles_file):
            with open(handles_file, 'r') as f:
                yt_channels = [c.strip() for c in f.read().split(',') if c.strip()]

    yt_playlists = args.playlists or os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists and isinstance(yt_playlists, str):
        yt_playlists = [p.strip() for p in yt_playlists.split(',') if p.strip()]

    if not yt_channels and not yt_playlists:
        raise ValueError("No channels or playlists provided.")

    key = args.api_key or api_key
    settings = Settings()
    asyncio.run(run(key, settings, yt_channels, yt_playlists, cookie_file, batch_size=args.batch_size))

if __name__ == '__main__':
    main()
