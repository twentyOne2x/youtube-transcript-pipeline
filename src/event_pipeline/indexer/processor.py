from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

try:
    from openai import OpenAI  # type: ignore
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError("openai package is required for the diarization indexer") from exc

try:
    from pinecone import Pinecone  # type: ignore
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError("pinecone package is required for the diarization indexer") from exc

from src.event_pipeline.schemas import DiarizationReadyEvent
from src.utils.gcs import delete_blob, download_file

from .settings import IndexerSettings

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class Chunk:
    text: str
    start_sec: Optional[float]
    end_sec: Optional[float]
    speaker: Optional[str]
    index: int


@dataclass(slots=True)
class IndexingResult:
    chunk_count: int
    vector_count: int
    deleted_mp3: bool
    deleted_diarized: bool


def process_event(
    event: DiarizationReadyEvent,
    settings: IndexerSettings,
    *,
    embedding_client: Optional[OpenAI] = None,
    pinecone_index=None,
) -> IndexingResult:
    """
    Convert a diarization-ready payload into Pinecone vectors.

    Args:
        event: Incoming diarization payload.
        settings: Runtime configuration.
        embedding_client: Optional OpenAI client override (primarily for tests).
        pinecone_index: Optional Pinecone index handle override.
    """
    LOG.info("Indexing diarization payload for %s", event.video_id)
    diarized_data = _load_json(event.diarized_uri)
    metadata = _load_metadata(event.metadata_uri)
    chunks = list(_generate_chunks(diarized_data, settings.chunk_max_chars))
    if not chunks:
        LOG.warning("No content extracted from %s; skipping index.", event.diarized_uri)
        return IndexingResult(chunk_count=0, vector_count=0, deleted_mp3=False, deleted_diarized=False)

    embedding_client = embedding_client or OpenAI(api_key=settings.openai_api_key)
    pinecone_index = pinecone_index or _pinecone_index(settings)

    embeddings = _embed_chunks(chunks, embedding_client, settings.embedding_model)
    vectors = _build_vectors(event, chunks, embeddings, metadata)
    _upsert_vectors(pinecone_index, vectors, settings.pinecone_namespace)

    deleted_mp3 = False
    deleted_diarized = False
    if settings.delete_source_mp3:
        try:
            delete_blob(event.mp3_uri)
            deleted_mp3 = True
            LOG.info("Deleted source MP3 after indexing: %s", event.mp3_uri)
        except Exception as exc:  # pragma: no cover - network side-effect
            LOG.warning("Failed to delete MP3 %s: %s", event.mp3_uri, exc)
    if settings.delete_diarized_json:
        try:
            delete_blob(event.diarized_uri)
            deleted_diarized = True
            LOG.info("Deleted diarized JSON after indexing: %s", event.diarized_uri)
        except Exception as exc:  # pragma: no cover - network side-effect
            LOG.warning("Failed to delete diarized output %s: %s", event.diarized_uri, exc)

    return IndexingResult(
        chunk_count=len(chunks),
        vector_count=len(vectors),
        deleted_mp3=deleted_mp3,
        deleted_diarized=deleted_diarized,
    )


def _pinecone_index(settings: IndexerSettings):
    client = Pinecone(api_key=settings.pinecone_api_key)
    return client.Index(settings.pinecone_index)


def _load_json(uri: str) -> Sequence[dict]:
    path = download_file(uri)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    finally:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
    if not isinstance(data, Sequence):
        raise ValueError(f"Diarization payload must be a sequence, got {type(data)!r}")
    return data


def _load_metadata(uri: Optional[str]) -> dict:
    if not uri:
        return {}
    path = download_file(uri)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
    if isinstance(payload, dict):
        return payload
    LOG.warning("Unexpected metadata shape (%s); ignoring.", type(payload).__name__)
    return {}


