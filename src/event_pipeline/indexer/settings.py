from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IndexerSettings(BaseSettings):
    """Environment-driven configuration for the diarization indexer."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    pinecone_api_key: str = Field(..., alias="PINECONE_API_KEY")
    pinecone_index: str = Field(..., alias="PINECONE_INDEX")
    pinecone_namespace: str | None = Field(default=None, alias="PINECONE_NAMESPACE")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    chunk_max_chars: int = Field(default=1200, alias="INDEXER_CHUNK_MAX_CHARS")
    delete_source_mp3: bool = Field(default=False, alias="INDEXER_DELETE_MP3")
    delete_diarized_json: bool = Field(default=False, alias="INDEXER_DELETE_DIARIZED")


@lru_cache(maxsize=1)
def get_indexer_settings() -> IndexerSettings:
    return IndexerSettings()  # type: ignore[arg-type]
