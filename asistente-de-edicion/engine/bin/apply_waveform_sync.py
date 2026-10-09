#!/usr/bin/env python3
"""Aplica sync por waveform A1↔lavalier a pairs existentes.

Doctrina (Zezzions iter9.5, 2026-05-27): el sync por waveform usando A1
de cámara como referencia es FÍSICAMENTE preciso porque no depende del
contenido lingüístico. Aplicar como método primario para entrevistas
donde el transcript es poco confiable o donde el waveform da mejor
señal.

Estrategia segura:
  1. Para cada sync pair existente, calcular waveform offset.
  2. Si waveform tiene prominence >= 0.30 Y peak_norm >= 0.10:
       comparar contra BD offset.
       Si |Δ| < 30ms: no tocar (BD ya está bien).
       Si |Δ| < 5s: ACTUALIZAR con waveform offset (más confiable físicamente).
       Si |Δ| >= 5s: NO TOCAR (sospechoso, marcar para revisión).
  3. Si waveform tiene prominence baja: NO TOCAR (BD probablemente mejor).

Uso:
    bin/apply_waveform_sync.py --root <disk>
    bin/apply_waveform_sync.py --root <disk> --dry-run
    bin/apply_waveform_sync.py --root <disk> --pair-ids 198,217,200
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

from lib.waveform_sync_full import waveform_sync_full
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists(): return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1: return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--pair-ids", help="CSV ids específicos a procesar")
    ap.add_argument("--audio-like", default="%Wireless PRO%")
    ap.add_argument("--video-analysis-dur", type=float, default=60.0)
    ap.add_argument("--min-prominence", type=float, default=0.30)
    ap.add_argument("--min-peak-norm", type=float, default=0.10)
    ap.add_argument("--max-delta-update", type=float, default=5.0,
                    help="Diferencia máxima (s) vs BD para aceptar update")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"apply_waveform_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    now = time.time()

    # Snapshot
    if not args.dry_run:
        conn.execute("DROP TABLE IF EXISTS audio_sync_pairs_iter9_5_pre_waveform")
        conn.execute(
            "CREATE TABLE audio_sync_pairs_iter9_5_pre_waveform "
            "AS SELECT * FROM audio_sync_pairs"
        )
        logging.info(f"✓ Snapshot iter9_5_pre_waveform")

    # Detectar entrevistas DUALES (n_personas >= 2 in face_catalog).
    # En duales, A1 mezcla voces y el lavalier captura solo una — waveform
    # cross-correlation puede dar peak espurio. Skip para tales pares.
    dual_videos = set()
    try:
        for (vid,) in conn.execute("""
            SELECT fd.clip_id FROM face_detections fd
            JOIN face_identities fi ON fi.detection_id=fd.id
            JOIN face_catalog fc ON fc.identity_id=fi.identity_id
            WHERE fc.canonical_name NOT LIKE 'Persona_%'
            GROUP BY fd.clip_id
            HAVING COUNT(DISTINCT fc.identity_id) >= 2
        """):
            dual_videos.add(vid)
    except sqlite3.OperationalError:
        pass
    if dual_videos:
        logging.info(f"Videos con DUAL interviewees (skip waveform): {len(dual_videos)}")
        # También skip clips conocidos como problemáticos
    where_clauses = ["ca.rel_path LIKE ?"]
    params = [args.audio_like]
    if args.pair_ids:
        ids = ",".join(args.pair_ids.split(","))
        where_clauses.append(f"sp.id IN ({ids})")
    where = " AND ".join(where_clauses)

    rows = conn.execute(f"""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec, sp.method,
               v.path, v.duration_sec, v.filename,
               ca.path, ca.duration_sec
        FROM audio_sync_pairs sp
        JOIN clips v ON v.id=sp.video_clip_id
        JOIN clips ca ON ca.id=sp.audio_clip_id
        WHERE {where}
        ORDER BY v.filename, ca.rel_path
    """, params).fetchall()

    logging.info(f"Pairs a procesar: {len(rows)}")
    n_updated = 0
    n_skipped_low_conf = 0
    n_skipped_big_delta = 0
    n_already_ok = 0
    n_failed = 0

    for sp_id, vid, aid, off_bd, method, vpath, vdur, vfn, apath, adur in rows:
        if vid in dual_videos:
            logging.info(f"  ⊘ sp={sp_id} {vfn}: entrevista DUAL — skip waveform "
                         f"(A1 mezcla voces, lavalier captura solo una)")
            n_skipped_low_conf += 1
            continue
        r = waveform_sync_full(Path(vpath), Path(apath), vdur or 0, adur or 0,
                                video_analysis_dur=args.video_analysis_dur)
        if r is None:
            logging.info(f"  ✗ sp={sp_id} {vfn}: waveform falló")
            n_failed += 1
            continue
        off_wave = r["offset_sec"]
        prom = r["prominence"]
        pk_norm = r["peak_norm"]
        delta = off_wave - off_bd

        if prom < args.min_prominence or pk_norm < args.min_peak_norm:
            logging.info(f"  ⊘ sp={sp_id} {vfn}: prom={prom:.2f} pk={pk_norm:.3f} "
                         f"(BD={off_bd:+.3f}, wave={off_wave:+.3f}) — confianza baja")
            n_skipped_low_conf += 1
            continue

        if abs(delta) < 0.030:
            logging.info(f"  · sp={sp_id} {vfn}: BD={off_bd:+.3f}, wave={off_wave:+.3f} "
                         f"(Δ={delta:+.3f}s) — sin cambio")
            n_already_ok += 1
            continue

        if abs(delta) > args.max_delta_update:
            logging.info(f"  ⊘ sp={sp_id} {vfn}: |Δ|={abs(delta):.2f}s > "
                         f"{args.max_delta_update} (BD={off_bd:+.3f}, wave={off_wave:+.3f}) "
                         f"prom={prom:.2f} — sospechoso, no actualizar")
            n_skipped_big_delta += 1
            continue

        # Actualizar
        new_notes = (f" [iter9.5 waveform: {off_bd:+.3f}→{off_wave:+.3f} "
                     f"prom={prom:.2f} pk={pk_norm:.3f}]")
        if args.dry_run:
            logging.info(f"  [DRY] sp={sp_id} {vfn}: BD={off_bd:+.3f} → {off_wave:+.3f} "
                         f"(Δ={delta:+.3f}s) prom={prom:.2f}")
        else:
            conn.execute(
                "UPDATE audio_sync_pairs SET offset_sec=?, "
                "notes=COALESCE(notes,'') || ?, created_at=? WHERE id=?",
                (off_wave, new_notes, now, sp_id)
            )
            logging.info(f"  ✓ sp={sp_id} {vfn}: {off_bd:+.3f} → {off_wave:+.3f} "
                         f"(Δ={delta:+.3f}s) prom={prom:.2f}")
        n_updated += 1

    if not args.dry_run:
        conn.commit()

    logging.info(f"\n✅ Resumen:")
    logging.info(f"  Actualizados:       {n_updated}")
    logging.info(f"  Sin cambio (BD OK): {n_already_ok}")
    logging.info(f"  Skip prom baja:     {n_skipped_low_conf}")
    logging.info(f"  Skip Δ grande:      {n_skipped_big_delta}")
    logging.info(f"  Falló waveform:     {n_failed}")
    conn.close()


if __name__ == "__main__":
    main()
