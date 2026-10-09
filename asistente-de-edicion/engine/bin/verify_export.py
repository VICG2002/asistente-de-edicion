#!/usr/bin/env python3
"""El entregable se MIRA, no solo se mide. Vistas de cada frontera de corte.

POR QUE EXISTE
El motor ya mide el entregable: `bin/verify_audio.py` saca LUFS, LRA, true peak
y muestras a fondo de escala, y encontro siete de catorce exports con clipping
duro que nadie habia visto. Pero medir el sonido no es mirar la pieza. Un salto
de imagen en un corte, un fundido que se comio un fotograma, un subtitulo que
quedo detras de una capa: nada de eso sale en un numero, y todo eso viaja al
entregable igual.

Esto genera las vistas —`bin/vista_tramo.py`, imagen mas onda en la misma
regla— en cada frontera de corte y en los extremos, y escribe un reporte con
QUE hay que comprobar en cada una. Mirarlas es trabajo del asistente; ponerlas
delante es trabajo de este script.

MIDE Y AVISA, NO REPRUEBA
Mismo contrato que `verify_audio.py` (decision de Victor, 2026-08-19). Aqui
ademas es obligado: si un corte salta o no es juicio editorial, y ese juicio no
lo firma un umbral.

DE DONDE SALEN LAS FRONTERAS
  Con `--proyecto` y `--timeline`, de la timeline de Resolve leida en frio
  (`lib/timeline_resolve.py`): son los cortes que el editor DECIDIO.
  Sin ellos, de la deteccion de escena sobre el propio export: son los cortes
  que se VEN. No es lo mismo y el reporte lo dice — un corte solo-imagen sobre
  audio continuo se detecta igual, pero un corte entre dos planos casi
  identicos no.

LA TRAMPA DE LOS BLOQUES
La duracion de la timeline NO es la duracion del corte. En Morsa la timeline
media 78:10 y solo 24:56 eran contenido, en 7 bloques; lo entregado era el
PRIMER bloque, 16:35.7. Confundirlos es un error de 61 minutos. Por eso los
tiempos de timeline se trasladan al export restando el inicio del bloque, el
bloque se elige con `--bloque` y CUAL se uso queda escrito en el reporte.

EL TOPE DE TRES PASADAS
Corregir, volver a mirar y corregir otra vez converge o no converge. Si a la
tercera sigue habiendo algo, no es un problema que se arregle mirando mas: se
le dice al editor. Este script se niega a una cuarta pasada.

Uso:
    python3 bin/verify_export.py --root <disco>
    python3 bin/verify_export.py --archivo <export.mov> --frames 6
    python3 bin/verify_export.py --root <disco> --archivo <export.mov> \\
        --proyecto "<proyecto>" --timeline "<nombre>" --config project_config.json
    python3 bin/verify_export.py --root <disco> --pasada 2
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import frames as F  # noqa: E402

EXTS = (".mov", ".mp4", ".mxf", ".mkv")
VENTANA = 1.5          # segundos a cada lado de la frontera
MAX_PASADAS = 3
MEDIOS = 3             # puntos de muestreo dentro del cuerpo de la pieza

QUE_MIRAR = [
    "salto o flash de imagen en el corte",
    "pico de onda en la frontera — el clic que se colo pese al fundido",
    "subtitulo tapado por una capa superior",
    "continuidad de color entre los dos planos",
]


def _vista_tramo():
    spec = importlib.util.spec_from_file_location(
        "vista_tramo", HERE / "vista_tramo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resoluble: {root_arg}")


def duracion(path: Path) -> float:
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True,
            timeout=60)
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def fronteras_de_timeline(proyecto: str, timeline: str, config: str | None,
                          bloque: int) -> tuple[list[float], str]:
    """Los cortes que el editor DECIDIO, ya en tiempo de export."""
    from lib.timeline_resolve import leer_timeline
    tl = leer_timeline(proyecto, timeline, config=config)
    bl = tl.bloques()
    if not bl:
        return [], f"timeline {timeline!r} sin bloques con contenido"
    idx = max(1, min(bloque, len(bl))) - 1
    ini, fin = bl[idx]
    cortes = [tl.seg(f) - tl.seg(ini) for f in tl.cortes_sync()
              if ini <= f <= fin]
    nota = (f"timeline `{timeline}` del proyecto `{proyecto}`, bloque {idx+1} "
            f"de {len(bl)}: {tl.seg(fin) - tl.seg(ini):.1f} s de contenido "
            f"dentro de {tl.duracion_s:.1f} s de timeline. Cortes SINCRONICOS "
            f"— los que cortan imagen y sonido a la vez.")
    return sorted(t for t in cortes if t > 0), nota


def fronteras_por_escena(export: Path, dur: float) -> tuple[list[float], str]:
    """Los cortes que se VEN, detectados sobre el propio archivo."""
    with tempfile.TemporaryDirectory() as d:
        cands = F.extraer_escenas(str(export), Path(d), 0.0, dur, ancho=320)
        cortes = [c["t_sec"] for c in cands if c["motivo"] == "escena"]
    nota = ("deteccion de escena sobre el propio export. **No son los cortes "
            "de la timeline**: un corte entre dos planos casi identicos no se "
            "detecta, y un cambio de luz fuerte puede contarse como corte. "
            "Para los cortes reales, pasa `--proyecto` y `--timeline`.")
    return sorted(cortes), nota


def fusionar(puntos: list[tuple[float, str]],
             ventana: float = VENTANA) -> tuple[list[tuple[float, str]], int]:
    """Dos puntos a menos de una ventana producen la MISMA vista.

    Mismo principio que el dedup de frames: no se mira dos veces lo mismo. Y
    cuando dos se pisan, gana el corte sobre el punto de muestreo — el corte es
    una decision del editor; el medio es solo un sitio donde asomarse.
    """
    if not puntos:
        return [], 0
    orden = sorted(puntos, key=lambda x: x[0])
    out = [orden[0]]
    caidos = 0
    for t, etiqueta in orden[1:]:
        t0, e0 = out[-1]
        if t - t0 < ventana:
            caidos += 1
            if etiqueta.startswith("corte") and not e0.startswith("corte"):
                out[-1] = (t, etiqueta)
            continue
        out.append((t, etiqueta))
    return out, caidos


def puntos_de_muestreo(dur: float) -> list[tuple[float, str]]:
    """Los extremos y unos medios. Los extremos porque es donde se cuela un
    fotograma negro o una cola de audio, y los medios para juzgar la
    consistencia del color a lo largo de la pieza."""
    out = [(min(2.0, dur / 2), "arranque")]
    for i in range(1, MEDIOS + 1):
        out.append((dur * i / (MEDIOS + 1), f"medio {i}"))
    out.append((max(0.0, dur - 2.0), "cierre"))
    return [(t, e) for t, e in out if 0 <= t <= dur]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--archivo", default="",
                    help="Un export concreto. Por defecto, todos los del disco.")
    ap.add_argument("--proyecto", default="",
                    help="Proyecto de Resolve, para leer los cortes reales")
    ap.add_argument("--timeline", default="")
    ap.add_argument("--config", default="",
                    help="project_config.json, para clasificar las pistas")
    ap.add_argument("--bloque", type=int, default=1,
                    help="Que bloque de la timeline se entrego (1 = el primero)")
    ap.add_argument("--frames", type=int, default=6,
                    help="Frames por vista")
    ap.add_argument("--max-cortes", type=int, default=40,
                    help="Tope de fronteras a mirar. Si hay mas, se reparten "
                         "y SE DICE cuantas quedaron fuera.")
    ap.add_argument("--pasada", type=int, default=1,
                    help=f"Que ronda de revision es. Maximo {MAX_PASADAS}.")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.pasada > MAX_PASADAS:
        sys.exit(
            f"Pasada {args.pasada}: por encima del tope de {MAX_PASADAS}.\n"
            "Tres rondas de corregir y volver a mirar convergen o no convergen. "
            "Lo que queda se le DICE al editor, con la lista de lo que sigue "
            "mal y por que no se corrigio. No se mira una cuarta vez.")

    if not args.root and not args.archivo:
        sys.exit("Hace falta --root, o --archivo")

    root = resolve_root(args.root) if args.root else None
    if args.archivo:
        exports = [Path(args.archivo)]
    else:
        exports = sorted(p for p in root.rglob("*")
                         if p.is_file() and p.suffix.lower() in EXTS
                         and not p.name.startswith("._")
                         and ".cinema_assistant" not in p.parts)
    exports = [p for p in exports if p.exists()]
    if not exports:
        print("No hay exports que mirar.")
        return

    vt = _vista_tramo()
    hoy = dt.date.today()
    # Sin --root, las vistas van JUNTO al export, no al directorio de trabajo:
    # el cwd es lo que sea que tuviera la terminal, y eso siembra carpetas en
    # sitios que nadie eligio (paso: se sembro una en el propio motor).
    base_vistas = ((root / ".cinema_assistant" / "vistas" / "export")
                   if root else exports[0].parent / "vistas_export")

    L = [f"# El entregable, mirado — {hoy.isoformat()}\n",
         f"Generado con `bin/verify_export.py`, pasada {args.pasada} de "
         f"{MAX_PASADAS}. **Mide y avisa, no reprueba**: si un corte salta es "
         f"juicio editorial y ese juicio no lo firma un umbral.\n",
         "En cada vista hay que comprobar:\n"]
    L += [f"{i}. {q}" for i, q in enumerate(QUE_MIRAR, 1)]
    L.append("")

    total_vistas = 0
    for export in exports:
        dur = duracion(export)
        if dur <= 0:
            L.append(f"\n## `{export.name}`\n\nffprobe no le saca duracion. "
                     f"Se salta.\n")
            continue

        if args.proyecto and args.timeline:
            try:
                cortes, nota = fronteras_de_timeline(
                    args.proyecto, args.timeline, args.config or None,
                    args.bloque)
            except Exception as e:
                cortes, nota = fronteras_por_escena(export, dur)
                nota = f"no se pudo leer la timeline ({e}); {nota}"
        else:
            cortes, nota = fronteras_por_escena(export, dur)

        fuera = 0
        if len(cortes) > args.max_cortes:
            idx = F.indices_repartidos(len(cortes), args.max_cortes)
            fuera = len(cortes) - len(idx)
            cortes = [cortes[i] for i in idx]

        L.append(f"\n## `{export.name}`\n")
        L.append(f"Duracion {dur/60:.2f} min · {len(cortes)} fronteras · "
                 f"fuente de las fronteras: {nota}\n")
        if fuera:
            L.append(f"**{fuera} fronteras quedaron fuera** del tope de "
                     f"`--max-cortes {args.max_cortes}`. Las miradas van "
                     f"repartidas de punta a punta, no son las primeras.\n")

        destino = base_vistas / export.stem
        puntos, pisados = fusionar(
            [(t, f"corte {i+1}") for i, t in enumerate(cortes)]
            + puntos_de_muestreo(dur))
        if pisados:
            L.append(f"{pisados} puntos caian dentro de la ventana de otro y "
                     f"habrian dado la misma vista: fusionados.\n")

        L.append("| t | que es | vista |")
        L.append("|---:|---|---|")
        for t, etiqueta in puntos:
            ini = max(0.0, t - VENTANA)
            fin = min(dur, t + VENTANA)
            if fin - ini < 0.2:
                continue
            env = vt.envolvente(str(export), ini, fin - ini)
            pico = float(env.max()) if env is not None and env.size else 1.0
            png = destino / f"{t:08.2f}.png".replace(" ", "0")
            try:
                vt.construir(
                    video=str(export), inicio=ini, fin=fin,
                    n_frames=max(1, args.frames), env_a=env,
                    etq_a="sonido del export", env_b=None, etq_b="",
                    pico=pico or 1.0, palabras=[], silencios=[],
                    titulo=f"{export.name} · {etiqueta} · {t:.2f} s",
                    destino=png)
            except Exception as e:
                L.append(f"| {t:.2f} | {etiqueta} | no se pudo dibujar: {e} |")
                continue
            total_vistas += 1
            L.append(f"| {t:.2f} | {etiqueta} | `{png}` |")

    out = Path(args.out) if args.out else (
        (root / ".cinema_assistant" / "reports" / f"export-{hoy:%Y%m%d}.md")
        if root else exports[0].parent / f"export-{hoy:%Y%m%d}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"{out}")
    print(f"  exports={len(exports)}  vistas={total_vistas}  "
          f"pasada={args.pasada}/{MAX_PASADAS}")
    print("  Las vistas NO se han mirado todavia: eso es el siguiente paso, y "
          "es del asistente.")


if __name__ == "__main__":
    main()
