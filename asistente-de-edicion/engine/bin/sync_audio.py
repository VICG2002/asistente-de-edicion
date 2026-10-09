#!/usr/bin/env python3
"""Pair videos with external WAV audio by absolute timecode.

Usage:
    python3 bin/sync_audio.py --root /Volumes/MI_DISCO \
        --video-folder "Jilotepec 2" \
        --audio-folder "AUDIOS/AUDIOS JILOTEPEC ULTIMO FIN" \
        [--audio-tz-shift -6]

Reads videos from the manifest (filtered by parent_folder if given). Indexes the
audio folder if needed. Extracts BWF Time Reference for absolute timestamp.
Pairs by temporal overlap. Writes to audio_sync_pairs table + CSV report.

Never modifies source files.
"""

from __future__ import annotations

import argparse
import csv
import glob
import logging
import sqlite3
import sys
import time
from datetime import timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(ENGINE_ROOT))

from lib import audio_sync, classify, manifest, probe


# El esquema de audio_sync_pairs vive en lib/manifest.py (una sola definicion).


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )


def ensure_audio_indexed(conn, root: Path, audio_folder_rel: str) -> int:
    """Walk the audio folder and index any WAVs/audios not in the manifest yet."""
    audio_root = root / audio_folder_rel
    if not audio_root.exists():
        sys.exit(f"Audio folder not found: {audio_root}")
    added = 0
    import os
    for r, dirs, files in os.walk(audio_root):
        rp = Path(r)
        dirs[:] = [d for d in dirs if not classify.should_skip_dir(rp / d)]
        for name in files:
            p = rp / name
            if classify.should_skip(p) or classify.file_kind(p) != "audio":
                continue
            existing = manifest.existing_signature(conn, str(p))
            if existing is not None:
                continue
            rec = {c: None for c in manifest.CLIP_COLUMNS}
            try:
                st = p.stat()
            except OSError:
                continue
            summary, raw, source = probe.probe(p)
            rec.update({
                "path": str(p),
                "rel_path": str(p.relative_to(root)),
                "parent_folder": p.parent.name,
                "filename": p.name,
                "ext": p.suffix.lower(),
                "size_bytes": st.st_size,
                "mtime": st.st_mtime,
                "file_kind": "audio",
                "sha256_partial": manifest.partial_hash(p),
                "probe_source": source,
                "indexed_at": time.time(),
                "index_status": "ok",
            })
            rec.update(summary)
            manifest.upsert_clip(conn, rec)
            added += 1
    conn.commit()
    return added


def collect_videos(conn, folder: str | None) -> list[dict]:
    sql = (
        "SELECT id, path, parent_folder, duration_sec, creation_time "
        "FROM clips WHERE file_kind = 'video' AND index_status = 'ok' "
    )
    params: list = []
    if folder:
        sql += "AND parent_folder = ? "
        params.append(folder)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        start = audio_sync.parse_video_creation_time(r[4])
        out.append({
            "id": r[0], "path": r[1], "parent_folder": r[2],
            "duration_sec": r[3], "creation_time": r[4],
            "start_dt": start,
        })
    return out


def collect_audios(conn, audio_folder_rel: str) -> list[dict]:
    """Find audio clips in the manifest under the given folder. Extract BWF timestamps."""
    like = f"%/{audio_folder_rel}/%"
    rows = conn.execute(
        "SELECT id, path, parent_folder, duration_sec FROM clips "
        "WHERE file_kind = 'audio' AND index_status = 'ok' AND path LIKE ?",
        (like,),
    ).fetchall()
    out = []
    for r in rows:
        path = Path(r[1])
        bwf = audio_sync.extract_audio_start(path)
        out.append({
            "id": r[0], "path": r[1], "parent_folder": r[2],
            "duration_sec": bwf.get("duration_sec") or r[3],
            "start_dt": bwf.get("absolute_start"),
            "method": bwf.get("method"),
        })
    return out


