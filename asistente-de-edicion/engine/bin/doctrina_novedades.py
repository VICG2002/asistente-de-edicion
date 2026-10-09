#!/usr/bin/env python3
"""Qué trae la doctrina del plugin que la doctrina local todavia no tiene.

Problema que resuelve (hallazgo 5.5 de la auditoria 2026-07-27): el instalador
siembra la doctrina UNA sola vez y despues nunca la toca, para no pisar el
trabajo de nadie. Correcto, pero significa que quien instalo el plugin nunca
vuelve a recibir una leccion nueva, ni actualizando.

Ahora hay dos capas dentro de ~/memoria-asistente-edicion/:

    metodologia/  lecciones/   -> TUYAS. El instalador no las toca jamas.
    _del-plugin/               -> copia de referencia, se refresca siempre.

Este script las compara y dice que falta. Es un AVISO, NO UNA FUSION: la
doctrina es prosa con criterio editorial dentro, y mezclarla sola iria contra
el principio rector del colectivo — la IA ejecuta, el editor decide.

Uso:
    python3 bin/doctrina_novedades.py            # resumen
    python3 bin/doctrina_novedades.py --detalle  # + las lineas nuevas
    python3 bin/doctrina_novedades.py --quiet    # solo exit code

Exit: 0 si no hay novedades, 0 tambien si las hay (es informativo, no una
compuerta). Usa --strict para que devuelva 1 cuando haya novedades.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

DOCTRINA = Path.home() / "memoria-asistente-edicion"
DEL_PLUGIN = DOCTRINA / "_del-plugin"

# Se reparte igual que en bootstrap.sh: estos tres a lecciones/, el resto a
# metodologia/. Si aquel case cambia, este mapa cambia en el mismo commit.
A_LECCIONES = {"patrones-exitosos.md", "lo-que-no-hacer.md",
               "errores-comunes-a-corregir.md"}

# Personal: nunca se empaqueta, asi que nunca aparece como novedad.
NO_EMPAQUETADO = {"preferencias-del-usuario.md", "LEEME.md"}


def carpeta_destino(nombre: str) -> str:
    return "lecciones" if nombre in A_LECCIONES else "metodologia"


def local_de(nombre: str) -> Path:
    """Donde vive (o viviria) ese documento en la doctrina propia."""
    directo = DOCTRINA / carpeta_destino(nombre) / nombre
    if directo.exists():
        return directo
    # Tolerante: si el usuario lo movio de carpeta, buscarlo por nombre.
    for p in DOCTRINA.rglob(nombre):
        if "_del-plugin" not in p.parts and ".git" not in p.parts:
            return p
    return directo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detalle", action="store_true",
                    help="mostrar las lineas que el plugin trae de mas")
    ap.add_argument("--quiet", action="store_true", help="no imprimir nada")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 si hay novedades (para usar como compuerta)")
    args = ap.parse_args()

    def out(*a):
        if not args.quiet:
            print(*a)

    if not DEL_PLUGIN.is_dir():
        out("No hay copia de referencia del plugin en", DEL_PLUGIN)
        out("Corre el bootstrap del plugin para crearla.")
        return 0

    nuevos: list[str] = []
    adelantados: list[tuple[str, int, int]] = []

    for ref in sorted(DEL_PLUGIN.glob("*.md")):
        if ref.name in NO_EMPAQUETADO:
            continue
        mio = local_de(ref.name)
        if not mio.exists():
            nuevos.append(ref.name)
            continue
        texto_ref = ref.read_text(encoding="utf-8", errors="replace")
        texto_mio = mio.read_text(encoding="utf-8", errors="replace")
        if texto_ref == texto_mio:
            continue
        lineas_ref = texto_ref.splitlines()
        lineas_mio = texto_mio.splitlines()
        # Solo interesa lo que el plugin trae DE MAS. Si el usuario va por
        # delante (lo normal en la Mac de quien escribe la doctrina), no es
        # una novedad: es su trabajo.
        de_mas = sum(1 for ln in difflib.unified_diff(lineas_mio, lineas_ref, n=0)
                     if ln.startswith("+") and not ln.startswith("+++"))
        if de_mas:
            adelantados.append((ref.name, de_mas, len(lineas_mio)))

    if not nuevos and not adelantados:
        out("Doctrina al dia: nada nuevo en la copia del plugin.")
        return 0

    out()
    out("=" * 66)
    out("DOCTRINA — el plugin trae material que tu copia no tiene")
    out("=" * 66)
    if nuevos:
        out()
        out(f"Documentos NUEVOS ({len(nuevos)}):")
        for n in nuevos:
            out(f"    {n}   ->  iria en {carpeta_destino(n)}/")
    if adelantados:
        out()
        out(f"Documentos con lineas nuevas ({len(adelantados)}):")
        ancho = max(len(n) for n, _, _ in adelantados)
        for nombre, de_mas, propias in adelantados:
            out(f"    {nombre:<{ancho}}  +{de_mas} lineas  (tu copia tiene {propias})")

    if args.detalle:
        for nombre, _, _ in adelantados:
            ref = DEL_PLUGIN / nombre
            mio = local_de(nombre)
            out()
            out("-" * 66)
            out(f"{nombre} — lo que el plugin trae de mas")
            out("-" * 66)
            for ln in difflib.unified_diff(
                    mio.read_text(encoding="utf-8", errors="replace").splitlines(),
                    ref.read_text(encoding="utf-8", errors="replace").splitlines(),
                    fromfile="tuyo", tofile="del plugin", lineterm="", n=1):
                out(ln)

    out()
    out("Esto es un AVISO, no una fusion. Revisa y copia a mano lo que quieras")
    out("incorporar: la doctrina lleva criterio editorial dentro.")
    out(f"Comparar en detalle:  python3 {Path(__file__).name} --detalle")
    out()
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
