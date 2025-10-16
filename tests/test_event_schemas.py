import pytest

from src.event_pipeline.schemas import (
    DiarizationReadyEvent,
    Mp3DownloadEvent,
    Mp3ReadyEvent,
    TopicName,
    YouTubeCookieRequestEvent,
    YouTubeNewVideoEvent,
    decode_pubsub_message,
)


def test_youtube_new_video_serialization_roundtrip():
    event = YouTubeNewVideoEvent(video_id="abc123def45", channel_id="UC123")
    encoded = event.to_base64_json()
    decoded = YouTubeNewVideoEvent.from_base64(encoded)
    assert decoded.video_id == event.video_id
    assert decoded.channel_id == event.channel_id
    assert decoded.event_version == "v1"


def test_mp3_ready_requires_gcs_uri():
    event = Mp3ReadyEvent(gcs_uri="gs://bucket/file.mp3", metadata_uri=None, video_id="vid123")
    assert event.gcs_uri.startswith("gs://")
    with pytest.raises(ValueError):
        Mp3ReadyEvent(gcs_uri="http://example.com", metadata_uri=None, video_id="vid123")


def test_decode_pubsub_message():
    original = Mp3DownloadEvent(
        video_id="vid123",
        channel_id="UC123",
        metadata={"title": "Sample"},
    )
    envelope = {
        "message": {
            "attributes": {"topic": TopicName.MP3_DOWNLOAD.value},
            "data": original.to_base64_json(),
        }
    }
    parsed = decode_pubsub_message(envelope, model=Mp3DownloadEvent)
    assert parsed.metadata["title"] == "Sample"


def test_diarization_ready_requires_gcs_uris():
    with pytest.raises(ValueError):
        DiarizationReadyEvent(
            mp3_uri="gs://bucket/file.mp3",
            diarized_uri="http://bad",
            metadata_uri=None,
            video_id="vid123",
            entities_uri=None,
        )


def test_cookie_request_event_roundtrip():
    event = YouTubeCookieRequestEvent(
        video_id="vid123",
        channel_id="UC123",
        reason="cookie_required",
        details="Sign in to confirm you're not a bot.",
    )
    encoded = event.to_base64_json()
    decoded = YouTubeCookieRequestEvent.from_base64(encoded)
    assert decoded.video_id == event.video_id
    assert decoded.reason == "cookie_required"
