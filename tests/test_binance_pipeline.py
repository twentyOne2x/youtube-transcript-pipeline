from __future__ import annotations

from pathlib import Path

import pytest

from src.data_ingestion_binance.course import CourseVideo
from src.event_pipeline.binance.publisher import discover_courses
from src.event_pipeline.binance.downloader import process_course_event
from src.event_pipeline.schemas import BinanceCourseEvent


@pytest.fixture(autouse=True)
def binance_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_OUTPUT_DIR", str(tmp_path / "binance"))
    monkeypatch.setenv("BINANCE_GCS_BUCKET", "bucket")
    monkeypatch.setenv("BINANCE_GCS_PREFIX", "binance")
    yield
    monkeypatch.delenv("BINANCE_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("BINANCE_GCS_BUCKET", raising=False)
    monkeypatch.delenv("BINANCE_GCS_PREFIX", raising=False)


def test_discover_courses(monkeypatch):
    from src.event_pipeline import binance as binance_pkg

    def fake_collect(base, language, timeout=30):
        if language == "en":
            return ["https://academy/binance/beginner", "https://academy/binance/intermediate"]
        return ["https://academy/binance/pro"]

    monkeypatch.setattr(binance_pkg.publisher, "collect_course_urls", fake_collect)

    events = discover_courses(languages=["en", "en", "es"], limit_per_language=1)
    assert len(events) == 2
    languages = {event.language for event in events}
    assert languages == {"en", "es"}


def test_process_course_event(monkeypatch, tmp_path):
    from src.event_pipeline import binance as binance_pkg

    video = CourseVideo(
        course_slug="intro",
        course_title="Intro",
        language="en",
        video_link="https://wistia.com/abc",
        translation_id="tr",
        description=None,
        article_slug=None,
    )

    monkeypatch.setattr(binance_pkg.downloader, "parse_course", lambda *args, **kwargs: [video])

    def fake_download_video(course_video, settings, session=None):
        clip_dir = Path(settings.ensure_output_dir()) / "en" / "intro" / "clip"
        clip_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = clip_dir / "clip.mp4"
        mp4_path.write_bytes(b"mp4")
        metadata_path = clip_dir / "metadata.json"
        metadata_path.write_text("{}", encoding="utf-8")
        return mp4_path

    monkeypatch.setattr(binance_pkg.downloader, "download_video", fake_download_video)
    monkeypatch.setattr(binance_pkg.downloader, "_convert_to_mp3", lambda path: path.with_suffix(".mp3"))

    def fake_upload(mp3_path, metadata_path, settings, base_dir):
        return "gs://bucket/binance/audio.mp3", "gs://bucket/binance/meta.json"

    monkeypatch.setattr(binance_pkg.downloader, "_upload_outputs", fake_upload)

    event = BinanceCourseEvent(course_url="https://academy/binance/beginner", language="en")
    ready_events = process_course_event(event)

    assert len(ready_events) == 1
    ready = ready_events[0]
    assert ready.gcs_uri.endswith("audio.mp3")
    assert ready.metadata_uri.endswith("meta.json")
    assert ready.video_id.startswith("binance_en")
