from __future__ import annotations

import mimetypes
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

try:
    from google.auth.exceptions import DefaultCredentialsError  # type: ignore
except ImportError:  # pragma: no cover - fallback for minimal environments
    class DefaultCredentialsError(Exception):
        """Fallback when google-auth is unavailable."""

        pass

try:
    from google.cloud import storage  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    storage = None  # type: ignore

_client_lock = threading.Lock()


@lru_cache(maxsize=1)
def _storage_client() -> Any:
    if storage is None:  # pragma: no cover - requires google-cloud-storage
        raise ImportError("google-cloud-storage is required for storage client operations")
    with _client_lock:
        return storage.Client()


def _guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(path.as_posix())
    return ctype or "application/octet-stream"


def upload_file(local_path: Path, bucket_name: str, blob_name: str) -> None:
    try:
        client = _storage_client()
    except (DefaultCredentialsError, ImportError):
        _upload_with_gsutil(local_path, bucket_name, blob_name)
        return

    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.content_type = _guess_content_type(local_path)
    blob.upload_from_filename(local_path.as_posix())


def blob_exists(bucket_name: str, blob_name: str) -> bool:
    """
    Check whether a blob exists in GCS. Falls back to `gsutil stat` when the
    storage client cannot be initialized (e.g. missing ADC).
    """
    try:
        client = _storage_client()
    except (DefaultCredentialsError, ImportError):
        dest = f"gs://{bucket_name}/{blob_name}"
        result = subprocess.run(
            ["gsutil", "-q", "stat", dest],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return blob.exists()


def _upload_with_gsutil(local_path: Path, bucket_name: str, blob_name: str) -> None:
    dest = f"gs://{bucket_name}/{blob_name}"
    subprocess.run([
        "gsutil",
        "cp",
        local_path.as_posix(),
        dest,
    ], check=True)


def maybe_upload(
    local_path: Path,
    *,
    bucket: Optional[str],
    prefix: Optional[str] = None,
    relative_key: Optional[str] = None,
) -> Optional[str]:
    """
    Upload `local_path` to GCS when a bucket is provided.

    Args:
        local_path: File to upload.
        bucket: Target GCS bucket (`None` disables uploads).
        prefix: Optional directory prefix.
        relative_key: Optional key to append; defaults to the file name.

    Returns:
        gs:// URI when uploaded, otherwise ``None``.
    """
    if not bucket:
        return None
    key = relative_key or local_path.name
    parts = [p for p in (prefix or "", key) if p]
    blob_name = "/".join(parts)
    upload_file(local_path, bucket, blob_name)
    return f"gs://{bucket}/{blob_name}"
