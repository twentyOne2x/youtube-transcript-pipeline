from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import assemblyai as aai
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage

from src.event_pipeline.schemas import DiarizationReadyEvent, Mp3ReadyEvent
from src.utils.gcs import maybe_upload

LOG = logging.getLogger(__name__)


@dataclass
class DiarizationResult:
    diarized_path: Path
    diarized_uri: str
    entities_path: Optional[Path]
    entities_uri: Optional[str]


def _output_dir() -> Path:
    root = os.getenv("DIARIZATION_OUTPUT_DIR", "/tmp/diarization")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prefix(event: Mp3ReadyEvent) -> str:
    return "/".join(
        p for p in ("youtube_diarized", event.video_id) if p
    )


def run_diarization(event: Mp3ReadyEvent, api_key: str, bucket: str) -> DiarizationResult:
    if not api_key:
        raise ValueError("AssemblyAI API key required for diarization.")
    aai.settings.api_key = api_key
    transcriber = aai.Transcriber()
    LOG.info("Submitting %s for diarization", event.gcs_uri)
    config = aai.TranscriptionConfig(
        speaker_labels=True,
        entity_detection=True,
    )
    source_path = _resolve_audio_source(event.gcs_uri)
    try:
        transcript = transcriber.transcribe(source_path, config=config)
    finally:
        if source_path != event.gcs_uri and Path(source_path).exists():
            try:
                Path(source_path).unlink()
            except OSError:
                pass

    output_dir = _output_dir()
    diarized_path = output_dir / f"{event.video_id}_diarized.json"
    diarized_path.write_text(json.dumps(transcript.json_response, indent=2), encoding="utf-8")

    entities = transcript.json_response.get("entities") if hasattr(transcript, "json_response") else None
    entities_path: Optional[Path] = None
    if entities:
        entities_path = output_dir / f"{event.video_id}_entities.json"
        entities_path.write_text(json.dumps(entities, indent=2), encoding="utf-8")

    prefix = _prefix(event)
    diarized_uri = maybe_upload(diarized_path, bucket=bucket, prefix=prefix)
    entities_uri = None
    if entities_path:
        entities_uri = maybe_upload(entities_path, bucket=bucket, prefix=prefix)

    if not diarized_uri:
        raise RuntimeError("Failed to upload diarization output to GCS.")

    return DiarizationResult(
        diarized_path=diarized_path,
        diarized_uri=diarized_uri,
        entities_path=entities_path,
        entities_uri=entities_uri,
    )


def build_ready_event(src_event: Mp3ReadyEvent, result: DiarizationResult) -> DiarizationReadyEvent:
    return DiarizationReadyEvent(
        mp3_uri=src_event.gcs_uri,
        diarized_uri=result.diarized_uri,
        entities_uri=result.entities_uri,
    )


def _resolve_audio_source(gcs_or_path: str) -> str:
    if gcs_or_path.startswith("gs://"):
        return _download_gcs_object(gcs_or_path)
    return gcs_or_path


def _download_gcs_object(uri: str) -> str:
    bucket_name, blob_name = _split_gs_uri(uri)
    fd, temp_path = tempfile.mkstemp(suffix=Path(blob_name).suffix or ".mp3")
    os.close(fd)
    tmp = Path(temp_path)
    try:
        client = storage.Client()
        client.bucket(bucket_name).blob(blob_name).download_to_filename(tmp.as_posix())
    except DefaultCredentialsError:
        subprocess.run(["gsutil", "cp", uri, tmp.as_posix()], check=True)
    return tmp.as_posix()


def _split_gs_uri(uri: str) -> tuple[str, str]:
    without_scheme = uri[len("gs://") :]
    bucket, _, blob = without_scheme.partition("/")
    if not bucket or not blob:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return bucket, blob
