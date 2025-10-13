# YouTube Audio → Diarized Transcripts Pipeline

Batch-download YouTube audio (MP3) with resilient cookie/client rotation, then generate **speaker-aware** transcripts (and optional entity extraction) with AssemblyAI. The pipeline plans work up front, runs adaptively with global concurrency caps, and writes clean sidecars next to each audio file.

---

## Executive Summary

Since the first commit this project has grown into a fully event-driven ingestion stack:

- Cloud Run services handle webhooks, metadata enrichment, downloads, diarization, and warehouse fan-out via Pub/Sub topics (`yt-new-video`, `mp3-ready`, `diarization-ready`, `yt-cookie-request`, ...).
- Pump.fun livestream clipping and Binance Academy course pipelines reuse the same architecture and storage layout.
- `scripts/fetch_youtube_cookies.py` automates cookie export, Secret Manager upload, and Cloud Run redeploy; downloader failures publish `yt-cookie-request` events so the Telegram bot can prompt operators.
- Telegram notifications now cover every stage and expose `/runs`, `/gce`, `/cost_*`, `/help` commands.
- Tests span schema validation, service endpoints, cookie workflow, pumpfun/binance downloaders, and helper utilities.

See the appendix for a detailed comparison between the first commit and the current codebase.

---

## Features

- **Planner first:** prints a concise plan and saves CSVs of what will be downloaded/transcribed.
- **Robust downloads:** yt-dlp with cookie export (from your browser), client rotation, 416/429 handling, and safe filenames.
- **Predictable layout:** `.../<channel>/<YYYY-MM-DD>_<videoId>_<title>/<same>.mp3`
- **Diarization + entities:** writes `_diarized_content.json` and `_entities.json` next to each MP3.
- **Idempotent:** skips items that already exist or have valid sidecars; persistent cache for transcripts.
- **Adaptive concurrency:** global semaphore auto-tunes based on success/error rates.
- **Clear progress:** plan tables + global counters; CSVs land in `data/links/youtube/`.
- **Pump.fun support:** download archived livestream clips as MP4/MP3 into a parallel directory tree.
- **Binance Academy support:** crawl Learn & Earn courses, pull hosted videos, and store alongside structured metadata.
- **Event-driven deployment:** Cloud Run + Pub/Sub services emit Telegram notifications (`/runs`, `/gce`, `/cost*`, cookie prompts) so operations stay visible.

---

## Quickstart

### 1) Requirements

- **Python 3.12** (recommended)
- **FFmpeg** in your PATH  
- A Chromium-based browser (Brave/Chrome/Chromium) with a profile that is **logged in to YouTube** (for age-restricted/private videos, if you have access)
- API keys:
  - **YouTube Data API** (for video discovery)
  - **AssemblyAI** (one or more; comma-separated) for transcription

### 2) Install

```bash
git clone <this-repo>
cd <this-repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3) Create .env

```bash
# --- YouTube discovery ---
YOUTUBE_API_KEY=YOUR_YT_DATA_API_KEY

# --- AssemblyAI (comma-separated for parallel workers) ---
ASSEMBLY_AI_API_KEYS=key1,key2,key3

# --- Download behavior (see config.Settings for more) ---
BATCH_SIZE=10
GLOBAL_MAX_DOWNLOADS=4                 # initial global concurrency (auto-tunes)
FFMPEG_THREADS=1

# --- Cookies export control ---
BROWSER=brave                          # brave | chrome | chromium
PROFILE=Default                        # browser profile name
USE_COOKIE_CACHE=1
MAX_COOKIE_AGE_DAYS=7
COOKIE_ATTEMPT_ORDER=keyring,plain     # try keyring first, then "plain" extraction
COOKIE_EXPORT_TIMEOUT=20

# --- Planning / prioritization (download + transcription) ---
PRIORITIZE_SMALL_CHANNELS=1
GROUP_BY_YEAR=0
CHANNELS_PER_PASS=1
MAX_PER_CHANNEL=0                      # 0 = unlimited

