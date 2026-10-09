#!/usr/bin/env python3
"""Para cada tramo en clip_curated_segments con `full_text` vacío, compone
una descripción usable a partir de los datos disponibles:

  - Personajes mencionados en el transcript del tramo (whitelist del proyecto).
  - Plano + ángulo del tramo.
  - Sector (folder padre) + horario inferido de creation_time.
  - Snippet del transcript word-level que cae dentro del tramo.
  - Categoría → acción genérica ("Pegue", "Charla", "Toma fija", "Timelapse"…).

El resultado NO responde literalmente las 8 preguntas, pero entrega al
editor un marker con: quién aparece (cuando hay evidencia), qué se ve,
en qué plano, qué se dice — útil para decidir cortes sin abrir el clip.

Los tramos con `curated_by='claude'` (descripciones que yo escribí) NO se
tocan.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras  # noqa: E402
from lib import cast as cast_lib  # noqa: E402
from lib import proyecto  # noqa: E402
from lib.guards import exigir_prefix  # noqa: E402
from lib import manifest  # noqa: E402


# El cast NO vive aqui. Vivia: eran 26 nombres de personas reales del rodaje de
# JILOTEPEC escritos dentro del motor. Se movio a `<disco>/.cinema_assistant/
# cast.json` el 2026-07-31 por dos razones —privacidad de esas personas y porque
# era un hardcode de proyecto que dejaba a cualquier otro rodaje sin atribucion,
# en silencio y con exit 0. Ver lib/cast.py.
#
# La whitelist se aplica SOLO si el transcript menciona el nombre; nunca por
# inferencia de caras.

CATEGORY_ACTION = {
    "entrevista":     "Entrevista",
    "charla-equipo":  "Charla del equipo",
    "accion-dialogo": "Pegue con beta-calling",
    "accion":         "Acción/escalada",
    "b-roll":         "B-roll",
    "timelapse":      "Timelapse — variación de luz",
    "pov-static":     "POV cámara fija",
    "scenic":         "Paisaje",
    "portrait":       "Retrato",
    "b-roll-gear":    "B-roll de equipo",
    "discard":        "Descarte",
    "export":         "Export del documental",
}

DEFAULT_SUBJECT = {
    "drone":   "Toma aérea",
    "gopro":   "POV cámara",
    "main":    "Escena",
    "other":   "Escena",
}


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


# `detect_camera` local eliminada: la clasificacion vive en `lib/cameras.py`
# + `config/camera_profiles.json`. Esta copia no conocia VICG ni FX30.


def hour_band(creation: str) -> str:
    if not creation or len(creation) < 13:
        return ""
    h = creation[11:13]
    if not h.isdigit():
        return ""
    h = int(h)
    if 6 <= h < 12:  return "mañana"
    if 12 <= h < 17: return "tarde"
    if 17 <= h < 20: return "atardecer"
    return "noche"


def detect_sector(rel_path: str, prefix: str = "") -> str:
    """El sector de un clip, con el prefijo REAL del proyecto.

    Hasta el 2026-08-05 esto era `re.match(r"ESCALANDO MEXICO/([^/]+)/?")`: en
    cualquier proyecto que no fuera aquel devolvia cadena vacia, y todos los
    tramos quedaban sin sector sin que nada avisara. Estaba en la deuda
    congelada del linter desde julio, y en la ruta caliente del pipeline.
    """
    seg = proyecto.sector_de(rel_path, prefix) or ""
    # Se conserva: los sectores numerados ("01_JILOTEPEC") se normalizan.
    return re.sub(r"^\d+_", "", seg).strip()


def extract_transcript_window(words, start_sec: float, end_sec: float, max_chars: int = 200):
    """Devuelve (texto, n_distinct) de las palabras que caen en el tramo."""
    in_range = [w for w, t in words if start_sec <= t <= end_sec]
    if not in_range:
        return "", 0
    text = " ".join(in_range)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    distinct = len({normalize(w) for w in in_range} - {""})
    return text, distinct


def characters_from_text(text: str, cast: dict[str, set[str]]) -> list[str]:
    """Detecta nombres del cast mencionados textualmente en el tramo.

    `cast` viene de lib.cast.load_cast(root): es del PROYECTO, no del motor.
    Con cast vacio devuelve [] — no se inventan personajes.
    """
    if not text or not cast:
        return []
    tokens = [normalize(t) for t in re.split(r"[\s,.!¡¿?]+", text)]
    tokens = set(t for t in tokens if t)
    found = []
    for name, aliases in cast.items():
        if any(a in tokens for a in aliases):
            found.append(name)
    return found


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
    ap.add_argument("--force", action="store_true",
                    help="re-componer también los que ya tienen full_text auto")
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. rel_path prefix del proyecto. Pasar '' "
                         "para proyecto plano. Sin esto, el sector de cada "
                         "tramo sale vacío y la descripción pierde el lugar.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("enrich_curated_segments"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    # El cast es del proyecto, no del motor. Si no esta declarado se avisa y se
    # sigue sin atribuir personajes: es una perdida de riqueza, no un error — y
    # sobre todo, no pasa callado.
    cast = cast_lib.load_cast(root)
    if cast:
        print(f"  cast del proyecto: {len(cast)} persona(s) declarada(s)")
    else:
        print(cast_lib.aviso_sin_cast(root, "enrich_curated_segments"),
              file=sys.stderr)

    conn = manifest.conectar(str(db))

    # Cache de transcripts (clip_id → words)
    transcripts = {}

    # Pre-carga clip info
    clip_info = {}
    for r in conn.execute("""
        SELECT c.id, c.filename, c.camera_model, c.rel_path, c.creation_time,
               IFNULL(d.category, '') AS category, c.camera_make, c.ext
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        WHERE c.file_kind='video' AND c.index_status='ok'
    """):
        clip_info[r[0]] = {
            "filename": r[1], "camera_model": r[2], "rel_path": r[3],
            "creation_time": r[4], "category": r[5],
            "camera_make": r[6], "ext": r[7],
        }
    reg = cameras.load_profiles(root)

    sql_segs = """
        SELECT id, clip_id, seg_index, start_sec, end_sec, shot_value, angle,
               full_text, curated_by
        FROM clip_curated_segments
    """
    rows = conn.execute(sql_segs).fetchall()

    n_total = len(rows)
    n_updated = 0
    n_skip_claude = 0
    n_skip_existing = 0
    for (rid, cid, idx, s, e, shot, angle, ft, by) in rows:
        if by == "claude":
            n_skip_claude += 1
            continue
        if ft and not args.force:
            n_skip_existing += 1
            continue

        info = clip_info.get(cid, {})
        sector = detect_sector(info.get("rel_path") or "", args.project_prefix)
        hour = hour_band(info.get("creation_time") or "")
        cat = info.get("category") or ""
        camera = cameras.role_of(info.get("filename"), info.get("camera_model"),
                                 info.get("camera_make"), info.get("ext"),
                                 registry=reg)

        # Transcript
        if cid not in transcripts:
            tp = tr_dir / f"{cid}.json"
            if tp.exists():
                try:
                    with tp.open() as f:
                        tr = json.load(f)
                    transcripts[cid] = tr.get("words", [])
                except Exception:
                    transcripts[cid] = []
            else:
                transcripts[cid] = []
        words = transcripts[cid]
        snippet, distinct = extract_transcript_window(words, s, e)

        # Personajes evidentes en el transcript del tramo
        chars_list = characters_from_text(snippet, cast)
        chars_str = ", ".join(chars_list)

        # Sujeto: si hay personajes mencionados, usarlos. Si no, fallback por
        # categoría / cámara.
        if chars_str:
            subject = chars_str
        elif cat:
            subject = CATEGORY_ACTION.get(cat, "Escena")
        else:
            subject = DEFAULT_SUBJECT.get(camera, "Escena")

        # Plano + ángulo (por tramo)
        plano = (shot + " + " + angle) if shot else (angle or "Normal")

        # Acción inferida + snippet
        action_bits = []
        if cat == "timelapse":
            action_bits.append("Variación de luz / movimiento de sol")
        elif distinct >= 5:
            action_bits.append(f'Diálogo: "{snippet}"')
        elif cat in ("accion-dialogo", "accion"):
            action_bits.append("Pegue de escalada activa")
        elif cat == "b-roll":
            action_bits.append("B-roll de la escena")
        elif cat == "pov-static":
            action_bits.append("POV en cámara fija")

        loc_bits = []
        if sector:
            loc_bits.append(sector)
        if hour:
            loc_bits.append(hour)
        if loc_bits:
            action_bits.append("(" + ", ".join(loc_bits) + ")")

        accion = ". ".join(action_bits) if action_bits else "Sin contenido detectable"

        full_text = f"{subject} | {plano} | {accion}"

        # Persist
        conn.execute(
            "UPDATE clip_curated_segments SET full_text=?, characters=? WHERE id=?",
            (full_text, chars_str, rid)
        )
        n_updated += 1

    conn.commit()
    conn.close()
    print(f"Tramos: {n_total} totales.")
    print(f"  Actualizados con full_text auto: {n_updated}")
    print(f"  Skip (curated_by='claude' preservado): {n_skip_claude}")
    print(f"  Skip (ya tenía full_text, sin --force): {n_skip_existing}")


if __name__ == "__main__":
    main()
