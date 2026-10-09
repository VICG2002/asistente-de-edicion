#!/usr/bin/env python3
"""Genera candidatos de `clip_curated_segments` por clip aplicando criterios
curatoriales:

  - Filtra tramos con motion (avg_activity) > 50 → shake extremo.
  - Filtra tramos < MIN_TRAMO_SEC.
  - Clips largos (>120s) NO entrevista/charla → 1 tramo central de 10s con
    is_representative=1 y total_dur_note.
  - Timelapses → 1 tramo central; descripción placeholder.
  - Entrevistas/charla-equipo/accion-dialogo → tramos de content_segments
    (habla densa); fallback a clip_segments.
  - Otros (b-roll uniforme) → tramos de clip_segments filtrados.

Los rows generados quedan con `curated_by='auto'`. La Fase D (yo, Claude)
re-escribe los rows importantes con `curated_by='claude'` agregando los
campos descriptivos (what_action, what_stands, etc.).
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


MIN_TRAMO_SEC = 3.0
MAX_ACTIVITY = 50.0
LONG_CLIP_THRESHOLD = 120.0
SAMPLE_LEN = 10.0           # duración de muestra representativa
TIMELAPSE_SAMPLE_LEN = 10.0


CATEGORIES_FULL_COVERAGE = {
    "charla-equipo", "accion-dialogo"
}

# En entrevista los markers Purple por pregunta ya cubren la información.
# Generar también un tramo Green sería duplicación visual confusa.
CATEGORIES_SKIP = {"entrevista"}


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def get_segments(conn, clip_id):
    """Carga (start, end, activity) de clip_segments + (start, end) de
    content_segments. Devuelve lista deduplicada de candidatos."""
    visual = conn.execute(
        "SELECT start_sec, end_sec, avg_activity FROM clip_segments "
        "WHERE clip_id=? ORDER BY start_sec", (clip_id,)
    ).fetchall()
    content = conn.execute(
        "SELECT start_sec, end_sec FROM content_segments "
        "WHERE clip_id=? ORDER BY start_sec", (clip_id,)
    ).fetchall()
    return visual, content


def midpoint_sample(dur: float, sample_len: float) -> tuple[float, float]:
    """Devuelve (start, end) de una muestra centrada en el clip."""
    center = dur / 2
    half = sample_len / 2
    return max(0.0, center - half), min(dur, center + half)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_curated_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            seg_index INTEGER,
            start_sec REAL,
            end_sec REAL,
            shot_value TEXT,
            angle TEXT,
            characters TEXT,
            what_action TEXT,
            what_stands TEXT,
            where_at TEXT,
            objects TEXT,
            dialogue_idea TEXT,
            full_text TEXT,
            is_representative INTEGER DEFAULT 0,
            total_dur_note TEXT,
            curated_by TEXT DEFAULT 'auto',
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ccs_clip ON clip_curated_segments(clip_id)")

    # Borrar solo los curated_by='auto' — preserva los que yo edite con 'claude'
    conn.execute("DELETE FROM clip_curated_segments WHERE curated_by='auto'")

    rows = conn.execute("""
        SELECT c.id, c.duration_sec,
               IFNULL(d.category, '') AS category,
               IFNULL(sv.shot_value, '') AS shot_value,
               IFNULL(ang.angle, 'Normal') AS angle
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        LEFT JOIN clip_shot_values sv ON sv.clip_id = c.id
        LEFT JOIN clip_angles ang ON ang.clip_id = c.id
        WHERE c.file_kind='video' AND c.index_status='ok'
          AND c.rel_path NOT LIKE '%repetido%'
    """).fetchall()

    now = time.time()
    n_total = 0
    by_kind = {"full": 0, "representative": 0, "timelapse": 0,
               "skipped": 0, "interview_skip": 0}
    for cid, dur, cat, shot, angle in rows:
        dur = dur or 0
        if dur < MIN_TRAMO_SEC:
            by_kind["skipped"] += 1
            continue

        if cat in CATEGORIES_SKIP:
            # Entrevistas: solo Purple por pregunta. Sin Green redundante.
            by_kind["interview_skip"] += 1
            continue

        segs = []

        if cat == "timelapse":
            # Una sola muestra central de 10s. Editor decide si sube/baja.
            s, e = midpoint_sample(dur, TIMELAPSE_SAMPLE_LEN)
            segs.append({
                "s": s, "e": e, "shot": shot, "angle": angle,
                "is_representative": 1,
                "total_dur_note": f"timelapse total {dur:.0f}s; muestra de {e-s:.0f}s",
                "dialogue_idea": ""
            })
            by_kind["timelapse"] += 1

        elif cat in CATEGORIES_FULL_COVERAGE:
            # Cobertura completa: usar content_segments + clip_segments,
            # filtrando los que excedan motion.
            visual, content = get_segments(conn, cid)
            # Preferimos content (habla densa) como base
            base = content if content else [(s, e) for s, e, _ in visual]
            if not base:
                base = [(0, dur)]
            for (s, e) in base:
                if e - s < MIN_TRAMO_SEC:
                    continue
                segs.append({
                    "s": s, "e": e, "shot": shot, "angle": angle,
                    "is_representative": 0, "total_dur_note": "",
                    "dialogue_idea": ""
                })
            by_kind["full"] += 1

        else:
            # B-roll / pov-static / accion: clip largo → muestra. Corto → todo.
            if dur > LONG_CLIP_THRESHOLD:
                # Tomamos el segmento visual más estable + de mejor calidad
                visual, _ = get_segments(conn, cid)
                # filtra extremos
                clean = [(s, e, a) for s, e, a in visual
                         if (a or 0) <= MAX_ACTIVITY and e - s >= MIN_TRAMO_SEC]
                if clean:
                    # Pick the longest clean
                    clean.sort(key=lambda x: -(x[1] - x[0]))
                    s, e, _ = clean[0]
                    # recortar a SAMPLE_LEN centrado
                    if e - s > SAMPLE_LEN:
                        mid = (s + e) / 2
                        s, e = mid - SAMPLE_LEN / 2, mid + SAMPLE_LEN / 2
                else:
                    s, e = midpoint_sample(dur, SAMPLE_LEN)
                segs.append({
                    "s": s, "e": e, "shot": shot, "angle": angle,
                    "is_representative": 1,
                    "total_dur_note": f"clip total {dur:.0f}s; muestra de {e-s:.0f}s",
                    "dialogue_idea": ""
                })
                by_kind["representative"] += 1
            else:
                # Clip corto: 1 segmento = todo el clip (si no es extremo)
                visual, _ = get_segments(conn, cid)
                clean = [(s, e, a) for s, e, a in visual
                         if (a or 0) <= MAX_ACTIVITY and e - s >= MIN_TRAMO_SEC]
                if not clean:
                    # fallback: clip entero
                    clean = [(0, dur, 0)]
                for s, e, _ in clean:
                    segs.append({
                        "s": s, "e": e, "shot": shot, "angle": angle,
                        "is_representative": 0, "total_dur_note": "",
                        "dialogue_idea": ""
                    })
                by_kind["full"] += 1

        # Insert
        for i, sg in enumerate(segs):
            conn.execute(
                "INSERT INTO clip_curated_segments "
                "(clip_id, seg_index, start_sec, end_sec, shot_value, angle, "
                " characters, what_action, what_stands, where_at, objects, "
                " dialogue_idea, full_text, is_representative, total_dur_note, "
                " curated_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, i, sg["s"], sg["e"], sg["shot"], sg["angle"],
                 "", "", "", "", "", sg["dialogue_idea"], "",
                 sg["is_representative"], sg["total_dur_note"],
                 "auto", now)
            )
            n_total += 1

    conn.commit()
    conn.close()
    print(f"Curated segments generados: {n_total}")
    print(f"  cobertura_completa : {by_kind['full']}  (entrevistas/diálogo/cortos)")
    print(f"  representativos    : {by_kind['representative']}  (clips largos)")
    print(f"  timelapses         : {by_kind['timelapse']}")
    print(f"  skipped            : {by_kind['skipped']}")


if __name__ == "__main__":
    main()
