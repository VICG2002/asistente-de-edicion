#!/usr/bin/env python3
"""Para cada tramo curado, envía sus 5 frames + datos auxiliares (YOLO,
pose, transcript) a un Vision LLM local vía Ollama y obtiene la
descripción real respondiendo las 8 preguntas estándar.

Ollama debe estar corriendo en localhost:11434 con un modelo vision como:
  - qwen2.5vl:7b    (recomendado, balance calidad/velocidad)
  - minicpm-v:8b
  - llava:13b
  - llama3.2-vision:11b

Levanta Ollama con:  `ollama serve` (background) + `ollama pull qwen2.5vl:7b`.

Output: actualiza `clip_curated_segments` con:
  characters, what_action, what_stands, where_at, objects, dialogue_idea,
  full_text (compuesto en formato SUJETO | PLANO + ÁNGULO | ACCIÓN).

Skip tramos con `curated_by='claude'` (ya escritos a mano).
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cast as cast_lib  # noqa: E402
from urllib import request as urlreq
from urllib.error import URLError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras  # noqa: E402
from lib import manifest  # noqa: E402


OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5vl:3b"  # 3b corre bien en M-series, suficiente calidad.
# Para mas calidad y suficiente RAM: `ollama pull qwen2.5vl:7b` y pasar
# --model qwen2.5vl:7b en la llamada.

# El cast NO vive aqui. Vivia: era la whitelist de personas reales del rodaje de
# JILOTEPEC, escrita dentro del motor. Y estaba DUPLICADA en cinco archivos
# (enrich_curated_segments, derive_characters, clean_character_identity,
# describe_segments_local_llm, attribute_faces_via_transcript), que para el
# 2026-07-31 ya habian divergido: esta copia tenia 20 nombres y las otras 25, asi
# que siete personas nunca se detectaban aqui y si en el resto. Mismo patron que
# `classify_camera` escrita cinco veces.
#
# Ahora sale de `<disco>/.cinema_assistant/cast.json` via lib/cast.py. Una fuente,
# por proyecto, fuera del codigo que se publica.


PROMPT_PERSONAS = """Eres un asistente de edición de cine documental. Analiza los frames de
un tramo con PERSONAS en cuadro. Describe SOLO lo que ves.

REGLA CRÍTICA: SOLO usa nombres propios si están en IDENTIDADES CONFIRMADAS
abajo. Para personas en cuadro sin identidad confirmada, descríbelas
visualmente: "hombre con barba", "mujer con audífonos". NO inventes nombres.

IDENTIDADES CONFIRMADAS en este tramo (audio + face_recognition validados,
PUEDES usar estos nombres si reconoces a esas personas):
{confirmed_identities}

DATOS TÉCNICOS:
- Plano declarado: {shot_value} ({shot_name})
- Ángulo declarado: {angle}
- Duración del tramo: {tramo_dur:.0f}s
- Tipo de cámara: {camera}

TRANSCRIPT del tramo:
{transcript_snippet}

OBJETOS DETECTADOS POR YOLO (pueden tener falsos positivos):
{yolo_objects}

REGLA: NO describas la ropa, colores de vestimenta ni accesorios cosméticos.
Importa QUIÉN aparece (nombre confirmado o descripción mínima) y QUÉ HACE.

Responde EN JSON, máximo 1-2 oraciones por campo, español de México:

{{
  "what_action":   "Qué hacen las personas (verbos concretos, gestual, posición corporal)",
  "characters":    "Si hay identidad confirmada: usa el nombre. Si no: descripción mínima sin ropa: 'hombre adulto', 'mujer joven', 'grupo de 3 personas'",
  "what_stands":   "Qué resalta visualmente (gesto, expresión, mirada, posición corporal — NO ropa)",
  "where_at":      "Dónde están (calle, plaza, explanada, interior, terreno natural — lo que se vea)",
  "objects":       "Objetos funcionales / herramientas / instrumentos / animales (no incluyas ropa)",
  "dialogue_idea": "Si el transcript tiene contenido: idea principal. Si no, ''.",
  "shot_value_observed":  "Confirma o corrige (GPG/PG/PE/PA/PM/PP/PPP/PD)",
  "angle_observed":       "Confirma o corrige (Normal/Picado/Contrapicado/Cenital/Nadir/Holandés/Escorzo/POV)"
}}

