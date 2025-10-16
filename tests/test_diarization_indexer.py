from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.event_pipeline.indexer.processor import process_event
from src.event_pipeline.indexer.settings import IndexerSettings
from src.event_pipeline.schemas import DiarizationReadyEvent


@pytest.fixture
def fake_settings():
    return IndexerSettings(
        pinecone_api_key="test-key",
        pinecone_index="test-index",
        pinecone_namespace="test-namespace",
        openai_api_key="sk-test",
        embedding_model="text-embedding-test",
        chunk_max_chars=20,
        delete_source_mp3=True,
        delete_diarized_json=True,
    )


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = self
        self.last_input = None

    def create(self, model: str, input: list[str]):
        self.last_input = input
        vectors = []
        for idx, _ in enumerate(input):
            vectors.append(SimpleNamespace(embedding=[float(idx), float(idx) + 0.5]))
        return SimpleNamespace(data=vectors)


class FakePineconeIndex:
    def __init__(self):
        self.upserts = []

    def upsert(self, vectors, namespace=None):
        self.upserts.append((vectors, namespace))


def test_process_event_indexes_vectors(monkeypatch, tmp_path, fake_settings):
    diarized_payload = [
        {
            "text": "Hello pumpfun world this is a test clip.",
            "start": 0,
            "end": 4200,
            "speaker": "A",
            "words": [
                {"text": "Hello", "start": 0, "end": 400},
                {"text": "pumpfun", "start": 400, "end": 800},
                {"text": "world", "start": 800, "end": 1200},
                {"text": "this", "start": 1200, "end": 1600},
                {"text": "is", "start": 1600, "end": 2000},
                {"text": "a", "start": 2000, "end": 2400},
                {"text": "test", "start": 2400, "end": 2800},
                {"text": "clip.", "start": 2800, "end": 3200},
            ],
        }
    ]
    metadata_payload = {
        "clip": {"clipId": "clip123", "roomName": "roomXYZ", "startTime": "2025-01-01T00:00:00Z", "endTime": "2025-01-01T01:00:00Z"},
        "coin": {"name": "Test Coin", "symbol": "TCOIN"},
    }

    diarized_path = tmp_path / "diarized.json"
    diarized_path.write_text(json.dumps(diarized_payload), encoding="utf-8")
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata_payload), encoding="utf-8")

    def fake_download(uri: str):
        if uri.endswith("diarized.json"):
            return diarized_path
        if uri.endswith("metadata.json"):
            return metadata_path
        raise ValueError(f"Unexpected URI: {uri}")

    deleted = []

    def fake_delete(uri: str):
        deleted.append(uri)

    monkeypatch.setattr("src.event_pipeline.indexer.processor.download_file", fake_download)
    monkeypatch.setattr("src.event_pipeline.indexer.processor.delete_blob", fake_delete)

    event = DiarizationReadyEvent(
        mp3_uri="gs://bucket/pumpfun_streams/roomXYZ/clip.mp3",
        diarized_uri="gs://bucket/diarized.json",
        metadata_uri="gs://bucket/metadata.json",
        video_id="pumpfun_roomXYZ_clip123",
        entities_uri=None,
    )

    embedding_client = FakeEmbeddingClient()
    pinecone_index = FakePineconeIndex()

    result = process_event(
        event,
        fake_settings,
        embedding_client=embedding_client,
        pinecone_index=pinecone_index,
    )

    assert result.vector_count == len(pinecone_index.upserts[0][0])
    assert result.chunk_count == len(embedding_client.last_input)
    assert pinecone_index.upserts[0][1] == "test-namespace"

    vector_metadata = pinecone_index.upserts[0][0][0]["metadata"]
    assert vector_metadata["source"] == "pumpfun"
    assert vector_metadata["pumpfun_coin_name"] == "Test Coin"
    assert vector_metadata["pumpfun_clip_id"] == "clip123"

    # Ensure cleanup toggles were respected
    assert "gs://bucket/pumpfun_streams/roomXYZ/clip.mp3" in deleted
    assert "gs://bucket/diarized.json" in deleted
