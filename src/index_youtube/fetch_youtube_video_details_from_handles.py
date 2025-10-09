import os
import traceback
from typing import List, Optional, Dict, Tuple
import json

import aiohttp
import pandas as pd
from googleapiclient.discovery import build
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from datetime import datetime
import logging
import csv
import asyncio

from src.index_youtube.constants import YOUTUBE_VIDEOS_CSV_FILE_PATH  # keep only what we actually use
from src.index_youtube.utils import (
    root_directory,
    authenticate_service_account,
    get_videos_from_playlist,
    get_channel_id,
    reset_channel_mappings,
    get_channel_names,
)

# Load environment variables from the .env file
load_dotenv()

# =========================
# Helpers / Types
# =========================

HEADERS = ['video_id', 'title', 'channel_name', 'channel_id', 'published_date', 'url']


def _parse_iso_utc(ts: str) -> datetime:
    # YouTube returns "YYYY-MM-DDTHH:MM:SSZ"
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


# =========================
# Core fetchers
# =========================

async def get_multiple_video_details(
    channel_name: str,
    youtube,
    video_ids: List[str],
    existing_video_ids: set,
    passthrough_ids: set,
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
) -> List[dict]:
    """
    Fetch details for a batch of video IDs and return rows to append.
    - PASSTHROUGH uses channel_id matching (robust).
    - Filtering only applies if keyword lists are provided.
    """
    MAX_IDS_PER_REQUEST = 50  # YouTube API's limitation
    logging.info(f"[{channel_name}] Fetching details for {len(video_ids)} videos...")

    def include_by_keywords(title: str) -> bool:
        t = title.lower()
        if keywords and len(keywords) > 0:
            if not any(k.lower() in t for k in keywords):
                return False
        if keywords_to_exclude and len(keywords_to_exclude) > 0:
            if any(k.lower() in t for k in keywords_to_exclude):
                return False
        return True

    all_video_details: List[dict] = []

    for i in range(0, len(video_ids), MAX_IDS_PER_REQUEST):
        batch = video_ids[i:i + MAX_IDS_PER_REQUEST]
        try:
            resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
            items = resp.get('items', [])
        except Exception as e:
            logging.error(f"Error fetching video details for batch: {e}")
            traceback.print_exc()
            continue

        for item in items:
            snippet = item.get('snippet', {})
            video_id = item.get('id')
            if not video_id:
                continue

            # De-dupe only by video_id (robust)
            if video_id in existing_video_ids:
                continue

            ch_id = snippet.get('channelId', '')
            ch_title = snippet.get('channelTitle', '')
            title = snippet.get('title', '')
            published_at = snippet.get('publishedAt')

            if not published_at:
                # Very rare, but skip if missing essential metadata
                continue

            # PASSTHROUGH: accept regardless of title if channel_id is whitelisted
            # Otherwise apply keyword include/exclude (only if user provided them)
            if (ch_id in passthrough_ids) or include_by_keywords(title):
                dt = _parse_iso_utc(published_at)
                all_video_details.append({
                    'video_id': video_id,
                    'title': title,
                    'channel_name': ch_title,
                    'channel_id': ch_id,
                    'published_date': dt.strftime("%Y-%m-%d"),
                    'url': f'https://www.youtube.com/watch?v={video_id}',
                })

    return all_video_details


