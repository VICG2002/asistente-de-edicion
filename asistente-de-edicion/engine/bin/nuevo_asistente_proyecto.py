#!/usr/bin/env python3
"""Genera el `asistente_<proyecto>.lua` de un proyecto nuevo.

EL PROBLEMA QUE RESUELVE
El plugin distribuye `resolve/asistente_lib.lua`, pero no habia plantilla del
script por proyecto: cada proyecto nuevo se hacia copiando a mano el del
anterior y cambiando rutas. Medido el 2026-08-03:

    asistente_morsa.lua  vs  asistente_fcc.lua        19 lineas de 993
    asistente_avalanches.lua vs asistente_fcc.lua    278 lineas

O sea: uno es una copia del otro, y el tercero es una generacion vieja que
nunca recibio los arreglos hechos despues. Tres de los cinco scripts de
proyecto estaban desfasados, y el de Morsa arrastraba `LISTO — FILM CLUB CAFE`
en el mensaje de cierre porque nadie reviso las 993 lineas al copiarlas.

QUE HACE
Toma el script de REFERENCIA (por defecto el mas reciente del motor) y escribe
uno nuevo con la cabecera, las rutas y el prefijo del proyecto destino.
Sustituye SOLO lo que identifica al proyecto:

    DATA_PATH, MULTICAM_PATH   rutas al horneado del disco
    PFX                        prefijo de las timelines
    el nombre en los dos banners (apertura y cierre)

No reescribe la logica: eso vive en el script de referencia y en
`asistente_lib.lua`. Si manana la logica cambia, se regenera y ya.

Comprueba lo obvio antes de dar por bueno: que no quede ni una mencion al
proyecto de referencia. Eso es justo lo que fallo al copiar a mano.

Uso:
    python3 bin/nuevo_asistente_proyecto.py --disco "/Volumes/T9/Diez50/Morsa" \\
        --nombre "MORSA" --slug morsa
    python3 bin/nuevo_asistente_proyecto.py --disco <d> --nombre <N> --slug <s> \\
        --referencia asistente_morsa.lua --nota "Concierto, 3 camaras" --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MOTOR = Path.home() / "cinema-assistant"
RESOLVE = MOTOR / "resolve"


# Capacidades que un script de proyecto puede tener o no, con la marca por la
# que se reconocen en el codigo.
#
# POR QUE EXISTE ESTA TABLA (2026-08-18, cobertura de agosto dia 2). La referencia
# por defecto es "el asistente_*.lua mas reciente por mtime", y eso NO es lo
# mismo que "el mas completo". Medido ese dia: el mas reciente era
# `asistente_mab.lua` (documental) y no tenia NADA de la logica de pieza con
# guion — ni bloques de toma, ni corte de silencios, ni marcadores de capsula.
# Generar desde ahi habria dado un script que se carga limpio, imprime "LISTO"
# y no puede hacer lo que se le pidio. El generador nacio justamente para que
# los scripts no divergieran; sin esta comprobacion, propaga la divergencia en
# vez de cazarla.
CAPACIDADES = {
    "tomas":      ("marcarBloques",         "bloques de toma y marcadores de capsula"),
    "cortes":     ("appendConCortes",       "corte de silencios en la timeline"),
    "reels":      ("COLOR_REEL",            "reparto por capsula / reel"),
    "bts":        ("BEHIND THE SCENES",     "timeline de behind the scenes"),
    "cronologia": ("construirPorTiempoReal", "timelines posicionadas por tiempo real"),
    "multiproyecto": ("LIB.esAjeno", "convivir con otra asistencia en el mismo proyecto"),
}

# Lo que cada tipo de proyecto NO puede permitirse perder.
EXIGIDAS_POR_KIND = {
    "comercial": ("tomas", "cortes", "reels"),
}

# Lo que se le exige a CUALQUIER proyecto, sea del tipo que sea. `multiproyecto`
# entra aqui el 2026-08-18: el editor abrio el dia 2 de un cliente en el mismo
# proyecto de Resolve que el dia 1, y un script sin esa guarda procesa los clips
# del otro rodaje y da su cobertura por incompleta.
EXIGIDAS_SIEMPRE = ("multiproyecto",)


def capacidades_de(path: Path) -> set:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    return {k for k, (marca, _) in CAPACIDADES.items() if marca in txt}


def elegir_referencia() -> Path:
    """El asistente_*.lua mas reciente que no sea una plantilla ni un spike."""
    cands = [p for p in RESOLVE.glob("asistente_*.lua") if p.name != "asistente_lib.lua"]
    if not cands:
        sys.exit(f"No hay ningun asistente_*.lua en {RESOLVE} del que partir.")
    return max(cands, key=lambda p: p.stat().st_mtime)


def kind_del_proyecto(disco: Path) -> str:
    cfg = disco / ".cinema_assistant" / "project_config.json"
    if not cfg.exists():
        return ""
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("project_kind") or "")
    except (json.JSONDecodeError, OSError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--disco", required=True,
                    help="Carpeta del proyecto (la que contiene .cinema_assistant/)")
    ap.add_argument("--nombre", required=True,
                    help="Nombre para los banners y el prefijo, ej. MORSA")
    ap.add_argument("--slug", required=True,
                    help="Slug de los horneados, ej. morsa -> morsa_data.lua")
    ap.add_argument("--referencia", default=None,
                    help="asistente_*.lua del que partir (default: el mas reciente)")
    ap.add_argument("--nota", default="",
                    help="Una linea de contexto para la cabecera del script.")
    ap.add_argument("--kind", default=None,
                    help="Tipo de proyecto para exigir capacidades a la "
                         "referencia (default: el project_kind del disco)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    disco = Path(args.disco)
    if not (disco / ".cinema_assistant").is_dir():
        sys.exit(f"{disco} no parece un proyecto: no tiene .cinema_assistant/")

    ref = Path(args.referencia) if args.referencia else elegir_referencia()
    if not ref.is_absolute():
        ref = RESOLVE / ref
    if not ref.exists():
        sys.exit(f"No existe la referencia {ref}")
    # Que trae y que NO trae la referencia. Se dice SIEMPRE, aunque no falte
    # nada: la divergencia entre scripts es invisible hasta que se busca.
    tiene = capacidades_de(ref)
    print(f"Referencia: {ref.name}"
          + ("  (elegida por mtime — la mas reciente, que no es la mas completa)"
             if not args.referencia else ""))
    for k, (_marca, desc) in CAPACIDADES.items():
        print(f"   {'si' if k in tiene else 'NO':>2}  {k:<11} {desc}")

    kind = args.kind or kind_del_proyecto(disco)
    exigidas = tuple(EXIGIDAS_POR_KIND.get(kind, ())) + EXIGIDAS_SIEMPRE
    faltan = [k for k in exigidas if k not in tiene]
    if faltan:
        alternativas = sorted(
            (p.name for p in RESOLVE.glob("asistente_*.lua")
             if p.name != "asistente_lib.lua"
             and not set(exigidas) - capacidades_de(p)),
        )
        print(f"\n⚠⚠ {ref.name} NO sirve para un proyecto '{kind}': le falta "
              + ", ".join(faltan))
        print("   Un script sin esto se carga limpio, imprime LISTO y no hace "
              "lo que se le pidio.")
        if alternativas:
            print("   Referencias que si lo traen: " + ", ".join(alternativas))
            print(f"   Repetir con:  --referencia {alternativas[0]}")
        else:
            print("   Ninguna referencia del motor lo trae: hay que portarlo a mano.")
        return 2

    src = ref.read_text(encoding="utf-8")

    # De que proyecto viene, para poder comprobar que no queda rastro.
    m = re.search(r'local PFX = "([^"]+?)\s*—\s*"', src)
    viejo_pfx = m.group(1) if m else ""
    m = re.search(r'local DATA_PATH = "([^"]+)"', src)
    viejo_slug = ""
    if m:
        mm = re.search(r'/([^/]+)_data\.lua$', m.group(1))
        viejo_slug = mm.group(1) if mm else ""

    bake = f"{disco}/.cinema_assistant/resolve"
    nuevo = src
    nuevo = re.sub(r'local DATA_PATH = "[^"]*"',
                   f'local DATA_PATH = "{bake}/{args.slug}_data.lua"', nuevo)
    nuevo = re.sub(r'local MULTICAM_PATH = "[^"]*"',
                   f'local MULTICAM_PATH = "{bake}/{args.slug}_multicam.lua"', nuevo)
    nuevo = re.sub(r'local PFX = "[^"]*"',
                   f'local PFX = "{args.nombre} — "', nuevo)
    # Los dos banners (apertura y cierre) y el dofile de la cabecera.
    nuevo = re.sub(r'print\("  ASISTENTE DE EDICION — [^"]*"\)',
                   f'print("  ASISTENTE DE EDICION — {args.nombre}")', nuevo)
    nuevo = re.sub(r'print\("  LISTO — [^"]*"\)',
                   f'print("  LISTO — {args.nombre}")', nuevo)
    nuevo = re.sub(r'dofile\("[^"]*asistente_[a-z0-9_]+\.lua"\)',
                   f'dofile("{RESOLVE}/asistente_{args.slug}.lua")', nuevo)

    # Cabecera: se reemplaza el bloque de comentario inicial entero, que es lo
    # que describe al proyecto viejo y lo que nadie relee al copiar.
    cuerpo = nuevo[nuevo.index("local DATA_PATH"):]
    cabecera = (
        "-- ============================================================\n"
        f"--  ASISTENTE DE EDICION — {args.nombre}\n"
        "--  Consola de DaVinci Resolve (modo Lua):\n"
        f'--    dofile("{RESOLVE}/asistente_{args.slug}.lua")\n'
        "--\n"
        f"--  Generado por bin/nuevo_asistente_proyecto.py a partir de {ref.name}.\n"
        f"--  Proyecto: {disco}\n"
    )
    if args.nota:
        cabecera += f"--  {args.nota}\n"
    cabecera += (
        "--\n"
        "--  Markers (solo los accionables, todos de PUNTO):\n"
        "--    Red/Yellow — cull / revisar.   Purple — pregunta de entrevista.\n"
        "--    Blue — arranque de respuesta.  Sand — pausa real.\n"
        "--    Mint — palabra clave.          Lemon — candidato emocional.\n"
        "--    Cyan — audio externo sync.\n"
        "--\n"
        "--  Timelines: A-ROLL, B-ROLL, una por camara y AUDIOS EXTERNOS\n"
        "--  (esta ultima es la cronologia del dia, posicionada por tiempo real).\n"
        "--  Entre A-ROLL y B-ROLL tienen que estar TODOS los clips: el script\n"
        "--  lo comprueba al cerrar y avisa si falta alguno.\n"
        "-- ============================================================\n\n"
    )
    nuevo = cabecera + cuerpo

    # PREFIJO UNICO. El script borra al arrancar todas las timelines que empiezan
    # por su PFX. Dos proyectos con el mismo prefijo se destruyen las timelines
    # el uno al otro en cuanto compartan proyecto de Resolve — y compartirlo es
    # lo normal, no la excepcion.
    pfx_nuevo = f"{args.nombre} — "
    destino = RESOLVE / f"asistente_{args.slug}.lua"
    for otro in sorted(RESOLVE.glob("asistente_*.lua")):
        if otro.name in ("asistente_lib.lua", destino.name):
            continue
        m2 = re.search(r'^local PFX = "([^"]*)"',
                       otro.read_text(encoding="utf-8", errors="ignore"), re.M)
        if m2 and m2.group(1) == pfx_nuevo:
            print(f"\n⚠⚠ El prefijo {pfx_nuevo!r} ya lo usa {otro.name}.")
            print("   Dos proyectos con el mismo PFX se borran las timelines el "
                  "uno al otro en cuanto")
            print("   compartan proyecto de Resolve. Elige otro --nombre.")
            return 2


    # Comprobacion: no puede quedar rastro del proyecto de referencia.
    # Se ignoran las menciones LEGITIMAS: la ruta del disco destino (que puede
    # llamarse igual que el proyecto de referencia si se regenera en el mismo
    # sitio) y la linea de atribucion de la cabecera.
    def legitima(linea: str) -> bool:
        return (str(disco) in linea) or ("Generado por bin/" in linea)

    restos, vistas = [], set()
    for aguja in filter(None, {viejo_pfx, viejo_slug}):
        for i, linea in enumerate(nuevo.splitlines(), 1):
            if i in vistas or legitima(linea):
                continue
            if re.search(re.escape(aguja), linea, re.IGNORECASE):
                vistas.add(i)
                restos.append(f"  linea {i}: {linea.strip()[:90]}")
    if restos:
        print(f"\n⚠ Quedan menciones al proyecto de referencia "
              f"('{viejo_pfx}'/'{viejo_slug}'):")
        for r in restos[:12]:
            print(r)
        print("  Son comentarios de doctrina (casos historicos) o hay que "
              "corregirlos a mano. Revisalos antes de usar el script.")

    if args.dry_run:
        print(f"\n(dry-run) se escribiria {destino} — {len(nuevo.splitlines())} lineas")
        return 0

    if destino.exists():
        sys.exit(f"Ya existe {destino}. Borralo a mano si de verdad quieres "
                 "regenerarlo (puede tener ajustes propios del proyecto).")
    destino.write_text(nuevo, encoding="utf-8")
    print(f"Escrito {destino} ({len(nuevo.splitlines())} lineas)")

    r = subprocess.run(["luac", "-p", str(destino)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"⚠ El script generado NO compila:\n{r.stderr}")
    print("luac -p OK")
    print("\nProbarlo sin abrir Resolve:")
    print(f"  cd {RESOLVE} && lua mock_resolve_full.lua asistente_{args.slug}.lua")
    print("\nEn Resolve (sobre proyecto duplicado):")
    print(f'  dofile("{destino}")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