Responde SOLO el JSON.
"""


PROMPT_PAISAJE = """Eres un asistente de edición de cine documental. Analiza los frames de
un tramo SIN PERSONAS en cuadro — es un paisaje, toma establecedora,
timelapse, vista del terreno o transición.

NO INVENTES PERSONAS. Si no ves personas, describe el paisaje, la
composición, la luz, el movimiento (nubes, agua, cámara), elementos
naturales o construidos.

IDENTIDADES CONFIRMADAS asociadas a este clip (referencia contextual):
{confirmed_identities}

DATOS TÉCNICOS:
- Plano declarado: {shot_value} ({shot_name})
- Ángulo declarado: {angle}
- Duración del tramo: {tramo_dur:.0f}s
- Tipo de cámara: {camera}

TRANSCRIPT (audio ambiente o voz fuera de cuadro):
{transcript_snippet}

OBJETOS DETECTADOS POR YOLO:
{yolo_objects}

Responde EN JSON, español de México, conciso:

{{
  "what_action":   "Qué cambia (movimiento de nubes/luz/viento/agua/cámara). Si nada cambia, describe el estado fijo.",
  "characters":    "DEJA VACÍO o 'sin personas en cuadro'. NO INVENTES PERSONAS.",
  "what_stands":   "Elemento dominante de la composición (silueta, arquitectura, color del cielo, contraste, vegetación)",
  "where_at":      "Tipo de lugar (paisaje urbano, calle, explanada, bosque, valle, cielo). Indicios geográficos.",
  "objects":       "Elementos del encuadre (vegetación, agua, nubes, vehículos, edificios, mobiliario)",
  "dialogue_idea": "Si hay voz / conversación lejana en el transcript: idea principal. Si no, ''.",
  "shot_value_observed":  "Confirma o corrige (GPG/PG/PE/PA/PM/PP/PPP/PD)",
  "angle_observed":       "Confirma o corrige (Normal/Picado/Contrapicado/Cenital/Nadir/Holandés/Escorzo/POV)"
}}

Responde SOLO el JSON.
"""


PROMPT_DETALLE = """Eres un asistente de edición de cine documental. Analiza los frames de
un tramo en PLANO DETALLE — un objeto, parte del cuerpo, equipo, textura
o elemento específico en primer plano.

Concéntrate en describir EL OBJETO/DETALLE como protagonista. Si hay
persona, es sólo parcialmente visible (manos, pies, parte del rostro).

IDENTIDADES CONFIRMADAS asociadas a este clip (referencia contextual):
{confirmed_identities}

DATOS TÉCNICOS:
- Plano declarado: {shot_value} ({shot_name})
- Ángulo declarado: {angle}
- Duración del tramo: {tramo_dur:.0f}s
- Tipo de cámara: {camera}

TRANSCRIPT:
{transcript_snippet}

OBJETOS DETECTADOS POR YOLO:
{yolo_objects}

REGLA: NO describas la ropa ni accesorios cosméticos del cuerpo
asociado. Enfócate en el objeto, la acción y el contexto funcional.

Responde EN JSON, español de México, observacional:

{{
  "what_action":   "Movimiento/gesto del objeto o de la parte del cuerpo (mano sujetando algo, dedo señalando, objeto siendo manipulado)",
  "characters":    "Si hay parte de persona: 'mano', 'pie', 'rostro parcial'. Sin describir ropa. Si solo objeto: ''.",
  "what_stands":   "Qué llama la atención del objeto: textura, forma, deformación, función",
  "where_at":      "Contexto inferido del marco (en mesa, en mano, en suelo, contra cielo)",
  "objects":       "EL objeto principal + funcionales que lo acompañan",
  "dialogue_idea": "Si hay diálogo asociado en el transcript: idea. Si no, ''.",
  "shot_value_observed":  "Confirma o corrige (GPG/PG/PE/PA/PM/PP/PPP/PD)",
  "angle_observed":       "Confirma o corrige (Normal/Picado/Contrapicado/Cenital/Nadir/Holandés/Escorzo/POV)"
}}

