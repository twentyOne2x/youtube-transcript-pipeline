from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient

from src.event_pipeline.schemas import (
    DiarizationReadyEvent,
    Mp3ReadyEvent,
    PumpfunClipEvent,
)
from src.event_pipeline.indexer.processor import IndexingResult
from src.event_pipeline.youtube.diarization import DiarizationResult


def _envelope(event) -> dict:
    return {"message": {"data": event.to_base64_json()}}


def test_pumpfun_pipeline_end_to_end(monkeypatch):
    from services import pumpfun_publisher, pumpfun_downloader, diarization_worker, diarization_indexer

    sample_event = PumpfunClipEvent(
        room="room-xyz",
        clip_id="clip-123",
        playlist_url="https://clips.example/playlist.m3u8",
        clip={"clipId": "clip-123"},
        coin={"name": "Room XYZ"},
    )

    published_messages: list[tuple[str, object, dict | None]] = []
    indexed_events: list[DiarizationReadyEvent] = []

    def record_publish(_, event, *, topic):
        published_messages.append((topic, event, {"source": topic.split("-")[0]}))

    # Pumpfun publisher setup
    monkeypatch.setattr(pumpfun_publisher.app, "_discover", lambda payload: [sample_event])
    monkeypatch.setattr(
        pumpfun_publisher.app,
        "get_settings",
        lambda: SimpleNamespace(pumpfun_clip_topic="pumpfun-clip"),
    )
    monkeypatch.setattr(
        pumpfun_publisher.app,
        "publish_event",
        lambda topic, event, attributes=None: record_publish("publisher", event, topic=topic),
    )

    # Pumpfun downloader setup
    mp3_event = Mp3ReadyEvent(
        gcs_uri="gs://bucket/pumpfun/room-xyz/clip-123.mp3",
        metadata_uri="gs://bucket/pumpfun/room-xyz/clip-123.json",
        video_id="pumpfun_room-xyz_clip-123",
    )
    monkeypatch.setattr(
        pumpfun_downloader.app,
        "process_clip_event",
        lambda event: mp3_event,
    )
    monkeypatch.setattr(
        pumpfun_downloader.app,
        "get_settings",
        lambda: SimpleNamespace(mp3_ready_topic="mp3-ready"),
    )
    monkeypatch.setattr(
        pumpfun_downloader.app,
        "publish_event",
        lambda topic, event, attributes=None: record_publish("downloader", event, topic=topic),
    )

    # Diarization worker setup
    diarization_result = DiarizationResult(
        diarized_path=Path("/tmp/diarized.json"),
        diarized_uri="gs://bucket/pumpfun/room-xyz/clip-123_diarized.json",
        entities_path=None,
        entities_uri=None,
    )
    monkeypatch.setattr(
        diarization_worker.app,
        "run_diarization",
        lambda event, api_key, bucket: diarization_result,
    )
    monkeypatch.setattr(
        diarization_worker.app,
        "get_settings",
        lambda: SimpleNamespace(
            assembly_ai_keys=[],
            assembly_ai_api_key="assemblyai-key",
            media_bucket="bucket",
            diarization_ready_topic="diarization-ready",
        ),
    )
    monkeypatch.setattr(
        diarization_worker.app,
        "publish_event",
        lambda topic, event, attributes=None: record_publish("diarizer", event, topic=topic),
    )

    # Diarization indexer setup
    def fake_process_event(event, settings):
        indexed_events.append(event)
        return IndexingResult(chunk_count=1, vector_count=1, deleted_mp3=False, deleted_diarized=False)

    monkeypatch.setattr(diarization_indexer.app, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(diarization_indexer.app, "process_event", fake_process_event)

    publisher_client = TestClient(pumpfun_publisher.app)
    downloader_client = TestClient(pumpfun_downloader.app)
    diarizer_client = TestClient(diarization_worker.app)
    indexer_client = TestClient(diarization_indexer.app)

    # Trigger publisher (empty body accepted)
    response = publisher_client.post("/trigger")
    assert response.status_code == 200
    assert response.json() == {"published": 1}

    # Extract the published PumpfunClipEvent
    pumpfun_messages = [e for topic, e, _ in published_messages if topic == "pumpfun-clip"]
    assert pumpfun_messages == [sample_event]

    # Simulate downloader invocation via Pub/Sub envelope
    envelope = _envelope(sample_event)
    response = downloader_client.post("/pubsub/push", json=envelope)
    assert response.status_code == 204

    mp3_messages = [e for topic, e, _ in published_messages if topic == "mp3-ready"]
    assert mp3_messages == [mp3_event]

    # Simulate diarization worker
    response = diarizer_client.post("/pubsub/push", json=_envelope(mp3_event))
    assert response.status_code == 204

    diarization_messages = [e for topic, e, _ in published_messages if topic == "diarization-ready"]
    assert len(diarization_messages) == 1
    diarization_event = diarization_messages[0]
    assert isinstance(diarization_event, DiarizationReadyEvent)
    assert diarization_event.diarized_uri.endswith("_diarized.json")

    # Simulate indexer receiving the message
    response = indexer_client.post("/pubsub/push", json=_envelope(diarization_event))
    assert response.status_code == 204
    assert indexed_events == [diarization_event]
