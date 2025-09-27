#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-Default}"         # brave profile name
BROWSER="${BROWSER:-brave}"           # brave|chrome|chromium|edge
FINAL_COOKIES="$PWD/src/data_ingestion_youtube/load/youtube_cookies.txt"
VENV_DIR="${VENV_DIR:-$PWD/.yt-dlp-venv}"

# Use a single watch URL (yt-dlp test video) so we don't expand "Recommended" playlists
YT_URL="${YT_URL:-https://www.youtube.com/watch?v=BaW_jenozKc}"

say()  { printf "\033[1;32m%s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
err()  { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

validate() {
  [[ -s "$1" ]] || return 1
  grep -q '^# Netscape HTTP Cookie File' "$1" || return 1
  # Basic “7 tab fields” Netscape line check (non-comment)
  awk 'NR>1 && $0 !~ /^#/ && NF { n=split($0,f,"\t"); if(n!=7) exit 1 }' "$1" || return 1
}

fallback_paste() {
  say "FALLBACK: paste the **Cookie** request header value from a real YouTube request."
  echo "Open DevTools → Network → reload YouTube, then click a request to **www.youtube.com** such as:"
  echo "  • XHR/fetch: /youtubei/v1/next, /youtubei/v1/browse, /youtubei/v1/player"
  echo "  • Document: /watch?v=…"
  echo "Avoid service/utility pages like **RotateCookiesPage** or anything to googleadservices/metrics."
  echo "In Headers → Request Headers → **Cookie**, copy only the VALUE (everything after 'Cookie:')."
  printf "Paste Cookie header: "
  IFS= read -r COOKIE || true
  [[ "${COOKIE:-}" == *"="* ]] || { err "No valid Cookie header pasted."; exit 1; }

  mkdir -p "$(dirname "$FINAL_COOKIES")"
  {
    echo "# Netscape HTTP Cookie File"
    echo "# Generated from a pasted YouTube request Cookie header."
    IFS=';' read -ra parts <<<"$COOKIE"
    for kv in "${parts[@]}"; do
      kv="$(echo "$kv" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      [[ "$kv" == *=* ]] || continue
      name="${kv%%=*}"; value="${kv#*=}"
      # Skip attributes; keep only real cookie name=value pairs
      case "$name" in
        path|Path|domain|Domain|expires|Expires|max-age|Max-Age|secure|Secure|httponly|HttpOnly|samesite|SameSite) continue;;
      esac
      # Most YT auth cookies live on .youtube.com; add them there with session expiry
      printf ".youtube.com\tTRUE\t/\tTRUE\t0\t%s\t%s\n" "$name" "$value"
    done
  } > "$FINAL_COOKIES"

  validate "$FINAL_COOKIES" && { say "Cookies written to: $FINAL_COOKIES"; exit 0; }
  err "Generated cookies failed validation."; exit 1
}

# --- tiny venv with yt-dlp only (no keyring/dbus) ---
if [[ ! -d "$VENV_DIR" ]]; then
  say "Creating venv at $VENV_DIR …"
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip -q install --upgrade pip wheel >/dev/null
if ! "$VENV_DIR/bin/python" -m pip -q install yt-dlp >/dev/null; then
  warn "Could not install yt-dlp in venv — continuing with fallback."
  fallback_paste
fi

# Try browser export (simulate + no-playlist so we don't crawl anything)
set +e
mkdir -p "$(dirname "$FINAL_COOKIES")"
"$VENV_DIR/bin/yt-dlp" \
  --simulate --no-playlist --skip-download \
  --cookies-from-browser "${BROWSER}:${PROFILE}" \
  --cookies "$FINAL_COOKIES" \
  "$YT_URL" 2>yt_cookie_export.log
rc=$?
set -e

if [[ $rc -ne 0 ]] || ! validate "$FINAL_COOKIES"; then
  warn "Browser export failed or produced an invalid cookies file. Falling back to manual Cookie header."
  fallback_paste
fi

say "Cookies exported to: $FINAL_COOKIES"
