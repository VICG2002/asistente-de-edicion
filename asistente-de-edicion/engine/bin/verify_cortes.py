#!/usr/bin/env python3
"""Garantia del corte de silencios: comprueba que no se corto habla.

POR QUE UN VERIFICADOR Y NO UNA PROMESA
`derive_silence_cuts.py` decide con lo que midio `silencedetect`, que es una
deteccion por ventana movil. Este script vuelve al audio y mide OTRA COSA sobre
el resultado final: el RMS de cada tramo que se va a quitar, ya con el aire
descontado. Dos medidas distintas sobre la misma fuente; si el corte se comio
una palabra, el RMS del tramo sube y sale por aqui.

No se usan los tiempos de palabra de Whisper para esto. Esta MEDIDO que derivan
(IMODAE 2026-08-07, C1178: declaraban un hueco en 29-31 s donde hay voz y no
veian el de 37-40 s que si existe). Un verificador que se apoye en ellos daria
falsos rojos en unos clips y verde falso en otros.

QUE COMPRUEBA
  1. Estructura: ningun tramo se sale del clip, ninguno se solapa, todos van en
     orden y lo conservado mas lo quitado cubre el clip entero.
  2. Contenido: el RMS de cada tramo que se quita esta por debajo del umbral con
     que se midio, mas la tolerancia.

Sale 1 si algo falla. Es de la tanda obligatoria del §12b del playbook cuando el
proyecto lleva cortes.

Uso:
    python3 bin/verify_cortes.py --root <disco>
    python3 bin/verify_cortes.py --root <disco> --tolerancia-db 6
    python3 bin/verify_cortes.py --root <disco> --solo-estructura
"""

from __future__ import annotations

import argparse
import glob
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import manifest                            # noqa: E402
from lib.guards import exigir_tablas, report_done    # noqa: E402

