from __future__ import annotations

import pytest

from src.data_ingestion_pumpfun.downloader import ClipDownloadResult
from src.event_pipeline.pumpfun.publisher import discover_clip_events
from src.event_pipeline.pumpfun.downloader import process_clip_event
from src.event_pipeline.schemas import PumpfunClipEvent


@pytest.fixture
def pumpfun_event():
    return PumpfunClipEvent(
        room="abcd",
        clip_id="123",
        playlist_url="https://example.com/playlist.m3u8",
        clip={"clipId": "123", "playlistUrl": "https://example.com"},
        coin={"name": "Demo"},
    )


def test_discover_clip_events(monkeypatch, tmp_path):
    from src.event_pipeline.pumpfun import publisher

    def fake_fetch_coin(room, settings, session=None):
        return {"name": "Coin"}

    def fake_iter_clips(room, settings, max_results=None, session=None):
        yield {"clipId": "1", "playlistUrl": "https://cdn/1.m3u8"}
        yield {"clipId": "2", "playlistUrl": "https://cdn/2.m3u8"}

    monkeypatch.setattr(publisher, "fetch_coin_metadata", fake_fetch_coin)
    monkeypatch.setattr(publisher, "iter_clips", fake_iter_clips)
    monkeypatch.setattr(publisher, "hydrate_room_labels", lambda entries, settings, session=None: False)

    events = discover_clip_events(config_path=None, rooms=["room-1"], max_results_override=2)
    assert len(events) == 2
    assert events[0].room == "room-1"
    assert events[0].clip_id == "1"


def test_process_clip_event_generates_ready(monkeypatch, pumpfun_event, tmp_path):
    from src.event_pipeline import pumpfun as pumpfun_pkg

    def fake_download_clip(**kwargs):
        return ClipDownloadResult(
            room_name="abcd",
            clip_id="123",
            mp4_path=None,
            mp3_path=None,
            skipped=False,
            metadata_path=None,
            gcs_mp4_uri="gs://bucket/pumpfun/clip.mp4",
            gcs_mp3_uri="gs://bucket/pumpfun/clip.mp3",
            gcs_metadata_uri="gs://bucket/pumpfun/clip.json",
        )

    class FakeSettings:
        gcs_bucket = "bucket"
        gcs_prefix = "pumpfun"
        keep_local_files = False
        skip_existing = True

    monkeypatch.setattr(pumpfun_pkg.downloader, "_ensure_settings", lambda: FakeSettings())
    monkeypatch.setattr(pumpfun_pkg.downloader, "download_clip", fake_download_clip)

    ready = process_clip_event(pumpfun_event)
    assert ready is not None
    assert ready.gcs_uri.endswith("clip.mp3")
    assert ready.video_id.startswith("pumpfun_abcd_123")


def test_process_clip_event_skipped(monkeypatch, pumpfun_event):
    from src.event_pipeline import pumpfun as pumpfun_pkg

    def fake_download_clip(**kwargs):
        return ClipDownloadResult(
            room_name="abcd",
            clip_id="123",
            mp4_path=None,
            mp3_path=None,
            skipped=True,
            metadata_path=None,
        )

    class FakeSettings:
        gcs_bucket = "bucket"
        gcs_prefix = "pumpfun"
        keep_local_files = False
        skip_existing = True

    monkeypatch.setattr(pumpfun_pkg.downloader, "_ensure_settings", lambda: FakeSettings())
    monkeypatch.setattr(pumpfun_pkg.downloader, "download_clip", fake_download_clip)

    ready = process_clip_event(pumpfun_event)
    assert ready is None
