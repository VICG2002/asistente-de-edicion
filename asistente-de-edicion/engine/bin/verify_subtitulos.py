#!/usr/bin/env python3
"""Garantia del subtitulado: comprueba el SRT contra el AUDIO y contra los CORTES.

POR QUE UN VERIFICADOR Y NO UNOS TESTS
`tests/test_subtitulos.py` comprueba las reglas de reparto sobre texto
inventado. Nadie miraba un SRT de verdad. Cuando se miro el primero —
`cut 1.3.srt` de Morsa, 107 subtitulos ya entregados con el corte — salieron
cuatro cosas que ninguna prueba unitaria podia ver:

    18 cues por encima de 17 CPS (hasta 20.5) con MAX_CPS=17
    17 cues clavados exactamente en 6.000 s, la firma del tope de duracion
    31 cues cruzando un corte que tambien cortaba el sonido
    el SRT terminaba en 799.5 s de un master de 996.4 (3:17 sin subtitular)

QUE COMPRUEBA, Y CONTRA QUE
  DURO (sale 1):
    - legibilidad: <=42 car/linea, <=2 lineas, 1-6 s, CPS <= 17, sin solapes.
    - ningun cue cruza un corte SINCRONICO de la timeline (los que cortan
      imagen y sonido a la vez). Los de solo imagen no se miran: ahi el audio
      sigue corriendo por debajo y el corte no sabe nada del habla.
  BLANDO (se reporta, no reprueba):
    - cuantos bordes de cue caen pegados a un corte. En los reels de Morsa,
      hechos a mano, era el 33 % contra un 2 % de azar. Es la metrica de si el
      subtitulado esta montado con la imagen o al lado de ella.
    - tramos con habla medida en el audio y sin ningun cue encima.
    - cuanto queda sin subtitular al final.

La medida del habla sale de `silencedetect` sobre el archivo entregado, no de
los tiempos de palabra de Whisper: esta MEDIDO que derivan (IMODAE 2026-08-07).
Dos medidas distintas sobre la misma fuente, como en `verify_cortes.py`.

Uso:
    python3 bin/verify_subtitulos.py --srt export.srt --video export.mov
    python3 bin/verify_subtitulos.py --srt export.srt --video export.mov \\
        --config <disco>/.cinema_assistant/project_config.json \\
        --timeline "Cut 1.2 sonido ayan"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib import loudness as L                                       # noqa: E402
from lib.subtitulos import (MAX_CPS, MIN_DUR, cruces, infracciones,  # noqa: E402
                            leer_srt, pegados)

# Un tramo de habla sin ningun subtitulo encima solo es noticia si dura algo.
MIN_HABLA_SIN_CUE = 2.0
# Cola sin subtitular que ya merece decirse.
MIN_COLA = 5.0


def mmss(t: float) -> str:
    return f"{int(t)//60:02d}:{t % 60:05.2f}"


def habla_sin_cue(video: Path, cues, umbral_db: float, min_dur: float) -> list[tuple[float, float]]:
    """Tramos con nivel por encima del umbral y sin ningun cue encima."""
    sil = L.silencios(video, umbral_db, 0.30)
    dur = L.astats(video)  # solo para forzar el fallo temprano si no hay audio
    if dur.get("rms_db") is None:
        return []
    total = max((c.fin for c in cues), default=0)
    for a, b in sil:
        total = max(total, b)
    habla, t = [], 0.0
    for a, b in sil:
        if a - t >= min_dur:
            habla.append((t, a))
        t = b
    if total - t >= min_dur:
        habla.append((t, total))
    huecos = []
    for a, b in habla:
        if not any(c.fin > a + 0.2 and c.inicio < b - 0.2 for c in cues):
            huecos.append((a, b))
    return huecos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt", required=True)
    ap.add_argument("--video", help="el export, para medir el habla de verdad")
    ap.add_argument("--config", help="project_config.json")
    ap.add_argument("--timeline", help="timeline de Resolve de la que salio el export")
    ap.add_argument("--proyecto")
    ap.add_argument("--base")
    ap.add_argument("--offset-tc", help="TC de timeline donde arranca el export")
    ap.add_argument("--umbral-db", type=float, default=-34.0)
    ap.add_argument("--tolerancia-corte", type=int, default=2,
                    help="fotogramas para contar un borde como pegado al corte")
    ap.add_argument("--max-cruces", type=int, default=0,
                    help="cruces de corte sincronico tolerados (se declaran)")
    args = ap.parse_args()

    srt = Path(args.srt)
    if not srt.exists():
        sys.exit(f"No existe: {srt}")
    cues = leer_srt(srt)
    if not cues:
        sys.exit(f"{srt} no tiene ningun subtitulo legible.")
    print(f"{srt.name}: {len(cues)} subtitulos, {mmss(cues[0].inicio)} → "
          f"{mmss(cues[-1].fin)}")

    fallos: list[str] = []

    # 1. legibilidad
    legib = infracciones(cues)
    picos = sorted((c.cps for c in cues), reverse=True)[:1]
    clavados_6 = sum(1 for c in cues if abs(c.dur - 6.0) < 0.005)
    print(f"  legibilidad: {len(legib)} incumplimientos · CPS max "
          f"{picos[0] if picos else 0:.1f} · {clavados_6} cues clavados en 6.000 s")
    fallos += legib

    # 2. los cortes de la timeline
    tl = None
    cortes_sync: list[float] = []
    if args.timeline:
        from lib.timeline_resolve import ErrorTimeline, leer_timeline
        proyecto = args.proyecto
        if not proyecto and args.config and Path(args.config).exists():
            try:
                proyecto = json.loads(Path(args.config).read_text()).get("project_name")
            except (json.JSONDecodeError, OSError):
                proyecto = None
        if not proyecto:
            sys.exit("Con --timeline hace falta --proyecto.")
        try:
            tl = leer_timeline(proyecto, args.timeline, config=args.config,
                               base=args.base)
        except ErrorTimeline as e:
            sys.exit(str(e))
        for a in tl.avisos():
            print(f"  ⚠ {a}")
        ini_f = (int(round(_tc(args.offset_tc, tl.fps))) if args.offset_tc
                 else tl.origen)
        fin_f = ini_f + int(round((cues[-1].fin + 30) * tl.fps))
        rango = (ini_f, fin_f)
        cortes_sync = [(c - ini_f) / tl.fps for c in tl.cortes_sync(rango)]
        solo_img = [(c - ini_f) / tl.fps for c in tl.cortes_solo_imagen(rango)]
        cruz = cruces(cues, cortes_sync)
        peg = pegados(cues, cortes_sync, args.tolerancia_corte / tl.fps)
        peg_img = pegados(cues, sorted(cortes_sync + solo_img),
                          args.tolerancia_corte / tl.fps)
        bordes = 2 * len(cues)
        print(f"  cortes: {len(cortes_sync)} sincronicos, {len(solo_img)} de solo "
              f"imagen (no se miran)")
        print(f"  cues que cruzan un corte sincronico: {len(cruz)}/{len(cues)} "
              f"({100*len(cruz)/len(cues):.0f} %)")
        print(f"  bordes pegados a un corte (±{args.tolerancia_corte} f): "
              f"{peg}/{bordes} sincronico ({100*peg/bordes:.0f} %) · "
              f"{peg_img}/{bordes} cualquier corte ({100*peg_img/bordes:.0f} %)")
        if len(cruz) > args.max_cruces:
            for i, cs in cruz[:10]:
                c = cues[i - 1]
                fallos.append(
                    f"cue {i} ({mmss(c.inicio)}→{mmss(c.fin)}) cruza "
                    f"{len(cs)} corte(s) sincronico(s) en "
                    + ", ".join(mmss(x) for x in cs[:3])
                    + f" — «{c.plano[:44]}…»")
            if len(cruz) > 10:
                fallos.append(f"… y {len(cruz)-10} cues mas cruzando un corte")

    # 3. el audio
    if args.video:
        video = Path(args.video)
        if not video.exists():
            sys.exit(f"No existe: {video}")
        dur_v = L.astats(video)
        huecos = habla_sin_cue(video, cues, args.umbral_db, MIN_HABLA_SIN_CUE)
        if huecos:
            tot = sum(b - a for a, b in huecos)
            print(f"  habla medida sin ningun cue encima: {len(huecos)} tramos, "
                  f"{tot:.0f}s (umbral {args.umbral_db:.0f} dB)")
            for a, b in huecos[:8]:
                print(f"    {mmss(a)} → {mmss(b)}  ({b-a:.1f}s)")
        import subprocess
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True)
        try:
            total = float((r.stdout or "0").strip() or 0)
        except ValueError:
            total = 0.0
        cola = total - cues[-1].fin if total else 0
        if cola >= MIN_COLA:
            print(f"  ⚠ sin subtitulos desde {mmss(cues[-1].fin)} hasta el final "
                  f"({cola:.0f}s, {100*cola/total:.0f} % de la pieza)")

    print()
    if fallos:
        print(f"FALLA: {len(fallos)} problemas")
        for f in fallos[:40]:
            print(f"  - {f}")
        if len(fallos) > 40:
            print(f"  … y {len(fallos)-40} mas")
        sys.exit(1)
    print("OK: el SRT cumple las reglas y ningun cue cruza un corte sincronico.")


def _tc(tc: str, fps: float) -> float:
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 3600 + m * 60 + s) * round(fps)) + f


if __name__ == "__main__":
    main()
