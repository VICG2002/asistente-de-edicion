#!/usr/bin/env python3
"""Pair interview clips with external recordings by waveform correlation.

Usage:
    python3 bin/sync_waveform.py --root /Volumes/MI_DISCO \
        [--location JILOTEPEC] [--audio-like '%audios%jilo%'] \
        [--min-dur 60] [--min-confidence 0.35] [--limit N]

Selects main-camera clips (Canon EOS 6D / Blackmagic — the interview cameras)
that have scratch audio and run long, plus external recordings already in the
manifest. Cross-correlates loudness envelopes and writes the best match per clip
(above the confidence threshold) to audio_sync_pairs with method='waveform'.

GoPro and drone footage is excluded — it carries usable embedded audio and is
not dual-system. Never modifies source files.
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

from lib import cameras
from lib import waveform_sync as wf
from lib import manifest  # noqa: E402

# BUG HISTORICO (arreglado con el registro de camaras, v0.2.0): aqui vivia
#   MAIN_CAM_SQL = "(substr(upper(filename),1,4) IN ('MVI_','BLAC')
#                    OR camera_model='Canon EOS 6D')"
# que NO conocia VICG, ILME-FX30 ni ILCE-6700. Todo el material de FX30 y a6700
# quedaba fuera del sync por envelope, en silencio y con exit 0. La lista ahora
# sale del registro `config/camera_profiles.json`.


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--location", default="",
                    help="case-insensitive substring of rel_path to filter "
                         "clips. Default '' = todo (histórico 'jilo' era "
                         "hardcode Jilotepec — fix FCC 2026-07-09).")
    ap.add_argument("--audio-like", default="%",
                    help="LIKE pattern (on lowercased rel_path) selecting "
                         "audio files. Default '%%' = todos.")
    ap.add_argument("--min-dur", type=float, default=60.0)
    ap.add_argument("--min-audio-dur", type=float, default=60.0,
                    help="ignore external recordings shorter than this (fragments "
                         "produce spurious high-confidence matches)")
    ap.add_argument("--min-confidence", type=float, default=0.35)
    ap.add_argument("--limit", type=int, default=0, help="cap clip count (smoke test)")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db = ws / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    log_file = ws / "logs" / f"wfsync_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    loc = f"%{args.location.lower()}%"
    videos = conn.execute(
        "SELECT id, path, filename, duration_sec FROM clips "
        "WHERE file_kind='video' AND index_status='ok' AND has_audio=1 "
        f"AND duration_sec >= ? AND {cameras.main_cam_sql(disk_root=root)} "
        "AND lower(rel_path) LIKE ? AND lower(rel_path) NOT LIKE '%repetido%' "
        "ORDER BY filename",
        (args.min_dur, loc),
    ).fetchall()
    audios = conn.execute(
        "SELECT id, path, filename FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' AND lower(rel_path) LIKE ? "
        "AND duration_sec >= ? "
        "ORDER BY filename",
        (args.audio_like, args.min_audio_dur),
    ).fetchall()
    if args.limit:
        videos = videos[: args.limit]
    logging.info(f"Candidate clips: {len(videos)}  |  external recordings: {len(audios)}")
    if not videos or not audios:
        sys.exit("Nothing to correlate.")

    logging.info("Extracting audio envelopes for the recordings...")
    aud_env = {}
    for i, (aid, apath, aname) in enumerate(audios, 1):
        env = wf.extract_envelope(Path(apath))
        if env is not None:
            aud_env[aid] = (aname, env)
        if i % 20 == 0:
            logging.info(f"  recordings {i}/{len(audios)}")
    logging.info(f"Recording envelopes ready: {len(aud_env)}/{len(audios)}")
    if not aud_env:
        sys.exit("No usable recording envelopes.")

    pairs = []
    errs = 0
    t0 = time.time()
    for i, (vid, vpath, vname, vdur) in enumerate(videos, 1):
        venv = wf.extract_envelope(Path(vpath))
        if venv is None:
            errs += 1
            continue
        best = None
        for aid, (aname, aenv) in aud_env.items():
            res = wf.correlate(venv, aenv)
            if res is None:
                continue
            offset, conf = res
            if best is None or conf > best[1]:
                best = (aid, conf, offset, aname)
        if best and best[1] >= args.min_confidence:
            aid, conf, offset, aname = best
            pairs.append((vid, aid, offset, conf))
            logging.info(f"  [{i}/{len(videos)}] {vname} <- {aname}  "
                         f"conf={conf:.2f}  offset={offset:+.1f}s")
        if i % 25 == 0:
            logging.info(f"progress {i}/{len(videos)} pairs={len(pairs)} "
                         f"err={errs} {(time.time() - t0) / 60:.1f}min")

    logging.info(f"Done. {len(pairs)} pair(s) at conf>={args.min_confidence}, "
                 f"{errs} extraction error(s), {(time.time() - t0) / 60:.1f}min.")

    manifest.ensure_audio_sync_pairs(conn)
    now = time.time()
    for vid, aid, offset, conf in pairs:
        conn.execute(
            "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, method, "
            "offset_sec, confidence, created_at) VALUES (?,?,?,?,?,?)",
            (vid, aid, "waveform", offset, conf, now),
        )
    conn.commit()
    conn.close()
    logging.info(f"Wrote {len(pairs)} pair(s) to audio_sync_pairs.")


if __name__ == "__main__":
    main()
