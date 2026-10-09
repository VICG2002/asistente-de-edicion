#!/usr/bin/env python3
"""Verifica lo que volvio de Resolve por el canal de regreso (.drt), sin mirar Resolve.

PARA QUE SIRVE
Despues de `Diez50 Aplicar` en Resolve, el aplicador deja en la carpeta de
recibos del pedido un .drt por timeline construida y el de su reporte. Este
script los lee con lib/drt.py y los compara contra <recibos>/pedido.json:
nombre, sello en el frame 0, pistas en su orden y con clips, y el estado del
reporte. Ver lib/regreso.py.

    python3 bin/verificar_regreso.py --recibos "<recibos>/<sello>/"
    python3 bin/verificar_regreso.py --ultimo          # el pedido mas reciente
    python3 bin/verificar_regreso.py --ultimo --esperar 60

Salida: 0 verde o amarillo, 1 rojo, 2 pendiente (todavia no llego nada).
Si no llego nada y en --errores hay un error_<proyecto>.drt posterior al
pedido, sale rojo con el error que dejo Resolve (E01, E02...).
Deja verificacion.json junto a los recibos.

--esperar espera a que aparezca reporte.drt. Es el CLI el que espera, no
Resolve: el aplicador sigue siendo un script que el editor lanza y termina.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from lib import regreso as RG  # noqa: E402
from lib.pedido import APP_SUPPORT  # noqa: E402

SALIDA = {"verde": 0, "amarillo": 0, "rojo": 1, "pendiente": 2}


def ultimo(base: Path) -> Path | None:
    candidatas = [d for d in base.iterdir() if d.is_dir() and (d / "pedido.json").exists()]
    return max(candidatas, key=lambda d: (d / "pedido.json").stat().st_mtime, default=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--recibos", help="Carpeta de recibos de UN pedido (<base>/<sello>/).")
    g.add_argument("--ultimo", action="store_true",
                   help="El pedido mas reciente de --base.")
    ap.add_argument("--base", default=str(APP_SUPPORT / "recibos"),
                    help="Carpeta base de recibos, para --ultimo.")
    ap.add_argument("--errores", default=str(APP_SUPPORT / "errores"),
                    help="Carpeta donde el aplicador exporta error_<proyecto>.drt.")
    ap.add_argument("--esperar", type=float, default=0.0,
                    help="Segundos a esperar a que llegue reporte.drt.")
    a = ap.parse_args(argv)

    if a.ultimo:
        base = Path(a.base).expanduser()
        recibos = ultimo(base) if base.is_dir() else None
        if recibos is None:
            print(f"No hay pedidos en {base}.", file=sys.stderr)
            return 1
    else:
        recibos = Path(a.recibos).expanduser()

    errores = Path(a.errores).expanduser()
    copia = RG.leer_copia(recibos)
    limite = time.monotonic() + max(0.0, a.esperar)
    # Se espera al reporte, que es lo ultimo que exporta el aplicador; un error
    # de este proyecto posterior al pedido corta la espera (no llegara mas).
    while (not (recibos / "reporte.drt").exists() and time.monotonic() < limite
           and not RG.error_reciente(errores, copia)):
        time.sleep(1.0)
    if a.esperar and (recibos / "reporte.drt").exists():
        time.sleep(1.0)      # que Resolve termine de escribir el ultimo archivo

    v = RG.verificar(recibos, errores)
    print(f"Pedido {v.sello or '?'} ({v.proyecto or '?'}) en {recibos}")
    for h in v.hallazgos:
        print(h.linea())
    if v.veredicto != "pendiente" and (recibos / "pedido.json").exists():
        RG.escribir(v)
    print(f"VEREDICTO: {v.veredicto.upper()}")
    if v.veredicto == "pendiente":
        print("Si en Resolve no aparecio una timeline 'Diez50 ...', abre el proyecto "
              "del pedido y repite Workspace > Scripts > Diez50 Aplicar.")
    return SALIDA[v.veredicto]


if __name__ == "__main__":
    sys.exit(main())
