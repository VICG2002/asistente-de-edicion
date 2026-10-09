#!/usr/bin/env python3
"""Ingest de JSONs con descripciones por tramo (Fase D del Plan v11) a la
tabla `clip_curated_segments` con `curated_by='claude'`.

Formato esperado por JSON file (uno por clip o por hoja):
  [
    {
      "clip_id": 329,
      "segments": [
        {
          "start_sec": 22.5,
          "end_sec": 75.1,
          "shot_value": "PM",                       # opcional, sino del clip
          "angle":      "Normal",                   # opcional, sino del clip
          "characters": "ESCALADOR_A, ESCALADORA_B",             # texto libre o nombres canon
          "what_action": "ESCALADOR_A explica el origen de la caravana",
          "what_stands": "ESCALADOR_A gestualizando con las manos al hablar",
          "where_at":   "Base del crag, lluvia detrás",
          "objects":    "casco, cuerdas, agua",
          "dialogue_idea": "Queríamos romper nuestros límites",
          "full_text":  "ESCALADOR_A, ESCALADORA_B | PM + Normal | ESCALADOR_A explica el origen…"
        },
        ...
      ]
    },
    ...
  ]

Reemplaza CUALQUIER `clip_curated_segments` con `curated_by='auto'` para los
clip_ids dados; los `curated_by='claude'` previos se conservan a menos que
el JSON los regenere.
"""

from __future__ import annotations

import argparse
import glob
import json
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


def compose_full_text(seg: dict) -> str:
    """Si no se da full_text explícito, lo compone."""
    if seg.get("full_text"):
        return seg["full_text"]
    chars = seg.get("characters") or "Escena"
    shot = seg.get("shot_value") or ""
    angle = seg.get("angle") or "Normal"
    plano = f"{shot} + {angle}" if shot else angle
    bits = []
    if seg.get("what_action"):
        bits.append(seg["what_action"])
    if seg.get("where_at"):
        bits.append(seg["where_at"])
    if seg.get("objects"):
        bits.append("Objetos: " + seg["objects"])
    if seg.get("dialogue_idea"):
        bits.append('Idea: "' + seg["dialogue_idea"] + '"')
    accion = ". ".join(b for b in bits if b)
    return f"{chars} | {plano} | {accion}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dir", required=True,
                    help="Carpeta con los JSONs (uno por sheet o por clip)")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    src_dir = Path(args.dir).expanduser()
    if not src_dir.exists():
        sys.exit(f"No existe directorio {src_dir}")

    files = sorted(src_dir.glob("*.json"))
    if not files:
        sys.exit(f"No hay JSONs en {src_dir}")

    conn = manifest.conectar(str(db))
    now = time.time()
    n_clips = 0
    n_segs = 0
    for fp in files:
        with fp.open() as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            cid = entry.get("clip_id")
            if cid is None:
                continue
            segs = entry.get("segments", [])
            if not segs:
                continue
            # Borra los 'auto' y 'claude' previos para este clip
            conn.execute(
                "DELETE FROM clip_curated_segments WHERE clip_id=?",
                (cid,)
            )
            for i, seg in enumerate(segs):
                full = compose_full_text(seg)
                conn.execute(
                    "INSERT INTO clip_curated_segments "
                    "(clip_id, seg_index, start_sec, end_sec, shot_value, angle, "
                    " characters, what_action, what_stands, where_at, objects, "
                    " dialogue_idea, full_text, is_representative, total_dur_note, "
                    " curated_by, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, i,
                     float(seg["start_sec"]), float(seg["end_sec"]),
                     seg.get("shot_value", ""), seg.get("angle", "Normal"),
                     seg.get("characters", ""), seg.get("what_action", ""),
                     seg.get("what_stands", ""), seg.get("where_at", ""),
                     seg.get("objects", ""), seg.get("dialogue_idea", ""),
                     full,
                     int(seg.get("is_representative", 0)),
                     seg.get("total_dur_note", ""),
                     "claude", now)
                )
                n_segs += 1
            n_clips += 1

    conn.commit()
    conn.close()
    print(f"Bake-curated: {n_clips} clips re-escritos / {n_segs} tramos.")


if __name__ == "__main__":
    main()
