#!/usr/bin/env bash
set -euo pipefail

project=""
bucket=""
source_path=""
prefix="youtube_audio"
gcloud_bin="../rag/google-cloud-sdk/bin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      project="$2"; shift 2 ;;
    --bucket)
      bucket="$2"; shift 2 ;;
    --source)
      source_path="$2"; shift 2 ;;
    --prefix)
      prefix="$2"; shift 2 ;;
    --gcloud-bin)
      gcloud_bin="$2"; shift 2 ;;
    -h|--help)
      cat <<'USAGE'
Usage: sync_youtube_to_gcs.sh --project PROJECT --bucket BUCKET --source PATH [--prefix PREFIX] [--gcloud-bin PATH]

Uploads in two passes:
  1) metadata (skip mp3/mp4)
  2) audio/video (skip json)
USAGE
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1 ;;
  esac
done

if [[ -z "$project" || -z "$bucket" || -z "$source_path" ]]; then
  echo "--project, --bucket, and --source are required." >&2
  exit 1
fi

if [[ ! -d "$source_path" ]]; then
  echo "Source directory '$source_path' not found." >&2
  exit 1
fi

export PATH="$gcloud_bin:$PATH"

mkdir -p uploads
log_stamp=$(date +%Y%m%d_%H%M%S)

cmd_base=( ./scripts/setup_storage_bucket.sh sync \
  --project "$project" \
  --bucket "$bucket" \
  --source "$source_path" \
  --prefix "$prefix" )

echo "[1/2] Uploading metadata (.json)"
"${cmd_base[@]}" --exclude '.*\.(mp3|mp4)$' | tee "uploads/youtube_metadata_${log_stamp}.log"

echo "[2/2] Uploading audio/video (.mp3/.mp4)"
"${cmd_base[@]}" --exclude '.*\.json$' | tee "uploads/youtube_media_${log_stamp}.log"

echo "Uploads complete. Logs saved in uploads/."
