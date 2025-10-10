from __future__ import annotations

import argparse
import logging
from typing import List, Optional, Set

import requests

from .config import BinanceSettings
from .course import CourseVideo, parse_course
from .downloader import download_video
from .sitemap import collect_course_urls
from .utils import sanitize_component

LOG = logging.getLogger("binance_academy")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Binance Academy videos.")
    parser.add_argument(
        "--languages",
        nargs="+",
        help="Language codes to process (default: environment BINANCE_LANGUAGE_CODES or 'en').",
    )
    parser.add_argument(
        "--include-course",
        action="append",
        help="Specific course URL(s) to process (skips sitemap discovery).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of courses per language.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    settings = BinanceSettings()
    if args.languages:
        settings = BinanceSettings(
            languages=args.languages,
            output_dir=settings.output_dir,
            request_timeout=settings.request_timeout,
            gcs_bucket=settings.gcs_bucket,
            gcs_prefix=settings.gcs_prefix,
            keep_local_files=settings.keep_local_files,
            max_consecutive_errors=settings.max_consecutive_errors,
            sitemap_base=settings.sitemap_base,
            wistia_timeout=settings.wistia_timeout,
        )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "gzip, deflate",
        }
    )

    visited_video_ids: Set[str] = set()
    error_count = 0

    for language in settings.languages:
        if args.include_course:
            course_urls = list(dict.fromkeys(args.include_course))
        else:
            course_urls = collect_course_urls(settings.sitemap_base, language, timeout=settings.request_timeout)
        if args.limit:
            course_urls = course_urls[: args.limit]

        LOG.info("Processing %d courses for %s", len(course_urls), language)

        for course_url in course_urls:
            try:
                videos: List[CourseVideo] = parse_course(
                    course_url,
                    language=language,
                    timeout=settings.request_timeout,
                    session=session,
                )
            except Exception as exc:
                LOG.error("Failed to parse course %s: %s", course_url, exc)
                error_count += 1
                if error_count >= settings.max_consecutive_errors:
                    raise SystemExit("Too many consecutive errors, aborting.")
                continue

            if not videos:
                LOG.info("No videos found for %s", course_url)
                continue

            for video in videos:
                video_id = sanitize_component(video.video_link)
                if video_id in visited_video_ids:
                    LOG.debug("Skipping duplicate video %s", video.video_link)
                    continue
                visited_video_ids.add(video_id)
                try:
                    download_video(video, settings, session=session)
                except Exception as exc:
                    LOG.error("Failed to download %s: %s", video.video_link, exc)
                    error_count += 1
                    if error_count >= settings.max_consecutive_errors:
                        raise SystemExit("Too many consecutive errors, aborting.")
                    continue
                else:
                    error_count = 0


if __name__ == "__main__":
    main()
