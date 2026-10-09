"""Re-transcripción estricta con VAD + banda voz + parámetros conservadores.

Caso fundador (Zezzions 2026-05-26): de 274 transcripts, 59 estaban
alucinados ("Suscríbete al canal" ×1517, "no no no" ×28, "la hora de" ×625)
porque Whisper sobre música fuerte inventa texto. Este módulo re-transcribe
con:

  1. Banda voz (highpass 300 Hz, lowpass 3400 Hz) — ffmpeg pre-filter que
     elimina rumble bajo y agudos donde domina la música.
  2. Silero VAD (-vad + ggml-silero-v5.1.2.bin) — saltea zonas sin voz
     genuina, no las transcribe.
  3. --no-fallback — desactiva fallback de temperatura (más conservador).
  4. --no-speech-thold 0.7 (default 0.6) — más estricto detectando silencio.
  5. --logprob-thold -0.5 (default -1.0) — descarta segmentos baja prob.

Esto reduce drásticamente la alucinación a costa de a veces perder palabras
reales en zonas muy ruidosas. Para Zezzions (música ambient + entrevista)
es un trade-off favorable.

Misma API que `lib.transcribe.transcribe()` para sustituibilidad.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

WHISPER_BIN = "whisper-cli"
VAD_MODEL_DEFAULT = str(Path.home() / "cinema-assistant" / "models" / "ggml-silero-v5.1.2.bin")


def transcribe_strict(media_path, model_path, lang="es",
                       vad_model: str = VAD_MODEL_DEFAULT,
                       voice_bandpass: bool = True,
                       no_speech_thold: float = 0.7,
                       logprob_thold: float = -0.5,
                       use_vad: bool = True,
                       timeout_sec: int = 7200):
    """Re-transcripción estricta. Devuelve {'text', 'words'} o None.

    Args:
      vad_model: path al modelo Silero VAD para whisper.cpp. Si no existe,
                 se desactiva VAD automáticamente.
      voice_bandpass: aplicar filtro highpass 300 + lowpass 3400 antes de
                 whisper. Recomendado para audio con música ambient.
      use_vad: usar VAD para saltarse zonas sin voz. Reduce alucinaciones.
    """
    media_path = Path(media_path)
    model_path = Path(model_path)
    if not media_path.exists() or not model_path.exists():
        return None
    vad_available = use_vad and Path(vad_model).exists()

    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        af = []
        if voice_bandpass:
            af.append("highpass=f=300")
            af.append("lowpass=f=3400")
        af_arg = ["-af", ",".join(af)] if af else []
        ext = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(media_path),
             "-ar", "16000", "-ac", "1", "-map", "a:0",
             *af_arg,
             "-c:a", "pcm_s16le", wav],
            capture_output=True,
        )
        if ext.returncode != 0 or not os.path.exists(wav):
            return None
        out = os.path.join(td, "t")
        cmd = [
            WHISPER_BIN, "-m", str(model_path), "-f", wav, "-l", lang,
            "-ml", "1", "-sow",                # word-level timestamps
            "-oj", "-of", out, "-np",
            "--no-fallback",                    # no temperature fallback
            "-nth", str(no_speech_thold),       # 0.7 vs default 0.6
            "-lpt", str(logprob_thold),         # -0.5 vs default -1.0
        ]
        if vad_available:
            cmd.extend([
                "--vad",
                "-vm", vad_model,
                "-vt", "0.55",                  # un poco más estricto que 0.50
                "-vspd", "300",                 # min speech 300ms (filtra clicks)
            ])
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Uso: python transcribe_strict.py <media> <model.bin> [lang]")
        sys.exit(1)
    media, model = sys.argv[1], sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) > 3 else "es"
    r = transcribe_strict(media, model, lang=lang)
    if r is None:
        print("Falló")
        sys.exit(1)
    print(f"Words: {len(r['words'])}")
    print(f"Primeros 200 chars: {r['text'][:200]}")
    print(f"Últimos 200 chars: {r['text'][-200:]}")