Responde SOLO el JSON.
"""


def pick_prompt(yolo_str: str, shot_value: str, category: str) -> str:
    """Heurística para elegir el prompt apropiado:
    - PD → PROMPT_DETALLE
    - timelapse → PROMPT_PAISAJE
    - YOLO corrió y NO vio personas → PROMPT_PAISAJE
    - SIN datos YOLO (tabla vacía) → PROMPT_PERSONAS (caso MAB 2026-06-11:
      asumir paisaje sin evidencia ruteó 218 tramos callejeros al prompt de
      paisaje y qwen describió nubes en un rodaje de vox pops)
    - Default → PROMPT_PERSONAS
    """
    if shot_value == "PD":
        return PROMPT_DETALLE
    if category == "timelapse":
        return PROMPT_PAISAJE
    if yolo_str is None or not yolo_str.strip():
        return PROMPT_PERSONAS  # sin datos no se asume ausencia de personas
    if "person" not in yolo_str.lower():
        return PROMPT_PAISAJE
    return PROMPT_PERSONAS


SHOT_NAMES = {
    "GPG": "Gran Plano General", "PG": "Plano General", "PE": "Plano Entero",
    "PA": "Plano Americano", "PM": "Plano Medio", "PP": "Primer Plano",
    "PPP": "Primerísimo Primer Plano", "PD": "Plano Detalle",
}


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


# detect_sector() se borro el 2026-08-05. Estaba definida y NO la llamaba nadie
# —codigo muerto—, pero cargaba deuda en el linter porque parseaba el sector con
# `re.match(r"ESCALANDO MEXICO/([^/]+)")`, que en cualquier otro proyecto
# devuelve cadena vacia. Si vuelve a hacer falta el sector aqui, es
# `lib.proyecto.sector_de(rel_path, prefix)`.


def describe_camera(filename: str, model: str, make=None, ext=None, reg=None) -> str:
    """Etiqueta legible de la camara para el prompt del LLM.

    Sale del registro (`config/camera_profiles.json`). La version anterior tenia
    su propia lista y devolvia "otra" para FX30 y a6700 — es decir, el LLM
    describia el material principal del proyecto sin saber con que se filmo.
    """
    p = cameras.classify(filename, model, make, ext, registry=reg)
    if p.id == (reg.fallback.id if reg else "otro"):
        return "camara no catalogada"
    return f"{p.label} ({p.role_label})"


def extract_transcript_window(words, s, e, max_chars=250):
    in_range = [w for w, t in words if s <= t <= e]
    if not in_range:
        return ""
    text = " ".join(in_range)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


def encode_image(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode("ascii")


def call_ollama(model: str, prompt: str, image_paths: list[Path],
                timeout: int = 240) -> str | None:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [encode_image(p) for p in image_paths],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 700},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        OLLAMA_URL, data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        return body.get("response", "")
    except URLError as e:
        print(f"  ⚠ Ollama error: {e}")
        return None
    except Exception as e:
        print(f"  ⚠ Error: {e}")
        return None


def parse_json_response(text: str) -> dict | None:
    """Extrae el primer bloque {...} del texto."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        try:
            cleaned = re.sub(r",\s*}", "}", m.group(0))
            cleaned = re.sub(r",\s*]", "]", cleaned)
            return json.loads(cleaned)
        except Exception:
            return None


