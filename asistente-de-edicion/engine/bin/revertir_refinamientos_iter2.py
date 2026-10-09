#!/usr/bin/env python3
"""Deshace los refinamientos que escribio `refine_all_pairs.py` con el signo malo.

QUE PASO (2026-08-13)
`lib/sync_refiner.refine_offset` centraba la busqueda del pico en
`v_start + current_offset` cuando la convencion del motor —`offset =
audio_start - video_start`— obliga a `v_start - current_offset`. Es decir, a
2x offset del sitio correcto.

Para los pares de esta casa, que son lavaliers CONTINUOS con offsets de decenas
o miles de segundos, el efecto no fue "medir 2x offset de mas": fue que
`a_center` se iba a negativo, `a_start = max(0.0, ...)` lo recortaba a 0, y el
refinador acababa correlacionando el tramo de video contra los PRIMEROS segundos
del WAV — donde la coincidencia real no esta. Lo que devolvia era un pico
espurio, acotado solo por `--search-window`, y `refine_all_pairs.py` lo escribia
si superaba `--min-prominence` (0.15 por defecto). Ruido con cara de medida.

Medido en T9 el 2026-08-13: 33 pares afectados, todos con offsets entre -22 s y
-3519 s. Desplazamientos de hasta 4.19 s en MAS ALLA DEL BALON (27 pares) y
FILM CLUB CAFE (6).

POR QUE SE PUEDE DESHACER SIN MEDIR NADA
`refine_all_pairs.py` deja el delta aplicado escrito en la nota:

    ... | iter2 refined +1830ms (prom=0.42)

Asi que el valor anterior es aritmetica exacta, no una reconstruccion:

    offset_previo = offset_actual - delta

No hace falta el material, ni Whisper, ni el disco de origen: solo el manifest.

QUE HACE Y QUE NO
Revierte y marca la nota. NO vuelve a refinar: para eso se corre
`refine_all_pairs.py` con el codigo ya corregido, que es un paso aparte y
deliberado. Primero dejar los datos como estaban, despues decidir si se refina.

Uso:
    python3 bin/revertir_refinamientos_iter2.py --root <disco/proyecto>
    python3 bin/revertir_refinamientos_iter2.py --buscar-bajo /Volumes/T9_DIEZ50
    python3 bin/revertir_refinamientos_iter2.py --buscar-bajo /Volumes/T9 --aplicar

Sin `--aplicar` no escribe nada: enseña la tabla y sale.
Con `--aplicar` copia el manifest a `manifest.sqlite.antes-de-revertir-<fecha>`
antes de tocarlo.
"""

from __future__ import annotations

import argparse
import glob
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from lib import manifest  # noqa: E402

# El formato lo escribe refine_all_pairs.py:
#     new_notes = (notes or "") + f" | iter2 refined {delta_ms:+.0f}ms (prom={prom:.2f})"
MARCA = re.compile(r"\s*\|\s*iter2 refined ([+-]?\d+)ms \(prom=([0-9.]+)\)")


def manifests_bajo(raiz: Path) -> list[Path]:
    patron = str(raiz / "*" / "*" / ".cinema_assistant" / "manifest.sqlite")
    hallados = [Path(p) for p in glob.glob(patron)]
    directo = raiz / ".cinema_assistant" / "manifest.sqlite"
    if directo.exists():
        hallados.append(directo)
    return sorted(set(hallados))


