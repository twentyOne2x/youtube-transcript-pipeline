#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"

usage() {
  cat <<'USAGE'
Usage: run_diarization.sh [OPTIONS]

Runs src/data_ingestion_youtube/load/save_speaker_raw_diarized_audio_files.py.
The script requires the following environment variables:
  ASSEMBLY_AI_API_KEYS          (comma-separated; required)
  Optional runtime env vars:
    MAX_CONCURRENT_TRANSCRIPTIONS (default 10)
    MAX_UPLOADS_PER_KEY           (default 4)
    AAI_TRANSCRIBE_TIMEOUT_SEC    (default 1800)
    AAI_UPLOAD_RETRY_MAX          (default 6)
    AAI_BACKOFF_BASE              (default 1.8)
    AAI_BACKOFF_CAP               (default 60)
    AAI_UPLOAD_CHUNK_SIZE         (default 5 MiB)
    PRIORITIZE_SMALL_CHANNELS     (default true)
    GROUP_BY_YEAR                 (default false)
    CHANNELS_PER_PASS             (default 1)
    MAX_PER_CHANNEL               (default 0 = unlimited)
    AAI_ENABLE_ENTITIES           (default true)
    YOUTUBE_VIDEO_DIRECTORY       (defaults from src)

Any CLI arguments will be forwarded to the underlying Python script.
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

cd "$repo_root"
source .venv/bin/activate
export PYTHONPATH="$repo_root:${PYTHONPATH:-}"
exec python -m src.data_ingestion_youtube.load.save_speaker_raw_diarized_audio_files "$@"
