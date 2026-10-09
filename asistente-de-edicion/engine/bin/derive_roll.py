#!/usr/bin/env python3
"""Clasifica el material en A-roll y B-roll, y escribe un informe auditable.

Reglas (del usuario)
--------------------
- **Documental**: las entrevistas son A-roll automaticamente.
- **Ficcion**: las tomas con mayor densidad de dialogo son A-roll.
- Todo lo demas, B-roll.

De donde sale el tipo de proyecto
---------------------------------
`<disco>/.cinema_assistant/project_config.json` -> `"project_kind": "documental"
| "ficcion"`. Si falta, se INFIERE (hay categorias de entrevista -> documental)
y se AVISA. Nunca se decide en silencio.

Nada se calcula de cero: la densidad de dialogo combina senales que ya estan en
el manifest (`content_segments`, `transcript_quality`, `audio_sync_pairs`,
`question_segments`, `clip_descriptions`). Cada decision queda escrita con su
motivo en español en `clip_roll.reason`, para poder auditarla y corregirla.

Correcciones a mano: en project_config.json
    "roll_overrides": { "1234": "A", "1250": "B", "1299": "descarte" }

Uso:
    python3 bin/derive_roll.py --root <disco>
    python3 bin/derive_roll.py --root <disco> --kind ficcion
    python3 bin/derive_roll.py --root <disco> --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import dialogue_density as dd                   # noqa: E402
from lib.guards import assert_selected, report_done      # noqa: E402
from lib import manifest  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_roll (
    clip_id INTEGER PRIMARY KEY,
    roll TEXT,               -- A | B | descarte
    score REAL,              -- densidad de dialogo 0..1
    scene TEXT,              -- escena detectada (ficcion), si la hay
    take TEXT,
    reason TEXT,             -- en español, legible, auditable
    source TEXT,             -- regla | override
    decided_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_roll ON clip_roll(roll);
"""


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def leer_config(root: Path) -> dict:
    p = root / ".cinema_assistant" / "project_config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"  ⚠ project_config.json ilegible ({p}) — sigo sin el.")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--kind", choices=["documental", "ficcion", "comercial"],
                    default=None,
                    help="Fuerza el tipo de proyecto (gana sobre project_config.json).")
    ap.add_argument("--project-prefix", default="",
                    help="Prefijo de rel_path. Vacio = todo el manifest.")
    ap.add_argument("--informe", default="",
                    help="Ruta del informe. Default: "
                         "<disco>/.cinema_assistant/reports/informe_roll.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    cfg = leer_config(root)
    conn = manifest.conectar(str(db))
    conn.executescript(SCHEMA)

    # --- clips -------------------------------------------------------------
    pfx = args.project_prefix
    SIN_CATEGORIAS = """
        SELECT c.id, c.filename, c.rel_path, c.duration_sec, '' AS category
        FROM clips c
        WHERE c.file_kind='video' AND c.index_status='ok'
          AND (? = '' OR c.rel_path LIKE ? || '%')
        ORDER BY c.id
    """
    try:
        rows = conn.execute("""
            SELECT c.id, c.filename, c.rel_path, c.duration_sec,
                   IFNULL(d.category,'') AS category
            FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id = c.id
            WHERE c.file_kind='video' AND c.index_status='ok'
              AND (? = '' OR c.rel_path LIKE ? || '%')
            ORDER BY c.id
        """, (pfx, pfx)).fetchall()
    except sqlite3.OperationalError:
        # Sin `clip_descriptions` todavia. Las otras cinco señales de este
        # script ya toleran su tabla ausente; esta no, y reventaba con un
        # traceback de SQLite en cualquier proyecto recien transcrito (IMODAE
        # 2026-08-07). En una pieza con guion ademas nunca hara falta: la
        # categoria no decide nada, decide quien dice el texto.
        print("  · sin tabla clip_descriptions — se sigue sin categorias. "
              "Correr derive_video_categories.py si el proyecto las necesita.")
        rows = conn.execute(SIN_CATEGORIAS, (pfx, pfx)).fetchall()
    assert_selected(rows, "clips de video a clasificar", filters={
        "--root": str(root), "--project-prefix": pfx or "(todo)",
    })

    # --- senales -----------------------------------------------------------
    habla = defaultdict(float)
    distintas = Counter()
    try:
        for cid, s, e, dw in conn.execute(
                "SELECT clip_id, start_sec, end_sec, distinct_words "
                "FROM content_segments"):
            habla[cid] += max(0.0, float(e or 0) - float(s or 0))
            distintas[cid] += int(dw or 0)
    except sqlite3.OperationalError:
        print("  · sin tabla content_segments — la densidad se apoyara solo en "
              "categoria y sync. Correr derive_content_segments.py.")

    con_sync = set()
    try:
        con_sync = {r[0] for r in conn.execute(
            "SELECT DISTINCT video_clip_id FROM audio_sync_pairs")}
    except sqlite3.OperationalError:
        pass
    con_preguntas = set()
    try:
        con_preguntas = {r[0] for r in conn.execute(
            "SELECT DISTINCT clip_id FROM question_segments")}
    except sqlite3.OperationalError:
        pass
    alucinados = set()
    try:
        alucinados = {r[0] for r in conn.execute(
            "SELECT clip_id FROM transcript_quality WHERE is_hallucinated=1")}
    except sqlite3.OperationalError:
        pass
    # Clips que dicen el texto guionizado. Es la señal de A-roll en una pieza
    # con guion; en documental y ficcion no se usa.
    #
    # UN GRUPO DE UNA SOLA TOMA NO ES UNA TOMA (2026-08-18, cobertura
    # de agosto dia 2). `derive_takes` abre un grupo por cada clip con dialogo, se repita
    # o no, asi que "tiene fila en clip_takes" incluye la charla de rodaje: el
    # "ya esta aprobado, Guadalupe" de 11 s, el "dile a mi asistente que son
    # uno arriba y uno abajo". Con la regla vieja esos 16 clips entraban a
    # A-ROLL y el editor tenia que saltarselos uno por uno.
    #
    # La senal de que algo es una toma es que el texto SE REPITIO: eso es lo
    # que significa rodar varios intentos de la misma pieza. Es la misma regla
    # con la que `derive_video_categories` marca 'toma-guionizada', para que
    # las dos no puedan contradecirse.
    con_toma, sueltos = set(), 0
    try:
        con_toma = {r[0] for r in conn.execute("""
            SELECT clip_id FROM clip_takes WHERE group_id IN (
                SELECT group_id FROM clip_takes GROUP BY group_id HAVING COUNT(*) > 1)
        """)}
        sueltos = conn.execute("""
            SELECT COUNT(*) FROM clip_takes WHERE group_id IN (
                SELECT group_id FROM clip_takes GROUP BY group_id HAVING COUNT(*) = 1)
        """).fetchone()[0]
    except sqlite3.OperationalError:
        pass

    # --- tipo de proyecto --------------------------------------------------
    kind = args.kind or cfg.get("project_kind")
    inferido = False
    # `comercial` NO se infiere: se declara. Un rodaje con guion se parece
    # demasiado a la ficcion desde el manifest, y adivinar mal ahi reparte el
    # A-roll entero por un umbral que no aplica.
    if kind not in ("documental", "ficcion", "comercial"):
        hay_entrevistas = any(
            (r[4] or "").lower() in dd.CATEGORIAS_ENTREVISTA for r in rows)
        kind = "documental" if hay_entrevistas else "ficcion"
        inferido = True

    # --- densidad por clip -------------------------------------------------
    densidades = {}
    motivos_densidad = {}
    escenas = {}
    for cid, fn, rel, dur, cat in rows:
        d, motivo = dd.dialogue_density(
            duration_sec=dur,
            speech_sec=habla.get(cid, 0.0),
            distinct_words=distintas.get(cid, 0),
            tiene_sync=cid in con_sync,
            tiene_preguntas=cid in con_preguntas,
            is_hallucinated=cid in alucinados,
        )
        densidades[cid] = d
        motivos_densidad[cid] = motivo
        escenas[cid] = dd.detectar_escena(rel or "", fn or "")

    # --- decidir -----------------------------------------------------------
    overrides = {}
    for k, v in (cfg.get("roll_overrides") or {}).items():
        try:
            overrides[int(k)] = str(v)
        except (TypeError, ValueError):
            continue

    umbral = None
    umbral_motivo = ""
    dens_por_escena = {}
    n_con_escena = 0
    if kind == "ficcion":
        candidatos = [densidades[r[0]] for r in rows
                      if (r[4] or "").lower() not in dd.CATEGORIAS_FUERA]
        umbral, umbral_motivo = dd.umbral_ficcion(candidatos)
        por_escena = defaultdict(list)
        for cid, (esc, _t) in escenas.items():
            if esc:
                por_escena[esc].append(densidades[cid])
        dens_por_escena = {e: dd.mediana(v) for e, v in por_escena.items()}
        n_con_escena = sum(1 for e, _ in escenas.values() if e)

    # --- camaras declaradas como behind the scenes -------------------------
    # `"bts_camaras": ["DJI_001"]` — por parent_folder o por fragmento de ruta,
    # igual que los skews. Cual camara es la de BTS no se puede deducir del
    # manifest: la misma camara es principal en otro rodaje. Lo sabe quien
    # estuvo ahi, asi que se declara.
    bts_cams = [str(c) for c in (cfg.get("bts_camaras") or []) if str(c).strip()]

    # Comercial CON entrevistas (2026-08-18). Opt-in y declarado, nunca inferido:
    # desde el manifest una entrevista y una charla de rodaje larga se parecen
    # demasiado. Ver lib/dialogue_density.decidir_comercial.
    aroll_entrevistas = bool(cfg.get("aroll_incluye_entrevistas"))

    def es_bts(rel_path: str) -> str:
        for c in bts_cams:
            if c and (f"/{c}/" in f"/{rel_path or ''}" or (rel_path or "").startswith(c + "/")):
                return c
        return ""

    decisiones = {}
    n_bts = 0
    for cid, fn, rel, dur, cat in rows:
        if cid in overrides:
            decisiones[cid] = (overrides[cid], densidades[cid],
                               "fijado a mano en project_config.json "
                               "(roll_overrides)", "override")
            continue
        cam = es_bts(rel)
        if cam:
            n_bts += 1
            decisiones[cid] = (dd.BTS, densidades[cid],
                               f"cámara '{cam}' declarada como behind the scenes "
                               f"en project_config.json (bts_camaras)", "regla")
            continue
        if kind == "comercial":
            # Segunda via a A-roll, opt-in: un comercial con entrevistas. La
            # entrevista no se agrupa en clip_takes (cada respuesta es distinta)
            # y con la regla pura caia en B-roll siendo el material principal.
            es_entrev = (aroll_entrevistas
                         and (cat or "").lower() in dd.CATEGORIAS_ENTREVISTA)
            roll, motivo = dd.decidir_comercial(
                categoria=cat, es_toma=cid in con_toma,
                densidad=densidades[cid], es_entrevista=es_entrev)
        elif kind == "documental":
            es_entrevista = ("entrevista" in (rel or "").lower()
                             or cid in con_preguntas)
            roll, motivo = dd.decidir_documental(
                categoria=cat, es_entrevista=es_entrevista,
                densidad=densidades[cid])
        else:
            esc, _t = escenas[cid]
            roll, motivo = dd.decidir_ficcion(
                categoria=cat, densidad=densidades[cid], umbral=umbral,
                densidad_escena=dens_por_escena.get(esc) if esc else None,
                escena=esc)
        if roll != dd.DESCARTE:
            motivo = f"{motivo} · {motivos_densidad[cid]}"
        decisiones[cid] = (roll, densidades[cid], motivo, "regla")

    # --- escribir ----------------------------------------------------------
    if not args.dry_run:
        conn.execute("DELETE FROM clip_roll")
        now = time.time()
        for cid, (roll, score, motivo, src) in decisiones.items():
            esc, toma = escenas.get(cid, (None, None))
            conn.execute(
                "INSERT INTO clip_roll (clip_id, roll, score, scene, take,"
                " reason, source, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, roll, score, esc, toma, motivo, src, now))
        conn.commit()

    # --- reporte -----------------------------------------------------------
    cuenta = Counter(v[0] for v in decisiones.values())
    print(f"\nSeparacion A-roll / B-roll — {root.name}")
    print("=" * 66)
    print(f"tipo de proyecto : {kind}" + ("  (INFERIDO — ver aviso abajo)"
                                          if inferido else ""))
    if kind == "comercial":
        # Decirlo SIEMPRE, encendido o apagado: si el flag falta en un proyecto
        # que si tiene entrevistas, sus clips se van a B-roll y nadie se entera.
        n_ent = sum(1 for r in rows
                    if (r[4] or "").lower() in dd.CATEGORIAS_ENTREVISTA)
        estado = "SI" if aroll_entrevistas else "NO"
        print(f"entrevistas a A-roll: {estado}"
              f"  ({n_ent} clip(s) categorizados como entrevista)")
        print(f"tomas (texto repetido): {len(con_toma)} clip(s) en grupos de 2 o mas")
        if sueltos:
            print(f"   {sueltos} clip(s) con dialogo quedaron en grupo de UNA toma: "
                  "no son toma, van por la regla general")
        if n_ent and not aroll_entrevistas:
            print("   ⚠  hay entrevistas y se estan yendo a B-ROLL. Si sostienen"
                  " la pieza, declarar")
            print('      {"aroll_incluye_entrevistas": true}  en '
                  'project_config.json')
    if kind == "ficcion":
        print(f"umbral efectivo  : {umbral:.2f}  ({umbral_motivo})")
        print(f"clips con escena : {n_con_escena} de {len(rows)}"
              f"  ({len(dens_por_escena)} escenas)")
    print("")
    for r, etiqueta in ((dd.A_ROLL, "A-roll"), (dd.B_ROLL, "B-roll"),
                        (dd.BTS, "behind the scenes"), (dd.DESCARTE, "descartes")):
        n = cuenta.get(r, 0)
        pct = 100 * n / len(rows) if rows else 0
        print(f"  {etiqueta:<10} {n:>5}  ({pct:4.1f}%)")
    if overrides:
        print(f"  {'overrides':<10} {len(overrides):>5}  (fijados a mano)")

    if inferido:
        print(f"\n⚠  `project_kind` no estaba en project_config.json. Se infirio "
              f"'{kind}'.\n   Si es incorrecto, la clasificacion entera lo es. "
              f"Fijarlo explicitamente:\n"
              f'   {{"project_kind": "documental"}}  en '
              f"{root}/.cinema_assistant/project_config.json")
    if kind != "comercial" and len(con_toma) >= 5:
        print(f"\n⚠  Hay {len(con_toma)} clips agrupados como TOMAS del mismo "
              f"texto (derive_takes).\n   Eso es un rodaje con guion. Si esta "
              f"pieza lo es, el A-roll no se decide por\n   densidad sino por "
              f"quien dice el texto:\n"
              f'   {{"project_kind": "comercial"}}  en '
              f"{root}/.cinema_assistant/project_config.json")
    if kind == "ficcion" and n_con_escena < len(rows) * 0.5:
        print(f"\n⚠  Solo {n_con_escena} de {len(rows)} clips tienen escena "
              f"reconocible en su nombre o carpeta.\n   En ficcion la decision "
              f"deberia tomarse por ESCENA: sin eso, el inserto mudo de una "
              f"escena\n   de dialogo cae en B-roll y rompe la continuidad del "
              f"A-roll. Convenciones que\n   se reconocen: ESC12_T03, SC12_TK03, "
              f"12A-3, E12T03, o una carpeta por escena.")

    # --- informe -----------------------------------------------------------
    escritos = 0
    if not args.dry_run:
        destino = Path(args.informe) if args.informe else \
            root / ".cinema_assistant" / "reports" / "informe_roll.md"
        destino.parent.mkdir(parents=True, exist_ok=True)
        L = []
        L.append(f"# A-roll / B-roll — {root.name}\n")
        L.append(f"Generado: {time.strftime('%Y-%m-%d %H:%M')}  ·  "
                 f"tipo de proyecto: **{kind}**"
                 + ("  _(inferido)_" if inferido else "") + "\n")
        if kind == "ficcion":
            L.append(f"Umbral efectivo: **{umbral:.2f}** ({umbral_motivo})\n")
        L.append("\n| | clips | % |\n|---|---:|---:|")
        for r, etiqueta in ((dd.A_ROLL, "A-roll"), (dd.B_ROLL, "B-roll"),
                            (dd.DESCARTE, "descartes")):
            n = cuenta.get(r, 0)
            L.append(f"| {etiqueta} | {n} | {100*n/len(rows) if rows else 0:.1f}% |")
        L.append("\n> Revisar esta lista **antes** de dejar que el script mueva "
                 "clips de bin.\n> Para corregir un clip, agregarlo a "
                 "`roll_overrides` en `project_config.json`.\n")
        for r, etiqueta in ((dd.A_ROLL, "A-roll"), (dd.DESCARTE, "descartes")):
            items = [(cid, v) for cid, v in decisiones.items() if v[0] == r]
            if not items:
                continue
            L.append(f"\n## {etiqueta} ({len(items)})\n")
            L.append("| clip | archivo | densidad | por que |")
            L.append("|---:|---|---:|---|")
            nombres = {cid: fn for cid, fn, _, _, _ in rows}
            for cid, (roll, score, motivo, src) in sorted(
                    items, key=lambda x: -x[1][1]):
                L.append(f"| {cid} | {nombres.get(cid,'?')} | {score:.2f} | "
                         f"{motivo} |")
        destino.write_text("\n".join(L) + "\n", encoding="utf-8")
        escritos = 1
        print(f"\nInforme: {destino}")

    conn.close()
    report_done("derive_roll", clips=len(rows), a_roll=cuenta.get(dd.A_ROLL, 0),
                b_roll=cuenta.get(dd.B_ROLL, 0), bts=cuenta.get(dd.BTS, 0),
                descartes=cuenta.get(dd.DESCARTE, 0), informe=escritos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
