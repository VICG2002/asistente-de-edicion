"""Detección de alucinaciones de Whisper en transcripts cacheados.

Caso fundador (Zezzions, 2026-05-25): el audio scratch de cámara Sony FX30
durante entrevistas con música alta llevó a Whisper a producir transcripts
con repetición masiva ('gracias' ×100, 'sí,' ×134, 'no, no, no' ×52) que
inflaron la densidad léxica de los clips y los marcaron como entrevista
falsamente, además de generar preguntas Purple falsas.

**Refinamiento crítico (Zezzions, 2026-05-25, 2do pass):** la alucinación
suele estar localizada (ej. al final del clip cuando la música tapa todo y
Whisper empieza a inventar). El transcript anterior, **antes** de la ráfaga
de alucinación, es contenido editorial real ("Me llamo ENTREVISTADO_4...", "Te late
si empezamos con tu nombre, lo que haces..."). Descartar el transcript
ENTERO por una ráfaga al final perdió 8+ entrevistas reales en Zezzions.

Por eso `analyze_transcript()` ahora devuelve un dict con:
- `is_hallucinated`: True solo si > `garbage_ratio_threshold` del transcript
  es basura (default 50%)
- `clean_until_idx`: índice de palabra donde empieza la primera ráfaga de
  alucinación (las palabras 0..clean_until_idx son CONFIABLES; downstream
  puede usar solo esas).
- `clean_until_sec`: timestamp en segundos donde se corta la zona limpia.
- `reasons`: lista de razones (frases típicas, n-gramas, palabra dominante).

API legacy:
- `is_hallucinated(text, words)` devuelve `(bool, reason)` por compat —
  usa el threshold relajado (descarta solo si > 50% basura).
"""

from __future__ import annotations

from collections import Counter

# Frases que Whisper produce en silencio o ruido (no asociadas a habla real).
# Lista creciente conforme se descubren patrones nuevos.
HALLUCINATION_PHRASES = (
    "gracias",            # filler clásico en silencio
    "suscríbete",         # outro de YouTube
    "amara.org",          # créditos de Amara
    "subtítulos",         # créditos
    "subscribe",          # YouTube outro
    "thank you",          # alucinación en inglés
    "thanks for watching",
    "by sdi media",       # créditos de doblaje
    "transcripción",      # créditos
)

# Tokens permitidos a alta repetición sin disparar el detector — son palabras
# muy frecuentes en habla casual mexicana. Se examinan en `is_hallucinated`.
_FREQ_OK = {"y", "que", "de", "la", "el", "no", "es", "se", "lo", "a", "en"}


