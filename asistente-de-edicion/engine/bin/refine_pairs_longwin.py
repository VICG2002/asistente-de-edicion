#!/usr/bin/env python3
"""Re-medición de offsets video↔WAV con ventana larga + etapa fina.

Caso MAB (2026-06-12): el A1 de la cámara de Ayan ES el feed del lavalier —
"es prácticamente el mismo audio" (usuario). Contra esa señal, la correlación
de ventana larga debe dar prominence altísima y precisión de milisegundos;
cualquier pair con delay visible ahí es un offset mal medido por los métodos
cortos (transcript jitter, chrono, ventana de 45s con música).

Recorre TODOS los audio_sync_pairs cuyo audio matchea --audio-like y re-mide
con lib del verificador multicam (envelope sobre hasta 150s + PCM fino).
Aplica solo si prominence >= umbral del grupo. Respeta method='manual%'.

Uso:
    bin/refine_pairs_longwin.py --root <disk> [--audio-like '%wireless%']
        [--min-prom-lavfeed 0.5] [--min-prom-scratch 0.45]
        [--lavfeed-group Ayan] [--max-correction 3.0] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from verify_multicam_deltas import measure_delta  # noqa: E402
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
    ap.add_argument("--audio-like", default="%wireless%")
    ap.add_argument("--lavfeed-group", default="Ayan",
                    help="parent_folder de la cámara con el RX al A1 (señal "
                         "≈ WAV): umbral de prominence más exigente y "
                         "expectativa de corrección casi total.")
    ap.add_argument("--min-prom-lavfeed", type=float, default=0.50)
    ap.add_argument("--min-prom-scratch", type=float, default=0.45)
    ap.add_argument("--max-correction", type=float, default=3.0)
    ap.add_argument("--only-interviews", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    conn = manifest.conectar(str(root / ".cinema_assistant" / "manifest.sqlite"))
    extra = ""
    if args.only_interviews:
        extra = ("AND v.id IN (SELECT clip_id FROM clip_descriptions "
                 "WHERE category LIKE 'entrevista%') ")
    rows = conn.execute(f"""
        SELECT sp.id, sp.offset_sec, sp.method, v.path, v.duration_sec,
               v.parent_folder, v.filename, a.path, a.duration_sec, a.filename
        FROM audio_sync_pairs sp
        JOIN clips v ON v.id = sp.video_clip_id
        JOIN clips a ON a.id = sp.audio_clip_id
        WHERE lower(a.filename) LIKE ? AND sp.method NOT LIKE 'manual%'
          AND sp.method NOT LIKE '%-longwin'
          AND IFNULL(sp.notes, '') NOT LIKE '%[longwin ok%' {extra}
        ORDER BY v.parent_folder, v.filename
    """, (args.audio_like,)).fetchall()
    print(f"Pares a re-medir: {len(rows)}")

    now = time.time()
    n_app = n_weak = n_big = n_none = n_ok = 0
    deltas_lavfeed = []
    for (pid, off, method, vp, vdur, grp, vfn, apath, adur, afn) in rows:
        r = measure_delta(Path(vp), Path(apath), vdur or 0, adur or 0, off)
        if r is None:
            n_none += 1
            continue
        new_off, prom, fine = r
        min_prom = (args.min_prom_lavfeed if grp == args.lavfeed_group
                    else args.min_prom_scratch)
        corr = new_off - off
        if prom < min_prom:
            n_weak += 1
            continue
        if abs(corr) > args.max_correction:
            n_big += 1
            print(f"  ⚠ {vfn}: corrección {corr:+.2f}s > max — NO aplicada, revisar")
            continue
        if grp == args.lavfeed_group:
            deltas_lavfeed.append(corr)
        if abs(corr) >= 0.02:
            tag = "[DRY] " if args.dry_run else ""
            print(f"  {tag}✓ {vfn} ({grp}) vs {afn}: {off:+.3f} → {new_off:+.3f} "
                  f"(Δ{corr * 1000:+.0f}ms, prom={prom:.2f}, fine={'sí' if fine else 'no'})")
            if not args.dry_run:
                conn.execute(
                    "UPDATE audio_sync_pairs SET offset_sec=?, "
                    "method=method || '-longwin', confidence=0.95, "
                    "notes=IFNULL(notes,'') || ? WHERE id=?",
                    (new_off, f" [longwin {off:+.2f}->{new_off:+.2f} prom={prom:.2f}]", pid))
            n_app += 1
        else:
            # Medido y ya estaba bien: eso TAMBIEN es una validacion, y se
            # anota. Sin la nota, verify_lav_offsets seguia diciendo "ENTREVISTA
            # SIN VALIDAR — correr refine_pairs_longwin", y correrlo otra vez no
            # dejaba rastro: un consejo en bucle (Asistente, 2026-09-28: 24 de
            # 26 pares confirmados y ninguno registrado).
            if not args.dry_run:
                conn.execute(
                    "UPDATE audio_sync_pairs SET notes=IFNULL(notes,'') || ? WHERE id=?",
                    (f" [longwin ok Δ{corr * 1000:+.0f}ms prom={prom:.2f}]", pid))
            n_ok += 1
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"\nAplicados: {n_app} | confirmados sin cambio: {n_ok} | "
          f"prom débil (sin tocar): {n_weak} | "
          f"corrección sospechosa: {n_big} | sin medición: {n_none}")
    if deltas_lavfeed:
        import statistics
        print(f"Correcciones en grupo lavfeed: media={statistics.mean(deltas_lavfeed) * 1000:+.0f}ms "
              f"máx={max(abs(d) for d in deltas_lavfeed) * 1000:.0f}ms n={len(deltas_lavfeed)}")


if __name__ == "__main__":
    main()
