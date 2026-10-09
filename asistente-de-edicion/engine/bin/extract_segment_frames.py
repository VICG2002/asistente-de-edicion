#!/usr/bin/env python3
"""Extrae los frames de cada tramo curado para el analisis visual posterior.

Por cada `clip_curated_segments`, deja frames en
`<root>/.cinema_assistant/seg_frames/<seg_id>/<pct>.jpg`, donde <pct> es el
porcentaje del tramo donde cayo el frame. Skip si ya existen, salvo --force.

TRES MOTORES, Y EL DEFAULT NO CAMBIA NADA
  porcentaje  (default) Lo de siempre: 10/30/50/70/90 %, adaptado a la
              duracion. Ningun proyecto existente cambia de comportamiento.
  escena      Frames donde la imagen CAMBIA (`lib/frames.py`), con presupuesto
              por duracion y dedup de los casi identicos. Si el tramo resulta
              estatico, cae a uniforme y lo declara.
  uniforme    Presupuesto por duracion repartido por igual, sin detectar nada.
              El barato.

POR QUE IMPORTA
Cinco frames por porcentaje en una entrevista a plano fijo son cinco veces la
misma imagen, y cada una se paga tres veces: disco, batch de YOLO y descripcion
del vision LLM (de tres a cinco horas sobre 1700 tramos). El motor `escena`
gasta el presupuesto donde hay algo distinto que ver.

LOS CUES: LOS TIEMPOS QUE EL HABLA SENALA
`--cues beats,silencios` fuerza un frame en momentos que la seleccion visual se
pierde justo porque senalar algo apenas cambia la imagen:
  beats      `interview_beats` — la pregunta y donde empieza a responder.
  silencios  `clip_silences` — el centro de cada pausa medida sobre el
             lavalier sincronizado.
Los cues se descuentan del tope ANTES de elegir, asi que el reparto no los
desaloja. Solo valen con motor escena/uniforme: el motor `porcentaje` reparte
por posicion fija y no tiene tope donde reservarlos.

LO QUE NO ESTA AQUI
Los cortes de la timeline TAMBIEN son cues, pero viven en tiempo de timeline y
no en tiempo de clip: mapearlos exige la tabla de items, y una conversion mal
hecha pondria frames en el sitio equivocado sin avisar. Donde ese tiempo es el
nativo es en `bin/verify_export.py`, que trabaja sobre el export. Ahi si se
usan.
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402
from lib import frames as F  # noqa: E402


DEFAULT_PCTS = (10, 30, 50, 70, 90)
TARGET_WIDTH = 720  # frame width — reducido a 720px (suficiente para vision
                    # LLM, ahorra ~40% de espacio vs 960px sin pérdida útil)


def adaptive_pcts(dur: float, base_pcts: tuple) -> list[int]:
    """Optimización: menos frames en tramos cortos y uniformes.
    Cumple directiva 'no extraer 5 frames cuando 1 alcanza'."""
    if dur < 10:
        return [50]                       # tramo corto: 1 frame al medio
    if dur < 30:
        return [25, 50, 75]               # mediano: 3 frames
    return list(base_pcts)                # largo: 5 frames (10/30/50/70/90)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def extract_frame(video_path: str, t_sec: float, out_path: Path) -> bool:
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{t_sec:.3f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={TARGET_WIDTH}:-2",
        "-q:v", "3",
        str(out_path)
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


FUENTES_CUE = ("beats", "silencios")


def cues_del_clip(conn, clip_id: int, fuentes: list[str]) -> list[float]:
    """Los tiempos que el habla senala, en tiempo de VIDEO del clip.

    Ambas tablas ya guardan tiempo de video: `derive_interview_beats.py` trabaja
    sobre el master del clip y `detect_pauses.py` convierte el lavalier con el
    offset del manifest antes de escribir. Aqui no se convierte nada — si
    hubiera que convertir, este seria el sitio equivocado para hacerlo.

    Una tabla que no existe no es un error: es un proyecto que todavia no paso
    por ese paso. Devuelve lo que haya.
    """
    out: list[float] = []
    if "beats" in fuentes:
        try:
            for (t,) in conn.execute(
                    "SELECT start_sec FROM interview_beats "
                    "WHERE clip_id=? AND kind IN ('pregunta','respuesta') "
                    "AND start_sec IS NOT NULL", (clip_id,)):
                out.append(float(t))
        except sqlite3.Error:
            pass
    if "silencios" in fuentes:
        try:
            for (a, b) in conn.execute(
                    "SELECT start_sec, end_sec FROM clip_silences "
                    "WHERE clip_id=? AND start_sec IS NOT NULL "
                    "AND end_sec IS NOT NULL", (clip_id,)):
                out.append((float(a) + float(b)) / 2.0)
        except sqlite3.Error:
            pass
    return sorted(set(round(t, 3) for t in out))


def nombrar_por_pct(elegidos: list[dict], inicio: float, dur: float,
                    out_seg: Path) -> tuple[int, int]:
    """Renombra los frames a `<pct>.jpg`, el porcentaje del tramo donde cayeron.

    POR QUE UN ENTERO Y NO EL TIEMPO
    `bin/analyze_segment_objects.py` hace `int(fp.stem)` para poblar
    `segment_objects.frame_pct`. Un nombre que no sea entero lo hace saltarse
    el frame EN SILENCIO. El porcentaje conserva el significado de esa columna
    y no obliga a migrar la tabla.

    El desempate es real: en un tramo largo dos cortes seguidos caen en el mismo
    porcentaje entero. Se avanza al siguiente hueco libre; si no queda ninguno,
    ese frame se descarta y se CUENTA — no se pisa un archivo en silencio.
    """
    usados: set[int] = set()
    escritos = 0
    perdidos = 0
    for f in elegidos:
        rel = (float(f["t_sec"]) - inicio) / dur if dur > 0 else 0.0
        pct = max(0, min(100, int(round(rel * 100))))
        while pct in usados and pct < 100:
            pct += 1
        while pct in usados and pct > 0:
            pct -= 1
        origen = Path(f["path"])
        if pct in usados:
            origen.unlink(missing_ok=True)
            perdidos += 1
            continue
        usados.add(pct)
        destino = out_seg / f"{pct:03d}.jpg"
        try:
            origen.replace(destino)
            escritos += 1
        except OSError:
            perdidos += 1
    return escritos, perdidos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--pcts", default=",".join(str(p) for p in DEFAULT_PCTS),
                    help="Porcentajes del tramo donde sacar frames")
    ap.add_argument("--force", action="store_true",
                    help="Re-extraer aunque ya existan los frames")
    ap.add_argument("--skip-claude", action="store_true",
                    help="Skip tramos curated_by='claude' (ya descritos a mano)")
    ap.add_argument("--max-segs", type=int, default=0,
                    help="0 = todos. Util para debug.")
    ap.add_argument("--motor", choices=("porcentaje", "escena", "uniforme"),
                    default="porcentaje",
                    help="porcentaje (default, lo de siempre) | escena "
                         "(donde la imagen cambia) | uniforme (presupuesto "
                         "por duracion, sin detectar)")
    ap.add_argument("--tope", type=int, default=0,
                    help="Frames maximos por tramo. 0 = presupuesto por "
                         "duracion (lib/frames.presupuesto). Solo con motor "
                         "escena/uniforme.")
    ap.add_argument("--cues", default="",
                    help="Fuentes de cues separadas por coma: "
                         f"{','.join(FUENTES_CUE)}. Solo con motor "
                         "escena/uniforme.")
    ap.add_argument("--sin-dedup", action="store_true",
                    help="No tirar los frames casi identicos. Solo con motor "
                         "escena/uniforme.")
    args = ap.parse_args()

    pcts = [int(p) for p in args.pcts.split(",") if p.strip()]
    fuentes = [f.strip() for f in args.cues.split(",") if f.strip()]
    desconocidas = [f for f in fuentes if f not in FUENTES_CUE]
    if desconocidas:
        sys.exit(f"Fuente de cues desconocida: {', '.join(desconocidas)}. "
                 f"Validas: {', '.join(FUENTES_CUE)}")
    if fuentes and args.motor == "porcentaje":
        sys.exit("--cues no aplica al motor 'porcentaje': reparte por posicion "
                 "fija y no tiene tope donde reservarlos. Usa --motor escena.")
    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    frames_dir = root / ".cinema_assistant" / "seg_frames"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    # Optimización: solo tramos que vale la pena describir. Skip:
    #  - curated_by='claude' (ya escritos a mano)
    #  - category 'discard' o 'export' (no son material editorial real)
    #  - clip status='cull' EXCEPTO si es entrevista (S-Log3 false-positive en
    #    analyze_clips marca como cull los clips Sony FX30 sin LUT → no debe
    #    castigar el extracto de frames cuando el clip es claramente entrevista.
    #    Caso fundador Zezzions 2026-05-26.)
    sql = """
        SELECT ccs.id, ccs.clip_id, ccs.start_sec, ccs.end_sec,
               ccs.curated_by, c.path
        FROM clip_curated_segments ccs
        JOIN clips c ON c.id = ccs.clip_id
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        LEFT JOIN clip_analysis ca ON ca.clip_id = c.id
        WHERE c.file_kind = 'video' AND c.index_status='ok'
          AND (d.category IS NULL OR d.category NOT IN ('discard', 'export'))
          AND (ca.analysis_status IS NULL OR ca.analysis_status != 'cull'
               OR d.category LIKE 'entrevista%')
    """
    rows = conn.execute(sql).fetchall()

    if args.max_segs > 0:
        rows = rows[: args.max_segs]

    print(f"Tramos por procesar: {len(rows)} (filtrados — skip discard/export/cull)")
    if args.motor == "porcentaje":
        print("Motor: porcentaje — frames adaptivos: 1 si <10s, 3 si <30s, 5 si más")
    else:
        print(f"Motor: {args.motor} — presupuesto por duración"
              f"{'' if args.tope <= 0 else f', tope {args.tope}'}"
              f"{'' if args.sin_dedup else ', con dedup'}"
              f"{'' if not fuentes else ', cues: ' + ','.join(fuentes)}")
    n_ok = 0
    n_skipped = 0
    n_failed = 0
    n_frames_total = 0
    n_dup = 0
    n_cues = 0
    n_cues_fuera = 0
    n_perdidos = 0
    caidas = 0
    for seg_id, cid, s, e, by, vpath in rows:
        if args.skip_claude and by == "claude":
            n_skipped += 1
            continue
        if not vpath or not os.path.exists(vpath):
            n_failed += 1
            continue
        out_seg = frames_dir / str(seg_id)
        dur = max(0.5, e - s)

        if args.motor == "porcentaje":
            seg_pcts = adaptive_pcts(dur, tuple(pcts))
            all_present = all((out_seg / f"{p:02d}.jpg").exists()
                              for p in seg_pcts)
            if all_present and not args.force:
                n_skipped += 1
                continue
            out_seg.mkdir(parents=True, exist_ok=True)
            for p in seg_pcts:
                t = s + dur * (p / 100.0)
                out_p = out_seg / f"{p:02d}.jpg"
                if out_p.exists() and not args.force:
                    continue
                if extract_frame(vpath, t, out_p):
                    n_frames_total += 1
                else:
                    n_failed += 1
            n_ok += 1
        else:
            # El tramo se re-hace entero o no se toca. Mezclar frames de dos
            # motores en el mismo directorio deja huerfanos que el glob del
            # consumidor encuentra igual y describe como si fueran de esta
            # corrida.
            if out_seg.exists() and any(out_seg.glob("*.jpg")) and not args.force:
                n_skipped += 1
                continue
            out_seg.mkdir(parents=True, exist_ok=True)
            for viejo_jpg in out_seg.glob("*.jpg"):
                viejo_jpg.unlink(missing_ok=True)
            cues = cues_del_clip(conn, cid, fuentes) if fuentes else []
            elegidos, acta = F.elegir_frames(
                vpath, out_seg, s, s + dur,
                motor=("escena" if args.motor == "escena" else "uniforme"),
                tope=(args.tope if args.tope > 0 else None),
                cues=cues, ancho=TARGET_WIDTH, dedup=not args.sin_dedup)
            escritos, perdidos = nombrar_por_pct(elegidos, s, dur, out_seg)
            n_frames_total += escritos
            n_perdidos += perdidos
            n_dup += acta["duplicados"]
            n_cues += acta["cues"]
            n_cues_fuera += acta["cues_fuera"]
            if acta["caida"]:
                caidas += 1
            if escritos == 0:
                n_failed += 1
            else:
                n_ok += 1

        if n_ok and n_ok % 100 == 0:
            print(f"  [{n_ok}/{len(rows)}] tramos | {n_frames_total} frames")

    conn.close()

    print(f"\nDone. tramos={n_ok}  frames={n_frames_total}  "
          f"skipped={n_skipped}  failed={n_failed}")
    if args.motor != "porcentaje":
        # El acta de la corrida. Sin estos numeros no se puede juzgar si el
        # motor eligio bien: un dedup que no tira nada esta mal calibrado, y
        # una caida masiva a uniforme dice que el material es estatico y que
        # detectar escenas no aporta en este proyecto.
        print(f"       duplicados_tirados={n_dup}  cues={n_cues}  "
              f"cues_fuera_del_tramo={n_cues_fuera}")
        print(f"       tramos_estaticos_caidos_a_uniforme={caidas}"
              f"{'' if not n_perdidos else f'  frames_sin_hueco_de_pct={n_perdidos}'}")
        if fuentes and n_cues == 0:
            print("       AVISO: se pidieron cues y no salio ninguno. "
                  "¿Corriste derive_interview_beats.py / detect_pauses.py?")


if __name__ == "__main__":
    main()
