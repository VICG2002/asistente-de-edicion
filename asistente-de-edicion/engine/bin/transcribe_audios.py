#!/usr/bin/env python3
"""Transcribe the external audio recordings under AUDIOS/, caching transcripts.

Transcribes the stereo mixes and named files; skips the per-microphone _Tr track
files, which duplicate the _LR mix. Feeds both the transcript-based sync and the
sound catalog (a file with no real speech is ambience / room tone / SFX).
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
from lib.guards import assert_selected, lock_exclusivo, report_done
from lib import manifest  # noqa: E402


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
    ap.add_argument("--lang", default="es")
    ap.add_argument("--audio-like", default="%audios%",
                    help="rel_path LIKE pattern (lowercased). Default '%%audios%%' "
                         "mantiene compat con ESCALANDO MEXICO. Para Zezzions "
                         "(audios bajo 'Audio 001 VICG/Lavas/'): '%%lavas%%'.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Procesar solo los primeros N audios (smoke testing).")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db = ws / "manifest.sqlite"
    cache = ws / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)
    log_file = ws / "logs" / f"transcribe_audio_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    rows = conn.execute(
        "SELECT id, path, filename FROM clips WHERE file_kind='audio' "
        "AND index_status='ok' AND lower(rel_path) LIKE ? "
        "AND filename NOT GLOB '*_[Tt][Rr][0-9]*' "
        "ORDER BY rel_path, filename",
        (args.audio_like.lower(),)
    ).fetchall()
    conn.close()
    if args.limit:
        rows = rows[: args.limit]
    logging.info(f"Audio a transcribir (mixes/nombrados, sin pistas _Tr): {len(rows)} "
                 f"(audio-like={args.audio_like})")
    assert_selected(rows, "audios a transcribir",
                    filters={"--root": str(root), "--audio-like": args.audio_like},
                    hint="Proyecto plano (carpeta=cadena): probar --audio-like 'audio/%'")

    done = cached = empty = 0
    t0 = time.time()
    # Leccion 43: NUNCA dos Whisper en paralelo, comparten la GPU Metal y el
    # mas largo se arrastra sin limite. El lock hace la regla estructural, no
    # una promesa: si transcribe_clips.py ya lo tiene, esto aborta al tomarlo.
    with lock_exclusivo("transcribe_audios"):
        for i, (aid, path, fn) in enumerate(rows, 1):
            cf = cache / f"{aid}.json"
            if cf.exists():
                cached += 1
            else:
                r = tr.transcribe(path, args.model, lang=args.lang)
                if r and r.get("words"):
                    cf.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
                    done += 1
                else:
                    # cache an empty marker — non-speech files (ambience/SFX) won't retry
                    cf.write_text(json.dumps({"text": "", "words": []}), encoding="utf-8")
                    empty += 1
            if i % 10 == 0:
                logging.info(f"  {i}/{len(rows)}  texto={done} vacios={empty} cache={cached} "
                             f"({(time.time() - t0) / 60:.1f}min)")
    logging.info(f"Listo. {done} con texto, {empty} sin habla, {cached} en cache, "
                 f"{(time.time() - t0) / 60:.1f}min.")
    report_done("transcribe_audios", con_texto=done, sin_habla=empty, cache=cached)


if __name__ == "__main__":
    main()
