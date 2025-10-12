from __future__ import annotations

import logging
import os

import base64
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Request, Response

from src.event_pipeline.pubsub import publish_event
from src.event_pipeline.schemas import (
    Mp3DownloadEvent,
    YouTubeCookieRequestEvent,
    decode_pubsub_message,
)
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.downloader import build_ready_event, download_mp3

LOG = logging.getLogger(__name__)
app = FastAPI(title="YouTube MP3 Downloader")

DEFAULT_LOCAL_ROOT = "/tmp/youtube_audio"
_COOKIE_ENV = "YOUTUBE_COOKIE_FILE"
_COOKIE_TMP_ENV = "YOUTUBE_COOKIE_TMP"
_COOKIE_B64_ENV = "YOUTUBE_COOKIE_B64"
_COOKIE_SECRET_ENV = "YOUTUBE_COOKIE_SECRET"
_COOKIE_SECRET_VERSION_ENV = "YOUTUBE_COOKIE_SECRET_VERSION"

try:
    from google.cloud import secretmanager  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    secretmanager = None  # type: ignore

_COOKIE_ALERT_LOCK = Lock()
_COOKIE_ALERTED: set[str] = set()


def _ensure_env():
    _prepare_cookie_file()
    settings = get_settings()
    os.environ.setdefault("YOUTUBE_VIDEO_DIRECTORY", DEFAULT_LOCAL_ROOT)
    os.environ.setdefault("YOUTUBE_GCS_BUCKET", settings.media_bucket)
    os.environ.setdefault("YOUTUBE_GCS_PREFIX", "youtube_audio")
    return settings


def _prepare_cookie_file() -> None:
    """
    Ensure YOUTUBE_COOKIE_FILE points at a readable Netscape cookie file.
    Accepts either:
      * direct file path (already mounted via Secret Manager),
      * base64 payload via YOUTUBE_COOKIE_B64,
      * Secret Manager reference via YOUTUBE_COOKIE_SECRET (+ optional version).
    """

    def _tmp_path() -> Path:
        configured = os.environ.get(_COOKIE_TMP_ENV)
        if configured:
            return Path(configured)
        return Path("/tmp/youtube_cookies.txt")

    existing = os.environ.get(_COOKIE_ENV)
    if existing:
        path = Path(existing)
        if path.is_file():
            return
        LOG.warning("YOUTUBE_COOKIE_FILE defined but missing: %s", path)
        os.environ.pop(_COOKIE_ENV, None)

    # Base64 override
    cookie_b64 = os.environ.get(_COOKIE_B64_ENV)
    if cookie_b64:
        try:
            decoded = base64.b64decode(cookie_b64)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.error("Failed to decode YOUTUBE_COOKIE_B64: %s", exc)
        else:
            tmp = _tmp_path()
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(decoded)
            os.environ[_COOKIE_ENV] = tmp.as_posix()
            LOG.info("Loaded YouTube cookies from YOUTUBE_COOKIE_B64 into %s", tmp)
            return

    # Secret Manager reference
    secret_name = os.environ.get(_COOKIE_SECRET_ENV)
    if secret_name:
        if secretmanager is None:
            LOG.error(
                "YOUTUBE_COOKIE_SECRET provided but google-cloud-secret-manager is not installed."
            )
        else:
            version = os.environ.get(_COOKIE_SECRET_VERSION_ENV, "latest")
            full_name = secret_name
            if "/versions/" not in full_name:
                full_name = f"{secret_name}/versions/{version}"
            try:
                client = secretmanager.SecretManagerServiceClient()
                response = client.access_secret_version(request={"name": full_name})
            except Exception as exc:  # pragma: no cover - network call
                LOG.error("Failed to access Secret Manager for cookies: %s", exc)
            else:
                tmp = _tmp_path()
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(response.payload.data)
                os.environ[_COOKIE_ENV] = tmp.as_posix()
                LOG.info("Loaded YouTube cookies from Secret Manager version %s", full_name)
                return

    # No cookies available; leave unset.
    if _COOKIE_ENV in os.environ:
        return
    LOG.info("Proceeding without YOUTUBE_COOKIE_FILE (unauthenticated downloads only).")


def _looks_like_cookie_error(message: str) -> bool:
    m = message.lower()
    return any(
        token in m
        for token in (
            "sign in to confirm",
            "cookies",
            "cookie file",
            "login required",
            "account issue",
        )
    )


def _should_alert_cookie(video_id: str) -> bool:
    if not video_id:
        return True
    with _COOKIE_ALERT_LOCK:
        if video_id in _COOKIE_ALERTED:
            return False
        _COOKIE_ALERTED.add(video_id)
        return True


def _publish_cookie_request(settings, event: Mp3DownloadEvent, reason: str) -> None:
    topic = getattr(settings, "youtube_cookie_topic", None)
    if not topic:
        LOG.warning("Cookie request detected but YOUTUBE_COOKIE_TOPIC not configured.")
        return
    payload = YouTubeCookieRequestEvent(
        video_id=event.video_id,
        channel_id=event.channel_id,
        reason="cookie_required",
        details=reason[:500],
    )
    publish_event(
        topic,
        payload,
        attributes={"source": "youtube-cookie"},
    )
    LOG.info("Published YouTube cookie request for %s", event.video_id)


@app.post("/pubsub/push")
async def handle_pubsub_push(request: Request) -> Response:
    body = await request.json()
    event = decode_pubsub_message(body, model=Mp3DownloadEvent)
    settings = _ensure_env()
    try:
        artifact = download_mp3(event)
    except RuntimeError as exc:
        message = str(exc)
        if _looks_like_cookie_error(message) and _should_alert_cookie(event.video_id):
            _publish_cookie_request(settings, event, message)
        raise HTTPException(status_code=500, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    ready_event = build_ready_event(artifact, event)
    publish_event(
        settings.mp3_ready_topic,
        ready_event,
        attributes={"source": "mp3-downloader"},
    )
    LOG.info("Published mp3-ready event for %s", event.video_id)
    return Response(status_code=204)
