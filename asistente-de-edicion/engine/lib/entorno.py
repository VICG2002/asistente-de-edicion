"""Que interprete corre cada paso, segun lo que ese paso necesita.

POR QUE EXISTE (2026-08-05). El motor tiene ~4700 lineas de identidad por cara y
voz que NUNCA se han ejecutado. No por un bug: porque el orquestador invoca
`python3` pelado y ese, en la Mac de Victor, es el de Homebrew (3.14.5), que no
tiene cv2, insightface, sklearn ni resemblyzer. Las dependencias estan
instaladas —en `~/cinema-assistant/.venv`, con los modelos en disco— y el
pipeline nunca activa ese venv.

La solucion NO es "activar el venv siempre": romperia las instalaciones de
Bernardo y Adrian, que tienen el venv con lo minimo. Cada paso declara que
capacidad necesita y aqui se resuelve con que interprete se le llama.

CONTRATO: un paso cuya capacidad no exista en ninguna parte NO se ejecuta y NO
falla la corrida. Se anota en la seccion "no corrio y por que" del resumen, con
el comando exacto para habilitarlo. Antes ese trabajo simplemente no se hacia,
y no se hacia en silencio.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent

# Mismo diccionario que bin/doctor.py. Vive alli porque doctor es la
# herramienta de diagnostico; aqui se importa para no tener dos verdades.
try:
    sys.path.insert(0, str(ENGINE))
    from bin.doctor import CAPACIDADES
except Exception:                                   # pragma: no cover
    CAPACIDADES = {"nucleo": ["numpy", "PIL"]}

CACHE = ENGINE / "logs" / "entorno.json"
CACHE_TTL_SEG = 24 * 3600


def _candidatos() -> list[str]:
    """Interpretes a probar, en orden de preferencia para CADA capacidad.

    EL ORDEN IMPORTA Y NO ES EL OBVIO. El `python3` del sistema va PRIMERO,
    aunque el venv tenga mas cosas. Razon: el nucleo —indexar, transcribir,
    sincronizar, hornear— lleva meses corriendo en ese interprete, y si el venv
    fuera primero, TODO el pipeline cambiaria de interprete de golpe (aqui, de
    3.14 a 3.12) como efecto lateral de haber cableado la identidad. Eso es un
    cambio grande disfrazado de detalle, y romperia la promesa de que Bernardo y
    Adrian no notan nada.

    Asi, `nucleo` se resuelve al de siempre y solo las capacidades que el
    sistema NO tiene —caras, voz, eventos— caen al venv. Que es justo lo que
    hace falta.

    `CINEMA_PY` va antes que todo: es el override explicito del usuario.
    """
    fuera = []
    extra = os.environ.get("CINEMA_PY")
    if extra and Path(extra).exists():
        fuera.append(extra)
    sistema = shutil.which("python3")
    if sistema:
        fuera.append(sistema)
    venv = ENGINE / ".venv" / "bin" / "python"
    if venv.exists():
        fuera.append(str(venv))
    # Sin duplicados, conservando el orden.
    return list(dict.fromkeys(fuera))


def _firma() -> str:
    """Cambia cuando cambia algo que invalide el cache."""
    partes = []
    for ruta in _candidatos():
        try:
            partes.append(f"{ruta}:{Path(ruta).stat().st_mtime_ns}")
        except OSError:
            partes.append(f"{ruta}:?")
    return "|".join(partes)


def _leer_cache() -> dict | None:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("firma") != _firma():
        return None
    if time.time() - d.get("ts", 0) > CACHE_TTL_SEG:
        return None
    return d.get("mapa")


def _escribir_cache(mapa: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(
            {"firma": _firma(), "ts": time.time(), "mapa": mapa},
            indent=2), encoding="utf-8")
    except OSError:
        pass        # el cache es una optimizacion, no un requisito


def _probar(ruta: str, modulos: list[str]) -> bool:
    """Un subproceso por interprete y capacidad. Se importa DE VERDAD: un
    paquete presente pero roto (onnxruntime sin su .dylib) tiene que contar
    como ausente, no como disponible."""
    codigo = "import " + ", ".join(modulos)
    try:
        r = subprocess.run([ruta, "-c", codigo], capture_output=True,
                           timeout=180)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def mapa_de_capacidades(*, usar_cache: bool = True) -> dict[str, str | None]:
    """{capacidad: ruta del interprete que la provee, o None}."""
    if usar_cache:
        cacheado = _leer_cache()
        if cacheado is not None:
            return cacheado

    mapa: dict[str, str | None] = {}
    for cap, modulos in CAPACIDADES.items():
        elegido = None
        for ruta in _candidatos():
            if _probar(ruta, modulos):
                elegido = ruta
                break
        mapa[cap] = elegido

    if usar_cache:
        _escribir_cache(mapa)
    return mapa


def interprete_para(capacidad: str, mapa: dict | None = None) -> str | None:
    """El interprete que puede correr un paso que necesita `capacidad`."""
    m = mapa if mapa is not None else mapa_de_capacidades()
    return m.get(capacidad)


def interprete_para_todas(capacidades: list[str],
                          mapa: dict | None = None) -> tuple[str | None, list[str]]:
    """Un paso puede necesitar varias capacidades a la vez.

    Devuelve (interprete, faltantes). El interprete tiene que ser UNO solo que
    las tenga TODAS: no se puede correr medio script en un python y medio en
    otro. Si alguna falta, el paso no corre.
    """
    m = mapa if mapa is not None else mapa_de_capacidades()
    faltan = [c for c in capacidades if not m.get(c)]
    if faltan:
        return None, faltan

    # Interseccion: un interprete que las tenga todas.
    candidatos = [m[c] for c in capacidades]
    if len(set(candidatos)) == 1:
        return candidatos[0], []
    for ruta in _candidatos():
        if all(_probar(ruta, CAPACIDADES[c]) for c in capacidades):
            return ruta, []
    return None, capacidades


COMO_INSTALAR = (
    "bash ~/cinema-assistant/bin/instalar_extras.sh   "
    "(instala en el venv: opencv, insightface, onnxruntime, scikit-learn, "
    "resemblyzer, librosa, panns_inference; ~2 GB)")
