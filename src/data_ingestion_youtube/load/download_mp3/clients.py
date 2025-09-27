from typing import List

from .config import Settings


def set_client(opts: dict, client: str):
    y = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
    if client == "web":
        y["player_client"] = ["web_safari"]
    elif client in ("web_safari", "web_creator", "web_embedded", "android", "ios", "tv"):
        y["player_client"] = [client]
    else:
        y["player_client"] = ["web_safari"]

    have_cookiefile = bool(opts.get("_fallback_cookie_file"))
    have_browser_cookies = "_browser" in opts and "_profile" in opts
    using_cookies = have_cookiefile or have_browser_cookies

    if using_cookies and y["player_client"][0].startswith("web"):
        if have_cookiefile:
            opts["cookiefile"] = opts["_fallback_cookie_file"]
            opts.pop("cookiesfrombrowser", None)
        else:
            opts["cookiesfrombrowser"] = (
                opts.get("_browser", "brave"),
                opts.get("_profile", "Default"),
                None,
                True
            )
            opts.pop("cookiefile", None)
    else:
        opts.pop("cookiefile", None)
        opts.pop("cookiesfrombrowser", None)

class ClientRotator:
    def __init__(self, using_cookies: bool, settings: Settings):
        order: List[str] = (["web_safari", "web_creator", "web_embedded", "android", "ios", "tv"]
                            if using_cookies else ["android", "ios", "tv", "web_safari"])
        # honor DEFAULT_CLIENT if present
        dc = (settings.default_client or "").strip()
        if dc and dc in order:
            order.remove(dc)
            order.insert(0, dc)
        self.order = order
        self.idx = 0

    def current(self) -> str:
        return self.order[self.idx]

    def next(self) -> str:
        self.idx = (self.idx + 1) % len(self.order)
        return self.current()

    def force_web_with_cookies(self):
        if "web_safari" in self.order:
            self.idx = self.order.index("web_safari")
        else:
            self.idx = 0