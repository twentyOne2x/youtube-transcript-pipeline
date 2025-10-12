from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Sequence, Set

from src.data_ingestion_binance.config import BinanceSettings
from src.data_ingestion_binance.sitemap import collect_course_urls
from src.event_pipeline.schemas import BinanceCourseEvent

LOG = logging.getLogger(__name__)


def _normalize_languages(values: Optional[Iterable[str]], settings: BinanceSettings) -> List[str]:
    if values:
        langs = [value.strip() for value in values if value and value.strip()]
        if langs:
            return langs
    return list(settings.languages)


def _collect_courses(
    language: str,
    *,
    include_courses: Optional[Sequence[str]],
    limit: Optional[int],
    settings: BinanceSettings,
) -> List[str]:
    if include_courses:
        filtered = [url for url in include_courses if url]
        return filtered[:limit] if limit else filtered
    urls = collect_course_urls(settings.sitemap_base, language, timeout=settings.request_timeout)
    if limit:
        return urls[:limit]
    return urls


def discover_courses(
    *,
    languages: Optional[Iterable[str]] = None,
    include_courses: Optional[Sequence[str]] = None,
    limit_per_language: Optional[int] = None,
    settings: Optional[BinanceSettings] = None,
) -> List[BinanceCourseEvent]:
    """
    Build Binance course events for downstream processing.

    Args:
        languages: Optional explicit language codes.
        include_courses: Optional explicit course URLs.
        limit_per_language: Optional cap per language.
        settings: Optional BinanceSettings instance.
    """
    base_settings = settings or BinanceSettings()
    effective_settings = replace(base_settings)
    events: List[BinanceCourseEvent] = []
    seen: Set[str] = set()
    for language in _normalize_languages(languages, effective_settings):
        course_urls = _collect_courses(
            language,
            include_courses=include_courses,
            limit=limit_per_language,
            settings=effective_settings,
        )
        LOG.info("Discovered %d Binance courses for %s", len(course_urls), language)
        for url in course_urls:
            key = f"{language}:{url}"
            if key in seen:
                continue
            seen.add(key)
            events.append(
                BinanceCourseEvent(
                    course_url=url,
                    language=language,
                )
            )
    return events
