#!/usr/bin/env python3
"""For each clip categorized as `entrevista`, parse the transcript to detect
question-answer pairs and emit a `question_segments` row per question.

A question is text between '¿' and '?' (Spanish punctuation always pairs).
The question's start_sec is the timestamp of the first word; end_sec is
either the start of the next question, or the end of the clip.

Output: table question_segments(clip_id, seg_index, start_sec, end_sec,
                                question_text, response_summary).
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.question_filter import split_questions, is_real_question_q  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


# `split_questions` / `is_real_question_q` viven en lib/question_filter.py
# (única fuente de verdad, importadas arriba). Detectan tanto `¿...?` explícitas
# como prompts del entrevistador sin signos (interrogativo acentuado al frente).


def map_chars_to_words(text: str, words: list[tuple[str, float]]) -> list[int]:
    """For each word, return the char position in the joined text where it starts.
    Whisper's text join is approximately ' '.join(words) but may include punctuation
    glued to words. We use a heuristic: search for each word at or after the cursor."""
    positions = []
    cursor = 0
    lower_text = text
    for w, _t in words:
        ww = w.strip()
        if not ww:
            positions.append(cursor)
            continue
        idx = lower_text.find(ww, cursor)
        if idx == -1:
            # try case-insensitive fallback
            idx = lower_text.lower().find(ww.lower(), cursor)
        if idx == -1:
            idx = cursor
        positions.append(idx)
        cursor = idx + len(ww)
    return positions


def find_question_start_time(q_char_start: int, words: list[tuple[str, float]],
                              char_positions: list[int]) -> float | None:
    """First word whose char_pos >= q_char_start."""
    for (w, t), pos in zip(words, char_positions):
        if pos >= q_char_start:
            return t
    return None


def summarize_response(text: str, q_end_char: int, next_q_char: int) -> str:
    """Tomar las primeras 1-2 oraciones de la respuesta para un resumen
    concreto y directo (≤ 200 chars)."""
    chunk = text[q_end_char:next_q_char].strip()
    # Strip leading fillers
    chunk = re.sub(r"^[\s,.\-]*", "", chunk)
    chunk = re.sub(r"^(Eh|Este|Pues|Y|O\s*sea)[\s,.]+", "", chunk, flags=re.IGNORECASE)
    # First 2 sentences. Split on . ! ? followed by space + capital letter.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿])", chunk)
    pick = []
    chars = 0
    for s in sentences:
        if not s.strip():
            continue
        pick.append(s.strip())
        chars += len(s)
        if chars >= 120:
            break
        if len(pick) >= 2:
            break
    out = " ".join(pick)
    if len(out) > 200:
        out = out[:200].rsplit(" ", 1)[0] + "…"
    return out


def process_interview(clip_id: int, transcript_path: Path, clip_dur: float,
                      clean_until_sec: float | None = None):
    """Detecta preguntas de entrevista en un transcript cacheado.

    Si `clean_until_sec` está provisto, solo emite preguntas cuyo
    `start_sec < clean_until_sec` (la zona después es alucinación de Whisper
    por música/ruido — refinado Zezzions 2026-05-26).
    """
    with transcript_path.open() as f:
        tr = json.load(f)
    text = tr.get("text", "").strip()
    # Soportar formato standard [word, t] y master [word, t, src]
    words = []
    for x in tr.get("words", []):
        if isinstance(x, list) and len(x) >= 2:
            words.append((x[0], x[1]))  # ignora src si está
    if not text or not words:
        return []

    questions = split_questions(text)
    if not questions:
        return []

    char_pos = map_chars_to_words(text, words)
    if len(char_pos) != len(words):
        return []

    # Primero generamos los segmentos brutos por pregunta.
    raw = []
    for i, (cs, ce, qtext) in enumerate(questions):
        t_start = find_question_start_time(cs, words, char_pos)
        if t_start is None:
            continue
        # Filtro de zona limpia: si la pregunta cae después de la basura,
        # ignorarla (es alucinación de Whisper sobre música).
        if clean_until_sec is not None and t_start >= clean_until_sec:
            continue
        next_q_char = questions[i + 1][0] if i + 1 < len(questions) else len(text)
        t_end = clip_dur
        if i + 1 < len(questions):
            t_end_candidate = find_question_start_time(questions[i + 1][0], words, char_pos)
            if t_end_candidate is not None:
                t_end = t_end_candidate
        # Truncar t_end a la zona limpia si la pregunta termina en basura.
        if clean_until_sec is not None and t_end > clean_until_sec:
            t_end = clean_until_sec
        if t_end - t_start < 1.0:
            continue
        summary = summarize_response(text, ce, next_q_char)
        raw.append({
            "start_sec": t_start,
            "end_sec":   t_end,
            "question":  qtext,
            "ce":        ce,
            "response_summary": summary,
        })

    # Fusión: cuando dos preguntas están a <5s una de otra, asumimos que la
    # entrevistadora reformuló (o lanzó dos seguidas) y nos quedamos con la
    # que tiene respuesta más larga (más sustantiva).
    GAP_MERGE = 5.0
    merged = []
    for q in raw:
        if merged:
            prev = merged[-1]
            gap = q["start_sec"] - prev["end_sec"]
            # gap pequeño = prev no tuvo respuesta y se reformuló la pregunta
            if gap <= GAP_MERGE and (prev["end_sec"] - prev["start_sec"]) < 8.0:
                # decidir cuál pregunta es la mejor: la más larga literal o la
                # que tendrá respuesta más extensa (q.end - q.start es proxy).
                if len(q["question"]) >= len(prev["question"]):
                    q["start_sec"] = prev["start_sec"]  # extender hacia atrás
                    merged[-1] = q
                else:
                    prev["end_sec"] = q["end_sec"]
                    prev["response_summary"] = q["response_summary"] or prev["response_summary"]
                continue
        merged.append(q)

    out = []
    for i, q in enumerate(merged):
        out.append({
            "seg_index":        i,
            "start_sec":        q["start_sec"],
            "end_sec":          q["end_sec"],
            "question":         q["question"],
            "response_summary": q["response_summary"],
        })
    return out


# `question_short` es la pregunta CURADA que llevan los markers Purple (FCC
# 2026-07-11). Nacio como "columna nueva" agregada a mano en un proyecto y
# nunca entro a este CREATE TABLE: en un manifest NUEVO no existia, el bake caia
# en silencio al texto crudo de Whisper y su candado ("preguntas SIN CURAR")
# tambien se tragaba el error (Asistente, 2026-09-28). Y como este script hace
# DELETE en cada corrida, una curaduria hecha antes de volver a correrlo se perdia.

def asegurar_question_short(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(question_segments)")}
    if "question_short" not in cols:
        conn.execute("ALTER TABLE question_segments ADD COLUMN question_short TEXT")
    # Donde arranca la respuesta, fijado por el curador (ver
    # lib/interview_beats.separar_pregunta_respuesta). Vacio = lo calcula.
    if "answer_sec" not in cols:
        conn.execute("ALTER TABLE question_segments ADD COLUMN answer_sec REAL")


def clips_curados(conn) -> set[int]:
    """Los clips cuya curaduria de preguntas (Capa 2c) ya se cerro.

    Son del curador: el pipeline no los borra ni los vuelve a detectar. El
    DELETE global se llevaba las preguntas que la curaduria AGREGO a mano y
    resucitaba las que DESCARTO en cuanto el pipeline volvia a correr para
    sumar material (Asistente, 2026-09-29, el segundo dia de rodaje). Para
    reabrir un clip, se saca de la tabla.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS question_clips_curados ("
                 "clip_id INTEGER PRIMARY KEY, curado_en TEXT, nota TEXT)")
    return {r[0] for r in conn.execute("SELECT clip_id FROM question_clips_curados")}


