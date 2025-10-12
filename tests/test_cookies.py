from __future__ import annotations

import pytest

from telegram_state_notifier.notifier import cookies
from telegram_state_notifier.notifier.settings import Settings


def test_looks_like_cookie_text_detects_header():
    assert cookies.looks_like_cookie_text("Cookie: VISITOR_INFO1_LIVE=abc;")
    assert cookies.looks_like_cookie_text("# Netscape HTTP Cookie File")


def test_ensure_netscape_format_from_header():
    netscape = cookies.ensure_netscape_format("Cookie: VISITOR_INFO1_LIVE=abc; HSID=xyz;")
    assert "VISITOR_INFO1_LIVE" in netscape
    assert netscape.startswith("# Netscape HTTP Cookie File")


def test_store_cookie_handles_local_path(tmp_path, monkeypatch):
    target = tmp_path / "cookie.txt"
    settings = Settings(
        telegram_bot_token="token",
        telegram_chat_id="chat",
        youtube_cookie_local_path=target.as_posix(),
    )
    monkeypatch.setattr(cookies, "secretmanager", None, raising=False)
    cookies.store_cookie(settings, "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tTEST\tvalue\n")
    assert target.exists()
