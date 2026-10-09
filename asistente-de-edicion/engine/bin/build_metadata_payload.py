#!/usr/bin/env python3
"""Compone los 5 campos de metadata de DaVinci Resolve por clip a partir de
TODAS las tablas: clip_descriptions, clip_curated_segments, clip_characters
(updated by identify_faces), clip_shot_values, clip_angles, clip_analysis.

Reglas de asignación (lección v10 → v11: cada dato a su campo dedicado):

  Description = resumen de los tramos curados (1-3 líneas) — sintetiza los
                full_text de clip_curated_segments. Si no hay tramos,
                usa la descripción legacy de clip_descriptions.description.

  Shot        = shot_value dominante (heurístico o manual).

  Scene       = category (entrevista, accion-dialogo, etc.).

  Keywords    = lista coma-separada filtrable:
                  categoría, shot_value, angle, personajes, sector.

  Comments    = SOLO analysis_notes (notas técnicas: clipping, sombras
                aplastadas, mucho movimiento, etc.) — todo lo demás tiene
                su propio campo y NO se duplica aquí.

Output: tabla nueva `clip_metadata_payload(clip_id, description, shot,
                                            scene, keywords, comments)`.
La consume export_lua_data.py para emitir el payload al Lua.
"""

from __future__ import annotations

import argparse
import glob
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.guards import assert_prefix_casa, assert_selected, exigir_prefix  # noqa: E402
from lib import manifest  # noqa: E402


# Detecta sector del rel_path (segundo nivel después del project_prefix).
# Si project_prefix es "" (proyecto plano), toma el primer subfolder del rel_path.
# El parametro NO tiene default: lo tuvo ("ESCALANDO MEXICO/") y en cualquier otro
# proyecto devolvia "" para todos los clips, dejando el payload sin sector.
def detect_sector(rel_path: str, project_prefix: str) -> str:
    rel_path = rel_path or ""
    if project_prefix:
        pat = re.escape(project_prefix) + r"([^/]+)"
    else:
        pat = r"^([^/]+)"
    m = re.match(pat, rel_path)
    if not m:
        return ""
    seg = m.group(1).strip()
    seg = re.sub(r"^\d+_", "", seg).strip()
    return seg


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def summarize_segments(segments: list[dict], clip_dur: float = 0) -> str:
    """Resumen GLOBAL del clip — distinto al full_text de cada tramo.

    Filosofía v14: la Description del clip responde 'qué es este clip
    como UN TODO' (tipo de toma, cuántos momentos clave, arco temporal),
    mientras que el marker Green de cada tramo responde 'qué pasa EN
    ESTE momento'. Evita repetir literalmente el full_text del tramo.

    Reglas:
      - Si solo 1 tramo cubre el clip entero: la Description es un
        resumen corto del tipo (1 línea: SUJETO genérico | PLANO | acción).
        El marker del tramo lleva el detalle.
      - Si N tramos: la Description menciona N momentos clave + el arco
        (qué cambia entre tramos: planos, sujetos, acción).
    """
    if not segments:
        return ""

    # ¿Cuántos tramos curados por LLM/Claude (con descripción real)?
    rich = [s for s in segments if s["full_text"] and "|" in s["full_text"]]

    if len(segments) == 1:
        s = segments[0]
        text = s["full_text"] or s["what_action"] or ""
        parts = text.split(" | ", 2)
        if len(parts) == 3:
            subject, plano, _accion = parts
            # Description = TÍTULO del clip, no la misma desc del tramo.
            # El editor lee el marker Green para el detalle de la acción.
            dur_str = f"{clip_dur:.0f}s" if clip_dur else ""
            return f"{subject} ({plano}{', ' + dur_str if dur_str else ''}). Ver marker Green T0 para descripción de la acción."
        return f"Clip de {clip_dur:.0f}s. Ver marker Green para detalle." if clip_dur else "Ver marker Green para detalle."

    # Múltiples tramos: arco del clip
    n = len(segments)
    # Sujetos únicos a través de los tramos
    subjects = []
    planos = []
    for s in segments[:6]:
        text = s["full_text"] or ""
        parts = text.split(" | ", 2)
        if len(parts) == 3:
            subjects.append(parts[0].strip())
            planos.append(parts[1].strip())
    uniq_subj = []
    seen = set()
    for sj in subjects:
        if sj and sj not in seen:
            seen.add(sj)
            uniq_subj.append(sj)
    uniq_planos = []
    seen = set()
    for p in planos:
        if p and p not in seen:
            seen.add(p)
            uniq_planos.append(p)

    arco = []
    if uniq_subj:
        if len(uniq_subj) == 1:
            arco.append(f"Sujeto: {uniq_subj[0]}")
        else:
            arco.append(f"Sujetos a lo largo del clip: {', '.join(uniq_subj[:3])}")
    if uniq_planos:
        if len(uniq_planos) == 1:
            arco.append(f"Plano uniforme: {uniq_planos[0]}")
        else:
            arco.append(f"Cambios de plano: {' → '.join(uniq_planos[:4])}")

    head = f"Clip de {clip_dur:.0f}s con {n} tramos clave." if clip_dur else f"Clip con {n} tramos clave."
    return head + " " + ". ".join(arco) + ". Ver markers Green para detalle por tramo."


