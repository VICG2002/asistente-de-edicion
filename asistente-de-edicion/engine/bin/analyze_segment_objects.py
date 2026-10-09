#!/usr/bin/env python3
"""Corre YOLOv8 sobre los frames de cada tramo, identifica objetos y
guarda en `segment_objects(seg_id, frame_pct, label, conf, bbox)`.

Usa el modelo `yolov8n.pt` (nano, ~6MB) por default — rápido en CPU y
suficiente para categorías comunes (persona, perro, vehículo, cuerda,
etc.). Para más detalle, swap a `yolov8s.pt` o `yolov8m.pt`.

El resultado se agrega como contexto auxiliar al prompt del LLM (no
reemplaza la descripción, solo enriquece).
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
import warnings
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

warnings.filterwarnings("ignore")


MIN_CONF = 0.35


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
    ap.add_argument("--model", default="yolov8n.pt",
                    help="yolov8n.pt (nano) / yolov8s.pt / yolov8m.pt")
    ap.add_argument("--max-segs", type=int, default=0)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    frames_dir = root / ".cinema_assistant" / "seg_frames"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    print("Cargando YOLO…")
    from ultralytics import YOLO
    model = YOLO(args.model)

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS segment_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seg_id INTEGER NOT NULL,
            frame_pct INTEGER,
            label TEXT,
            conf REAL,
            bbox TEXT,
            created_at REAL,
            FOREIGN KEY (seg_id) REFERENCES clip_curated_segments(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_so_seg ON segment_objects(seg_id)")

    seg_ids = [r[0] for r in conn.execute(
        "SELECT id FROM clip_curated_segments ORDER BY id"
    )]
    if args.max_segs > 0:
        seg_ids = seg_ids[: args.max_segs]

    # Skip los que ya tienen objetos
    done_ids = set(r[0] for r in conn.execute(
        "SELECT DISTINCT seg_id FROM segment_objects"
    ))
    seg_ids = [s for s in seg_ids if s not in done_ids]
    print(f"Tramos por procesar: {len(seg_ids)}")

    now = time.time()
    n_ok = 0
    n_objs = 0
    n_sin_pct = 0
    for seg_id in seg_ids:
        seg_dir = frames_dir / str(seg_id)
        if not seg_dir.exists():
            continue
        frames = sorted(seg_dir.glob("*.jpg"))
        if not frames:
            continue
        for fp in frames:
            try:
                pct = int(fp.stem)
            except ValueError:
                # `frame_pct` es un entero por contrato. Un frame con otro
                # nombre NO se procesa — pero antes se saltaba en silencio y el
                # resumen final decia que todo habia ido bien. Ahora se cuenta
                # y se dice: un tramo entero puede quedarse sin objetos por un
                # cambio de nombrado y nadie enterarse.
                n_sin_pct += 1
                continue
            results = model.predict(str(fp), verbose=False, conf=MIN_CONF)
            if not results:
                continue
            for r in results:
                names = r.names
                for box in r.boxes:
                    label = names[int(box.cls.item())]
                    conf = float(box.conf.item())
                    xyxy = box.xyxy[0].tolist()
                    bbox_str = ",".join(f"{v:.0f}" for v in xyxy)
                    conn.execute(
                        "INSERT INTO segment_objects (seg_id, frame_pct, label, "
                        "conf, bbox, created_at) VALUES (?,?,?,?,?,?)",
                        (seg_id, pct, label, conf, bbox_str, now)
                    )
                    n_objs += 1
        conn.commit()
        n_ok += 1
        if n_ok % 50 == 0:
            print(f"  [{n_ok}] tramos | {n_objs} objetos acumulados")

    conn.close()
    print(f"\nDone. Tramos procesados: {n_ok}  |  objetos detectados: {n_objs}")
    if n_sin_pct:
        print(f"AVISO: {n_sin_pct} frames saltados por nombre no numerico. "
              f"`frame_pct` es un entero por contrato; revisa con que motor se "
              f"extrajeron (bin/extract_segment_frames.py).")


if __name__ == "__main__":
    main()
