#!/usr/bin/env python3
"""Inventario de capacidades de esta Mac: que puede y que no puede hacer el motor.

Por cada capacidad dice QUE INTERPRETE o binario la provee, con su version. No
instala nada; solo mira y reporta.

POR QUE EXISTE (2026-08-05). El motor tiene ~4700 lineas de identidad por cara y
voz —InsightFace, ArcFace, Resemblyzer, PANNs— con las dependencias instaladas y
los modelos en disco. Nada de eso corrio nunca, porque `run_full_pipeline.sh`
invoca `python3` pelado, que en esta Mac es el de Homebrew (3.14.5) y no tiene
cv2 ni insightface ni sklearn. Las dependencias estan en el venv (3.12.13), que
el pipeline nunca activa.

Esa desconexion llevaba meses sin que nadie la viera, porque nada la miraba. Este
script es lo que la habria hecho visible el dia 1: una tabla de una pantalla que
dice "caras: NO en el interprete que usa el pipeline".

Uso:
    python3 bin/doctor.py            # tabla legible
    python3 bin/doctor.py --json     # para el orquestador
    python3 bin/doctor.py --import-real   # importa de verdad, no solo find_spec
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
HOME = Path.home()

# Una capacidad es un conjunto de modulos que tienen que importar TODOS. El
# nombre es el que usan los pasos del pipeline al declarar `requiere=[...]`.
CAPACIDADES: dict[str, list[str]] = {
    "nucleo":  ["numpy", "PIL"],
    "waveform": ["scipy"],
    "caras":   ["cv2", "insightface", "onnxruntime"],
    "cluster": ["sklearn"],
    "voz":     ["resemblyzer", "soundfile", "librosa"],
    "eventos": ["panns_inference"],
    "diarizacion": ["pyannote.audio"],
}

# Que se rompe si falta cada una. Sin esto la tabla dice "NO" y el usuario no
# sabe si eso le importa.
CONSECUENCIA = {
    "nucleo":  "nada funciona: indexar, analizar, hornear",
    "waveform": ("sin las herramientas manuales de correlacion fina: "
                 "apply_waveform_sync, compute_sync_offset, sync_siblings_rule. "
                 "El pipeline automatico NO la necesita (sync_refiner cae a numpy)"),
    "caras":   "sin deteccion ni catalogo de caras (detect_faces, build_face_catalog)",
    "cluster": "sin agrupar caras ni voces (DBSCAN, aglomerativo)",
    "voz":     "sin embeddings de voz ni diarizacion local",
    "eventos": "sin eventos sonoros (aplausos, risas, musica)",
    "diarizacion": "opcional: pyannote. El fallback local cubre 1-2 hablantes",
}

BINARIOS = {
    "ffmpeg":     ("nucleo", "decodificar y muestrear todo el material"),
    "ffprobe":    ("nucleo", "leer metadata de cada archivo al indexar"),
    "mediainfo":  ("nucleo", "fallback de ffprobe"),
    "exiftool":   ("nucleo", "hora de inicio de los WAV de campo (BWF)"),
    "whisper-cli": ("transcripcion", "transcribir (whisper.cpp)"),
    "fpcalc":     ("huella", "huella acustica (chromaprint)"),
    "lua":        ("resolve", "probar el bake contra el mock sin abrir Resolve"),
    "luac":       ("resolve", "validar la sintaxis del horneado antes de pegarlo"),
}

MODELOS = {
    "whisper turbo": ENGINE / "models" / "ggml-large-v3-turbo.bin",
    "insightface buffalo_l": HOME / ".insightface" / "models" / "buffalo_l",
    "panns cnn14": HOME / "panns_data" / "Cnn14_mAP=0.431.pth",
    "panns labels": HOME / "panns_data" / "class_labels_indices.csv",
}

SONDA = r"""
import json, sys
try:
    from importlib.util import find_spec
except Exception:
    find_spec = None
mods = json.loads(sys.argv[1])
real = sys.argv[2] == "1"
out = {}
for m in mods:
    try:
        if real:
            __import__(m)
            out[m] = True
        else:
            out[m] = find_spec(m) is not None
    except Exception:
        out[m] = False
