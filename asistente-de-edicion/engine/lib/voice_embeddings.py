"""Voice embeddings con Resemblyzer — identidad por voz cross-audio.

Caso de uso: en Zezzions 2026-05-26 el usuario reportó dos entrevistados con el mismo
nombre de pila (uno, hermano de ENTREVISTADO_11; el otro, el DJ). Identidad por
transcript los confundió. Solución: cada voz produce un embedding 256-D que se puede
clusterar; voces iguales caen cerca en el espacio, distintas caen lejos.

Aplicaciones:
  1. Auto-asignar identidades cross-audio: si voz X aparece en audio A
     etiquetado "ENTREVISTADO_11" y la misma voz aparece en audio B sin etiqueta,
     proponer "ENTREVISTADO_11" para B.
  2. Verificar que el dual-lavalier tenga DOS voces distintas (no la
     misma persona con dos micros).
  3. Detectar voz del entrevistador vs entrevistado.
  4. Match cross-proyecto (la voz de ENTREVISTADO_11 se reconoce en otro proyecto).

Requiere: `pip install resemblyzer soundfile librosa` (en .venv).
Modelo: GE2E pre-entrenado (descargado automáticamente al primer uso, ~17MB).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import logging
import numpy as np


_ENCODER = None


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        from resemblyzer import VoiceEncoder
        _ENCODER = VoiceEncoder(verbose=False)
    return _ENCODER


def load_audio_for_voice(path: Path, target_sr: int = 16000,
                         apply_vad: bool = True) -> np.ndarray | None:
    """Carga audio mono 16kHz. Si apply_vad=True, hace preprocess_wav de
    Resemblyzer que aplica VAD + normalización RMS (recomendado para audios
    con ambiente musical fuerte como Sessions GPI).

    Resemblyzer espera 16kHz mono float32 en [-1, 1].
    """
    try:
        import librosa
        wav, _sr = librosa.load(str(path), sr=target_sr, mono=True)
        if wav.size == 0:
            return None
        wav = wav.astype(np.float32)
        if apply_vad:
            from resemblyzer import preprocess_wav
            wav = preprocess_wav(wav, source_sr=target_sr)
            if wav.size == 0:
                return None
        return wav
    except Exception as e:
        logging.warning(f"voice_embeddings: no se pudo cargar {path.name}: {e}")
        return None


def compute_embedding(path: Path, apply_vad: bool = True) -> np.ndarray | None:
    """Calcula embedding GE2E de un archivo (256-D L2-normalized).

    apply_vad=True (default) preprocesa con VAD + RMS normalize — más robusto
    a ambiente musical. Para audios ya limpios usar apply_vad=False (~3x más
    rápido).
    """
    wav = load_audio_for_voice(path, apply_vad=apply_vad)
    if wav is None:
        return None
    try:
        enc = _get_encoder()
        emb = enc.embed_utterance(wav)
        return emb  # 256-D float32, L2-normalized
    except Exception as e:
        logging.warning(f"voice_embeddings: embedding falló para {path.name}: {e}")
        return None


def compute_embeddings_per_segment(path: Path, segment_dur: float = 4.0,
                                   step: float = 2.0) -> list[tuple[float, float, np.ndarray]]:
    """Devuelve embeddings ventana-deslizante de un audio largo.

    Útil para diarización ad-hoc: cada (start_sec, end_sec, embedding).
    Voces distintas en distintas zonas del audio se separan automáticamente
    al clusterar.
    """
    wav = load_audio_for_voice(path)
    if wav is None:
        return []
    sr = 16000
    out: list[tuple[float, float, np.ndarray]] = []
    enc = _get_encoder()
    total = len(wav) / sr
    t = 0.0
    while t + segment_dur <= total + 0.01:
        s = int(t * sr)
        e = s + int(segment_dur * sr)
        chunk = wav[s:e]
        if len(chunk) < int(segment_dur * sr * 0.5):
            break
        try:
            emb = enc.embed_utterance(chunk)
            out.append((t, t + segment_dur, emb))
        except Exception:
            pass
        t += step
    return out


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Similitud coseno entre dos embeddings (L2-normalized → producto escalar)."""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# Umbrales de decisión (calibrados con GE2E):
# >= 0.80: misma persona, alta confianza
# >= 0.70: misma persona, evidencia razonable
# >= 0.60: posible misma persona, requiere otra señal
# <  0.60: diferentes personas
SAME_PERSON_HIGH = 0.80
SAME_PERSON_MED = 0.70
SAME_PERSON_LOW = 0.60


def match_voice(query_emb: np.ndarray,
                catalog: dict[str, np.ndarray],
                min_sim: float = SAME_PERSON_MED) -> Optional[tuple[str, float]]:
    """Match contra catálogo {nombre: embedding}. Retorna (nombre, sim) si
    el mejor match supera min_sim, sino None.
    """
    if not catalog:
        return None
    best = max(catalog.items(), key=lambda kv: cosine_similarity(query_emb, kv[1]))
    sim = cosine_similarity(query_emb, best[1])
    if sim >= min_sim:
        return best[0], sim
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python voice_embeddings.py <audio1> [audio2 ...]")
        sys.exit(1)
    paths = [Path(p) for p in sys.argv[1:]]
    embs: dict[str, np.ndarray] = {}
    for p in paths:
        e = compute_embedding(p)
        # Etiqueta visible: incluye carpeta para distinguir Dr/Izq con mismo filename
        label = f"{p.parent.name}/{p.name}"
        if e is not None:
            embs[label] = e
            print(f"  ✓ {label}: emb shape={e.shape}, norm={np.linalg.norm(e):.3f}")
        else:
            print(f"  ✗ {label}: falló")
    # Cross-similitudes
    names = list(embs.keys())
    if len(names) >= 2:
        print("\nCross-similitudes:")
        for i in range(len(names)):
            for j in range(i+1, len(names)):
                sim = cosine_similarity(embs[names[i]], embs[names[j]])
                tag = ("misma persona" if sim >= SAME_PERSON_HIGH else
                       "probable misma" if sim >= SAME_PERSON_MED else
                       "posible" if sim >= SAME_PERSON_LOW else
                       "distintas")
                print(f"  {names[i]:>30} ↔ {names[j]:<30} sim={sim:+.3f}  ({tag})")
