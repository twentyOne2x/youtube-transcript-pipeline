#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Provision and sync a Google Cloud Storage bucket for media artifacts.
#
# Requirements:
#   - gcloud CLI with the storage component installed.
#   - You are authenticated (`gcloud auth login` or service account).
#   - The target project is set via --project or gcloud config.
#
# Usage:
#   ./scripts/setup_storage_bucket.sh create --bucket my-bucket --location us-central1
#   ./scripts/setup_storage_bucket.sh sync   --bucket my-bucket --source datasets/evaluation_data/pumpfun_streams
#
# -----------------------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage:
  setup_storage_bucket.sh create --bucket BUCKET [--location REGION] [options]
  setup_storage_bucket.sh sync   --bucket BUCKET [--source PATH] [--prefix PREFIX]

Commands:
  create   Create/configure a bucket with secure defaults.
  sync     Rsync a local directory into the bucket.

Options:
  --project PROJECT_ID        Override gcloud project (optional).
  --bucket BUCKET_NAME        Target bucket (required).
  --location REGION           Bucket location (default: us-central1).
  --uniform / --no-uniform    Toggle uniform bucket-level access (default: enabled).
  --versioning                Enable object versioning.
  --retention-days N          Set object retention in days (omit for none).
  --make-public               Grant public read access (disabled by default).
  --source PATH               Local path to sync (default: datasets/evaluation_data/pumpfun_streams).
  --prefix PATH               Path prefix inside the bucket (default: pumpfun_streams).
  --exclude REGEX             Regex (gsutil -x) to skip files during sync.
  -h, --help                  Show this message.
EOF
}

command=""
project=""
bucket=""
location="us-central1"
uniform=true
versioning=false
make_public=false
retention_days=""
source_path="datasets/evaluation_data/pumpfun_streams"
prefix="pumpfun_streams"
exclude_regex=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    create|sync)
      command="$1"; shift ;;
    --project)
      project="$2"; shift 2 ;;
    --bucket)
      bucket="$2"; shift 2 ;;
    --location)
      location="$2"; shift 2 ;;
    --uniform)
      uniform=true; shift ;;
    --no-uniform)
      uniform=false; shift ;;
    --versioning)
      versioning=true; shift ;;
    --make-public)
      make_public=true; shift ;;
    --retention-days)
      retention_days="$2"; shift 2 ;;
    --source)
      source_path="$2"; shift 2 ;;
    --prefix)
      prefix="$2"; shift 2 ;;
    --exclude)
      exclude_regex="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage; exit 1 ;;
  esac
done

if [[ -z "${command}" ]]; then
  echo "Missing command (create | sync)." >&2
  usage
  exit 1
fi

if [[ -z "${bucket}" ]]; then
  echo "--bucket is required." >&2
  usage
  exit 1
fi

if [[ -n "${project}" ]]; then
  gcloud config set project "${project}" >/dev/null
fi

bucket_exists() {
  gcloud storage buckets describe "gs://${bucket}" >/dev/null 2>&1
}

if [[ "${command}" == "create" ]]; then
  if bucket_exists; then
    echo "Bucket gs://${bucket} already exists; applying updates."
  else
    echo "Creating bucket gs://${bucket} in ${location}..."
    gcloud storage buckets create "gs://${bucket}" --location "${location}"
  fi

  if [[ "${uniform}" == true ]]; then
    echo "Enabling uniform bucket-level access..."
    gcloud storage buckets update "gs://${bucket}" --uniform-bucket-level-access
  fi

  if [[ "${versioning}" == true ]]; then
    echo "Enabling object versioning..."
    gcloud storage buckets update "gs://${bucket}" --versioning
  fi

  if [[ -n "${retention_days}" ]]; then
    echo "Applying retention policy (${retention_days} day(s))..."
    gcloud storage buckets update "gs://${bucket}" --retention "${retention_days}d"
  fi

  if [[ "${make_public}" == true ]]; then
    echo "Granting allUsers storage.objectViewer (public read)."
    gcloud storage buckets add-iam-policy-binding "gs://${bucket}" \
      --member="allUsers" \
      --role="roles/storage.objectViewer"
  else
    echo "Bucket remains private (recommended)."
  fi

  echo "Bucket configuration complete."
  exit 0
fi

if [[ "${command}" == "sync" ]]; then
  if ! bucket_exists; then
    echo "Bucket gs://${bucket} does not exist. Run create first." >&2
    exit 1
  fi

  if [[ ! -d "${source_path}" ]]; then
    echo "Local source path '${source_path}' not found." >&2
    exit 1
  fi

  dest="gs://${bucket}/${prefix}"
  echo "Syncing ${source_path} -> ${dest}"
  if [[ -n "${exclude_regex}" ]]; then
    gsutil -m rsync -r -x "${exclude_regex}" "${source_path}" "${dest}"
  else
    gsutil -m rsync -r "${source_path}" "${dest}"
  fi
  echo "Sync complete."
  exit 0
fi
