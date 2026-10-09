#!/usr/bin/env python3
"""Ingenieria inversa de un montaje ya hecho en DaVinci Resolve.

QUE RESUELVE
`lib/timeline_resolve.py` sabe LEER una timeline. Este script la INTERPRETA:
separa las piezas entregadas del material de trabajo, decide que gramatica de
montaje usa cada una, y separa el A-roll del B-roll, de los reencuadres y de los
graficos. El resultado es un corpus JSON que responde la pregunta del encargo:
que toma se eligio, donde entro cada plano de B-roll y por cuanto tiempo.

COMO SE IDENTIFICA UNA PIEZA — dos filtros, y hacen falta los dos
  1. DURACION. El bloque de una pieza calza con su export a +-0.15 s.
     No sirve un umbral tipo "todo bloque > 25 s es una pieza": en `Reels 11-13`
     los bloques 21 y 25 son selects de B-roll de 35.5 s y 27.5 s, mas largos que
     la pieza mas corta del dia.
  2. LOS CORTES. La duracion sola tampoco basta: en el dia 1, `Reel 001 v4` mide
     40.33 s y `Reel 3 V003` 40.37 s — cuatro centesimas. Con solo la duracion,
     dos piezas distintas casaban con el MISMO export y el corpus salia con un
     reel repetido y otro perdido. Se decide MIRANDO si el export ensena los
     cortes que la timeline declara.

LAS TRES GRAMATICAS, medidas sobre los reels de Espinosa (2026-08-27)
  voice-over  A-roll continuo de UNA toma + insertos encima en pistas altas.
              Es la que produce "momentos de B-roll".
  a-roll-puro Igual pero sin insertos superpuestos.
  secuencial  V1 encadena clips DISTINTOS, cada uno con su propio audio. Aqui no
              hay "B-roll sobre A-roll" porque no hay A-roll continuo.

QUE NO ES B-ROLL, aunque viva en una pista alta
  - Los graficos (cortinilla, titulos): son plantilla, no seleccion de material.
  - Los REENCUADRES: un item en pista alta cuya fuente es la MISMA toma del
    A-roll. Es un punch-in, no un corte a otra imagen. En el reel 0011 hay 12 de
    estos y contarlos como B-roll triplicaria la cuenta.

EL `In` DE RESOLVE VA EN FOTOGRAMAS DE LA FUENTE, no de la timeline. Comprobado
por rango el 2026-08-27: C1332 dura 19.5 s y tiene un item con In=1067, que a
29.97 fps (el de la timeline) no existiria — el clip solo tiene 585 fotogramas a
esa tasa. A 59.94, que es su tasa real, si. Importa porque el dia 1 monta a
23.976 material de 29.97, y confundirlos desplaza el fotograma buscado.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import timeline_resolve as tr  # noqa: E402

TOL_EXPORT_S = 0.15
# Plantilla grafica: no es seleccion de material, es diseno. Se cuenta aparte.
# "hook" entro en la lista el 2026-08-27: el dia 1 abre sus cuatro reels con una
# placa animada `Hook Apertura.mp4`, y contarla como B-roll decia que ese dia
# "abre con recurso en el segundo 0.0" cuando lo que abre es una placa de texto.
GRAFICOS = ("cortinilla", "titulo", "título", "hook", "logo", "placa", "bumper")

# Extensiones de FOTO FIJA. No son grafico (son material del rodaje) ni clip de
# video. El dia 1 mete dos fotos de la sesion como inserto y merecen contarse
# como lo que son.
FOTO_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic")

# Fraccion minima de los cortes declarados que el export debe ensenar para dar el
# emparejamiento por bueno. Medido sobre el reel 0011: el acierto casa el 68 % y
# los dos rivales del mismo dia, 12 % y 20 %. La separacion es limpia.
# No llega al 100 % porque no todo corte de timeline cambia la imagen: un corte
# de audio bajo el mismo plano, o un punch-in muy suave, no se ven.
CORTES_MIN = 0.45

_FPS_CACHE: dict[str, float] = {}
_CORTES_CACHE: dict[str, list[float]] = {}


# --------------------------------------------------------------------------
# sondeo de archivos
# --------------------------------------------------------------------------

def _ffprobe(path: str, entries: str) -> str:
    try:
        return subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", entries, "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return ""


def dur_video(p: Path) -> float | None:
    out = _ffprobe(str(p), "format=duration")
    try:
        return float(out.split(",")[0])
    except Exception:
        return None


def fps_de(path: str) -> float:
    if path not in _FPS_CACHE:
        try:
            n, d = _ffprobe(path, "stream=r_frame_rate").split("/")
            _FPS_CACHE[path] = float(n) / float(d)
        except Exception:
            _FPS_CACHE[path] = 29.97
    return _FPS_CACHE[path]


def cortes_detectados(path: str, umbral: float = 0.25) -> list[float]:
    """Cortes VISIBLES del export, por deteccion de escena.

    Se cachea: el mismo .mov se compara contra varios bloques y sondear un 4K
    vertical no es barato.

    POR QUE ASI Y NO COMPARANDO LA IMAGEN CONTRA LA FUENTE: se intento primero y
    no discrimina. El editor reencuadra cada inserto de forma distinta para el
    9:16 y ademas hay correccion de color S-Log3 -> Rec709, asi que el mismo
    plano correlaciona 0.80 en un inserto y 0.12 en el de al lado. El patron de
    cortes es inmune a las dos cosas.
    """
    if path not in _CORTES_CACHE:
        try:
            err = subprocess.run(
                ["ffmpeg", "-v", "info", "-i", path, "-vf",
                 f"select='gt(scene,{umbral})',showinfo", "-f", "null", "-"],
                capture_output=True, text=True, timeout=900).stderr
            _CORTES_CACHE[path] = sorted(
                float(m) for m in re.findall(r"pts_time:([0-9.]+)", err))
        except Exception:
            _CORTES_CACHE[path] = []
    return _CORTES_CACHE[path]


def exports_de(carpeta: Path) -> list[dict]:
    if not carpeta.is_dir():
        return []
    out = []
    for p in sorted(carpeta.iterdir()):
        if p.name.startswith("._") or p.suffix.lower() not in (".mov", ".mp4"):
            continue
        d = dur_video(p)
        if d:
            out.append({"nombre": p.name, "dur": d, "mtime": p.stat().st_mtime})
    return out


# --------------------------------------------------------------------------
# interpretacion del montaje
# --------------------------------------------------------------------------

def es_grafico(ruta: str) -> bool:
    base = ruta.rsplit("/", 1)[-1].lower()
    return any(g in base for g in GRAFICOS)


def es_foto(ruta: str) -> bool:
    return ruta.lower().endswith(FOTO_EXT)


def analizar_pieza(tl: tr.Timeline, a: int, b: int) -> dict:
    """Separa A-roll, reencuadres, B-roll y graficos dentro de un bloque."""
    def dentro(it):
        return it.inicio < b and it.fin > a

    def rel(f):
        return round(tl.seg(f) - tl.seg(a), 3)

    v_items, a_items = [], []
    for p in tl.pistas:
        for it in p.items:
            if not dentro(it):
                continue
            if p.tipo == "V":
                v_items.append((p, it))
            elif p.tipo == "A":
                a_items.append((p, it))

    # A-roll: la fuente que esta en V1 y ADEMAS suena en A1 (imagen con su sonido)
    fuentes_v1 = {it.ruta for p, it in v_items if p.indice == 1 and it.ruta}
    a1_rutas = {it.ruta for p, it in a_items if p.indice == 1 and it.ruta}
    aroll_srcs = (fuentes_v1 & a1_rutas) or fuentes_v1

    trozos_a1 = sorted(
        [{"ini": rel(it.inicio), "fin": rel(it.fin),
          "dur": round(it.duracion / tl.fps, 3), "in_f": it.entrada,
          "src": it.ruta.rsplit("/", 1)[-1]}
         for p, it in a_items if p.indice == 1 and it.ruta],
        key=lambda x: x["ini"])

    broll, reencuadres, graficos, fotos = [], [], [], []
    for p, it in sorted(v_items, key=lambda x: x[1].inicio):
        if not it.ruta:
            continue
        reg = {"pista": p.etiqueta, "ini": rel(it.inicio), "fin": rel(it.fin),
               "dur": round(it.duracion / tl.fps, 3), "in_f": it.entrada,
               "src": it.ruta.rsplit("/", 1)[-1], "ruta": it.ruta}
        if es_grafico(it.ruta):
            graficos.append(reg)
        elif es_foto(it.ruta):
            fotos.append(reg)
        elif p.indice == 1:
            continue                                # el A-roll (o la cadena)
        elif it.ruta in aroll_srcs:
            reencuadres.append(reg)                 # punch-in sobre la misma toma
        else:
            broll.append(reg)

    gramatica = ("secuencial" if len(fuentes_v1) > 2
                 else "voice-over" if broll else "a-roll-puro")

    return {
        "gramatica": gramatica,
        "aroll": {
            "fuentes": sorted(s.rsplit("/", 1)[-1] for s in aroll_srcs),
            "n_trozos_a1": len(trozos_a1),
            "trozos_a1": trozos_a1,
        },
        "broll": broll,
        "reencuadres": reencuadres,
        "graficos": graficos,
        "fotos": fotos,
        "musica": [{"ini": rel(it.inicio), "dur": round(it.duracion / tl.fps, 3),
                    "in_f": it.entrada, "src": it.ruta.rsplit("/", 1)[-1]}
                   for p, it in a_items if p.indice >= 2 and it.ruta],
        "subtitulos": sorted(
            [{"ini": rel(it.inicio), "fin": rel(it.fin),
              "texto": it.nombre.replace("<br>", "\n")}
             for it in tl.items_de("ST") if dentro(it)],
            key=lambda x: x["ini"]),
        "cortes_imagen_rel": sorted({rel(f) for f in tl.cortes_imagen((a, b))}),
        "cortes_solo_imagen": [rel(f) for f in tl.cortes_solo_imagen((a, b))],
        "cortes_sincronicos": [rel(f) for f in tl.cortes_sync((a, b))],
    }


def verificar(pz: dict, export: Path, dur: float) -> float:
    """Fraccion de los cortes que la timeline declara que el export ENSENA."""
    teoricos = [x for x in pz["cortes_imagen_rel"] if 0.3 < x < dur - 0.3]
    if not teoricos:
        return 0.0
    det = cortes_detectados(str(export))
    return sum(1 for x in teoricos
               if any(abs(x - d) < 0.25 for d in det)) / len(teoricos)


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proyecto", required=True)
    ap.add_argument("--timeline", action="append", required=True,
                    help="nombre::carpeta_de_exports::project_config (repetible)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    corpus = {"proyecto": args.proyecto, "piezas": [], "descartados": []}
    for spec in args.timeline:
        nombre, exp_dir, cfg = spec.split("::")
        tl = tr.leer_timeline(args.proyecto, nombre, config=cfg or None)
        exps = exports_de(Path(exp_dir))
        print(f"\n=== {nombre} — {len(tl.bloques())} bloques, {len(exps)} exports")
        for i, (a, b) in enumerate(tl.bloques(), 1):
            dur = (b - a) / tl.fps
            cand = [e for e in exps if abs(e["dur"] - dur) <= TOL_EXPORT_S]
            if not cand:
                corpus["descartados"].append(
                    {"timeline": nombre, "bloque": i, "dur": round(dur, 2),
                     "razon": "ningun export con esa duracion"})
                continue
            pz = analizar_pieza(tl, a, b)
            marcados = sorted(
                ((verificar(pz, Path(exp_dir) / e["nombre"], dur), e) for e in cand),
                key=lambda x: (-x[0], -x[1]["mtime"]))
            mejor, exp = marcados[0]
            if mejor < CORTES_MIN:
                corpus["descartados"].append(
                    {"timeline": nombre, "bloque": i, "dur": round(dur, 2),
                     "razon": f"ningun export ensena los cortes de este bloque "
                              f"(mejor {mejor:.0%} < {CORTES_MIN:.0%})",
                     "candidatos_por_duracion": [e["nombre"] for e in cand]})
                continue
            pz.update({
                "timeline": nombre, "bloque": i,
                "dur_bloque_s": round(dur, 3), "export": exp["nombre"],
                "dur_export_s": round(exp["dur"], 3), "fps": round(tl.fps, 3),
                "modificada": tl.modificada,
                "verificacion": {
                    "cortes_casados": round(mejor, 3),
                    "rivales": [{"export": e["nombre"],
                                 "cortes_casados": round(c, 3)}
                                for c, e in marcados[1:]]},
            })
            corpus["piezas"].append(pz)
            print(f"  bloque {i:2d} {dur:6.2f}s -> {exp['nombre']:26s} "
                  f"cortes={mejor:4.0%} [{pz['gramatica']:11s}] "
                  f"A-roll={','.join(pz['aroll']['fuentes'])[:24]:24s} "
                  f"B={len(pz['broll']):2d} reenc={len(pz['reencuadres']):2d}")

    Path(args.out).write_text(json.dumps(corpus, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    g: dict[str, int] = {}
    for p in corpus["piezas"]:
        g[p["gramatica"]] = g.get(p["gramatica"], 0) + 1
    print(f"\n{len(corpus['piezas'])} piezas -> {args.out}")
    print(f"  gramaticas: {g}")
    print(f"  insertos de B-roll: {sum(len(p['broll']) for p in corpus['piezas'])}")
    print(f"  bloques descartados: {len(corpus['descartados'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
