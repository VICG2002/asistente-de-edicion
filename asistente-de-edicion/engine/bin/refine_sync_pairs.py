#!/usr/bin/env python3
"""Refina offsets de sync existentes con resolución sub-segundo.

Caso fundador (Zezzions 2026-05-26): usuario reportó "el audio del
interlocutor es correcto pero el sync no coincide exactamente". Los
métodos de sync histórico (transcript/phrases/questions con jitter
~200ms, envelope/chromaprint con ~100-125ms) producen offsets cerca
del real pero no precisos. Este script:

  1. Lee cada audio_sync_pair existente.
  2. Toma offset_sec actual como punto de partida.
  3. Cross-correlate ENVELOPE log-RMS de A1 (audio cámara) vs A2
     (lavalier externo) en banda de voz 300-3400 Hz, con resolución 10ms.
  4. Busca peak dentro de ±SEARCH_WINDOW segundos del offset actual.
  5. Si prominence del peak supera el umbral, aplica el delta.

NO toca pairs con method='manual' (curado por el usuario).
NO toca pairs con conf >= 0.95 a menos que --force (asume que ya están bien).

Uso:
    bin/refine_sync_pairs.py --root <disk> --dry-run     # ver propuestas
    bin/refine_sync_pairs.py --root <disk>               # aplicar
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

from lib.sync_refiner import refine_offset
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
    ap.add_argument("--min-prominence", type=float, default=0.30,
                    help="prominence del peak para aceptar refinement")
    ap.add_argument("--min-delta-ms", type=float, default=30.0,
                    help="delta mínimo (ms) para aplicar — menor se ignora")
    ap.add_argument("--max-delta-ms", type=float, default=2500.0,
                    help="delta máximo (ms) para aplicar — mayor probable falso")
    ap.add_argument("--search-window", type=float, default=2.0,
                    help="±window seg de búsqueda alrededor del offset actual")
    ap.add_argument("--analysis-dur", type=float, default=30.0,
                    help="duración del tramo de audio a analizar (s)")
    ap.add_argument("--force", action="store_true",
                    help="incluir pairs con conf >= 0.95")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-voice-bandpass", action="store_true",
                    help="Correlaciona full-spectrum en vez de filtrar 300-3400 Hz. "
                         "Para musica/concierto el filtro de voz borra bajo y kick. "
                         "Leer el aviso de refine_offset(): en concierto NINGUN modo "
                         "de este refinador es de fiar.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"refine_sync_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    where_extra = ""
    if not args.force:
        where_extra = "AND sp.confidence < 0.95 "

    # Fix iter2 (2026-05-26): respetar también method que terminen con '-locked'
    # (transcript-rescue-locked, chrono-derived-locked, etc.) — son blindados.
    rows = conn.execute(f"""
        SELECT sp.id, cv.filename, cv.path, cv.duration_sec,
               ca.filename, ca.path, ca.duration_sec,
               sp.offset_sec, sp.confidence, sp.method, sp.notes
        FROM audio_sync_pairs sp
        JOIN clips cv ON cv.id = sp.video_clip_id
        JOIN clips ca ON ca.id = sp.audio_clip_id
        WHERE sp.confidence >= 0.30
          {where_extra}
          AND sp.method != 'manual'
          AND (sp.method NOT LIKE '%-locked')
        ORDER BY sp.confidence DESC
    """).fetchall()
    logging.info(f"Sync pairs candidatos a refinar: {len(rows)}")

    n_applied = 0
    n_skipped_low_prom = 0
    n_skipped_small_delta = 0
    n_skipped_large_delta = 0
    n_failed = 0
    now = time.time()

    for sp_id, vfn, vpath, vdur, afn, apath, adur, off, conf, method, notes in rows:
        logging.info(f"\n[sp{sp_id}] {vfn} ↔ {Path(apath).parent.name}/{afn}  "
                     f"off={off:+.3f}s conf={conf:.2f} method={method}")
        r = refine_offset(Path(vpath), Path(apath), off,
                          vdur or 0, adur or 0,
                          window=args.search_window,
                          analysis_dur=args.analysis_dur,
                          voice_bandpass=not args.no_voice_bandpass)
        if r is None:
            logging.info(f"  → falló (audio insuficiente o peak inválido)")
            n_failed += 1
            continue
        delta_ms = r.delta_sec * 1000
        # extraer prominence del notes
        prom = 0.0
        for tok in r.notes.split(","):
            if "prominence=" in tok:
                try:
                    prom = float(tok.split("=")[1])
                except Exception:
                    pass
        logging.info(f"  Δ={delta_ms:+.0f}ms  new_offset={r.new_offset:+.3f}s  "
                     f"prom={prom:.3f}  peak_norm={r.correlation_peak:.3f}  conf={r.confidence:.2f}")

        if prom < args.min_prominence:
            logging.info(f"  ✗ prominence {prom:.3f} < {args.min_prominence} — skip")
            n_skipped_low_prom += 1
            continue
        if abs(delta_ms) < args.min_delta_ms:
            logging.info(f"  · Δ {delta_ms:+.0f}ms < {args.min_delta_ms}ms — ya está bien, skip")
            n_skipped_small_delta += 1
            continue
        if abs(delta_ms) > args.max_delta_ms:
            logging.info(f"  ✗ Δ {delta_ms:+.0f}ms > {args.max_delta_ms}ms — sospechoso, skip")
            n_skipped_large_delta += 1
            continue

        # Aplicar
        new_notes = (notes or "") + f" | refined +{delta_ms:+.0f}ms (prom={prom:.2f})"
        if args.dry_run:
            logging.info(f"  [DRY] aplicaría {off:+.3f} → {r.new_offset:+.3f}")
        else:
            conn.execute(
                "UPDATE audio_sync_pairs SET offset_sec=?, notes=?, created_at=? WHERE id=?",
                (r.new_offset, new_notes, now, sp_id)
            )
            n_applied += 1
            logging.info(f"  ✓ aplicado: {off:+.3f} → {r.new_offset:+.3f}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nrefine_sync: aplicados={n_applied}, "
                 f"skip(prom_baja)={n_skipped_low_prom}, "
                 f"skip(Δ_pequeño)={n_skipped_small_delta}, "
                 f"skip(Δ_grande)={n_skipped_large_delta}, "
                 f"falló={n_failed}")


if __name__ == "__main__":
    main()
