"""Audio-video sync by absolute timecode.

Strategy:
  1. Extract BWF Time Reference + Origination Date from WAV files via exiftool.
     Compute absolute start datetime: origination_date + (time_reference / sample_rate).
  2. Extract container creation_time / timecode from video clips (already in manifest).
  3. Pair videos and audios that overlap in time. Compute offset.
  4. (Future) Waveform cross-correlation as fallback when timecode unavailable.

Returns dicts that bin/sync_audio.py writes to the audio_sync_pairs table.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _exiftool_json(path: Path) -> dict:
    """Run exiftool with JSON output, return first record dict (or {} on failure)."""
    cmd = [
        "exiftool", "-j", "-n",
        "-BWF:all", "-RIFF:all",
        "-DateTimeOriginal", "-TimeReference", "-SampleRate",
        "-Duration", "-OriginationDate", "-OriginationTime",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return data[0] if data else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {}


def _parse_iso_datetime(s: str | None) -> datetime | None:
    """Parse a datetime string into a tz-aware UTC datetime if possible."""
    if not s:
        return None
    s = s.strip()
    formats = (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
        "%Y:%m:%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_date_only(s: str) -> datetime | None:
    """Parse just the date portion (YYYY-MM-DD or YYYY:MM:DD) of a string, dropping any time."""
    if not s:
        return None
    date_part = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%Y:%m:%d"):
        try:
            return datetime.strptime(date_part, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_audio_start(path: Path) -> dict:
    """Return dict with absolute_start (datetime), duration_sec, method, raw fields.

    Sources of truth for the absolute start of a recording, in order of preference:
    1. OriginationDate + OriginationTime (separate BWF fields) — recorder native.
    2. DateTimeOriginal (combined) — used directly.
    3. OriginationDate (date only) + TimeReference/sample_rate (offset from midnight).

    Avoids the double-count trap of adding TimeReference on top of an already-complete
    DateTimeOriginal.
    """
    info = _exiftool_json(path)
    duration_s = info.get("Duration")
    duration_sec = None
    if isinstance(duration_s, (int, float)):
        duration_sec = float(duration_s)
    elif isinstance(duration_s, str):
        m = re.match(r"^\s*([\d\.]+)", duration_s)
        if m:
            duration_sec = float(m.group(1))

    time_ref = info.get("TimeReference")
    sample_rate = info.get("SampleRate")
    orig_date = info.get("OriginationDate")
    orig_time = info.get("OriginationTime")
    dt_original = info.get("DateTimeOriginal")

    abs_start = None
    method = None

    if orig_date and orig_time:
        combined = f"{orig_date.split(' ')[0]} {orig_time}"
        abs_start = _parse_iso_datetime(combined)
        if abs_start is not None:
            method = "bwf_origination_date_time"

    if abs_start is None and dt_original:
        dt = _parse_iso_datetime(dt_original)
        if dt is not None:
            abs_start = dt
            method = "datetime_original"

    if abs_start is None and time_ref is not None and sample_rate and (orig_date or dt_original):
        base = _parse_date_only(orig_date or dt_original)
        if base is not None:
            abs_start = base + timedelta(seconds=float(time_ref) / float(sample_rate))
            method = "date_plus_timereference"

    return {
        "absolute_start": abs_start,
        "duration_sec": duration_sec,
        "method": method,
        "raw_time_reference": time_ref,
        "raw_sample_rate": sample_rate,
        "raw_origination_date": orig_date,
        "raw_origination_time": orig_time,
        "raw_datetime_original": dt_original,
    }


def parse_video_creation_time(creation_time: str | None) -> datetime | None:
    return _parse_iso_datetime(creation_time)


def find_overlaps(
    videos: list[dict],
    audios: list[dict],
    audio_tz_offset_hours: float = 0.0,
) -> list[dict]:
    """Pair videos with audios that overlap in time. Returns list of pair dicts."""
    pairs = []
    for v in videos:
        v_start = v.get("start_dt")
        v_dur = v.get("duration_sec") or 0
        if v_start is None or v_dur <= 0:
            continue
        v_end = v_start + timedelta(seconds=v_dur)
        for a in audios:
            a_start_naive = a.get("start_dt")
            a_dur = a.get("duration_sec") or 0
            if a_start_naive is None or a_dur <= 0:
                continue
            a_start = a_start_naive + timedelta(hours=audio_tz_offset_hours)
            a_end = a_start + timedelta(seconds=a_dur)
            overlap_start = max(v_start, a_start)
            overlap_end = min(v_end, a_end)
            overlap = (overlap_end - overlap_start).total_seconds()
            if overlap <= 0:
                continue
            offset = (a_start - v_start).total_seconds()
            confidence = min(1.0, overlap / max(v_dur, 1.0))
            pairs.append({
                "video_clip_id": v["id"],
                "audio_clip_id": a["id"],
                "method": "timecode",
                "offset_sec": offset,
                "confidence": confidence,
                "video_start_iso": v_start.isoformat(),
                "audio_start_iso": a_start.isoformat(),
                "overlap_sec": overlap,
            })
    return pairs
