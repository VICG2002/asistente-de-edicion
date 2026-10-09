#!/usr/bin/env python3
"""Verifica la REGLA DURA del orden de pistas de audio.

    Los audios externos (lavaliers, grabadora de campo) van SIEMPRE despues
    de todos los audios de camara.

Layout obligatorio de cualquier timeline del asistente:

    A1                cámara base
    A2 .. A(1+C)      cámaras compañeras (multicám)
    A(2+C) ..         lavaliers

El script de Resolve vuelca el layout de cada timeline que construye a
`<disco>/.cinema_assistant/resolve/<proy>_layout.json`. Aqui se comprueba.

Sin esto la regla depende de que el codigo la respete por costumbre — y ya paso
una vez que un cambio en el orden de colocacion la rompio sin que nadie lo
notara hasta abrir la timeline.

Uso:
    python3 bin/verify_track_order.py --root <disco>
    python3 bin/verify_track_order.py --root <disco> --verbose
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

PREFIJO_CAMARA = "CAM "
PREFIJO_EXTERNO = "LAVA "


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def revisar(timeline: dict):
    """Devuelve (ok, motivo, sin_nombrar)."""
    pistas = [p for p in timeline.get("pistas", []) if p.get("items", 0) > 0]
    ultima_cam = 0
    primer_ext = None
    sin_nombrar = []
    for p in pistas:
        nombre = p.get("nombre") or ""
        i = p.get("i", 0)
        if nombre.startswith(PREFIJO_CAMARA):
            ultima_cam = max(ultima_cam, i)
        elif nombre.startswith(PREFIJO_EXTERNO):
            if primer_ext is None:
                primer_ext = i
        else:
            sin_nombrar.append((i, nombre))
    if primer_ext is not None and primer_ext < ultima_cam:
        return (False,
                f"audio externo en A{primer_ext} por ENCIMA de audio de camara "
                f"en A{ultima_cam}", sin_nombrar)
    return True, "", sin_nombrar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--layout", default="",
                    help="JSON de layouts. Default: el <proy>_layout.json del disco.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    if args.layout:
        path = Path(args.layout)
    else:
        cand = sorted((root / ".cinema_assistant" / "resolve").glob("*_layout.json"))
        if not cand:
            print("No encuentro el volcado de layouts.")
            print("Lo genera el asistente_<proyecto>.lua al construir las timelines.")
            print("Correrlo primero desde la Consola de Resolve.")
            return 1
        path = cand[-1]

    try:
        layouts = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"Volcado ilegible ({path}): {e}")

    print(f"\nOrden de pistas de audio — {root.name}")
    print("=" * 66)
    print("REGLA: los audios externos van SIEMPRE despues de los de camara.")
    print(f"volcado: {path.name}   timelines: {len(layouts)}\n")

    fallidas = []
    anonimas = 0
    for tl in layouts:
        ok, motivo, sin_nombrar = revisar(tl)
        nombre = tl.get("timeline", "?")
        anonimas += len(sin_nombrar)
        marca = "✓" if ok else "✗"
        print(f"  {marca} {nombre}")
        if args.verbose or not ok:
            for p in tl.get("pistas", []):
                if p.get("items", 0) > 0 or args.verbose:
                    print(f"        A{p.get('i')}  {p.get('nombre') or '(sin nombre)':<18}"
                          f" {p.get('items', 0)} item(s)")
        if not ok:
            fallidas.append((nombre, motivo))
            print(f"        → {motivo}")
        if sin_nombrar and args.verbose:
            for i, n in sin_nombrar:
                print(f"        · A{i} sin prefijo CAM/LAVA ({n or 'vacio'}) — "
                      f"no se puede verificar")

    print("")
    if anonimas and not args.verbose:
        print(f"·  {anonimas} pista(s) con contenido sin prefijo CAM/LAVA: no entran "
              f"en la verificacion.\n   Correr con --verbose para verlas.")
    if fallidas:
        print(f"✗ {len(fallidas)} timeline(s) violan la regla:")
        for nombre, motivo in fallidas:
            print(f"    {nombre}: {motivo}")
        return 1
    print("✓ Todas las timelines respetan el orden: cámara primero, externo después.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
