#!/usr/bin/env python3
"""Ata cada clip a la capsula (reel) del brief a la que pertenece.

POR QUE EXISTE
Un rodaje de campaña graba cinco o seis piezas el mismo dia. Al volver, el
material es una sola cronologia plana: 178 clips en tres camaras que no dicen a
cual de las seis capsulas pertenecen. Sin esto, la primera tarde de edicion se
va en abrir clips para averiguar donde acaba una capsula y empieza la otra.

COMO DECIDE
La unidad no es el clip, es el GRUPO DE TOMAS de `derive_takes.py`: todas las
tomas que dicen el mismo texto pertenecen a la misma capsula por construccion,
asi que se decide una vez por grupo y se propaga. El grupo se puntua contra el
vocabulario del tema de cada capsula (ver lib/reels.py).

El material sin dialogo —dron, planos de detalle, la camara B— no tiene texto
contra el que puntuar, pero si tiene hora: hereda la capsula que se estaba
rodando en ese momento. Eso exige que los relojes esten corregidos; si una
camara no tiene skew declarado, se avisa POR CAMARA y en grande al cerrar,
porque un reloj corrido reparte su material en la capsula equivocada sin que
nada lo diga (leccion de Morsa, 2026-08-03).

QUE NO HACE
No lee el guion literal del brief: en IMODAE los textos del set se reescribieron
y no comparten una frase con el brief. Y no cierra la decision — escribe un
informe con la evidencia de cada asignacion y el segundo candidato, para que el
editor corrija con `reel_overrides` en project_config.json:

    "reel_overrides": {"grupo:96": 4, "clip:1234": 2, "grupo:101": null}

Uso:
    python3 bin/derive_reels.py --root <disco>
    python3 bin/derive_reels.py --root <disco> --dry-run
    python3 bin/derive_reels.py --root <disco> --min-puntos 5 --sin-ventanas
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import manifest, proyecto, reels                      # noqa: E402
from lib.guards import assert_selected, exigir_tablas, report_done  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_reels (
    clip_id INTEGER PRIMARY KEY,
    reel_n INTEGER,
    reel_titulo TEXT,
    group_id INTEGER,
    metodo TEXT,             -- texto | ventana | override
    score REAL,              -- puntos de vocabulario (0 si vino por hora)
    evidencia TEXT,          -- en español, legible, auditable
    decided_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_clip_reels_n ON clip_reels(reel_n);
-- Que capsula se habla en cada TRAMO del clip. `clip_reels` dice a cual
-- pertenece el clip; esto dice que mas hay dentro. Sin esta tabla, un clip que
-- cubre dos capsulas hacia desaparecer la segunda del reparto sin avisar.
CREATE TABLE IF NOT EXISTS clip_reel_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    idx INTEGER,
    start_sec REAL,
    end_sec REAL,
    reel_n INTEGER,
    reel_titulo TEXT,
    score REAL,
    evidencia TEXT,
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_reel_seg_clip ON clip_reel_segments(clip_id);
CREATE INDEX IF NOT EXISTS idx_reel_seg_n ON clip_reel_segments(reel_n);
"""


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def hhmm(t: float | None, huso: float) -> str:
    return "--:--" if t is None else time.strftime(
        "%H:%M", time.gmtime(t + huso * 3600))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-puntos", type=int, default=reels.MIN_PUNTOS_DEFECTO,
                    help=f"puntos minimos para dar una capsula por buena "
                         f"(default {reels.MIN_PUNTOS_DEFECTO})")
    ap.add_argument("--sin-ventanas", action="store_true",
                    help="No repartir el material sin dialogo por la hora. "
                         "Usalo si los relojes no son de fiar.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ca = root / ".cinema_assistant"
    db = ca / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}. Corre antes index_project.py.")

    cfg = proyecto.leer_config(root)
    capsulas = cfg.get("capsulas") or cfg.get("reels") or []
    if not capsulas:
        sys.exit(
            "\nproject_config.json no declara `capsulas` (ni `reels`).\n"
            "Cada entrada necesita al menos:\n"
            '    {"n": 1, "titulo": "...", "tema": "...", '
            '"palabras_clave": ["...", "..."]}\n'
            "Sin el vocabulario del tema no hay contra que puntuar el material.")
    huso = float(cfg.get("huso_horario_horas") or 0)
    skews = {str(k): float(v) for k, v in
             (cfg.get("camera_skews_manual") or {}).items()}

    conn = manifest.conectar(str(db))
    exigir_tablas(conn, {
        "take_groups": "python3 bin/derive_takes.py --root <disco>",
        "clip_takes": "python3 bin/derive_takes.py --root <disco>",
    }, "derive_reels")
    conn.executescript(SCHEMA)

    grupos = conn.execute(
        "SELECT id, texto_canonico, n_tomas FROM take_groups ORDER BY id").fetchall()
    assert_selected(grupos, "grupos de tomas", filters={"--root": str(root)},
                    hint="Corre antes derive_takes.py: sin grupos no hay texto "
                         "contra el que puntuar las capsulas.")

    clips_de_grupo: dict[int, list[int]] = defaultdict(list)
    grupo_de_clip: dict[int, int] = {}
    for cid, gid in conn.execute("SELECT clip_id, group_id FROM clip_takes"):
        clips_de_grupo[gid].append(cid)
        grupo_de_clip[cid] = gid

    videos = conn.execute(
        "SELECT id, filename, parent_folder, creation_time, duration_sec "
        "FROM clips WHERE file_kind='video' AND index_status='ok' "
        "ORDER BY COALESCE(creation_time,''), filename").fetchall()
    assert_selected(videos, "clips de video", filters={"--root": str(root)})

    # --- tiempo real corregido, avisando de las camaras sin skew -------------
    sin_skew, t_real = set(), {}
    for cid, _fn, pf, ct, _d in videos:
        pf = pf or "?"
        if pf not in skews:
            sin_skew.add(pf)
        t_real[cid] = reels.tiempo_real(ct, skews.get(pf, 0.0))

    # --- 1. decidir por grupo -----------------------------------------------
    ov_grupo, ov_clip = reels.leer_overrides(cfg)
    veredictos = {}
    for gid, texto, _n in grupos:
        v = reels.elegir_capsula(texto or "", capsulas, args.min_puntos)
        if gid in ov_grupo:
            n = ov_grupo[gid]
            v = dict(v, reel_n=n, ambiguo=False, metodo="override",
                     titulo=next((c.get("titulo", "") for c in capsulas
                                  if c.get("n") == n), ""),
                     motivo="fijado a mano en project_config.json (reel_overrides)")
        veredictos[gid] = v

    # --- 2. propagar a los clips del grupo ----------------------------------
    asignacion: dict[int, dict] = {}
    for gid, v in veredictos.items():
        if v.get("reel_n") is None:
            continue
        for cid in clips_de_grupo.get(gid, []):
            asignacion[cid] = {
                "reel_n": v["reel_n"], "titulo": v.get("titulo", ""),
                "group_id": gid, "metodo": v.get("metodo", "texto"),
                "score": float(v.get("puntos", 0)),
                "evidencia": v.get("motivo", "")
                + (" · ⚠ ambiguo: el segundo candidato queda muy cerca"
                   if v.get("ambiguo") else ""),
            }

    # --- 3. repartir el material sin dialogo por la hora --------------------
    vent = reels.ventanas([(a["reel_n"], t_real.get(cid))
                           for cid, a in asignacion.items()])
    n_ventana = 0
    if not args.sin_ventanas:
        for cid, _fn, _pf, _ct, _d in videos:
            if cid in asignacion:
                continue
            n, motivo = reels.reel_de_ventana(t_real.get(cid), vent)
            if n is None:
                continue
            asignacion[cid] = {
                "reel_n": n,
                "titulo": next((c.get("titulo", "") for c in capsulas
                                if c.get("n") == n), ""),
                "group_id": None, "metodo": "ventana", "score": 0.0,
                "evidencia": motivo,
            }
            n_ventana += 1

    # --- 4. overrides por clip: mandan sobre todo lo anterior ---------------
    n_ov = 0
    for cid, n in ov_clip.items():
        if n is None:
            asignacion.pop(cid, None)
        else:
            asignacion[cid] = {
                "reel_n": n,
                "titulo": next((c.get("titulo", "") for c in capsulas
                                if c.get("n") == n), ""),
                "group_id": grupo_de_clip.get(cid), "metodo": "override",
                "score": 0.0,
                "evidencia": "fijado a mano en project_config.json (reel_overrides)",
            }
        n_ov += 1

    # --- 5. que MAS se habla dentro de cada clip ---------------------------
    # El reparto de arriba da UNA capsula por clip. Esto mira dentro: un clip
    # de 69 s puede arrancar con una capsula y seguir con otra, y sin esto la
    # segunda desaparecia del reparto sin que nada lo dijera.
    tr_dir = ca / "transcripts"
    segmentos: dict[int, list] = {}
    if tr_dir.is_dir():
        for cid, _fn, _pf, _ct, _d in videos:
            p = tr_dir / f"{cid}.json"
            if not p.exists():
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            segs = reels.segmentar_por_capsula(
                d.get("words") or [], capsulas,
                dominante=(asignacion.get(cid) or {}).get("reel_n"))
            if segs:
                segmentos[cid] = segs

    # Capsulas que SOLO aparecen dentro de un clip asignado a otra: son las que
    # se daban por no rodadas.
    por_reel = Counter(a["reel_n"] for a in asignacion.values())
    en_segmentos = {s["reel_n"] for segs in segmentos.values() for s in segs}
    secundarias = []
    for cid, segs in segmentos.items():
        principal = (asignacion.get(cid) or {}).get("reel_n")
        for s in segs:
            if s["reel_n"] != principal:
                secundarias.append((cid, s, principal))

    sin_material = reels.capsulas_sin_material(capsulas, set(por_reel), en_segmentos)
    sin_reel = [v for v in videos if v[0] not in asignacion]

    if args.dry_run:
        print(f"[dry-run] {len(grupos)} grupos -> "
              f"{sum(1 for v in veredictos.values() if v.get('reel_n'))} con capsula; "
              f"{len(asignacion)} de {len(videos)} clips asignados "
              f"({n_ventana} por hora).")
        for n in sorted(por_reel):
            print(f"    reel {n}: {por_reel[n]} clips")
        return 0

    conn.execute("DELETE FROM clip_reels")          # lint:ok delete-global
    conn.execute("DELETE FROM clip_reel_segments")  # lint:ok delete-global
    ahora = time.time()
    for cid, a in asignacion.items():
        conn.execute(
            "INSERT INTO clip_reels (clip_id, reel_n, reel_titulo, group_id,"
            " metodo, score, evidencia, decided_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, a["reel_n"], a["titulo"], a["group_id"], a["metodo"],
             a["score"], a["evidencia"], ahora))
    for cid, segs in segmentos.items():
        for i, s in enumerate(segs, 1):
            conn.execute(
                "INSERT INTO clip_reel_segments (clip_id, idx, start_sec,"
                " end_sec, reel_n, reel_titulo, score, evidencia, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, i, s["s"], s["e"], s["reel_n"], s["titulo"],
                 float(s["puntos"]), ", ".join(s["evidencia"][:6]), ahora))
    conn.commit()

    # --- informe -----------------------------------------------------------
    nombre = {c.get("n"): c.get("titulo", "") for c in capsulas}
    fn_de = {v[0]: v[1] for v in videos}
    rep_dir = ca / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep = rep_dir / f"reels-{time.strftime('%Y%m%d')}.md"
    with rep.open("w", encoding="utf-8") as f:
        f.write("# Reparto del material entre las cápsulas\n\n")
        f.write(f"{len(videos)} clips de video · {len(capsulas)} cápsulas "
                f"declaradas · **{len(asignacion)} asignados**, "
                f"{len(sin_reel)} sin cápsula.\n\n")
        f.write("El motor propone con la evidencia delante; **la asignación la "
                "firmas tú**. Para corregir, en `project_config.json`:\n\n")
        f.write('```json\n"reel_overrides": {"grupo:96": 4, "clip:1234": 2, '
                '"grupo:101": null}\n```\n\n')
        f.write("`null` fuerza *sin cápsula* — sirve para sacar del reparto la "
                "charla de rodaje que el vocabulario emparejó por casualidad.\n\n")

        f.write("## Grupos de tomas\n\n")
        f.write("| Grupo | Tomas | Cápsula | Puntos | Evidencia | 2º candidato |\n")
        f.write("|---|---|---|---|---|---|\n")
        for gid, texto, n_tomas in grupos:
            v = veredictos[gid]
            r = v.get("ranking") or []
            seg = r[1] if len(r) > 1 else None
            cap = (f"**{v['reel_n']} — {nombre.get(v['reel_n'], '')}**"
                   if v.get("reel_n") else "_sin cápsula_")
            if v.get("ambiguo"):
                cap += " ⚠"
            ev = ", ".join(v.get("evidencia", [])[:5]) or "—"
            s2 = f"{seg['n']} ({seg['puntos']})" if seg and seg["puntos"] else "—"
            f.write(f"| {gid} | {n_tomas} | {cap} | {v.get('puntos', 0)} | "
                    f"{ev} | {s2} |\n")
            f.write(f"| | | | | _{(texto or '')[:110]}…_ | |\n")

        f.write("\n## Ventanas de rodaje\n\n")
        if vent:
            f.write("Hora corregida por el skew de cada cámara"
                    + (f" (huso {huso:+.0f} h)" if huso else " (UTC)") + ".\n\n")
            f.write("| Cápsula | De | A | Clips | Por texto | Por hora |\n")
            f.write("|---|---|---|---|---|---|\n")
            for n in sorted(vent):
                t0, t1 = vent[n]
                por_txt = sum(1 for a in asignacion.values()
                              if a["reel_n"] == n and a["metodo"] != "ventana")
                f.write(f"| {n} — {nombre.get(n, '')} | {hhmm(t0, huso)} | "
                        f"{hhmm(t1, huso)} | {por_reel[n]} | {por_txt} | "
                        f"{por_reel[n] - por_txt} |\n")
        else:
            f.write("_Ninguna: ningún grupo alcanzó una cápsula._\n")

        f.write("\n## Otras cápsulas DENTRO de un clip\n\n")
        if secundarias:
            f.write("Estos clips están asignados a una cápsula pero **hablan "
                    "también de otra**. Sin esta tabla, esa segunda cápsula "
                    "desaparecía del reparto y se daba por no rodada.\n\n")
            f.write("Los tiempos salen de los word-timings de Whisper, que "
                    "**derivan**: sirven para saber por dónde entra el otro tema, "
                    "no para cortar.\n\n")
            f.write("| Clip | Asignado a | También habla de | Dentro del clip | Evidencia |\n")
            f.write("|---|---|---|---|---|\n")
            for cid, s, principal in sorted(secundarias, key=lambda x: -(x[1]["e"] - x[1]["s"])):
                f.write(f"| `{fn_de.get(cid, cid)}` | "
                        f"{principal if principal else '—'} | "
                        f"**{s['reel_n']} — {nombre.get(s['reel_n'], '')}** | "
                        f"{s['s']:.0f}–{s['e']:.0f}s | "
                        f"{', '.join(s['evidencia'][:4])} |\n")
        else:
            f.write("_Ninguno: cada clip habla de una sola cápsula._\n")

        f.write("\n## Cápsulas declaradas sin material\n\n")
        if sin_material:
            f.write("**Antes de decir que una cápsula no se rodó, mirar esta "
                    "tabla.** No es lo mismo que no haya material a que el "
                    "material esté dentro de un clip asignado a otra cápsula.\n\n")
            f.write("| Cápsula | Estado |\n|---|---|\n")
            for c in sin_material:
                estado = ("**SÍ hay material**, dentro de un clip asignado a "
                          "otra cápsula — ver la tabla de arriba"
                          if c["solo_en_segmentos"]
                          else "sin rastro en ningún transcript")
                f.write(f"| {c['n']} — {c['titulo']} | {estado} |\n")
        else:
            f.write("_Ninguna: las {} cápsulas declaradas tienen material._\n"
                    .format(len(capsulas)))

        f.write(f"\n## Sin cápsula ({len(sin_reel)} clips)\n\n")
        if sin_reel:
            f.write("Se quedan fuera del reparto. En la timeline de A-ROLL "
                    "salen bajo un marcador `SIN CÁPSULA`.\n\n")
            for cid, fn, pf, _ct, dur in sin_reel[:60]:
                gid = grupo_de_clip.get(cid)
                por = (f"grupo {gid} sin cápsula" if gid
                       else "sin diálogo y fuera de toda ventana")
                f.write(f"- `{fn}` ({pf}, {dur or 0:.0f}s) — {por}\n")
            if len(sin_reel) > 60:
                f.write(f"- … y {len(sin_reel) - 60} más\n")
        else:
            f.write("_Ninguno._\n")

        if sin_skew:
            f.write("\n## ⚠ Cámaras sin skew declarado\n\n")
            f.write("Se ordenan con su reloj CRUDO. Si ese reloj está corrido, "
                    "su material se reparte en la cápsula equivocada y nada lo "
                    "dice. Se declara en `project_config.json`:\n\n")
            f.write('```json\n"camera_skews_manual": {'
                    + ", ".join(f'"{c}": 0.0' for c in sorted(sin_skew))
                    + '}\n```\n\n')
            f.write("Negativo = la cámara va atrasada respecto al tiempo real.\n")

    print(f"\nReparto por cápsula — {root.name}")
    print("=" * 66)
    for n in sorted(por_reel):
        por_txt = sum(1 for a in asignacion.values()
                      if a["reel_n"] == n and a["metodo"] != "ventana")
        print(f"  reel {n} — {nombre.get(n, '')[:42]:<42} {por_reel[n]:>4} clips"
              f"  ({por_txt} por texto, {por_reel[n]-por_txt} por hora)")
    print(f"  {'sin cápsula':<49} {len(sin_reel):>4} clips")

    if secundarias:
        print(f"\n  {len(secundarias)} tramo(s) de OTRA cápsula dentro de un clip "
              f"ya asignado:")
        for cid, s, principal in sorted(secundarias,
                                        key=lambda x: -(x[1]["e"] - x[1]["s"]))[:6]:
            print(f"    · {fn_de.get(cid, cid)} ({principal}) habla también de "
                  f"la {s['reel_n']} en {s['s']:.0f}–{s['e']:.0f}s")
    if sin_material:
        print("")
        for c in sin_material:
            if c["solo_en_segmentos"]:
                print(f"  ⚠ cápsula {c['n']} sin clip propio, pero SI hay material "
                      f"dentro de otro clip. NO decir que no se rodó.")
            else:
                print(f"  · cápsula {c['n']} — sin rastro en ningún transcript.")

    if sin_skew:
        print(f"\n  ⚠ Sin skew de reloj declarado: {', '.join(sorted(sin_skew))}")
        print("    Se ordenan con su reloj crudo — si está corrido, su material")
        print("    cae en la cápsula equivocada. Ver el informe.")
    print(f"\n  informe: {rep}")
    report_done("derive_reels", grupos=len(grupos),
                grupos_con_capsula=sum(1 for v in veredictos.values()
                                       if v.get("reel_n")),
                clips=len(asignacion), por_hora=n_ventana,
                overrides=n_ov, sin_capsula=len(sin_reel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
