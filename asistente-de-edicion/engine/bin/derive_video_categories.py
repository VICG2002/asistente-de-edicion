#!/usr/bin/env python3
"""Clasifica videos como `entrevista` o categorias derivadas, poblando
`clip_descriptions.category` para los clips que no estaban categorizados.

Senales (las mismas documentadas en `metodologia/pasos-a-seguir.md` paso 7,
de mas debil a mas fuerte):

  1. Carpeta llamada `entrevista` / `ENTREVISTA` / etc. — match por rel_path.
     Conservadora: solo clips de video bajo una carpeta con 'entrevista' en su
     nombre.
  2. (no implementada todavia) Heuristica: camara principal + >=60 s + motion <8.
  3. (no implementada todavia) Transcript denso (>=60 palabras distintas).

Por que solo senal 1: cuando se categorizo JILOTEPEC se mezclaron senales con
revision humana (hojas de contacto + desc_*.json). Para los 4 sectores
faltantes, capturar las entrevistas obvias por nombre de carpeta es 95% del
trabajo y no introduce falsos positivos. El operador puede agregar a mano las
restantes via revision de hojas de contacto si las nota.

NO sobreescribe categorias existentes — solo agrega para clips que no tienen
ningun row en clip_descriptions, o que estan vacios.

Uso:
    python3 bin/derive_video_categories.py --root /Volumes/MI_DISCO
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.transcript_quality import analyze_transcript
from lib.guards import assert_prefix_casa, exigir_prefix  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. Prefijo de rel_path del proyecto. Pasar "
                         "'' para proyecto plano (todo el manifest). No hay "
                         "default a proposito: uno heredado deja el proyecto "
                         "entero sin categorizar y sale con exit 0.")
    ap.add_argument("--min-distinct", type=int, default=60,
                    help="Señal 3: palabras distintas mínimas en transcript limpio.")
    ap.add_argument("--min-dur", type=float, default=30.0,
                    help="Señal 3: duración mínima del clip en segundos.")
    ap.add_argument("--no-senal3", action="store_true",
                    help="Desactivar señal 3 (transcript denso).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo reporta lo que actualizaria, no escribe.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("derive_video_categories"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    # Cero entrevistas puede ser legitimo; un prefix que no casa con nada, no.
    assert_prefix_casa(conn, args.project_prefix, "derive_video_categories")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_descriptions (
            clip_id INTEGER PRIMARY KEY,
            category TEXT,
            description TEXT,
            source TEXT,
            updated_at REAL
        )
    """)

    # Senal 1: rel_path con 'entrevista'. Para cada candidato, validar el
    # transcript con `lib.transcript_quality.analyze_transcript()` v3 — si el
    # audio scratch produjo alucinaciones LOCALIZADAS (caso Zezzions
    # 2026-05-26: musica alta encima de dialogo solo al final del clip),
    # NO descartar el clip entero. Marcar como 'entrevista-degradada' solo
    # cuando > 50% basura. Guardar `clean_until_sec` en clip_descriptions.
    # description (formato `[clean_until_sec=N.NN]`) para que
    # derive_question_segments filtre preguntas en zona basura.
    tr_dir = root / ".cinema_assistant" / "transcripts"
    pfx = args.project_prefix
    candidates = conn.execute("""
        SELECT c.id, c.rel_path, IFNULL(d.category,'') AS existing_cat,
               IFNULL(d.source,'') AS existing_src
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        WHERE c.file_kind='video' AND c.index_status='ok'
          AND lower(c.rel_path) LIKE '%entrevista%'
          AND (? = '' OR c.rel_path LIKE ? || '%')
    """, (pfx, pfx)).fetchall()

    n_total = len(candidates)
    n_skip = 0
    n_update = 0
    n_degraded = 0
    by_sector = {}
    by_cat = {"entrevista": 0, "entrevista-degradada": 0}
    n_rescued = 0  # entrevistas-degradadas v1 que la v3 rescata
    now = time.time()
    for cid, rp, existing, existing_src in candidates:
        sector_end = rp.find("/", 17)
        sector = rp[17:sector_end] if sector_end > 17 else "?"

        # Skip si ya tiene categoria asignada — EXCEPTO si es 'entrevista-degradada'
        # que viene de este mismo script (re-evaluable con v3).
        is_self_degraded = (
            existing == "entrevista-degradada"
            and existing_src.startswith("derive_video_categories.py")
        )
        if existing and existing != "" and not is_self_degraded:
            n_skip += 1
            continue

        # Detectar alucinaciones del transcript con analyze_transcript() v3.
        # Solo descartar el clip entero si > 50% basura. Si la basura es
        # localizada (ej. solo al final), conservar como 'entrevista' y
        # guardar clean_until_sec para que downstream filtre la zona basura.
        cat = "entrevista"
        reason = ""
        clean_until_sec = None
        tr_path = tr_dir / f"{cid}.json"
        if tr_path.exists():
            try:
                d = json.loads(tr_path.read_text(encoding="utf-8"))
                analysis = analyze_transcript(d.get("text", ""), d.get("words", []) or [])
                if analysis["is_hallucinated"]:
                    cat = "entrevista-degradada"
                    reason = "; ".join(analysis["reasons"][:2])
                    n_degraded += 1
                elif analysis["clean_until_sec"] is not None:
                    # Localizada pero no descartable: conservar como entrevista
                    # y propagar clean_until_sec a downstream.
                    clean_until_sec = analysis["clean_until_sec"]
                    reason = f"clean_until_sec={clean_until_sec:.2f}"
            except Exception:
                pass

        if not args.dry_run:
            # Formato del description segun el caso:
            # - 'entrevista-degradada': "[degradada: razon]" — descartar entero
            # - 'entrevista' con clean_until_sec: "[clean_until_sec=N.NN]" —
            #   downstream filtra zona basura desde ese timestamp en adelante
            # - 'entrevista' sin basura: ""
            if cat == "entrevista-degradada":
                desc = f"[degradada: {reason}]"
            elif clean_until_sec is not None:
                desc = f"[clean_until_sec={clean_until_sec:.2f}]"
            else:
                desc = ""
            conn.execute(
                "INSERT OR REPLACE INTO clip_descriptions"
                " (clip_id, category, description, source, updated_at)"
                " VALUES (?,?,?,?,?)",
                (cid, cat, desc, "derive_video_categories.py:senal1", now)
            )
        n_update += 1
        by_cat[cat] += 1
        if is_self_degraded and cat == "entrevista":
            n_rescued += 1
        by_sector[sector] = by_sector.get(sector, 0) + 1

    # --- Señal 3: transcript denso + filtro anti-alucinación -----------------
    # Para proyectos sin carpetas 'entrevista' (vox pop, eventos), la señal
    # más fuerte es el contenido: >= min_distinct palabras distintas en zona
    # limpia del transcript + duración mínima + cámara principal. Conservadora:
    # NUNCA sobreescribe categoría existente. Doctrina paso 7 del playbook.
    # SENAL 0 — la toma guionizada NO es una entrevista (2026-08-18, Daniel
    # Espinosa dia 2).
    #
    # La densidad lexica no puede distinguirlas: un texto de prompter de 60 s
    # es tan denso como una respuesta de entrevista, y la senal 3 marco como
    # `entrevista` los 15 intentos de cinco reels distintos. Con eso, el corte
    # de silencios —que se pide SOLO en entrevistas— se habria comido las
    # esperas de prompter de las tomas, y el reparto A-roll habria mezclado dos
    # cosas que el editor necesita separadas.
    #
    # Lo que SI distingue es un hecho medido, no una heuristica: si el texto se
    # repitio, es una toma. `derive_takes` ya lo sabe porque agrupa por texto.
    # Un grupo de UNA sola toma no cuenta: eso es un clip suelto con dialogo,
    # que es justamente lo que puede ser una entrevista.
    tomas_repetidas = set()
    try:
        tomas_repetidas = {r[0] for r in conn.execute("""
            SELECT clip_id FROM clip_takes WHERE group_id IN (
                SELECT group_id FROM clip_takes GROUP BY group_id HAVING COUNT(*) > 1)
        """)}
    except sqlite3.OperationalError:
        pass   # proyecto sin derive_takes: no aplica y no pasa nada
    n0 = 0
    if tomas_repetidas and not args.dry_run:
        for cid in sorted(tomas_repetidas):
            conn.execute(
                "INSERT OR REPLACE INTO clip_descriptions"
                " (clip_id, category, description, source, updated_at)"
                " VALUES (?,?,?,?,?)",
                (cid, "toma-guionizada",
                 "el texto se repite en otros clips (derive_takes)",
                 "derive_video_categories.py:senal0", now))
            n0 += 1
    elif tomas_repetidas:
        n0 = len(tomas_repetidas)

    n3_total = n3_update = n3_skip_hal = 0
    if not args.no_senal3:
        s3_candidates = conn.execute("""
            SELECT c.id, c.duration_sec
            FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id = c.id
            WHERE c.file_kind='video' AND c.index_status='ok'
              AND (? = '' OR c.rel_path LIKE ? || '%')
              AND IFNULL(d.category,'') = ''
              AND c.duration_sec >= ?
        """, (pfx, pfx, args.min_dur)).fetchall()
        n3_total = len(s3_candidates)
        for cid, dur in s3_candidates:
            tr_path = tr_dir / f"{cid}.json"
            if not tr_path.exists():
                continue
            try:
                d = json.loads(tr_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            words = d.get("words", []) or []
            analysis = analyze_transcript(d.get("text", ""), words)
            if analysis["is_hallucinated"]:
                n3_skip_hal += 1
                continue
            clean_until = analysis.get("clean_until_sec")
            # Palabras distintas dentro de la zona limpia
            distinct = set()
            for w in words:
                if not (isinstance(w, list) and len(w) >= 2):
                    continue
                if clean_until is not None and isinstance(w[1], (int, float)) \
                        and w[1] > clean_until:
                    break
                t = str(w[0]).lower().strip(".,?¿!¡;:'\"-")
                if len(t) >= 2:
                    distinct.add(t)
            if len(distinct) < args.min_distinct:
                continue
            if not args.dry_run:
                desc = (f"[clean_until_sec={clean_until:.2f}]"
                        if clean_until is not None else "")
                conn.execute(
                    "INSERT OR REPLACE INTO clip_descriptions"
                    " (clip_id, category, description, source, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (cid, "entrevista", desc,
                     "derive_video_categories.py:senal3", now)
                )
            n3_update += 1

    # OVERRIDES DEL EDITOR — la ultima palabra, y por eso van al final.
    #
    # POR QUE (2026-08-18). Las senales miden lo que se puede medir, y hay
    # material que ninguna alcanza. En el dia 2 de esa cobertura, tres clips
    # del mismo evento —9 s, 11 s y 35 s— se quedaron por debajo del umbral de
    # 60 palabras distintas: son cortos, no pobres. Son la misma conversacion
    # que los cinco clips largos que si pasaron, y dejarlos fuera los sacaba del
    # corte de pausas y de A-roll.
    #
    #     "category_overrides": {"C1253.MP4": "entrevista", "C1246.MP4": null}
    #
    # `null` borra la categoria (devuelve el clip a 'sin categoria'). Un nombre
    # que no existe en el material se avisa, no se ignora: asi se caza el
    # override que dejo de aplicar despues de un recorte.
    cfg_path = Path(args.root) / ".cinema_assistant" / "project_config.json"
    overrides = {}
    if cfg_path.exists():
        try:
            overrides = json.loads(cfg_path.read_text(encoding="utf-8")).get(
                "category_overrides") or {}
        except (json.JSONDecodeError, OSError):
            overrides = {}
    n_ovr = 0
    for fn, cat in overrides.items():
        row = conn.execute("SELECT id FROM clips WHERE filename=? AND file_kind='video'",
                           (fn,)).fetchone()
        if not row:
            print(f"  ⚠ category_override '{fn}': ese clip no esta en el material")
            continue
        if args.dry_run:
            n_ovr += 1
            continue
        if cat is None:
            conn.execute("DELETE FROM clip_descriptions WHERE clip_id=?", (row[0],))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO clip_descriptions"
                " (clip_id, category, description, source, updated_at)"
                " VALUES (?,?,?,?,?)",
                (row[0], str(cat), "declarado por el editor en project_config.json",
                 "derive_video_categories.py:override", now))
        n_ovr += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    if overrides:
        print(f"Overrides del editor aplicados: {n_ovr} de {len(overrides)}")
    print(f"Senal 0 (el texto se repite -> 'toma-guionizada', no entrevista): {n0}")
    print(f"Candidatos via senal 1 (rel_path con 'entrevista'): {n_total}")
    print(f"  ya tenian categoria — preservados: {n_skip}")
    print(f"  {'(dry-run) ' if args.dry_run else ''}actualizados: {n_update}")
    print(f"    'entrevista':            {by_cat['entrevista']}")
    print(f"    'entrevista-degradada':  {by_cat['entrevista-degradada']}  "
          f"(transcript con alucinaciones de Whisper)")
    if n_rescued:
        print(f"  rescatadas de 'entrevista-degradada' v1 -> 'entrevista' v3: {n_rescued}")
    if by_sector:
        print("  Por sector:")
        for s, n in sorted(by_sector.items()):
            print(f"    {s}: {n}")
    if not args.no_senal3:
        print(f"Candidatos via senal 3 (transcript denso, sin categoria previa): {n3_total}")
        print(f"  marcados 'entrevista': {n3_update}")
        print(f"  saltados por transcript alucinado: {n3_skip_hal}")


if __name__ == "__main__":
    main()
