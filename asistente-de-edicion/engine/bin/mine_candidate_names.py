#!/usr/bin/env python3
"""Mina nombres propios candidatos en los transcripts para sugerir nuevos
miembros del CAST del proyecto. Aplica criterios CONSERVADORES: solo
propone nombres con evidencia fuerte.

Filtros aplicados (todos en cascada):

  1. Palabra capitalizada (mayúscula inicial).
  2. NO está al inicio de oración (descarta capitalizaciones gramaticales).
  3. NO está en el CAST actual (ya conocidos).
  4. NO es lugar / topónimo conocido del proyecto.
  5. NO es palabra común capitalizada (Dios, Señor, días, meses).
  6. NO es palabra de < 3 caracteres.
  7. Menciones ≥ 3 (global) y ≥ 2 (por clip, en al menos 1 clip).
  8. Patrón gramatical: precedido por "venga", "viene", "es", "se llama",
     "soy", "le dije a", "vamos con", "felicidades", "gracias" — i.e.
     contextos donde un nombre propio tiene sentido.

Output: tabla en stdout con candidato + evidencia (n menciones, n clips,
contextos de ejemplo). El usuario valida y agrega manualmente al CAST.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402
from lib.cast import load_cast  # noqa: E402


# Nombres ya conocidos en el CAST (no proponer estos como nuevos). Salen del
# cast.json del proyecto (lib/cast.py), que main() carga aqui. Hasta el
# 2026-10-08 este set traia escrito a mano el reparto de un rodaje: la misma
# falla, de privacidad y funcional, que la auditoria del 2026-07-31 le quito a
# enrich_curated_segments.py. En el rodaje de otra persona no casaba con nadie.
CAST_KNOWN: set[str] = set()

# Topónimos y lugares del proyecto.
PLACES = {
    "mexico", "méxico", "jilotepec", "monterrey", "guanajuato",
    "guadalajara", "guadalcazar", "guadalcázar", "chontacatlan",
    "chontacatlán", "potrero", "chico", "espana", "españa", "francia",
    "canada", "canadá", "estados", "unidos", "querétaro", "queretaro",
    "lujuria", "huasteca", "chihuahua", "diente", "cantil", "minas",
    "mineral", "chico", "san", "luis", "potosi", "potosí", "comalapa",
    "lorenzo", "centro", "norte", "sur", "este", "oeste",
    # Topónimos detectados en el mining inicial
    "sierra", "yosemite", "coconetla", "concagua", "pachuca", "baja",
    "paz", "petzl", "roctrip", "zoom", "petzel",
}

# Palabras comunes en mayúscula que no son nombres propios.
COMMON_CAPS = {
    "dios", "señor", "señora", "señorita", "dn", "don", "doña", "santo",
    "santa", "vírgen", "virgen", "lunes", "martes", "miércoles", "jueves",
    "viernes", "sábado", "domingo", "enero", "febrero", "marzo", "abril",
    "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
    "noviembre", "diciembre", "navidad", "pascua", "pero", "porque",
    "entonces", "ahora", "después", "antes", "aquí", "allá", "ahí",
    "si", "sí", "no", "claro", "bueno", "vale", "ok", "ay", "uy",
    "venga", "vamos", "viene", "voy", "soy", "está", "están", "estás",
    "es", "son", "el", "la", "los", "las", "una", "uno", "unos", "unas",
    "que", "qué", "como", "cómo", "cuando", "cuándo", "porque", "porqué",
    "donde", "dónde", "yo", "tú", "él", "ella", "nosotros", "ustedes",
    "ellos", "ellas", "este", "esta", "esto", "ese", "esa", "eso",
    "aquel", "aquella", "muy", "más", "menos", "tan", "tanto",
    "vamos", "ven", "ah", "oh", "eh", "wow", "uy", "uf", "ay",
    "gracias", "amén", "amen", "salud", "felicidades", "buenas",
    "buenos", "buenas", "días", "noches", "tardes", "mañana", "noche",
    "tarde", "encadene", "ascenso", "ruta", "vía", "via", "anilla",
    "presa", "magnesia", "cuerda", "arnés", "casco",
    "le", "lo", "la", "les", "los", "las", "me", "te", "se", "nos", "os",
    "y", "o", "u", "ni", "pero", "mas", "sino", "también", "tambien",
    "hay", "ha", "han", "he", "hubo", "habrá", "habría", "fue", "fui",
    "fuera", "iba", "ven", "vi", "ver", "viste", "vino", "viene",
    "puedo", "puede", "pueden", "quiero", "quiere", "quieren",
    "okay", "okey", "ah", "oh", "hum",
    # Muletillas capitalizadas detectadas en mining inicial
    "pues", "verdad", "para", "bien", "hola", "okay", "okey",
    "incluso", "además", "después", "mientras", "tampoco", "siempre",
    "nunca", "casi", "todo", "todos", "todas", "nada", "algo",
    "solo", "sólo", "mismo", "misma", "alguien", "nadie",
}

# Whitelist suave: nombres de pila comunes en español (alta probabilidad
# de ser persona real cuando aparecen con contextos del NAME_CONTEXTS).
COMMON_SPANISH_NAMES = {
    "ricardo", "juan", "carlos", "sergio", "jorge", "eduardo", "germán",
    "german", "paco", "césar", "cesar", "raúl", "raul", "hugo", "luis",
    "mario", "miguel", "alejandro", "fernando", "antonio", "pedro",
    "manuel", "francisco", "rafael", "alberto", "ramón", "ramon",
    "armando", "andrés", "andres", "ernesto", "enrique", "felipe",
    "guillermo", "héctor", "hector", "ignacio", "ismael", "jesús",
    "jesus", "joaquín", "joaquin", "joel", "josé", "jose", "leonardo",
    "lorenzo", "lucas", "marco", "mauricio", "nicolás", "nicolas",
    "octavio", "oscar", "óscar", "pablo", "patricio", "rodrigo",
    "samuel", "santiago", "saúl", "saul", "sebastián", "sebastian",
    "tomás", "tomas", "víctor", "victor", "ana", "carmen", "rosa",
    "maría", "maria", "laura", "patricia", "sofía", "sofia", "isabel",
    "andrea", "valeria", "lucía", "lucia", "fernanda", "alejandra",
    "gabriela", "natalia", "claudia", "sandra", "diana", "elena",
    "paula", "verónica", "veronica", "monica", "mónica", "regina",
    "alicia", "beatriz", "cristina", "daniela", "estela", "fátima",
    "fatima", "gloria", "irene", "julia", "leticia", "marta", "marisa",
    "raquel", "rebeca", "silvia", "yolanda",
}

# Contextos donde un nombre tiene sentido (palabra que precede).
NAME_CONTEXTS = {
    "venga", "viene", "ven", "vamos", "soy", "llamo", "llama",
    "felicidades", "gracias", "saluda", "a", "con", "ese", "esa",
    "le", "la", "lo", "para", "es", "está", "estaba", "fue", "vio",
    "como", "verdad", "oye", "hola", "señor", "señora", "señorita",
    "compa", "amigo", "amiga", "hermano", "hermana", "carnal",
}

MIN_GLOBAL_MENTIONS = 3
MIN_CLIPS = 2
NAME_MAX_LEN = 20


def normalize_lower(w: str) -> str:
    return unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def is_candidate_name(word: str) -> bool:
    """Heurística básica: empieza mayúscula, ≥3 chars, alfabético."""
    if not word or len(word) < 3 or len(word) > NAME_MAX_LEN:
        return False
    if not word[0].isupper():
        return False
    # Resto en minúscula (Nombre, no NOMBRE ni nombre)
    if not all(c.isalpha() or c in "-'áéíóúñü" for c in word):
        return False
    norm = normalize_lower(word)
    if norm in CAST_KNOWN or norm in PLACES or norm in COMMON_CAPS:
        return False
    return True


def extract_names_with_context(words: list[tuple[str, float]]) -> list[tuple[str, str]]:
    """Devuelve (nombre_candidato, palabra_previa)."""
    out = []
    prev = ""
    for w, _t in words:
        # quitar puntuación final
        ww = w.strip(",.;:!¡¿?\"'()[]")
        if is_candidate_name(ww):
            out.append((ww, prev.lower().strip(",.;:!¡¿?\"'()[]")))
        prev = w
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--top", type=int, default=30,
                    help="Mostrar top N candidatos por frecuencia")
    ap.add_argument("--strict", action="store_true",
                    help="Filtrar también por contexto gramatical NAME_CONTEXTS")
    args = ap.parse_args()

    root = resolve_root(args.root)
    for nombre, alias in load_cast(root).items():
        CAST_KNOWN.add(normalize_lower(nombre))
        CAST_KNOWN.update(normalize_lower(a) for a in alias)
    tr_dir = root / ".cinema_assistant" / "transcripts"
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not tr_dir.exists():
        sys.exit(f"No transcripts dir at {tr_dir}")

    # Cargar metadata de clips para mostrar ubicación
    conn = manifest.conectar(str(db))
    clip_info = {}
    for r in conn.execute(
        "SELECT id, filename, rel_path FROM clips WHERE index_status='ok'"
    ):
        clip_info[r[0]] = {"filename": r[1], "rel_path": r[2] or ""}
    conn.close()

    # Mining
    global_counts = Counter()
    by_clip = defaultdict(Counter)
    contexts_by_name = defaultdict(list)
    valid_contexts = defaultdict(int)

    for f in tr_dir.iterdir():
        if not f.name.endswith(".json") or f.name.startswith("._"):
            continue
        try:
            cid = int(f.stem)
        except ValueError:
            continue
        try:
            with f.open() as fh:
                tr = json.load(fh)
        except Exception:
            continue
        words = [tuple(x) for x in tr.get("words", [])]
        if not words:
            continue
        for name, prev in extract_names_with_context(words):
            if args.strict and prev not in NAME_CONTEXTS:
                continue
            global_counts[name] += 1
            by_clip[name][cid] += 1
            if prev in NAME_CONTEXTS:
                valid_contexts[name] += 1
            if len(contexts_by_name[name]) < 3 and prev:
                contexts_by_name[name].append((prev, cid))

    # Filtrar candidatos
    candidates = []
    for name, count in global_counts.items():
        if count < MIN_GLOBAL_MENTIONS:
            continue
        n_clips = len(by_clip[name])
        if n_clips < MIN_CLIPS:
            continue
        is_common_name = normalize_lower(name) in COMMON_SPANISH_NAMES
        candidates.append({
            "name": name,
            "mentions": count,
            "n_clips": n_clips,
            "valid_contexts": valid_contexts.get(name, 0),
            "context_samples": contexts_by_name[name][:3],
            "clips_sample": list(by_clip[name].keys())[:3],
            "is_common_name": is_common_name,
        })
    # Ordenar por: es_nombre_común (descending), contextos válidos, menciones
    candidates.sort(key=lambda c: (-int(c["is_common_name"]), -c["valid_contexts"], -c["mentions"]))

    print(f"Encontrados {len(candidates)} candidatos con evidencia (>= {MIN_GLOBAL_MENTIONS} menciones, >= {MIN_CLIPS} clips).")
    print(f"\n=== NOMBRES PROPIOS COMUNES EN ESPAÑOL (alta confianza — probable que sean personas reales) ===\n")
    high = [c for c in candidates if c["is_common_name"]]
    print(f"{'Candidato':14} {'Mencs':>5} {'Clips':>5}  Ejemplos de contexto")
    print("-" * 90)
    for c in high[:args.top]:
        ctx_str = "; ".join(f'\"{prev} {c["name"]}\"' for prev, _cid in c["context_samples"][:2])
        print(f"{c['name']:14} {c['mentions']:>5} {c['n_clips']:>5}  {ctx_str[:65]}")

    print(f"\n=== OTROS (palabras capitalizadas inciertas — verificar) ===\n")
    low = [c for c in candidates if not c["is_common_name"]]
    print(f"{'Candidato':14} {'Mencs':>5} {'Clips':>5}")
    print("-" * 40)
    for c in low[:min(15, len(low))]:
        print(f"{c['name']:14} {c['mentions']:>5} {c['n_clips']:>5}")


if __name__ == "__main__":
    main()
