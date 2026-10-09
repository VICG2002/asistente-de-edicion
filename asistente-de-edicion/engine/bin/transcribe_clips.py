#!/usr/bin/env python3
"""Transcribe the clips of a location, caching word-level transcripts.

Scales transcription beyond the sample: selects clips by location + camera class
from the manifest, transcribes each with whisper.cpp, and caches the transcript
under .cinema_assistant/transcripts/<clip_id>.json (reused on re-runs).
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib import cameras
from lib import guards
from lib import transcribe as tr
from lib import manifest  # noqa: E402

# Que camaras llevan dialogo / audio scratch que valga la pena transcribir:
# ya NO se decide aqui. Lo dice el registro `config/camera_profiles.json` via
# `cameras.main_cam_sql()`. Antes esta constante era una de cinco listas que se
# desincronizaron entre si (la a6700 solo estaba aqui — leccion 50).


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
    ap.add_argument("--model", required=True)
    ap.add_argument("--location", default="",
                    help="rel_path substring, lowercased. Default '' = TODO el "
                         "proyecto (el default histórico 'jilo' era un hardcode "
                         "Jilotepec que dejaba 0 clips en otros proyectos — "
                         "detectado en Film Club Café 2026-07-09).")
    ap.add_argument("--cameras", choices=["main", "all"], default="main")
    ap.add_argument("--lang", default="es")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db = ws / "manifest.sqlite"
    cache = ws / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)
    log_file = ws / "logs" / f"transcribe_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    sql = ("SELECT id, path, filename FROM clips WHERE file_kind='video' "
           "AND index_status='ok' AND has_audio=1 AND lower(rel_path) LIKE ? "
           "AND lower(rel_path) NOT LIKE '%repetido%' "
           # v0.2.0: BRAW/R3D no los abre ffmpeg. Se saltan DICIENDOLO
           # (guards.reportar_no_decodables), nunca en silencio.
           f"AND {guards.SQL_DECODABLE}")
    if args.cameras == "main":
        sql += " AND " + cameras.main_cam_sql(disk_root=root)
    sql += " ORDER BY creation_time, filename"
    rows = conn.execute(sql, (f"%{args.location.lower()}%",)).fetchall()
    guards.reportar_no_decodables(conn, "transcribe_clips",
                                  "lower(rel_path) LIKE ?",
                                  (f"%{args.location.lower()}%",))
    conn.close()
    logging.info(f"Clips a transcribir: {len(rows)} (location~{args.location}, "
                 f"cameras={args.cameras})")

    done, cached, failed = 0, 0, 0
    t0 = time.time()
    # Leccion 43: NUNCA dos Whisper en paralelo, comparten la GPU Metal y el
    # mas largo se arrastra sin limite. El lock hace la regla estructural, no
    # una promesa: si transcribe_audios.py ya lo tiene, esto aborta al tomarlo.
    with guards.lock_exclusivo("transcribe_clips"):
        for i, (cid, path, fn) in enumerate(rows, 1):
            cf = cache / f"{cid}.json"
            if cf.exists():
                cached += 1
            else:
                r = tr.transcribe(path, args.model, lang=args.lang)
                if r and r.get("words"):
                    cf.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
                    done += 1
                else:
                    failed += 1
            if i % 10 == 0:
                logging.info(f"  {i}/{len(rows)}  nuevos={done} cache={cached} "
                             f"fail={failed}  ({(time.time() - t0) / 60:.1f}min)")
    logging.info(f"Listo. {done} transcritos, {cached} ya en cache, {failed} fallos, "
                 f"{(time.time() - t0) / 60:.1f}min.")


if __name__ == "__main__":
    main()
