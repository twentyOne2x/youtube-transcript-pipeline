from __future__ import annotations

import mimetypes
import os
import subprocess
import threading
import tempfile
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


def split_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")
    without_scheme = uri[len("gs://") :]
    bucket, _, blob = without_scheme.partition("/")
    if not bucket or not blob:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return bucket, blob


def download_file(uri: str, destination: Optional[Path] = None) -> Path:
    bucket_name, blob_name = split_gs_uri(uri)
    if destination is None:
        suffix = Path(blob_name).suffix or ""
        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        destination = Path(temp_path)

    try:
        client = _storage_client()
    except (DefaultCredentialsError, ImportError):
        subprocess.run(
            ["gsutil", "cp", uri, destination.as_posix()],
            check=True,
        )
        return destination

    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.download_to_filename(destination.as_posix())
    return destination


def delete_blob(uri: str) -> None:
    bucket_name, blob_name = split_gs_uri(uri)
    try:
        client = _storage_client()
    except (DefaultCredentialsError, ImportError):
        subprocess.run(["gsutil", "rm", uri], check=True)
        return
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.delete()
