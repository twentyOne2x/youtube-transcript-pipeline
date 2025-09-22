import os
import traceback
from typing import List, Optional
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

from src.index_youtube.constants import KEYWORDS_TO_INCLUDE, KEYWORDS_TO_EXCLUDE, YOUTUBE_VIDEOS_CSV_FILE_PATH, AUTHORS, FIRMS
from src.index_youtube.utils import root_directory, authenticate_service_account, get_videos_from_playlist, \
    get_channel_id, reset_channel_mappings, get_channel_names

# Load environment variables from the .env file
load_dotenv()


async def get_multiple_video_details(channel_name, youtube, video_ids, keywords, keywords_to_exclude, PASSTHROUGH):
    MAX_IDS_PER_REQUEST = 50  # YouTube API's limitation
    logging.info(f"[{channel_name}] Fetching video details for {len(video_ids)} videos...")

    # This function will handle the request and processing of each batch of video IDs
    async def process_id_batch(id_batch):
        try:
            id_list = ','.join(id_batch)

            video_response = youtube.videos().list(
                part="snippet",
                id=id_list  # Pass a batch of video IDs here
            ).execute()

            items = video_response.get('items', [])

            youtube_videos_df = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, encoding='utf-8')
            youtube_videos_df['title'] = youtube_videos_df['title'].str.replace(' +', ' ', regex=True)

            batch_video_details = []

            for item in items:
                video_info_item = item['snippet']
                video_title = video_info_item['title']

                video_row = youtube_videos_df[youtube_videos_df['title'] == video_title]

                def append_video_details(video_row, video_info_item, video_title, item):
                    if not video_row.empty:
                        # print(f"Video {video_title} already exists in the CSV file. Skipping...")
                        return

                    published_at = video_info_item['publishedAt']
                    parsed_published_at = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")

                    return {
                        'title': video_title,
                        'channel_name': video_info_item['channelTitle'],
                        'published_date': parsed_published_at.strftime("%Y-%m-%d"),
                        'url': f'https://www.youtube.com/watch?v={item["id"]}',
                    }

                if video_info_item['channelTitle'] in PASSTHROUGH:
                    video_detail = append_video_details(video_row, video_info_item, video_title, item)
                    if video_detail:
                        batch_video_details.append(video_detail)
                elif (not keywords or any(keyword.lower() in video_title.lower() for keyword in keywords)) \
                        and not any(keyword.lower() in video_title.lower() for keyword in keywords_to_exclude):
                    video_detail = append_video_details(video_row, video_info_item, video_title, item)
                    if video_detail:
                        batch_video_details.append(video_detail)

            return batch_video_details

        except Exception as e:
            logging.error(f"Error occurred while fetching video details. Error: {e}")
            traceback.print_exc()
            return []

    # Now we create batches of video IDs and collect the details
    all_video_details = []
    for i in range(0, len(video_ids), MAX_IDS_PER_REQUEST):
        batch = video_ids[i:i + MAX_IDS_PER_REQUEST]
        batch_details = await process_id_batch(batch)  # async function to process each batch
        all_video_details.extend(batch_details)

    return all_video_details


async def get_video_info(session, credentials: ServiceAccountCredentials, api_key: str, channel_id: str, channel_name: str, PASSTHROUGH, max_results: int = 50) -> List[dict]:
    """
    Retrieves video information (URL, ID, title, and published date) from a YouTube channel using the YouTube Data API.

    Args:
        api_key (str): Your YouTube Data API key.
        channel_id (str): The YouTube channel ID.
        max_results (int, optional): Maximum number of results to retrieve. Defaults to 50.

    Returns:
        list: A list of dictionaries containing video URL, ID, title, and published date from the channel.
    """
    # Initialize the YouTube API client
    youtube = build('youtube', 'v3', credentials=credentials, developerKey=api_key)

    # Get the "Uploads" playlist ID
    channel_request = youtube.channels().list(
        part="contentDetails",
        id=channel_id,
        fields="items/contentDetails/relatedPlaylists/uploads"
    )
    channel_response = channel_request.execute()
    try:
        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        logging.error(f"Error occurred while fetching the uploads playlist ID. Error: {e}")
        return []
    # Fetch videos from the "Uploads" playlist
    video_info = []
    next_page_token = None

    while True:
        playlist_request = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=max_results,
            pageToken=next_page_token,
        )
        try:
            playlist_response = playlist_request.execute()
        except Exception as e:
            logging.error(f"Error occurred while fetching videos from the channel. Error: {e}")
            return video_info
        video_ids = [item["snippet"]["resourceId"]["videoId"] for item in playlist_response.get('items', [])]
        video_details = await get_multiple_video_details(channel_name, youtube, video_ids, KEYWORDS_TO_INCLUDE, KEYWORDS_TO_EXCLUDE, PASSTHROUGH)

        video_info.extend([video for video in video_details if video])  # Extend the list instead of overwriting it

        next_page_token = playlist_response.get('nextPageToken')

        if not next_page_token:
            break

    return video_info


