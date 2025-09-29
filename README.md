# YouTube Audio → Diarized Transcripts Pipeline

Batch-download YouTube audio (MP3) with resilient cookie/client rotation, then generate **speaker-aware** transcripts (and optional entity extraction) with AssemblyAI. The pipeline plans work up front, runs adaptively with global concurrency caps, and writes clean sidecars next to each audio file.

---

## Features

- **Planner first:** prints a concise plan and saves CSVs of what will be downloaded/transcribed.
- **Robust downloads:** yt-dlp with cookie export (from your browser), client rotation, 416/429 handling, and safe filenames.
- **Predictable layout:** `.../<channel>/<YYYY-MM-DD>_<videoId>_<title>/<same>.mp3`
- **Diarization + entities:** writes `_diarized_content.json` and `_entities.json` next to each MP3.
- **Idempotent:** skips items that already exist or have valid sidecars; persistent cache for transcripts.
- **Adaptive concurrency:** global semaphore auto-tunes based on success/error rates.
- **Clear progress:** plan tables + global counters; CSVs land in `data/links/youtube/`.

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

## Configuration reference

Many knobs are environment-driven. Important ones:

### Cookies & Browser
- `BROWSER` / `PROFILE` – where to export cookies from
- `USE_COOKIE_CACHE` – `1` to reuse cached cookies
- `MAX_COOKIE_AGE_DAYS` – recache after N days
- `COOKIE_ATTEMPT_ORDER` – `keyring,plain` or `plain,keyring` (try "plain" if your keyring blocks)
- Cookie file lives at: `src/data_ingestion_youtube/load/download_mp3/youtube_cookies.txt`

### Downloads (see `download_mp3/config.py::Settings`)
- `GLOBAL_MAX_DOWNLOADS` – initial global concurrency
- `BATCH_SIZE` – items per wave
- `FFMPEG_THREADS` – cap FFmpeg CPU
- `preferred_itags`, `audio_format` (mp3/m4a/opus), etc.

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

## Troubleshooting

- **"Sign in to confirm your age" / 429s**: ensure your browser profile is logged in; set `BROWSER`, `PROFILE`; let the cookie exporter run (or re-run after `MAX_COOKIE_AGE_DAYS`).

- **HTTP 416 / partial files**: the downloader is configured to avoid resumes and `.part` files; it also purges stale crumbs before retries.

- **"FFmpeg not found"**: install FFmpeg and ensure it's on PATH.

- **Slow or throttled**: lower `GLOBAL_MAX_DOWNLOADS` / `BATCH_SIZE`, or increase if everything is green; the scheduler auto-tunes as it goes.

- **Weird titles/paths on Windows**: all filenames are sanitized and length-limited; if you still hit issues, check `run.py` sanitizers.

## Scripts

- `cleanup_media.sh` – clean orphaned/partial media (optional)
- `get_youtube_cookies.sh` – helper to export cookies (optional)
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