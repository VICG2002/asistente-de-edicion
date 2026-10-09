#!/usr/bin/env python3
"""Asigna identidades del catálogo a TODAS las detecciones (incluyendo las
que quedaron en cluster `noise`), y actualiza el resumen por clip en
`clip_characters` con los nombres canónicos del catálogo.

Para cada detection:
  - Compara su embedding contra cada centroide del catálogo.
  - Si la distancia coseno < THRESH, asigna esa identidad.
  - Inserta en `face_identities` (override si ya existía).

Después, para cada clip, genera:
  - `clip_characters.characters` = "ESCALADOR_A (12), ESCALADORA_B (4)" — nombres + conteo.
  - `clip_characters.context`    = se conserva si ya estaba; sino se completa.
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


COS_THRESHOLD = 0.40   # distancia coseno máxima para identificar (1 - similarity)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def cosine_distance(a, b):
    return 1.0 - float(np.dot(a, b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--threshold", type=float, default=COS_THRESHOLD,
                    help="distancia coseno máxima para identificar")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))

    # Carga catálogo
    cat = conn.execute(
        "SELECT identity_id, canonical_name, centroid FROM face_catalog WHERE centroid IS NOT NULL"
    ).fetchall()
    if not cat:
        sys.exit("Catálogo vacío. Corre build_face_catalog.py --assign primero.")
    identities = []
    for iid, name, cb in cat:
        centroid = np.frombuffer(cb, dtype=np.float32)
        if centroid.size != 512:
            continue
        identities.append((iid, name, centroid))
    print(f"Catálogo: {len(identities)} identidades.")

    # Carga TODAS las detecciones
    rows = conn.execute(
        "SELECT id, clip_id, embedding FROM face_detections WHERE embedding IS NOT NULL"
    ).fetchall()
    print(f"Detecciones a evaluar: {len(rows)}")

    n_assigned = 0
    n_skipped = 0
    by_clip = defaultdict(Counter)
    for det_id, cid, emb_bytes in rows:
        emb = np.frombuffer(emb_bytes, dtype=np.float32)
        if emb.size != 512:
            n_skipped += 1
            continue
        best_iid, best_name, best_dist = None, None, 1e9
        for iid, name, cent in identities:
            d = cosine_distance(emb, cent)
            if d < best_dist:
                best_dist = d
                best_iid = iid
                best_name = name
        if best_dist <= args.threshold:
            conn.execute(
                "INSERT OR REPLACE INTO face_identities (detection_id, identity_id, confidence) "
                "VALUES (?, ?, ?)",
                (det_id, best_iid, 1.0 - best_dist)
            )
            by_clip[cid][best_name] += 1
            n_assigned += 1
        else:
            n_skipped += 1
    conn.commit()
    print(f"Asignadas: {n_assigned}  |  sin identidad: {n_skipped}")

    # Actualiza clip_characters
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_characters (
            clip_id INTEGER PRIMARY KEY,
            characters TEXT,
            context TEXT,
            updated_at REAL
        )
    """)
    now = time.time()
    n_updated = 0
    for cid, counter in by_clip.items():
        chars_str = ", ".join(
            f"{name} ({n})" for name, n in counter.most_common()
        )
        # Conserva context anterior si existe
        ctx_row = conn.execute(
            "SELECT context FROM clip_characters WHERE clip_id=?", (cid,)
        ).fetchone()
        ctx = ctx_row[0] if ctx_row else ""
        conn.execute(
            "INSERT OR REPLACE INTO clip_characters (clip_id, characters, context, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (cid, chars_str, ctx, now)
        )
        n_updated += 1
    conn.commit()
    print(f"clip_characters actualizado en {n_updated} clips.")

    # Resumen
    print("\nTop identidades por nº de clips:")
    by_name = Counter()
    for cid, counter in by_clip.items():
        for name in counter:
            by_name[name] += 1
    for name, n in by_name.most_common():
        print(f"  {name:20s}: {n} clips")

    conn.close()


if __name__ == "__main__":
    main()
