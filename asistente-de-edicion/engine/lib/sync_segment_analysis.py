"""Análisis físico de sync por segmentos del transcript.

Funciones reusables extraídas de bin/verify_sync_physical.py para usarse en
el pipeline completo (sync_pipeline_full.py, build_lavalier_pairs.py,
sync_review.py).

Doctrina: ver `~/memoria-asistente-edicion/metodologia/sync-sin-ground-truth.md`

Convención del offset:
    offset = audio_start_world - video_start_world
    audio_t = video_t - offset
    En frame 0 del video, audio_t = -offset. Si offset<0, audio empezó antes.

    Ejemplo numérico, porque las dos líneas de arriba se leen igual de bien al
    revés y así fue como se propagó el error: offset = -5.0 significa que la
    grabadora arrancó 5 s antes que la cámara, así que el frame 0 del video cae
    en el segundo 5 del audio.

    CUIDADO AL "CORREGIR" ESTO. La conversión inversa sí lleva `+`:

        video_t = audio_t + offset

    Las dos son ciertas y se parecen mucho, que es por lo que el error se
    propaga. `bin/detect_pauses.py:278` usa la inversa y está BIEN.

    OJO (2026-08-13): hasta hoy la segunda línea decía `+ offset` y la tercera
    `audio_t = offset`. El CÓDIGO de este módulo siempre estuvo bien —línea 134
    hace `offset = -median(shift)` y la 107 `expected_t_a = t_v - expected`—,
    pero el docstring es la referencia que leyeron otros módulos, y en
    `lib/sync_refiner` y `lib/identity_fusion` se programó el `+` en la
    dirección que no toca. Este comentario es el parche de la raíz.

Métrica clave para "shift":
    Para cada palabra física que ocurre en ambos transcripts,
    shift = t_audio - t_video.
    offset = -shift (porque shift = -(audio_start - video_start) = -offset).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np

# ============================================================================
# I/O
# ============================================================================

def load_words(tr_dir: Path, clip_id: int) -> list[tuple[str, float]]:
    """Carga transcripts/{clip_id}.json y devuelve [(word_lower_clean, t_start)].
    Maneja formato lista [word, time] que es lo que produce el motor."""
    p = tr_dir / f"{clip_id}.json"
    if not p.exists():
        return []
    try:
        d = json.load(p.open())
    except Exception:
        return []
    out = []
    for w in d.get("words", []) or []:
        if isinstance(w, list) and len(w) >= 2:
            tx = str(w[0]).lower().strip(".,?¿!¡;:'\"-")
            try:
                t = float(w[1])
            except (TypeError, ValueError):
                continue
            if tx:
                out.append((tx, t))
    return out


# ============================================================================
# N-grama anchors
# ============================================================================

def to_ngrams(words: list[tuple[str, float]], n: int = 3,
              tmin: Optional[float] = None,
              tmax: Optional[float] = None) -> list[tuple[str, float]]:
    """Construye n-gramas (texto, timestamp_mid)."""
    out = []
    for i in range(len(words) - n + 1):
        text = " ".join(w[0] for w in words[i:i+n])
        t_mid = words[i + n//2][1]
        if tmin is not None and t_mid < tmin:
            continue
        if tmax is not None and t_mid > tmax:
            continue
        out.append((text, t_mid))
    return out


def compute_offset(words_v: list, words_a: list, expected: Optional[float] = None,
                   window: float = 3.0, n_gram: int = 3,
                   min_anchors: int = 20) -> Optional[dict]:
    """Calcula offset físico entre video y audio por ngram-alignment.

    Args:
        words_v: words del video.
        words_a: words del audio.
        expected: si dado, filtra anchors cuyo shift sea |shift - expected_shift| > window.
                  expected_shift = -expected (porque offset = -shift).
        window: ventana ±sec para filtrar outliers.
        n_gram: tamaño de n-grama (default 3).
        min_anchors: mínimo de anchors filtrados para devolver resultado.

    Returns:
        None si insuficiente, o dict:
            { 'offset': float, 'n_anchors': int, 'mad': float, 'raw_shifts': list }
    """
    if not words_v or not words_a:
        return None
    ng_v = to_ngrams(words_v, n_gram)
    ng_a = to_ngrams(words_a, n_gram)
    if not ng_v or not ng_a:
        return None
    amap: dict[str, list[float]] = {}
    for text, t in ng_a:
        amap.setdefault(text, []).append(t)

    shifts = []
    for text, t_v in ng_v:
        if text in amap:
            if expected is not None:
                expected_t_a = t_v - expected  # at world time of t_v, audio_t = t_v + offset = t_v - (-offset) … offset = -expected_shift, expected_shift = -expected, expected_t_a = t_v + expected_shift = t_v + (-expected)
                # Re-derivation: shift = t_a - t_v = -offset. expected_shift = -expected_offset = -expected.
                # expected_t_a = t_v + expected_shift = t_v + (-expected) = t_v - expected
                cands = [t for t in amap[text] if abs(t - expected_t_a) < window]
                if not cands:
                    continue
                t_a = min(cands, key=lambda t: abs(t - expected_t_a))
            else:
                if len(amap[text]) != 1:
                    continue
                t_a = amap[text][0]
            shifts.append(t_a - t_v)

    if len(shifts) < min_anchors:
        return None

    arr = np.array(shifts)
    median_shift = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median_shift)))
    if mad > 0:
        keep = arr[(arr > median_shift - 3*mad) & (arr < median_shift + 3*mad)]
    else:
        keep = arr
    if len(keep) < max(5, min_anchors // 4):
        return None

    return {
        "offset": -float(np.median(keep)),
        "n_anchors": int(len(keep)),
        "mad": mad,
        "raw_n_shifts": int(len(shifts)),
    }


# ============================================================================
# Análisis por segmentos
# ============================================================================

def segment_offsets(words_v: list, words_a: list, expected: float,
                    seg_sec: float = 60.0, n_gram: int = 3,
                    window: float = 3.0, min_anchors_per_seg: int = 5) -> list[dict]:
    """Divide el video en segmentos de seg_sec y calcula offset en cada uno.

    Returns: lista de dicts: {t_start, t_end, offset, n_anchors, mad}
    """
    if not words_v or not words_a:
        return []
    v_end = words_v[-1][1]
    results = []
    t0 = 0.0
    while t0 < v_end:
        t1 = t0 + seg_sec
        # Subset del video en [t0, t1]
        v_subset = [w for w in words_v if t0 <= w[1] < t1]
        if len(v_subset) >= n_gram + 2:
            r = compute_offset(v_subset, words_a, expected=expected,
                               window=window, n_gram=n_gram,
                               min_anchors=min_anchors_per_seg)
            if r is not None:
                results.append({
                    "t_start": t0, "t_end": t1,
                    "offset": r["offset"], "n_anchors": r["n_anchors"],
                    "mad": r["mad"],
                })
        t0 = t1
    return results


# ============================================================================
# Multi-take detection
# ============================================================================

def multitake_score(words_v: list, words_a: list, n: int = 4) -> tuple[float, int]:
    """Detecta multi-take: % de n-gramas del video que aparecen >1 vez en audio.

    Returns (score [0..1], n_grams_common).
    Umbral 0.10 = multi-take detectado.
    """
    if not words_v or not words_a:
        return 0.0, 0
    v_ngrams = set(t for t, _ in to_ngrams(words_v, n))
    a_count = Counter(t for t, _ in to_ngrams(words_a, n))
    multi = 0
    total = 0
    for ng in v_ngrams:
        if ng in a_count:
            total += 1
            if a_count[ng] > 1:
                multi += 1
    if total == 0:
        return 0.0, 0
    return multi / total, total


# ============================================================================
# 4-gram overlap (para detección de hermanos)
# ============================================================================

def fourgram_overlap(text_a: str, text_b: str, n: int = 4) -> float:
    """% de 4-gramas de A que aparecen en B. Símbolo asimétrico (use simétrico
    si quieres: max(overlap(a,b), overlap(b,a)) o promedio).
    """
    def to_words(t):
        return [w.lower().strip(".,?¿!¡;:'\"-") for w in t.split() if w.strip()]
    wa = to_words(text_a)
    wb = to_words(text_b)
    if len(wa) < n or len(wb) < n:
        return 0.0
    ngrams_a = set(" ".join(wa[i:i+n]) for i in range(len(wa) - n + 1))
    ngrams_b = set(" ".join(wb[i:i+n]) for i in range(len(wb) - n + 1))
    if not ngrams_a or not ngrams_b:
        return 0.0
    return len(ngrams_a & ngrams_b) / len(ngrams_a)


# ============================================================================
# Clasificación
# ============================================================================

# Categorías canónicas
STATUS_PRIORITY = {
    "multitake":         0,
    "drift_severe":      1,
    "bias_refinable":    2,
    "bias_uncertain":    3,
    "drift_partial":     4,
    "no_data":           5,
    "minor_bias":        6,
    "insufficient":      7,
    "ok":                8,
}

STATUS_LABEL = {
    "multitake":      "🎬 MULTI-TAKE",
    "drift_severe":   "🚨 DRIFT SEVERE",
    "bias_refinable": "🔧 REFINABLE",
    "bias_uncertain": "⚠ BIAS DUDOSO",
    "drift_partial":  "⚠ DRIFT-LEVE",
    "no_data":        "  no-data",
    "minor_bias":     "  minor",
    "insufficient":   "  insuficiente",
    "ok":             "✓ OK",
}


def classify_pair(seg_offsets: list, bd_off: Optional[float],
                  multi_score: float = 0.0,
                  bias_threshold: float = 0.150,
                  mad_threshold_refinable: float = 0.15,
                  mad_threshold_drift: float = 0.30,
                  min_anchors_refinable: int = 100) -> tuple[str, dict]:
    """Clasifica un sync pair según consistencia de segmentos + multi-take.

    Args:
        seg_offsets: salida de segment_offsets()
        bd_off: offset en BD (puede ser None si es candidato nuevo)
        multi_score: salida de multitake_score()

    Returns: (status_key, info_dict)
    """
    info = {"multitake_score": round(multi_score, 3)}

    if not seg_offsets:
        info["reason"] = "sin anchors suficientes en transcript"
        return "no_data", info

    n_segs = len(seg_offsets)
    if n_segs < 2:
        info.update({"n_segs": 1, "seg_offset": seg_offsets[0]["offset"]})
        info["reason"] = "solo 1 segmento con anchors"
        return "insufficient", info

    offsets = np.array([s["offset"] for s in seg_offsets])
    ns = np.array([s["n_anchors"] for s in seg_offsets])
    median_global = float(np.median(offsets))
    mad_segments = float(np.median(np.abs(offsets - median_global)))
    total_n = int(ns.sum())

    info.update({
        "n_segs": n_segs,
        "median_offset": round(median_global, 3),
        "mad_segments": round(mad_segments, 3),
        "total_anchors": total_n,
        "seg_range": (round(float(offsets.min()), 3), round(float(offsets.max()), 3)),
    })

    if bd_off is not None:
        bias = median_global - bd_off
        info["bias_vs_bd"] = round(bias, 3)

    # 1. Multi-take primero (más específico)
    if multi_score > 0.10:
        info["reason"] = (f"Multi-take: {multi_score:.0%} de n-gramas del video "
                          f"aparecen >1 vez en audio. Sync único NO aplica.")
        return "multitake", info

    # 2. Drift severo
    if mad_segments > 0.50:
        info["reason"] = (f"MAD entre segmentos = {mad_segments:.3f}s — drift severo "
                          f"o audio equivocado")
        return "drift_severe", info

    # 3. Drift parcial
    if mad_segments > mad_threshold_drift:
        info["reason"] = f"MAD = {mad_segments:.3f}s — drift sutil"
        return "drift_partial", info

    # 4. Bias check (solo si bd_off dado)
    if bd_off is None:
        info["reason"] = f"Sync nuevo: offset físico={median_global:+.3f}, MAD={mad_segments:.3f}"
        # Si MAD bajo, refinable; sino dudoso
        if mad_segments < mad_threshold_refinable and total_n >= min_anchors_refinable:
            return "bias_refinable", info
        return "bias_uncertain", info

    bias = info["bias_vs_bd"]
    if abs(bias) > bias_threshold:
        if mad_segments < mad_threshold_refinable and total_n >= min_anchors_refinable:
            info["reason"] = (f"Bias confiable = {bias:+.3f}s, "
                              f"MAD={mad_segments:.3f}, n={total_n} — refinement seguro")
            info["refine_safe"] = True
            return "bias_refinable", info
        info["reason"] = (f"Bias = {bias:+.3f}s pero MAD={mad_segments:.3f} — "
                          f"refinement con cautela")
        return "bias_uncertain", info

    if abs(bias) > 0.050:
        info["reason"] = f"Bias leve = {bias:+.3f}s"
        return "minor_bias", info

    info["reason"] = f"OK: bias={bias:+.3f}s, MAD={mad_segments:.3f}"
    return "ok", info


# ============================================================================
# Validación temporal (audio_mtime debe contener video.creation_time)
# ============================================================================

def temporal_validity(audio_mtime_unix: float, audio_dur_sec: float,
                       video_creation_unix: float,
                       slack_sec: float = 60.0) -> tuple[bool, str]:
    """Verifica que el video.creation_time caiga dentro de la ventana
    de grabación del audio (audio_mtime - audio_dur, audio_mtime + slack).

    Returns (is_valid, reason).
    """
    if not audio_mtime_unix or not video_creation_unix:
        return True, "sin timestamps disponibles (skip check)"
    audio_start = audio_mtime_unix - audio_dur_sec
    audio_end = audio_mtime_unix + slack_sec
    if audio_start - slack_sec <= video_creation_unix <= audio_end:
        return True, "OK"
    delta = min(abs(video_creation_unix - audio_start),
                abs(video_creation_unix - audio_mtime_unix))
    return False, (f"video.creation_time fuera de ventana audio "
                   f"[{audio_start:.0f}, {audio_end:.0f}] — Δ={delta:.0f}s")
