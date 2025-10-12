# YouTube Cookie Acquisition Playbook

Reliable, fresh cookies are the difference between a smooth YouTube ingestion pipeline and a wall of HTTP 403/429 errors. This playbook summarises the approaches we’ve had success with, the failure modes to watch for, and a checklist for iterating quickly when Google rotates their auth layers again.

---

## TL;DR Workflow

1. **Pick a real browser profile** (Chrome/Brave/Edge) that already plays any target video without extra prompts.
2. **Run `get_youtube_cookies.sh`** – it wraps `yt-dlp --cookies-from-browser`, validates output, and drops a Netscape file at `src/data_ingestion_youtube/load/youtube_cookies.txt`.
3. If the script fails, **paste a live `Cookie:` header** from DevTools → Network to the fallback prompt.
4. **Verify** with `scripts/run_download_mp3.sh --limit 1 --clients web_embedded` (or another client that requires cookies).
5. Repeat every time YouTube rotates auth (weekly-ish) or when `yt-dlp` warns that cookies are stale.

If step 2 succeeds, you’re done. The rest of this doc catalogues the edge cases and alternative flows.

---

## 1. Prerequisites & Hygiene

| Item | Notes |
|------|-------|
| **Browser profile** | Use a desktop browser signed into the desired account. Don’t use incognito — cookies go away. |
| **Login prompts cleared** | Visit `https://www.youtube.com/watch?v=BaW_jenozKc` and make sure it plays without CAPTCHA, age gate, or consent modals. |
| **Same machine** | `yt-dlp --cookies-from-browser` reads the **local** browser profile; run the script on the same host where the browser profile lives. |
| **Python & venv** | `get_youtube_cookies.sh` creates a thin virtualenv under `.yt-dlp-venv`. Ensure Python 3.9+ is installed. |
| **Permissions** | Close the browser or ensure the profile isn’t locked. On macOS, unlock Keychain popup if prompted. |

Optional, but helpful:

- `brew install chromium` (or Brave/Chrome) if you need a dedicated profile.
- `chmod +x get_youtube_cookies.sh` once.

---

## 2. Primary Flow: `get_youtube_cookies.sh`

```bash
# Configure browser + profile names (defaults: brave / Default)
export BROWSER=chrome
export PROFILE="Default"

# Run from repo root
./get_youtube_cookies.sh
```

What happens:

1. A fresh virtualenv installs the latest `yt-dlp`.
2. `yt-dlp --cookies-from-browser "${BROWSER}:${PROFILE}"` grabs cookies and writes them to `src/data_ingestion_youtube/load/youtube_cookies.txt`.
3. The script validates the Netscape format (header + 7 tab-separated fields per cookie). If validation fails, it falls back to a manual prompt (Section 3).

**Success output**
```
Cookies exported to: /path/to/src/data_ingestion_youtube/load/youtube_cookies.txt
```

**Common errors**

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Browser export failed …` | Browser profile name mismatch | Use `profiles.ini` (Firefox) or check `~/Library/Application Support/BraveSoftware/Brave-Browser` (macOS) for profile names. |
| `Keyring` prompt or `dbus` errors | Desktop services locked | On macOS, unlock Keychain. On Ubuntu servers, run with `BROWSER=chromium` pointing at a copy of the profile. |
| `0 cookies saved` | Profile doesn’t have YT auth cookies | Log into YouTube in that profile, accept consent banners, then rerun. |

---

## 3. Manual Fallback: Paste `Cookie:` Header

If `yt-dlp` fails, the script prompts you to paste a cookie header:

1. Open Chrome DevTools (Network tab).
2. Reload `https://www.youtube.com/watch?v=BaW_jenozKc`.
3. Click a request to `www.youtube.com` (e.g. `/youtubei/v1/player` or `/watch?v=…`).
4. Under **Request Headers**, copy the value of `Cookie:` (everything after the colon).
5. Paste into the script prompt.

