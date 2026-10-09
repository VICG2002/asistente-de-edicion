#!/usr/bin/env python3
"""Escribe pedidos en el buzon del aplicador de Resolve, y su stub del menu.

PARA QUE SIRVE
Es la unica puerta al buzon (~/Library/Application Support/Diez50/buzon.lua):
el motor, el CLI y Claude escriben por aqui, nunca a mano. En el menu de
Resolve Free, `Diez50 Aplicar` lee el pedido del proyecto abierto y lo cumple
(resolve/aplicar.lua). Ver lib/pedido.py para el contrato.

SUBCOMANDOS
  construir  las timelines de un proyecto documental desde su horneado (Fase 1):
           A-ROLL, B-ROLL y AUDIOS EXTERNOS, exportadas a .drt en
           <disco>/.cinema_assistant/resolve/recibos/<sello>/. El prefijo sale
           del timeline_prefix del project_config.json si no se da --nombre.
  prueba   el pedido de la prueba de ida y vuelta (Fase 0): una timeline con un
           orden de pistas conocido, exportada a .drt, con la media del kit de
           diagnostico. Despues: bin/verificar_regreso.py.
  stub     escribe "Diez50 Aplicar.lua" con rutas absolutas literales.
  ver      el buzon actual, en JSON.
  quitar   borra del buzon el pedido de un proyecto (solo el pedido).

    python3 bin/pedido.py construir --root "$DISK" --proyecto ProyectoAsistente \\
        --dia 2026-09-28
    python3 bin/pedido.py prueba --proyecto DIEZ50_IDA
    python3 bin/pedido.py stub

PARA OTRA MAQUINA (la VM de prueba)
--otra-maquina no comprueba que las rutas existan (son de la otra Mac) ni crea
la carpeta de recibos: deja pedido-<sello>.json junto al buzon, para copiarlo
alla a <recibos>/pedido.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from lib import pedido as P  # noqa: E402

RECIBOS_POR_DEFECTO = P.APP_SUPPORT / "recibos"


def cmd_prueba(a: argparse.Namespace) -> int:
    kit = Path(a.kit).expanduser()
    media = {"negro": kit / "media" / "negro_10min.mov",
             "camara": kit / "media" / "par" / "camara.mov",
             "lav": kit / "media" / "par" / "lav.wav"}
    ped = P.pedido_prueba(negro=str(media["negro"]), camara=str(media["camara"]),
                          lav=str(media["lav"]),
                          recibos_base=str(Path(a.recibos).expanduser()),
                          sello=a.sello, reaplicar=a.reaplicar)
    buzon = Path(a.buzon).expanduser()
    try:
        P.poner_pedido(a.proyecto, ped, ruta=buzon, motor_version=a.motor_version,
                       validar_rutas=not a.otra_maquina,
                       crear_recibos=not a.otra_maquina)
    except P.ErrorPedido as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    recibos = ped["rutas"]["recibos"]
    print(f"Pedido {ped['sello']} para el proyecto '{a.proyecto}'")
    print(f"  buzon   : {buzon}")
    print(f"  recibos : {recibos}")
    if a.otra_maquina:
        copia = buzon.parent / f"pedido-{ped['sello']}.json"
        copia.write_text(json.dumps(P.copia_pedido(a.proyecto, ped, a.motor_version),
                                    indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  copia   : {copia}  (va alla a {recibos}pedido.json)")
    print("Siguiente: abre ese proyecto en Resolve, Workspace > Scripts > Diez50 Aplicar,")
    print(f"y luego: python3 bin/verificar_regreso.py --recibos '{recibos}'")
    return 0


def cmd_construir(a: argparse.Namespace) -> int:
    buzon = Path(a.buzon).expanduser()
    modo = {"si": True, "no": False}.get(a.modo_merge)
    try:
        ped = P.pedido_construir(a.root, nombre=a.nombre, slug=a.slug, dia=a.dia,
                                 sello=a.sello, reaplicar=a.reaplicar, modo_merge=modo)
        P.poner_pedido(a.proyecto, ped, ruta=buzon, motor_version=a.motor_version)
    except P.ErrorPedido as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    esp = ped["esperado"]
    recibos = ped["rutas"]["recibos"]
    print(f"Pedido {ped['sello']} (construir) para el proyecto '{a.proyecto}'")
    print(f"  horneado : {ped['rutas']['data']}")
    print(f"  material : {len(esp['clips'])} clip(s) y {len(esp['audios'])} WAV"
          + (f" del {a.dia}" if a.dia else ""))
    for n in esp["timelines"]:
        print(f"  timeline : {n}")
    print(f"  recibos  : {recibos}")
    print("Siguiente: abre ese proyecto en Resolve, Workspace > Scripts > Diez50 Aplicar.")
    print("Si en 10 s no aparece una timeline 'Diez50 ...', abre el proyecto y repite.")
    print(f"Despues: python3 bin/verificar_regreso.py --recibos '{recibos}'")
    return 0


def cmd_stub(a: argparse.Namespace) -> int:
    aplicar = a.aplicar or str(ENGINE / "resolve" / "aplicar.lua")
    buzon = str(Path(a.buzon).expanduser())
    errores = a.errores or str(Path(buzon).parent / "errores") + "/"
    texto = P.texto_stub(aplicar, buzon, errores)
    if a.imprimir:
        print(texto, end="")
        return 0
    destino = Path(a.destino).expanduser() / P.STUB_NOMBRE
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")
    if not a.otra_maquina:
        Path(errores).mkdir(parents=True, exist_ok=True)
    print(f"Stub: {destino}")
    return 0


def cmd_ver(a: argparse.Namespace) -> int:
    print(json.dumps(P.leer_buzon(Path(a.buzon).expanduser()), indent=2, ensure_ascii=False))
    return 0


def cmd_quitar(a: argparse.Namespace) -> int:
    if P.quitar_pedido(a.proyecto, ruta=Path(a.buzon).expanduser()):
        print(f"Pedido de '{a.proyecto}' quitado del buzon.")
        return 0
    print(f"No habia pedido para '{a.proyecto}'.")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--buzon", default=str(P.BUZON_POR_DEFECTO),
                    help="Ruta del buzon.lua (el .json vive al lado).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    co = sub.add_parser("construir", help="Pedido de las timelines de un proyecto.")
    co.add_argument("--root", required=True, help="Carpeta del proyecto (con .cinema_assistant/).")
    co.add_argument("--proyecto", required=True,
                    help="Nombre EXACTO del proyecto de Resolve donde se aplica.")
    co.add_argument("--nombre", help="Prefijo de las timelines sin el ' — ' (por defecto, "
                                     "el timeline_prefix del project_config.json).")
    co.add_argument("--slug", help="Que horneado, si hay mas de uno (<slug>_data.lua).")
    co.add_argument("--dia", help="Solo el material de ese dia (AAAA-MM-DD).")
    co.add_argument("--modo-merge", choices=("auto", "si", "no"), default="auto",
                    help="auto: lo decide el aplicador mirando el Media Pool.")
    co.add_argument("--sello", help="Sello fijo (por defecto, uno nuevo).")
    co.add_argument("--reaplicar", action="store_true",
                    help="Aplicar aunque ya exista el reporte de ese sello.")
    co.add_argument("--motor-version", default="dev")
    co.set_defaults(f=cmd_construir)

    pr = sub.add_parser("prueba", help="Pedido de la prueba de ida y vuelta.")
    pr.add_argument("--proyecto", required=True,
                    help="Nombre EXACTO del proyecto de Resolve donde se aplica.")
    pr.add_argument("--kit", default="~/Diez50-diag",
                    help="Kit de diagnostico con media/ (en la Mac donde corre Resolve).")
    pr.add_argument("--recibos", default=str(RECIBOS_POR_DEFECTO),
                    help="Carpeta base de recibos; el pedido usa <recibos>/<sello>/.")
    pr.add_argument("--sello", help="Sello fijo (por defecto, uno nuevo).")
    pr.add_argument("--reaplicar", action="store_true",
                    help="Aplicar aunque ya exista el reporte de ese sello.")
    pr.add_argument("--motor-version", default="dev")
    pr.add_argument("--otra-maquina", action="store_true",
                    help="Las rutas son de otra Mac: no se comprueban ni se crean.")
    pr.set_defaults(f=cmd_prueba)

    st = sub.add_parser("stub", help="Escribe el stub 'Diez50 Aplicar.lua'.")
    st.add_argument("--aplicar", help="Ruta absoluta de aplicar.lua (por defecto, la del motor).")
    st.add_argument("--errores", help="Carpeta donde el aplicador exporta sus errores.")
    st.add_argument("--destino", default=str(P.DIR_STUBS),
                    help="Carpeta del menu (Fusion/Scripts/Utility).")
    st.add_argument("--imprimir", action="store_true", help="Solo imprimirlo.")
    st.add_argument("--otra-maquina", action="store_true",
                    help="No crear la carpeta de errores aqui.")
    st.set_defaults(f=cmd_stub)

    ve = sub.add_parser("ver", help="Muestra el buzon.")
    ve.set_defaults(f=cmd_ver)

    qu = sub.add_parser("quitar", help="Quita el pedido de un proyecto.")
    qu.add_argument("--proyecto", required=True)
    qu.set_defaults(f=cmd_quitar)

    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