def save_video_info_to_csv(video_info_list, csv_file_path, existing_video_names, headers):
    # Remove duplicates based on video titles
    video_info_list = [
        video_info for video_info in video_info_list
        if video_info['title'] not in existing_video_names
    ]

    # Read existing CSV into a DataFrame, or create a new one if the file doesn't exist
    try:
        existing_df = pd.read_csv(csv_file_path)
    except FileNotFoundError:
        existing_df = pd.DataFrame(columns=headers)

    # Filter out existing video names
    new_videos = [video for video in video_info_list if video['title'] not in existing_video_names]

    # Convert new video info list to DataFrame
    new_df = pd.DataFrame(new_videos, columns=headers)

    # Concatenate new data with existing data
    combined_df = pd.concat([existing_df, new_df], ignore_index=True)

    # Write the combined DataFrame to CSV
    combined_df.to_csv(csv_file_path, index=False, quoting=csv.QUOTE_MINIMAL)

    # Log the added videos
    #for video in new_videos:
    #    logging.info(f"Added: {video}")


async def fetch_and_save_channel_videos_async(session, channel_id, channel_name, credentials, api_key, csv_file_path, existing_video_names, headers, PASSTHROUGH):
    video_info_list = await get_video_info(session, credentials, api_key, channel_id, channel_name, PASSTHROUGH)

    save_video_info_to_csv(video_info_list, csv_file_path, existing_video_names, headers)
    logging.info(f"[{channel_name}] Saved {len(video_info_list)} videos to CSV file {csv_file_path}.")


async def fetch_youtube_videos(api_key, yt_channels, yt_playlists, keywords, keywords_to_exclude, PASSTHROUGH, fetch_videos):
    """
    Fetches YouTube video information from specified channels and playlists, and optionally filters them based on keywords.

    Args:
        api_key (str): Your YouTube Data API key.
        yt_channels (Optional[List[str]]): List of YouTube channel names or IDs to fetch videos from.
        yt_playlists (Optional[List[str]]): List of YouTube playlist IDs to fetch videos from.
        keywords (List[str]): Keywords to filter videos by.
        keywords_to_exclude (List[str]): Keywords to exclude videos by.
        fetch_videos (bool): Whether to fetch videos or not.

    Returns:
        List[dict]: A list of dictionaries containing video information.
    """
    service_account_file = os.environ.get('SERVICE_ACCOUNT_FILE')
    credentials = None

    if service_account_file:
        credentials = authenticate_service_account(service_account_file)
        logging.info("Service account file found. Proceeding with public channels, playlists, or private videos if accessible via Google Service Account.")
    else:
        logging.info("No service account file found. Proceeding with public channels or playlists.")

    csv_file_exists, csv_file_path, headers = setup_csv()

    existing_data, existing_video_names, existing_channel_names = load_existing_data(csv_file_exists, csv_file_path)

    channel_handle_to_name = get_channel_names(api_key, yt_channels)

    channels_in_csv, channels_not_in_csv = separate_channels_based_on_csv(channel_handle_to_name, existing_channel_names, yt_channels)

    if fetch_videos:
        await fetch_channel_videos(api_key, channel_handle_to_name, channels_in_csv, channels_not_in_csv, credentials, csv_file_path, existing_video_names, headers, yt_channels, PASSTHROUGH)

        await fetch_playlist_videos(api_key, credentials, csv_file_path, existing_video_names, headers, yt_playlists)

    return existing_data


async def fetch_playlist_videos(api_key, credentials, csv_file_path, existing_video_names, headers, yt_playlists):
    if yt_playlists:
        for playlist_id in yt_playlists:
            video_info_list = get_videos_from_playlist(credentials, api_key, playlist_id)
            save_video_info_to_csv(video_info_list, csv_file_path, existing_video_names, headers)