def preguntas_curadas(conn) -> dict[tuple[int, int], str]:
    """{(clip_id, segundo de inicio redondeado): question_short} ya curadas."""
    return {(cid, int(round(s or 0))): qs for cid, s, qs in conn.execute(
        "SELECT clip_id, start_sec, question_short FROM question_segments "
        "WHERE COALESCE(TRIM(question_short), '') != ''")}


def restaurar_curadas(conn, curadas: dict[tuple[int, int], str]) -> int:
    n = 0
    for (cid, seg), qs in curadas.items():
        n += conn.execute(
            "UPDATE question_segments SET question_short=? WHERE clip_id=? "
            "AND CAST(ROUND(start_sec) AS INTEGER)=? "
            "AND COALESCE(TRIM(question_short), '')=''", (qs, cid, seg)).rowcount
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS question_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            seg_index INTEGER,
            start_sec REAL,
            end_sec REAL,
            question_text TEXT,
            question_short TEXT,
            response_summary TEXT,
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qseg_clip ON question_segments(clip_id)")
    asegurar_question_short(conn)
    curadas = preguntas_curadas(conn)
    curados = clips_curados(conn)
    conn.execute("DELETE FROM question_segments WHERE clip_id NOT IN "
                 "(SELECT clip_id FROM question_clips_curados)")
    if curados:
        print(f"  {len(curados)} clip(s) con la curaduria cerrada: no se tocan")

    # All clips/audios cuyas categorías indican entrevista (video o audio).
    # `description` puede traer `clean_until=Xs` (analyze_transcript-v3),
    # marcador del rango utilizable cuando el transcript tiene alucinación
    # localizada. Si está, se pasa a `process_interview` para filtrar.
    rows = conn.execute("""
        SELECT d.clip_id, c.duration_sec, c.file_kind, d.description
        FROM clip_descriptions d
        JOIN clips c ON c.id = d.clip_id
        WHERE d.category IN ('entrevista', 'entrevista-audio', 'dialogo-audio',
                             'entrevista-solo-video')
    """).fetchall()

    import re as _re
    _CLEAN_RE = _re.compile(r"clean_until=(\d+)s")

    now = time.time()
    n_video = n_audio = 0
    n_qs = 0
    n_master_used = 0

    # Importar detección por master (iter9.1)
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE.parent))
    from lib.master_question_detect import detect_questions_from_master

    for clip_id, dur, kind, desc in rows:
        if clip_id in curados:
            continue
        master_tp = tr_dir / f"master_{clip_id}.json"
        plain_tp = tr_dir / f"{clip_id}.json"

        # Si hay master Y es video, usar detección por source separado
        # (evita el bug de mezclar palabras del entrevistador y entrevistado).
        if master_tp.exists() and kind == "video":
            with master_tp.open() as f:
                master = json.load(f)
            qs = detect_questions_from_master(master, dur or 0.0)
            if qs:
                n_master_used += 1
            else:
                # Fallback al plain transcript del video
                if plain_tp.exists():
                    qs = process_interview(clip_id, plain_tp, dur or 0.0)
        elif plain_tp.exists():
            clean_until = None
            m = _CLEAN_RE.search(desc or "")
            if m:
                clean_until = float(m.group(1))
            qs = process_interview(clip_id, plain_tp, dur or 0.0, clean_until_sec=clean_until)
        else:
            continue
        if not qs:
            continue
        for q in qs:
            conn.execute(
                "INSERT INTO question_segments(clip_id, seg_index, start_sec, end_sec, "
                "question_text, response_summary, created_at) VALUES (?,?,?,?,?,?,?)",
                (clip_id, q["seg_index"], q["start_sec"], q["end_sec"],
                 q["question"], q["response_summary"], now)
            )
            n_qs += 1
        if kind == "audio":
            n_audio += 1
        else:
            n_video += 1
        print(f"  {kind} clip {clip_id} ({dur:.0f}s): {len(qs)} preguntas")

    print(f"  master transcripts usados: {n_master_used} (combinan A1 + lavaliers)")
    n_rest = restaurar_curadas(conn, curadas)
    if curadas:
        print(f"  curaduria preservada: {n_rest} de {len(curadas)} question_short "
              f"vuelven a su pregunta (clip + segundo de inicio)")
    conn.commit()
    conn.close()
    print(f"\nQuestion segments: {n_video} entrevistas video, "
          f"{n_audio} audios entrevista, {n_qs} preguntas totales")


if __name__ == "__main__":
    main()