def dedupe_keep_order(items):
    seen = set()
    out = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. rel_path prefix; '' = proyecto plano. Sin "
                         "default a proposito: este script hace DELETE global de "
                         "clip_metadata_payload antes de repoblarla, asi que un "
                         "prefix que no casa vacia la tabla y no escribe nada.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("build_metadata_payload"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    # ORDEN IMPORTANTE: esta comprobacion va ANTES del DELETE de abajo. El script
    # vacia clip_metadata_payload y la repuebla; si el prefix no casa con nada, el
    # DELETE se ejecuta igual y la repoblacion inserta cero filas. Resultado: se
    # pierde el payload entero de un proyecto ya procesado, en silencio.
    assert_prefix_casa(conn, args.project_prefix, "build_metadata_payload")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_metadata_payload (
            clip_id INTEGER PRIMARY KEY,
            description TEXT,
            shot TEXT,
            scene TEXT,
            keywords TEXT,
            comments TEXT,
            updated_at REAL
        )
    """)
    conn.execute("DELETE FROM clip_metadata_payload")

    # Pre-carga curated_segments por clip
    segs_by_clip = {}
    for row in conn.execute("""
        SELECT clip_id, seg_index, start_sec, end_sec, shot_value, angle,
               characters, what_action, what_stands, where_at, objects,
               dialogue_idea, full_text, is_representative, total_dur_note,
               curated_by
        FROM clip_curated_segments ORDER BY clip_id, seg_index
    """):
        cid = row[0]
        segs_by_clip.setdefault(cid, []).append({
            "seg_index": row[1], "start_sec": row[2], "end_sec": row[3],
            "shot_value": row[4] or "", "angle": row[5] or "",
            "characters": row[6] or "", "what_action": row[7] or "",
            "what_stands": row[8] or "", "where_at": row[9] or "",
            "objects": row[10] or "", "dialogue_idea": row[11] or "",
            "full_text": row[12] or "",
            "is_representative": row[13] or 0,
            "total_dur_note": row[14] or "",
            "curated_by": row[15] or "auto",
        })

    # A-roll / B-roll (v0.2.0). Opcional: si no se corrió derive_roll.py, el
    # payload simplemente no lleva ese keyword.
    roll_by_clip: dict = {}
    try:
        for cid_r, rol in conn.execute("SELECT clip_id, roll FROM clip_roll"):
            roll_by_clip[cid_r] = rol or ""
    except sqlite3.OperationalError:
        pass

    # clip_characters es opcional (Fase 2 / curaduría posterior) — puede no
    # existir en un proyecto fresco (FCC 2026-07-09).
    has_chars = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='clip_characters'").fetchone() is not None
    chars_expr = "IFNULL(cc.characters, '')" if has_chars else "''"
    chars_join = ("LEFT JOIN clip_characters cc ON cc.clip_id = c.id"
                  if has_chars else "")
    rows = conn.execute(f"""
        SELECT c.id, c.rel_path, c.file_kind, c.duration_sec,
               IFNULL(d.category, '') as category,
               IFNULL(d.description, '') as desc_legacy,
               IFNULL(sv.shot_value, '') as shot,
               IFNULL(ang.angle, '') as angle,
               {chars_expr} as chars,
               IFNULL(ca.analysis_notes, '') as analysis_notes
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        LEFT JOIN clip_shot_values sv ON sv.clip_id = c.id
        LEFT JOIN clip_angles ang ON ang.clip_id = c.id
        {chars_join}
        LEFT JOIN clip_analysis ca ON ca.clip_id = c.id
        WHERE c.index_status='ok'
    """).fetchall()

    now = time.time()
    n = 0
    for (cid, rel_path, kind, dur, cat, desc_legacy, shot, angle, chars,
         analysis_notes) in rows:
        segs = segs_by_clip.get(cid, [])

        # Description GLOBAL del clip (distinta del full_text por tramo).
        # Si hay tramos curados, sintetiza el ARCO; si no, usa desc legacy.
        if any(s["full_text"] for s in segs):
            desc = summarize_segments(segs, dur or 0)
        else:
            desc = desc_legacy

        # Shot dominante: el shot_value del primer tramo (= el del clip) si existe;
        # si los tramos tienen shots distintos, concatenar con "/".
        seg_shots = dedupe_keep_order([s["shot_value"] for s in segs if s["shot_value"]])
        if seg_shots:
            shot_field = "/".join(seg_shots[:3])
        else:
            shot_field = shot

        # Scene: la categoría
        scene_field = cat

        # Keywords: rol + categoría + shot + angle + nombres + sector + ángulos
        # adicionales de los tramos si difieren.
        kws = []
        # A-ROLL / B-ROLL primero (v0.2.0): es el filtro mas util del Metadata
        # Editor y la base para que el editor arme sus Smart Bins a mano.
        # Va como keyword — NO mueve el clip de bin, no es destructivo.
        rol = roll_by_clip.get(cid)
        if rol:
            kws.append({"A": "A-ROLL", "B": "B-ROLL"}.get(rol, rol))
        if cat:
            kws.append(cat)
        if shot:
            kws.append(shot)
        if angle:
            kws.append(angle)
        for s in segs:
            if s["angle"] and s["angle"] not in kws:
                kws.append(s["angle"])
            if s["shot_value"] and s["shot_value"] not in kws:
                kws.append(s["shot_value"])
        # Personajes: limpiar "ESCALADOR_A (3)" → "ESCALADOR_A"
        if chars:
            for token in re.split(r"[,;]", chars):
                name = re.sub(r"\s*\(\d+\)$", "", token).strip()
                if name and name not in kws:
                    kws.append(name)
        # Sector
        sec = detect_sector(rel_path or "", args.project_prefix)
        if sec and sec not in kws:
            kws.append(sec)

        kws_str = ", ".join(kws)

        # Comments: SOLO notas técnicas. Las dimensiones que tienen su campo
        # propio (shot, angle, personajes) NO se duplican aquí. (Lección v10.)
        comments_str = analysis_notes

        conn.execute(
            "INSERT INTO clip_metadata_payload "
            "(clip_id, description, shot, scene, keywords, comments, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, desc, shot_field, scene_field, kws_str, comments_str, now)
        )
        n += 1

    conn.commit()
    conn.close()
    print(f"Metadata payload generada para {n} clips/audios.")


if __name__ == "__main__":
    main()