async def fetch_channel_videos(api_key, channel_handle_to_name, channels_in_csv, channels_not_in_csv, credentials, csv_file_path, existing_video_names, headers, yt_channels, PASSTHROUGH):
    # Define the path for storing the mapping between channel names and their IDs
    channel_mapping_filepath = f"{root_directory()}/data/links/channel_handle_to_id_mapping.json"

    # Load existing mappings if the file exists, or initialize an empty dictionary
    channel_name_to_id = {}  # Initialize regardless
    if os.path.exists(channel_mapping_filepath):
        with open(channel_mapping_filepath, 'r', encoding='utf-8') as file:
            channel_name_to_id = json.load(file)
    else:
        # Here, the file does not exist, so we're creating a new one with empty data.
        # This ensures that a file is present from this point forward.
        with open(channel_mapping_filepath, 'w', encoding='utf-8') as file:
            json.dump(channel_name_to_id, file, ensure_ascii=False, indent=4)

    async with aiohttp.ClientSession() as session:
        # This part of the logic is kept as originally intended, processing the channels based on whether they are in the CSV or not
        all_channels = set(channels_in_csv + channels_not_in_csv)  # Avoid duplicate channel processing
        for channel_handle in all_channels:
            channel_name = channel_handle_to_name.get(channel_handle)
            if channel_name:
                # Retrieve the channel ID, either from the mapping or by requesting it
                channel_id = await get_channel_id(session, api_key, channel_handle, channel_name_to_id)
                if channel_id:
                    # Your existing method to fetch and save videos
                    # session, channel_id, channel_name, credentials, api_key, csv_file_path, existing_video_names, headers
                    await fetch_and_save_channel_videos_async(session, channel_id, channel_name, credentials, api_key, csv_file_path, existing_video_names, headers, PASSTHROUGH)

    # After processing all channels, save the potentially updated mapping back to the file
    with open(channel_mapping_filepath, 'w', encoding='utf-8') as file:
        json.dump(channel_name_to_id, file, ensure_ascii=False, indent=4)


def separate_channels_based_on_csv(channel_handle_to_name, existing_channel_names, yt_channels):
    # Creating two separate lists for channels: one for channels not in the CSV file,
    # and another for channels already in the CSV file
    channels_not_in_csv = []
    channels_in_csv = []
    for channel_handle in yt_channels:
        channel_name = channel_handle_to_name.get(channel_handle)
        if channel_name:
            if channel_name in existing_channel_names:
                channels_in_csv.append(channel_handle)
            else:
                channels_not_in_csv.append(channel_handle)
    return channels_in_csv, channels_not_in_csv



def load_existing_data(csv_file_exists, csv_file_path):
    # Create a DataFrame to store existing data read from the CSV file and create sets of existing video names and channel names
    if csv_file_exists:
        existing_data_df = pd.read_csv(csv_file_path, encoding='utf-8')

        # Create a set for existing video names for faster lookup
        existing_video_names = set(existing_data_df['title'].str.strip())

        # Create a set for existing channel names for faster lookup
        existing_channel_names = set(existing_data_df['channel_name'].str.strip())

        # Convert the DataFrame back to a list of dictionaries to create existing_data
        existing_data = existing_data_df.to_dict('records')
    else:
        # If the CSV file does not exist, initialize empty sets and list
        existing_data = []
        existing_video_names = set()
        existing_channel_names = set()

    return existing_data, existing_video_names, existing_channel_names


def setup_csv():
    # Check if the CSV file already exists
    csv_file_exists = os.path.exists(YOUTUBE_VIDEOS_CSV_FILE_PATH)
    headers = ['title', 'channel_name', 'published_date', 'url']
    # Check if the CSV file exists and if its headers match the expected headers
    if csv_file_exists:
        existing_data_df = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, encoding='utf-8', nrows=0)  # Read just the header
        existing_headers = existing_data_df.columns.tolist()

        if existing_headers != headers:
            # Create a new CSV file with the specified headers
            pd.DataFrame(columns=headers).to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False, encoding='utf-8')
    else:
        # Create a new CSV file with the specified headers
        pd.DataFrame(columns=headers).to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False, encoding='utf-8')
    return csv_file_exists, YOUTUBE_VIDEOS_CSV_FILE_PATH, headers