async def get_video_info(
    session,
    credentials: Optional[ServiceAccountCredentials],
    api_key: str,
    channel_id: str,
    channel_name: str,
    passthrough_ids: set,
    existing_video_ids: set,
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
    max_results: int = 50,
) -> List[dict]:
    """
    Retrieves video info (URL, ID, title, published date, channel_id) for a channel by walking its Uploads playlist.
    """
    youtube = build('youtube', 'v3', credentials=credentials, developerKey=api_key)

    # Get the "Uploads" playlist ID
    try:
        channel_response = youtube.channels().list(
            part="contentDetails",
            id=channel_id,
            fields="items/contentDetails/relatedPlaylists/uploads"
        ).execute()
        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        logging.error(f"[{channel_name}] Could not get uploads playlist: {e}")
        return []

    video_info: List[dict] = []
    next_page_token = None

    while True:
        try:
            playlist_response = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=max_results,
                pageToken=next_page_token,
            ).execute()
        except Exception as e:
            logging.error(f"[{channel_name}] Error listing playlistItems: {e}")
            break

        items = playlist_response.get('items', [])
        video_ids = [it["snippet"]["resourceId"]["videoId"] for it in items if "snippet" in it and "resourceId" in it.get("snippet", {})]

        # Enrich with snippets + filter/accept
        batch_rows = await get_multiple_video_details(
            channel_name=channel_name,
            youtube=youtube,
            video_ids=video_ids,
            existing_video_ids=existing_video_ids,
            passthrough_ids=passthrough_ids,
            keywords=keywords,
            keywords_to_exclude=keywords_to_exclude,
        )

        video_info.extend(batch_rows)

        next_page_token = playlist_response.get('nextPageToken')
        if not next_page_token:
            break

    return video_info


def save_video_info_to_csv(video_info_list: List[dict], csv_file_path: str, existing_video_ids: set):
    """
    Append new videos to CSV, keeping only non-duplicates (by video_id).
    """
    # Remove duplicates by video_id (in-memory pre-filter)
    to_add = [row for row in video_info_list if row.get('video_id') not in existing_video_ids]
    if not to_add:
        return

    # Read existing (if any)
    try:
        existing_df = pd.read_csv(csv_file_path, encoding='utf-8')
    except FileNotFoundError:
        existing_df = pd.DataFrame(columns=HEADERS)

    new_df = pd.DataFrame(to_add, columns=HEADERS)
    combined_df = pd.concat([existing_df, new_df], ignore_index=True)

    # Final de-dupe by video_id
    if 'video_id' in combined_df.columns:
        combined_df.drop_duplicates(subset=['video_id'], inplace=True)

    combined_df.to_csv(csv_file_path, index=False, quoting=csv.QUOTE_MINIMAL)


async def fetch_and_save_channel_videos_async(
    session,
    channel_id: str,
    channel_name: str,
    credentials: Optional[ServiceAccountCredentials],
    api_key: str,
    csv_file_path: str,
    existing_video_ids: set,
    passthrough_ids: set,
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
):
    rows = await get_video_info(
        session=session,
        credentials=credentials,
        api_key=api_key,
        channel_id=channel_id,
        channel_name=channel_name,
        passthrough_ids=passthrough_ids,
        existing_video_ids=existing_video_ids,
        keywords=keywords,
        keywords_to_exclude=keywords_to_exclude,
    )
    save_video_info_to_csv(rows, csv_file_path, existing_video_ids)
    logging.info(f"[{channel_name}] Saved {len(rows)} new videos to CSV.")