def afectados(db: Path) -> list[dict]:
    """Los pares con marca de refinamiento, en SOLO LECTURA."""
    conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        filas = conn.execute(
            """SELECT p.id, p.offset_sec, p.notes, p.method, p.confidence,
                      (SELECT filename FROM clips WHERE id=p.video_clip_id),
                      (SELECT filename FROM clips WHERE id=p.audio_clip_id)
               FROM audio_sync_pairs p
               WHERE p.notes LIKE '%iter2 refined%'
               ORDER BY p.id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []                      # proyecto sin tabla de sync
    finally:
        conn.close()

    fuera = []
    for pid, off, notas, metodo, conf, vfn, afn in filas:
        m = MARCA.search(notas or "")
        if not m:
            # Hay marca pero no se puede leer el delta: sin el, revertir seria
            # adivinar. Se reporta y no se toca.
            fuera.append({"id": pid, "off": off, "delta": None, "notas": notas,
                          "metodo": metodo, "conf": conf, "vfn": vfn, "afn": afn})
            continue
        delta = int(m.group(1)) / 1000.0
        fuera.append({
            "id": pid, "off": off, "delta": delta, "previo": off - delta,
            "notas": notas, "notas_limpias": MARCA.sub("", notas or "").strip(),
            "metodo": metodo, "conf": conf, "vfn": vfn, "afn": afn,
        })
    return fuera


def revertir(db: Path, filas: list[dict]) -> int:
    marca_tiempo = time.strftime("%Y%m%d-%H%M%S")
    respaldo = db.with_suffix(f".sqlite.antes-de-revertir-{marca_tiempo}")
    shutil.copy2(db, respaldo)
    print(f"    respaldo: {respaldo.name}")

    conn = manifest.conectar(str(db))
    n = 0
    ahora = time.time()
    for f in filas:
        if f["delta"] is None:
            continue
        nota = (f["notas_limpias"]
                + f" | revertido iter2 {f['delta']:+.3f}s (signo malo, 2026-08-13)")
        conn.execute(
            "UPDATE audio_sync_pairs SET offset_sec=?, notes=?, created_at=? WHERE id=?",
            (f["previo"], nota.strip(" |"), ahora, f["id"]))
        n += 1
    conn.commit()
    conn.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="raiz de UN proyecto (el que tiene .cinema_assistant/)")
    ap.add_argument("--buscar-bajo", help="barre <raiz>/*/*/.cinema_assistant/")
    ap.add_argument("--aplicar", action="store_true",
                    help="escribe. Sin esto solo enseña la tabla.")
    args = ap.parse_args()

    if not args.root and not args.buscar_bajo:
        ap.error("hace falta --root o --buscar-bajo")

    if args.root:
        db = Path(args.root) / ".cinema_assistant" / "manifest.sqlite"
        if not db.exists():
            print(f"No hay manifest en {db}", file=sys.stderr)
            return 2
        dbs = [db]
    else:
        dbs = manifests_bajo(Path(args.buscar_bajo))
        if not dbs:
            print(f"Ningun manifest bajo {args.buscar_bajo}", file=sys.stderr)
            return 2

    total, total_sin_delta = 0, 0
    for db in dbs:
        filas = afectados(db)
        if not filas:
            continue
        proyecto = db.parent.parent.name
        con_delta = [f for f in filas if f["delta"] is not None]
        sin_delta = [f for f in filas if f["delta"] is None]
        total += len(con_delta)
        total_sin_delta += len(sin_delta)

        print(f"\n=== {proyecto} — {len(con_delta)} par(es) a revertir ===")
        print(f"  {'par':<7} {'clip':<26} {'ahora':>11} {'delta':>9} {'queda en':>11}")
        for f in con_delta:
            print(f"  sp{f['id']:<5} {str(f['vfn'])[:26]:<26} "
                  f"{f['off']:>+11.3f} {f['delta']:>+9.3f} {f['previo']:>+11.3f}")
        if sin_delta:
            print(f"  AVISO: {len(sin_delta)} con marca pero sin delta legible; "
                  "no se tocan:")
            for f in sin_delta:
                print(f"    sp{f['id']}: {f['notas']}")

        if args.aplicar:
            n = revertir(db, con_delta)
            print(f"    revertidos: {n}")

    if total == 0 and total_sin_delta == 0:
        print("No hay refinamientos iter2 en ningun manifest. Nada que hacer.")
        return 0

    print(f"\nTOTAL: {total} par(es) reversible(s)"
          + (f", {total_sin_delta} sin delta legible" if total_sin_delta else ""))
    if not args.aplicar:
        print("\nEsto fue un ensayo: no se escribio nada.")
        print("Para aplicarlo, repite el comando con --aplicar.")
    else:
        print("\nHecho. Siguiente paso, si lo quieres: volver a refinar con el")
        print("codigo ya corregido —")
        print("  python3 bin/refine_all_pairs.py --root <disco> --dry-run")
        print("y revisar los numeros antes de escribir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
