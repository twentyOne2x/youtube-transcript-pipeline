#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"

usage() {
  cat <<'USAGE'
Usage: run_download_mp3.sh [OPTIONS]

Runs python -m src.data_ingestion_youtube.load.download_mp3.run (via CLI wrapper).
Key inputs:
  YOUTUBE_API_KEY              (required unless --api_key supplied)
  data/links/youtube/youtube_channel_handles.txt  (default channel list)
  YOUTUBE_PLAYLISTS            (optional comma-separated playlist IDs)
  Optional cookie env vars:
    USE_BROWSER_COOKIES, BROWSER, PROFILE, etc.

CLI options forwarded to the downloader:
  --api_key KEY                Override API key
  --channels CHANNEL [...]     YouTube channel names/IDs
  --playlists PLAYLIST [...]   YouTube playlist IDs
  --batch_size N               Items per batch (default env BATCH_SIZE or 10)

Other behaviour is driven by download_mp3/config.py environment settings
(e.g., AUDIO_FORMAT, GLOBAL_MAX_DOWNLOADS, DEFAULT_CLIENT, etc.).
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

cd "$repo_root"
source .venv/bin/activate
export PYTHONPATH="$repo_root:${PYTHONPATH:-}"
exec python -m src.data_ingestion_youtube.load.download_mp3.run "$@"
