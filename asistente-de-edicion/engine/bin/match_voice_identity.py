#!/usr/bin/env python3
"""Voice-First identity matching para sync — Fase 2 del pipeline rebuild.

Para cada video de entrevista (o con caras detectadas), calcula el
embedding de voz esperado (vía face_voice_links existentes) y score TODOS
los audios candidatos. Guarda candidatos en `sync_candidates`:

  - status='auto': top1 con sim ≥0.85 Y prominence ≥0.05 → único candidato.
  - status='needs_review': múltiples candidatos cercanos al top.
  - status='rejected': sim < 0.45 → no es match.

Después, Fase 4 (`compute_sync_offset.py`) toma cada candidato `auto` o
`needs_review` y le calcula offset. La convergencia entre voice_match +
offset acústico decide qué pares finalmente se escriben a
`audio_sync_pairs`.

Uso:
    bin/match_voice_identity.py --root <disk>
    bin/match_voice_identity.py --root <disk> --reset      # borrar candidatos previos
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

from lib.voice_match import (
    expected_voice_embedding_for_video,
    n_persons_in_frame,
    top_audio_candidates,
    prominence,
    filter_candidates_by_distinctness,
    THRESH_AUTO, THRESH_REVIEW,
)
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
    ap.add_argument("--min-video-dur", type=float, default=30.0)
    ap.add_argument("--audio-like", default="%lavas%")
    ap.add_argument("--reset", action="store_true",
                    help="Borrar todos los sync_candidates previos")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"match_voice_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_clip_id INTEGER, audio_clip_id INTEGER,
            identity_score REAL, offset_sec REAL, method TEXT,
            conf REAL, status TEXT, signals_json TEXT, notes TEXT,
            created_at REAL
        )
    """)
    if args.reset:
        conn.execute("DELETE FROM sync_candidates")

    # Videos candidatos: con audio + duración mínima + caras detectadas
    video_rows = conn.execute("""
        SELECT c.id, c.filename, c.duration_sec
        FROM clips c
        WHERE c.file_kind='video' AND c.index_status='ok' AND c.has_audio=1
          AND c.duration_sec >= ?
          AND c.id IN (SELECT DISTINCT clip_id FROM face_detections)
        ORDER BY c.duration_sec DESC
    """, (args.min_video_dur,)).fetchall()
    if args.limit:
        video_rows = video_rows[:args.limit]
    logging.info(f"Videos candidatos: {len(video_rows)}")

    audio_ids = [r[0] for r in conn.execute(
        "SELECT id FROM clips WHERE file_kind='audio' AND index_status='ok' "
        "AND duration_sec >= 30 AND lower(rel_path) LIKE ?",
        (args.audio_like,)
    ).fetchall()]
    logging.info(f"Audios candidatos: {len(audio_ids)}")

    now = time.time()
    n_auto = n_review = n_rejected = n_no_expected = 0
    for vid, vfn, vdur in video_rows:
        expected = expected_voice_embedding_for_video(conn, vid)
        if expected is None:
            n_no_expected += 1
            logging.info(f"[{vid:<4}] {vfn} ({vdur:.0f}s): NO_EXPECTED_EMB")
            continue
        n_persons = n_persons_in_frame(conn, vid)
        top = top_audio_candidates(conn, vid, audio_ids, expected, top_k=8)
        if not top:
            continue
        # Para dual-lavalier (n_persons>=2) permitir max 2 candidatos auto.
        # Para single, solo top1.
        candidates_to_keep = filter_candidates_by_distinctness(top, min_prominence=0.05)
        if n_persons >= 2 and len(candidates_to_keep) == 1:
            # Permitir un segundo candidato si está cerca del top
            for aid, sim, _ in top[1:3]:
                if sim >= THRESH_REVIEW and sim >= top[0][1] - 0.15:
                    candidates_to_keep.append((aid, sim, "needs_review"))
        prom = prominence(top)
        for rank, (aid, sim, status) in enumerate(candidates_to_keep, 1):
            conn.execute(
                "INSERT INTO sync_candidates "
                "(video_clip_id, audio_clip_id, identity_score, status, "
                "method, conf, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (vid, aid, sim, status, "voice-match", sim,
                 f"rank={rank} of {len(candidates_to_keep)}, prominence={prom:.3f}, "
                 f"n_persons_in_frame={n_persons}",
                 now)
            )
            if status == "auto": n_auto += 1
            elif status == "needs_review": n_review += 1
            else: n_rejected += 1
        # Log resumido por video
        best = top[0]
        best_fn = conn.execute(
            "SELECT filename, rel_path FROM clips WHERE id=?", (best[0],)
        ).fetchone()
        kept_status = candidates_to_keep[0][2] if candidates_to_keep else "none"
        logging.info(f"[{vid:<4}] {vfn[:30]} dur={vdur:.0f}s persons={n_persons} "
                     f"prom={prom:.3f} top1=({Path(best_fn[1]).parent.name}/{best_fn[0][:20]}, "
                     f"{best[1]:.2f}) → {kept_status}")
    conn.commit()
    conn.close()
    logging.info(f"\nResumen: auto={n_auto}, needs_review={n_review}, "
                 f"rejected={n_rejected}, no_expected_emb={n_no_expected}")


if __name__ == "__main__":
    main()
