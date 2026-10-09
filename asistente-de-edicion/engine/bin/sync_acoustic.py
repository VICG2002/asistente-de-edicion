#!/usr/bin/env python3
"""Sync acoustic fallback — cross-correlación de envelopes de loudness.

Para entrevistas grabadas en eventos con MÚSICA fuerte: el `sync_transcript`
falla porque Whisper no logra extraer suficientes palabras-ancla del audio
de cámara tapado por la música. Pero el envelope de loudness de la MÚSICA
ambiente es la MISMA señal en el A1 de cámara y en el lavalier (con SNR
distinto). Esa señal SÍ correla y nos da el offset.

Caso fundador (Zezzions 2026-05-26): 10 entrevistas video sin sync. El
sugerencia del usuario fue exactamente esto — usar la similaridad acústica
de la música y elementos del sonido para emparejar.

Estrategia:
  1. Para cada video candidato (entrevista video sin sync_pair existente)
     extraer envelope de loudness via `lib.waveform_sync.extract_envelope`.
  2. Para cada audio externo del MISMO DÍA (limitar por creation_time
     para reducir espacio de búsqueda + bajar tasa de falsos positivos)
     extraer envelope.
  3. Cross-correlate. Si conf >= --min-confidence (default 0.30 — más bajo
     que sync_transcript porque acoustic es señal más ruidosa pero más
     robusta a falta de habla).
  4. Insertar par en `audio_sync_pairs` con method='waveform-envelope'.

Uso:
    bin/sync_acoustic.py --root /Volumes/.../Zezzions VICG \\
        --min-confidence 0.30 --max-pairs-per-video 2
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

from lib.waveform_sync import correlate, extract_envelope
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def same_day(t1: str, t2: str, tolerance_hours: int = 36) -> bool:
    """True si dos timestamps ISO caen dentro de tolerance_hours.

    Si CUALQUIERA de los dos no es parseable como datetime completo, NO
    filtrar (return True) — la correlación es el filtro real. Caso MAB
    2026-06-11: el Zoom H1n escribe creation_time solo-hora ("13:24:12");
    el fallback viejo comparaba "2026-06-11" == "13:24:12" y excluía
    TODOS los audios del Zoom silenciosamente.
    """
    if not t1 or not t2:
        return True  # sin info de fecha, no filtrar
    try:
        from datetime import datetime
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return abs((d1 - d2).total_seconds()) <= tolerance_hours * 3600
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%lavas%",
                    help="rel_path LIKE (lowercased) para audios externos.")
    ap.add_argument("--location", default="video",
                    help="rel_path LIKE para video candidato.")
    ap.add_argument("--min-confidence", type=float, default=0.30)
    ap.add_argument("--max-pairs-per-video", type=int, default=2)
    ap.add_argument("--only-missing", action="store_true", default=True,
                    help="Solo videos que NO tengan ya audio_sync_pairs.")
    ap.add_argument("--same-day-tolerance-hours", type=int, default=36)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")
    log_file = root / ".cinema_assistant" / "logs" / f"sync_acoustic_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Audios externos
    audio_rows = conn.execute(
        "SELECT id, filename, path, creation_time, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND duration_sec >= 30 "
        "ORDER BY filename",
        (args.audio_like,)
    ).fetchall()
    logging.info(f"Audios externos candidatos: {len(audio_rows)}")

    # Pre-extraer envelopes de audios externos
    audio_env_cache = {}
    for aid, afn, apath, actime, adur in audio_rows:
        env = extract_envelope(Path(apath))
        if env is None:
            logging.info(f"  skip audio {aid} {afn}: no envelope")
            continue
        audio_env_cache[aid] = {"env": env, "filename": afn,
                                "ctime": actime, "dur": adur}
    logging.info(f"Envelopes de audio listos: {len(audio_env_cache)}")

    # Videos candidatos: entrevistas video (limpias o parciales)
    # Si only-missing, excluir los que ya tienen sync_pair.
    missing_clause = (
        "AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)"
        if args.only_missing else ""
    )
    video_rows = conn.execute(
        f"""SELECT c.id, c.filename, c.path, c.creation_time, c.duration_sec
            FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id=c.id
            WHERE c.file_kind='video' AND c.index_status='ok'
              AND c.has_audio=1
              AND lower(c.rel_path) LIKE ?
              AND (d.category LIKE 'entrevista%' OR c.duration_sec >= 60)
              {missing_clause}
            ORDER BY c.creation_time""",
        (f"%{args.location.lower()}%",)
    ).fetchall()
    logging.info(f"Videos candidatos: {len(video_rows)}")

    # Asegurar tabla
    manifest.ensure_audio_sync_pairs(conn)

    now = time.time()
    n_pairs_total = 0
    for vid, vfn, vpath, vctime, vdur in video_rows:
        venv = extract_envelope(Path(vpath))
        if venv is None:
            continue
        scored = []
        for aid, info in audio_env_cache.items():
            # Filtrar por mismo día (rod heuristic — evita matches cruzados
            # de eventos lejanos)
            if not same_day(vctime, info["ctime"], args.same_day_tolerance_hours):
                continue
            res = correlate(venv, info["env"])
            if res is None:
                continue
            offset, conf = res
            scored.append({"aid": aid, "afn": info["filename"],
                           "offset": offset, "conf": conf})
        scored.sort(key=lambda m: m["conf"], reverse=True)
        kept = [s for s in scored if s["conf"] >= args.min_confidence][:args.max_pairs_per_video]
        for rank, s in enumerate(kept, 1):
            conn.execute(
                "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, "
                "method, offset_sec, confidence, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (vid, s["aid"], "waveform-envelope", s["offset"], s["conf"],
                 f"rank={rank}", now)
            )
            n_pairs_total += 1
            tag = f"#{rank}" if args.max_pairs_per_video > 1 else ""
            logging.info(f"  {vfn} -> {s['afn']} {tag} "
                         f"offset={s['offset']:+.2f}s conf={s['conf']:.2f}")
        if not kept:
            top = scored[0] if scored else None
            if top:
                logging.info(f"  {vfn}: sin par >= {args.min_confidence} "
                             f"(mejor candidato {top['afn']} conf={top['conf']:.2f})")
            else:
                logging.info(f"  {vfn}: sin candidatos")
    conn.commit()
    conn.close()
    logging.info(f"sync_acoustic: {n_pairs_total} pares escritos.")


if __name__ == "__main__":
    main()
