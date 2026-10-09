#!/usr/bin/env python3
"""Re-transcribe los clips marcados `needs_retranscribe=1` por `audit_transcripts.py`,
usando `lib.transcribe_strict` (band-pass voz + VAD + thresholds estrictos).

NO re-transcribe los que ya pasaron audit. Cache antiguo se reemplaza solo
para los marcados.

Uso:
    bin/retranscribe_audit.py --root <disk> --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin
    bin/retranscribe_audit.py --root <disk> ... --only-audio    # solo audios externos
    bin/retranscribe_audit.py --root <disk> ... --max N         # límite (debug)
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.transcribe_strict import transcribe_strict
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
    ap.add_argument("--model", required=True)
    ap.add_argument("--only-audio", action="store_true",
                    help="Solo audios externos (skip videos)")
    ap.add_argument("--only-video", action="store_true",
                    help="Solo videos (skip audios externos)")
    ap.add_argument("--max", type=int, default=0, help="0 = todos")
    ap.add_argument("--no-speech-thold", type=float, default=0.7)
    ap.add_argument("--logprob-thold", type=float, default=-0.5)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"retranscribe_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    tr_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = root / ".cinema_assistant" / "transcripts_legacy"
    backup_dir.mkdir(parents=True, exist_ok=True)

    conn = manifest.conectar(str(db))
    kind_filter = ""
    if args.only_audio:
        kind_filter = "AND c.file_kind='audio'"
    elif args.only_video:
        kind_filter = "AND c.file_kind='video'"

    rows = conn.execute(f"""
        SELECT tq.clip_id, c.filename, c.file_kind, c.path, c.duration_sec
        FROM transcript_quality tq JOIN clips c ON c.id=tq.clip_id
        WHERE tq.needs_retranscribe=1 {kind_filter}
        ORDER BY c.file_kind, c.duration_sec DESC
    """).fetchall()
    if args.max:
        rows = rows[:args.max]
    logging.info(f"Clips a re-transcribir: {len(rows)}")

    n_ok = n_fail = 0
    for cid, fn, kind, fpath, dur in rows:
        logging.info(f"[{cid}] {kind} {fn} ({dur:.0f}s)")
        try:
            r = transcribe_strict(
                fpath, args.model, lang="es",
                no_speech_thold=args.no_speech_thold,
                logprob_thold=args.logprob_thold,
                voice_bandpass=True, use_vad=True,
            )
        except Exception as e:
            logging.warning(f"  ERROR: {e}")
            r = None
        if r is None:
            n_fail += 1
            logging.warning("  falló re-transcripción")
            continue
        n_ok += 1
        # Backup transcript viejo
        old_path = tr_dir / f"{cid}.json"
        if old_path.exists():
            backup_path = backup_dir / f"{cid}.{int(time.time())}.json"
            shutil.copy2(str(old_path), str(backup_path))
        # Escribir nuevo
        old_path.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        logging.info(f"  ✓ {len(r['words'])} words escritas (legacy guardado)")

    logging.info(f"\nRetranscribe done. ok={n_ok}  fail={n_fail}")
    conn.close()


if __name__ == "__main__":
    main()
