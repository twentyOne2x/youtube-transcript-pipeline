# Event-Driven Media Pipeline — Design & Roadmap

## Goals

- Replace the current batch downloads with an event-driven pipeline that reacts to new videos/minutes.
- Use managed GCP services (Cloud Run, Cloud Functions, Pub/Sub, Cloud Storage) to minimize ops and cost.
- Ensure every asset (metadata, MP3, diarization output) flows automatically into Google Cloud Storage (“media-just-skyline-474622-e1”) and downstream ingestion.
- Reuse existing Python modules (YouTube fetch, Pumpfun downloader, Binance crawler, diarization) with minimal refactoring.

## High-Level Architecture

```
YouTube PubSubHubbub ─▶ Cloud Run (Webhook Handler) ─┐
                                                     │ yt-new-video (Pub/Sub)
                                                     ▼
                                  Cloud Run (Metadata Enricher) ─▶ mp3-download topic
                                                     ▼
                                   Cloud Run (MP3 Downloader) ─▶ GCS: youtube_audio/
                                                     │                            └─▶ mp3-ready topic
                                                     ▼
                        Cloud Run / Function (Diarization Worker) ─▶ GCS: ..._diarized_content.json
                                                     │                            └─▶ diarization-ready topic
                                                     ▼
                           Cloud Run / Function (Warehouse Ingestion) ─▶ BigQuery / search index

Cloud Scheduler ─▶ Cloud Run (Binance Course Publisher) ─▶ binance-course topic ─▶ Cloud Run (Binance Downloader) → GCS → mp3-ready → diarization-ready

Cloud Scheduler ─▶ Cloud Run (Pump.fun Clip Publisher) ─▶ pumpfun-clip topic ─▶ Cloud Run (Pump.fun Downloader) → GCS → mp3-ready → diarization-ready
```

### Topics / Events

| Topic / Event            | Payload (JSON)                                  | Producer                             | Consumer(s)                               |
|------------------------- |-------------------------------------------------|-------------------------------------- |-------------------------------------------|
| `yt-new-video`           | `{ "videoId", "channelId" }`                    | YouTube webhook handler               | Metadata enricher                         |
| `mp3-download`           | `{ "videoId", "channelId", "metadata" }`        | Metadata enricher                     | MP3 downloader                            |
| `mp3-ready`              | `{ "gcsUri", "metadataUri", "videoId" }`        | MP3 downloader                        | Diarization worker                        |
| GCS finalize (MP3)       | Storage event (if we prefer Eventarc)           | Cloud Storage                         | Diarization worker (alternative trigger) |
| `diarization-ready`      | `{ "mp3Uri", "diarizedUri", "entitiesUri" }`    | Diarization worker                    | Warehouse ingestion / downstream         |
| `yt-cookie-request`      | `{ "videoId", "channelId", "reason", "details" }` | MP3 downloader (auth failure)         | Telegram notifier / operators            |
| `binance-course`         | `{ "courseUrl", "language" }`                   | Binance course publisher               | Binance downloader                        |
| `pumpfun-clip`           | `{ "room", "clipId", "playlistUrl", "clip", "coin" }` | Pump.fun clip publisher                 | Pump.fun downloader                       |

## Component Overview

### 1. YouTube Webhook Handler (Cloud Run)
- REST endpoint registered with PubSubHubbub for each channel.
- Validates hub challenge/signature, publishes to `yt-new-video`.
- Language: Python (FastAPI/Flask); minimal container (~10MB).

### 2. Metadata Enricher (Cloud Run)
- Subscribes to `yt-new-video`.
- Calls YouTube Data API for the fresh upload, caches the raw snippet JSON to `/tmp/youtube_metadata/<video_id>.video_metadata.json`.
- Emits `mp3-download` with `source=metadata-enricher` and attaches lightweight title/publishedAt metadata for downstream naming.
- Reuses the shared client in `src/event_pipeline/youtube/metadata.py`.

