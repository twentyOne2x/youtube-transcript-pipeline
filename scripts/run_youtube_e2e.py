import os
import time
import uuid
from pathlib import Path

from dotenv import dotenv_values
from googleapiclient.discovery import build

from src.event_pipeline.schemas import YouTubeNewVideoEvent, Mp3ReadyEvent
from src.event_pipeline.settings import get_settings
from src.event_pipeline.youtube.metadata import YouTubeMetadataClient, persist_metadata, build_download_event
from src.event_pipeline.youtube.downloader import download_mp3, build_ready_event as build_mp3_ready_event
from src.event_pipeline.youtube.diarization import run_diarization, build_ready_event as build_diarization_ready_event
from services.warehouse_ingestion.app import _warehouse_dir
from src.utils.gcs import maybe_upload
from src.data_ingestion_youtube.load.download_mp3.config import Settings as DlSettings

CONFIG = dotenv_values('.env')

YOUTUBE_API_KEY = CONFIG['YOUTUBE_API_KEY']
ASSEMBLY_KEYS = CONFIG.get('ASSEMBLY_AI_API_KEYS', '').strip('"')
ASSEMBLY_AI_API_KEY = ASSEMBLY_KEYS.split(',')[0] if ASSEMBLY_KEYS else None

PROJECT = os.environ.get('GCP_PROJECT') or 'just-skyline-474622-e1'
MEDIA_BUCKET = os.environ.get('MEDIA_BUCKET') or 'media-just-skyline-474622-e1'

if not ASSEMBLY_AI_API_KEY:
    raise SystemExit('Missing ASSEMBLY_AI_API_KEYS in .env')

os.environ.setdefault('GCP_PROJECT', PROJECT)
os.environ.setdefault('MEDIA_BUCKET', MEDIA_BUCKET)
os.environ.setdefault('YOUTUBE_API_KEY', YOUTUBE_API_KEY)
os.environ.setdefault('ASSEMBLY_AI_API_KEY', ASSEMBLY_AI_API_KEY)
os.environ.setdefault('ASSEMBLYAI_API_KEY', ASSEMBLY_AI_API_KEY)

run_id = uuid.uuid4().hex[:8]
base_local = Path('/tmp/youtube_pipeline_e2e') / run_id
metadata_dir = base_local / 'metadata'
audio_dir = base_local / 'audio'
diara_dir = base_local / 'diarization'
warehouse_dir = base_local / 'warehouse'
metadata_dir.mkdir(parents=True, exist_ok=True)
audio_dir.mkdir(parents=True, exist_ok=True)
diara_dir.mkdir(parents=True, exist_ok=True)
warehouse_dir.mkdir(parents=True, exist_ok=True)

os.environ['METADATA_CACHE_DIR'] = str(metadata_dir)
os.environ['YOUTUBE_VIDEO_DIRECTORY'] = str(audio_dir)
gcs_prefix = f'youtube_e2e/{run_id}'
os.environ['YOUTUBE_GCS_BUCKET'] = MEDIA_BUCKET
os.environ['YOUTUBE_GCS_PREFIX'] = gcs_prefix
os.environ['YOUTUBE_KEEP_LOCAL'] = 'true'
os.environ['DIARIZATION_OUTPUT_DIR'] = str(diara_dir)
os.environ['WAREHOUSE_BUFFER_DIR'] = str(warehouse_dir)

get_settings.cache_clear()
settings = get_settings()

video_id = os.environ.get('YOUTUBE_E2E_VIDEO_ID', 'H46AkZbr9K0')
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
resp = youtube.videos().list(part='snippet', id=video_id).execute()
if not resp['items']:
    raise SystemExit(f'Video {video_id} not found')
item = resp['items'][0]
channel_id = item['snippet']['channelId']
print('Using video', video_id, 'from channel', channel_id)

event = YouTubeNewVideoEvent(video_id=video_id, channel_id=channel_id)
metadata_client = YouTubeMetadataClient(api_key=YOUTUBE_API_KEY)
video_metadata = metadata_client.fetch_video(video_id)
metadata_path = persist_metadata(video_metadata, metadata_dir, video_id)
metadata_prefix = f'youtube_metadata/{run_id}'
metadata_uri = maybe_upload(metadata_path, bucket=MEDIA_BUCKET, prefix=metadata_prefix)
print('Metadata stored at', metadata_uri)

download_event = build_download_event(event, video_metadata)
if metadata_uri:
    download_event.metadata['metadata_uri'] = metadata_uri

dl_settings = DlSettings(gcs_bucket=MEDIA_BUCKET, gcs_prefix=gcs_prefix, keep_local_files=True)
artifact = download_mp3(download_event, settings=dl_settings)
print('MP3 artifact local:', artifact.local_path)
print('MP3 artifact gcs :', artifact.gcs_uri)

mp3_ready_event = build_mp3_ready_event(artifact, download_event)
print('mp3_ready_event:', mp3_ready_event)

start = time.time()
local_mp3_event = Mp3ReadyEvent.model_construct(
    gcs_uri=artifact.local_path.as_posix(),
    metadata_uri=mp3_ready_event.metadata_uri,
    video_id=mp3_ready_event.video_id,
)
diara_result = run_diarization(local_mp3_event, api_key=ASSEMBLY_AI_API_KEY, bucket=MEDIA_BUCKET)
print('Diarization uploaded:', diara_result.diarized_uri)
print('Entities uri:', diara_result.entities_uri)
print('Diarization elapsed %.2fs' % (time.time() - start))

d_ready_event = build_diarization_ready_event(mp3_ready_event, diara_result)
buffer_dir = _warehouse_dir()
out_path = buffer_dir / f"{d_ready_event.mp3_uri.replace('gs://', '').replace('/', '_')}.json"
out_path.write_text(str(d_ready_event.to_message()), encoding='utf-8')
print('Warehouse event stored at', out_path)

print('E2E pipeline complete. Run ID:', run_id)
