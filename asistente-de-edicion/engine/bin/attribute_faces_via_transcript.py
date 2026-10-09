#!/usr/bin/env python3
"""Atribución multi-señal de caras a identidades usando el AUDIO como ground
truth. Aprovecha que el transcript "sabe" quién habla en cada momento,
algo que InsightFace por sí solo no puede determinar.

3 señales combinadas:

  1. RESPUESTA-EN-ENTREVISTA: en clips de category='entrevista', las
     caras detectadas durante un tramo de respuesta (entre Qn y Qn+1)
     se atribuyen al entrevistado configurado (--interviewee, default
     'ESCALADOR_A' para JILOTEPEC).

  2. MENCIÓN-DE-NOMBRE: si el transcript menciona un nombre del cast
     en t±5s y hay una cara detectada en ese rango, vota por esa
     identidad. Filtra menciones obviamente del entrevistador
     (ej. "ESCALADOR_A" dicho como pregunta al inicio).

  3. CLUSTER-AUTO-LABEL: después de acumular votos por
     (face_detection_id, identity), agrupar por cluster_id de
     face_catalog y, si ≥ THRESH del cluster vota por un nombre,
     confirmar ese cluster con esa identidad.

Tabla nueva: `face_attributions(detection_id, identity_name, weight, method)`.
Refuerza `face_identities` con mayor confianza, y actualiza
`clip_characters` con identidades CONFIRMADAS por audio + visión.
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
from collections import Counter, defaultdict
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


# NO hay cast por defecto. Lo hubo: JILOTEPEC_FALLBACK_CAST, con los 25 nombres
# reales de ese rodaje. Se quito el 2026-07-31. Era lo peor de los cinco casos:
# cuando el descubrimiento fallaba en OTRO proyecto, este script atribuia las
# caras a personas de un rodaje ajeno — no dejaba el trabajo sin hacer, lo hacia
# MAL y guardaba nombres falsos en el manifest. Sin cast, cast vacio y aviso.

# CAST y INTERVIEWERS se rellenan en main() — defaults vacíos para no
# romper imports.
CAST: dict[str, set[str]] = {}
INTERVIEWERS: set[str] = set()

NAME_WINDOW_SEC = 5.0                   # Ventana ±5s alrededor de una mención
CLUSTER_LABEL_THRESHOLD = 0.40          # ≥40% del cluster vota por un nombre
                                        # (bajado de 0.55 en 2026-05-26 para
                                        # capturar más identidades en proyectos
                                        # con menos question_segments).
CLUSTER_MIN_DETECTIONS = 2              # Mínimo de detecciones para confirmar
                                        # (bajado de 3 a 2 por el mismo motivo).


# ── Auto-discovery de cast del proyecto ──────────────────────────────────

# Patrones de auto-ID en transcripts (en español):
SELF_ID_PATTERNS = [
    re.compile(r"\b[Yy]o\s+soy\s+([A-ZÁ-Úa-zá-ú]+(?:\s+[A-ZÁ-Úa-zá-ú]+)?)", re.UNICODE),
    re.compile(r"\b[Mm]i\s+nombre\s+(?:es|artístico\s+es)\s+([A-ZÁ-Úa-zá-ú]+(?:\s+[A-ZÁ-Úa-zá-ú]+)?)", re.UNICODE),
    re.compile(r"\b[Mm]e\s+llamo\s+([A-ZÁ-Úa-zá-ú]+(?:\s+[A-ZÁ-Úa-zá-ú]+)?)", re.UNICODE),
    re.compile(r"\b[Hh]ola[,.]?\s+soy\s+([A-ZÁ-Úa-zá-ú]+(?:\s+[A-ZÁ-Úa-zá-ú]+)?)", re.UNICODE),
    re.compile(r"\b[Ss]oy\s+hermano\s+de\s+([A-ZÁ-Úa-zá-ú]+)", re.UNICODE),
    re.compile(r"\b[Mm]i\s+proyecto\s+(?:es|se\s+llama)\s+([A-ZÁ-Úa-zá-ú]+(?:\s+[A-ZÁ-Úa-zá-ú]+)?)", re.UNICODE),
]

# Stopwords obvias que NO son nombres aunque matcheen el patrón
NON_NAME_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "este", "esta", "ese", "esa", "aquel", "aquella",
    "muy", "más", "mas", "menos", "tan", "tanto",
    "que", "quien", "como", "donde", "cuando",
    "yo", "tu", "el", "ella", "nosotros", "ustedes", "ellos",
    "ok", "ah", "eh", "uh", "mm", "si", "no",
    "para", "por", "con", "sin", "de", "en", "a",
    "muchas", "gracias", "hola", "adios",
    "soy", "es", "son", "hermano", "papá", "mamá",
    "diversos", "varios", "muchos", "algunos",
    # Auto-ID válidos que dependen del contexto:
    "uno", "alguien", "nadie",
    # Falsos positivos observados en Zezzions (palabras coloquiales que
    # Whisper a veces capitaliza como si fueran nombres):
    "poco", "cuaco", "bernie", "vic", "mucho", "todo", "algo", "nada",
    "siempre", "nunca", "casi", "tal", "vez", "quizás", "quizas",
    "guey", "güey", "neta", "onda", "chido", "bien", "mal",
    "luego", "después", "antes", "ahora", "después",
    # Verbos comunes capitalizados (Whisper a veces capitaliza al inicio
    # de oración palabras que matchen patrones):
    "vamos", "estamos", "vengo", "vine", "fuiste", "llevo",
    "tengo", "tenemos", "tienen", "hago", "hacemos",
}


def _normalize_name(raw: str) -> str | None:
    """Limpia un candidato de nombre. Retorna 'Forma_Title' o None si inválido."""
    if not raw:
        return None
    parts = raw.strip().split()
    # Filtrar partes que son stopwords
    parts = [p for p in parts if p.lower() not in NON_NAME_STOPWORDS]
    if not parts:
        return None
    # Cada parte debe empezar con mayúscula (heurística para nombre propio)
    # — pero como Whisper a veces lowercase, también aceptar 1ra letra match con cualquier carácter.
    if any(len(p) < 2 for p in parts):
        return None
    cap_parts = [p.capitalize() for p in parts]
    return " ".join(cap_parts)


def discover_cast_from_clip_characters(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Lee `clip_characters.characters` ya curado por Claude/usuario.

    Formato típico (Zezzions):
      "ENTREVISTADO_5* (DJ, analista de datos)"
      "ENTREVISTADO_13 (rapero/productor) y ENTREVISTADO_10 (rapero/productor)"
      "ENTREVISTADO_9* (acordeonista)"
    """
    out: dict[str, set[str]] = {}
    rows = conn.execute("SELECT characters FROM clip_characters WHERE characters IS NOT NULL").fetchall()
    for (chars,) in rows:
        if not chars:
            continue
        # Quitar paréntesis con contexto (es ruido para names)
        clean = re.sub(r"\([^)]*\)", "", chars)
        # Quitar asterisco (* = auto-id por transcript, no confirmado por cara)
        clean = clean.replace("*", "")
        # Split por separadores comunes
        parts = re.split(r"\s+y\s+|\s*,\s*|\s+;\s+", clean)
        for p in parts:
            name = _normalize_name(p)
            if not name:
                continue
            # Si tiene 2+ palabras, agregar también la primera como alias
            tokens = name.split()
            key = name
            if key not in out:
                out[key] = set()
            # Alias: primer nombre, último nombre, normalizado
            for t in tokens:
                nt = normalize(t)
                if nt and len(nt) >= 3 and nt not in NON_NAME_STOPWORDS:
                    out[key].add(nt)
            # Si key es de 1 palabra, esa palabra es alias
            if len(tokens) == 1:
                nt = normalize(tokens[0])
                if nt:
                    out[key].add(nt)
    return out


