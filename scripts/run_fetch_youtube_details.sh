#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"

usage() {
  cat <<'USAGE'
Usage: run_fetch_youtube_details.sh [OPTIONS]

Runs src/index_youtube/fetch_youtube_video_details_from_handles.py.
Configuration is primarily via environment variables and CSV inputs.

Important env vars / inputs:
  YOUTUBE_API_KEY             (API key; required unless passed via CLI)
  SERVICE_ACCOUNT_FILE        (optional, for private videos)
  data/links/youtube/youtube_channel_handles.txt (comma-separated handles)
  YOUTUBE_PLAYLISTS           (optional, comma-separated playlist IDs)
  YOUTUBE_CHANNEL_HANDLES     (optional override; comma-separated)
  YOUTUBE_VIDEO_IDS           (optional, pre-fetch specific video IDs)

The underlying script accepts CLI flags (see python ... --help):
  --api_key KEY               Override API key
  --handles HANDLE [HANDLE...]  Channels/handles to fetch
  --playlists PLAYLIST [...]  Playlist IDs
  --reset-mappings            Reset cached channel mappings
  --keywords KW [...]         Include only titles containing keywords
  --keywords_to_exclude KW [...]  Exclude titles containing keywords
  --output PATH               Override CSV output path

Outputs:
  data/links/youtube/youtube_videos.csv (aggregated video list)
  Logs emitted via logging module.
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

cd "$repo_root"
source .venv/bin/activate
export PYTHONPATH="$repo_root:${PYTHONPATH:-}"
exec python -m src.index_youtube.fetch_youtube_video_details_from_handles "$@"
