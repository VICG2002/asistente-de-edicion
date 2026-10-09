#!/usr/bin/env python3
"""Corre un script Lua del motor DENTRO de Resolve Studio, desde fuera.

Es la linea de la Consola sin la Consola: `resolve.Fusion().Execute(lua)`
ejecuta el string en un estado donde existen `resolve`, `io`, `os` y `dofile`,
asi que el MISMO aplicador que en Free se pega a mano aqui corre solo
(medido en Resolve Studio 21.1, 2026-09-28; ver
`metodologia/resolve-integracion.md`). Solo Studio: Free no abre el API externo.

Lo que agrega a la linea de la Consola:
  - los globals se asignan en el mismo string, antes del `dofile`;
  - `print` se acumula y se escribe a `<disco>/.cinema_assistant/logs/`;
  - el veredicto (OK / el error) vuelve por `fusion:SetData`, porque
    `Execute` devuelve None.

    python3 bin/aplicar_en_resolve.py --root "$DISK" --clave dia29 \\
        resolve/asistente_asistente.lua 'DIA="2026-09-29"' MODO_MERGE=true

Cada global va como NOMBRE=valor_lua: las cadenas con sus comillas.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB = ("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/"
       "fusionscript.so")


def conectar():
    os.environ.setdefault("RESOLVE_SCRIPT_API", API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", LIB)
    sys.path.append(os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"))
    import DaVinciResolveScript as dvr  # noqa: E402
    r = dvr.scriptapp("Resolve")
    if r is None:
        sys.exit("Resolve no responde por scripting externo (¿abierto? ¿Studio? "
                 "¿Preferences > General > External scripting = Local?)")
    return r


def lua_str(s: str) -> str:
    return "[==[" + s + "]==]"


def envoltorio(script: str, clave: str, globales: list[str], log: str) -> str:
    """El Lua que se manda: asigna globals, corre el script con pcall, deja log
    y veredicto."""
    for g in globales:
        if "=" not in g:
            raise ValueError(f"global sin '=': {g!r}")
    nombres = [g.split("=", 1)[0].strip() for g in globales]
    asignaciones = "\n".join(n + " = " + g.split("=", 1)[1] for n, g in zip(nombres, globales))
    # Los globals VIVEN entre una corrida y otra en el estado Lua de Resolve
    # (igual que en la Consola). Medido el 2026-10-01: un LAVAS_SOLO_PLAN = true
    # de la corrida anterior convirtio la siguiente en otro plan. Peor seria un
    # MERGE_FORZAR = true que sobreviviera. Se borran al terminar.
    limpieza = "\n".join(f"{n} = nil" for n in nombres)
    return f"""
local __buf = {{}}
local __print = print
print = function(...)
  local t = {{}}
  for i = 1, select('#', ...) do t[#t+1] = tostring(select(i, ...)) end
  __buf[#__buf+1] = table.concat(t, "\\t")
  __print(...)
end
local ok, err = pcall(function()
{asignaciones}
  dofile({lua_str(script)})
end)
{limpieza}
print = __print
local f = io.open({lua_str(log)}, "w")
if f then f:write(table.concat(__buf, "\\n")) f:close() end
fusion:SetData("claude.{clave}", (ok and "OK" or ("ERROR: " .. tostring(err))) .. " @" .. os.time())
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="disco del proyecto (para los logs)")
    ap.add_argument("--clave", required=True, help="nombre corto de esta corrida")
    ap.add_argument("--espera", type=float, default=1800, help="segundos maximos")
    ap.add_argument("script")
    ap.add_argument("globales", nargs="*")
    args = ap.parse_args()

    logdir = Path(args.root) / ".cinema_assistant" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = str(logdir / f"resolve-{args.clave}-{time.strftime('%Y%m%d-%H%M%S')}.log")
    lua = envoltorio(str(Path(args.script).resolve()), args.clave, args.globales, log)

    fu = conectar().Fusion()
    fu.SetData(f"claude.{args.clave}", "")
    t0 = time.time()
    fu.Execute(lua)
    while True:
        v = fu.GetData(f"claude.{args.clave}")
        if v:
            break
        if time.time() - t0 > args.espera:
            # Un dialogo modal abierto en Resolve bloquea el API sin error.
            v = f"SIN VEREDICTO a los {args.espera:.0f} s (¿un dialogo abierto en Resolve?)"
            break
        time.sleep(2)
    print(f"veredicto: {v}   ({time.time() - t0:.1f} s)")
    print(f"log: {log}")
    sys.exit(0 if str(v).startswith("OK") else 1)


if __name__ == "__main__":
    main()