# --- AssemblyAI tuning ---
AAI_MAX_CONCURRENT_TRANSCRIPTIONS=10
AAI_MAX_UPLOADS_PER_KEY=4
AAI_TRANSCRIBE_TIMEOUT_SEC=1800
AAI_UPLOAD_RETRY_MAX=6
AAI_ENABLE_ENTITIES=1                  # 1=save _entities.json
```

The location for downloads is defined by `YOUTUBE_VIDEO_DIRECTORY` in `src/__init__.py`.

### 4) Provide channel handles (optional)

Create `data/links/youtube/youtube_channel_handles.txt` with comma-separated handles or channel IDs, e.g.:

```
@Computerphile,@3Blue1Brown,UC_x5XG1OV2P6uZZ5FSM9Ttw
```

You can also pass channels via `--channels` at runtime.

## Usage

### A) Download MP3s (simple CLI)

```bash
# Uses yt-dlp under the hood + cookie exporter
python -m src.data_ingestion_youtube.load.download_mp3.cli \
  --channels @YourFavChannel @AnotherOne \
  --batch_size 10
```

If `--channels` is omitted, it falls back to `data/links/youtube/youtube_channel_handles.txt`.

**Outputs:**
- Per-channel folders under `YOUTUBE_VIDEO_DIRECTORY`:
  ```
  <YOUTUBE_VIDEO_DIRECTORY>/<channel>/<YYYY-MM-DD>_<videoId>_<title>/<same>.mp3
  ```
- Plan CSV: `data/links/youtube/planned_downloads_summary.csv`
- Console plan table: newest → oldest, fewest missing channels first

### B) Download MP3s (advanced runner with prioritization)

```bash
python -m src.data_ingestion_youtube.load.download_mp3.run \
  --api_key "$YOUTUBE_API_KEY" \
  --channels @YourFavChannel @AnotherOne \
  --batch_size 10 \
  --group-by-year \
  --channels-per-pass 2
```

**Flags:**
- `--no-prioritize-small-channels` – process channels alphabetically instead
- `--group-by-year` – process 2025 first, then 2024, then unknown
- `--channels-per-pass N` – run several small channels together per wave

### C) Transcribe (speaker diarization + entities)

```bash
python -m src.data_ingestion_youtube.load.save_speaker_raw_diarized_audio_files
```

**What it does:**
- Scans all MP3s under `YOUTUBE_VIDEO_DIRECTORY` (expects filenames like `YYYY-MM-DD_<id>_<title>.mp3`)
- Skips items that already have both valid sidecars:
  - `_diarized_content.json`
  - `_entities.json` (if `AAI_ENABLE_ENTITIES=1`)
- Parallelizes by API key (round-robin assignment); per-key upload rate is limited
- Writes plan CSV: `data/links/youtube/planned_transcriptions_summary.csv`

**Sidecar examples** (saved next to the MP3):
- `..._diarized_content.json` – array of utterances with `{text, start, end, confidence, speaker, words:[...]}`
- `..._entities.json` – `{ video_file, entities:[...], entities_with_speakers:[...] }`

A persistent cache lives at: `<YOUTUBE_VIDEO_DIRECTORY>/.diarization_cache.pkl`

## Pump.fun livestream downloads

Download archived Pump.fun livestream clips into `datasets/evaluation_data/pumpfun_streams/<coin-name>_<mint-prefix>/...`.

```bash
# Example: grab the newest clip for a mint address
.venv/bin/python -m src.data_ingestion_pumpfun.cli \
  --rooms G278EULAmdbd3rUrUhrKX6zgs1NS68XdhLMsG1s5pump \
  --max-clips 3
