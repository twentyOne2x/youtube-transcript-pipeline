#!/usr/bin/env python3
# Converts a raw "Cookie" request header into a Netscape cookies.txt for yt-dlp.
# Usage:
#   1) Copy the full Cookie header value from DevTools (Network -> request -> Headers).
#   2) Run this script and paste the line when prompted.
#   3) It writes youtube_cookies.txt in the current directory.

from pathlib import Path
import sys

print("Paste the FULL Cookie header value for a youtube.com request, then press Enter.\n")
try:
    cookie_header = sys.stdin.readline().strip()
except KeyboardInterrupt:
    sys.exit(1)

if not cookie_header or "=" not in cookie_header:
    print("No cookie header detected. Aborting.")
    sys.exit(1)

pairs = []
for part in cookie_header.split(";"):
    part = part.strip()
    if not part:
        continue
    if "=" not in part:
        continue
    name, value = part.split("=", 1)
    name, value = name.strip(), value.strip()
    # Skip obvious non-cookies (rare, but just in case)
    if not name:
        continue
    pairs.append((name, value))

out = Path("youtube_cookies.txt")
with out.open("w", encoding="utf-8") as f:
    f.write("# Netscape HTTP Cookie File\n")
    f.write("# This file was generated from a request Cookie header.\n")
    f.write("# It's session-scoped (expiry 0) and may need to be refreshed periodically.\n")
    # Netscape format:
    # domain \t include_subdomains \t path \t secure \t expires(Unix) \t name \t value
    domain = ".youtube.com"
    path = "/"
    secure = "TRUE"
    expires = "0"  # session cookies; refresh when they expire
    for name, value in pairs:
        f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")

print(f"Written: {out.resolve()}")
print("Move this file to: src/data_ingestion_youtube/load/youtube_cookies.txt (your script checks that path).")