print(json.dumps({"version": "%d.%d.%d" % sys.version_info[:3], "mods": out}))
"""


def interpretes() -> list[tuple[str, str]]:
    """(etiqueta, ruta) de los interpretes que vale la pena sondear.

    Orden deliberado: primero el que usa HOY el pipeline, porque es el que
    determina que corre de verdad."""
    fuera = []
    sistema = shutil.which("python3")
    if sistema:
        fuera.append(("python3 (el del pipeline)", sistema))
    venv = ENGINE / ".venv" / "bin" / "python"
    if venv.exists():
        fuera.append(("venv del motor", str(venv)))
    extra = os.environ.get("CINEMA_PY")
    if extra and Path(extra).exists() and extra not in [r for _, r in fuera]:
        fuera.append(("$CINEMA_PY", extra))
    return fuera


def sondear(ruta: str, modulos: list[str], import_real: bool) -> dict:
    """Un solo subproceso por interprete: pregunta por TODOS los modulos de una."""
    try:
        r = subprocess.run(
            [ruta, "-c", SONDA, json.dumps(modulos), "1" if import_real else "0"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"version": "?", "mods": {m: False for m in modulos},
                    "error": (r.stderr or "").strip()[:200]}
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:  # interprete roto, timeout, JSON ilegible
        return {"version": "?", "mods": {m: False for m in modulos},
                "error": f"{type(e).__name__}: {e}"[:200]}


def version_binario(nombre: str, ruta: str) -> str:
    flags = {"lua": "-v", "luac": "-v", "exiftool": "-ver",
             "mediainfo": "--Version", "fpcalc": "-version"}
    flag = flags.get(nombre, "-version" if nombre.startswith("ff") else "--version")
    try:
        r = subprocess.run([ruta, flag], capture_output=True, text=True, timeout=20)
        txt = ((r.stdout or "") + " " + (r.stderr or "")).strip()
        return txt.splitlines()[0][:60] if txt else "?"
    except Exception:
        return "?"


def whisper_tiene_metal(ruta: str) -> bool | None:
    """En Apple Silicon, Metal es un 2-4x gratis. Nadie tenia hoy este dato.

    OJO CON EL METODO (2026-08-05): la primera version de esta funcion miraba
    `otool -L` buscando el framework Metal, y devolvia NO. Era falso. ggml carga
    sus backends como plugins dinamicos en tiempo de ejecucion, asi que el
    binario no enlaza Metal aunque lo use; `otool` da 0 coincidencias y aun asi
    el arranque imprime `ggml_metal_library_init: using embedded metal library`.

    La unica sonda fiable es arrancarlo y leer lo que dice. Es el mismo error
    que la leccion de la a6700: dar algo por ausente sin comprobar que el metodo
    de medicion pueda verlo."""
    try:
        r = subprocess.run([ruta, "--help"], capture_output=True,
                           text=True, timeout=30)
        salida = (r.stdout or "") + (r.stderr or "")
        if "ggml_metal" in salida:
            return True
        # Arranco y no menciono Metal: es un build sin ese backend.
        return False if salida.strip() else None
    except Exception:
        return None


def _tam(p: Path) -> str:
    try:
        if p.is_dir():
            b = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        else:
            b = p.stat().st_size
    except OSError:
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:.0f}{u}" if u == "B" else f"{b:.1f}{u}"
        b /= 1024.0
    return "?"


def recolectar(import_real: bool = False) -> dict:
    todos = sorted({m for mods in CAPACIDADES.values() for m in mods})
    py = {}
    for etiqueta, ruta in interpretes():
        r = sondear(ruta, todos, import_real)
        caps = {}
        for cap, mods in CAPACIDADES.items():
            faltan = [m for m in mods if not r["mods"].get(m)]
            caps[cap] = {"ok": not faltan, "faltan": faltan}
        py[etiqueta] = {"ruta": ruta, "version": r["version"],
                        "capacidades": caps, "error": r.get("error")}

    bins = {}
    for nombre, (_grupo, para_que) in BINARIOS.items():
        ruta = shutil.which(nombre)
        d = {"ok": bool(ruta), "ruta": ruta, "para_que": para_que,
             "version": version_binario(nombre, ruta) if ruta else None}
        if nombre == "whisper-cli" and ruta:
            d["metal"] = whisper_tiene_metal(ruta)
        bins[nombre] = d

    mods = {n: {"ok": p.exists(), "ruta": str(p),
                "tam": _tam(p) if p.exists() else None}
            for n, p in MODELOS.items()}

    # Quien provee cada capacidad: el PRIMER interprete que la tiene, en el
    # orden de interpretes(). Si el del pipeline no la tiene pero el venv si,
    # esa discrepancia es justo el hallazgo que hay que gritar.
    provee = {}
    for cap in CAPACIDADES:
        provee[cap] = next(
            (e for e, d in py.items() if d["capacidades"][cap]["ok"]), None)

    return {"interpretes": py, "binarios": bins, "modelos": mods,
            "provee": provee, "engine": str(ENGINE)}


def _si_no(ok) -> str:
    return "ok " if ok else ("NO " if ok is False else "?  ")


def imprimir(inv: dict) -> int:
    print("=" * 72)
    print("DOCTOR — que puede hacer el motor en esta Mac")
    print("=" * 72)

    print("\nINTERPRETES")
    for etiqueta, d in inv["interpretes"].items():
        print(f"  {etiqueta}  ({d['version']})  {d['ruta']}")
        if d.get("error"):
            print(f"      error al sondear: {d['error']}")
        listo = [c for c, v in d["capacidades"].items() if v["ok"]]
        falta = [c for c, v in d["capacidades"].items() if not v["ok"]]
        print(f"      tiene: {', '.join(listo) if listo else '(nada)'}")
        if falta:
            print(f"      NO tiene: {', '.join(falta)}")

    print("\nCAPACIDADES")
    for cap in CAPACIDADES:
        quien = inv["provee"][cap]
        estado = _si_no(bool(quien))
        donde = quien if quien else "ningun interprete"
        print(f"  {estado} {cap:<12} {donde}")
        if not quien:
            print(f"        sin esto: {CONSECUENCIA[cap]}")

    print("\nBINARIOS")
    for nombre, d in inv["binarios"].items():
        extra = ""
        if nombre == "whisper-cli" and d["ok"]:
            m = d.get("metal")
            extra = ("  [Metal: si]" if m else
                     "  [Metal: NO — se pierde un 2-4x en Apple Silicon; "
                     "recompilar whisper.cpp con WHISPER_METAL=1]"
                     if m is False else "")
        print(f"  {_si_no(d['ok'])} {nombre:<12} {d['version'] or d['para_que']}{extra}")

    print("\nMODELOS")
    for nombre, d in inv["modelos"].items():
        print(f"  {_si_no(d['ok'])} {nombre:<24} {d['tam'] or d['ruta']}")

    # El diagnostico que motivo este script.
    print()
    problemas = []
    pipeline = next(iter(inv["interpretes"].values()), None)
    if pipeline:
        for cap, v in pipeline["capacidades"].items():
            if not v["ok"] and inv["provee"][cap]:
                problemas.append(
                    f"  '{cap}' existe en '{inv['provee'][cap]}' pero NO en el "
                    f"interprete que usa el pipeline.\n"
                    f"      Falta ahi: {', '.join(v['faltan'])}\n"
                    f"      Efecto: {CONSECUENCIA[cap]}")
    if problemas:
        print("=" * 72)
        print("CAPACIDADES INSTALADAS QUE EL PIPELINE NO PUEDE USAR")
        print("=" * 72)
        print("\n".join(problemas))
        print("\n  El paso que las necesite tiene que invocar el interprete que\n"
              "  las tiene. Mientras eso no pase, ese trabajo no se hace — y\n"
              "  hasta hoy no se hacia en silencio.")
    sin_nada = [c for c, q in inv["provee"].items() if not q]
    if sin_nada:
        print(f"\n  Sin ningun interprete: {', '.join(sin_nada)}")
        print("  Para instalarlas:  bash bin/instalar_extras.sh   (aun no existe)")

    falta_nucleo = not inv["provee"].get("nucleo")
    falta_bin = [n for n, d in inv["binarios"].items()
                 if not d["ok"] and BINARIOS[n][0] == "nucleo"]
    if falta_nucleo or falta_bin:
        print(f"\n  FALTA EL NUCLEO: {', '.join(falta_bin) or 'modulos base'}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="salida JSON para el orquestador")
    ap.add_argument("--import-real", action="store_true",
                    help="importa de verdad en vez de find_spec (lento pero "
                         "detecta paquetes instalados pero rotos)")
    args = ap.parse_args()

    inv = recolectar(import_real=args.import_real)
    if args.json:
        print(json.dumps(inv, indent=2, ensure_ascii=False))
        return 0 if inv["provee"].get("nucleo") else 1
    return imprimir(inv)


if __name__ == "__main__":
    sys.exit(main())