async def fetch_youtube_videos(
    api_key: str,
    yt_channels: Optional[List[str]],
    yt_playlists: Optional[List[str]],
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
    PASSTHROUGH: List[str],
    fetch_videos: bool,
) -> List[dict]:
    """
    Main orchestrator: resolves channels, fetches videos, optionally handles playlists.
    Returns the existing CSV rows (list of dicts) after ensuring CSV exists/migrated.
    """
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials: Optional[ServiceAccountCredentials] = None

    if service_account_file:
        credentials = authenticate_service_account(service_account_file)
        logging.info("Service account found. Private items accessible if permitted.")
    else:
        logging.info("No service account file found. Proceeding with public access.")

    csv_file_exists, csv_file_path = setup_csv()

    existing_data, existing_video_ids, existing_channel_names = load_existing_data(csv_file_exists, csv_file_path)

    # Resolve channel handles -> names and ids
    channel_handle_to_name: Dict[str, str] = get_channel_names(api_key, yt_channels)

    channels_in_csv, channels_not_in_csv = separate_channels_based_on_csv(
        channel_handle_to_name, existing_channel_names, yt_channels
    )

    # Build mapping of handle->id (persisted)
    channel_mapping_filepath = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"
    if os.path.exists(channel_mapping_filepath):
        with open(channel_mapping_filepath, 'r', encoding='utf-8') as f:
            handle_to_id_map: Dict[str, str] = json.load(f)
    else:
        handle_to_id_map = {}
        with open(channel_mapping_filepath, 'w', encoding='utf-8') as f:
            json.dump(handle_to_id_map, f, ensure_ascii=False, indent=4)

    # Build PASSTHROUGH_IDS set using resolved IDs
    passthrough_ids: set = set()

    async with aiohttp.ClientSession() as session:
        # Process all unique channels (in/out of CSV)
        all_handles = set(channels_in_csv + channels_not_in_csv)
        for handle in all_handles:
            ch_name = channel_handle_to_name.get(handle)
            if not ch_name:
                continue
            ch_id = await get_channel_id(session, api_key, handle, handle_to_id_map)
            if ch_id:
                # If this channel is in PASSTHROUGH by NAME, include its ID in passthrough_ids
                if ch_name in PASSTHROUGH:
                    passthrough_ids.add(ch_id)

        # Now actually fetch videos (optionally)
        if fetch_videos:
            for handle in all_handles:
                ch_name = channel_handle_to_name.get(handle)
                if not ch_name:
                    continue
                ch_id = await get_channel_id(session, api_key, handle, handle_to_id_map)
                if not ch_id:
                    logging.error(f"Could not resolve channel ID for {handle} ({ch_name})")
                    continue

                await fetch_and_save_channel_videos_async(
                    session=session,
                    channel_id=ch_id,
                    channel_name=ch_name,
                    credentials=credentials,
                    api_key=api_key,
                    csv_file_path=csv_file_path,
                    existing_video_ids=existing_video_ids,
                    passthrough_ids=passthrough_ids,
                    keywords=keywords,
                    keywords_to_exclude=keywords_to_exclude,
                )

        # Persist any updated mapping to disk
        with open(channel_mapping_filepath, 'w', encoding='utf-8') as f:
            json.dump(handle_to_id_map, f, ensure_ascii=False, indent=4)

    # Playlists (optional)
    await fetch_playlist_videos(api_key, credentials, csv_file_path, existing_video_ids, yt_playlists)

    return existing_data


async def fetch_playlist_videos(
    api_key: str,
    credentials: Optional[ServiceAccountCredentials],
    csv_file_path: str,
    existing_video_ids: set,
    yt_playlists: Optional[List[str]],
):
    if not yt_playlists:
        return
    # Note: get_videos_from_playlist should return rows with at least url/title/channel_name/published_date
    for playlist_id in yt_playlists:
        try:
            video_info_list = get_videos_from_playlist(credentials, api_key, playlist_id)
        except Exception as e:
            logging.error(f"Error fetching playlist {playlist_id}: {e}")
            continue

        # Ensure each item has video_id and channel_id fields
        for v in video_info_list:
            # video_id from URL if missing
            if 'url' in v and 'video_id' not in v:
                url = v['url']
                if 'v=' in url:
                    v['video_id'] = url.split('v=')[1].split('&')[0]
                elif 'youtu.be/' in url:
                    v['video_id'] = url.split('youtu.be/')[1].split('?')[0]

            # channel_id might not be available from playlist util; keep empty if unknown
            if 'channel_id' not in v:
                v['channel_id'] = ''

            # enforce headers
            for k in HEADERS:
                if k not in v:
                    v[k] = v.get(k, '')

        save_video_info_to_csv(video_info_list, csv_file_path, existing_video_ids)


