#!/usr/bin/env python3
"""Clasifica eventos sonoros (PANNs) en TODOS los audios + videos con audio
del proyecto. Persiste en tabla `sound_events` para uso posterior.

Útil para:
  - sync por evento único (aplauso, riff, bang) cuando otros métodos fallan
  - clasificación automática de audios (música/diálogo/ambient/aplauso)
  - localizar momentos hito en B-roll (rinden timeline markers especiales)

Uso:
    bin/index_sound_events.py --root <disk>
    bin/index_sound_events.py --root <disk> --kind audio    # solo audios
    bin/index_sound_events.py --root <disk> --groups applause,laughter
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sound_events import classify_audio
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            start_sec REAL,
            end_sec REAL,
            label TEXT,
            label_idx INTEGER,
            confidence REAL,
            event_group TEXT,
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_se_clip ON sound_events(clip_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_se_group ON sound_events(event_group)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_se_time ON sound_events(start_sec, end_sec)")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--kind", choices=["audio", "video", "both"], default="both")
    ap.add_argument("--min-confidence", type=float, default=0.20)
    ap.add_argument("--hop-sec", type=float, default=1.0,
                    help="paso entre ventanas de clasificación (segundos)")
    ap.add_argument("--groups", default=None,
                    help="filtrar solo eventos en estos grupos (coma-separado)")
    ap.add_argument("--force", action="store_true",
                    help="re-procesar clips que ya tienen eventos")
    ap.add_argument("--max-dur", type=float, default=600.0,
                    help="duración máxima por clip — clips más largos se "
                         "limitan (ambient de 1h tomaría horas en CPU)")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sound_events_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    ensure_schema(conn)

    if args.kind == "audio":
        kind_clause = "c.file_kind='audio'"
    elif args.kind == "video":
        kind_clause = "c.file_kind='video' AND c.has_audio=1"
    else:
        kind_clause = "(c.file_kind='audio' OR (c.file_kind='video' AND c.has_audio=1))"

    rows = conn.execute(f"""
        SELECT c.id, c.filename, c.path, c.duration_sec
        FROM clips c
        WHERE {kind_clause} AND c.index_status='ok'
          AND c.duration_sec >= 5
        ORDER BY c.duration_sec
    """).fetchall()
    logging.info(f"Candidatos: {len(rows)}")

    if not args.force:
        indexed = set(r[0] for r in conn.execute(
            "SELECT DISTINCT clip_id FROM sound_events").fetchall())
        before = len(rows)
        rows = [r for r in rows if r[0] not in indexed]
        if len(rows) < before:
            logging.info(f"Skipping {before - len(rows)} ya indexados (--force para re-indexar)")

    groups_filter = set(args.groups.split(",")) if args.groups else None

    total_events = 0
    now = time.time()
    for i, (cid, fn, fpath, dur) in enumerate(rows, 1):
        if dur > args.max_dur:
            logging.info(f"[{i}/{len(rows)}] {fn} ({dur:.0f}s) — truncado a {args.max_dur:.0f}s")
        else:
            logging.info(f"[{i}/{len(rows)}] {fn} ({dur:.0f}s)")
        try:
            events = classify_audio(Path(fpath), hop_sec=args.hop_sec,
                                    min_confidence=args.min_confidence)
        except Exception as e:
            logging.warning(f"  fallo: {e}")
            continue
        if groups_filter:
            events = [e for e in events if e.group in groups_filter]
        for ev in events:
            conn.execute(
                "INSERT INTO sound_events "
                "(clip_id, start_sec, end_sec, label, label_idx, confidence, "
                "event_group, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, ev.start, ev.end, ev.label, ev.label_idx,
                 ev.confidence, ev.group, now)
            )
            total_events += 1
        conn.commit()
        if events:
            from collections import Counter
            groups = Counter(e.group for e in events)
            top = ", ".join(f"{g}={n}" for g, n in groups.most_common(4))
            logging.info(f"  {len(events)} eventos: {top}")

    logging.info(f"Total eventos indexados: {total_events}")
    conn.close()


if __name__ == "__main__":
    main()
