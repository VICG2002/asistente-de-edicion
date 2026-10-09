#!/usr/bin/env python3
"""Assign a shot value to each video clip using the project's standard
shot-scale taxonomy (Spanish abbreviations).

Shot scale (cine-doc standard, español — obligatorio desde 2026-05-23):

  GPG — Gran Plano General (gran escenario, sujeto casi invisible)
  PG  — Plano General (sujeto en su entorno, ubica la acción)
  PE  — Plano Entero (cabeza a pies)
  PA  — Plano Americano / Tres Cuartos (corta por rodillas)
  PM  — Plano Medio (corta por cintura, entrevistas/diálogos)
  PP  — Primer Plano (hombros a cabeza, valor emocional)
  PPP — Primerísimo Primer Plano (barbilla a frente, máxima emoción)
  PD  — Plano Detalle (objeto o parte específica)

Heuristic mapping (project: JILOTEPEC piloto):
  drone                                       → GPG
  timelapse (motion < 2, dur > 60)            → PG
  gopro POV (motion < 10, dur > 60)           → PG
  gopro POV (motion ≥ 10)                     → PA
  entrevista (main camera, low motion)        → PM
  charla-equipo (main, motion < 10)           → PM
  accion-dialogo (main, motion high)          → PA
  b-roll (main, motion ≥ 20)                  → PA
  b-roll (main, motion 10-20)                 → PG
  b-roll (main, motion < 10, dur > 60)        → PG
  b-roll (main, dur < 10)                     → PP  (cut-in candidate)
  discard / export                            → (none)

Marker-duration multiplier by shot value (for the Resolve script):
  PD         →  1.00  (use the whole segment — detail is the point)
  PPP        →  1.00
  PP         →  0.90
  PM         →  0.85
  PA         →  0.75
  PE         →  0.70
  PG         →  0.50
  GPG        →  0.40

Manual overrides JSON: {clip_id: shot_value_code}.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras  # noqa: E402
from lib import manifest  # noqa: E402


# Spanish shot-scale taxonomy (obligatorio desde 2026-05-23).
SHOT_DURATION_MULT = {
    "GPG": 0.40,
    "PG":  0.50,
    "PE":  0.70,
    "PA":  0.75,
    "PM":  0.85,
    "PP":  0.90,
    "PPP": 1.00,
    "PD":  1.00,
}

# Human-readable names for use in markers (label/notes).
SHOT_NAMES = {
    "GPG": "Gran Plano General",
    "PG":  "Plano General",
    "PE":  "Plano Entero",
    "PA":  "Plano Americano",
    "PM":  "Plano Medio",
    "PP":  "Primer Plano",
    "PPP": "Primerísimo Primer Plano",
    "PD":  "Plano Detalle",
}


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def heuristic(camera, category, motion, dur) -> str | None:
    motion = motion or 0.0
    dur = dur or 0.0

    if camera == "drone":
        return "GPG"
    if category == "timelapse":
        return "PG"
    if category == "export" or category == "discard":
        return None
    if camera == "gopro":
        if motion < 10 and dur > 60:
            return "PG"
        return "PA"
    if category == "entrevista":
        return "PM"
    if category == "charla-equipo":
        return "PM"
    if category in ("accion-dialogo", "accion"):
        return "PA"
    if camera == "main":
        if motion >= 20:
            return "PA"
        if motion >= 10:
            return "PG"
        if dur > 60:
            return "PG"
        if dur < 10:
            return "PP"
        return "PM"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--overrides", help="Optional JSON file: {clip_id: shot_value}")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    overrides = {}
    if args.overrides:
        with open(args.overrides) as f:
            raw = json.load(f)
        overrides = {int(k): v for k, v in raw.items()}

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_shot_values (
            clip_id INTEGER PRIMARY KEY,
            shot_value TEXT,
            source TEXT,    -- 'heuristic' | 'manual'
            multiplier REAL,
            updated_at REAL
        )
    """)
    conn.execute("DELETE FROM clip_shot_values")

    rows = conn.execute("""
        SELECT c.id, c.filename, c.duration_sec, c.camera_model,
               a.motion_score_mean, d.category, c.camera_make, c.ext
        FROM clips c
        LEFT JOIN clip_analysis a ON a.clip_id = c.id
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        WHERE c.file_kind = 'video' AND c.index_status = 'ok'
    """).fetchall()

    # La clasificacion de camara sale del registro (lib/cameras.py). Esta copia
    # local decia "same as export_lua_data.py" pero ya no lo era: le faltaban
    # VICG, ILME-FX30 y ILCE-6700.
    reg = cameras.load_profiles(root)

    now = time.time()
    n = 0
    by_shot = {}
    for clip_id, fn, dur, model, motion, cat, make, ext in rows:
        camera = cameras.role_of(fn, model, make, ext, registry=reg)
        sv = overrides.get(clip_id)
        source = "manual" if sv else "heuristic"
        if not sv:
            sv = heuristic(camera, cat or "", motion, dur)
        if not sv:
            continue
        mult = SHOT_DURATION_MULT.get(sv, 0.85)
        conn.execute(
            "INSERT INTO clip_shot_values(clip_id, shot_value, source, multiplier, updated_at) "
            "VALUES (?,?,?,?,?)",
            (clip_id, sv, source, mult, now)
        )
        n += 1
        by_shot[sv] = by_shot.get(sv, 0) + 1

    conn.commit()
    conn.close()
    print(f"Shot values asignados a {n} clips")
    for sv in ("GPG", "PG", "PE", "PA", "PM", "PP", "PPP", "PD"):
        if sv in by_shot:
            name = SHOT_NAMES.get(sv, sv)
            print(f"  {sv:3s} {name:30s} ({SHOT_DURATION_MULT.get(sv,0.85):.2f}x): {by_shot[sv]}")


if __name__ == "__main__":
    main()