async def fetch_channel_videos(
    api_key: str,
    channel_handle_to_name: Dict[str, str],
    channels_in_csv: List[str],
    channels_not_in_csv: List[str],
    credentials: Optional[ServiceAccountCredentials],
    csv_file_path: str,
    existing_video_ids: set,
    yt_channels: List[str],
    PASSTHROUGH: List[str],
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
):
    """
    (Kept for parity with your earlier structure, no longer used directly — logic in fetch_youtube_videos)
    """
    pass  # intentionally not used; logic consolidated in fetch_youtube_videos


def separate_channels_based_on_csv(
    channel_handle_to_name: Dict[str, str],
    existing_channel_names: set,
    yt_channels: List[str],
) -> Tuple[List[str], List[str]]:
    channels_not_in_csv = []
    channels_in_csv = []
    for handle in yt_channels:
        ch_name = channel_handle_to_name.get(handle)
        if not ch_name:
            continue
        if ch_name in existing_channel_names:
            channels_in_csv.append(handle)
        else:
            channels_not_in_csv.append(handle)
    return channels_in_csv, channels_not_in_csv


def load_existing_data(csv_file_exists: bool, csv_file_path: str):
    if csv_file_exists:
        existing_data_df = pd.read_csv(csv_file_path, encoding='utf-8')

        # Add missing columns if needed (migration light guard)
        for col in HEADERS:
            if col not in existing_data_df.columns:
                existing_data_df[col] = ''  # create blank column

        # Create a set for existing video IDs for faster lookup
        if 'video_id' in existing_data_df.columns:
            existing_video_ids = set(existing_data_df['video_id'].astype(str).str.strip())
        else:
            existing_video_ids = set()

        existing_channel_names = set(existing_data_df['channel_name'].astype(str).str.strip())
        existing_data = existing_data_df.to_dict('records')
    else:
        existing_data = []
        existing_video_ids = set()
        existing_channel_names = set()

    return existing_data, existing_video_ids, existing_channel_names


def setup_csv():
    """
    Ensure CSV exists with HEADERS. If existing, migrate to include new HEADERS (notably channel_id).
    """
    csv_file_exists = os.path.exists(YOUTUBE_VIDEOS_CSV_FILE_PATH)
    if not csv_file_exists:
        pd.DataFrame(columns=HEADERS).to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False, encoding='utf-8')
        return False, YOUTUBE_VIDEOS_CSV_FILE_PATH

    # Existing file: migrate columns if needed
    df_head = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, encoding='utf-8', nrows=0)
    existing_headers = df_head.columns.tolist()

    if existing_headers != HEADERS:
        logging.info("Migrating CSV to the new header schema...")
        df = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, encoding='utf-8')

        # Ensure video_id exists (extract from url if missing)
        if 'video_id' not in df.columns:
            def extract_video_id(url):
                if pd.isna(url):
                    return ''
                if 'v=' in url:
                    return url.split('v=')[1].split('&')[0]
                elif 'youtu.be/' in url:
                    return url.split('youtu.be/')[1].split('?')[0]
                return ''
            df['video_id'] = df.get('url', '').apply(extract_video_id) if 'url' in df.columns else ''

        # Ensure channel_id exists
        if 'channel_id' not in df.columns:
            df['channel_id'] = ''

        # Ensure other expected columns exist
        for col in HEADERS:
            if col not in df.columns:
                df[col] = ''

        # Reorder
        df = df[HEADERS]
        df.to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False, encoding='utf-8')
        logging.info(f"Migration complete. Columns now: {HEADERS}")

    return True, YOUTUBE_VIDEOS_CSV_FILE_PATH