def discover_cast_from_transcripts(tr_dir: Path, min_clips: int = 2) -> dict[str, set[str]]:
    """Mining de transcripts buscando patrones de auto-ID. Cuenta cuántos
    clips DISTINTOS mencionan cada nombre. Retorna nombres con ≥ min_clips.
    """
    if not tr_dir.exists():
        return {}
    clips_by_name: dict[str, set[str]] = {}
    for tp in tr_dir.glob("*.json"):
        try:
            with tp.open() as f:
                tr = json.load(f)
        except Exception:
            continue
        text = tr.get("text", "") or ""
        if not text:
            continue
        for pat in SELF_ID_PATTERNS:
            for m in pat.finditer(text):
                name = _normalize_name(m.group(1))
                if not name:
                    continue
                clips_by_name.setdefault(name, set()).add(tp.stem)
    out: dict[str, set[str]] = {}
    for name, clip_ids in clips_by_name.items():
        if len(clip_ids) < min_clips:
            continue
        aliases = set()
        for t in name.split():
            nt = normalize(t)
            if nt and len(nt) >= 3 and nt not in NON_NAME_STOPWORDS:
                aliases.add(nt)
        out[name] = aliases
    return out


def discover_cast(conn: sqlite3.Connection, tr_dir: Path,
                  cast_json: Path | None = None) -> tuple[dict[str, set[str]], set[str]]:
    """Encuentra el cast del proyecto. Orden de prioridad:

      1. `cast.json` explícito (--cast-json o `<root>/.cinema_assistant/cast.json`)
      2. clip_characters ya curado
      3. Mining de transcripts (auto-ID patterns)

    Si hay cast.json y tiene "interviewers", lo usamos. Si no, intentamos
    auto-detectar interviewers (nombres mencionados en >40% de los clips
    pero que rara vez son entrevistados — heurística simple).

    Retorna (cast, interviewers).
    """
    cast: dict[str, set[str]] = {}
    interviewers: set[str] = set()

    # 1. cast.json explícito
    if cast_json and cast_json.exists():
        data = json.loads(cast_json.read_text())
        for name, aliases in data.get("cast", {}).items():
            cast[name] = set(aliases)
        interviewers = set(data.get("interviewers", []))
        if cast:
            return cast, interviewers

    # 2. clip_characters
    cc = discover_cast_from_clip_characters(conn)
    cast.update(cc)

    # 3. Mining de transcripts (complementa lo que no salió de clip_characters)
    tr_cast = discover_cast_from_transcripts(tr_dir)
    for name, aliases in tr_cast.items():
        if name not in cast:
            cast[name] = set()
        cast[name].update(aliases)

    # Sin fallback: si no hubo cast.json, ni clip_characters, ni mining, el
    # resultado correcto es vacio. Atribuir con el reparto de otro rodaje seria
    # escribir datos falsos en el manifest.

    # Deduplicación: si "ENTREVISTADO_4" y "ENTREVISTADO_4" ambos existen, la versión
    # más larga gana (más específica). Misma para "ENTREVISTADO_5"/"ENTREVISTADO_5".
    # Mergea los aliases del corto en el largo.
    keys = list(cast.keys())
    to_remove = set()
    for k in keys:
        if " " in k:
            continue  # es de 2+ palabras
        # buscar variantes "k <apellido>"
        for other in keys:
            if other == k or other in to_remove:
                continue
            if other.split()[0] == k:
                # k es la versión corta, other es la larga
                cast[other].update(cast[k])
                cast[other].add(normalize(k))
                to_remove.add(k)
                break
    for k in to_remove:
        del cast[k]

    return cast, interviewers


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def find_name_mentions(words: list[tuple[str, float]]) -> dict[str, list[float]]:
    """{ 'ESCALADOR_A': [t1, t2, ...], 'ESCALADORA_D': [...] } — timestamps de cada mención."""
    out = defaultdict(list)
    for w, t in words:
        nw = normalize(w)
        if not nw:
            continue
        for name, aliases in CAST.items():
            if nw in aliases:
                out[name].append(float(t))
    return dict(out)


