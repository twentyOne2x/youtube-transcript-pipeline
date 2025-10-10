from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
import logging

import requests

from .utils import extract_app_data, default_headers

LOG = logging.getLogger(__name__)


@dataclass
class CourseVideo:
    course_slug: str
    course_title: str
    language: str
    video_link: str
    translation_id: str
    description: Optional[str]
    article_slug: Optional[str]


def _pick_course_blob(app_data: Dict) -> Optional[Dict]:
    loader = app_data.get("appState", {}).get("loader", {}).get("dataByRouteId", {})
    for blob in loader.values():
        course = blob.get("course")
        if course and isinstance(course, dict):
            return course
    return None


def parse_course(url: str, language: str, timeout: int = 30, session: Optional[requests.Session] = None) -> List[CourseVideo]:
    sess = session or requests.Session()
    sess.headers.update(default_headers())
    LOG.debug("Fetching course page %s", url)
    response = sess.get(url, timeout=timeout)
    response.raise_for_status()
    app_data = extract_app_data(response.text)
    course_blob = _pick_course_blob(app_data)
    if not course_blob:
        LOG.warning("No course data found for %s", url)
    content = course_blob.get("content", []) if course_blob else []
    course_slug = url.rstrip("/").split("/")[-1]
    course_title = course_blob.get("courseName") if course_blob else course_slug
    videos: List[CourseVideo] = []
    for item in content:
        video_link = item.get("videoLink")
        chapter_type = item.get("chapterType")
        translation_id = item.get("translationId")
        if not video_link or chapter_type != "VIDEO":
            continue
        description = None
        try:
            content_blob = item.get("content")
            if isinstance(content_blob, str):
                # content is a small JSON string containing copy text
                # but may not always be present.
                import json  # local import to avoid overhead when unused

                description = json.loads(content_blob)
        except Exception:
            description = None
        videos.append(
            CourseVideo(
                course_slug=course_slug,
                course_title=course_title,
                language=language,
                video_link=video_link,
                translation_id=translation_id or "",
                description=description if isinstance(description, str) else None,
                article_slug=item.get("articleId"),
            )
        )
    return videos
