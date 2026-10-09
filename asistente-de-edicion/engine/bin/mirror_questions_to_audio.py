#!/usr/bin/env python3
"""Replica las question_segments de cada clip de VIDEO entrevista hacia su
WAV externo sincronizado, aplicando el offset.

Convención del proyecto:
    offset = audio_start - video_start    (negativo = audio empezó antes)

Mapeo de tiempo:
    t_audio = t_video - offset

Las preguntas mirroreadas tienen `seg_index >= 1000` para distinguirlas de
las del propio audio. El audio puede tener sus propias preguntas detectadas
del transcript (índices 0..N) y las del video sincronizado (1000..1000+N).
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
import time
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-confidence", type=float, default=0.3,
                    help="solo mirrorear sync pairs por encima de este conf")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    # El espejo lleva tambien la pregunta CURADA (question_short). Sin esto el
    # marker del audio salia con el garble de Whisper aunque la del video ya
    # estuviera curada (leccion 48, Fantastico Comics; hasta hoy se arreglaba
    # con un UPDATE a mano despues de cada corrida).
    cols_q = {r[1] for r in conn.execute("PRAGMA table_info(question_segments)")}
    con_curada = "question_short" in cols_q
    col_curada = "question_short" if con_curada else "NULL"
    # Y el arranque CURADO de la respuesta, llevado al tiempo del WAV.
    col_answer = "answer_sec" if "answer_sec" in cols_q else "NULL"
    # Borrar mirrors previos (seg_index >= 1000)
    conn.execute("DELETE FROM question_segments WHERE seg_index >= 1000")

    # Cargar pares sync
    pairs = conn.execute("""
        SELECT video_clip_id, audio_clip_id, offset_sec, confidence
        FROM audio_sync_pairs
        WHERE confidence >= ?
    """, (args.min_confidence,)).fetchall()
    print(f"Sync pairs evaluados: {len(pairs)}")

    n_mirrored = 0
    now = time.time()
    for vid, aid, offset, conf in pairs:
        # Preguntas del video
        vqs = conn.execute(
            f"SELECT seg_index, start_sec, end_sec, question_text, response_summary, "
            f"{col_curada}, {col_answer} FROM question_segments WHERE clip_id=? AND seg_index < 1000 "
            "ORDER BY seg_index",
            (vid,)
        ).fetchall()
        if not vqs:
            continue
        # Duración del audio para clamp
        adur_row = conn.execute(
            "SELECT duration_sec FROM clips WHERE id=?", (aid,)
        ).fetchone()
        adur = adur_row[0] if adur_row and adur_row[0] else 1e9

        for vidx, vs, ve, qtext, summary, qshort, ans in vqs:
            ts = vs - (offset or 0)
            te = ve - (offset or 0)
            # clamp al rango del audio
            ts = max(0.0, ts)
            te = min(adur, te)
            if te <= ts:
                continue
            if con_curada:
                cur = conn.execute(
                    "INSERT INTO question_segments(clip_id, seg_index, start_sec, end_sec, "
                    "question_text, question_short, response_summary, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (aid, 1000 + vidx, ts, te, f"[del video sync] {qtext}",
                     qshort, summary, now))
                if ans is not None and col_answer != "NULL":
                    conn.execute("UPDATE question_segments SET answer_sec=? WHERE id=?",
                                 (ans - (offset or 0), cur.lastrowid))
            else:
                conn.execute(
                    "INSERT INTO question_segments(clip_id, seg_index, start_sec, end_sec, "
                    "question_text, response_summary, created_at) VALUES (?,?,?,?,?,?,?)",
                    (aid, 1000 + vidx, ts, te, f"[del video sync] {qtext}",
                     summary, now))
            n_mirrored += 1

    conn.commit()
    conn.close()
    print(f"Preguntas mirroreadas a audios: {n_mirrored}")


if __name__ == "__main__":
    main()
