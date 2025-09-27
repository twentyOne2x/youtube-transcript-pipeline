from typing import Dict, List
import logging
from .config import Settings

def debug_log_formats(info: Dict, url: str, settings: Settings):
    if not settings.debug_list_formats:
        return
    fmts = info.get('formats') or []
    logging.info(f"--- Available formats for {url} ({len(fmts)}) ---")
    for f in fmts:
        logging.info(
            "id=%s ext=%s vcodec=%s acodec=%s abr=%s tbr=%s asr=%s proto=%s has_url=%s note=%s",
            f.get('format_id'), f.get('ext'), f.get('vcodec'), f.get('acodec'), f.get('abr'),
            f.get('tbr'), f.get('asr'), f.get('protocol'), bool(f.get('url')), f.get('format_note')
        )

def ranked_audio_format_ids(info: Dict, settings: Settings) -> List[str]:
    fmts = info.get('formats') or []

    def is_audio_only(f):
        return (f.get('vcodec') in (None, 'none')) and (f.get('acodec') not in (None, 'none'))

    def score(f):
        ext = (f.get('ext') or '').lower()
        ac = (f.get('acodec') or '').lower()
        proto = (f.get('protocol') or '')
        abr = int(f.get('abr') or 0)
        s = 0
        if f.get('url'): s += 1000
        if 'm3u8' not in proto: s += 300
        af = settings.audio_format
        if af == 'm4a':
            if ext == 'm4a' or 'mp4a' in ac: s += 150
        elif af == 'opus':
            if ext in ('webm',) or 'opus' in ac: s += 150
        elif af == 'mp3':
            if ext == 'm4a' or 'mp4a' in ac: s += 150
            elif 'opus' in ac: s += 120
        s += abr
        return s

    candidates = [f for f in fmts if is_audio_only(f)]
    candidates.sort(key=score, reverse=True)
    ranked = [f.get('format_id') for f in candidates if f.get('format_id')]

    # 🔽 prepend preferred itags if configured
    if settings.preferred_itags:
        front = [i for i in settings.preferred_itags if i in ranked]
        tail = [i for i in ranked if i not in settings.preferred_itags]
        ranked = front + tail

    return ranked