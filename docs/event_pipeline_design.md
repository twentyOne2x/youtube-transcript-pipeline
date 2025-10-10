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

Binance crawler (Cloud Scheduler + Cloud Run) ─▶ binance-course topic ─▶ Downloader → GCS → diarization-ready

Pump.fun scheduler / webhook (TBD) ─▶ pumpfun-clip topic ─▶ Downloader → GCS → diarization-ready
```

### Topics / Events

| Topic / Event            | Payload (JSON)                                  | Producer                             | Consumer(s)                               |
|------------------------- |-------------------------------------------------|-------------------------------------- |-------------------------------------------|
| `yt-new-video`           | `{ "videoId", "channelId" }`                    | YouTube webhook handler               | Metadata enricher                         |
| `mp3-download`           | `{ "videoId", "channelId", "metadata" }`        | Metadata enricher                     | MP3 downloader                            |
| `mp3-ready`              | `{ "gcsUri", "metadataUri", "videoId" }`        | MP3 downloader                        | Diarization worker                        |
| GCS finalize (MP3)       | Storage event (if we prefer Eventarc)           | Cloud Storage                         | Diarization worker (alternative trigger) |
| `diarization-ready`      | `{ "mp3Uri", "diarizedUri", "entitiesUri" }`    | Diarization worker                    | Warehouse ingestion / downstream         |
| `binance-course`         | `{ "courseUrl", "language" }`                   | Scheduler-based sitemap crawler       | Binance downloader                        |
| `pumpfun-clip`           | `{ "clipUrl" }`                                 | (Future) Pump.fun event discovery     | Pump.fun downloader                       |

## Component Overview

### 1. YouTube Webhook Handler (Cloud Run)
- REST endpoint registered with PubSubHubbub for each channel.
- Validates hub challenge/signature, publishes to `yt-new-video`.
- Language: Python (FastAPI/Flask); minimal container (~10MB).

### 2. Metadata Enricher (Cloud Run)
- Subscribes to `yt-new-video`.
- Calls YouTube Data API, stores metadata (Firestore/BigQuery), updates summary CSV.
- Emits `mp3-download`.
- Reuses logic from `fetch_youtube_video_details_from_handles.py`.

### 3. MP3 Downloader (Cloud Run Job)
- Subscribes to `mp3-download`.
- Runs `yt-dlp` using existing `download_mp3` module.
- Uploads MP3 + metadata.json to `gs://media-just-skyline-474622-e1/youtube_audio/...`.
- Emits `mp3-ready`.
- Requires container with ffmpeg, yt-dlp, python libs.

### 4. Diarization Worker (Cloud Run / Function)
- Triggered by `mp3-ready` topic or by GCS finalize.
- Streams MP3 to AssemblyAI, waits for diarization & entities.
- Writes `_diarized_content.json` & `_entities.json` back to the same prefix.
- Emits `diarization-ready`.
- Uses current `save_speaker_raw_diarized_audio_files.py` logic.

### 5. Warehouse Ingestion (Cloud Run / Function)
- Subscribes to `diarization-ready`.
- Loads transcript metadata into BigQuery (or other datastore).
- Optionally updates downstream search indices.
- Integrates with the existing “ingestion” repo via REST or Pub/Sub.

### 6. Binance Pipeline
- Cloud Scheduler daily job triggers Cloud Run “sitemap crawler”.
- Crawler publishes course URLs to `binance-course`.
- Downloader reuses `src/data_ingestion_binance` modules to fetch Wistia MP4s, save to GCS, and emit `diarization-ready`.

### 7. Pump.fun Pipeline
- Interim: run existing CLI via Cloud Scheduler.
- Target: event feed ingesting clip URLs (Twitter, RSS, custom webhook). Publishes to `pumpfun-clip`.
- Downloader reuses Pump.fun module, writes to GCS, emits `diarization-ready`.

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
- [ ] Configure Secret Manager entries (YouTube API, AssemblyAI, Pump.fun keys).
- [ ] Update docs for environment variables and module behavior (README sections done).

### Phase 2 — YouTube Pipeline
- [x] ~~Implement Cloud Run webhook handler.~~
- [x] ~~Deploy metadata enricher service with Pub/Sub trigger (container & tests ready; deploy via Makefile/gcloud).~~
- [x] ~~Deploy MP3 downloader service (yt-dlp container).~~
- [x] ~~Configure GCS notification (or Pub/Sub message) for MP3 finalize.~~
- [x] ~~Deploy diarization worker.~~
- [x] ~~Deploy warehouse ingestion function/service.~~
- [x] ~~End-to-end test with a single channel and verify artifacts in GCS + downstream ingestion.~~

### Phase 3 — Binance & Pump.fun Integration
- [ ] Scheduler-driven sitemap crawl (Cloud Run job).
- [ ] Deploy Binance downloader (reusing new modules).
- [ ] Integrate with diarization-ready event.
- [ ] Define Pump.fun source strategy (RSS, manual list, or webhook).
- [ ] Deploy Pump.fun downloader with Pub/Sub topic.

### Phase 4 — Enhancements & Monitoring
- [ ] Centralized logging (Cloud Logging dashboards).
- [ ] Alerting (errors per topic / service).
- [ ] Dead-letter queues for failed messages.
- [ ] Optional: BigQuery pipeline / ingestion repo integration tests.

## Implementation Status — YouTube Pipeline

- Core event schemas live in `src/event_pipeline/schemas.py` with topic constants and helpers.
- Service containers added under `services/` (`youtube_webhook`, `metadata_enricher`, `mp3_downloader`, `diarization_worker`, `warehouse_ingestion`) each with FastAPI entrypoints and Dockerfiles.
- Common settings + Pub/Sub helpers live in `src/event_pipeline/settings.py` and `src/event_pipeline/pubsub.py`; YouTube-specific orchestration in `src/event_pipeline/youtube/`.
- Local automation: `Makefile` for venv bootstrap, pytest, Docker builds, and Cloud Run deploys.
- Bootstrap script `infra/gcloud/bootstrap_youtube_pipeline.sh` provisions Pub/Sub topics and service accounts.
- Tests: `tests/test_event_schemas.py` + `tests/test_youtube_services.py` cover serialization and service wiring (run with `make test`).
- Live validation (`scripts/run_youtube_e2e.py`) on 2025-10-10 processed video `H46AkZbr9K0`, storing outputs under `gs://media-just-skyline-474622-e1/youtube_e2e/a25a24ca/` and writing warehouse buffers to `/tmp/youtube_pipeline_e2e/a25a24ca/`.

## Testing Strategy

### Unit / Integration Tests
- [ ] `pytest` suites for sitemap parsing, Wistia downloader, Pump.fun modules.
- [ ] Mocked tests for YouTube metadata fetcher (using `responses` or `httpretty`).
- [ ] Tests for GCS upload helper (`src/utils/gcs`).

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

## Notes on Costs & Limits
- Cloud Run free tier: 180,000 vCPU-seconds & 360,000 GB-seconds per month (per region). The workload stays within free limits for low frequency.
- Pub/Sub free tier: 10 GB per month, plenty for simple metadata.
- Cloud Storage: retains existing costs for MP3/JSON.
- AssemblyAI billing remains as per usage.

## Next Steps
- Agree on message schema & topics (final review).
- Prioritize containerization of metadata downloader and MP3 pipeline.
- Set up CI/CD workflow for automated deployment.
- Implement staging environment test harness.