def analyze_transcript(text: str, words: list, *,
                       min_phrase_hits: int = 8,
                       min_ngram_hits: int = 8,
                       min_word_dominance: float = 0.30,
                       min_word_dominance_count: int = 30,
                       garbage_ratio_threshold: float = 0.50,
                       window_size: int = 50) -> dict:
    """Analiza un transcript y devuelve datos sobre alucinaciones LOCALIZADAS.

    En lugar de descartar el transcript entero, detecta dónde empieza la
    primera ráfaga de basura y devuelve cuánto del transcript es confiable.

    Returns dict con:
      - is_hallucinated: True si > garbage_ratio_threshold del transcript es basura
      - clean_until_idx: índice de palabra donde empieza la basura (None si limpio)
      - clean_until_sec: timestamp donde empieza la basura (None si limpio)
      - garbage_ratio: fracción 0..1 del transcript afectado
      - reasons: list[str] razones detectadas
      - n_words: total de palabras
    """
    out = {"is_hallucinated": False, "clean_until_idx": None,
           "clean_until_sec": None, "garbage_ratio": 0.0, "reasons": [],
           "n_words": len(words) if words else 0}
    if not text or not words:
        return out
    total = len(words)
    wlist = [
        (w[0].lower().strip(".,;:!?¿¡() ") if isinstance(w, list) and w else "")
        for w in words
    ]
    timestamps = [
        (w[1] if isinstance(w, list) and len(w) > 1 else 0.0)
        for w in words
    ]

    # 1. Marcar cada palabra como tóxica o limpia.
    #    Ventana deslizante de `window_size`: si la ventana es alucinatoria
    #    (palabra dominante >= 40% O n-grama-3 ≥ 5×), TODAS sus palabras
    #    cuentan como tóxicas. Una ráfaga aislada no contamina el resto.
    toxic_idx = [False] * total
    step = max(1, window_size // 2)
    for start in range(0, max(1, total - window_size + 1), step):
        win = wlist[start:start + window_size]
        is_toxic = False
        c = Counter(w for w in win if w)
        if c:
            most, n_most = c.most_common(1)[0]
            if most and most not in _FREQ_OK and n_most / len(win) >= 0.40:
                is_toxic = True
        if not is_toxic and len(win) >= 3:
            ng3 = Counter(tuple(win[i:i+3]) for i in range(len(win) - 2))
            if ng3:
                _, c3 = ng3.most_common(1)[0]
                if c3 >= 5:
                    is_toxic = True
        if is_toxic:
            for i in range(start, min(start + window_size, total)):
                toxic_idx[i] = True

    # 2. Estadísticas: cuántas palabras tóxicas vs limpias en TOTAL.
    toxic_count = sum(toxic_idx)
    out["garbage_ratio"] = toxic_count / total if total else 0.0
    # clean_until_idx = primer índice tóxico (sugiere downstream conservador
    # que use solo palabras 0..clean_until_idx). Pero la ZONA LIMPIA puede ser
    # más amplia (regiones limpias intercaladas) — para eso ver `clean_words`.
    first_toxic = next((i for i, t in enumerate(toxic_idx) if t), total)
    out["clean_until_idx"] = first_toxic
    out["clean_until_sec"] = (
        timestamps[first_toxic] if first_toxic < len(timestamps) else
        (timestamps[-1] if timestamps else None)
    )
    out["clean_words"] = total - toxic_count
    out["toxic_words"] = toxic_count

    # 3. Razones globales (para reporte)
    low = text.lower()
    for phrase in HALLUCINATION_PHRASES:
        c = low.count(phrase)
        if c >= min_phrase_hits:
            out["reasons"].append(f"frase '{phrase}' ×{c}")
    counter = Counter(w for w in wlist if w)
    if counter:
        most, n_most = counter.most_common(1)[0]
        if (most not in _FREQ_OK
                and n_most >= min_word_dominance_count
                and n_most / total >= min_word_dominance):
            out["reasons"].append(f"palabra '{most}' ×{n_most} ({100*n_most/total:.0f}%)")
    for n in (3, 4):
        if len(wlist) < n:
            continue
        ng_count = Counter(tuple(wlist[i:i+n]) for i in range(len(wlist) - n + 1))
        if ng_count:
            ng, c = ng_count.most_common(1)[0]
            if c >= min_ngram_hits:
                out["reasons"].append(f"n-grama-{n} '{' '.join(ng)}' ×{c}")

    # 4. Veredicto. Descartar SOLO si:
    #    - garbage_ratio supera el threshold (default 50%)
    #    - Y la zona limpia no tiene contenido editorial sustancial
    #      (< 100 palabras limpias O < 30s de habla limpia).
    #    Si hay contenido limpio sustancial Y la basura está localizada
    #    (incluso > 50%), mantener el transcript como utilizable; downstream
    #    usa `clean_until_idx` para limitar el rango de búsqueda de preguntas.
    enough_clean_words = out["clean_words"] >= 100
    enough_clean_sec = (out["clean_until_sec"] or 0) >= 30
    out["is_hallucinated"] = (
        out["garbage_ratio"] >= garbage_ratio_threshold
        and not (enough_clean_words and enough_clean_sec)
    )
    return out


def is_hallucinated(text: str, words: list, **kwargs) -> tuple[bool, str]:
    """API legacy: returns (bool, reason).

    `bool` True solo si > 50% del transcript es basura (no por una ráfaga
    aislada). Para diagnóstico fino usar `analyze_transcript()`.
    """
    info = analyze_transcript(text, words, **kwargs)
    reason = "; ".join(info["reasons"]) if info["reasons"] else ""
    if info["is_hallucinated"] and info["garbage_ratio"] > 0:
        reason = f"basura {100*info['garbage_ratio']:.0f}% del transcript; " + reason
    return info["is_hallucinated"], reason


def hallucination_summary(transcripts_dir, clip_ids):
    """Resumen por lote — devuelve dict {clip_id: (is_garbage, reason)}.

    Args:
        transcripts_dir: pathlib.Path al directorio con <clip_id>.json files.
        clip_ids: iterable de ints (clip ids a chequear).
    """
    import json
    from pathlib import Path
    out = {}
    base = Path(transcripts_dir)
    for cid in clip_ids:
        p = base / f"{cid}.json"
        if not p.exists():
            out[cid] = (False, "(sin transcript)")
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            out[cid] = (True, f"json inválido: {e}")
            continue
        out[cid] = is_hallucinated(d.get("text", ""), d.get("words", []) or [])
    return out
