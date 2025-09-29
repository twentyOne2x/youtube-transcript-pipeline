from typing import List

from .config import Settings


_ALLOWED: List[str] = ["web_embedded", "android", "ios", "tv"]


def _normalize_client(raw: str | None) -> str:
    """
    Normalize a requested client name to one of the allowed values.
    Treat 'web' and unknowns as 'web_embedded'.
    """
    c = (raw or "").strip().lower()
    if c in _ALLOWED:
        return c
    if c == "web":
        return "web_embedded"
    return "web_embedded"


def set_client(opts: dict, client: str):
    """
    Set yt-dlp extractor_args for the chosen client and configure cookies.
    Never use 'web_safari' or 'web_creator'.
    """
    y = opts.setdefault("extractor_args", {}).setdefault("youtube", {})

    client = _normalize_client(client)
    y["player_client"] = [client]

    have_cookiefile = bool(opts.get("_fallback_cookie_file"))
    have_browser_cookies = "_browser" in opts and "_profile" in opts
    using_cookies = have_cookiefile or have_browser_cookies

    # Apply cookies only for web-like clients (web_embedded counts as web)
    if using_cookies and y["player_client"][0].startswith("web"):
        if have_cookiefile:
            opts["cookiefile"] = opts["_fallback_cookie_file"]
            opts.pop("cookiesfrombrowser", None)
        else:
            opts["cookiesfrombrowser"] = (
                opts.get("_browser", "brave"),
                opts.get("_profile", "Default"),
                None,
                True,
            )
            opts.pop("cookiefile", None)
    else:
        opts.pop("cookiefile", None)
        opts.pop("cookiesfrombrowser", None)


class ClientRotator:
    """
    Rotation strategy:
      - Base order always includes web_embedded first.
      - Honor Settings.default_client if it is one of: web_embedded, android, ios, tv.
      - Exclude web_safari and web_creator entirely.
    """

    def __init__(self, using_cookies: bool, settings: Settings):
        # Start from a fixed safe order
        order: List[str] = ["web_embedded", "android", "ios", "tv"]

        # Honor DEFAULT_CLIENT even if cookies are absent
        dc = _normalize_client(getattr(settings, "default_client", None))
        if dc in order:
            # Move dc to the front while preserving relative order of others
            order = [dc] + [c for c in order if c != dc]

        self.order = order
        self.idx = 0

    def current(self) -> str:
        return self.order[self.idx]

    def next(self) -> str:
        self.idx = (self.idx + 1) % len(self.order)
        return self.current()

    def force_web_with_cookies(self):
        # Pin to web_embedded
        self.idx = self.order.index("web_embedded") if "web_embedded" in self.order else 0
