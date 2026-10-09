"""Diarización de speakers con pyannote.audio v4 — quién habla cuándo.

Caso de uso (Zezzions 2026-05-26): la entrevista 2645 era DUAL-LAVALIER —
dos personas (ENTREVISTADO_13 y ENTREVISTADO_10) en la misma entrevista, cada una con su lavalier
en un canal distinto (Dr e Izq). El detector actual la pasó por alto.

Con diarización:
  - Para cada audio externo (lavalier), identificar cuántos speakers hay.
  - Si en el audio Dr hay un speaker dominante S1 y en Izq hay otro S2,
    es dual-lavalier con dos interlocutores → exigir sync con AMBOS.
  - Si los dos lavaliers capturan al MISMO speaker (mismo S1 dominante),
    es un solo entrevistado con dos micros, syncar con el que tenga mejor SNR.

También útil para:
  - Detectar entrevistador vs entrevistado en cada audio.
  - Romper falsos positivos: si la voz en el audio NO coincide con la
    voz del video (transcript), es par incorrecto.
  - Auto-categorizar: audio con 1 speaker dominante > 70% del tiempo →
    "voz individual"; con 2+ speakers balanceados → "diálogo / entrevista".

Requiere: `pip install pyannote.audio` (>= 4.0) y modelo pretrained.
Modelo: `pyannote/speaker-diarization-3.1` (sin token Hugging Face en v4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from dataclasses import dataclass
import logging


@dataclass
class SpeakerSegment:
    start: float
    end: float
    speaker_label: str   # "SPEAKER_00", "SPEAKER_01", ...

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class DiarizationResult:
    segments: list[SpeakerSegment]
    speakers: list[str]                       # labels únicos
    duration_per_speaker: dict[str, float]    # tiempo total por speaker
    total_voice_time: float
    total_audio_dur: float

    @property
    def dominant_speaker(self) -> Optional[str]:
        """Speaker con más tiempo de habla. None si no hay voz."""
        if not self.duration_per_speaker:
            return None
        return max(self.duration_per_speaker.items(), key=lambda kv: kv[1])[0]

    @property
    def n_speakers(self) -> int:
        return len(self.speakers)

    @property
    def dominant_ratio(self) -> float:
        """Fracción del tiempo de voz hablada por el dominante."""
        if self.total_voice_time <= 0:
            return 0.0
        d = self.dominant_speaker
        return self.duration_per_speaker.get(d, 0.0) / self.total_voice_time


_PIPELINE = None


def _get_pipeline():
    """Carga pyannote diarization pipeline. Cacheado entre llamadas."""
    global _PIPELINE
    if _PIPELINE is None:
        from pyannote.audio import Pipeline
        # v4 quitó el requisito de Hugging Face token para modelos públicos.
        try:
            _PIPELINE = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
        except Exception as e:
            logging.error(f"speaker_diarization: no se pudo cargar pipeline: {e}")
            logging.error("Si pide token, exportar HUGGINGFACE_TOKEN o usar `huggingface-cli login`.")
            raise
    return _PIPELINE


def diarize(path: Path, num_speakers: Optional[int] = None,
            min_speakers: int = 1, max_speakers: int = 5) -> Optional[DiarizationResult]:
    """Diariza un audio. Retorna segmentos con etiqueta de speaker.

    Args:
      path: audio (16kHz se recomienda; pyannote resamplea internamente)
      num_speakers: si conoces el número exacto (None = auto-detectar)
      min_speakers / max_speakers: bordes del auto-detect

    Performance: ~1-3x real-time en M-series Apple Silicon (CPU only).
    Para procesos largos, ver lib.audio_resampler.cache_16k para evitar
    resample repetido.
    """
    pipeline = _get_pipeline()
    try:
        if num_speakers is not None:
            diar = pipeline(str(path), num_speakers=num_speakers)
        else:
            diar = pipeline(str(path), min_speakers=min_speakers, max_speakers=max_speakers)
    except Exception as e:
        logging.warning(f"speaker_diarization: falló para {path.name}: {e}")
        return None

    segs: list[SpeakerSegment] = []
    durations: dict[str, float] = {}
    for turn, _, speaker in diar.itertracks(yield_label=True):
        s = float(turn.start)
        e = float(turn.end)
        segs.append(SpeakerSegment(s, e, speaker))
        durations[speaker] = durations.get(speaker, 0.0) + (e - s)

    speakers = sorted(durations.keys())
    total_voice = sum(durations.values())

    # Obtener duración total del audio para calcular ratios
    try:
        import soundfile as sf
        info = sf.info(str(path))
        total_dur = info.frames / info.samplerate
    except Exception:
        total_dur = max((s.end for s in segs), default=0.0)

    return DiarizationResult(
        segments=segs,
        speakers=speakers,
        duration_per_speaker=durations,
        total_voice_time=total_voice,
        total_audio_dur=total_dur,
    )


def classify_audio_role(result: DiarizationResult) -> str:
    """Heurística de etiqueta automática para un audio.

    Returns one of:
      "single-speaker": 1 speaker domina >85% del tiempo de voz.
      "interview-dual": 2 speakers cada uno >25%, suma >80%.
      "interview-multi": 3+ speakers significativos.
      "ambient-or-mixed": no hay speaker dominante claro (<50% del audio
                          es voz, o muchos speakers fragmentados).
    """
    if result.total_voice_time < 5.0:
        return "ambient-or-mixed"
    voice_ratio = result.total_voice_time / max(result.total_audio_dur, 1e-3)
    if voice_ratio < 0.30:
        return "ambient-or-mixed"
    if result.dominant_ratio >= 0.85:
        return "single-speaker"
    # Top-2 speakers
    top2 = sorted(result.duration_per_speaker.values(), reverse=True)[:2]
    if len(top2) >= 2 and (top2[0] + top2[1]) / result.total_voice_time >= 0.80:
        if top2[1] / result.total_voice_time >= 0.25:
            return "interview-dual"
    if result.n_speakers >= 3:
        return "interview-multi"
    return "single-speaker"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python speaker_diarization.py <audio.wav> [audio2 ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        p = Path(arg)
        print(f"\n=== {p.parent.name}/{p.name} ===")
        r = diarize(p)
        if r is None:
            print("  (falló)")
            continue
        print(f"  duración audio: {r.total_audio_dur:.1f}s")
        print(f"  tiempo de voz : {r.total_voice_time:.1f}s "
              f"({100*r.total_voice_time/max(r.total_audio_dur,1):.1f}%)")
        print(f"  speakers      : {r.n_speakers}  ({', '.join(r.speakers)})")
        for sp, dur in sorted(r.duration_per_speaker.items(), key=lambda kv: -kv[1]):
            ratio = dur / max(r.total_voice_time, 1e-3)
            print(f"    {sp}: {dur:6.1f}s ({100*ratio:5.1f}% del habla)")
        print(f"  rol estimado  : {classify_audio_role(r)}")