```

For recurring channels, keep a JSON config in `data/links/pumpfun/pumpfun_rooms.json`:

```json
{
  "defaults": { "max_clips": 0 },
  "rooms": [
    { "room": "Af4F…Hpump", "label": "Official Dudas" },
    { "room": "B1oE…rpump", "label": "rasmr" }
  ]
}
```

When this file exists the CLI will auto-load it, grab **all clips** (`max_clips: 0`), and log each channel name. Top it up with new mint addresses as needed.

Key options:

- `--rooms` / `--rooms-file` – list of Pump.fun room IDs (coin mint addresses).
- `--max-clips` – cap the number of clips to pull per room.
- `--mp4-only`, `--mp3-only`, `--no-mp4` – control which formats are saved.
- `--output-dir` – override the default target directory.
- `--config` – provide an alternate rooms configuration file.

Environment overrides (optional):

- `PUMPFUN_API_BASE` (default `https://livestream-api.pump.fun`)
- `PUMPFUN_CLIPS_BASE` (default `https://clips.pump.fun`)
- `PUMPFUN_COIN_API_BASE` (default `https://frontend-api-v3.pump.fun`)
- `PUMPFUN_MAX_CLIPS_PER_ROOM` (default `25`)
- `PUMPFUN_DOWNLOAD_MP4`, `PUMPFUN_DOWNLOAD_MP3`, `PUMPFUN_SKIP_EXISTING`
- `PUMPFUN_MP3_BITRATE` (default `192k`)
- `PUMPFUN_GCS_BUCKET` (optional) upload outputs straight to GCS; pair with Google auth (`GOOGLE_APPLICATION_CREDENTIALS` or gcloud auth).
- `PUMPFUN_GCS_PREFIX` (default `pumpfun_streams`) controls the remote folder.
- `PUMPFUN_KEEP_LOCAL` (default `true`) keep/delete local copies after upload.

### Refreshing channel names

If you add new mints without labels, auto-fill them via:

```bash
.venv/bin/python -m src.data_ingestion_pumpfun.room_config \
  data/links/pumpfun/pumpfun_rooms.json --refresh-labels
```

The helper hits Pump.fun’s public API to backfill the `label` field for each mint.

### Provisioning Google Cloud Storage

Ship large media to Cloud Storage rather than Git:

```bash
# Authenticate first: gcloud auth login
./scripts/setup_storage_bucket.sh create --bucket my-media-bucket --location us-central1
# Optional extras
./scripts/setup_storage_bucket.sh create --bucket my-media-bucket --versioning --retention-days 30
```

To sync any existing downloads:

```bash
./scripts/setup_storage_bucket.sh sync --bucket my-media-bucket --prefix pumpfun_streams
```

To stage uploads in phases:

```bash
# 1) Push metadata first (skip mp3/mp4)
PATH="../rag/google-cloud-sdk/bin:$PATH" ./scripts/setup_storage_bucket.sh sync \
  --bucket my-media-bucket \
  --source datasets/evaluation_data/pumpfun_streams \
  --prefix pumpfun_streams \
  --exclude '.*\\.(mp3|mp4)$'

# 2) Later, send audio/video (skip json)
PATH="../rag/google-cloud-sdk/bin:$PATH" ./scripts/setup_storage_bucket.sh sync \
  --bucket my-media-bucket \
  --source datasets/evaluation_data/pumpfun_streams \
  --prefix pumpfun_streams \
  --exclude '.*\\.json$'
```

Set `PUMPFUN_GCS_BUCKET=my-media-bucket` (and credentials via `GOOGLE_APPLICATION_CREDENTIALS` or `gcloud auth`) before running the downloader and files will stream directly to `gs://my-media-bucket/pumpfun_streams/...`. Toggle `PUMPFUN_KEEP_LOCAL=0` to clean up local clips after upload.

### Binance Academy videos

Fetch Learn & Earn videos by language via the new CLI:

```bash
source .venv/bin/activate
export BINANCE_GCS_BUCKET=my-media-bucket              # optional
python -m src.data_ingestion_binance.cli --languages en --limit 5
```

