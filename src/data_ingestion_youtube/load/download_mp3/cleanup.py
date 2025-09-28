#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean & consolidate downloaded YouTube audio trees.

- Merge duplicate video directories that share the same YouTube ID
  (e.g., punctuation/title variant directories), preferring the one
  that already has an .mp3 (largest mp3 wins).
- Convert existing .webm/.m4a/.mp4 audio to .mp3 (ffmpeg required).
- Remove temp crumbs (*.part, *.ytdl, *.part-Frag*).
- If an .mp3 exists (and passes size threshold), remove other media files.
- Prune empty directories.

Run with --dry-run first (default). Use --no-dry-run to apply changes.
"""

from __future__ import annotations
import argparse
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- Config defaults ----------
DEFAULT_KEEP_EXT = {".mp3", ".json", ".txt"}  # always keep these
AUDIO_SRC_EXT = {".webm", ".m4a", ".mp4", ".wav"}  # can be converted to mp3
TEMP_GLOBS = (".part", ".ytdl")  # suffix patterns; .part-FragXX handled by startswith
YT_ID_RE = re.compile(r"_([A-Za-z0-9_-]{11})_")
FALLBACK_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
MIN_MP3_KB = 128  # mp3 smaller than this is likely bogus

# ---------- Helpers ----------
def _human(n: int) -> str:
    for unit in ("B","KB","MB","GB","TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1024:.1f}{unit}"
        n /= 1024
    return f"{n}B"

def find_video_id(name: str) -> Optional[str]:
    m = YT_ID_RE.search(name)
    if m:
        return m.group(1)
    m2 = FALLBACK_ID_RE.search(name)
    return m2.group(0) if m2 else None

def mp3_in(dirpath: Path) -> List[Path]:
    return [p for p in dirpath.glob("*.mp3") if p.is_file()]

def best_dir_for_id(dirs: List[Path]) -> Path:
    # Prefer dir that has mp3; if multiple, choose largest mp3; else most recent mtime
    def score(d: Path) -> Tuple[int, int]:
        m = mp3_in(d)
        if m:
            best = max(m, key=lambda p: p.stat().st_size if p.exists() else 0)
            return (2, best.stat().st_size if best.exists() else 0)
        # score by total size as a tie-break
        total = 0
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_file():
                        total += os.stat(e.path).st_size
                except Exception:
                    pass
        return (1, total)
    return max(dirs, key=score)

def ensure_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def convert_to_mp3(src: Path, dst: Path, bitrate: str = "192k") -> bool:
    # ffmpeg -y -i src -vn -acodec libmp3lame -b:a 192k dst
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-acodec", "libmp3lame", "-b:a", bitrate, str(dst)]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1024 * MIN_MP3_KB
    except Exception:
        return False

def safe_move(src: Path, dst_dir: Path, dry_run: bool) -> Optional[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / src.name
    if target.exists():
        # avoid clobber: append -dupN
        stem, ext = src.stem, src.suffix
        i = 1
        while True:
            cand = dst_dir / f"{stem}-dup{i}{ext}"
            if not cand.exists():
                target = cand
                break
            i += 1
    if dry_run:
        logging.info(f"[dry-run] move {src} -> {target}")
        return target
    try:
        shutil.move(str(src), str(target))
        return target
    except Exception as e:
        logging.warning(f"move failed {src} -> {target}: {e}")
        return None

def remove_file(p: Path, dry_run: bool):
    if dry_run:
        logging.info(f"[dry-run] rm {p}")
        return
    try:
        p.unlink(missing_ok=True)
    except Exception as e:
        logging.warning(f"rm failed {p}: {e}")

def rmdir_if_empty(d: Path, dry_run: bool):
    try:
        if any(d.iterdir()):
            return
    except Exception:
        return
    if dry_run:
        logging.info(f"[dry-run] rmdir {d}")
        return
    try:
        d.rmdir()
    except Exception as e:
        logging.debug(f"rmdir skipped {d}: {e}")

# ---------- Core passes ----------
def merge_duplicate_dirs(root: Path, dry_run: bool) -> int:
    """Group leaf dirs by YouTube ID and merge into a single canonical dir per ID."""
    groups: Dict[str, List[Path]] = {}
    for d in root.rglob("*"):
        if not d.is_dir():
            continue
        vid = find_video_id(d.name)
        if vid:
            groups.setdefault(vid, []).append(d)

    merged = 0
    for vid, dirs in groups.items():
        uniq = sorted(set(map(str, dirs)))
        if len(uniq) <= 1:
            continue
        dirs = sorted({Path(p) for p in uniq})
        canonical = best_dir_for_id(dirs)
        logging.info(f"[merge] {vid}: {len(dirs)} dirs -> canonical: {canonical.name}")
        for other in dirs:
            if other == canonical:
                continue
            # Move files up into canonical
            with os.scandir(other) as it:
                for e in it:
                    p = Path(e.path)
                    if p.is_file():
                        safe_move(p, canonical, dry_run=dry_run)
            # Try remove the now-empty dir
            rmdir_if_empty(other, dry_run=dry_run)
        merged += 1
    return merged

def clean_one_video_dir(d: Path, dry_run: bool, do_convert: bool, delete_extras: bool,
                        min_mp3_kb: int, keep_ext: set[str], ffmpeg_ok: bool):
    # 1) purge temp files
    for e in d.iterdir():
        if e.is_file():
            name = e.name
            if name.endswith(".part") or name.endswith(".ytdl") or ".part-Frag" in name:
                remove_file(e, dry_run)

    # 2) ensure mp3 exists
    mp3s = mp3_in(d)
    have_mp3 = any(p.stat().st_size >= min_mp3_kb * 1024 for p in mp3s) if mp3s else False

    if not have_mp3 and do_convert:
        if not ffmpeg_ok:
            logging.warning(f"[convert] ffmpeg missing; cannot convert in {d}")
        else:
            # pick best source by size
            candidates = []
            for ext in AUDIO_SRC_EXT:
                candidates.extend([p for p in d.glob(f"*{ext}") if p.is_file()])
            if candidates:
                src = max(candidates, key=lambda p: p.stat().st_size)
                dst = d / (src.stem + ".mp3")
                logging.info(f"[convert] {src.name} -> {dst.name}")
                if not dry_run:
                    ok = convert_to_mp3(src, dst)
                    if not ok:
                        logging.warning(f"[convert] failed for {src.name}")

    # 3) delete extra media if mp3 present
    mp3s = mp3_in(d)
    have_mp3 = any(p.stat().st_size >= min_mp3_kb * 1024 for p in mp3s) if mp3s else False

    if have_mp3 and delete_extras:
        for e in d.iterdir():
            if e.is_file():
                ext = e.suffix.lower()
                if ext in AUDIO_SRC_EXT:
                    # Remove only if mp3 present and > threshold
                    logging.info(f"[prune] removing extra media: {e.name}")
                    remove_file(e, dry_run)

    # 4) remove tiny/bogus mp3s
    for m in mp3_in(d):
        if m.stat().st_size < min_mp3_kb * 1024:
            logging.info(f"[prune] mp3 too small ({_human(m.stat().st_size)}): {m.name}")
            remove_file(m, dry_run)

    # 5) remove non-kept extensions EXCEPT the ones we explicitly keep
    for e in list(d.iterdir()):
        if e.is_file():
            ext = e.suffix.lower()
            if ext and ext not in keep_ext and ext not in AUDIO_SRC_EXT and not e.name.endswith(".mp3"):
                # leave unknowns; we’re strict but safe: only delete obvious temp above.
                continue

    # 6) try to prune dir if empty
    rmdir_if_empty(d, dry_run)

def clean_tree(root: Path, dry_run: bool = True, do_convert: bool = True, delete_extras: bool = True,
               merge_dups: bool = True, min_mp3_kb: int = MIN_MP3_KB,
               keep_ext: Optional[set[str]] = None) -> None:
    keep_ext = set(keep_ext or DEFAULT_KEEP_EXT)
    ffmpeg_ok = ensure_ffmpeg() if do_convert else False

    if merge_dups:
        merged = merge_duplicate_dirs(root, dry_run=dry_run)
        if merged:
            logging.info(f"[merge] completed for {merged} video-id groups")

    # Walk leaf dirs that look like video folders
    video_dirs: List[Path] = []
    for d in root.rglob("*"):
        if d.is_dir() and find_video_id(d.name):
            video_dirs.append(d)
    # Sort shortest path first (higher probability of being actual leaf)
    video_dirs.sort(key=lambda p: len(p.as_posix()))

    for d in video_dirs:
        clean_one_video_dir(d, dry_run=dry_run, do_convert=do_convert, delete_extras=delete_extras,
                            min_mp3_kb=min_mp3_kb, keep_ext=keep_ext, ffmpeg_ok=ffmpeg_ok)

    # Finally, prune any now-empty non-video directories
    empties = []
    for d in sorted({p.parent for p in video_dirs}, key=lambda p: len(p.as_posix()), reverse=True):
        rmdir_if_empty(d, dry_run)
        try:
            if not any(d.iterdir()):
                empties.append(d)
        except Exception:
            pass
    if empties:
        logging.info(f"Pruned {len(empties)} empty directories (or scheduled in dry-run).")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Clean & consolidate YouTube audio tree.")
    ap.add_argument("--root", type=str, required=True,
                    help="Root directory containing channel subfolders (e.g., $YOUTUBE_VIDEO_DIRECTORY or datasets/.../@channel).")
    ap.add_argument("--no-merge-duplicates", action="store_true",
                    help="Disable merging directories that share the same YouTube video ID.")
    ap.add_argument("--no-convert", action="store_true", help="Disable converting .webm/.m4a/.mp4 to .mp3")
    ap.add_argument("--keep-video-extras", action="store_true",
                    help="Keep .webm/.mp4/.m4a even if .mp3 exists.")
    ap.add_argument("--min-mp3-kb", type=int, default=MIN_MP3_KB, help="Minimum mp3 size to consider valid.")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Show actions without changing anything (default).")
    ap.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Apply changes.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s - %(message)s",
    )

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"Root does not exist: {root}")

    clean_tree(
        root=root,
        dry_run=args.dry_run,
        do_convert=not args.no_convert,
        delete_extras=not args.keep_video_extras,
        merge_dups=not args.no_merge_duplicates,
        min_mp3_kb=max(0, args.min_mp3_kb),
        keep_ext=DEFAULT_KEEP_EXT,
    )
    logging.info("Done.")

if __name__ == "__main__":
    main()