def detect_interviewee_from_characters(conn: sqlite3.Connection, clip_id: int) -> list[str]:
    """Lee `clip_characters` ya curado para este clip y devuelve TODAS las
    personas listadas (puede ser 1, 2 o más en caso de entrevista dual).

    Returns: lista de nombres canónicos. Vacío si no hay info.
    """
    row = conn.execute(
        "SELECT characters FROM clip_characters WHERE clip_id=?", (clip_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    chars = row[0]
    clean = re.sub(r"\([^)]*\)", "", chars).replace("*", "")
    parts = re.split(r"\s+y\s+|\s*,\s*|\s+;\s+", clean)
    parts = [p.strip() for p in parts if p.strip()]
    parts = [p for p in parts if not re.match(r"^Persona_\d+", p)]
    # Quitar el formato "Nombre (~N)" (output previo del attribution)
    parts = [re.sub(r"\s*\(~\d+\)\s*$", "", p).strip() for p in parts]
    out = []
    for p in parts:
        name = _normalize_name(p)
        if name:
            out.append(name)
    return out


def detect_interviewee(words: list[tuple[str, float]], text: str) -> str | None:
    """Detecta el entrevistado del clip a partir del transcript.

    Reglas (por orden de prioridad):
    1. Buscar "¿cuál es tu nombre?" o "¿cómo te llamas?" → tomar el nombre
       del cast mencionado en los siguientes ~20 segundos de transcript.
    2. Como fallback: el nombre del cast (excluyendo INTERVIEWERS)
       mencionado con MAYOR FRECUENCIA en el clip.
    3. Si no hay ninguno → None (sin atribución por respuesta).
    """
    if not words:
        return None

    # Regla 1: pregunta directa "¿cuál es tu nombre?"
    text_low = (text or "").lower()
    triggers = [
        r"¿cu[áa]l es tu nombre\?",
        r"¿c[óo]mo te llamas\?",
        r"¿cu[áa]l es tu nombre completo\?",
    ]
    for pat in triggers:
        m = re.search(pat, text_low)
        if m:
            # Char position → tiempo aproximado
            char_pos = m.end()
            # Buscar palabras siguientes hasta los primeros nombres en ~20s
            # Como no tenemos un mapeo char→time perfecto, usamos word index.
            # Asumimos que el nombre aparece en las próximas 20 palabras
            # después del match (aproximación razonable).
            cum = 0
            after_words = []
            for w, t in words:
                cum += len(w) + 1
                if cum > char_pos:
                    after_words.append((w, t))
                if len(after_words) >= 20:
                    break
            for w, t in after_words:
                nw = normalize(w)
                for name, aliases in CAST.items():
                    if name in INTERVIEWERS:
                        continue
                    if nw in aliases:
                        return name

    # Regla 2: nombre más mencionado (excluyendo INTERVIEWERS)
    counts = Counter()
    for w, _t in words:
        nw = normalize(w)
        for name, aliases in CAST.items():
            if name in INTERVIEWERS:
                continue
            if nw in aliases:
                counts[name] += 1
    if not counts:
        return None
    top_name, top_n = counts.most_common(1)[0]
    # Solo se considera entrevistado si fue mencionado ≥ 3 veces
    # (filtra menciones casuales)
    return top_name if top_n >= 3 else None


def main():
    global CAST, INTERVIEWERS
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--interviewee", default=None,
                    help="(Legacy) entrevistado fijo. Default: auto-detectar por clip.")
    ap.add_argument("--cast-json", default=None,
                    help="Archivo JSON {\"cast\": {nombre: [aliases]}, \"interviewers\": [...]} "
                         "para overridear auto-discovery.")
    ap.add_argument("--print-cast", action="store_true",
                    help="Solo descubrir y mostrar el cast detectado, sin atribuir.")
    ap.add_argument("--reset", action="store_true",
                    help="Borrar face_attributions y clip_characters auto antes.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))

    # ── Auto-discovery del cast ─────────────────────────────────────────
    cast_json_path: Path | None = None
    if args.cast_json:
        cast_json_path = Path(args.cast_json)
    else:
        default_path = root / ".cinema_assistant" / "cast.json"
        if default_path.exists():
            cast_json_path = default_path
    CAST, INTERVIEWERS = discover_cast(conn, tr_dir, cast_json_path)
    print(f"=== CAST detectado ({len(CAST)} personas) ===")
    for name in sorted(CAST.keys()):
        aliases = sorted(CAST[name])
        flag = "  [INTERVIEWER]" if name in INTERVIEWERS else ""
        print(f"  {name:<30} aliases={aliases}{flag}")
    if args.print_cast:
        conn.close()
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_attributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_id INTEGER NOT NULL,
            identity_name TEXT NOT NULL,
            weight REAL,
            method TEXT,
            created_at REAL,
            FOREIGN KEY (detection_id) REFERENCES face_detections(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_det ON face_attributions(detection_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_name ON face_attributions(identity_name)")
    if args.reset:
        conn.execute("DELETE FROM face_attributions")

    now = time.time()

    # ── Señal 1: RESPUESTA EN ENTREVISTA (entrevistado detectado por clip) ─
    print("=== 1. Atribuyendo caras de RESPUESTAS al entrevistado "
          "(auto-detectado por clip a partir del transcript) ===")
    n_resp_attr = 0
    n_clips_with_interviewee = 0
    n_clips_no_interviewee = 0
    interview_clips = conn.execute("""
        SELECT c.id FROM clips c
        JOIN clip_descriptions d ON d.clip_id = c.id
        WHERE c.file_kind='video' AND d.category LIKE 'entrevista%'
    """).fetchall()
    n_fallback_clips = 0
    n_fallback_attr = 0
    for (cid,) in interview_clips:
        qs = conn.execute(
            "SELECT start_sec, end_sec FROM question_segments "
            "WHERE clip_id=? AND seg_index < 1000 ORDER BY seg_index",
            (cid,)
        ).fetchall()
        faces = conn.execute(
            "SELECT id, frame_t_sec FROM face_detections WHERE clip_id=?",
            (cid,)
        ).fetchall()
        if not faces:
            continue
        # FALLBACK 2026-05-26: clip de entrevista CON clip_characters Y SIN
        # question_segments → atribuir TODAS las caras a la(s) persona(s)
        # listada(s) en clip_characters (con peso 0.5 reducido por
        # incertidumbre temporal).
        if not qs:
            chars_list = detect_interviewee_from_characters(conn, cid)
            if not chars_list:
                continue
            wpp = 0.5 / max(len(chars_list), 1)
            for det_id, _ft in faces:
                for name in chars_list:
                    conn.execute(
                        "INSERT INTO face_attributions (detection_id, identity_name, weight, method, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (det_id, name, wpp, "interview_no_questions", now)
                    )
                    n_fallback_attr += 1
            n_fallback_clips += 1
            continue

        # Detectar entrevistado(s) del clip. Prioridad:
        #   1. clip_characters (puede tener 1+ personas: dual-lavalier)
        #   2. transcript (auto-detección por mención, solo 1 nombre)
        interviewees = detect_interviewee_from_characters(conn, cid)
        if not interviewees:
            tp = tr_dir / f"{cid}.json"
            if not tp.exists():
                n_clips_no_interviewee += 1
                continue
            try:
                with tp.open() as f:
                    tr = json.load(f)
            except Exception:
                n_clips_no_interviewee += 1
                continue
            words = [tuple(x) for x in tr.get("words", [])]
            text = tr.get("text", "")
            single = detect_interviewee(words, text)
            if single:
                interviewees = [single]
        if not interviewees:
            n_clips_no_interviewee += 1
            continue
        n_clips_with_interviewee += 1

        # Peso por persona: si hay 1, peso 0.8; si hay 2+, peso 0.8/N.
        # En entrevistas duales NO sabemos cuál cara es cuál — repartimos.
        # Más adelante (señal 2 menciones + cluster_cooccurrence) refinarán.
        per_person_weight = 0.8 / max(len(interviewees), 1)
        for q_start, q_end in qs:
            r_start = q_start + 5.0   # asume que la pregunta dura ≤ 5s
            r_end = q_end
            if r_end <= r_start:
                continue
            for det_id, ft in faces:
                if r_start <= ft <= r_end:
                    for name in interviewees:
                        conn.execute(
                            "INSERT INTO face_attributions (detection_id, identity_name, weight, method, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (det_id, name, per_person_weight, "response_in_interview", now)
                        )
                        n_resp_attr += 1
    print(f"  Clips de entrevista con entrevistado detectado: {n_clips_with_interviewee}")
    print(f"  Clips sin entrevistado claro (sin atribución señal 1): {n_clips_no_interviewee}")
    print(f"  Atribuciones por respuesta-en-entrevista: {n_resp_attr}")
    print(f"  Fallback (clip_characters sin questions): {n_fallback_clips} clips, "
          f"{n_fallback_attr} atribuciones extras")

    # ── Señal 2: MENCIÓN DE NOMBRE ± 5s ──────────────────────────────────
    print("\n=== 2. Atribuyendo caras por MENCIÓN del nombre en el transcript "
          f"(ventana ±{NAME_WINDOW_SEC:.0f}s) ===")
    n_mention_attr = 0
    clips_with_faces = conn.execute("""
        SELECT DISTINCT fd.clip_id FROM face_detections fd
    """).fetchall()
    for (cid,) in clips_with_faces:
        tp = tr_dir / f"{cid}.json"
        if not tp.exists():
            continue
        try:
            with tp.open() as f:
                tr = json.load(f)
        except Exception:
            continue
        words = [tuple(x) for x in tr.get("words", [])]
        if not words:
            continue
        mentions = find_name_mentions(words)
        if not mentions:
            continue
        # Excluye entrevistadores como "objetos de atribución por mención"
        # (ej. cuando ESCALADOR_A dice "ESCALADORA_B" no es para identificar a ESCALADORA_B en cuadro).
        for name in list(mentions.keys()):
            if name in INTERVIEWERS:
                continue
            faces = conn.execute(
                "SELECT id, frame_t_sec FROM face_detections WHERE clip_id=?",
                (cid,)
            ).fetchall()
            for det_id, ft in faces:
                for mention_t in mentions[name]:
                    if abs(ft - mention_t) <= NAME_WINDOW_SEC:
                        conn.execute(
                            "INSERT INTO face_attributions (detection_id, identity_name, weight, method, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (det_id, name, 0.5, "name_mention_nearby", now)
                        )
                        n_mention_attr += 1
                        break   # un voto por detección por nombre
    print(f"  Atribuciones por mención cercana: {n_mention_attr}")

    conn.commit()

    # ── Señal 3: CLUSTER AUTO-LABEL ──────────────────────────────────────
    print("\n=== 3. Cluster auto-labeling (mayoría de votos por cluster) ===")
    # Para cada detection, sumar pesos por nombre.
    det_votes = defaultdict(lambda: Counter())
    for det_id, name, w in conn.execute(
        "SELECT detection_id, identity_name, weight FROM face_attributions"
    ):
        det_votes[det_id][name] += w
    # Ahora agrupar por cluster (vía face_identities existente).
    cluster_votes = defaultdict(lambda: Counter())
    cluster_det_count = Counter()
    for det_id, votes in det_votes.items():
        # Obtener cluster (identity_id actual de InsightFace)
        row = conn.execute(
            "SELECT identity_id FROM face_identities WHERE detection_id=?",
            (det_id,)
        ).fetchone()
        if not row:
            continue
        iid = row[0]
        for name, weight in votes.items():
            cluster_votes[iid][name] += weight
        cluster_det_count[iid] += 1

    print(f"  Clusters con votos: {len(cluster_votes)}")
    confirmed = {}
    for iid, votes in cluster_votes.items():
        if not votes:
            continue
        total = sum(votes.values())
        top_name, top_w = votes.most_common(1)[0]
        ratio = top_w / total if total else 0
        n_det = cluster_det_count[iid]
        if ratio >= CLUSTER_LABEL_THRESHOLD and n_det >= CLUSTER_MIN_DETECTIONS:
            confirmed[iid] = (top_name, ratio, n_det)
            print(f"  cluster {iid} → {top_name}  ({ratio*100:.0f}% de {n_det} detecciones)")

    # ── Actualiza face_catalog con los nombres confirmados ───────────────
    print(f"\n=== Actualizando face_catalog ({len(confirmed)} clusters confirmados) ===")
    for iid, (name, ratio, n_det) in confirmed.items():
        current = conn.execute(
            "SELECT canonical_name FROM face_catalog WHERE identity_id=?", (iid,)
        ).fetchone()
        if current and current[0] == name:
            continue
        # ¿Existe ya OTRO face_catalog con este nombre canonical?
        # Esto pasa cuando DBSCAN separa la misma persona en 2 clusters por
        # diferencia de luz/ángulo, pero el audio confirma que son la misma.
        existing_other = conn.execute(
            "SELECT identity_id FROM face_catalog WHERE canonical_name=? AND identity_id<>?",
            (name, iid)
        ).fetchone()
        if existing_other:
            # MERGE: redirigir TODAS las face_identities del cluster iid al other,
            # luego borrar la fila iid de face_catalog.
            other_iid = existing_other[0]
            n_merged = conn.execute(
                "UPDATE face_identities SET identity_id=? WHERE identity_id=?",
                (other_iid, iid)
            ).rowcount
            conn.execute("DELETE FROM face_catalog WHERE identity_id=?", (iid,))
            # actualizar n_detections del catalog existente
            n_total = conn.execute(
                "SELECT COUNT(*) FROM face_identities WHERE identity_id=?", (other_iid,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE face_catalog SET n_detections=?, updated_at=? WHERE identity_id=?",
                (n_total, now, other_iid)
            )
            print(f"  cluster {iid}: MERGED en '{name}' (identity_id={other_iid}, "
                  f"{n_merged} detecciones redirigidas)")
            continue
        if current:
            # Sobrescribir si el actual es provisional ("Persona_NN")
            is_provisional = bool(re.match(r"^Persona_\d+$", current[0] or ""))
            if is_provisional:
                conn.execute(
                    "UPDATE face_catalog SET canonical_name=?, aliases=?, "
                    "n_detections=?, updated_at=? WHERE identity_id=?",
                    (name, name.lower(), n_det, now, iid)
                )
                print(f"  cluster {iid}: '{current[0]}' → '{name}' "
                      f"(audio confirma {ratio*100:.0f}% de {n_det} det)")
            else:
                print(f"  cluster {iid}: ya tenía '{current[0]}', "
                      f"audio sugiere '{name}' ({ratio*100:.0f}%) — no sobrescribe")
            continue
        # No existe ni con este id ni con este nombre — crear fresh
        conn.execute(
            "INSERT INTO face_catalog (canonical_name, aliases, "
            "n_detections, cluster_id, updated_at) VALUES (?,?,?,?,?)",
            (name, name.lower(), n_det, iid, now)
        )

    # ── Pobla clip_characters con identidades AUDIO-CONFIRMADAS ──────────
    print("\n=== Actualizando clip_characters con identidades audio-confirmadas ===")
    # Por clip: contar votos consolidados (de las atribuciones del clip).
    clip_votes = defaultdict(lambda: Counter())
    for det_id, name, w in conn.execute(
        "SELECT detection_id, identity_name, weight FROM face_attributions"
    ):
        cid_row = conn.execute(
            "SELECT clip_id FROM face_detections WHERE id=?", (det_id,)
        ).fetchone()
        if cid_row:
            clip_votes[cid_row[0]][name] += w
    n_clips_updated = 0
    n_clips_preserved = 0
    for cid, votes in clip_votes.items():
        if not votes:
            continue
        ranked = ", ".join(f"{n} (~{int(w)})" for n, w in votes.most_common())
        prev = conn.execute(
            "SELECT characters, context FROM clip_characters WHERE clip_id=?",
            (cid,)
        ).fetchone()
        # PRESERVE manual curation: si prev NO tiene formato auto "(~N)" en
        # ningún token, es curación manual (de Claude o usuario). NO sobrescribir.
        # Permitir merge: si prev tiene "(~N)" → es asignación previa de este
        # script, OK actualizar.
        prev_chars = (prev[0] or "") if prev else ""
        prev_ctx = (prev[1] or "") if prev else ""
        is_manual = bool(prev_chars) and ("(~" not in prev_chars)
        if is_manual:
            # Conservar manual, mostrar diff por log
            print(f"  clip {cid}: preservado manual '{prev_chars[:50]}...' "
                  f"(auto sugiere: '{ranked[:50]}...')")
            n_clips_preserved += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO clip_characters (clip_id, characters, context, updated_at) "
            "VALUES (?,?,?,?)",
            (cid, ranked, prev_ctx, now)
        )
        n_clips_updated += 1
    print(f"\n  clip_characters: {n_clips_updated} updated, {n_clips_preserved} preservados (manual)")
    conn.commit()
    conn.close()

    print(f"\nDone. clip_characters actualizado en {n_clips_updated} clips.")
    print(f"      face_catalog confirmados: {len(confirmed)}")


if __name__ == "__main__":
    main()
