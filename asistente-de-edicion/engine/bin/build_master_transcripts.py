#!/usr/bin/env python3
"""Construir transcripts maestros A1+lavaliers para clips de entrevista.

Para cada clip de video con sync de lavalier(s), genera
`transcripts/master_{video_id}.json` que combina:
  - Palabras del A1 de cámara (donde se escucha mejor al entrevistador)
  - Palabras de lavaliers sincronizados (donde se escucha mejor al entrevistado)

Todas las palabras se ponen en escala del VIDEO (video_t=0 al inicio).
Duplicados (misma palabra en A1 + lavalier dentro de 0.5s) se resuelven a
favor del lavalier (mejor SNR).

Doctrina: `~/memoria-asistente-edicion/lecciones/patrones-exitosos.md`
sección "Transcript maestro Dr+Izq+A1 para detectar mejor preguntas".

Uso:
    bin/build_master_transcripts.py --root <disk>
    bin/build_master_transcripts.py --root <disk> --only-interviews
    bin/build_master_transcripts.py --root <disk> --videos 2571,2671
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

from lib.master_transcript import build_master_transcript, save_master_transcript
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--only-interviews", action="store_true",
                    help="Solo videos categorizados como 'entrevista*'")
    ap.add_argument("--videos", help="CSV substring(s) de filenames del video")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"master_transcripts_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Videos elegibles: con sync_pair (al menos un lavalier sincronizado)
    where = "WHERE EXISTS (SELECT 1 FROM audio_sync_pairs sp WHERE sp.video_clip_id=v.id)"
    params: list = []
    if args.only_interviews:
        where += (" AND EXISTS (SELECT 1 FROM clip_descriptions d "
                  "WHERE d.clip_id=v.id AND d.category LIKE 'entrevista%')")
    if args.videos:
        subs = [s.strip() for s in args.videos.split(",") if s.strip()]
        like_clauses = " OR ".join(["v.filename LIKE ?" for _ in subs])
        where += f" AND ({like_clauses})"
        params.extend([f"%{s}%" for s in subs])

    videos = conn.execute(f"""
        SELECT v.id, v.filename FROM clips v
        {where}
        ORDER BY v.filename
    """, params).fetchall()
    logging.info(f"Videos a procesar: {len(videos)}")

    n_ok, n_skipped = 0, 0
    for vid, vfn in videos:
        master = build_master_transcript(conn, tr_dir, vid)
        if master is None:
            logging.info(f"  ⊘ {vfn}: sin transcripts (A1 ni lavaliers)")
            n_skipped += 1
            continue
        if args.dry_run:
            logging.info(f"  [DRY] {vfn}: master={master['n_words_total']} words, "
                         f"sources={master['sources']}")
            continue
        out = save_master_transcript(tr_dir, vid, master)
        n_ok += 1
        logging.info(f"  ✓ {vfn}: {master['n_words_total']} words "
                     f"({master['n_sync_audios']} lavalier sync) → {out.name}")

    logging.info(f"\n✅ Maestros creados: {n_ok}, skipped: {n_skipped}")
    conn.close()


if __name__ == "__main__":
    main()