The crawler reads the `learnAndEarn` XML sitemaps, extracts Wistia video links, downloads the highest-quality MP4, writes `metadata.json`, and (optionally) uploads both to GCS under `binance_academy/<language>/<course>/<video>.mp4`. Override defaults with:

- `BINANCE_LANGUAGE_CODES` – comma-separated language codes from the Binance sitemaps (`en`, `fr`, …).
- `BINANCE_OUTPUT_DIR` – local target directory (default `datasets/evaluation_data/binance_academy`).
- `BINANCE_GCS_BUCKET`, `BINANCE_GCS_PREFIX`, `BINANCE_KEEP_LOCAL` – mirror the Pump.fun/YT behaviour for direct cloud uploads.
- `BINANCE_REQUEST_TIMEOUT`, `BINANCE_WISTIA_TIMEOUT` – HTTP timeouts.

To process a single course (or re-run one video), pass `--include-course` with the full URL; the command supports repeated flags.

### Automating with GitHub Actions

A reusable workflow (`.github/workflows/pumpfun-download.yml`) runs the downloader on a schedule or on-demand and syncs the results to Google Cloud Storage (GCS). To enable it:

1. Create a GCP bucket (e.g. `pumpfun-assets`) and service account with `Storage Object Admin`.
2. Add repository secrets:
   - `GCP_PROJECT_ID` – target project.
   - `GCP_SA_KEY` – service-account JSON (base64 **not** required).
   - `GCS_BUCKET` – bucket name.
3. Optionally provide `max_clips_override` when triggering `workflow_dispatch`; omit or set `0` to download everything per room.

Outputs are also archived as a GitHub workflow artifact, but GCS is the scalable home for MP4/MP3: Git LFS quickly becomes costly and has bandwidth caps, whereas object storage gives cheap, durable access and simple querying (via signed URLs, lifecycle rules, or downstream indexing in BigQuery/BigLake). Keep metadata alongside the media in GCS—e.g., sync the `metadata.json` files or stream an index into a database for faster queries.

## How it works (high level)

### Downloading (`src/data_ingestion_youtube/load/download_mp3/...`)