The script converts the header into Netscape format (one line per cookie on `.youtube.com`) and writes `youtube_cookies.txt`.

Use this when:

- You only need a quick cookie file (e.g. emergency fix).
- You’re operating on a headless server but can proxy a browser session locally.

Downside: you must repeat the manual copy/paste when cookies expire.

---

## 4. Debugging & Validation

After saving cookies:

```bash
# Dry-run a single download
SAMPLE_VIDEO="https://www.youtube.com/watch?v=BaW_jenozKc"
scripts/run_download_mp3.sh --video "$SAMPLE_VIDEO" --no-upload --limit 1
```

Look for log lines:

- `Using cookie file: youtube_cookies.txt`
- Successful `yt-dlp` format selection (no 403/429 errors)

If downloads still fail:

- Delete the cache (`rm src/data_ingestion_youtube/load/youtube_cookies.txt`) and rerun the script.
- Check `yt_cookie_export.log` (created by the helper script) for browser export errors.
- Ensure `DEFAULT_CLIENT` is `web_embedded` or another client requiring cookies in `src/data_ingestion_youtube/load/download_mp3/config.py`.

---

## 5. Advanced / Automated Options

### A. Remote Debugging Snapshot

1. Launch Chrome with remote debugging:
   ```bash
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
     --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-youtube
   ```
2. Log in to YouTube, accept prompts.
3. Use `yt-dlp --cookies-from-browser chrome:/tmp/chrome-youtube` (or run the helper script with `BROWSER=chrome PROFILE=/tmp/chrome-youtube`).

Useful when you want a clean, disposable profile.

### B. Copy Profile Between Machines

If you ingest on a headless box:

1. Copy the browser profile directory from your workstation (e.g. `~/Library/Application Support/Google/Chrome/Profile 3`) to the server.
2. Set `BROWSER=chromium`, `PROFILE=/path/to/Profile 3`.
3. Run `get_youtube_cookies.sh` on the server.

### C. Periodic Regeneration

Cookies typically last 1–7 days. Automate refreshes:

```bash
# cron job (macOS/Linux)
0 */12 * * * cd /path/to/repo && BROWSER=chrome PROFILE=Default ./get_youtube_cookies.sh >/tmp/yt-cookie-refresh.log 2>&1
```

Only do this if the server can access the browser profile (e.g. via NAS mount). Otherwise keep a reminder to refresh manually.

### D. Alternative Tools

- **`yt-dlp --cookies-from-browser`** – what the script uses internally. Run manually if you want full control.
- **`cookies.txt` browser extension** – export netscape cookies straight from the browser (ensure you remove non-YouTube entries afterwards).
- **Playwright / Puppeteer** – script a headless login and export cookies. Requires handling MFA/consent prompts; more brittle, but automatable.

---

## 6. Resetting When Things Break

If YouTube introduces new consent flows or invalidates everything:

1. Delete the cached cookie file (`rm src/data_ingestion_youtube/load/youtube_cookies.txt`).
2. Clear YouTube cookies in your browser; log in again and accept every prompt.
3. Update `yt-dlp` (the helper script does this automatically, but you can force it with `python -m pip install --upgrade yt-dlp`).
4. Re-run `get_youtube_cookies.sh`.
5. Validate with the sample download command above.

If you still hit 403/429, rotate to a different browser profile or account (YouTube may be throttling the existing one).

---

## 7. Checklist for Troubleshooting Sessions

- [ ] Confirm the browser profile plays the target video without prompts.
- [ ] Use `./get_youtube_cookies.sh` with the correct `BROWSER`/`PROFILE`.
- [ ] Inspect `yt_cookie_export.log` if the script fails.
- [ ] Fall back to manual cookie header paste.
- [ ] Verify downloads with `scripts/run_download_mp3.sh`.
- [ ] Set a reminder (or cron) to refresh weekly.

Keep this doc handy when cookies rotate; copy/paste the workflow into task tickets so any engineer or operator can regenerate them without digging through Slack history.
