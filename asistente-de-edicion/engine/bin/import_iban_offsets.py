#!/usr/bin/env python3
"""Convierte lo que AutoSyncAudio logro en Resolve en anclas del manifest.

PARA QUE
Cuando el correlador del motor no puede medir el reloj de una camara —su A1
esta tapado por la musica del evento y solo devuelve picos falsos— queda la
opcion de que lo intente Resolve. Caso fundador: Morsa 2026-08-02, la a6700 de
Iban, 97 clips sin un solo par de sync despues de cinco metodos distintos
(envelope en ventana ancha, consenso entre clips, camara contra camara, ventana
pre-concierto, y huella espectral). Ninguno converge: la musica es PERIODICA y
la correlacion encuentra un pico creible en cada compas repetido.

FLUJO
    1. En Resolve, SOBRE PROYECTO DUPLICADO (AutoSyncAudio no se deshace):
         dofile(".../merge_pool.lua")
         SALIDA = "<disco>/.cinema_assistant/resolve/<proyecto>_merge.json"
         dofile(".../leer_sync_merge.lua")
    2. Aqui: se leen esos offsets, se convierten al signo del manifest y se
       escriben como pares `method='resolve-merge'` en audio_sync_pairs.
    3. Despues: derive_chrono_sync los toma como anclas
       (--anchor-methods transcript,resolve-merge) y cubre el resto por reloj.

LO QUE NO HACE
No acepta cualquier cosa que Resolve devuelva. Si los offsets implican skews de
camara que no concuerdan entre si mas alla de `--tolerancia`, NO escribe nada:
un skew equivocado correria decenas de clips y el error apareceria semanas
despues, en el corte. Mejor sin sync y dicho, que con sync y mentido.

CONVENCION DE SIGNOS (metodologia/merge-media-pool.md):
    offset_manifest_seg == -offset_resolve_muestras / sample_rate

Uso:
    python3 bin/import_iban_offsets.py --root <disco> \\
        --merge-json <disco>/.cinema_assistant/resolve/<p>_merge.json \\
        --grupo Iban [--tolerancia 1.0] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.guards import assert_selected, report_done      # noqa: E402
from lib import manifest  # noqa: E402

SAMPLE_RATE = 48000.0


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resoluble: {root_arg}")


def epoch(ct: str, utc_offset: int) -> float | None:
    if not ct:
        return None
    try:
        d = dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=utc_offset)))
    return d.timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--merge-json", required=True,
                    help="Salida de resolve/leer_sync_merge.lua")
    ap.add_argument("--grupo", default=None,
                    help="parent_folder de la camara a importar (ej. Iban). Si no "
                         "se pasa, se importan TODAS las camaras que aparezcan en "
                         "el JSON, cada una con su propia guarda de coherencia. "
                         "Era obligatorio de cuando esto era el rescate de UNA "
                         "camara; el circuito sirve para el lote entero.")
    ap.add_argument("--sample-rate", type=float, default=SAMPLE_RATE)
    ap.add_argument("--tolerancia", type=float, default=1.0,
                    help="Desviacion maxima (s) entre los skews implicados "
                         "para aceptar el lote.")
    ap.add_argument("--utc-offset", type=int, default=-6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Sin manifest en {db}")
    datos = json.loads(Path(args.merge_json).read_text(encoding="utf-8"))
    conn = manifest.conectar(str(db))

    if args.grupo:
        porRuta = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT path, id, creation_time, duration_sec FROM clips "
            "WHERE file_kind='video' AND parent_folder=?", (args.grupo,))}
    else:
        # Sin --grupo se importa el lote entero. La guarda de coherencia sigue
        # siendo POR CAMARA (cada cuerpo tiene su propio reloj), asi que el
        # script se relanza a si mismo una vez por camara presente en el JSON y
        # cada una se acepta o se rechaza por separado. Que una falle no puede
        # tumbar a las demas.
        import subprocess

        presentes = {r[0] for r in conn.execute(
            "SELECT DISTINCT parent_folder FROM clips WHERE file_kind='video'")}
        rutas_json = {c.get("path") for c in datos.get("clips", [])
                      if c.get("ligados", 0) > 0}
        grupos = sorted({g for g in presentes if any(
            r and f"/{g}/" in r for r in rutas_json)})
        if not grupos:
            print("Ninguna camara del JSON tiene clips ligados. Nada que importar.")
            return 0
        print(f"Camaras en el JSON: {', '.join(grupos)}\n")
        peor = 0
        for g in grupos:
            print(f"===== {g} =====")
            cmd = [sys.executable, __file__, "--root", str(root),
                   "--merge-json", args.merge_json, "--grupo", g,
                   "--tolerancia", str(args.tolerancia),
                   "--sample-rate", str(args.sample_rate),
                   "--utc-offset", str(args.utc_offset)]
            if args.dry_run:
                cmd.append("--dry-run")
            peor = max(peor, subprocess.run(cmd).returncode)
            print("")
        return peor
    audios = {r[0]: (r[1], (r[2] or 0) - (r[3] or 0)) for r in conn.execute(
        "SELECT path, id, mtime, duration_sec FROM clips WHERE file_kind='audio'")}

    ligados = [c for c in datos.get("clips", [])
               if c.get("ligados", 0) > 0 and c.get("path") in porRuta]
    assert_selected(ligados, f"clips de '{args.grupo}' con audio ligado por Resolve",
                    filters={"--merge-json": args.merge_json, "--grupo": args.grupo},
                    hint="Si Resolve no logro sincronizar esa camara, este paso no "
                         "tiene nada que importar — y eso es un resultado valido: "
                         "sus clips quedan como cobertura sin lavalier.")

    # Skew implicado por cada clip: real = wav_start - offset ; skew = ep - real
    filas, skews = [], []
    for c in ligados:
        vid, ct, _dur = porRuta[c["path"]]
        ep = epoch(ct, args.utc_offset)
        if ep is None:
            continue
        for apath, off_muestras in zip(c.get("audios", []),
                                       c.get("offsets_muestras", [])):
            if off_muestras is None or apath not in audios:
                continue
            aid, a_start = audios[apath]
            off_seg = -float(off_muestras) / args.sample_rate   # signo del manifest
            real = a_start - off_seg
            skews.append(ep - real)
            filas.append((vid, aid, off_seg, c["clip"], apath))

    if not filas:
        sys.exit("Ningun offset utilizable en el JSON (¿rutas de audio distintas?).")

    med = statistics.median(skews)
    disp = statistics.pstdev(skews) if len(skews) > 1 else 0.0
    print(f"Clips con audio ligado: {len(ligados)} | offsets leidos: {len(filas)}")
    print(f"  skew implicado para '{args.grupo}': {med:+.3f} s "
          f"(desv {disp:.3f} s, rango {min(skews):+.2f}..{max(skews):+.2f})")

    if disp > args.tolerancia:
        print()
        print(f"⚠⚠ NO SE IMPORTA NADA: la desviacion ({disp:.2f} s) supera la "
              f"tolerancia ({args.tolerancia:.2f} s).")
        print("   Los offsets de Resolve no concuerdan entre si, asi que no "
              "describen un reloj: describen ruido.")
        print("   Un skew equivocado correria todos los clips de esa camara y el "
              "error apareceria en el corte, semanas despues.")
        report_done("import_iban_offsets", importados=0, skew_desv=round(disp, 3))
        return 3

    # Se descartan los outliers antes de escribir: basta uno malo para envenenar
    # el skew que derive_chrono_sync medira despues sobre estas anclas.
    buenas = [f for f, s in zip(filas, skews) if abs(s - med) <= args.tolerancia]
    print(f"  anclas a escribir: {len(buenas)} (descartadas {len(filas)-len(buenas)} "
          "por alejarse de la mediana)")

    if args.dry_run:
        for vid, aid, off, nombre, apath in buenas[:10]:
            print(f"    {nombre:<20} offset={off:+9.3f}s  <- {Path(apath).name}")
        print("(dry-run: nada escrito)")
        report_done("import_iban_offsets", importados=0, dry_run=1)
        return 0

    now = time.time()
    n = 0
    for vid, aid, off, nombre, _apath in buenas:
        ya = conn.execute(
            "SELECT 1 FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
            (vid, aid)).fetchone()
        if ya:
            continue
        conn.execute(
            "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, offset_sec, "
            "confidence, method, notes, created_at) VALUES (?,?,?,?,?,?,?)",
            (vid, aid, off, 0.90, "resolve-merge",
             "Medido por AutoSyncAudio de Resolve e importado por "
             "import_iban_offsets.py; el correlador del motor no pudo con esta "
             "camara (musica del evento sobre el A1).", now))
        n += 1
    conn.commit()
    print(f"  escritas {n} anclas nuevas (method='resolve-merge').")
    print()
    print("Siguiente paso:")
    print(f"  python3 ~/cinema-assistant/bin/derive_chrono_sync.py --root {root} \\")
    print("    --per-chain --anchor-methods 'transcript,transcript-longwin,resolve-merge'")
    report_done("import_iban_offsets", importados=n, skew=round(med, 3),
                skew_desv=round(disp, 3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
