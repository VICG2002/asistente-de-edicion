#!/usr/bin/env python3
"""Hornea el plan de merge que ejecuta `resolve/merge_pool.lua`.

La decision arquitectonica
--------------------------
NO seleccionar 200 clips y dejar que Resolve empareje. Resolve empareja por
waveform sobre todo el set y se equivoca — es exactamente el fallo que el motor
ya resuelve (falsos positivos de B-roll "con energia RMS parecida",
`patrones-exitosos.md:531`).

En vez de eso: **una llamada de AutoSyncAudio por grupo verificado**, donde el
grupo es `{video, wav1[, wav2]}` que el motor ya emparejo con sus cuatro senales
(waveform, transcript, chromaprint, eventos). El motor aporta el EMPAREJADO —
lo dificil. Resolve aporta la alineacion sub-frame y el link nativo — lo unico
que solo el puede hacer.

Verificado en Resolve 21.0.2 Free (2026-07-30): sobre un par sintetico con
offset conocido de 5 s, Resolve clavo el sync con **0 ms de error**.

Convencion de signos
--------------------
El manifest guarda `offset = audio_start - video_start`.
Resolve reporta en `GetAudioMapping().linked_audio[n].offset` el numero de
MUESTRAS del archivo ligado que corresponden al frame 0 del video, con el signo
INVERTIDO respecto al motor:

    offset_resolve_seg  ==  -offset_manifest_seg

`verify_merge.py` usa esa igualdad para comprobar que el merge quedo donde el
motor dijo.

Uso:
    python3 bin/export_merge_plan.py --root <disco> --out <ruta.lua>
    python3 bin/export_merge_plan.py --root <disco> --min-conf 0.50
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.guards import assert_selected, report_done  # noqa: E402
from lib import manifest  # noqa: E402

MIN_CONF_DEFAULT = 0.50   # mas exigente que el bake de timelines (0.30):
                          # el merge NO se deshace por API.


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def lua_str(s) -> str:
    if s is None:
        return '""'
    s = (str(s).replace("\\", "\\\\").replace('"', '\\"')
         .replace("\n", "\\n").replace("\r", ""))
    return f'"{s}"'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="",
                    help="Default: <disco>/.cinema_assistant/resolve/<proy>_merge.lua")
    ap.add_argument("--min-conf", type=float, default=MIN_CONF_DEFAULT,
                    help=f"Confianza minima del par. Default {MIN_CONF_DEFAULT} "
                         f"— el merge no se deshace, mas vale quedarse corto.")
    ap.add_argument("--project-prefix", default="",
                    help="Prefijo de rel_path. Vacio = todo el manifest.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    pfx = args.project_prefix
    try:
        filas = conn.execute("""
            SELECT cv.path, cv.filename, ca.path, ca.filename,
                   p.offset_sec, p.confidence,
                   cv.duration_sec, ca.duration_sec, ca.audio_sample_rate
            FROM audio_sync_pairs p
            JOIN clips cv ON cv.id = p.video_clip_id
            JOIN clips ca ON ca.id = p.audio_clip_id
            WHERE (? = '' OR cv.rel_path LIKE ? || '%')
            ORDER BY cv.path, p.confidence DESC
        """, (pfx, pfx)).fetchall()
    except sqlite3.OperationalError:
        sys.exit("No hay tabla audio_sync_pairs. Correr antes el sync.")

    # Un grupo por video, con sus audios ordenados por confianza.
    grupos: dict = {}
    descartados_conf = 0
    for (vpath, vname, apath, aname, off, conf,
         vdur, adur, asr) in filas:
        if (conf or 0) < args.min_conf:
            descartados_conf += 1
            continue
        g = grupos.setdefault(vpath, {"name": vname, "dur": vdur or 0.0,
                                      "audios": [], "vistos": set()})
        if apath in g["vistos"]:
            continue
        g["vistos"].add(apath)
        g["audios"].append({
            "path": apath, "name": aname, "offset": off or 0.0,
            "conf": conf or 0.0, "dur": adur or 0.0, "sr": asr or 48000,
        })
    conn.close()

    grupos = {k: v for k, v in grupos.items() if v["audios"]}
    assert_selected(list(grupos), "grupos de merge (video + su audio verificado)",
                    filters={"--root": str(root), "--min-conf": args.min_conf},
                    hint="Bajar --min-conf o revisar que el sync haya corrido.")

    out = Path(args.out) if args.out else (
        root / ".cinema_assistant" / "resolve" / f"{root.name.lower()}_merge.lua")
    out.parent.mkdir(parents=True, exist_ok=True)

    L = ["-- Plan de merge — generado por bin/export_merge_plan.py",
         "-- Lo ejecuta resolve/merge_pool.lua dentro de la Consola de Resolve.",
         "--",
         "-- Cada grupo es {video, audio1[, audio2]} que el motor YA emparejo.",
         "-- Resolve solo alinea y liga: el emparejado (lo dificil) ya esta hecho.",
         "--",
         "-- offset = audio_start - video_start (convencion del motor).",
         "-- Resolve reporta el signo INVERTIDO en GetAudioMapping.",
         "return {",
         f"  min_conf = {args.min_conf:.2f},",
         "  grupos = {"]
    n_audios = 0
    for vpath, g in sorted(grupos.items()):
        audios = ", ".join(
            "{path=" + lua_str(a["path"])
            + ", name=" + lua_str(a["name"])
            + f", offset={a['offset']:.3f}, conf={a['conf']:.3f}"
            + f", sr={int(a['sr'])}, dur={a['dur']:.2f}}}"
            for a in g["audios"])
        n_audios += len(g["audios"])
        L.append("    {video=" + lua_str(vpath)
                 + ", name=" + lua_str(g["name"])
                 + f", dur={g['dur']:.2f}"
                 + ", audios={" + audios + "}},")
    L.append("  },")
    L.append("}")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")

    dobles = sum(1 for g in grupos.values() if len(g["audios"]) >= 2)
    print(f"\nPlan de merge escrito: {out}")
    print(f"  grupos (videos)      : {len(grupos)}")
    print(f"  audios a ligar       : {n_audios}")
    print(f"  con dos lavaliers    : {dobles}")
    print(f"  descartados por conf : {descartados_conf} (< {args.min_conf})")
    print(f"\nEn la Consola de Resolve, sobre un proyecto DUPLICADO:")
    print(f'  MERGE_PLAN = "{out}"')
    print(f'  dofile("{HERE.parent}/resolve/merge_pool.lua")')
    report_done("export_merge_plan", grupos=len(grupos), audios=n_audios,
                dobles=dobles, descartados=descartados_conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
