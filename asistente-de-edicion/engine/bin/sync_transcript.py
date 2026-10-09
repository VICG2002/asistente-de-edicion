#!/usr/bin/env python3
"""Sync interview clips to external recordings by transcript content alignment.

The current dual-system sync method — replaces the waveform approach. Reads the
cached transcripts (.cinema_assistant/transcripts/<id>.json), aligns each clip
that has real dialogue to the recording whose words match, and writes the result
to audio_sync_pairs (method='transcript'). Content-verified: it pairs by the
actual words spoken, so it cannot mismatch the way loudness correlation did.
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
from lib import transcript_sync as ts
from lib import manifest  # noqa: E402

# Que camaras llevan dialogo que valga la pena sincronizar lo dice el registro
# `config/camera_profiles.json` via `cameras.main_cam_sql()`. Antes esta lista
# vivia aqui y no conocia la a6700.


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def load_transcript(cache: Path, clip_id):
    cf = cache / f"{clip_id}.json"
    if not cf.exists():
        return None
    try:
        d = json.loads(cf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return d if d and d.get("words") else None


def distinct_words(words) -> int:
    """Distinct normalized words — a real interview has many; a clip that only
    produced Whisper hallucination ('suscribete al canal' x N) has few."""
    return len({ts.normalize(w) for w, _ in words} - {""})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--location", default="",
                    help="rel_path substring de los VIDEOS. Default '' = todo "
                         "el proyecto (el histórico 'jilo' era hardcode "
                         "Jilotepec — bug de doctrina, fix FCC 2026-07-09).")
    ap.add_argument("--audio-like", default="%",
                    help="rel_path LIKE de los AUDIOS externos. Default '%%' = "
                         "todos (histórico '%%audios%%jilo%%' era hardcode).")
    ap.add_argument("--min-distinct", type=int, default=60,
                    help="skip clips with fewer distinct words (no real dialogue)")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--max-pairs-per-video", type=int, default=1,
                    help="Emite hasta N pares por video. Default 1 (single-lavalier "
                         "= comportamiento ESC). Pasar 2 para dual-lavalier "
                         "(TX1+TX2 separados, caso Zezzions 2026-05-25).")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db = ws / "manifest.sqlite"
    cache = ws / "transcripts"
    log_file = ws / "logs" / f"synctr_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    clip_rows = conn.execute(
        "SELECT id, filename FROM clips WHERE file_kind='video' AND index_status='ok' "
        "AND has_audio=1 AND lower(rel_path) LIKE ? AND lower(rel_path) NOT LIKE '%repetido%' "
        f"AND {cameras.main_cam_sql(disk_root=root)} "
        "ORDER BY creation_time, filename",
        (f"%{args.location.lower()}%",)).fetchall()
    rec_rows = conn.execute(
        "SELECT id, filename FROM clips WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND duration_sec >= 60 ORDER BY filename",
        (args.audio_like,)).fetchall()

    recs, rec_name = [], {}
    for rid, fn in rec_rows:
        t = load_transcript(cache, rid)
        if t:
            recs.append((rid, t["words"]))
            rec_name[rid] = fn
    logging.info(f"Clips candidatos: {len(clip_rows)}  |  "
                 f"grabaciones con transcript: {len(recs)}/{len(rec_rows)}")
    if not recs:
        sys.exit("No hay transcripts de grabaciones; corre transcribe_clips primero.")

    pairs, attempted, skipped = [], 0, 0
    for cid, fn in clip_rows:
        t = load_transcript(cache, cid)
        if t is None:
            continue
        if distinct_words(t["words"]) < args.min_distinct:
            skipped += 1
            continue
        attempted += 1
        matches = ts.top_matches(t["words"], recs,
                                 min_conf=args.min_confidence,
                                 max_pairs=args.max_pairs_per_video)
        for rank, m in enumerate(matches, 1):
            pairs.append((cid, m["recording_id"], m["offset_sec"], m["confidence"]))
            tag = f"#{rank}" if args.max_pairs_per_video > 1 else ""
            logging.info(f"{fn} -> {rec_name.get(m['recording_id'], '?')} {tag} "
                         f"offset={m['offset_sec']:+.2f}s  conf={m['confidence']:.2f}  "
                         f"anclas={m['anchors']}")
    logging.info(f"Intentados {attempted}, saltados {skipped} (poco dialogo), "
                 f"{len(pairs)} pares >= conf {args.min_confidence} "
                 f"(max {args.max_pairs_per_video}/video)")

    manifest.ensure_audio_sync_pairs(conn)
    # Borrado selectivo: solo pares cuyo video pertenece a este `--location` Y
    # cuyo audio pertenece a este `--audio-like` (el universo que este run va a
    # re-escribir). Sin el filtro de audio, `--location ""` (proyecto plano)
    # degenera en DELETE global y destruye pares de otros metodos/audios
    # (caso MAB 2026-06-11: una pesca de anchors zoom borro 196 pares chrono).
    # Los metodos blindados (manual*, *-locked) NUNCA se borran automaticamente.
    conn.execute(
        "DELETE FROM audio_sync_pairs WHERE video_clip_id IN ("
        " SELECT id FROM clips WHERE lower(rel_path) LIKE ?)"
        " AND audio_clip_id IN ("
        " SELECT id FROM clips WHERE lower(rel_path) LIKE ?)"
        " AND method NOT LIKE 'manual%' AND method NOT LIKE '%-locked'",
        (f"%{args.location.lower()}%", args.audio_like.lower())
    )
    now = time.time()
    for cid, aid, off, conf in pairs:
        conn.execute(
            "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, method, "
            "offset_sec, confidence, created_at) VALUES (?,?,?,?,?,?)",
            (cid, aid, "transcript", off, conf, now),
        )
    conn.commit()
    conn.close()
    logging.info(f"audio_sync_pairs: {len(pairs)} pares escritos (method=transcript).")


if __name__ == "__main__":
    main()