def guardar_pares(conn, video_folder: str | None, pairs: list[dict],
                  now: float) -> tuple[int, int]:
    """Reemplaza los pares de TIMECODE del sector y no toca ningun otro.

    Borraba TODOS los pares de los videos del sector antes de insertar los
    suyos. En Asistente (2026-09-29) los WAV de Rode no traen timecode: no
    inserto ninguno y se llevo los 60 pares medidos por transcript, reloj y
    onda, el sync de un dia entero, sin un aviso. Ahora borra solo los de
    metodo 'timecode', que son los unicos que produce, y no duplica un par que
    otro metodo ya midio. Devuelve (insertados, ya_medidos).
    """
    if video_folder:
        conn.execute(
            "DELETE FROM audio_sync_pairs WHERE method = 'timecode' AND video_clip_id IN ("
            "  SELECT id FROM clips WHERE rel_path LIKE ?)",
            (f"%{video_folder}%",))
    else:
        conn.execute("DELETE FROM audio_sync_pairs WHERE method = 'timecode'")
    medidos = set(conn.execute("SELECT video_clip_id, audio_clip_id FROM audio_sync_pairs"))
    insertados = ya_medidos = 0
    for p in pairs:
        if (p["video_clip_id"], p["audio_clip_id"]) in medidos:
            ya_medidos += 1
            continue
        conn.execute(
            "INSERT INTO audio_sync_pairs "
            "(video_clip_id, audio_clip_id, method, offset_sec, confidence, "
            "video_start_iso, audio_start_iso, overlap_sec, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p["video_clip_id"], p["audio_clip_id"], p["method"],
             p["offset_sec"], p["confidence"],
             p["video_start_iso"], p["audio_start_iso"], p["overlap_sec"], now),
        )
        insertados += 1
    return insertados, ya_medidos


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--video-folder", default=None, help="parent_folder filter for videos")
    p.add_argument("--audio-folder", required=True, help="Path relative to root containing WAVs")
    p.add_argument("--audio-tz-shift", type=float, default=0.0,
                   help="Hours to shift audio timestamps to match video timezone (e.g. -6 if audio is UTC-6 but video is UTC)")
    p.add_argument("--reset-schema", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    root = resolve_root(args.root)
    workspace = root / ".cinema_assistant"
    db_path = workspace / "manifest.sqlite"
    if not db_path.exists():
        sys.exit(f"No manifest at {db_path}. Run index_project.py first.")

    log_file = workspace / "logs" / f"sync_{int(time.time())}.log"
    setup_logging(log_file)
    logging.info(f"Root: {root}")
    logging.info(f"Video folder filter: {args.video_folder or '(all)'}")
    logging.info(f"Audio folder: {args.audio_folder}")
    logging.info(f"Audio TZ shift: {args.audio_tz_shift:+.1f} h")

    conn = manifest.conectar(str(db_path))
    if args.reset_schema:
        # Opt-in destructivo, explicito vía flag. Si quedan pairs de otros
        # sectores y se pasa --reset-schema, el operador asume la perdida.
        conn.execute("DROP TABLE IF EXISTS audio_sync_pairs")  # lint:ok delete-global
    manifest.ensure_audio_sync_pairs(conn)

    logging.info("Ensuring audio folder is indexed...")
    added = ensure_audio_indexed(conn, root, args.audio_folder)
    logging.info(f"Added {added} new audio clip(s) to manifest.")

    videos = collect_videos(conn, args.video_folder)
    logging.info(f"Found {len(videos)} video clip(s) with usable creation_time.")
    audios = collect_audios(conn, args.audio_folder)
    logging.info(f"Found {len(audios)} audio clip(s) in target folder.")

    videos_with_time = [v for v in videos if v["start_dt"] is not None]
    audios_with_time = [a for a in audios if a["start_dt"] is not None]
    logging.info(f"Videos with timestamp: {len(videos_with_time)}/{len(videos)}")
    logging.info(f"Audios with timestamp: {len(audios_with_time)}/{len(audios)}")

    pairs = audio_sync.find_overlaps(
        videos_with_time, audios_with_time,
        audio_tz_offset_hours=args.audio_tz_shift,
    )
    logging.info(f"Found {len(pairs)} overlap pair(s).")

    now = time.time()
    insertados, ya_medidos = guardar_pares(conn, args.video_folder, pairs, now)
    if ya_medidos:
        logging.info(f"{ya_medidos} par(es) ya medidos por otro metodo: no se duplican.")
    logging.info(f"Pares de timecode guardados: {insertados}.")
    conn.commit()

    report = workspace / "reports" / f"sync_pairs_{int(now)}.csv"
    report.parent.mkdir(parents=True, exist_ok=True)
    with open(report, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video_clip_id", "audio_clip_id", "method", "offset_sec",
                    "confidence", "overlap_sec", "video_start_iso", "audio_start_iso"])
        for p in pairs:
            w.writerow([p["video_clip_id"], p["audio_clip_id"], p["method"],
                        f"{p['offset_sec']:.3f}", f"{p['confidence']:.3f}",
                        f"{p['overlap_sec']:.1f}", p["video_start_iso"], p["audio_start_iso"]])
    logging.info(f"Wrote {report}")

    conn.close()


if __name__ == "__main__":
    main()
