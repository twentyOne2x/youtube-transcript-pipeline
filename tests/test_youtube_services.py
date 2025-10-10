import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.event_pipeline.schemas import (
    DiarizationReadyEvent,
    Mp3DownloadEvent,
    Mp3ReadyEvent,
    YouTubeNewVideoEvent,
)
from src.event_pipeline.youtube.downloader import DownloadArtifact
from src.event_pipeline.youtube.metadata import VideoMetadata
from src.event_pipeline.youtube.diarization import DiarizationResult


@pytest.fixture(autouse=True)
def pipeline_env(monkeypatch):
    from src.event_pipeline.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("MEDIA_BUCKET", "test-bucket")
    yield


def test_webhook_handler_publishes_event(monkeypatch):
    from services.youtube_webhook.app import app

    published = []

    def fake_publish(topic, event, attributes=None):
        published.append((topic, event))
        return "msg-1"

    monkeypatch.setattr("services.youtube_webhook.app.publish_event", fake_publish)
    client = TestClient(app)

    feed = """
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <yt:videoId>abc123def45</yt:videoId>
            <yt:channelId>UC123</yt:channelId>
        </entry>
    </feed>
    """
    response = client.post("/hub", data=feed, headers={"Content-Type": "application/atom+xml"})
    assert response.status_code == 202
    assert published[0][0] == "yt-new-video"
    assert published[0][1].video_id == "abc123def45"


def test_metadata_enricher_publishes_download_event(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setenv("METADATA_CACHE_DIR", tmp_path.as_posix())

    from services.metadata_enricher.app import app

    published = []

    def fake_publish(topic, event, attributes=None):
        published.append((topic, event, attributes))
        return "msg-2"

    monkeypatch.setattr("services.metadata_enricher.app.publish_event", fake_publish)

    class StubClient:
        def fetch_video(self, video_id: str):
            return VideoMetadata(
                raw={
                    "snippet": {
                        "title": "Test Video",
                        "description": "testing",
                        "publishedAt": "2023-01-01T00:00:00Z",
                    }
                }
            )

    monkeypatch.setattr("services.metadata_enricher.app._client", lambda: StubClient())

    client = TestClient(app)
    envelope = {
        "message": {
            "data": YouTubeNewVideoEvent(video_id="abc123def45", channel_id="UC123").to_base64_json()
        }
    }
    response = client.post("/pubsub/push", json=envelope)
    assert response.status_code == 204
    topic, event, attrs = published[0]
    assert topic == "mp3-download"
    assert event.video_id == "abc123def45"
    assert attrs["source"] == "metadata-enricher"


def test_mp3_downloader_publishes_ready_event(monkeypatch, tmp_path):
    from services.mp3_downloader.app import app

    monkeypatch.setenv("YOUTUBE_VIDEO_DIRECTORY", tmp_path.as_posix())
    published = []

    fake_artifact = DownloadArtifact(
        local_path=tmp_path / "video.mp3",
        gcs_uri="gs://test-bucket/youtube_audio/abc123.mp3",
    )

    def fake_download(event):
        return fake_artifact

    def fake_build(artifact, event):
        return Mp3ReadyEvent(gcs_uri=artifact.gcs_uri, metadata_uri=None, video_id=event.video_id)

    def fake_publish(topic, event, attributes=None):
        published.append((topic, event))
        return "msg-3"

    monkeypatch.setattr("services.mp3_downloader.app.download_mp3", fake_download)
    monkeypatch.setattr("services.mp3_downloader.app.build_ready_event", fake_build)
    monkeypatch.setattr("services.mp3_downloader.app.publish_event", fake_publish)

    client = TestClient(app)
    envelope = {
        "message": {
            "data": Mp3DownloadEvent(
                video_id="abc123def45", channel_id="UC123", metadata={"title": "Test", "publishedAt": "2023-01-01"}
            ).to_base64_json()
        }
    }
    response = client.post("/pubsub/push", json=envelope)
    assert response.status_code == 204
    topic, event = published[0]
    assert topic == "mp3-ready"
    assert event.gcs_uri == "gs://test-bucket/youtube_audio/abc123.mp3"


def test_diarization_worker_publishes_ready_event(monkeypatch):
    from services.diarization_worker.app import app

    monkeypatch.setenv("ASSEMBLY_AI_API_KEY", "dummy")

    published = []

    result = DiarizationResult(
        diarized_path=Path("/tmp/diarized.json"),
        diarized_uri="gs://bucket/diarized.json",
        entities_path=None,
        entities_uri=None,
    )

    def fake_run(event, api_key, bucket):
        return result

    def fake_build(event, res):
        return DiarizationReadyEvent(
            mp3_uri=event.gcs_uri,
            diarized_uri=res.diarized_uri,
            entities_uri=res.entities_uri,
        )

    def fake_publish(topic, event, attributes=None):
        published.append((topic, event, attributes))
        return "msg-4"

    monkeypatch.setattr("services.diarization_worker.app.run_diarization", fake_run)
    monkeypatch.setattr("services.diarization_worker.app.build_ready_event", fake_build)
    monkeypatch.setattr("services.diarization_worker.app.publish_event", fake_publish)

    client = TestClient(app)
    envelope = {
        "message": {
            "data": Mp3ReadyEvent(
                gcs_uri="gs://bucket/audio.mp3",
                metadata_uri=None,
                video_id="abc123def45",
            ).to_base64_json()
        }
    }
    response = client.post("/pubsub/push", json=envelope)
    assert response.status_code == 204
    topic, event, attrs = published[0]
    assert topic == "diarization-ready"
    assert attrs["source"] == "diarization-worker"


def test_warehouse_ingestion_writes_buffer(monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_BUFFER_DIR", tmp_path.as_posix())
    from services.warehouse_ingestion.app import app

    client = TestClient(app)
    event = DiarizationReadyEvent(
        mp3_uri="gs://bucket/audio.mp3",
        diarized_uri="gs://bucket/diarized.json",
        entities_uri=None,
    )
    envelope = {"message": {"data": event.to_base64_json()}}
    response = client.post("/pubsub/push", json=envelope)
    assert response.status_code == 204
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["mp3_uri"] == "gs://bucket/audio.mp3"
