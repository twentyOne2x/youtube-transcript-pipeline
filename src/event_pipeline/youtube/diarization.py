from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import assemblyai as aai

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
    transcript = transcriber.transcribe(audio_url=event.gcs_uri, config=config)

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
