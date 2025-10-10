import os
import subprocess
import uuid

import pytest
from dotenv import load_dotenv
from google.api_core.exceptions import Forbidden, NotFound
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage


@pytest.mark.integration
def test_media_bucket_exists_and_allows_roundtrip(tmp_path):
    load_dotenv()
    bucket_name = os.getenv("MEDIA_BUCKET", "media-just-skyline-474622-e1")

    try:
        client = storage.Client()
    except DefaultCredentialsError:
        _roundtrip_with_gsutil(bucket_name, tmp_path)
        return

    bucket = client.lookup_bucket(bucket_name)
    if bucket is None:
        raise AssertionError(f"GCS bucket '{bucket_name}' not found.")

    blob_name = f"tests/pipeline-integration-{uuid.uuid4()}.txt"
    blob = bucket.blob(blob_name)

    payload = "youtube-pipeline integration test"

    blob.upload_from_string(payload, content_type="text/plain")
    try:
        downloaded = blob.download_as_text()
        assert downloaded == payload
    except Forbidden:
        # Some principals only have writer access; confirm object existence instead.
        blobs = list(bucket.list_blobs(prefix=blob_name))
        assert any(b.name == blob_name for b in blobs), "Uploaded object not visible after write."
    except NotFound as exc:
        raise AssertionError(f"Failed to roundtrip object against bucket '{bucket_name}': {exc}") from exc
    finally:
        try:
            blob.delete()
        except Exception:
            # Best effort cleanup; do not fail test if deletion encounters a transient error.
            pass


def _roundtrip_with_gsutil(bucket_name: str, tmp_path):
    dest_path = f"gs://{bucket_name}/tests/pipeline-integration-{uuid.uuid4()}.txt"
    local_file = tmp_path / "payload.txt"
    local_file.write_text("youtube-pipeline integration test", encoding="utf-8")

    subprocess.run(["gsutil", "cp", local_file.as_posix(), dest_path], check=True)

    try:
        subprocess.run(["gsutil", "ls", dest_path], check=True, capture_output=True, text=True)
    finally:
        subprocess.run(["gsutil", "rm", dest_path], check=False)
