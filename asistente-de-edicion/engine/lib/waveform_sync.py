"""Audio sync by waveform energy-envelope cross-correlation.

For dual-system footage without reliable jam-sync timecode (DSLR, Blackmagic).
Each clip's scratch audio and each external recording is reduced to a low-rate
loudness envelope; the envelopes are cross-correlated to find the time offset.

Confidence is the normalized correlation peak (0..1) — it matches the rhythm of
sound, robust to level/EQ differences between a camera mic and a field recorder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

EXTRACT_SR = 8000      # Hz — ffmpeg decode rate before enveloping
ENVELOPE_HZ = 100      # Hz — RMS-window rate of the envelope (10 ms resolution)


def extract_envelope(path: Path):
    """Decode the file's first audio stream to a mono loudness envelope.

    Returns a 1-D float32 numpy array sampled at ENVELOPE_HZ, or None on failure.
    """
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", str(EXTRACT_SR), "-map", "a:0", "-f", "f32le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size < EXTRACT_SR:        # need at least ~1 s of audio
        return None
    win = EXTRACT_SR // ENVELOPE_HZ      # samples per envelope point
    usable = (samples.size // win) * win
    frames = samples[:usable].astype(np.float64).reshape(-1, win)
    env = np.sqrt(np.mean(frames * frames, axis=1))
    return env.astype(np.float32)


def correlate(clip_env, wav_env):
    """Cross-correlate a clip envelope against an external-recording envelope.

    Returns (offset_sec, confidence):
      offset_sec  = audio_start - video_start (negative => recording started first)
      confidence  = normalized correlation peak, 0..1
    Returns None when either envelope is too short to correlate.
    """
    if clip_env is None or wav_env is None:
        return None
    needle, hay, swapped = clip_env, wav_env, False
    if len(needle) > len(hay):
        needle, hay, swapped = hay, needle, True
    m, n = len(needle), len(hay)
    if m < 16 or n < 16:
        return None
    a = needle.astype(np.float64) - float(np.mean(needle))
    b = hay.astype(np.float64) - float(np.mean(hay))
    na = float(np.sqrt(np.dot(a, a)))
    if na <= 0.0:
        return None
    size = 1 << int(np.ceil(np.log2(n + m)))
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    max_lag = n - m
    corr = corr[: max_lag + 1]
    # sliding-window energy of b, for a normalized (level-independent) score
    cs = np.concatenate(([0.0], np.cumsum(b * b)))
    win_energy = cs[m : m + max_lag + 1] - cs[0 : max_lag + 1]
    ncc = corr / (na * np.sqrt(np.maximum(win_energy, 1e-9)))
    k = int(np.argmax(ncc))
    conf = float(np.clip(ncc[k], 0.0, 1.0))
    lag_sec = k / ENVELOPE_HZ
    offset = lag_sec if swapped else -lag_sec
    return offset, conf
