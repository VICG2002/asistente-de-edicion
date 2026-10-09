#!/usr/bin/env python3
"""Derive content segments + descriptions for external audio recordings (WAV).

Same lexical-density logic as derive_content_segments.py, but applied to
`file_kind == 'audio'` clips so the editor can see what each recording
contains without opening it.

Also generates a short description per audio (first ~120 chars of the
transcript or first sentence) and stores it into clip_descriptions so the
audio appears in the Lua bake with markers.
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
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


WINDOW_SEC = 60.0
STEP_SEC = 10.0
MIN_DENSITY = 6.0           # audios son más sueltos que cámara fija — bajar
GAP_TOLERANCE = 20.0
MIN_SEG_LEN = 10.0


def normalize_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def derive_segments(words: list[tuple[str, float]], clip_dur: float) -> list[dict]:
    if not words or clip_dur <= 0:
        return []
    norm_words = [(normalize_word(w), t) for w, t in words]
    norm_words = [(w, t) for w, t in norm_words if w]
    if not norm_words:
        return []

    starts = []
    t = 0.0
    while t < clip_dur:
        ws = t
        we = min(t + WINDOW_SEC, clip_dur)
        bucket = {w for w, wt in norm_words if ws <= wt < we}
        density = len(bucket) / ((we - ws) / 60.0) if we > ws else 0.0
        starts.append((ws, we, density, len(bucket)))
        t += STEP_SEC

    raw = []
    in_seg = False
    seg_start = 0.0
    seg_distinct = 0
    last_above = 0.0
    for ws, we, dens, n in starts:
        if dens >= MIN_DENSITY:
            if not in_seg:
                seg_start = ws
                seg_distinct = n
                in_seg = True
            else:
                seg_distinct = max(seg_distinct, n)
            last_above = we
        elif in_seg and (ws - last_above) >= GAP_TOLERANCE:
            raw.append((seg_start, last_above, seg_distinct))
            in_seg = False
    if in_seg:
        raw.append((seg_start, last_above, seg_distinct))

    merged = []
    for s, e, n in raw:
        if merged and s - merged[-1][1] <= GAP_TOLERANCE:
            ps, pe, pn = merged[-1]
            merged[-1] = (ps, e, max(pn, n))
        else:
            merged.append((s, e, n))

    out = []
    for s, e, n in merged:
        if e - s < MIN_SEG_LEN:
            continue
        out.append({"start": s, "end": e, "distinct": n})
    return out


# Etiquetas heurísticas de contenido de audio basadas en palabras clave.
def classify_audio(text: str, distinct: int) -> tuple[str, str]:
    """Devuelve (category, subject_hint) para usar en clip_descriptions."""
    low = (text or "").lower()
    if not distinct:
        return ("ambiente", "Ambiente / sin habla")
    if distinct < 20:
        return ("ambiente", "Ambiente con poca voz")
    if any(k in low for k in ("entrevista", "encadene", "lujuria",
                              "caravana", "13 años", "hermano")):
        return ("entrevista-audio", "Audio de entrevista")
    if any(k in low for k in ("venga", "vamos", "pegue")):
        return ("accion-audio", "Audio de pegue / beta-calling")
    if any(k in low for k in ("cumpleaños", "campamento", "desayuno",
                              "corroncho", "no joda", "chela")):
        return ("charla-audio", "Charla de campamento / convivencia")
    return ("dialogo-audio", "Audio con diálogo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-distinct", type=int, default=15,
                    help="omit audios with very few distinct words")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))

    # Tabla compartida con video — la usamos también para audio.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            seg_index INTEGER,
            start_sec REAL,
            end_sec REAL,
            distinct_words INTEGER,
            density REAL,
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    # Borrar segmentos previos SOLO de audios para no pisar los de video.
    audio_ids = [r[0] for r in conn.execute(
        "SELECT id FROM clips WHERE file_kind='audio' AND index_status='ok' "
        "AND filename NOT GLOB '*_[Tt][Rr][0-9]*'"
    )]
    if audio_ids:
        marks = ",".join("?" * len(audio_ids))
        conn.execute(f"DELETE FROM content_segments WHERE clip_id IN ({marks})", audio_ids)

    # Tabla de descripciones también — registrar entrada por audio.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_descriptions (
            clip_id INTEGER PRIMARY KEY,
            category TEXT,
            description TEXT,
            source TEXT,
            updated_at REAL
        )
    """)

    now = time.time()
    n_processed = 0
    n_segs = 0
    n_with_desc = 0

    SHOT_NAMES_AUDIO = "Audio externo"  # no aplica plano

    rows = conn.execute(
        "SELECT id, filename, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND filename NOT GLOB '*_[Tt][Rr][0-9]*'"
    ).fetchall()

    for clip_id, fn, dur in rows:
        tp = tr_dir / f"{clip_id}.json"
        if not tp.exists():
            continue
        try:
            with tp.open() as f:
                tr = json.load(f)
        except Exception:
            continue
        words = [tuple(x) for x in tr.get("words", [])]
        text = tr.get("text", "") or ""
        distinct_total = len({normalize_word(w) for w, _ in words} - {""})

        if distinct_total < args.min_distinct:
            # aun así registramos descripción mínima (ambiente)
            cat, subj = classify_audio(text, distinct_total)
            dur_safe = dur if dur is not None else 0.0
            desc = f"{subj} | {SHOT_NAMES_AUDIO} | Sin habla relevante " \
                   f"(distinct={distinct_total}, dur={dur_safe:.0f}s)"
            conn.execute(
                "INSERT OR REPLACE INTO clip_descriptions(clip_id, category, "
                "description, source, updated_at) VALUES (?,?,?,?,?)",
                (clip_id, cat, desc, "derive_audio_segments.py", now)
            )
            n_with_desc += 1
            continue

        segs = derive_segments(words, dur or 0.0)
        for i, s in enumerate(segs):
            density = s["distinct"] / ((s["end"] - s["start"]) / 60.0) if s["end"] > s["start"] else 0.0
            conn.execute(
                "INSERT INTO content_segments(clip_id, seg_index, start_sec, end_sec, "
                "distinct_words, density, created_at) VALUES (?,?,?,?,?,?,?)",
                (clip_id, i, s["start"], s["end"], s["distinct"], density, now)
            )
            n_segs += 1

        # Descripción del audio: SUJETO | Audio externo | extracto del transcript
        cat, subj = classify_audio(text, distinct_total)
        snippet = re.sub(r"\s+", " ", text).strip()
        if len(snippet) > 160:
            snippet = snippet[:160].rsplit(" ", 1)[0] + "…"
        desc = f"{subj} | {SHOT_NAMES_AUDIO} | {snippet}"
        conn.execute(
            "INSERT OR REPLACE INTO clip_descriptions(clip_id, category, "
            "description, source, updated_at) VALUES (?,?,?,?,?)",
            (clip_id, cat, desc, "derive_audio_segments.py", now)
        )
        n_with_desc += 1
        n_processed += 1

    conn.commit()
    conn.close()
    print(f"Audios procesados: {n_processed} con habla, {n_with_desc} con descripción.")
    print(f"Segmentos de contenido en audios: {n_segs}")


if __name__ == "__main__":
    main()
