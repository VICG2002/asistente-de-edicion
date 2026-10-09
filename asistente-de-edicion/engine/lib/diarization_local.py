"""Diarización local SIN Hugging Face gate — VAD + Resemblyzer clustering.

Caso fundador (Zezzions 2026-05-26): pyannote.audio v4 requiere aceptar
términos de uso en huggingface.co/pyannote/speaker-diarization-3.1 (modelo
gated). Para mantener "filosofía gratis 100% local sin dependencias
opcionales", construimos diarización propia con:

  1. webrtcvad — Voice Activity Detection (Google, open-source, sin gate)
  2. Resemblyzer GE2E embeddings — embedding por ventana de 1.5s de voz
  3. Clustering jerárquico aglomerativo (sklearn) — agrupa ventanas similares

Limitaciones documentadas (Zezzions GP musical fuerte):
  - Resemblyzer da similitudes infladas con música ambient (ver
    `patrones-exitosos.md` § "Resemblyzer no discrimina voces…").
  - Para 1-2 speakers limpios funciona; para 3+ en ambiente musical
    confunde. Si el usuario configura HF token y acepta términos, usar
    `lib.speaker_diarization` (pyannote) que es más robusto.

NO sustituye pyannote en todos los casos — es el fallback gratis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import numpy as np


@dataclass
class LocalSegment:
    start: float
    end: float
    speaker_label: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class LocalDiarizationResult:
    segments: list[LocalSegment]
    speakers: list[str]
    duration_per_speaker: dict[str, float]
    total_voice_time: float
    total_audio_dur: float

    @property
    def n_speakers(self) -> int:
        return len(self.speakers)

    @property
    def dominant_speaker(self) -> Optional[str]:
        if not self.duration_per_speaker:
            return None
        return max(self.duration_per_speaker.items(), key=lambda kv: kv[1])[0]

    @property
    def dominant_ratio(self) -> float:
        if self.total_voice_time <= 0:
            return 0.0
        return self.duration_per_speaker.get(self.dominant_speaker, 0.0) / self.total_voice_time


def vad_segments(wav: np.ndarray, sr: int = 16000,
                 frame_ms: int = 30, aggressiveness: int = 3) -> list[tuple[float, float]]:
    """webrtcvad: extrae segmentos de voz [(start_sec, end_sec)].

    aggressiveness:
      0 = menos agresivo (deja entrar más música/ruido como "voz")
      3 = más agresivo (excluye más, mejor para ambiente musical)
    """
    import webrtcvad
    vad = webrtcvad.Vad(aggressiveness)
    frame_len = int(sr * frame_ms / 1000)
    pcm = (wav * 32768).astype(np.int16).tobytes()
    segs: list[tuple[float, float]] = []
    in_speech = False
    seg_start = 0.0
    for i in range(0, len(pcm) - frame_len * 2, frame_len * 2):
        frame = pcm[i:i + frame_len * 2]
        if len(frame) < frame_len * 2:
            break
        t = i / (2 * sr)  # bytes / (2 bytes/sample) / sr
        try:
            is_speech = vad.is_speech(frame, sr)
        except Exception:
            continue
        if is_speech and not in_speech:
            seg_start = t
            in_speech = True
        elif not is_speech and in_speech:
            segs.append((seg_start, t))
            in_speech = False
    if in_speech:
        segs.append((seg_start, len(pcm) / (2 * sr)))
    # Mergear segmentos separados por <300ms
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < 0.3:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= 0.5]


def diarize_local(path: Path, num_speakers: Optional[int] = None,
                  max_speakers: int = 4,
                  window_dur: float = 1.5) -> Optional[LocalDiarizationResult]:
    """Diarización VAD + embeddings + clustering aglomerativo.

    Si num_speakers es None, intenta auto-detectar (2 a max_speakers)
    usando silhouette score.
    """
    try:
        import librosa
        from resemblyzer import VoiceEncoder, preprocess_wav
        from sklearn.cluster import AgglomerativeClustering
        try:
            from sklearn.metrics import silhouette_score
        except Exception:
            silhouette_score = None
    except ImportError as e:
        logging.error(f"diarization_local: falta dependencia: {e}")
        return None

    sr = 16000
    wav, _ = librosa.load(str(path), sr=sr, mono=True)
    wav = wav.astype(np.float32)
    total_dur = len(wav) / sr
    if total_dur < 5.0:
        return None

    # 1. VAD agresivo
    speech_segs = vad_segments(wav, sr, aggressiveness=3)
    if not speech_segs:
        return None
    voice_time = sum(e - s for s, e in speech_segs)

    # 2. Embeddings por ventana
    enc = VoiceEncoder(verbose=False)
    window_samples = int(window_dur * sr)
    points: list[tuple[float, float, np.ndarray]] = []
    for s_sec, e_sec in speech_segs:
        s_idx = int(s_sec * sr)
        e_idx = int(e_sec * sr)
        cur = s_idx
        while cur + window_samples <= e_idx:
            chunk = wav[cur:cur + window_samples]
            if len(chunk) < window_samples * 0.5:
                break
            try:
                chunk_clean = preprocess_wav(chunk, source_sr=sr)
                if len(chunk_clean) < sr * 0.5:
                    cur += window_samples
                    continue
                emb = enc.embed_utterance(chunk_clean)
                points.append((cur / sr, (cur + window_samples) / sr, emb))
            except Exception:
                pass
            cur += window_samples
    if len(points) < 3:
        return None

    X = np.stack([p[2] for p in points])

    # 3. Auto-detect num_speakers vía silhouette si no se dió
    if num_speakers is None:
        if silhouette_score and len(points) >= 4:
            best_k, best_score = 1, -1.0
            for k in range(2, min(max_speakers, len(points)) + 1):
                try:
                    cl = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average").fit(X)
                    s = silhouette_score(X, cl.labels_, metric="cosine")
                    if s > best_score:
                        best_score, best_k = s, k
                except Exception:
                    continue
            num_speakers = best_k if best_score > 0.1 else 1
        else:
            num_speakers = 1

    if num_speakers == 1:
        labels = [0] * len(points)
    else:
        cl = AgglomerativeClustering(n_clusters=num_speakers, metric="cosine", linkage="average").fit(X)
        labels = cl.labels_.tolist()

    # 4. Construir segmentos: mezclar ventanas contiguas con misma etiqueta
    segs: list[LocalSegment] = []
    durations: dict[str, float] = {}
    cur_start = points[0][0]
    cur_end = points[0][1]
    cur_label = labels[0]
    for i in range(1, len(points)):
        s, e, _ = points[i]
        if labels[i] == cur_label and s <= cur_end + 0.5:
            cur_end = e
        else:
            sp = f"SPEAKER_{cur_label:02d}"
            segs.append(LocalSegment(cur_start, cur_end, sp))
            durations[sp] = durations.get(sp, 0.0) + (cur_end - cur_start)
            cur_start, cur_end, cur_label = s, e, labels[i]
    sp = f"SPEAKER_{cur_label:02d}"
    segs.append(LocalSegment(cur_start, cur_end, sp))
    durations[sp] = durations.get(sp, 0.0) + (cur_end - cur_start)

    return LocalDiarizationResult(
        segments=segs,
        speakers=sorted(durations.keys()),
        duration_per_speaker=durations,
        total_voice_time=voice_time,
        total_audio_dur=total_dur,
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python diarization_local.py <audio.wav> [audio2 ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        p = Path(arg)
        print(f"\n=== {p.parent.name}/{p.name} ===")
        r = diarize_local(p, max_speakers=4)
        if r is None:
            print("  (falló o audio muy corto)")
            continue
        print(f"  duración audio   : {r.total_audio_dur:.1f}s")
        print(f"  tiempo de voz    : {r.total_voice_time:.1f}s "
              f"({100*r.total_voice_time/max(r.total_audio_dur,1):.1f}%)")
        print(f"  speakers detected: {r.n_speakers}")
        for sp, dur in sorted(r.duration_per_speaker.items(), key=lambda kv: -kv[1]):
            ratio = dur / max(r.total_voice_time, 1e-3)
            print(f"    {sp}: {dur:6.1f}s ({100*ratio:5.1f}%)")
        print(f"  speaker dominante: {r.dominant_speaker} ({100*r.dominant_ratio:.1f}%)")
