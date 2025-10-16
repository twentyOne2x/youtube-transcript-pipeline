"""Utilities for transforming diarization outputs into vector-store entries."""

from .processor import process_event, IndexingResult
from .settings import IndexerSettings

__all__ = [
    "process_event",
    "IndexerSettings",
    "IndexingResult",
]
