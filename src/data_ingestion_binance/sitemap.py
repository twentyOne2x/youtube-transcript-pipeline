from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List
import logging
import xml.etree.ElementTree as ET

import requests

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    priority: float | None = None


def fetch_sitemap(url: str, timeout: int = 30) -> Iterable[SitemapEntry]:
    LOG.debug("Fetching sitemap %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for url_elem in root.findall("sm:url", ns):
        loc = url_elem.findtext("sm:loc", default="", namespaces=ns).strip()
        if not loc:
            continue
        priority = url_elem.findtext("sm:priority", default="", namespaces=ns)
        try:
            priority_value = float(priority) if priority else None
        except ValueError:
            priority_value = None
        yield SitemapEntry(url=loc, priority=priority_value)


def collect_course_urls(base: str, language: str, timeout: int = 30) -> List[str]:
    sitemap_url = f"{base.rstrip('/')}/sitemap_course_{language}.xml"
    course_urls = []
    try:
        for entry in fetch_sitemap(sitemap_url, timeout=timeout):
            if "/course/" in entry.url:
                course_urls.append(entry.url)
    except requests.HTTPError as exc:
        LOG.warning("Failed to fetch sitemap %s: %s", sitemap_url, exc)
    LOG.info("Discovered %d course URLs for %s", len(course_urls), language)
    return course_urls