### 3. MP3 Downloader (Cloud Run Service)
- Subscribes to `mp3-download`.
- Invokes the shared `download_mp3` helper (yt-dlp + ffmpeg) to save audio under `/tmp/youtube_audio/<channel>/<video>/`.
- Uploads the final MP3 to `gs://media-just-skyline-474622-e1/youtube_audio/...` via `maybe_upload` and, when configured, removes the local copy.
- Emits `mp3-ready` with the resulting GCS URI (`source=mp3-downloader`).
- Container bundles yt-dlp, ffmpeg, and browser-cookie support for rate-limited channels.
- When authentication fails (e.g., `Sign in to confirm you're not a bot`), the service publishes a `yt-cookie-request` event so the Telegram bot can prompt operators for refreshed cookies.

### 4. Diarization Worker (Cloud Run / Function)
- Subscribes to `mp3-ready`.
- Downloads (or streams) the MP3, submits it to AssemblyAI with speaker diarization + entity detection enabled.
- Supports multiple AssemblyAI API keys via round-robin rotation (`ASSEMBLY_AI_API_KEYS` in `.env`).
- Writes `<video_id>_diarized.json` (+ optional entities JSON) to `/tmp/diarization` and re-uploads to `gs://media-just-skyline-474622-e1/youtube_diarized/<video_id>/`.
- Emits `diarization-ready` (`source=diarization-worker`) for downstream ingestion.

### 5. Warehouse Ingestion (Cloud Run / Function)
- Subscribes to `diarization-ready`.
- Buffers each event to `WAREHOUSE_BUFFER_DIR` (JSON snapshot) and republishes to `ingestion-diarization-ready` with `source=warehouse-ingestion`.
- Optionally POSTs the payload to an external ingestion endpoint when `INGESTION_ENDPOINT` is configured.
- Acts as the bridge to the Pinecone-backed ingestion stack.

### 6. Diarization Indexer (Cloud Run)
- Push-subscription target for `diarization-ready` (`projects/just-skyline-474622-e1/subscriptions/diarization-indexer-videos`).
- Validates Pub/Sub signature (optional) then hydrates namespace/channel policy before ingest.
- Downloads the referenced diarization + entity JSON, generates parent/child vectors, and upserts them into Pinecone.
- Responds with 204 on success so Pub/Sub can ack; failures bubble 5xx and trigger retries / dead-lettering.
- Deployment image: `us-central1-docker.pkg.dev/just-skyline-474622-e1/ingestion/diarization-indexer:latest`.

### 7. Binance Pipeline
- **Binance Course Publisher (Cloud Run)**
  - Triggered via Cloud Scheduler HTTP task (configurable cadence).
  - Uses `discover_courses` to read Binance sitemaps and publish `BinanceCourseEvent` messages to `binance-course`.
  - Supports ad-hoc course lists via payload (Scheduler or manual trigger).
- **Binance Downloader (Cloud Run)**
  - Pub/Sub push subscriber on `binance-course`.
  - Reuses `src/data_ingestion_binance` downloader to grab MP4 + metadata, transcodes to MP3, uploads artefacts to GCS, and publishes `Mp3ReadyEvent` to `mp3-ready`.
  - Cleans up local artefacts when `BINANCE_KEEP_LOCAL=0`.

### 8. Pump.fun Pipeline
- **Pump.fun Clip Publisher (Cloud Run)**
  - Triggered by Cloud Scheduler (recommended every 5–10 minutes).
  - Hydrates room labels from `pumpfun_rooms.json` (or `PUMPFUN_ROOMS` env) and uses `discover_clip_events` to enqueue fresh clips on `pumpfun-clip`.
  - Skips clips already present in GCS when `PUMPFUN_SKIP_EXISTING=1`.
- **Pump.fun Downloader (Cloud Run)**
  - Pub/Sub push subscriber consuming `pumpfun-clip`.
  - Invokes existing ffmpeg-based downloader, uploads metadata/mp3 to `gs://<bucket>/<prefix>/`, and publishes `Mp3ReadyEvent` (skipping duplicates).
  - Requires `PUMPFUN_GCS_BUCKET`/`PUMPFUN_GCS_PREFIX`. Automatically disables local retention for Cloud Run.

