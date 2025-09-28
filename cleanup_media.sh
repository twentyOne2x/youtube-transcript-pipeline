#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-$YOUTUBE_VIDEO_DIRECTORY}"
if [[ -z "${ROOT}" ]]; then
  echo "Usage: $0 <root-dir>"; exit 1
fi
python -m src.data_ingestion_youtube.load.download_mp3.cleanup \
  --root "$ROOT" --no-dry-run
