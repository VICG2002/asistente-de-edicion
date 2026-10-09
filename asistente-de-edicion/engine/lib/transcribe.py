"""Transcribe audio or video to a word-level transcript via whisper.cpp.

Pipeline: ffmpeg extracts the first audio stream to 16 kHz mono WAV; whisper-cli
transcribes it with one-word segments (-ml 1 -sow) so every word carries a
timestamp. Returns the full text plus a (word, start_seconds) list — the word
timing is what transcript_sync needs to align a clip to an external recording.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

WHISPER_BIN = "whisper-cli"


def transcribe(media_path, model_path, lang="es"):
    """Transcribe one audio/video file.

    Returns {'text': str, 'words': [(word, start_sec), ...]} or None on failure.
    """
    media_path = Path(media_path)
    model_path = Path(model_path)
    if not media_path.exists() or not model_path.exists():
        return None
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        ext = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(media_path),
             "-ar", "16000", "-ac", "1", "-map", "a:0", "-c:a", "pcm_s16le", wav],
            capture_output=True,
        )
        if ext.returncode != 0 or not os.path.exists(wav):
            return None
        out = os.path.join(td, "t")
        try:
            subprocess.run(
                [WHISPER_BIN, "-m", str(model_path), "-f", wav, "-l", lang,
                 "-ml", "1", "-sow", "-oj", "-of", out, "-np"],
                capture_output=True, timeout=7200,
            )
        except subprocess.TimeoutExpired:
            return None
        jf = out + ".json"
        if not os.path.exists(jf):
            return None
        try:
            data = json.loads(Path(jf).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    words = []
    for seg in data.get("transcription", []):
        txt = (seg.get("text") or "").strip()
        frm = (seg.get("offsets") or {}).get("from")
        if txt and frm is not None:
            words.append((txt, frm / 1000.0))
    if not words:
        return None
    return {"text": " ".join(w for w, _ in words), "words": words}