1. **Planner** (`run.py`): gathers channel videos (via `get_video_info`), normalizes dates, detects already-downloaded items by video ID (title changes don't break dedupe), and prints/saves a plan.

2. **Scheduler** (`batches.py`): flattens videos across channels, sorts globally by newest, runs in "waves", and auto-tunes a global semaphore when errors/rate limits occur.

3. **Clients & cookies** (`clients.py`, `cookies.py`): rotates among safe player clients (`web_embedded`, `android`, `ios`, `tv`). Exports a Netscape cookie file from your browser (cached & validated).

4. **yt-dlp opts** (`downloader.py`):
   - No resume, overwrite stale fragments, no `.part` files → avoids HTTP 416s
   - Prefers `m4a`/`mp4a` (or `opus`), then falls back smartly
   - FFmpeg post-processing to MP3 (or other formats via settings)

### Transcribing (`src/data_ingestion_youtube/load/save_speaker_raw_diarized_audio_files.py`)

- **Plan → execute**: groups by channel, prioritizes smaller backlogs, runs newest → oldest
- **Parallelism**: process pool across API keys, thread pools inside each worker for batches
- **Resilience**: robust uploads (SDK or raw streaming with retries), hard timeouts, and clean deletion of timed-out jobs
- **Entities**: optional; if only entities are missing but diarization exists, it will backfill entities only.

### Event-driven Services & Notifications

- **Cloud Run services**: webhook, metadata enricher, mp3 downloader, diarization worker, and warehouse ingestor are deployed as separate containers, communicating through Pub/Sub topics:
  - `yt-new-video` → metadata enrichment
  - `mp3-download` / `mp3-ready` → audio download + upload to GCS
  - `diarization-ready` → diarization indexer + warehouse pipelines
  - `yt-cookie-request` → emitted when authentication fails; consumed by the Telegram notifier
- **Secret Manager integration**: services read API keys, cookie jars, and other secrets from Secret Manager; the cookie helper script uploads new versions and triggers redeploys automatically.
- **Telegram bot**: receives push notifications for every stage, provides `/cost*`, `/runs`, `/gce`, `/help`, and handles cookie prompts by validating pasted headers and updating secrets.
- **Additional pipelines**: Pump.fun and Binance downloaders publish to `pumpfun-clip` and `binance-course` topics, reusing the same mp3 download + diarization workflow.

## Configuration reference

Many knobs are environment-driven. Important ones:

### Cookies & Browser
- Troubleshooting guide: [`docs/youtube_cookie_playbook.md`](docs/youtube_cookie_playbook.md)
- `BROWSER` / `PROFILE` – where to export cookies from when running locally.
- `USE_COOKIE_CACHE`, `MAX_COOKIE_AGE_DAYS`, `COOKIE_ATTEMPT_ORDER` – configure automated refresh cadence.
- Cookie file is read from `YOUTUBE_COOKIE_FILE` (or the default `src/data_ingestion_youtube/load/download_mp3/youtube_cookies.txt`).
- Use `python3 scripts/fetch_youtube_cookies.py` to convert a raw `Cookie:` header or export via `yt-dlp --cookies-from-browser`, push the new version to Secret Manager, **and** redeploy the Cloud Run downloader in one step:

  ```bash
  python3 scripts/fetch_youtube_cookies.py \
    --cookie-header-file /tmp/youtube_cookie_header.txt \
    --secret projects/<proj>/secrets/youtube-cookies \
    --project <proj> \
    --service youtube-mp3-downloader \
    --region us-central1
  ```

- When downloads fail with auth errors, the downloader emits `yt-cookie-request` events; the Telegram bot notifies the operator with the same instructions and accepts a pasted header to refresh secrets on the fly.

### Downloads (see `download_mp3/config.py::Settings`)
- `GLOBAL_MAX_DOWNLOADS` – initial global concurrency
- `BATCH_SIZE` – items per wave
- `FFMPEG_THREADS` – cap FFmpeg CPU
- `preferred_itags`, `audio_format` (mp3/m4a/opus), etc.
- `YOUTUBE_GCS_BUCKET` / `YOUTUBE_GCS_PREFIX` – optional GCS upload target for audio.
- `YOUTUBE_KEEP_LOCAL` – keep local MP3s after upload (default true; set false only if downstream jobs read from GCS).

### Planning / Prioritization
- `PRIORITIZE_SMALL_CHANNELS` – fewest missing first
- `GROUP_BY_YEAR` – process 2025 → ... → unknown
- `CHANNELS_PER_PASS` – batch several channels together
- `MAX_PER_CHANNEL` – cap items considered per channel

### AssemblyAI
- `ASSEMBLY_AI_API_KEYS` – comma-separated list
- `AAI_MAX_UPLOADS_PER_KEY` – upload concurrency per key
- `AAI_MAX_CONCURRENT_TRANSCRIPTIONS` – per-worker batch size (small is good)
- `AAI_ENABLE_ENTITIES` – include entity extraction
- `AAI_UPLOAD_RETRY_MAX`, `AAI_TRANSCRIBE_TIMEOUT_SEC`, `AAI_UPLOAD_CHUNK_SIZE`

## File & Folder Layout

```
<YOUTUBE_VIDEO_DIRECTORY>/
  <channel>/
    2025-02-12_<videoId>_<title>/
      2025-02-12_<videoId>_<title>.mp3
      2025-02-12_<videoId>_<title>_diarized_content.json
      2025-02-12_<videoId>_<title>_entities.json
  .diarization_cache.pkl

data/links/youtube/
  planned_downloads_summary.csv
  planned_transcriptions_summary.csv
  youtube_channel_handles.txt                    # (optional, comma-separated)
  youtube_videos.csv                              # (optional title allowlist)
  channel_handle_to_id_mapping.json               # (optional cache)
```

## Interpreting progress lines

- `SUCCESS` – finished ok
- `FAILED` – error
- `FALLBACK` – completed via a fallback path
- `SKIPPED` – already done / already exists

Transcription uses one-liners like:
```
57/200 done (28.5%) — 54 succeeded, 1 failed, 0 fallback, 2 skipped
```

(If you see a short form like `(S:54 F:1 FB:0 SK:2)`, that's the compact style conveying the same counters.)

## Appendix — Repo Evolution

### Executive Snapshot

| Aspect | First Commit (`a3d07ca`) | Current (`HEAD`) |
|--------|-------------------------|------------------|
| Architecture | Local scripts & notebooks | Cloud Run services linked by Pub/Sub |
| YouTube Flow | Manual download + diarization script | Webhook → metadata → downloader → diarizer → warehouse events |
| Cookie Handling | Hand-crafted Netscape file | `scripts/fetch_youtube_cookies.py` + Secret Manager + Telegram prompts |
| Telemetry | Ad-hoc logging | Telegram bot (stage alerts, `/runs`, `/gce`, `/cost*`, cookie prompts) |
| Extra Pipelines | None | Pump.fun livestream + Binance Academy courses |
| Tests | Minimal | Comprehensive Pytest suite (schemas, services, cookies, pipelines) |
| Storage | Local filesystem | GCS for audio/JSON + Pub/Sub events + Secret Manager |

### Highlights Since the First Commit

- **Event-driven services** under `services/` with Dockerfiles and deployment scripts.
- **Shared event models** (`src/event_pipeline`) and YouTube-specific modules for download/diarization workflows.
- **Automated cookie management** with Secret Manager uploads, Cloud Run redeploys, and Telegram prompts on failure.
- **Pump.fun & Binance** ingestion modules, CLIs, and Cloud Run services feeding the same pipelines.
- **Telegram notifier** for stage updates, cost reporting, Cloud Run/Functions/Compute summaries, and cookie capture.
- **Expanded datasets** for diarization testing plus new helper utilities (`src/utils/`, `scripts/`).
- **CI and tests** covering pipelines end-to-end (`tests/test_youtube_services.py`, `tests/test_pumpfun_pipeline.py`, etc.).

---

## Troubleshooting

- **"Sign in to confirm your age" / 429s**: ensure your browser profile is logged in; set `BROWSER`, `PROFILE`; let the cookie exporter run (or re-run after `MAX_COOKIE_AGE_DAYS`).

- **HTTP 416 / partial files**: the downloader is configured to avoid resumes and `.part` files; it also purges stale crumbs before retries.

- **"FFmpeg not found"**: install FFmpeg and ensure it's on PATH.

- **Slow or throttled**: lower `GLOBAL_MAX_DOWNLOADS` / `BATCH_SIZE`, or increase if everything is green; the scheduler auto-tunes as it goes.

- **Weird titles/paths on Windows**: all filenames are sanitized and length-limited; if you still hit issues, check `run.py` sanitizers.

## Scripts

- `cleanup_media.sh` – clean orphaned/partial media (optional)
- `get_youtube_cookies.sh` – helper to export cookies (optional)
- `scripts/fetch_youtube_cookies.py` – convert/push cookies and redeploy the downloader (recommended)
- `setup.sh` – system dependency bootstrap (if provided)

## Notes & Responsibilities

- Respect YouTube's Terms of Service and copyright law. Only download content you're authorized to process.
- Private/age-gated content requires your own account cookies; access depends on your account's permissions.
- AssemblyAI usage may incur costs; set appropriate concurrency and timeouts.

## Acknowledgements

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for downloading
- [AssemblyAI](https://www.assemblyai.com/) for transcription & diarization

## License

Add your project license here (e.g., MIT).
