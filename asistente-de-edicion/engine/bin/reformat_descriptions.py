#!/usr/bin/env python3
"""Re-formatear las descripciones de clip_descriptions al esquema estándar:

    [SUJETO] | [PLANO] | [ACCIÓN/CONTEXTO]

- SUJETO: personajes detectados en el clip (ESCALADOR_A, ESCALADORA_B, …), o el sujeto
  inferido si no hay personajes (paisaje, cueva, equipo, grupo).
- PLANO: nombre completo del shot_value en español (Plano Medio, Plano
  General, Plano Detalle…).
- ACCIÓN: extracto condensado de la descripción actual; en entrevistas
  empieza con `Pregunta: "…" / Respuesta: "…"`.

El script reescribe el campo `description` in-place. Conserva la categoría.

Override JSON opcional: { "clip_id": {"subject":"...","action":"..."} }.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cast as cast_lib  # noqa: E402
from lib import manifest  # noqa: E402


SHOT_NAMES = {
    "GPG": "Gran Plano General",
    "PG":  "Plano General",
    "PE":  "Plano Entero",
    "PA":  "Plano Americano",
    "PM":  "Plano Medio",
    "PP":  "Primer Plano",
    "PPP": "Primerísimo Primer Plano",
    "PD":  "Plano Detalle",
}

# Mapeo categoría → sujeto fallback (cuando no hay personajes en el clip).
CATEGORY_SUBJECT = {
    "entrevista":     "Entrevistado",
    "charla-equipo":  "Equipo",
    "accion-dialogo": "Escalador",
    "accion":         "Escalador",
    "b-roll":         "Escena",
    "timelapse":      "Paisaje",
    "pov-static":     "POV cámara",
    "scenic":         "Paisaje",
    "portrait":       "Sujeto",
    "b-roll-gear":    "Equipo",
    "discard":        "Descarte",
    "export":         "Export del documental",
}

# Los entrevistadores y el protagonista son DATOS DEL PROYECTO: viven en
# <disco>/.cinema_assistant/cast.json y se cargan en main() con lib/cast.py.
# Estuvieron aqui como nombres reales hardcodeados; el saneo de la auditoria
# 2026-07-31 los sustituyo por seudonimos y el filtro dejo de casar con lo que
# hay en el manifest, en silencio. Es el mismo caso que el CAST.

# Pistas de sujeto en la descripción actual.
SUBJECT_KEYWORDS = [
    ("cueva",       "Cueva (sector)"),
    ("paisaje",     "Paisaje"),
    ("peñasco",     "Peñasco"),
    ("boulder",     "Boulder"),
    ("campamento",  "Campamento"),
    ("topo",        "Lectura del topo"),
    ("drone",       "Toma aérea"),
    ("aérea",       "Toma aérea"),
    ("cuerda",      "Equipo (cuerda)"),
    ("gear",        "Equipo (gear)"),
    ("crashpad",    "Bouldering setup"),
    ("tienda",      "Campamento"),
    ("desayuno",    "Campamento (desayuno)"),
    ("rocoso",      "Paisaje rocoso"),
]


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def infer_subject(characters: str, category: str, current_desc: str,
                  interviewers: set[str], protagonist: str) -> str:
    """Build SUJETO field for the new format."""
    names = []
    if characters:
        for token in characters.split(","):
            token = token.strip()
            if not token:
                continue
            name = re.sub(r"\s*\(\d+\)$", "", token)
            names.append(name)

    if category == "entrevista":
        # En entrevistas: el sujeto en cuadro es el ENTREVISTADO, no la
        # entrevistadora. Filtrar nombres del whitelist de interviewers.
        names = [n for n in names if n not in interviewers]
        if names:
            return ", ".join(names)
        # Sin protagonista declarado se devuelve vacio: NO se inventa un
        # nombre, porque este valor se escribe en clip_descriptions.
        return protagonist

    if names:
        return ", ".join(names)

    # Try keywords in the description for non-interview clips
    desc_low = (current_desc or "").lower()
    for kw, subject in SUBJECT_KEYWORDS:
        if kw in desc_low:
            return subject
    return CATEGORY_SUBJECT.get(category, "Escena")


_SHOT_NAMES_LIST = list(SHOT_NAMES.values()) + ["(plano sin asignar)"]


def _strip_existing_format(desc: str) -> str:
    """Si la descripción ya está en formato `SUJETO | PLANO | ACCION`,
    devuelve solo el componente ACCION. Se aplica RECURSIVAMENTE: si la
    propia ACCION también está formateada (por bug previo de duplicación),
    se sigue pelando hasta no encontrar prefijo. Idempotente."""
    current = desc or ""
    for _ in range(5):  # safety cap on recursion
        if current.count(" | ") < 2:
            return current
        parts = current.split(" | ", 2)
        if len(parts) == 3 and parts[1].strip() in _SHOT_NAMES_LIST:
            current = parts[2]
            continue
        return current
    return current


def clean_action(desc: str) -> str:
    """Condense the action text: remove leading stars, dates, durations.
    Trim to ~140 chars at word boundary. Idempotente."""
    if not desc:
        return ""
    # Si ya está en formato SUJETO|PLANO|ACCION, agarrar solo la acción.
    s = _strip_existing_format(desc).strip()
    # Drop ★ markers
    s = re.sub(r"[★]{1,3}\s*", "", s).strip()
    # Drop leading 'EXPORT:', 'DISCARD:', 'ENTREVISTA …:' tags
    s = re.sub(r"^(EXPORT|DISCARD|ENTREVISTA[^:]*|DRONE):\s*", "", s, flags=re.IGNORECASE)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 140:
        cut = s[:140]
        last = cut.rfind(" ")
        if last > 60:
            cut = cut[:last]
        s = cut + "…"
    return s


def format_interview_action(question: str, response: str) -> str:
    """For interviews: 'Pregunta: "…" / Respuesta: "…"'."""
    q = (question or "").strip().rstrip("?")
    r = (response or "").strip()
    # Trim response to ~100 chars at sentence/word boundary
    if len(r) > 100:
        # Cut at first period after 50 chars
        m = re.search(r"[.!?]\s", r[50:])
        if m:
            r = r[: 50 + m.start() + 1]
        else:
            r = r[:100].rsplit(" ", 1)[0] + "…"
    return f'Pregunta: "¿{q}?" / Respuesta: "{r}"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--overrides", help="JSON con {clip_id: {subject, action}}")
    args = ap.parse_args()

    root = resolve_root(args.root)
    interviewers = cast_lib.load_interviewers(root)
    protagonist = cast_lib.load_protagonist(root)
    if not interviewers:
        print('  aviso: sin "interviewers" en cast.json — no se excluye a nadie del SUJETO',
              file=sys.stderr)
    if not protagonist:
        print('  aviso: sin "protagonist" en cast.json — las entrevistas sin personaje\n         detectado quedan con sujeto vacio (antes se asumia el protagonista)',
              file=sys.stderr)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    overrides = {}
    if args.overrides:
        with open(args.overrides) as f:
            raw = json.load(f)
        overrides = {int(k): v for k, v in raw.items()}

    conn = manifest.conectar(str(db))

    # Para entrevistas, la PREGUNTA RELEVANTE = la que tiene la respuesta
    # más sustantiva (mayor tiempo entre la pregunta y la siguiente). Se
    # filtra el setup-chatter (≤ 15s) que casi nunca es pregunta real.
    qrows = conn.execute("""
        SELECT clip_id, question_text, response_summary, (end_sec - start_sec) AS resp_dur
        FROM question_segments
        WHERE (end_sec - start_sec) > 15
        ORDER BY clip_id, resp_dur DESC
    """).fetchall()
    best_q = {}
    for clip_id, qtext, rtext, dur in qrows:
        if clip_id not in best_q:  # first row per clip = longest response
            best_q[clip_id] = (qtext, rtext)

    # Solo VIDEO clips. Las descripciones de audios externos las maneja
    # derive_audio_segments.py con su propio formato "Sujeto | Audio externo | ACCION".
    rows = conn.execute("""
        SELECT d.clip_id, d.category, d.description,
               IFNULL(cc.characters, ''), IFNULL(sv.shot_value, '')
        FROM clip_descriptions d
        JOIN clips c ON c.id = d.clip_id
        LEFT JOIN clip_characters cc ON cc.clip_id = d.clip_id
        LEFT JOIN clip_shot_values sv ON sv.clip_id = d.clip_id
        WHERE c.file_kind = 'video'
    """).fetchall()

    now = time.time()
    n_updated = 0
    for clip_id, cat, old_desc, chars, sv in rows:
        ov = overrides.get(clip_id, {})
        subject = ov.get("subject") or infer_subject(chars, cat, old_desc,
                                                     interviewers, protagonist)
        shot_name = SHOT_NAMES.get(sv, sv) if sv else "(plano sin asignar)"

        if cat == "entrevista" and clip_id in best_q:
            qtext, rtext = best_q[clip_id]
            action = ov.get("action") or format_interview_action(qtext, rtext)
        else:
            action = ov.get("action") or clean_action(old_desc)

        new_desc = f"{subject} | {shot_name} | {action}"

        conn.execute(
            "UPDATE clip_descriptions SET description = ?, updated_at = ? WHERE clip_id = ?",
            (new_desc, now, clip_id)
        )
        n_updated += 1

    conn.commit()
    conn.close()
    print(f"Reformateadas {n_updated} descripciones al esquema SUJETO | PLANO | ACCIÓN.")


if __name__ == "__main__":
    main()
