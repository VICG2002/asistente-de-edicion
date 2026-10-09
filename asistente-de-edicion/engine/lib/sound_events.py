"""Clasificación de eventos sonoros con PANNs (entrenado en AudioSet 527 clases).

Caso de uso (Zezzions 2026-05-26): los lavaliers fallaron en varias
entrevistas. Si el sync por transcript/envelope/chromaprint da bajo
match, los EVENTOS ÚNICOS son anclas confiables — un aplauso visible
en el video tiene un aplauso audible en el audio en el mismo instante
(módulo el offset). Sin importar la voz/música ambient.

Eventos útiles para sync (de AudioSet):
  - Applause (137)
  - Cheering (102, 73)
  - Laughter (16-22)
  - Whistling (52)
  - Music transitions (riff/drop específico)
  - Sudden silence

Modelo: Cnn14_DecisionLevelMax (PANNs) — pre-entrenado en AudioSet.
Pesa ~80 MB, corre ~5x real-time en M-series Apple Silicon CPU.

Requiere: `pip install panns_inference` (en .venv).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class SoundEvent:
    start: float           # segundos
    end: float
    label: str             # nombre AudioSet (ej "Applause")
    label_idx: int         # índice en AudioSet (0..527)
    confidence: float      # [0..1]
    group: str             # categoría agrupada: "applause", "laughter", etc.


# Mapeo grueso de labels de AudioSet a grupos útiles para edición documental.
# Indices son aproximados del archivo class_labels_indices.csv de AudioSet.
# El mapeo correcto se carga desde panns_inference si está disponible.
GROUP_BY_LABEL = {
    # Aplauso / cheering
    "Applause": "applause",
    "Cheering": "applause",
    "Chatter": "chatter",
    "Crowd": "chatter",
    # Risa
    "Laughter": "laughter",
    "Giggle": "laughter",
    "Chuckle, chortle": "laughter",
    "Belly laugh": "laughter",
    # Llanto — candidato a momento emocional en entrevista (v0.2.0).
    # Es SENAL, no verdad: el marker dice "candidato" y el editor confirma.
    "Crying, sobbing": "crying",
    "Sobbing": "crying",
    "Whimper": "crying",
    "Wail, moan": "crying",
    # Voz / habla
    "Speech": "speech",
    "Conversation": "speech",
    "Narration, monologue": "speech",
    # Música
    "Music": "music",
    "Singing": "singing",
    "Choir": "singing",
    "Musical instrument": "music",
    "Guitar": "music",
    "Drums": "music",
    "Drum kit": "music",
    "Bass drum": "music",
    "Percussion": "music",
    "Electronic music": "music",
    "Synthesizer": "music",
    "Piano": "music",
    "Keyboard (musical)": "music",
    # Whistling / silbidos
    "Whistle": "whistle",
    "Whistling": "whistle",
    # Eventos transientes únicos (útiles como sync anchors)
    "Bang": "bang",
    "Clap": "applause",
    "Snap": "bang",
    "Slam": "bang",
    "Smash, crash": "bang",
    "Breaking": "bang",
    # Ambient
    "Silence": "silence",
    "Background noise": "ambient",
    "Wind": "ambient",
    "Rain": "ambient",
}


_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from panns_inference import AudioTagging
        # checkpoint_path=None → descarga modelo (~80 MB primera vez)
        _MODEL = AudioTagging(checkpoint_path=None, device="cpu")
    return _MODEL


def classify_audio(path: Path, hop_sec: float = 1.0,
                   min_confidence: float = 0.20) -> list[SoundEvent]:
    """Clasifica un audio en eventos sonoros sliding-window.

    Cada ventana de `hop_sec`*2 segundos se clasifica en las 527 clases.
    Eventos consecutivos del mismo grupo se mergean.
    """
    import librosa
    sr = 32000  # PANNs default
    try:
        wav, _ = librosa.load(str(path), sr=sr, mono=True)
    except Exception as e:
        logging.error(f"sound_events: no se pudo cargar {path.name}: {e}")
        return []
    if wav.size == 0:
        return []

    model = _get_model()
    window_samples = int(2.0 * sr)   # ventana 2s
    hop_samples = int(hop_sec * sr)

    events_raw: list[SoundEvent] = []
    t = 0
    label_names = None
    while t + window_samples <= len(wav):
        chunk = wav[t:t + window_samples].astype(np.float32)
        chunk_batch = chunk[np.newaxis, :]
        clipwise_output, _ = model.inference(chunk_batch)
        scores = clipwise_output[0]  # 527-D
        if label_names is None:
            label_names = model.labels if hasattr(model, "labels") else _load_labels()
        # Top-K eventos por encima del threshold
        top_idx = np.argsort(-scores)[:5]
        for idx in top_idx:
            conf = float(scores[idx])
            if conf < min_confidence:
                continue
            label = label_names[idx] if label_names else f"class_{idx}"
            group = GROUP_BY_LABEL.get(label, "other")
            if group == "other":
                continue   # solo guardar grupos relevantes
            t_start = t / sr
            t_end = (t + window_samples) / sr
            events_raw.append(SoundEvent(
                start=t_start, end=t_end,
                label=label, label_idx=int(idx),
                confidence=conf, group=group
            ))
        t += hop_samples

    # Merge: ventanas consecutivas del mismo grupo se juntan
    events_raw.sort(key=lambda e: (e.start, e.group))
    merged: list[SoundEvent] = []
    for ev in events_raw:
        if (merged and merged[-1].group == ev.group
                and ev.start <= merged[-1].end + 0.2):
            # extender
            merged[-1].end = max(merged[-1].end, ev.end)
            merged[-1].confidence = max(merged[-1].confidence, ev.confidence)
        else:
            merged.append(ev)
    return merged


def _load_labels() -> list[str] | None:
    """Fallback: lee class_labels_indices.csv de panns_inference si existe."""
    try:
        import panns_inference, os, csv
        pkg_dir = Path(panns_inference.__file__).parent
        for p in pkg_dir.rglob("class_labels_indices.csv"):
            with p.open() as f:
                reader = csv.DictReader(f)
                return [r["display_name"] for r in reader]
    except Exception:
        return None


def find_unique_events(events: list[SoundEvent],
                       group: str,
                       min_separation: float = 5.0) -> list[SoundEvent]:
    """Filtra eventos de un grupo con suficiente separación temporal entre sí.
    Útil para sync: queremos eventos discretos identificables, no continuos.
    """
    out: list[SoundEvent] = []
    for ev in events:
        if ev.group != group:
            continue
        if out and ev.start - out[-1].end < min_separation:
            continue
        out.append(ev)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python sound_events.py <audio.wav>")
        sys.exit(1)
    for arg in sys.argv[1:]:
        p = Path(arg)
        print(f"\n=== {p.parent.name}/{p.name} ===")
        events = classify_audio(p, hop_sec=1.0, min_confidence=0.20)
        print(f"  total eventos: {len(events)}")
        from collections import Counter
        groups = Counter(e.group for e in events)
        for g, n in groups.most_common():
            print(f"  {g}: {n} eventos")
        print("\n  Primeros 8 eventos:")
        for ev in events[:8]:
            print(f"    [{ev.start:7.1f}-{ev.end:7.1f}] {ev.group:<12} {ev.label:<30} conf={ev.confidence:.2f}")
        # Eventos discretos útiles para sync:
        print("\n  Eventos de aplauso (anchors potenciales):")
        for ev in find_unique_events(events, "applause"):
            print(f"    [{ev.start:7.1f}-{ev.end:7.1f}] {ev.label:<25} conf={ev.confidence:.2f}")
