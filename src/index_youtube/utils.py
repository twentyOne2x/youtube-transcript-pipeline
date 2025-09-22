import json
import logging
import os
import subprocess
from typing import Optional, List, Tuple

import requests
import re
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build

# Add these regex patterns at module level
YT_CHANNEL_ID_RE = re.compile(r'^(UC[0-9A-Za-z_-]{22})$')
YT_CANONICAL_RE = re.compile(
    r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"', re.IGNORECASE)
YT_INITIALDATA_ID_RE = re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{22})"')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

_ROOT_DIR_CACHE = None


def root_directory() -> str:
    """Get the project root directory (cached)."""
    global _ROOT_DIR_CACHE
    if _ROOT_DIR_CACHE:
        return _ROOT_DIR_CACHE

    # Try git
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            check=True
        )
        _ROOT_DIR_CACHE = result.stdout.strip()
        return _ROOT_DIR_CACHE
    except:
        pass

    # Walk up to find markers
    from pathlib import Path
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / '.git').exists() or (parent / 'src').exists():
            _ROOT_DIR_CACHE = str(parent)
            return _ROOT_DIR_CACHE

    _ROOT_DIR_CACHE = str(Path.cwd())
    return _ROOT_DIR_CACHE

def authenticate_service_account(service_account_file: str) -> ServiceAccountCredentials:
    """Authenticates using service account and returns the session."""

    credentials = ServiceAccountCredentials.from_service_account_file(
        service_account_file,
        scopes=["https://www.googleapis.com/auth/youtube.readonly"]
    )
    return credentials


def get_videos_from_playlist(credentials: ServiceAccountCredentials, api_key: str, playlist_id: str, max_results: int = 5000) -> List[dict]:
    # Initialize the YouTube API client
    if credentials is None:
        youtube = build('youtube', 'v3', developerKey=api_key)
    else:
        youtube = build('youtube', 'v3', credentials=credentials, developerKey=api_key)

    video_info = []
    next_page_token = None

    while True:
        playlist_request = youtube.playlistItems().list(
            part="snippet",
            playlistId=playlist_id,
            maxResults=max_results,
            pageToken=next_page_token,
            fields="nextPageToken,items(snippet(publishedAt,resourceId(videoId),title))"
        )
        playlist_response = playlist_request.execute()
        items = playlist_response.get('items', [])

        for item in items:
            video_id = item["snippet"]["resourceId"]["videoId"]
            video_info.append({
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'id': video_id,
                'title': item["snippet"]["title"],
                'publishedAt': item["snippet"]["publishedAt"]
            })

        next_page_token = playlist_response.get("nextPageToken")

        if next_page_token is None or len(video_info) >= max_results:
            break

    return video_info


def get_channel_name(api_key, channel_handle):
    youtube = build('youtube', 'v3', developerKey=api_key)

    request = youtube.search().list(
        part='snippet',
        type='channel',
        q=channel_handle,
        maxResults=1,
        fields='items(snippet(channelTitle))'
    )
    response = request.execute()

    if response['items']:
        return response['items'][0]['snippet']['channelTitle']
    else:
        return None


def load_channel_ids(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as file:
            return json.load(file)
    else:
        return {}



async def get_channel_id(session, api_key, channel_handle, channel_name_to_id):
    """
    Get channel ID using the reliable method
    """
    if channel_handle in channel_name_to_id:
        return channel_name_to_id[channel_handle]

    try:
        channel_id = get_youtube_channel_id(channel_handle, api_key)
        channel_name_to_id[channel_handle] = channel_id
        logging.info(f"Resolved ID: {channel_handle} -> {channel_id}")
        return channel_id
    except Exception as e:
        logging.error(f"Failed to get ID for {channel_handle}: {e}")
        return None



def reset_channel_mappings():
    """
    Delete existing mapping files to force regeneration
    """
    mappings_to_delete = [
        f"{root_directory()}/data/links/channel_handle_to_id_mapping.json",
        f"{root_directory()}/data/links/channel_handle_to_name_mapping.json"
    ]

    for mapping_file in mappings_to_delete:
        if os.path.exists(mapping_file):
            os.remove(mapping_file)
            logging.info(f"Deleted {mapping_file}")

    logging.info("Channel mappings reset. They will be regenerated on next run.")




def get_channel_info_comprehensive(api_key: str, channel_handle: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Get channel name and ID using the reliable method
    """
    try:
        channel_id = get_youtube_channel_id(channel_handle, api_key)

        # Get channel name from ID
        youtube = build('youtube', 'v3', developerKey=api_key)
        response = youtube.channels().list(
            part='snippet',
            id=channel_id
        ).execute()

        if response.get('items'):
            channel_name = response['items'][0]['snippet']['title']
            logging.info(f"✓ Resolved: {channel_handle} -> '{channel_name}' (ID: {channel_id})")
            return channel_name, channel_id
    except Exception as e:
        logging.error(f"✗ Failed to resolve {channel_handle}: {e}")

    return None, None


def get_channel_names(api_key: str, yt_channels: list) -> dict:
    """
    Get channel names and IDs for all channels
    """
    mapping_filepath = os.path.join(root_directory(), "data/links/channel_handle_to_name_mapping.json")
    id_mapping_filepath = os.path.join(root_directory(), "data/links/channel_handle_to_id_mapping.json")

    channel_handle_to_name = {}
    channel_handle_to_id = {}

    for channel_handle in yt_channels:
        channel_name, channel_id = get_channel_info_comprehensive(api_key, channel_handle)

        if channel_name and channel_id:
            channel_handle_to_name[channel_handle] = channel_name
            channel_handle_to_id[channel_handle] = channel_id
        else:
            logging.error(f"Could not resolve {channel_handle}")

    # Save mappings
    with open(mapping_filepath, 'w', encoding='utf-8') as file:
        json.dump(channel_handle_to_name, file, ensure_ascii=False, indent=4)

    with open(id_mapping_filepath, 'w', encoding='utf-8') as file:
        json.dump(channel_handle_to_id, file, ensure_ascii=False, indent=4)

    return channel_handle_to_name

def get_youtube_channel_id(input_str: str, api_key: str) -> str:
    """
    Returns the canonical UC... Channel ID from:
    - UC… ID (returns as-is)
    - Full/partial URL: /channel/UC…, /@handle, /user/…, /c/…
    - Plain @handle
    - Legacy username

    Raises ValueError if not resolvable.
    """
    youtube = build('youtube', 'v3', developerKey=api_key)
    s = input_str.strip()

    # Already a UC… ID?
    if YT_CHANNEL_ID_RE.match(s):
        return s

    # Handle @handles via API
    if s.startswith('@'):
        handle = s[1:]  # Remove @
        try:
            response = youtube.channels().list(
                part='id',
                forHandle=handle
            ).execute()
            items = response.get('items', [])
            if items:
                return items[0]['id']
        except:
            pass

    # Try web scraping as fallback
    try:
        url = f"https://www.youtube.com/{s}" if not s.startswith('http') else s
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, timeout=10, allow_redirects=True, headers=headers)

        if r.status_code == 200:
            # Check final URL for channel ID
            m = re.search(r'/channel/(UC[0-9A-Za-z_-]{22})', r.url)
            if m:
                return m.group(1)

            # Check canonical link
            m = YT_CANONICAL_RE.search(r.text)
            if m:
                return m.group(1)

            # Check ytInitialData
            m = YT_INITIALDATA_ID_RE.search(r.text)
            if m:
                return m.group(1)
    except:
        pass

    raise ValueError(f"Could not resolve Channel ID from: {input_str}")