def _generate_chunks(entries: Sequence[dict], max_chars: int) -> Iterable[Chunk]:
    index = 0
    for entry in entries:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        speaker = entry.get("speaker")
        words = entry.get("words") or []
        if not isinstance(words, list) or not words:
            words = [
                {
                    "text": text,
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                }
            ]
        chunk_words: List[str] = []
        total_chars = 0
        chunk_start = None
        last_end = None

        for word in words:
            token = str(word.get("text") or "").strip()
            if not token:
                continue
            token_len = len(token) + (1 if chunk_words else 0)
            candidate_len = total_chars + token_len
            if chunk_words and candidate_len > max_chars:
                yield _build_chunk(
                    chunk_words,
                    chunk_start if chunk_start is not None else entry.get("start"),
                    last_end if last_end is not None else entry.get("end"),
                    speaker,
                    index,
                )
                index += 1
                chunk_words = []
                total_chars = 0
                chunk_start = None

            if chunk_start is None:
                chunk_start = word.get("start", entry.get("start"))
            chunk_words.append(token)
            total_chars += token_len
            last_end = word.get("end", entry.get("end"))

        if chunk_words:
            yield _build_chunk(
                chunk_words,
                chunk_start if chunk_start is not None else entry.get("start"),
                last_end if last_end is not None else entry.get("end"),
                speaker,
                index,
            )
            index += 1


def _build_chunk(words: List[str], start: Optional[float], end: Optional[float], speaker: Optional[str], index: int) -> Chunk:
    return Chunk(
        text=" ".join(words).strip(),
        start_sec=_to_seconds(start),
        end_sec=_to_seconds(end),
        speaker=speaker,
        index=index,
    )


def _to_seconds(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric / 1000.0


def _embed_chunks(chunks: Sequence[Chunk], client: OpenAI, model: str) -> List[List[float]]:
    texts = [chunk.text for chunk in chunks if chunk.text]
    if not texts:
        return []
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


def _build_vectors(
    event: DiarizationReadyEvent,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
    metadata: dict,
) -> List[dict]:
    if len(chunks) != len(embeddings):
        raise ValueError("Embedding count does not match chunk count.")
    source = _infer_source(event)
    clip_meta = metadata.get("clip", {}) if isinstance(metadata.get("clip"), dict) else {}
    coin_meta = metadata.get("coin", {}) if isinstance(metadata.get("coin"), dict) else {}

    vectors: List[dict] = []
    for chunk, embedding in zip(chunks, embeddings):
        vector_id = f"{event.video_id}:{chunk.index:04d}"
        payload = {
            "video_id": event.video_id,
            "source": source,
            "chunk_index": chunk.index,
            "text": chunk.text,
            "start_sec": chunk.start_sec,
            "end_sec": chunk.end_sec,
            "speaker": chunk.speaker,
            "mp3_uri": event.mp3_uri,
            "diarized_uri": event.diarized_uri,
            "metadata_uri": event.metadata_uri,
        }
        if source == "pumpfun":
            payload.update(
                {
                    "pumpfun_clip_id": clip_meta.get("clipId"),
                    "pumpfun_room": clip_meta.get("roomName") or clip_meta.get("room"),
                    "pumpfun_coin_name": coin_meta.get("name"),
                    "pumpfun_coin_symbol": coin_meta.get("symbol"),
                    "pumpfun_start_time": clip_meta.get("startTime"),
                    "pumpfun_end_time": clip_meta.get("endTime"),
                }
            )
        vectors.append(
            {
                "id": vector_id,
                "values": list(map(float, embedding)),
                "metadata": {k: v for k, v in payload.items() if v is not None},
            }
        )
    return vectors


def _upsert_vectors(index, vectors: Sequence[dict], namespace: Optional[str]) -> None:
    if not vectors:
        return
    batch_size = 100
    for idx in range(0, len(vectors), batch_size):
        batch = vectors[idx : idx + batch_size]
        if namespace:
            index.upsert(vectors=batch, namespace=namespace)
        else:
            index.upsert(vectors=batch)


def _infer_source(event: DiarizationReadyEvent) -> str:
    uri = event.mp3_uri.lower()
    if "pumpfun" in uri:
        return "pumpfun"
    if "youtube" in uri:
        return "youtube"
    return "unknown"
