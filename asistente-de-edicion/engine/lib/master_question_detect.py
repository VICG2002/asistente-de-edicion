"""Detección de preguntas/respuestas a partir del master transcript.

Estrategia (iter9.1, 2026-05-27):
  - NO mezclar palabras de fuentes distintas en un solo texto. Eso crea
    preguntas absurdas (palabras del entrevistador y del entrevistado
    intercaladas).
  - Procesar CADA fuente (A1, lavalier_146, lavalier_155, ...) por separado.
  - Para cada pregunta detectada en una fuente F1 en tiempo t_q, buscar la
    respuesta en OTRA fuente F2 en ventana [t_q, t_q + 60s].
  - Deduplicar preguntas con texto similar en ventana temporal cercana.

Las preguntas del entrevistador suelen detectarse en A1 (cámara).
Las respuestas suelen venir del lavalier del entrevistado.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Reutilizar lógica de detección existente
from bin.derive_question_segments import (
    map_chars_to_words, find_question_start_time, summarize_response,
)
# Filtro de preguntas: única fuente de verdad en lib/question_filter
from lib.question_filter import (  # noqa: F401  (re-export para compat)
    split_questions, is_real_question_q, normalize_question,
    EXTRA_FILLERS, STRONG_INTERROGATIVES_ACCENTED,
    STRONG_INTERROGATIVES_FIRST, STRONG_INTERROGATIVES, INTERVIEW_VERBS,
)


def detect_questions_in_source(text: str, words: list[tuple[str, float]],
                                clip_dur: float) -> list[dict]:
    """Detecta preguntas en una fuente individual usando lógica existente.

    Returns list of:
        {"start_sec": float, "end_sec": float, "question": str,
         "response_summary": str (sólo de la misma fuente)}
    """
    if not text or not words:
        return []
    questions = split_questions(text)
    if not questions:
        return []
    char_pos = map_chars_to_words(text, words)
    if len(char_pos) != len(words):
        return []

    raw = []
    for i, (cs, ce, qtext) in enumerate(questions):
        t_start = find_question_start_time(cs, words, char_pos)
        if t_start is None:
            continue
        next_q_char = questions[i + 1][0] if i + 1 < len(questions) else len(text)
        t_end = clip_dur
        if i + 1 < len(questions):
            t_end_cand = find_question_start_time(questions[i + 1][0], words, char_pos)
            if t_end_cand is not None:
                t_end = t_end_cand
        if t_end - t_start < 1.0:
            continue
        summary = summarize_response(text, ce, next_q_char)
        raw.append({
            "start_sec": t_start,
            "end_sec":   t_end,
            "question":  qtext,
            "response_summary": summary,
        })
    return raw


def find_response_in_other_sources(t_q: float,
                                    by_source: dict[str, dict],
                                    exclude_src: str,
                                    window_sec: float = 60.0,
                                    min_chars: int = 30) -> str:
    """Busca la respuesta a una pregunta detectada en `exclude_src` en t=t_q,
    buscando en otras fuentes palabras en [t_q, t_q + window_sec].
    Retorna texto concatenado de la mejor fuente respondedora.
    """
    best_text = ""
    best_n = 0
    for src, payload in by_source.items():
        if src == exclude_src:
            continue
        words = payload.get("words", [])
        if not words:
            continue
        chunk = []
        for w_tup in words:
            if not isinstance(w_tup, list) or len(w_tup) < 2:
                continue
            w, t = w_tup[0], w_tup[1]
            if t_q <= t <= t_q + window_sec:
                chunk.append(str(w))
        text = " ".join(chunk).strip()
        if len(text) > best_n and len(text) >= min_chars:
            best_n = len(text)
            best_text = text
    # Truncar a ~250 chars (mismo límite que summarize_response)
    if len(best_text) > 250:
        best_text = best_text[:250].rsplit(" ", 1)[0] + "…"
    return best_text


# `normalize_question`, `is_real_question_q` y las constantes léxicas
# (EXTRA_FILLERS, STRONG_INTERROGATIVES_*, INTERVIEW_VERBS) viven ahora en
# lib/question_filter.py (única fuente de verdad) y se importan arriba.


def detect_questions_from_master(master: dict, clip_dur: float,
                                   dedup_window_sec: float = 15.0
                                  ) -> list[dict]:
    """Detecta preguntas+respuestas del master por source separado.

    Algoritmo (iter9.2):
      1. Para cada source, detectar preguntas con su propio texto.
      2. Filtrar preguntas fuera de rango [0, clip_dur].
      3. Filtrar fillers conversacionales ("Qué tal", "Bueno", etc).
      4. Para cada pregunta sin respuesta densa, buscar en OTRA source
         en ventana temporal post-pregunta.
      5. Deduplicar preguntas con texto normalizado igual/similar dentro de
         dedup_window_sec.
      6. Asignar seg_index.

    Returns: list de dicts con start_sec, end_sec, question, response_summary, source
    """
    by_source = master.get("by_source") or {}
    if not by_source:
        return []

    all_questions = []
    for src, payload in by_source.items():
        text = payload.get("text", "")
        words_raw = payload.get("words", [])
        words = [(w[0], w[1]) for w in words_raw if isinstance(w, list) and len(w) >= 2]
        qs = detect_questions_in_source(text, words, clip_dur)
        for q in qs:
            # Filtro temporal: solo preguntas dentro del rango del video
            if q["start_sec"] < 0 or q["start_sec"] > clip_dur:
                continue
            # Filtro de fillers conversacionales
            if not is_real_question_q(q["question"]):
                continue
            # Si su respuesta del mismo source es flaca, complementar con otras
            if len(q["response_summary"]) < 60:
                ext_resp = find_response_in_other_sources(
                    q["start_sec"], by_source, exclude_src=src,
                    window_sec=60.0, min_chars=30
                )
                if ext_resp and len(ext_resp) > len(q["response_summary"]):
                    q["response_summary"] = ext_resp
            q["source"] = src
            all_questions.append(q)

    if not all_questions:
        return []

    # Ordenar por start_sec
    all_questions.sort(key=lambda x: x["start_sec"])

    # Dedup AGRESIVO (iter9.2): ventana variable según tamaño de la pregunta
    #   - Preguntas cortas (≤3 palabras): ventana 15s (puede repetir legítimamente)
    #   - Preguntas largas (≥5 palabras): ventana 90s (probable artefacto multi-source,
    #     misma pregunta capturada en A1+lavaliers con timestamps ligeramente
    #     desfasados, o repetida por el entrevistador a un entrevistado distraído)
    def words_set(q: str) -> set:
        return set(w for w in normalize_question(q).split() if len(w) >= 3)

    # Dedup en pasos: primero ventana corta, luego mejor calidad
    def overlap_ratio(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / min(len(a), len(b))

    deduped = []
    for q in all_questions:
        qw = words_set(q["question"])
        q_norm = normalize_question(q["question"])
        q_size = len(qw)
        # Ventana dinámica por tamaño:
        if q_size <= 3:
            win, threshold = 15.0, 0.70
        elif q_size <= 5:
            win, threshold = 90.0, 0.60
        else:
            win, threshold = 240.0, 0.55
        merged = False
        for prev in deduped:
            prev_norm = normalize_question(prev["question"])
            # CASO 1: pregunta IDÉNTICA normalizada — siempre dedupar, sin ventana.
            # (artefacto frecuente: Whisper alucina "Cuál es el acordeón" cada
            # 16s sobre silencio, o transcribe la misma pregunta en A1+lavalier)
            if q_norm == prev_norm:
                merged = True
                if len(q["response_summary"]) > len(prev["response_summary"]):
                    prev["response_summary"] = q["response_summary"]
                break
            # CASO 2: pregunta similar dentro de ventana
            if abs(prev["start_sec"] - q["start_sec"]) > win:
                continue
            prev_w = words_set(prev["question"])
            ratio = overlap_ratio(qw, prev_w)
            if ratio >= threshold:
                if len(q["response_summary"]) > len(prev["response_summary"]):
                    prev["response_summary"] = q["response_summary"]
                if len(q["question"]) > len(prev["question"]):
                    prev["question"] = q["question"]
                merged = True
                break
        if not merged:
            deduped.append(q)

    # Asignar seg_index
    for i, q in enumerate(deduped):
        q["seg_index"] = i

    return deduped
