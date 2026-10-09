#!/usr/bin/env python3
"""Asigna ángulo de cámara a cada clip de video.

Estándar cine-doc (español):
  Normal      — paralelo al suelo, altura ojos. Default.
  Picado      — desde arriba apuntando hacia abajo.
  Contrapicado — desde abajo apuntando hacia arriba.
  Cenital     — 90° desde arriba (drone/topo).
  Nadir       — 90° desde abajo apuntando al cielo.
  Holandés    — inclinación lateral 5-45° (tilted horizon).
  Escorzo     — over-the-shoulder, detrás del hombro de un personaje.
  POV         — sustituye los ojos del personaje (subjetiva).

Heurística inicial:
  - drone → Cenital
  - gopro + motion ≥ 15 → POV
  - gopro + motion < 15 → POV (estática)
  - resto → Normal

Override JSON: {clip_id: angle}.
"""

from __future__ import annotations

import argparse
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


VALID_ANGLES = {
    "Normal", "Picado", "Contrapicado", "Cenital", "Nadir",
    "Holandés", "Escorzo", "POV"
}


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


# La clasificacion de camara vive en `lib/cameras.py` + `config/camera_profiles.json`.
# Esta copia local no conocia VICG, FX30 ni a6700: todo ese material recibia
# angulo "Normal" por defecto en vez del que le toca.


def heuristic(camera: str, motion: float) -> str:
    if camera == "drone":
        return "Cenital"
    if camera == "gopro":
        return "POV"
    return "Normal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--overrides", help="JSON {clip_id: angle_name}")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    overrides = {}
    if args.overrides:
        with open(args.overrides) as f:
            raw = json.load(f)
        for k, v in raw.items():
            if v not in VALID_ANGLES:
                sys.exit(f"Ángulo inválido en overrides: {v}")
            overrides[int(k)] = v

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_angles (
            clip_id INTEGER PRIMARY KEY,
            angle TEXT,
            source TEXT,
            updated_at REAL
        )
    """)
    conn.execute("DELETE FROM clip_angles")

    rows = conn.execute("""
        SELECT c.id, c.filename, c.camera_model, IFNULL(a.motion_score_mean, 0),
               c.camera_make, c.ext
        FROM clips c
        LEFT JOIN clip_analysis a ON a.clip_id = c.id
        WHERE c.file_kind = 'video' AND c.index_status = 'ok'
    """).fetchall()

    reg = cameras.load_profiles(root)
    now = time.time()
    n = 0
    by_angle = {}
    for cid, fn, model, motion, make, ext in rows:
        if cid in overrides:
            angle = overrides[cid]
            source = "manual"
        else:
            rol = cameras.role_of(fn, model, make, ext, registry=reg)
            angle = heuristic(rol, motion)
            source = "heuristic"
        conn.execute(
            "INSERT INTO clip_angles (clip_id, angle, source, updated_at) "
            "VALUES (?,?,?,?)", (cid, angle, source, now)
        )
        n += 1
        by_angle[angle] = by_angle.get(angle, 0) + 1

    conn.commit()
    conn.close()
    print(f"Ángulos asignados a {n} clips.")
    for angle in sorted(VALID_ANGLES):
        if angle in by_angle:
            print(f"  {angle:13s}: {by_angle[angle]}")


if __name__ == "__main__":
    main()
