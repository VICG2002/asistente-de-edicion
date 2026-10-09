"""Technical analysis of video/audio clips via ffmpeg filters.

One ffmpeg pass per clip. Video: signalstats (exposure + frame-to-frame diff for
motion proxy) + blurdetect (overall mean blur from stderr). Audio: astats Overall.
Samples ~1 frame/sec to keep cost bounded.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from statistics import mean, median


META_RE = re.compile(r"^lavfi\.([\w]+)\.([\w\.]+)=([^\s]+)$")
FRAME_RE = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:([\-\d\.]+)$")
BLUR_LINE_RE = re.compile(r"\[Parsed_blurdetect.*\]\s+blur mean:\s+([\d\.\-]+)")
ASTATS_KEYS = {
    "Overall.Peak_level", "Overall.RMS_level",
    "Overall.Flat_factor", "Overall.Number_of_samples",
    "Overall.Min_level", "Overall.Max_level",
}


def _safe_float(s):
    try:
        v = float(s)
        if v != v:
            return None
        if v in (float("inf"), float("-inf")):
            return None
        return v
    except (ValueError, TypeError):
        return None


def build_filter_chain(fps: float) -> tuple[str, str]:
    """Build (video_filter, audio_filter). NO single quotes — passed via subprocess list.

    Sampling: 1 frame per second (clamped). signalstats gives per-frame exposure
    and YDIF (frame-to-frame diff = motion). blurdetect logs an overall mean
    blur to stderr at end of stream.
    """
    sample_every_n = max(1, int(round(fps or 24.0)))
    # format=yuv420p: signalstats reporta luma en la escala NATIVA del material
    # (0-1023 en 10-bit como FX30 HEVC Main10). Los umbrales del motor asumen
    # 8-bit (0-255) — forzar conversión antes de medir normaliza cualquier fuente.
    vf = (
        f"select=not(mod(n\\,{sample_every_n})),"
        f"format=yuv420p,"
        f"signalstats,"
        f"blurdetect,"
        f"metadata=mode=print:file=-"
    )
    af = "astats=metadata=1:reset=0,ametadata=mode=print:file=-"
    return vf, af


def run_ffmpeg(path: Path, fps: float, has_audio: bool, timeout: int = 600) -> tuple[str, str]:
    """Run ffmpeg and return (stdout, stderr). Filter metadata appears in stderr."""
    vf, af = build_filter_chain(fps)
    cmd = ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf", vf]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", af]
    else:
        cmd += ["-an"]
    cmd += ["-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.stdout, proc.stderr


def parse_video_frames(text: str) -> list[dict]:
    """Parse per-frame signalstats metadata. Looks for frame:N then lavfi.signalstats.* lines."""
    frames = []
    current = None
    for line in text.splitlines():
        line = line.strip()
        m = FRAME_RE.match(line)
        if m:
            if current is not None and any(k.startswith("signalstats.") for k in current):
                frames.append(current)
            current = {"frame": int(m.group(1)), "pts_time": _safe_float(m.group(3))}
            continue
        m = META_RE.match(line)
        if m and current is not None:
            ns, key, val = m.group(1), m.group(2), m.group(3)
            if ns != "signalstats":
                continue
            current[f"{ns}.{key}"] = _safe_float(val)
    if current is not None and any(k.startswith("signalstats.") for k in current):
        frames.append(current)
    return frames


def parse_astats_overall(text: str) -> dict:
    """Pull Overall.* keys from astats metadata (most recent values win → final state)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        m = META_RE.match(line)
        if not m or m.group(1) != "astats":
            continue
        key = m.group(2)
        if key in ASTATS_KEYS:
            result[key] = _safe_float(m.group(3))
    return result


def parse_blur_mean(text: str) -> float | None:
    """blurdetect logs '[Parsed_blurdetect_X] blur mean: V.VVV' at end of stream."""
    last = None
    for line in text.splitlines():
        m = BLUR_LINE_RE.search(line)
        if m:
            last = _safe_float(m.group(1))
    return last


