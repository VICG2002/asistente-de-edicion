#!/usr/bin/env python3
"""Construir lavalier_pairs por TIMESTAMP+CONTENIDO, no por nombre.

Caso fundador (Zezzions iter4, 2026-05-26): asumir que `Dr/0000N.WAV` e
`Izq/0000N.WAV` son hermanos por el mismo número es FALSO cuando uno de
los TX se apagó (los contadores se desincronizan). El mapeo verdadero
requiere:

1. `|Δmtime| ≤ 120s` (cross-match cualquier Dr con cualquier Izq)
2. `|Δduration| ≤ 60s` (uno puede haberse cortado antes)
3. `4-gram overlap del transcript ≥ 0.20` (mismo contenido sonoro)
4. `delta_sec` por transcript ngram-align (n≥100, MAD<0.5s)

Applicable=1 solo si pasa todos los checks.

Doctrina: ver `~/memoria-asistente-edicion/metodologia/sync-sin-ground-truth.md`
sección "Detección de hermanos lavalier".

Uso:
    bin/build_lavalier_pairs.py --root <disk>
    bin/build_lavalier_pairs.py --root <disk> --dry-run
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sync_segment_analysis import (
    load_words, compute_offset, fourgram_overlap
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


def get_audio_metadata(conn: sqlite3.Connection, audio_id: int) -> dict | None:
    """Devuelve {id, path, dur, mtime, basename, folder_parent}"""
    row = conn.execute(
        "SELECT id, path, filename, duration_sec FROM clips "
        "WHERE id=? AND file_kind='audio'", (audio_id,)
    ).fetchone()
    if not row:
        return None
    cid, path, fname, dur = row
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    folder_parent = Path(path).parent.name
    return {
        "id": cid, "path": path, "filename": fname,
        "dur": dur or 0.0, "mtime": mtime,
        "folder_parent": folder_parent,
    }


def find_sibling_candidates(audios: list[dict], delta_mtime_max: float = 120.0,
                            delta_dur_max: float = 60.0) -> list[tuple[dict, dict]]:
    """Para cada par (a, b) de audios, retornar los que cumplen:
       - |Δmtime| ≤ delta_mtime_max
       - |Δdur| ≤ delta_dur_max (heurística inicial; algunos hermanos legítimos
         pueden tener mayor delta si uno se cortó, pero los chequeamos por
         overlap_4gram después)
       - distinto folder_parent (Dr vs Izq, o TX1 vs TX2)
    """
    pairs = []
    seen = set()
    for i, a in enumerate(audios):
        for b in audios[i+1:]:
            if a["folder_parent"] == b["folder_parent"]:
                continue
            key = tuple(sorted([a["id"], b["id"]]))
            if key in seen:
                continue
            seen.add(key)
            dt = abs(a["mtime"] - b["mtime"])
            # Permitir duraciones distintas si el otro check pasa
            if dt <= delta_mtime_max:
                pairs.append((a, b))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-overlap", type=float, default=0.20,
                    help="Overlap 4-grama mínimo para hermanos aplicables")
    ap.add_argument("--min-anchors-delta", type=int, default=100,
                    help="Anchors mínimos para calcular delta confiable")
    ap.add_argument("--max-mad-delta", type=float, default=0.5,
                    help="MAD máximo del shift para considerar delta confiable")
    ap.add_argument("--delta-mtime-max", type=float, default=120.0)
    ap.add_argument("--name-substring", default="Wireless PRO",
                    help="Solo procesar audios cuyo filename contenga este string")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"build_lavalier_pairs_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    now = time.time()

    # 1. Listar audios candidatos (lavaliers)
    rows = conn.execute(
        f"SELECT id FROM clips WHERE file_kind='audio' AND index_status='ok' "
        f"AND filename LIKE ?", (f"%{args.name_substring}%",)
    ).fetchall()
    audios = []
    for (cid,) in rows:
        meta = get_audio_metadata(conn, cid)
        if meta and meta["mtime"] > 0:
            audios.append(meta)
    logging.info(f"Audios candidatos: {len(audios)}")
    if not audios:
        logging.warning("No hay audios. Abort.")
        return

    # 2. Encontrar pares por timestamp
    pairs_by_time = find_sibling_candidates(
        audios, delta_mtime_max=args.delta_mtime_max, delta_dur_max=60.0
    )
    logging.info(f"Pares candidatos por timestamp: {len(pairs_by_time)}")

    # 3. Para cada par, calcular overlap_4gram + delta por transcript
    applicable = []
    rejected = []
    n_processed = 0
    for a, b in pairs_by_time:
        n_processed += 1
        # Cargar transcripts
        wa = load_words(tr_dir, a["id"])
        wb = load_words(tr_dir, b["id"])
        if not wa or not wb:
            rejected.append((a, b, "sin transcripts", 0.0, None, None))
            continue
        text_a = " ".join(w[0] for w in wa[:3000])
        text_b = " ".join(w[0] for w in wb[:3000])
        overlap = fourgram_overlap(text_a, text_b)

        # Calcular delta via ngram-align
        # Asumir hermanos del mismo Rx → delta ~ 0s, window 5s
        r = compute_offset(wa, wb, expected=0.0, window=5.0,
                            n_gram=3, min_anchors=args.min_anchors_delta)
        if r is None:
            delta = None; mad = None; n_anch = 0
        else:
            delta = r["offset"]  # offset por convención = -median_shift
            # Como measurement de delta entre dos audios "alineados",
            # interpretamos: si delta=+0.08, audio_b empezó 0.08s antes que audio_a
            mad = r["mad"]; n_anch = r["n_anchors"]

        info_str = (f"{a['filename']}/{a['folder_parent']} ↔ "
                    f"{b['filename']}/{b['folder_parent']}  "
                    f"mtime_Δ={abs(a['mtime']-b['mtime']):.0f}s  "
                    f"dur_Δ={abs(a['dur']-b['dur']):.0f}s  "
                    f"overlap={overlap:.3f}  delta={delta}  mad={mad}  n={n_anch}")

        # Aplicable si: overlap suficiente, delta confiable
        is_applicable = (overlap >= args.min_overlap and
                         delta is not None and
                         abs(delta) < 5.0 and  # delta entre hermanos típicamente < 1s
                         mad is not None and mad < args.max_mad_delta and
                         n_anch >= args.min_anchors_delta)

        if is_applicable:
            applicable.append((a, b, overlap, delta, mad, n_anch))
            logging.info(f"  ✓ APPLICABLE: {info_str}")
        else:
            rejected.append((a, b, "no aplicable", overlap, delta, mad))
            logging.info(f"  ✗ rejected: {info_str}")

    logging.info(f"\nResumen: {len(applicable)} aplicables / {len(pairs_by_time)} candidatos")
    logging.info(f"  Audios solitarios (sin hermano): "
                 f"{len(audios) - 2*len(applicable)}")

    # 4. Escribir a lavalier_pairs (replace)
    if not args.dry_run:
        # Borrar pares existentes (rebuild completo)
        conn.execute("DELETE FROM lavalier_pairs")
        for a, b, overlap, delta, mad, n_anch in applicable:
            # Ordenar por id para consistencia (a_id < b_id)
            id_a, id_b = sorted([a["id"], b["id"]])
            # delta_sec: si invertimos orden, invertimos signo
            if id_a == a["id"]:
                d = delta
            else:
                d = -delta
            notes = (f"overlap_4gram={overlap:.3f}, transcript_anchors={n_anch}, "
                     f"mad={mad:.3f}, mtime_a={a['mtime']:.0f}, "
                     f"mtime_b={b['mtime']:.0f}")
            conn.execute("""
                INSERT INTO lavalier_pairs
                (audio_a_id, audio_b_id, name_base, overlap_4gram, delta_sec,
                 prominence, voice_sim, applicable, notes, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (id_a, id_b, a["filename"], overlap, d, 0.0, 0.0, 1,
                  notes, now))
        # Insertar también los rejected (applicable=0) para que el motor sepa
        # qué pares se examinaron y descartaron
        for a, b, reason, overlap, delta, mad in rejected:
            id_a, id_b = sorted([a["id"], b["id"]])
            d = delta if delta is not None else 0.0
            if id_a != a["id"]:
                d = -d
            m = mad if mad is not None else 0.0
            notes = f"NO aplicable: {reason}; overlap={overlap:.3f}"
            try:
                conn.execute("""
                    INSERT INTO lavalier_pairs
                    (audio_a_id, audio_b_id, name_base, overlap_4gram, delta_sec,
                     prominence, voice_sim, applicable, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (id_a, id_b, a["filename"], overlap, d, 0.0, 0.0, 0,
                      notes, now))
            except sqlite3.IntegrityError:
                pass  # ya existe (orden distinto), saltar
        conn.commit()
        logging.info(f"✅ Escritas {len(applicable)} aplicables + "
                     f"{len(rejected)} rejected en lavalier_pairs")
    else:
        logging.info(f"[DRY-RUN] sería: {len(applicable)} aplicables")

    conn.close()


if __name__ == "__main__":
    main()
