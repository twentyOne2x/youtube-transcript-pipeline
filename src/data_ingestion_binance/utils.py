from __future__ import annotations

from functools import lru_cache
from html import unescape
import json
import logging
import re
from typing import Any, Dict

LOG = logging.getLogger(__name__)

APP_DATA_PATTERN = re.compile(
    r'id="__APP_DATA"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def sanitize_component(value: str) -> str:
    replacements = {
        '"': "",
        "'": "",
        "/": "-",
        "\\": "-",
        "|": "-",
        ":": "-",
        "?": "",
        "*": "",
        "<": "",
        ">": "",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = value.replace("—", "-").replace("–", "-").replace("…", "...")
    cleaned = "".join(ch for ch in value if 32 <= ord(ch) < 127)
    return cleaned.strip() or "untitled"


def extract_app_data(html: str) -> Dict[str, Any]:
    match = APP_DATA_PATTERN.search(html)
    if not match:
        raise ValueError("Unable to locate __APP_DATA payload")
    payload = unescape(match.group(1))
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        LOG.debug("JSON decode failed: %s", payload[:200])
        raise ValueError("Failed to parse __APP_DATA JSON") from exc


@lru_cache(maxsize=1)
def default_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Encoding": "gzip, deflate",
    }