_RE_RMS = re.compile(r"RMS level dB:\s*(-?[\d.]+)")
TOLERANCIA_DB = 3.0
EPS = 0.05          # holgura de coma flotante, en segundos


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def rms_de_tramo(path: str, ini: float, dur: float,
                 timeout: int = 120) -> float | None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{ini:.3f}",
             "-t", f"{dur:.3f}", "-i", path, "-vn",
             "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    m = _RE_RMS.search(r.stderr or "")
    return float(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--tolerancia-db", type=float, default=TOLERANCIA_DB,
                    help=f"Cuanto puede pasarse del umbral el RMS de un tramo "
                         f"quitado antes de darlo por fallo "
                         f"(default {TOLERANCIA_DB:.0f} dB).")
    ap.add_argument("--solo-estructura", action="store_true",
                    help="Saltarse la re-medida de audio (rapido, mas debil).")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    exigir_tablas(conn, {
        "clip_keep_ranges": "python3 bin/derive_silence_cuts.py --root <disco>",
        "clip_cut_ranges": "python3 bin/derive_silence_cuts.py --root <disco>",
    }, "verify_cortes")

    clips = {cid: (fn, path, float(dur or 0)) for cid, fn, path, dur in conn.execute(
        "SELECT id, filename, path, duration_sec FROM clips WHERE file_kind='video'")}

    keep: dict[int, list] = defaultdict(list)
    for cid, i, a, b in conn.execute(
            "SELECT clip_id, idx, start_sec, end_sec FROM clip_keep_ranges "
            "ORDER BY clip_id, idx"):
        keep[cid].append((i, float(a), float(b)))
    cut: dict[int, list] = defaultdict(list)
    umbral: dict[int, float] = {}
    for cid, i, a, b, th in conn.execute(
            "SELECT clip_id, idx, start_sec, end_sec, threshold_db "
            "FROM clip_cut_ranges ORDER BY clip_id, idx"):
        cut[cid].append((i, float(a), float(b)))
        umbral[cid] = float(th) if th is not None else 0.0

    if not keep:
        print("No hay cortes en este proyecto: nada que verificar.")
        report_done("verify_cortes", clips=0, tramos=0, fallos=0)
        return 0

    fallos: list[str] = []

    # --- 1. estructura ------------------------------------------------------
    for cid, tramos in keep.items():
        fn, _path, dur = clips.get(cid, (f"clip {cid}", "", 0.0))
        prev_fin, prev_idx = None, 0
        for i, a, b in tramos:
            if b <= a:
                fallos.append(f"{fn}: tramo {i} vacio o invertido ({a:.2f}–{b:.2f})")
            if a < -EPS or b > dur + EPS:
                fallos.append(f"{fn}: tramo {i} se sale del clip "
                              f"({a:.2f}–{b:.2f} de {dur:.2f}s)")
            if prev_fin is not None and a < prev_fin - EPS:
                fallos.append(f"{fn}: tramo {i} se solapa con el anterior "
                              f"({a:.2f} < {prev_fin:.2f})")
            if i != prev_idx + 1:
                fallos.append(f"{fn}: los tramos no van en orden (idx {i} "
                              f"despues de {prev_idx})")
            prev_fin, prev_idx = b, i
        for i, a, b in cut.get(cid, []):
            if a < -EPS or b > dur + EPS:
                fallos.append(f"{fn}: corte {i} se sale del clip "
                              f"({a:.2f}–{b:.2f} de {dur:.2f}s)")
            for j, ka, kb in tramos:
                if min(b, kb) - max(a, ka) > EPS:
                    fallos.append(f"{fn}: el corte {i} ({a:.2f}–{b:.2f}) pisa el "
                                  f"tramo conservado {j} ({ka:.2f}–{kb:.2f})")
        # Lo conservado + lo quitado tiene que cubrir el clip desde la entrada.
        cubierto = (sum(b - a for _i, a, b in tramos)
                    + sum(b - a for _i, a, b in cut.get(cid, [])))
        # La entrada es donde empieza lo primero que se decidio, conservado o
        # cortado. Con el inicio del primer conservado, un corte en la cabeza
        # corria la entrada y su hueco no se veia (revision del PR #1).
        inicios = [a for _i, a, _b in tramos] + [a for _i, a, _b in cut.get(cid, [])]
        entrada = min(inicios) if inicios else 0.0
        hueco = dur - entrada - cubierto
        if hueco > 1.0:
            fallos.append(f"{fn}: {hueco:.1f}s del clip no estan ni conservados "
                          f"ni quitados — se perderian sin que nadie lo diga")

    n_estructura = len(fallos)
    print(f"Estructura: {len(keep)} clips, "
          f"{sum(len(v) for v in keep.values())} tramos, "
          f"{sum(len(v) for v in cut.values())} cortes  ->  "
          + ("OK" if n_estructura == 0 else f"{n_estructura} fallo(s)"))

    # --- 2. contenido: re-medir el audio de cada tramo quitado --------------
    medidos = sin_medir = 0
    peor = None
    if not args.solo_estructura:
        for cid, tramos in sorted(cut.items()):
            fn, path, _dur = clips.get(cid, (f"clip {cid}", "", 0.0))
            if not path or not Path(path).exists():
                sin_medir += len(tramos)
                continue
            techo = umbral.get(cid, 0.0) + args.tolerancia_db
            for i, a, b in tramos:
                rms = rms_de_tramo(path, a, b - a)
                if rms is None:
                    sin_medir += 1
                    continue
                medidos += 1
                if peor is None or rms > peor[0]:
                    peor = (rms, fn, i, a, b, techo)
                if rms > techo:
                    fallos.append(
                        f"{fn}: el corte {i} ({a:.2f}–{b:.2f}) tiene RMS "
                        f"{rms:.1f} dB, por encima del techo {techo:.1f} dB "
                        f"(umbral {umbral.get(cid, 0):.1f} + tolerancia "
                        f"{args.tolerancia_db:.0f}) — ahi hay sonido, no silencio")
        print(f"Contenido : {medidos} tramos re-medidos en el audio"
              + (f", {sin_medir} sin medir" if sin_medir else "")
              + "  ->  " + ("OK" if len(fallos) == n_estructura
                            else f"{len(fallos) - n_estructura} fallo(s)"))
        if peor:
            rms, fn, i, a, b, techo = peor
            print(f"  el tramo mas ruidoso que se quita: {fn} {a:.1f}–{b:.1f}s, "
                  f"RMS {rms:.1f} dB (techo {techo:.1f} dB)")
    else:
        print("Contenido : SALTADO (--solo-estructura) — la garantia es mas debil.")

    if fallos:
        print(f"\n{'=' * 66}")
        print(f"CORTES QUE NO PASAN: {len(fallos)}")
        print("=" * 66)
        for f in fallos[:40]:
            print(f"  · {f}")
        if len(fallos) > 40:
            print(f"  … y {len(fallos) - 40} mas")
        print("\nQue hacer: subir --aire en derive_silence_cuts (deja mas "
              "colchon),\no --min-silencio (corta solo las pausas largas). "
              "Y re-hornear.")
        report_done("verify_cortes", clips=len(keep),
                    tramos=sum(len(v) for v in keep.values()),
                    medidos=medidos, fallos=len(fallos))
        return 1

    print(f"\nOK: {sum(len(v) for v in cut.values())} cortes en {len(keep)} clips, "
          f"ninguno se come habla.")
    report_done("verify_cortes", clips=len(keep),
                tramos=sum(len(v) for v in keep.values()),
                medidos=medidos, fallos=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