def compose_full_text(seg, ans):
    chars = ans.get("characters") or seg.get("characters") or "Escena"
    shot = ans.get("shot_value_observed") or seg.get("shot_value") or ""
    angle = ans.get("angle_observed") or seg.get("angle") or "Normal"
    plano = f"{shot} + {angle}" if shot else angle
    action_bits = []
    if ans.get("what_action"):
        action_bits.append(ans["what_action"])
    if ans.get("where_at"):
        action_bits.append(ans["where_at"])
    if ans.get("objects"):
        action_bits.append("Objetos: " + ans["objects"])
    if ans.get("dialogue_idea"):
        action_bits.append('Idea: "' + ans["dialogue_idea"] + '"')
    accion = ". ".join(b for b in action_bits if b)
    return f"{chars} | {plano} | {accion}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-segs", type=int, default=0)
    ap.add_argument("--seg-ids", default=None,
                    help="csv de seg_ids específicos a procesar")
    ap.add_argument("--skip-claude", action="store_true", default=True)
    ap.add_argument("--skip-already-described", action="store_true", default=True,
                    help="skip tramos con curated_by='llm' (ya descritos en un run anterior)")
    ap.add_argument("--max-frames-per-seg", type=int, default=0,
                    help="0 = todos los disponibles. >0 = limita a N frames (toma el del medio + extremos)")
    ap.add_argument("--priority-categories", default="",
                    help="csv de categorías a procesar (entrevista,accion-dialogo,charla-equipo,b-roll). Vacío = todas.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    cast_aliases = cast_lib.load_cast(root)
    if not cast_aliases:
        print(cast_lib.aviso_sin_cast(root, "describe_segments_local_llm"), file=sys.stderr)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    frames_dir = root / ".cinema_assistant" / "seg_frames"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))

    sql = """
        SELECT ccs.id, ccs.clip_id, ccs.seg_index, ccs.start_sec, ccs.end_sec,
               ccs.shot_value, ccs.angle, ccs.curated_by,
               c.filename, c.camera_model, c.rel_path,
               IFNULL(d.category, ''), c.camera_make, c.ext
        FROM clip_curated_segments ccs
        JOIN clips c ON c.id = ccs.clip_id
        LEFT JOIN clip_descriptions d ON d.clip_id = ccs.clip_id
    """
    rows = conn.execute(sql).fetchall()

    if args.seg_ids:
        ids = set(int(x) for x in args.seg_ids.split(",") if x.strip())
        rows = [r for r in rows if r[0] in ids]
    else:
        # filtro por categorías prioritarias
        if args.priority_categories:
            pri = set(c.strip() for c in args.priority_categories.split(",") if c.strip())
            rows = [r for r in rows if r[11] in pri]
        # skip ya descritos por LLM (incremental)
        if args.skip_already_described:
            rows = [r for r in rows if r[7] != "llm"]
        if args.max_segs > 0:
            rows = rows[: args.max_segs]
    print(f"Tramos a procesar: {len(rows)}")

    # cache transcripts
    transcripts = {}
    n_ok = 0
    n_skip = 0
    n_fail = 0
    t0 = time.time()
    reg = cameras.load_profiles(root)
    for (seg_id, cid, idx, s, e, shot, angle, by,
         fn, cm, rel_path, cat, cam_make, cam_ext) in rows:
        if args.skip_claude and by == "claude":
            n_skip += 1
            continue

        seg_dir = frames_dir / str(seg_id)
        # Filtrar macOS xattr files (`._*.jpg`) que NO son JPGs reales y
        # rompen el endpoint con HTTP 500.
        frame_files = sorted(
            f for f in seg_dir.glob("*.jpg") if not f.name.startswith("._")
        )
        if not frame_files:
            n_fail += 1
            continue
        # Optimización: limitar frames si --max-frames-per-seg > 0
        if args.max_frames_per_seg > 0 and len(frame_files) > args.max_frames_per_seg:
            if args.max_frames_per_seg == 1:
                # solo el del medio
                frame_files = [frame_files[len(frame_files) // 2]]
            else:
                # extremos + medio (siempre incluye el primero y último)
                step = (len(frame_files) - 1) / (args.max_frames_per_seg - 1)
                idxs = sorted(set(int(i * step) for i in range(args.max_frames_per_seg)))
                frame_files = [frame_files[i] for i in idxs]

        # transcript window
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
        snippet = extract_transcript_window(transcripts[cid], s, e)
        if not snippet:
            snippet = "(sin habla detectada en este tramo)"

        # yolo objects summary (opcional — tabla ausente si YOLO no corrió,
        # p.ej. proyecto fresco FCC 2026-07-09; el prompt ya tiene fallback)
        try:
            yolo_rows = conn.execute(
                "SELECT label, COUNT(*) FROM segment_objects WHERE seg_id=? GROUP BY label ORDER BY 2 DESC LIMIT 12",
                (seg_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            yolo_rows = []
        # vacio = YOLO no corrió (pick_prompt no debe asumir "sin personas");
        # el placeholder visible para el prompt se aplica al formatear.
        yolo_str = ", ".join(f"{lbl} ({n})" for lbl, n in yolo_rows)

        # pose classes (opcional — si la tabla no existe, el LLM ve los frames igual)
        try:
            pose_rows = conn.execute(
                "SELECT frame_pct, pose_class FROM segment_poses WHERE seg_id=? ORDER BY frame_pct",
                (seg_id,)
            ).fetchall()
            pose_str = ", ".join(f"{p}%→{c}" for p, c in pose_rows) or "(sin pose)"
        except sqlite3.OperationalError:
            pose_str = "(no analizado)"

        # v15/v16: identidades CONFIRMADAS para el tramo desde 3 fuentes:
        #   1. face_attributions + face_identities (visión + audio cruzados)
        #   2. Nombres del CAST mencionados en el transcript del tramo
        #   3. clip_characters (poblado por scripts upstream con whitelist
        #      del proyecto — caso Zezzions 2026-05-26)
        confirmed_set = set()
        try:
            # Opcional: Fase 2 (InsightFace) puede no haber corrido aún en
            # un proyecto fresco (FCC 2026-07-09) — el LLM ve los frames.
            for (name,) in conn.execute("""
                SELECT DISTINCT fc.canonical_name
                FROM face_detections fd
                JOIN face_identities fi ON fi.detection_id = fd.id
                JOIN face_catalog fc ON fc.identity_id = fi.identity_id
                WHERE fd.clip_id = ?
                  AND fd.frame_t_sec BETWEEN ? AND ?
            """, (cid, s, e)):
                if name:
                    confirmed_set.add(name)
        except sqlite3.OperationalError:
            pass
        # Añadir nombres detectados en el transcript del tramo
        for w, t in transcripts.get(cid, []):
            if s <= t <= e:
                nw = normalize(w)
                for name, aliases in cast_aliases.items():
                    if nw in aliases:
                        confirmed_set.add(name)
        # Fuente 3: clip_characters. Formato "Nombre*(N), Otro(M)".
        # El * indica auto-identificación (peso alto). Lo aceptamos como
        # identidad CONFIRMADA en el clip; el LLM puede usarlo si reconoce
        # visualmente la persona.
        try:
            cc_row = conn.execute(
                "SELECT characters FROM clip_characters WHERE clip_id=?", (cid,)
            ).fetchone()
        except sqlite3.OperationalError:
            cc_row = None
        if cc_row and cc_row[0]:
            import re as _re
            for m in _re.finditer(r'([A-ZÁÉÍÓÚÑ][^,()*]+?)[\*\(]', cc_row[0]):
                nm = m.group(1).strip()
                if nm:
                    confirmed_set.add(nm)
        confirmed_str = ", ".join(sorted(confirmed_set)) if confirmed_set else "(ninguna confirmada — describe visualmente sin nombres)"

        camera = describe_camera(fn, cm, cam_make, cam_ext, reg)
        # v14: prompt adaptativo según tipo de tramo (persona/paisaje/detalle).
        prompt_tpl = pick_prompt(yolo_str, shot or "", cat or "")
        prompt = prompt_tpl.format(
            shot_value=shot or "?",
            shot_name=SHOT_NAMES.get(shot, ""),
            angle=angle or "Normal",
            tramo_dur=e - s,
            camera=camera,
            transcript_snippet=snippet,
            yolo_objects=yolo_str or "(sin datos YOLO — describe lo que veas)",
            confirmed_identities=confirmed_str,
        )

        response = call_ollama(args.model, prompt, frame_files)
        if not response:
            n_fail += 1
            continue
        ans = parse_json_response(response)
        if not ans:
            print(f"  ⚠ seg {seg_id}: respuesta sin JSON parseable")
            n_fail += 1
            continue

        full = compose_full_text({
            "characters": "", "shot_value": shot, "angle": angle
        }, ans)
        conn.execute(
            "UPDATE clip_curated_segments SET full_text=?, characters=?, "
            "what_action=?, what_stands=?, where_at=?, objects=?, dialogue_idea=?, "
            "curated_by='llm', created_at=? WHERE id=?",
            (full, ans.get("characters", "") or "",
             ans.get("what_action", "") or "",
             ans.get("what_stands", "") or "",
             ans.get("where_at", "") or "",
             ans.get("objects", "") or "",
             ans.get("dialogue_idea", "") or "",
             time.time(), seg_id)
        )
        conn.commit()
        n_ok += 1
        if n_ok % 10 == 0:
            elapsed = time.time() - t0
            rate = n_ok / elapsed if elapsed > 0 else 0
            print(f"  [{n_ok}] tramos descritos | {rate:.2f} tramos/s | eta {(len(rows)-n_ok-n_skip)/max(rate,0.01):.0f}s")

    conn.close()
    print(f"\nDone. ok={n_ok}  skip={n_skip}  fail={n_fail}")


if __name__ == "__main__":
    main()
