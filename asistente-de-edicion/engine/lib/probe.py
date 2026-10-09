"""Wrappers around ffprobe / mediainfo for extracting media metadata."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from config import defaults


def ffprobe_json(path: Path) -> dict | None:
    """Run ffprobe and return parsed JSON, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=defaults.FFPROBE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def mediainfo_json(path: Path) -> dict | None:
    """Fallback for formats ffprobe can't read (e.g. BRAW container quirks)."""
    cmd = ["mediainfo", "--Output=JSON", str(path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=defaults.MEDIAINFO_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _pick_stream(data: dict, codec_type: str) -> dict | None:
    for s in data.get("streams", []):
        if s.get("codec_type") == codec_type:
            return s
    return None


def _safe_float(v):
    try:
        if isinstance(v, str) and "/" in v:
            num, den = v.split("/")
            num, den = float(num), float(den)
            return num / den if den else None
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def summarize_ffprobe(data: dict) -> dict:
    """Reduce ffprobe JSON to a flat dict suitable for SQLite columns."""
    fmt = data.get("format", {})
    tags = fmt.get("tags", {}) or {}
    v = _pick_stream(data, "video") or {}
    a = _pick_stream(data, "audio") or {}

    fps = _safe_float(v.get("avg_frame_rate")) or _safe_float(v.get("r_frame_rate"))

    return {
        "codec_name": v.get("codec_name"),
        "width": _safe_int(v.get("width")),
        "height": _safe_int(v.get("height")),
        "fps": fps,
        "duration_sec": _safe_float(fmt.get("duration")),
        "bit_rate": _safe_int(fmt.get("bit_rate")),
        "color_space": v.get("color_space"),
        "pixel_format": v.get("pix_fmt"),
        "has_audio": 1 if a else 0,
        "audio_codec": a.get("codec_name"),
        "audio_channels": _safe_int(a.get("channels")),
        "audio_sample_rate": _safe_int(a.get("sample_rate")),
        "timecode": tags.get("timecode") or (v.get("tags", {}) or {}).get("timecode"),
        "creation_time": tags.get("creation_time"),
        "camera_make": tags.get("make") or tags.get("com.apple.quicktime.make"),
        "camera_model": tags.get("model") or tags.get("com.apple.quicktime.model"),
    }


def summarize_mediainfo(data: dict) -> dict:
    """Reduce mediainfo JSON to the same flat dict shape."""
    tracks = (data.get("media") or {}).get("track", []) or []
    general = next((t for t in tracks if t.get("@type") == "General"), {})
    video = next((t for t in tracks if t.get("@type") == "Video"), {})
    audio = next((t for t in tracks if t.get("@type") == "Audio"), {})

    return {
        "codec_name": video.get("Format") or video.get("CodecID"),
        "width": _safe_int(video.get("Width")),
        "height": _safe_int(video.get("Height")),
        "fps": _safe_float(video.get("FrameRate")),
        "duration_sec": _safe_float(general.get("Duration")),
        "bit_rate": _safe_int(general.get("OverallBitRate")),
        "color_space": video.get("ColorSpace"),
        "pixel_format": video.get("ChromaSubsampling"),
        "has_audio": 1 if audio else 0,
        "audio_codec": audio.get("Format"),
        "audio_channels": _safe_int(audio.get("Channels")),
        "audio_sample_rate": _safe_int(audio.get("SamplingRate")),
        "timecode": general.get("TimeCode_FirstFrame") or video.get("TimeCode_FirstFrame"),
        "creation_time": general.get("Encoded_Date") or general.get("File_Modified_Date"),
        "camera_make": general.get("Make"),
        "camera_model": general.get("Model"),
    }


def enrich_from_sony_sidecar(path: Path, summary: dict) -> dict:
    """Fill camera_make/camera_model from a Sony XAVC sidecar (<clip>M01.XML).

    Sony MP4s don't expose the camera model via ffprobe format tags, but the
    NonRealTimeMeta sidecar always carries <Device manufacturer=.. modelName=..>.
    Generic for any Sony body (FX30, FX3, a7S, ...). No-op if already filled
    or sidecar missing/unparseable.
    """
    if summary.get("camera_model"):
        return summary
    sidecar = path.parent / (path.stem + "M01.XML")
    if not sidecar.exists():
        return summary
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(sidecar).getroot()
        for el in root.iter():
            if el.tag.endswith("Device"):
                summary["camera_make"] = summary.get("camera_make") or el.get("manufacturer")
                summary["camera_model"] = el.get("modelName")
                break
    except Exception:
        pass
    return summary


def probe(path: Path) -> tuple[dict, dict | None, str]:
    """Try ffprobe then mediainfo. Returns (summary, raw, source)."""
    raw = ffprobe_json(path)
    if raw is not None and (raw.get("streams") or raw.get("format")):
        return enrich_from_sony_sidecar(path, summarize_ffprobe(raw)), raw, "ffprobe"
    raw = mediainfo_json(path)
    if raw is not None:
        return enrich_from_sony_sidecar(path, summarize_mediainfo(raw)), raw, "mediainfo"
    return {}, None, "none"
