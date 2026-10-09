#!/usr/bin/env python3
"""Test: transcript-based sync on a sample of interview clips.

Transcribes the sample clips and the external recordings (caching every
transcript under .cinema_assistant/transcripts/<clip_id>.json), then aligns each
clip to its recording by transcript content. Prints pairings, offsets and
confidence so the method can be judged before running it project-wide.
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

from lib import transcribe as tr
from lib import transcript_sync as ts
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def cached_transcript(clip_id, path, model, cache_dir, lang="es"):
    """Transcribe once, cache to <clip_id>.json, reuse thereafter."""
    cf = cache_dir / f"{clip_id}.json"
    if cf.exists():
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
            if d and d.get("words"):
                return d
        except (json.JSONDecodeError, OSError):
            pass
    r = tr.transcribe(path, model, lang=lang)
    if r:
        cf.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--clips", nargs="+", required=True, help="clip filenames")
    ap.add_argument("--audio-like", default="%audios%jilo%")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db = ws / "manifest.sqlite"
    cache = ws / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)
    log_file = ws / "logs" / f"transcript_test_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    clip_rows = []
    for fn in args.clips:
        row = conn.execute(
            "SELECT id, path, filename FROM clips WHERE filename=? AND file_kind='video' "
            "AND lower(rel_path) LIKE 'escalando mexico/%jilo%' LIMIT 1", (fn,)).fetchone()
        if row:
            clip_rows.append(row)
        else:
            logging.info(f"clip no encontrado: {fn}")
    rec_rows = conn.execute(
        "SELECT id, path, filename FROM clips WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND duration_sec >= 60 ORDER BY filename",
        (args.audio_like,)).fetchall()
    conn.close()
    logging.info(f"Clips de prueba: {len(clip_rows)}  |  grabaciones: {len(rec_rows)}")

    t0 = time.time()
    clip_tr = {}
    for cid, path, fn in clip_rows:
        logging.info(f"Transcribiendo clip {fn} ...")
        t = cached_transcript(cid, path, args.model, cache)
        if t and t.get("words"):
            clip_tr[cid] = (fn, t)
            logging.info(f"  {fn}: {len(t['words'])} palabras")
            logging.info(f"  TEXTO: {t['text'][:300]}")
        else:
            logging.info(f"  {fn}: FALLO")

    rec_tr = []
    for i, (rid, path, fn) in enumerate(rec_rows, 1):
        t = cached_transcript(rid, path, args.model, cache)
        if t and t.get("words"):
            rec_tr.append((rid, fn, t))
        if i % 5 == 0:
            logging.info(f"  grabaciones {i}/{len(rec_rows)}  ({(time.time()-t0)/60:.1f}min)")
    logging.info(f"Grabaciones con transcript: {len(rec_tr)}/{len(rec_rows)}")

    logging.info("=== SYNC POR TRANSCRIPT ===")
    rec_for_match = [(rid, t["words"]) for rid, fn, t in rec_tr]
    rec_name = {rid: fn for rid, fn, t in rec_tr}
    for cid, (fn, t) in clip_tr.items():
        m = ts.best_match(t["words"], rec_for_match)
        if m:
            logging.info(f"{fn}  ->  {rec_name.get(m['recording_id'], '?')}  "
                         f"offset={m['offset_sec']:+.2f}s  conf={m['confidence']:.2f}  "
                         f"anclas={m['anchors']}")
        else:
            logging.info(f"{fn}  ->  sin match")
    logging.info(f"Listo. {(time.time()-t0)/60:.1f}min total.")


if __name__ == "__main__":
    main()
