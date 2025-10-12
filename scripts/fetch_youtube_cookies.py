#!/usr/bin/env python3
"""
Helper script to export YouTube cookies for the media pipeline.

It attempts to use yt-dlp's --cookies-from-browser integration first, then falls
back to prompting for a manual Cookie header and converting it to Netscape
format. The resulting cookie jar can be uploaded to Secret Manager and mounted
for the mp3 downloader service.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
import shutil
import subprocess
from typing import Optional


def netscape_cookiefile_looks_ok(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    if not lines or not lines[0].startswith("# Netscape HTTP Cookie File"):
        return False
    for ln in lines[1:]:
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) != 7:
            return False
    return True


def export_with_ytdlp(output: Path, browser: str, profile: str, url: str) -> bool:
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        print("yt-dlp is not installed. Install with `pip install yt-dlp`.", file=sys.stderr)
        return False

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiesfrombrowser": (browser, profile, None, True),
        "cookiefile": output.as_posix(),
        "socket_timeout": 10,
        "retries": 1,
    }
    print(f"Attempting to export cookies from {browser}:{profile}…")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=False)
            ydl.save_cookies()
    except Exception as exc:  # pragma: no cover - depends on local browser state
        print(f"yt-dlp cookie export failed: {exc}", file=sys.stderr)
        if output.exists():
            output.unlink(missing_ok=True)
        return False

    if not netscape_cookiefile_looks_ok(output):
        print("Cookie export completed but the file is not in Netscape format.", file=sys.stderr)
        if output.exists():
            output.unlink(missing_ok=True)
        return False

    print(f"Cookies exported to {output}")
    return True


ATTRIBUTE_NAMES = {
    "path",
    "path_",
    "domain",
    "domain_",
    "expires",
    "max-age",
    "max_age",
    "secure",
    "httponly",
    "samesite",
}


def convert_cookie_header(raw: str) -> str:
    header = raw.strip()
    if not header:
        raise ValueError("Empty cookie header")
    if header.lower().startswith("cookie:"):
        header = header.split(":", 1)[1].strip()
    parts = []
    for token in header.split(";"):
        token = token.strip()
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name.lower() in ATTRIBUTE_NAMES:
            continue
        parts.append((name, value))
    if not parts:
        raise ValueError("No cookie name=value pairs detected.")

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from a pasted Cookie header.",
    ]
    for name, value in parts:
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def ensure_netscape_format(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("# Netscape HTTP Cookie File"):
        if not stripped.endswith("\n"):
            stripped += "\n"
        return stripped
    return convert_cookie_header(stripped)


def prompt_for_manual_cookie() -> str:
    instructions = textwrap.dedent(
        """
        Manual cookie export instructions:

        1. Open https://www.youtube.com/ in your browser (logged into the account that can view the videos).
        2. Open Developer Tools → Network.
        3. Reload the page and select a request to https://www.youtube.com/* (for example `/youtubei/v1/player`).
        4. In the Request headers section, copy the value of the `Cookie` header.
        5. Paste the entire header value (everything after `Cookie:`) here.
        """
    ).strip()
    print(instructions)
    try:
        header = input("\nPaste Cookie header> ").strip()
    except EOFError:
        raise ValueError("No cookie header received.")
    return convert_cookie_header(header)


def save_cookie_text(output: Path, text: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    if not netscape_cookiefile_looks_ok(output):
        raise ValueError("Generated cookie file is not in Netscape format.")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True)


def _upload_secret(secret: str, project: str, source: Path) -> None:
    if not shutil.which("gcloud"):
        raise RuntimeError("gcloud CLI not found; cannot upload secret.")
    base_cmd = ["gcloud", "secrets", "versions", "add", secret, f"--data-file={source.as_posix()}"]
    if project:
        base_cmd.extend(["--project", project])
    try:
        _run(base_cmd)
    except subprocess.CalledProcessError:
        # Attempt to create the secret and retry once
        create_cmd = ["gcloud", "secrets", "create", secret.split("/secrets/")[-1], "--replication-policy=automatic"]
        if project:
            create_cmd.extend(["--project", project])
        _run(create_cmd)
        _run(base_cmd)


def _redeploy_service(
    *,
    project: str,
    region: str,
    service: str,
    secret_name: str,
    mount_path: str,
) -> None:
    if not shutil.which("gcloud"):
        raise RuntimeError("gcloud CLI not found; cannot redeploy service.")
    cmd = [
        "gcloud",
        "run",
        "services",
        "update",
        service,
        "--set-secrets",
        f"{mount_path}={secret_name}:latest",
        "--update-env-vars",
        f"YOUTUBE_COOKIE_FILE={mount_path},DEFAULT_CLIENT=android,USE_BROWSER_COOKIES=false",
        "--region",
        region,
    ]
    if project:
        cmd.extend(["--project", project])
    _run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch YouTube cookies for the media pipeline.")
    parser.add_argument("--browser", default=os.environ.get("BROWSER", "brave"), help="Browser name (brave|chrome|chromium|edge).")
    parser.add_argument("--profile", default=os.environ.get("PROFILE", "Default"), help="Browser profile name or path.")
    parser.add_argument("--output", default="src/data_ingestion_youtube/load/youtube_cookies.txt", help="Destination Netscape cookie file.")
    parser.add_argument("--url", default="https://www.youtube.com/watch?v=BaW_jenozKc", help="YouTube URL to probe while exporting cookies.")
    parser.add_argument("--force-manual", action="store_true", help="Skip yt-dlp export and prompt for manual Cookie header.")
    parser.add_argument("--cookie-header", help="Raw `Cookie:` header value to convert (skips prompts).")
    parser.add_argument("--cookie-header-file", help="Path to a file containing a raw `Cookie:` header value.")
    parser.add_argument("--secret", help="Secret Manager resource name (projects/.../secrets/NAME) to receive the cookie jar.")
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""), help="GCP project for secret upload and redeploy.")
    parser.add_argument("--service", help="Cloud Run service to redeploy (e.g. youtube-mp3-downloader).")
    parser.add_argument("--region", default=os.environ.get("CLOUD_RUN_REGION", "us-central1"), help="Cloud Run region for redeploy.")
    parser.add_argument("--mount-path", default="/workspace/youtube_cookies.txt", help="Path inside the container where the cookie secret is mounted.")
    args = parser.parse_args()

    output_path = Path(args.output).expanduser().resolve()

    header_text: Optional[str] = None
    if args.cookie_header:
        header_text = args.cookie_header
    elif args.cookie_header_file:
        header_text = Path(args.cookie_header_file).read_text(encoding="utf-8")

    if header_text:
        netscape = ensure_netscape_format(header_text)
        save_cookie_text(output_path, netscape)
        print(f"Cookies saved to {output_path}")
    elif not args.force_manual and export_with_ytdlp(output_path, args.browser, args.profile, args.url):
        print("Cookie export completed successfully.")
    else:
        print("\nAutomatic export failed; falling back to manual instructions.", file=sys.stderr)
        try:
            netscape = prompt_for_manual_cookie()
            save_cookie_text(output_path, netscape)
            print(f"Cookies saved to {output_path}")
        except Exception as exc:
            print(f"❌ Manual cookie conversion failed: {exc}", file=sys.stderr)
            return 1

    if args.secret:
        try:
            _upload_secret(args.secret, args.project, output_path)
            print(f"Uploaded cookie jar to {args.secret}")
        except Exception as exc:
            print(f"⚠️ Failed to upload secret: {exc}", file=sys.stderr)
            return 1

        if args.service:
            secret_name = args.secret.split("/secrets/")[-1]
            try:
                _redeploy_service(
                    project=args.project,
                    region=args.region,
                    service=args.service,
                    secret_name=secret_name,
                    mount_path=args.mount_path,
                )
                print(f"Redeployed Cloud Run service {args.service}")
            except Exception as exc:
                print(f"⚠️ Failed to redeploy service: {exc}", file=sys.stderr)
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