def drop_duplicates_and_write_to_csv(filtered_away_csv_file_path, filtered_away_video_info_list, headers):
    existing_rows = []
    if os.path.exists(filtered_away_csv_file_path):
        with open(filtered_away_csv_file_path, 'r', newline='', encoding='utf-8') as csv_file:
            reader = csv.DictReader(csv_file)
            existing_rows = [row for row in reader]

    all_rows = existing_rows + filtered_away_video_info_list

    seen_ids = set()
    dedup_rows = []
    for row in all_rows:
        identifier = row.get('video_id') or row.get('url')
        if identifier and identifier not in seen_ids:
            seen_ids.add(identifier)
            dedup_rows.append(row)

    with open(filtered_away_csv_file_path, 'w', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(dedup_rows)


def filter_and_remove_videos(
    input_csv_path: str,
    keywords: Optional[List[str]],
    keywords_to_exclude: Optional[List[str]],
    PASSTHROUGH: List[str],
    channel_specific_filters: Optional[Dict[str, List[str]]] = None
):
    """
    Post-filter CSV (if you want to keep only some titles).
    This respects PASSTHROUGH by NAME (legacy), but since fetch time uses IDs,
    this is mainly for optional further pruning.
    """
    channel_specific_filters = channel_specific_filters or {}
    df = pd.read_csv(input_csv_path)

    passthrough_lower = {c.lower() for c in PASSTHROUGH}
    passthrough_df = df[df['channel_name'].astype(str).str.lower().isin(passthrough_lower)]
    non_passthrough_df = df[~df['channel_name'].astype(str).str.lower().isin(passthrough_lower)]

    # Global keyword filtering for non-passthrough
    if keywords:
        non_passthrough_df = non_passthrough_df[
            non_passthrough_df['title'].astype(str).str.lower().str.contains('|'.join([k.lower() for k in keywords]), na=False)
        ]

    # Exclude specific words even from passthrough if provided
    if keywords_to_exclude:
        patt = '|'.join([k.lower() for k in keywords_to_exclude])
        passthrough_df = passthrough_df[
            ~passthrough_df['title'].astype(str).str.lower().str.contains(patt, na=False)
        ]

    global_filtered_df = pd.concat([non_passthrough_df, passthrough_df], ignore_index=True)

    # Channel-specific filters
    final_filtered_df = pd.DataFrame()
    if channel_specific_filters:
        csf_lower = {k.lower(): [kw.lower() for kw in v] for k, v in channel_specific_filters.items()}
        channels_with_filters = set(global_filtered_df['channel_name'].astype(str).str.lower()) & set(csf_lower.keys())
        for ch in channels_with_filters:
            ch_df = global_filtered_df[global_filtered_df['channel_name'].astype(str).str.lower() == ch]
            patt = '|'.join(csf_lower[ch])
            ch_df = ch_df[ch_df['title'].astype(str).str.lower().str.contains(patt, na=False)]
            final_filtered_df = pd.concat([final_filtered_df, ch_df], ignore_index=True)

        final_filtered_df = pd.concat([
            final_filtered_df,
            global_filtered_df[~global_filtered_df['channel_name'].astype(str).str.lower().isin(channels_with_filters)]
        ], ignore_index=True)
    else:
        final_filtered_df = global_filtered_df

    # Identify removed videos (by video_id primarily)
    removed_df = df[~df['video_id'].isin(final_filtered_df['video_id'])] if 'video_id' in df.columns else df[~df['title'].isin(final_filtered_df['title'])]

    for _, r in removed_df.iterrows():
        vid = r.get('video_id', 'NO_ID')
        logging.info(f"Removed video: {vid} - {r['title']} - Channel: [{r['channel_name']}]")

    filtered_away_csv = f"{root_directory()}/data/links/youtube/filtered_away_youtube_videos.csv"
    write_header = not os.path.exists(filtered_away_csv)
    removed_df.to_csv(filtered_away_csv, mode='a', header=write_header, index=False)

    final_filtered_df.to_csv(input_csv_path, index=False)


async def fetch_all_videos(
    api_key: str,
    yt_channels: Optional[List[str]] = None,
    yt_playlists: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    keywords_to_exclude: Optional[List[str]] = None,
    PASSTHROUGH: Optional[List[str]] = None,
    fetch_videos: bool = True
):
    await fetch_youtube_videos(
        api_key, yt_channels, yt_playlists, keywords, keywords_to_exclude, PASSTHROUGH or [], fetch_videos
    )


# IO helpers
def get_youtube_channels_from_file(file_path: str) -> List[str]:
    with open(file_path, 'r', encoding='utf-8') as f:
        channels = f.read().split(',')
    return [c.strip() for c in channels if c.strip()]


def print_summary_table(csv_path: str, added_df: Optional[pd.DataFrame] = None):
    """
    Prints a summary table:
      Channel Title | Channel ID | Count of videos (in CSV) | Last video date | New videos this run + top 3 (date first)
    Ordered by most recent Last Date (descending).
    `added_df` should contain only the rows added during this run (same schema as CSV).
    """
    try:
        df = pd.read_csv(csv_path, encoding='utf-8')
    except FileNotFoundError:
        logging.info("No CSV yet to summarize.")
        return

    if df.empty:
        logging.info("CSV is empty — no summary to print.")
        return

    expected_cols = ['video_id', 'title', 'channel_name', 'channel_id', 'published_date', 'url']
    for col in expected_cols:
        if col not in df.columns:
            df[col] = ''

    # Make sure dates are proper datetimes for accurate max/sort
    df['__date'] = pd.to_datetime(df['published_date'], errors='coerce')

    # Base aggregates from ALL rows currently in CSV
    agg = df.groupby(['channel_name', 'channel_id'], dropna=False).agg(
        total_videos=('video_id', 'nunique'),
        last_video_dt=('__date', 'max')
    ).reset_index()

    # Render-friendly string for display
    agg['last_video_date'] = agg['last_video_dt'].dt.strftime('%Y-%m-%d').fillna('')

    # Build "new this run" aggregates from added_df (may be None/empty)
    added_summary = {}
    if added_df is not None and not added_df.empty:
        for col in expected_cols:
            if col not in added_df.columns:
                added_df[col] = ''
        added_df_sorted = added_df.copy()
        added_df_sorted['__date'] = pd.to_datetime(added_df_sorted['published_date'], errors='coerce')
        added_df_sorted.sort_values(['channel_name', '__date'], ascending=[True, False], inplace=True)

        for (ch_name, ch_id), grp in added_df_sorted.groupby(['channel_name', 'channel_id'], dropna=False):
            count_new = grp['video_id'].nunique()
            top3 = grp[['published_date', 'title']].head(3).values.tolist()
            added_summary[(ch_name, ch_id)] = {
                'count_new': int(count_new),
                'top3': top3
            }

    # Pretty print — ORDERED by most recent last date (descending)
    agg_sorted = agg.sort_values(['last_video_dt', 'channel_name'], ascending=[False, True]).fillna('')

    logging.info("\n" + "=" * 60)
    logging.info("SUMMARY TABLE")
    logging.info("=" * 60)
    header_fmt = "{:<40} | {:<28} | {:>6} | {:<10} | {:>4}"
    logging.info(header_fmt.format("Channel Title", "Channel ID", "Count", "Last Date", "New"))
    logging.info("-" * 60)

    row_fmt = "{:<40} | {:<28} | {:>6} | {:<10} | {:>4}"
    for _, row in agg_sorted.iterrows():
        key = (row['channel_name'], row['channel_id'])
        new_info = added_summary.get(key, {'count_new': 0, 'top3': []})
        logging.info(row_fmt.format(
            str(row['channel_name'])[:40],
            str(row['channel_id'])[:28],
            int(row['total_videos']),
            str(row['last_video_date']),
            new_info['count_new']
        ))
        for date_str, title in new_info['top3']:
            logging.info(f"  - {date_str} — {title}")

    logging.info("=" * 60 + "\n")


import re  # top-level (you already import re elsewhere)

def _parse_id_list_env(var: str) -> list[str]:
    raw = os.getenv(var, "") or ""
    return [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]

async def fetch_specific_videos(
    api_key: str,
    credentials: Optional[ServiceAccountCredentials],
    csv_file_path: str,
    existing_video_ids: set,
    video_ids: List[str],
    keywords: Optional[List[str]] = None,
    keywords_to_exclude: Optional[List[str]] = None,
):
    """
    Fetch exact videos by ID and append to CSV (no channel walking).
    Applies optional include/exclude keyword filters on title.
    """
    if not video_ids:
        return

    youtube = build('youtube', 'v3', credentials=credentials, developerKey=api_key)

    def include_by_keywords(title: str) -> bool:
        t = (title or "").lower()
        if keywords:
            if not any(k.lower() in t for k in keywords):
                return False
        if keywords_to_exclude:
            if any(k.lower() in t for k in keywords_to_exclude):
                return False
        return True

    MAX_IDS_PER_REQUEST = 50
    rows: List[dict] = []

    for i in range(0, len(video_ids), MAX_IDS_PER_REQUEST):
        batch = video_ids[i:i + MAX_IDS_PER_REQUEST]
        try:
            resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
            items = resp.get('items', [])
        except Exception as e:
            logging.error(f"[specific_videos] Error fetching batch: {e}")
            continue

        for item in items:
            vid = item.get('id')
            if not vid or vid in existing_video_ids:
                continue

            sn = item.get('snippet', {}) or {}
            title = sn.get('title', '')
            if not include_by_keywords(title):
                continue

            ch_id = sn.get('channelId', '') or ''
            ch_name = sn.get('channelTitle', '') or ''
            published_at = sn.get('publishedAt')
            if not published_at:
                continue

            dt = _parse_iso_utc(published_at)
            rows.append({
                'video_id': vid,
                'title': title,
                'channel_name': ch_name,
                'channel_id': ch_id,
                'published_date': dt.strftime("%Y-%m-%d"),
                'url': f'https://www.youtube.com/watch?v={vid}',
            })

    save_video_info_to_csv(rows, csv_file_path, existing_video_ids)
    existing_video_ids.update({r['video_id'] for r in rows})
    logging.info(f"[specific_videos] Saved {len(rows)} specific videos to CSV.")


def run():
    fetch_videos = True

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Get all channel handles that will be processed
    yt_channels_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
    yt_channels = get_youtube_channels_from_file(yt_channels_file)

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        raise ValueError("No API key provided. Set YOUTUBE_API_KEY in your environment or .env file.")

    # Check for known bad mappings and reset if needed
    mapping_file = os.path.join(root_directory(), "data/links/channel_handle_to_name_mapping.json")
    if os.path.exists(mapping_file):
        with open(mapping_file, 'r', encoding='utf-8') as f:
            existing_mappings = json.load(f)

        bad_mappings = {
            '@Delphi_Digital': ['Jambo Technology', 'DealFlow Podcast'],
            '@SolanaFndn': ['Web3RM', 'PAWS LABS OFFICIAL'],
            '@notthreadguy': ['Thread Guy Shorts', 'Thread Guy Media', 'Thread Guy Clips']
        }

        needs_reset = False
        for handle, wrong_names in bad_mappings.items():
            if handle in existing_mappings and existing_mappings[handle] in wrong_names:
                logging.warning(f"Detected incorrect mapping: {handle} -> {existing_mappings[handle]}")
                needs_reset = True

        if needs_reset:
            logging.warning("Resetting all mappings...")
            reset_channel_mappings()

    # Resolve channel names
    logging.info("=" * 60)
    logging.info("RESOLVING CHANNEL NAMES (COMPREHENSIVE)")
    logging.info("=" * 60)

    channel_handle_to_name = get_channel_names(api_key, yt_channels)

    missing = [h for h in yt_channels if not channel_handle_to_name.get(h)]
    for m in missing:
        logging.error(f"Failed to resolve: {m}")
    if missing:
        logging.warning(f"Could not resolve {len(missing)} handles: {missing}")

    logging.info("\n" + "=" * 60)
    logging.info("FINAL CHANNEL MAPPINGS:")
    for handle, name in channel_handle_to_name.items():
        status = "✓" if name else "✗"
        logging.info(f"  {status} {handle} -> {name if name else 'NOT FOUND'}")
    logging.info("=" * 60 + "\n")

    # All channels in PASSTHROUGH (by NAME); fetch layer uses IDs built from these names
    PASSTHROUGH = [name for name in channel_handle_to_name.values() if name]
    logging.info(f"PASSTHROUGH mode enabled for {len(PASSTHROUGH)} channels")

    # Ensure CSV exists/migrated + capture IDs BEFORE we fetch
    _csv_exists, csv_path = setup_csv()
    try:
        df_before = pd.read_csv(csv_path, encoding='utf-8')
    except FileNotFoundError:
        df_before = pd.DataFrame(columns=['video_id'])
    prev_ids = set(df_before['video_id'].astype(str)) if 'video_id' in df_before.columns else set()

    # NEW: optional specific video IDs (comma or newline separated)
    specific_video_ids = _parse_id_list_env("YOUTUBE_VIDEO_IDS")

    # Build credentials once (match your channel flow behavior)
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials: Optional[ServiceAccountCredentials] = None
    if service_account_file:
        credentials = authenticate_service_account(service_account_file)

    # Add specific videos first (so channel walks won’t re-add them)
    if specific_video_ids:
        asyncio.run(fetch_specific_videos(
            api_key=api_key,
            credentials=credentials,
            csv_file_path=csv_path,
            existing_video_ids=prev_ids,
            video_ids=specific_video_ids,
            keywords=[],  # or reuse your globals
            keywords_to_exclude=[],  # or reuse your globals
        ))

    # Fetch videos
    yt_playlists = os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists:
        yt_playlists = [p.strip() for p in yt_playlists.split(',') if p.strip()]

    if not yt_channels and not yt_playlists:
        raise ValueError("No channels or playlists provided.")

    # No keyword filtering by default (you can change these)
    keywords: List[str] = []
    keywords_to_exclude: List[str] = []

    # Kick off
    asyncio.run(fetch_all_videos(
        api_key=api_key,
        yt_channels=yt_channels,
        yt_playlists=yt_playlists,
        keywords=keywords,
        keywords_to_exclude=keywords_to_exclude,
        PASSTHROUGH=PASSTHROUGH,
        fetch_videos=True
    ))

    # Optional post-filter (does nothing if keywords lists are empty and everything is passthrough)
    filter_and_remove_videos(
        YOUTUBE_VIDEOS_CSV_FILE_PATH,
        keywords=keywords,
        keywords_to_exclude=keywords_to_exclude,
        PASSTHROUGH=PASSTHROUGH,
        channel_specific_filters={}
    )

    # Clean up the CSV (drop dups)
    df_after = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, delimiter=',', encoding='utf-8')
    initial_count = len(df_after)
    if 'video_id' in df_after.columns:
        df_after.drop_duplicates(subset=['video_id'], inplace=True)
    else:
        df_after.drop_duplicates(inplace=True)
    final_count = len(df_after)
    df_after.to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False)

    # Newly added rows (after all steps), compared to prev_ids
    added_df = df_after[~df_after['video_id'].astype(str).isin(prev_ids)].copy() if 'video_id' in df_after.columns else pd.DataFrame()

    # Summary statistics
    logging.info("\n" + "=" * 60)
    logging.info("SUMMARY:")
    logging.info(f"  Total videos in CSV: {final_count}")
    logging.info(f"  Duplicates removed this run: {initial_count - final_count}")
    logging.info(f"  New videos added this run: {len(added_df)}")
    logging.info("=" * 60)

    # Print the enhanced summary table
    print_summary_table(
        csv_path=YOUTUBE_VIDEOS_CSV_FILE_PATH,
        added_df=added_df
    )


if __name__ == '__main__':
    run()
