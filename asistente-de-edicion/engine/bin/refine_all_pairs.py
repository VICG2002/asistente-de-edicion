#!/usr/bin/env python3
"""Refinement final de TODOS los pairs (incluso locked) con ventana fina ±2s.

Caso fundador (Zezzions iter2 2026-05-26): "Sigue habiendo delays" reportado
por el usuario. Los pairs locked se conservan pero pueden tener offsets
imprecisos (±200-500ms) en relación al lip-sync visible.

Este refiner:
  - Procesa pares locked también (a diferencia de refine_sync_pairs.py).
  - Ventana de búsqueda fina (±2s alrededor del offset actual).
  - Usa envelope log-RMS de banda voz 300-3400 Hz.
  - Aplica delta solo si prominencia ≥ umbral (más permisivo: 0.15).
  - Mantiene el `method` original (no cambia 'transcript-rescue-locked' a otro).
  - Solo aplica cambios < 1s (sospechosos si Δ grande).

Uso:
    bin/refine_all_pairs.py --root <disk>
    bin/refine_all_pairs.py --root <disk> --max-delta-ms 1500
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


def pares_a_refinar(conn, include_locked_like: str | None, rehacer: bool = False):
    """Los pares que esta pasada va a medir.

    Un par ya medido por esta pasada lleva `iter2 ...` en las notas (refinado,
    confirmado sin cambio, debil o descartado) y no se vuelve a medir: correr el
    refinador de nuevo para sumar material (Asistente, 2026-09-29, segundo dia)
    re-refinaba los 60 pares del primero, que la ventana larga ya habia
    confirmado. `rehacer` los vuelve a medir a proposito.
    """
    # Fix iter3: respetar también 'manual-from-drt' y '*-locked' (NO los refinamos)
    return conn.execute("""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec, sp.method,
               sp.confidence, sp.notes, cv.path, cv.duration_sec, ca.path, ca.duration_sec,
               cv.filename, ca.filename
        FROM audio_sync_pairs sp
        JOIN clips cv ON cv.id=sp.video_clip_id
        JOIN clips ca ON ca.id=sp.audio_clip_id
        WHERE sp.method != 'manual'
          AND sp.method != 'manual-from-drt'
          AND ((sp.method NOT LIKE '%-locked') OR (? IS NOT NULL AND sp.method LIKE ?))
          AND (? = 1 OR IFNULL(sp.notes, '') NOT LIKE '%iter2 %')
        ORDER BY sp.confidence DESC
    """, (include_locked_like, include_locked_like or "", 1 if rehacer else 0)).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--rehacer", action="store_true",
                    help="volver a medir tambien los pares que esta pasada ya midio")
    ap.add_argument("--min-prominence", type=float, default=0.15)
    ap.add_argument("--min-delta-ms", type=float, default=30.0,
                    help="Delta mínimo para aplicar (descartar cambios despreciables)")
    ap.add_argument("--max-delta-ms", type=float, default=1500.0,
                    help="Delta máximo (mayor es sospechoso, mantener original)")
    ap.add_argument("--search-window", type=float, default=2.0,
                    help="±window seg de búsqueda alrededor del offset existente")
    ap.add_argument("--analysis-dur", type=float, default=45.0)
    ap.add_argument("--include-locked-like", default=None,
                    help="Patrón SQL LIKE de methods '-locked' que SÍ se refinan "
                         "(ej. 'chrono-%%'). Los locked son derivaciones blindadas "
                         "contra refinement CIEGO, pero las derivadas por reloj "
                         "(jitter ±1s) se benefician de una pasada explícita. "
                         "Los 'manual*' NUNCA se refinan.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-voice-bandpass", action="store_true",
                    help="Correlaciona full-spectrum en vez de filtrar 300-3400 Hz. "
                         "Para musica/concierto el filtro de voz borra bajo y kick. "
                         "Leer el aviso de refine_offset(): en concierto NINGUN modo "
                         "de este refinador es de fiar.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"refine_all_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    rows = pares_a_refinar(conn, args.include_locked_like, args.rehacer)
    logging.info(f"Pairs a refinar: {len(rows)}")

    def anotar(sp_id, notes, texto):
        """Medido y no aplicado tambien deja constancia (asi no se re-mide)."""
        if not args.dry_run:
            conn.execute("UPDATE audio_sync_pairs SET notes=? WHERE id=?",
                         ((notes or "") + f" | iter2 {texto}", sp_id))

    n_applied = 0
    n_low_prom = 0
    n_too_small = 0
    n_too_large = 0
    n_failed = 0
    now = time.time()

    for (sp_id, vid, aid, off, method, conf, notes, vpath, vdur,
         apath, adur, vfn, afn) in rows:
        r = refine_offset(Path(vpath), Path(apath), off, vdur or 0, adur or 0,
                          window=args.search_window, analysis_dur=args.analysis_dur,
                          voice_bandpass=not args.no_voice_bandpass)
        if r is None:
            n_failed += 1
            continue
        delta_ms = r.delta_sec * 1000
        prom = 0.0
        for tok in r.notes.split(","):
            if "prominence=" in tok:
                # `except:` a secas atrapaba KeyboardInterrupt y SystemExit, asi
                # que Ctrl-C no funcionaba dentro de este bucle. Solo interesan
                # los fallos de parseo del valor.
                try:
                    prom = float(tok.split("=")[1])
                except (ValueError, IndexError):
                    pass

        if prom < args.min_prominence:
            n_low_prom += 1
            anotar(sp_id, notes, f"debil (prom={prom:.2f})")
            continue
        if abs(delta_ms) < args.min_delta_ms:
            n_too_small += 1
            anotar(sp_id, notes, f"ok {delta_ms:+.0f}ms (prom={prom:.2f})")
            continue
        if abs(delta_ms) > args.max_delta_ms:
            logging.info(f"  ⊘ {vfn} sp{sp_id}: Δ={delta_ms:+.0f}ms > max {args.max_delta_ms} — descartar")
            n_too_large += 1
            anotar(sp_id, notes, f"descartado {delta_ms:+.0f}ms > max (prom={prom:.2f})")
            continue

        # Aplicar (sin cambiar method, manteniendo el locked si aplica)
        new_notes = (notes or "") + f" | iter2 refined {delta_ms:+.0f}ms (prom={prom:.2f})"
        if args.dry_run:
            logging.info(f"  [DRY] {vfn} sp{sp_id} method={method}: "
                         f"Δ={delta_ms:+.0f}ms prom={prom:.2f}: {off:+.3f}→{r.new_offset:+.3f}")
        else:
            conn.execute(
                "UPDATE audio_sync_pairs SET offset_sec=?, notes=?, created_at=? WHERE id=?",
                (r.new_offset, new_notes, now, sp_id)
            )
            n_applied += 1
            logging.info(f"  ✓ {vfn} sp{sp_id} {method}: Δ={delta_ms:+.0f}ms "
                         f"({off:+.3f}→{r.new_offset:+.3f}) prom={prom:.2f}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nResumen: aplicados={n_applied}, low_prom={n_low_prom}, "
                 f"too_small={n_too_small}, too_large={n_too_large}, falló={n_failed}")


if __name__ == "__main__":
    main()
