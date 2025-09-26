# choose where to put the cookies file
COOKIES_PATH="$PWD/src/data_ingestion_youtube/load/logs/youtube_cookies.txt"

# export from Brave's "Default" profile and WRITE to that file
yt-dlp --cookies-from-browser brave:Default \
       --cookies "$COOKIES_PATH" \
       --no-download https://youtube.com/

# verify
ls -l "$COOKIES_PATH"
head -n 5 "$COOKIES_PATH"
grep -E '^\.(google|youtube)\.com' "$COOKIES_PATH" | cut -f1,6 | sort -u