### 9. State Notifications (Telegram)
- The notifier now lives in a separate repository [`twentyOne2x/telegram-state-notifier`](https://github.com/twentyOne2x/telegram-state-notifier) (FastAPI + native Telegram API).
- Subscribe the deployed service to each stage topic (`yt-new-video`, `mp3-ready`, `diarization-ready`, `ingestion-diarization-ready`) with per-stage subscriptions so the `source` attribute identifies the originating component.
- Messages are Markdown-formatted and include the pipeline name prefix plus the relevant identifier (video ID, clip ID, or URI). Debug mode (`ENABLE_DEBUG_EVENTS=1`) suppresses outgoing messages but keeps structured logs.
- Stage map currently recognises: `youtube-webhook`, `metadata-enricher`, `mp3-downloader`, `pumpfun-downloader`, `binance-downloader`, `diarization-worker`, `warehouse-ingestion`, and `diarization-indexer`. Extend the map or attributes if new producers are added.
- Planned improvement: emit an explicit “cookie refresh required” notification when the downloader raises repeated 403/NoSuchFormat errors or detects an expired cookie file.
- Operators receive actionable alerts when `yt-cookie-request` is published; the bot explains how to run `python scripts/fetch_youtube_cookies.py --browser brave --profile Default` locally and falls back to a manual DevTools workflow. Replying to the bot with either a Netscape cookie file or a raw `Cookie:` header automatically uploads a new Secret Manager version and acknowledges the prompt.

### 10. YouTube Cookie Management
- Daily reliability for the downloader depends on supplying fresh YouTube cookies. Full playbook lives in `docs/youtube_cookie_playbook.md`; key points are summarised here.
- **Linux workstation flow (preferred):**
  1. Ensure Brave/Chrome profile on the same host is logged into the YouTube account that can see private/age-gated videos.
  2. Run `./get_youtube_cookies.sh` (set `BROWSER=brave|chrome` and `PROFILE=<name>` when needed). The script spins up a lightweight virtualenv, calls `yt-dlp --cookies-from-browser`, validates Netscape format, and writes `src/data_ingestion_youtube/load/youtube_cookies.txt`.
     - Alternatively run `python3 scripts/fetch_youtube_cookies.py --browser brave --profile Default --secret projects/<proj>/secrets/youtube-cookies --service youtube-mp3-downloader --project <proj>` to export, upload a new Secret Manager version, and redeploy Cloud Run in one step. Supply `--cookie-header-file` if you already copied a raw `Cookie:` header.
  3. Verify the export with `yt-dlp --cookies youtube_cookies.txt --simulate https://www.youtube.com/watch?v=BaW_jenozKc`.
  4. Upload the cookie file to Secret Manager for Cloud Run consumption, e.g.  
     `gcloud secrets versions add youtube-cookies --data-file=src/data_ingestion_youtube/load/youtube_cookies.txt`  
     then mount in each service:  
     `gcloud run services update youtube-mp3-downloader --region us-central1 --set-secrets YOUTUBE_COOKIE_FILE=youtube-cookies:latest:/workspace/youtube_cookies.txt`.
- **Runtime options recognised by the downloader:**
  - `YOUTUBE_COOKIE_FILE`: absolute path to a Netscape cookie jar (typically the Secret Manager mount path).
  - `YOUTUBE_COOKIE_B64`: base64-encoded cookie payload; decoded to `/tmp/youtube_cookies.txt` (or `YOUTUBE_COOKIE_TMP`).
  - `YOUTUBE_COOKIE_SECRET` (+ optional `YOUTUBE_COOKIE_SECRET_VERSION`): Secret Manager resource name; fetched at startup and cached to `/tmp`.
  - Set `DEFAULT_CLIENT=android` and `USE_BROWSER_COOKIES=false` on Cloud Run to avoid attempting local keyring lookups.
- **Headless / automation notes:**
  - Cloud Run itself cannot run Chromium with profile access, so cookie refresh must occur off-cluster (workstation, Cloud Workstation, or VM). The refreshed `youtube_cookies.txt` is treated as data and injected via Secret Manager or baked into an artifact.
  - For scheduled refresh, set up a Linux cron job (e.g. Cloud Workstation) that runs `get_youtube_cookies.sh`, validates freshness (`MAX_COOKIE_AGE_DAYS`), and pushes a new secret version.
  - Advanced option: Playwright/Puppeteer script to perform a programmatic login and export cookies. Requires handling MFA and consent flows; keep as a future enhancement.
- **Manual fallback:** when automated export fails, the script prompts for a pasted `Cookie` header (from DevTools → Network). Convert to Netscape format and store in the same location.
- **Operational safeguards:**
  - The downloader logs when cookies are missing or stale; hook this to a Pub/Sub + Telegram alert that pings operators (“Cookies expired – run get_youtube_cookies.sh”).
  - Track cookie expiry via `MAX_COOKIE_AGE_DAYS` and consider wiring the Telegram notifier to send prompts when the downloader switches to cookie-less mode.

## Deployment Plan

1. **Repository split / packaging**
   - Extract each stage into a standalone entrypoint (already modularized).
   - Add Dockerfiles for:
     - `metadata-enricher`
     - `mp3-downloader`
     - `diarization-worker`
     - `binance-crawler` / `binance-downloader`
     - `pumpfun-downloader` (optional)

2. **Infrastructure as Code (Terraform or gcloud scripts)**
   - Pub/Sub topics & subscriptions: `yt-new-video`, `mp3-download`, `mp3-ready`, `diarization-ready`, `binance-course`, `pumpfun-clip`.
   - Cloud Run services and triggers (Pub/Sub or Eventarc).
   - IAM: service accounts per component with least privilege (Pub/Sub publisher/subscriber, GCS object admin, Secret Manager access).
   - Cloud Scheduler jobs for Binance crawler (daily) and fallback periodic YouTube sweep (optional).

3. **Secrets & Configuration**
   - Store sensitive keys (YouTube API, AssemblyAI) in Secret Manager.
   - Cloud Run mounts secrets as environment variables.
   - GCS bucket `media-just-skyline-474622-e1` already provisioned; ensure object lifecycle rules & notifications are in place.

4. **Continuous Delivery**
   - GitHub Actions workflow per service:
     - Lint / tests.
     - Build container.
     - Deploy to Cloud Run (`gcloud run deploy`).
   - Parallel path for local dev (Makefile targets).

## Roadmap & Checklist

### Phase 1 — Foundations
- [x] ~~Finalize Pub/Sub topic naming & message schemas.~~
- [x] ~~Write Dockerfiles for each component.~~
- [x] ~~Add Makefile targets for `build`, `push`, `deploy`.~~
- [x] ~~Configure Secret Manager entries (YouTube API, AssemblyAI, Pump.fun keys).~~
- [x] ~~Update docs for environment variables and module behavior (README + design doc).~~

### Phase 2 — YouTube Pipeline
- [x] ~~Implement Cloud Run webhook handler.~~
- [x] ~~Deploy metadata enricher service with Pub/Sub trigger (container & tests ready; deploy via Makefile/gcloud).~~
- [x] ~~Deploy MP3 downloader service (yt-dlp container).~~
- [x] ~~Configure GCS notification (or Pub/Sub message) for MP3 finalize.~~
- [x] ~~Deploy diarization worker.~~
- [x] ~~Deploy warehouse ingestion function/service.~~
- [x] ~~End-to-end test with a single channel and verify artifacts in GCS + downstream ingestion.~~

### Phase 3 — Binance & Pump.fun Integration
- [x] Scheduler-driven sitemap crawl (Cloud Run job).
- [x] Deploy Binance downloader (reusing new modules).
- [x] Integrate with diarization-ready event (via mp3-ready topic).
- [x] Define Pump.fun source strategy (Scheduler + room config).
- [x] Deploy Pump.fun downloader with Pub/Sub topic.

### Phase 4 — Enhancements & Monitoring
- [ ] Centralized logging (Cloud Logging dashboards).
- [ ] Alerting (errors per topic / service).
- [ ] Dead-letter queues for failed messages.
- [ ] Optional: BigQuery pipeline / ingestion repo integration tests.
- [x] ~~Deploy Telegram notifier service (Cloud Run).~~
- [ ] Wire dedicated Pub/Sub subscriptions per stage (`yt-new-video`, `mp3-download`, `mp3-ready`, `diarization-ready`, `ingestion-diarization-ready`) to the notifier.
- [ ] Add automated smoke test that publishes synthetic events and confirms Telegram delivery.

## Implementation Status — YouTube Pipeline

- Core event schemas live in `src/event_pipeline/schemas.py` with topic constants and helpers.
- Service containers added under `services/` (`youtube_webhook`, `metadata_enricher`, `mp3_downloader`, `diarization_worker`, `warehouse_ingestion`) each with FastAPI entrypoints and Dockerfiles.
- Common settings + Pub/Sub helpers live in `src/event_pipeline/settings.py` and `src/event_pipeline/pubsub.py`; YouTube-specific orchestration in `src/event_pipeline/youtube/`.
- Diarization worker supports multi-key AssemblyAI rotation via `ASSEMBLY_AI_API_KEYS`; fallback to single `ASSEMBLY_AI_API_KEY` remains.
- Warehouse ingestion republishes downstream events and the separate diarization indexer service (ingestion repo) consumes `diarization-ready` → Pinecone.
- Local automation: `Makefile` for venv bootstrap, pytest, Docker builds, and Cloud Run depdloys.
- Bootstrap script `infra/gcloud/bootstrap_youtube_pipeline.sh` provisions Pub/Sub topics and service accounts.
- Tests: `tests/test_event_schemas.py` + `tests/test_youtube_services.py` cover serialization, service wiring, and AssemblyAI key rotation (run with `make test` or `pytest`).
- Live validation (`scripts/run_youtube_e2e.py`) on 2025-10-10 processed video `H46AkZbr9K0`, storing outputs under `gs://media-just-skyline-474622-e1/youtube_e2e/a25a24ca/` and writing warehouse buffers to `/tmp/youtube_pipeline_e2e/a25a24ca/`.
- Secret-backed Cloud Run deploys: metadata pulls `youtube-api-key`, diarizer pulls `assemblyai-api-key(s)`, warehouse republishes to `ingestion-diarization-ready` and can POST to an `INGESTION_ENDPOINT` for the external ingestion service.
- Telegram notifier deployed separately; currently subscribed to `yt-new-video` with additional stage subscriptions pending.

## Testing Strategy

### Unit / Integration Tests
- [ ] `pytest` suites for sitemap parsing, Wistia downloader, Pump.fun modules.
- [ ] Mocked tests for YouTube metadata fetcher (using `responses` or `httpretty`).
- [ ] Tests for GCS upload helper (`src/utils/gcs`).
- [x] AssemblyAI key rotation + service wiring (`tests/test_youtube_services.py`).

### Container Tests
- [ ] `python -m compileall` (already used) on all service modules.
- [ ] `pytest` executed inside container build step.

### End-to-End (Staging)
- [ ] Deploy to staging GCP project / bucket.
- [ ] Post test Pub/Sub messages to simulate new video.
- [ ] Verify GCS objects produced + diarization results.
- [ ] Ensure ingestion service receives `diarization-ready` and updates datastore.

### Manual Verification
- [ ] Run Binance crawler once, inspect downloaded MP4/metadata in GCS.
- [ ] Trigger Pump.fun pipeline with known clip URL and verify output.
- [ ] Confirm diarization worker handles >5 concurrent jobs (AssemblyAI rate limits).
- [ ] Publish staged events through Pub/Sub to confirm Telegram notifications for every pipeline stage.
- [ ] Regenerate cookies via `get_youtube_cookies.sh`, push to Secret Manager, and confirm the downloader picks up the new version without redeploy.

## Notes on Costs & Limits
- Cloud Run free tier: 180,000 vCPU-seconds & 360,000 GB-seconds per month (per region). The workload stays within free limits for low frequency.
- Pub/Sub free tier: 10 GB per month, plenty for simple metadata.
- Cloud Storage: retains existing costs for MP3/JSON.
- AssemblyAI billing remains as per usage.

## Next Steps
- Provision / verify remaining Pub/Sub subscriptions (stage-specific Telegram subs + any missing warehouse/downstream consumers).
- Script a smoke-test harness that publishes synthetic `yt-new-video` → validates end-to-end artifacts + Telegram delivery.
- Expand monitoring: Stackdriver alerts on Pub/Sub dead-letter counts, Cloud Run 5xx, AssemblyAI quota.
- Harden ingestion runbooks (replay tooling, Pinecone health checks, AssemblyAI key rotation process).
- Implement cookie-refresh notifications (Telegram ping + runbook link) triggered when the downloader detects missing/expired cookies.