def drop_duplicates_and_write_to_csv(filtered_away_csv_file_path, filtered_away_video_info_list, headers):
    # Load current CSV data
    existing_rows = []
    if os.path.exists(filtered_away_csv_file_path):
        with open(filtered_away_csv_file_path, 'r', newline='', encoding='utf-8') as csv_file:
            reader = csv.DictReader(csv_file)
            existing_rows = [row for row in reader]

    # Combine the existing rows with the new rows, and remove duplicates
    all_rows = existing_rows + filtered_away_video_info_list
    deduplicated_rows = {row['url']: row for row in all_rows}.values()

    # Write the deduplicated rows back to the CSV file
    with open(filtered_away_csv_file_path, 'w', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(deduplicated_rows)


def filter_and_remove_videos(input_csv_path, keywords, keywords_to_exclude, PASSTHROUGH, channel_specific_filters=None):
    if channel_specific_filters is None:
        channel_specific_filters = {}
    df = pd.read_csv(input_csv_path)

    # Separate rows corresponding to channels in PASSTHROUGH
    passthrough_df = df[df['channel_name'].str.lower().isin([channel.lower() for channel in PASSTHROUGH])]
    non_passthrough_df = df[~df['channel_name'].str.lower().isin([channel.lower() for channel in PASSTHROUGH])]

    # Apply global keyword filtering only to non-PASSTHROUGH channels
    if keywords:  # Only filter if keywords exist
        global_filtered_df = non_passthrough_df[non_passthrough_df['title'].str.lower().str.contains('|'.join(keywords).lower(), na=False)]
    else:
        global_filtered_df = non_passthrough_df  # Keep all non-passthrough if no keywords

    # Filter out titles from passthrough_df that contain any of the keywords_to_exclude
    if keywords_to_exclude:  # Only filter if exclusion keywords exist
        passthrough_df = passthrough_df[~passthrough_df['title'].str.lower().str.contains('|'.join(keywords_to_exclude).lower(), na=False)]

    # Concatenate PASSTHROUGH and non-PASSTHROUGH videos
    global_filtered_df = pd.concat([global_filtered_df, passthrough_df])

    # Convert keys in channel_specific_filters to lowercase
    channel_specific_filters = {channel.lower(): filters for channel, filters in channel_specific_filters.items()}

    # Initialize DataFrame for final filtered videos
    final_filtered_df = pd.DataFrame()

    # Identify the channels in the DataFrame that have channel-specific filters
    channels_with_specific_filters = set(global_filtered_df['channel_name'].str.lower()) & set(channel_specific_filters.keys())

    # Apply channel-specific keyword filtering to the channels identified
    for channel in channels_with_specific_filters:
        channel_keywords = channel_specific_filters[channel]
        channel_df = global_filtered_df[global_filtered_df['channel_name'].str.lower() == channel]
        channel_filtered_df = channel_df[channel_df['title'].str.lower().str.contains('|'.join(channel_keywords).lower(), na=False)]
        final_filtered_df = pd.concat([final_filtered_df, channel_filtered_df])

    # Concatenate videos from channels that did not have channel-specific filters
    final_filtered_df = pd.concat([final_filtered_df, global_filtered_df[~global_filtered_df['channel_name'].str.lower().isin(channels_with_specific_filters)]])

    # Identify removed videos
    removed_df = df[~df['title'].isin(final_filtered_df['title'])]

    # Log the removed video titles
    for _, removed_video in removed_df.iterrows():
        logging.info(f"Removed video: {removed_video['title']} - Channel: [{removed_video['channel_name']}]")

    # Append removed videos to filtered_away_youtube_videos.csv
    filtered_away_csv_file_path = f"{root_directory()}/data/links/youtube/filtered_away_youtube_videos.csv"
    write_filtered_away_header = not os.path.exists(filtered_away_csv_file_path)
    removed_df.to_csv(filtered_away_csv_file_path, mode='a', header=write_filtered_away_header, index=False)

    # Write the filtered data back to the CSV file
    final_filtered_df.to_csv(input_csv_path, index=False)


async def fetch_all_videos(api_key: str, yt_channels: Optional[List[str]] = None, yt_playlists: Optional[List[str]] = None,
              keywords: List[str] = None, keywords_to_exclude: List[str] = None, PASSTHROUGH: List[str] = None, fetch_videos: bool = True):
    await fetch_youtube_videos(api_key, yt_channels, yt_playlists, keywords, keywords_to_exclude, PASSTHROUGH, fetch_videos)


# Function to read the channel handles from a file
def get_youtube_channels_from_file(file_path):
    with open(file_path, 'r') as file:
        channels = file.read().split(',')
    return channels


def run():
    fetch_videos = True

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Get all channel names that will be processed
    yt_channels_file = os.path.join(root_directory(), 'data/links/youtube/youtube_channel_handles.txt')
    yt_channels = get_youtube_channels_from_file(yt_channels_file)

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        raise ValueError("No API key provided. Please provide an API key via command line argument or .env file.")

    # Check for known bad mappings and reset if needed
    mapping_file = os.path.join(root_directory(), "data/links/channel_handle_to_name_mapping.json")
    if os.path.exists(mapping_file):
        with open(mapping_file, 'r') as f:
            existing_mappings = json.load(f)

            # Known incorrect mappings
            bad_mappings = {
                '@Delphi_Digital': ['Jambo Technology', 'DealFlow Podcast'],
                '@SolanaFndn': ['Web3RM', 'PAWS LABS OFFICIAL'],
                '@notthreadguy': ['Thread Guy Shorts', 'Thread Guy Media', 'Thread Guy Clips']
            }

            # Check if any bad mapping exists
            needs_reset = False
            for handle, wrong_names in bad_mappings.items():
                if handle in existing_mappings and existing_mappings[handle] in wrong_names:
                    logging.warning(f"Detected incorrect mapping: {handle} -> {existing_mappings[handle]}")
                    needs_reset = True

            if needs_reset:
                logging.warning("Resetting all mappings...")
                reset_channel_mappings()

    # Get channel names using the comprehensive resolution method
    logging.info("=" * 60)
    logging.info("RESOLVING CHANNEL NAMES (COMPREHENSIVE)")
    logging.info("=" * 60)

    channel_handle_to_name = get_channel_names(api_key, yt_channels)

    # Verify we got valid mappings for all channels
    missing_channels = []
    for channel in yt_channels:
        if channel not in channel_handle_to_name or not channel_handle_to_name[channel]:
            missing_channels.append(channel)
            logging.error(f"Failed to resolve: {channel}")

    if missing_channels:
        logging.warning(f"Could not resolve {len(missing_channels)} channels: {missing_channels}")
        # You might want to exit or handle this differently

    # Display final mappings
    logging.info("\n" + "=" * 60)
    logging.info("FINAL CHANNEL MAPPINGS:")
    for handle, name in channel_handle_to_name.items():
        status = "✓" if name else "✗"
        logging.info(f"  {status} {handle} -> {name if name else 'NOT FOUND'}")
    logging.info("=" * 60 + "\n")

    # Put ALL channels in PASSTHROUGH (filter out None values)
    PASSTHROUGH = [name for name in channel_handle_to_name.values() if name]

    logging.info(f"PASSTHROUGH mode enabled for {len(PASSTHROUGH)} channels")

    # Empty channel-specific filters since everything is in passthrough
    channel_specific_filters = {}

    if not fetch_videos:
        logging.info("Applying new filters only, not fetching videos.")
        return

    # Fetch videos
    yt_playlists = os.environ.get('YOUTUBE_PLAYLISTS')
    if yt_playlists:
        yt_playlists = [playlist.strip() for playlist in yt_playlists.split(',')]

    if not yt_channels and not yt_playlists:
        raise ValueError(
            "No channels or playlists provided. Please provide channel names, IDs, or playlist IDs.")

    # Fetch all videos without filtering
    asyncio.run(fetch_all_videos(
        api_key,
        yt_channels,
        yt_playlists,
        keywords=[],  # No keyword filtering
        keywords_to_exclude=[],  # No exclusions
        PASSTHROUGH=PASSTHROUGH,
        fetch_videos=True
    ))

    # Apply filter (which won't filter anything since everything is in PASSTHROUGH)
    filter_and_remove_videos(
        YOUTUBE_VIDEOS_CSV_FILE_PATH,
        keywords=[],
        keywords_to_exclude=[],
        PASSTHROUGH=PASSTHROUGH,
        channel_specific_filters=channel_specific_filters
    )

    # Clean up the CSV
    df = pd.read_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, delimiter=',')
    initial_count = len(df)

    # Drop duplicates
    df.drop_duplicates(inplace=True)
    final_count = len(df)

    # Save cleaned data
    df.to_csv(YOUTUBE_VIDEOS_CSV_FILE_PATH, index=False)

    # Summary statistics
    logging.info("\n" + "=" * 60)
    logging.info("SUMMARY:")
    logging.info(f"  Total videos: {final_count}")
    logging.info(f"  Duplicates removed: {initial_count - final_count}")
    logging.info(f"  Channels processed: {len([c for c in channel_handle_to_name.values() if c])}")
    logging.info("=" * 60)

if __name__ == '__main__':
    run()