def aggregate_video(frames: list[dict]) -> dict:
    if not frames:
        return {}
    yavg = [f.get("signalstats.YAVG") for f in frames if f.get("signalstats.YAVG") is not None]
    ymax = [f.get("signalstats.YMAX") for f in frames if f.get("signalstats.YMAX") is not None]
    ymin = [f.get("signalstats.YMIN") for f in frames if f.get("signalstats.YMIN") is not None]
    yhigh = [f.get("signalstats.YHIGH") for f in frames if f.get("signalstats.YHIGH") is not None]
    ylow = [f.get("signalstats.YLOW") for f in frames if f.get("signalstats.YLOW") is not None]
    ydif = [f.get("signalstats.YDIF") for f in frames if f.get("signalstats.YDIF") is not None]
    satavg = [f.get("signalstats.SATAVG") for f in frames if f.get("signalstats.SATAVG") is not None]

    if not yavg:
        return {}

    n = len(yavg)
    overexposed_frames = sum(1 for v in yavg if v > 200)
    underexposed_frames = sum(1 for v in yavg if v < 30)
    highlights_clipped_frames = sum(1 for v in ymax if v >= 250)
    shadows_crushed_frames = sum(1 for v in ymin if v <= 5)

    return {
        "exposure_y_avg_mean": mean(yavg),
        "exposure_y_avg_median": median(yavg),
        "exposure_y_max_mean": mean(ymax) if ymax else None,
        "exposure_y_min_mean": mean(ymin) if ymin else None,
        "exposure_y_high_mean": mean(yhigh) if yhigh else None,
        "exposure_y_low_mean": mean(ylow) if ylow else None,
        "exposure_over_pct": 100 * overexposed_frames / n,
        "exposure_under_pct": 100 * underexposed_frames / n,
        "highlights_clipped_pct": 100 * highlights_clipped_frames / len(ymax) if ymax else None,
        "shadows_crushed_pct": 100 * shadows_crushed_frames / len(ymin) if ymin else None,
        "motion_score_mean": mean(ydif) if ydif else None,
        "motion_score_max": max(ydif) if ydif else None,
        "saturation_mean": mean(satavg) if satavg else None,
        "frame_samples": n,
    }


def aggregate_audio(astats: dict) -> dict:
    peak = astats.get("Overall.Peak_level")
    rms = astats.get("Overall.RMS_level")
    return {
        "audio_peak_db": peak,
        "audio_rms_db": rms,
        "audio_clip_likely": 1 if (peak is not None and peak >= -0.5) else 0,
        "audio_silent_likely": 1 if (rms is not None and rms < -55) else (1 if rms is None and peak is None else 0),
    }


def overall_flags(metrics: dict) -> tuple[str, list[str]]:
    """Decide keep / review / cull based on metrics. Returns (status, notes)."""
    notes = []
    status = "keep"

    over = metrics.get("exposure_over_pct") or 0
    under = metrics.get("exposure_under_pct") or 0
    clipped = metrics.get("highlights_clipped_pct") or 0
    crushed = metrics.get("shadows_crushed_pct") or 0
    yavg = metrics.get("exposure_y_avg_mean")
    blur = metrics.get("blur_mean")
    audio_rms = metrics.get("audio_rms_db")
    audio_silent = metrics.get("audio_silent_likely")
    audio_clip = metrics.get("audio_clip_likely")
    motion = metrics.get("motion_score_mean")

    if over > 20:
        notes.append(f"sobreexpuesto ({over:.0f}% frames)")
        status = "review"
    if under > 20:
        notes.append(f"subexpuesto ({under:.0f}% frames)")
        status = "review"
    if clipped > 80:
        notes.append(f"highlights quemados ({clipped:.0f}%)")
    if crushed > 80:
        notes.append(f"sombras aplastadas ({crushed:.0f}%)")
    if blur is not None and blur > 8.0:
        notes.append(f"foco débil (blur={blur:.1f})")
        status = "review"
    if audio_silent:
        notes.append("audio silencioso o ausente")
    if audio_clip:
        notes.append(f"audio cerca de clipping (peak={audio_rms:.1f}dB)" if audio_rms else "audio cerca de clipping")
    if motion is not None and motion > 30:
        notes.append(f"mucho movimiento (YDIF={motion:.1f})")

    if (yavg is not None and (yavg > 230 or yavg < 15)) or (blur is not None and blur > 14):
        status = "cull"
    elif over > 60 or under > 60:
        status = "cull"

    return status, notes


def analyze(path: Path, fps: float = 24.0, has_audio: bool = True, timeout: int = 600) -> dict:
    """Run analysis, return flat dict of metrics + status."""
    try:
        stdout, stderr = run_ffmpeg(path, fps, has_audio, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"analysis_status": "timeout"}
    except FileNotFoundError:
        return {"analysis_status": "ffmpeg_missing"}

    output = stdout + stderr
    frames = parse_video_frames(output)
    metrics = aggregate_video(frames)
    metrics["blur_mean"] = parse_blur_mean(stderr)

    if has_audio:
        astats = parse_astats_overall(output)
        metrics.update(aggregate_audio(astats))

    status, notes = overall_flags(metrics)
    metrics["analysis_status"] = status
    metrics["analysis_notes"] = "; ".join(notes) if notes else None
    return metrics
