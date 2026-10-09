"""Vocabulario del proyecto: nombres del cast + términos comunes, para
corregir errores de Whisper por fuzzy matching post-transcripción.

Doctrina (Zezzions iter9.4, 2026-05-27): Whisper mal-interpreta nombres
propios y términos específicos del dominio. Ejemplos típicos:
  - "Vici" → "Avicii"
  - "Mariana Ruis" → "Mariana Ruiz" (nombre inventado: un apellido del cast)

Estrategia: cargar vocabulario del proyecto (cast del manifest + términos
del dominio), y para cada palabra del transcript, si difiere de una entrada
del vocabulario por ≤2 caracteres (Levenshtein), sustituir.

NUNCA corregir palabras del español estándar (riesgo de sobrecorrección).
Solo aplicar a palabras del transcript que NO estén en un diccionario
español común.
"""
from __future__ import annotations

import json
import sqlite3
from difflib import get_close_matches, SequenceMatcher
from pathlib import Path
from typing import Optional


# Mexicanismos comunes (preservar acentuación) — válidos en cualquier proyecto
MEXICANISMOS = {
    "wey", "güey", "chido", "chingón", "neta", "cabrón", "porro",
    "banda", "ahorita", "órale",
}

# Términos legacy del proyecto Zezzions (música electrónica). Solo se usan
# como fallback cuando el proyecto NO define "vocabulary_hints" en su
# project_config.json — mantiene compat con el manifest de Zezzions.
LEGACY_ZEZZIONS_TERMS = {
    "Avicii", "Aphex Twin", "Daft Punk", "Tomorrowland",
    "Swedish House Mafia", "Skrillex", "Deadmau5", "Flume",
    "Boards of Canada", "Bonobo",
    "IDM", "EDM", "drum and bass", "techno", "house", "dubstep",
    "trip-hop", "garage", "UK garage", "synthwave",
    "GarageBand", "Logic", "Ableton", "FL Studio", "Reaper",
    "sintetizador", "acordeón", "MIDI", "setup",
    "Sessions", "Vías de León", "Mafex Twin",
}

# Compat con imports existentes
DEFAULT_DOMAIN_TERMS = MEXICANISMOS | LEGACY_ZEZZIONS_TERMS


def load_vocab_hints_from_config(disk_root: Path) -> set[str]:
    """Lee vocabulary_hints de <disk>/.cinema_assistant/project_config.json."""
    cfg_path = disk_root / ".cinema_assistant" / "project_config.json"
    hints = set()
    if cfg_path.exists():
        try:
            d = json.load(cfg_path.open())
            for term in d.get("vocabulary_hints", []) or []:
                if isinstance(term, str) and len(term) >= 3:
                    hints.add(term)
        except Exception:
            pass
    return hints


_PUNTUACION = ".,?¿!¡;:'\"-"


def _normalize(w: str) -> str:
    """Normaliza word para comparación."""
    return w.lower().strip(_PUNTUACION)


def _con_puntuacion(original: str, corregida: str) -> str:
    """La palabra corregida, con la puntuacion que rodeaba a la original.

    Sustituir la palabra entera se comia el punto final: "10.50." quedaba en
    "Diez50" y la oracion perdia su frontera, que es de lo que viven el inicio
    de oracion de aqui abajo y los cortes de los subtitulos (Asistente,
    2026-09-28). `correct_text` ya la conservaba; el `words` no.
    """
    ini = len(original) - len(original.lstrip(_PUNTUACION))
    fin = len(original.rstrip(_PUNTUACION))
    return original[:ini] + corregida + original[fin:]


def _similarity(a: str, b: str) -> float:
    """Similarity ratio 0-1 con SequenceMatcher."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def load_cast_from_manifest(conn: sqlite3.Connection) -> set[str]:
    """Carga nombres del cast desde face_catalog + clip_characters."""
    names = set()
    # face_catalog.canonical_name
    try:
        for (n,) in conn.execute(
            "SELECT canonical_name FROM face_catalog "
            "WHERE canonical_name NOT LIKE 'Persona_%' AND canonical_name != ''"
        ):
            if n:
                # Quitar marcadores como * y (notas)
                clean = n.replace("*", "").split("(")[0].strip()
                if clean and len(clean) >= 3:
                    names.add(clean)
                    # También cada palabra del nombre individualmente
                    for w in clean.split():
                        if len(w) >= 3:
                            names.add(w)
    except sqlite3.OperationalError:
        pass
    # voice_catalog.canonical_name
    try:
        for (n,) in conn.execute(
            "SELECT canonical_name FROM voice_catalog WHERE canonical_name != ''"
        ):
            if n:
                clean = n.replace("*", "").split("(")[0].strip()
                if clean and len(clean) >= 3:
                    names.add(clean)
                    for w in clean.split():
                        if len(w) >= 3:
                            names.add(w)
    except sqlite3.OperationalError:
        pass
    return names


def load_cast_json(disk_root: Path) -> set[str]:
    """Si existe disk/.cinema_assistant/cast.json, cargar nombres extra."""
    cast_path = disk_root / ".cinema_assistant" / "cast.json"
    names = set()
    if cast_path.exists():
        try:
            d = json.load(cast_path.open())
            for name in d.get("cast", []) or []:
                if isinstance(name, str) and len(name) >= 3:
                    names.add(name)
                    for w in name.split():
                        if len(w) >= 3:
                            names.add(w)
        except Exception:
            pass
    return names


def build_project_vocabulary(conn: sqlite3.Connection,
                               disk_root: Path,
                               extra_terms: Optional[set] = None) -> set[str]:
    """Vocabulario del proyecto = cast + dominio + extra.

    Dominio: vocabulary_hints del project_config.json si existen;
    si no, fallback a los términos legacy de Zezzions (compat).
    """
    vocab = set()
    vocab.update(MEXICANISMOS)
    hints = load_vocab_hints_from_config(disk_root)
    vocab.update(hints if hints else LEGACY_ZEZZIONS_TERMS)
    vocab.update(load_cast_from_manifest(conn))
    vocab.update(load_cast_json(disk_root))
    if extra_terms:
        vocab.update(extra_terms)
    # Quedarnos solo con términos >= 3 chars (cortos sobre-corrigen)
    vocab = {v for v in vocab if len(v) >= 3}
    return vocab


# Diccionario mínimo de palabras españolas comunes — si la palabra del
# transcript matchea aquí, NO la corregimos.
COMMON_SPANISH = {
    "que", "qué", "como", "cómo", "cuando", "cuándo", "donde", "dónde",
    "para", "por", "porque", "pero", "más", "menos", "muy", "mucho",
    "poco", "todo", "todos", "nada", "algo", "ahí", "aquí", "allí",
    "este", "esta", "estos", "estas", "ese", "esa", "eso",
    "soy", "eres", "es", "somos", "son", "fui", "fuiste", "fue",
    "estoy", "estás", "está", "estamos", "están", "estuvo",
    "tengo", "tienes", "tiene", "tenemos", "tienen",
    "puedo", "puedes", "puede", "podemos", "pueden",
    "hago", "haces", "hace", "hacemos", "hacen",
    "voy", "vas", "va", "vamos", "van", "ir",
    "veo", "ves", "ve", "vemos", "ven",
    "digo", "dices", "dice", "decimos", "dicen",
    "vivo", "vives", "vive", "vivimos", "viven",
    "gusta", "gustan", "gustaría", "encanta", "quiero", "quieres",
    "creo", "crees", "cree", "creemos", "creen", "pienso",
    "tiempo", "vida", "gente", "cosa", "cosas", "día", "año", "años",
    "casa", "trabajo", "música", "arte", "obra", "obras", "evento",
    "amigos", "familia", "persona", "personas",
    "estado", "estados", "unidos", "mundo", "país", "países",
    "ciudad", "equipo", "equipos", "partido", "partidos", "juego",
    "primero", "segundo", "último", "mismo", "siempre", "nunca",
    "también", "entonces", "después", "antes", "ahora", "ya",
    "claro", "cierto", "verdad", "bueno", "malo", "grande", "pequeño",
    "alguno", "ninguno", "cualquier", "varios",
    "con", "sin", "entre", "sobre", "hasta", "desde", "hacia",
    "aunque", "porque", "mientras", "según",
}


def load_explicit_corrections(disk_root) -> dict[str, str]:
    """Correcciones exactas del PROYECTO, de project_config.json.

        "vocabulary_fixes": {"Jimena": "Ximena", "Danilo": "Daniel"}

    POR QUE HACE FALTA (2026-08-06). La corrección por fuzzy se niega, con
    razón, a tocar palabras del español estándar: así evita convertir "mandas"
    en un nombre del cast. Pero eso deja fuera el caso más común de un rodaje:
    dos grafías VÁLIDAS del mismo nombre. En el comercial de IMODAE, Whisper
    escribió "Jimena" en 26 clips y "Ximena" en 4 — las dos son nombres
    correctos en español, y sólo el proyecto sabe cuál es la persona.

    Van en el disco, no aquí, por la misma razón que el cast: son datos de un
    rodaje. `EXPLICIT_CORRECTIONS_LOWER` de abajo nació con términos de UN
    proyecto (Zezzions: Avicii, techno, GarageBand) escritos dentro del motor,
    que es el defecto de capas que ya se corrigió con `cast.json` y con los
    horneados. Lo del proyecto gana sobre lo del motor.
    """
    from pathlib import Path
    if not disk_root:
        return {}
    p = Path(disk_root) / ".cinema_assistant" / "project_config.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    fixes = d.get("vocabulary_fixes") or {}
    if not isinstance(fixes, dict):
        return {}
    return {_normalize(k): str(v) for k, v in fixes.items() if k and v}


# Correcciones EXPLÍCITAS (lookup exacto, sin fuzzy) — errores documentados
# que Whisper produce frecuentemente y queremos forzar el reemplazo.
#
# OJO: casi todas son de Zezzions (música electrónica). Son datos de UN proyecto
# viviendo en el motor. Se conservan por compatibilidad con ese manifest, pero
# lo nuevo va en `vocabulary_fixes` del project_config.json del disco — ver
# load_explicit_corrections() arriba.
EXPLICIT_CORRECTIONS_LOWER = {
    # Artistas de música electrónica (homófonos comunes)
    "vici": "Avicii",
    "avici": "Avicii",
    "tecno": "techno",
    "session": "Sessions",
    "leon": "León",
    # GarageBand / Ableton / Logic etc — typos
    "garageband": "GarageBand",
    "garage band": "GarageBand",
    "ableton": "Ableton",
    # Vías de León (a veces transcrito mal)
    # añadir más cuando se descubran
}


def is_proper_noun_capitalized(w: str) -> bool:
    """¿La palabra original empieza con mayúscula y no es inicio de oración?
    Heurística: si la palabra completa tiene la primera letra en mayúscula
    y al menos otra letra minúscula, probablemente es nombre propio.
    """
    if len(w) < 2: return False
    return w[0].isupper() and any(c.islower() for c in w[1:])


def correct_transcript_words(words: list[tuple[str, float]],
                              vocabulary: set[str],
                              cutoff: float = 0.85,
                              min_word_len: int = 4,
                              fuzzy_only_proper_nouns: bool = True,
                              explicit: dict[str, str] | None = None
                              ) -> tuple[list[tuple[str, float]], dict[str, str]]:
    """Aplica corrección por vocabulario a cada palabra del transcript.

    Estrategia segura (iter9.4):
      1. EXPLICIT_CORRECTIONS_LOWER: lookup exacto sin fuzzy. Siempre aplica.
      2. Vocabulario fuzzy: SOLO si la palabra parece nombre propio (mayúscula
         inicial Y NO es la primera palabra de la oración). Reduce false positives
         de tipo "mandas"→"ENTREVISTADO_4".

    Args:
        words: lista [(word, time)]
        vocabulary: set de palabras conocidas del proyecto
        cutoff: similaridad mínima para considerar match (0-1)
        min_word_len: solo corregir palabras de esta longitud o más
        fuzzy_only_proper_nouns: si True, fuzzy solo aplica a candidatos
            que parecen nombres propios (capitalización + no inicio oración).

    Returns: (words corregidas, dict {original: corregida})
    """
    # Las del proyecto ganan sobre las del motor: si un rodaje declara que
    # "Jimena" es "Ximena", eso manda sobre cualquier default heredado.
    explicit_all = dict(EXPLICIT_CORRECTIONS_LOWER)
    explicit_all.update(explicit or {})

    if not vocabulary and not explicit_all:
        return words, {}

    # Solo nombres propios del cast (con mayúscula original) van al fuzzy
    proper_noun_vocab = {v: v for v in vocabulary
                          if v and v[0].isupper() and not v.islower()}
    proper_keys_lower = {v.lower(): v for v in proper_noun_vocab}

    corrections: dict[str, str] = {}
    corrected_words = []
    correction_cache: dict[str, str] = {}

    for i, (w, t) in enumerate(words):
        w_norm = _normalize(w)
        is_sentence_start = (i == 0) or (i > 0 and any(
            words[i-1][0].endswith(p) for p in ".!?"
        ))

        # 1. EXPLICIT corrections (siempre, incluso para palabras comunes)
        if w_norm in explicit_all:
            corrected = explicit_all[w_norm]
            if corrected.lower() != w_norm:
                corrections[w] = corrected
                corrected_words.append([_con_puntuacion(w, corrected), t])
                continue
            else:
                # Mismo lower → no es corrección real, solo case-change. Skip.
                corrected_words.append([w, t])
                continue

        # Skip si palabra corta o común
        if len(w_norm) < min_word_len or w_norm in COMMON_SPANISH:
            corrected_words.append([w, t])
            continue

        # 2. Fuzzy solo si parece nombre propio
        if fuzzy_only_proper_nouns:
            if not is_proper_noun_capitalized(w) or is_sentence_start:
                corrected_words.append([w, t])
                continue
            # Cache
            if w_norm in correction_cache:
                corrected = correction_cache[w_norm]
                if corrected != w_norm:
                    corrections[w] = corrected
                    corrected_words.append([_con_puntuacion(w, corrected), t])
                else:
                    corrected_words.append([w, t])
                continue
            # Match contra nombres propios
            matches = get_close_matches(w_norm, list(proper_keys_lower.keys()),
                                          n=1, cutoff=cutoff)
            if matches:
                match = matches[0]
                if abs(len(w_norm) - len(match)) <= 2:
                    corrected = proper_keys_lower[match]
                    # Solo registrar como corrección si REALMENTE cambia (ignorar puntuación)
                    if corrected.lower() != w_norm:
                        correction_cache[w_norm] = corrected
                        corrections[w] = corrected
                        corrected_words.append([_con_puntuacion(w, corrected), t])
                        continue
                    else:
                        correction_cache[w_norm] = w_norm
                        corrected_words.append([w, t])
                        continue
            correction_cache[w_norm] = w_norm
            corrected_words.append([w, t])
            continue

        # Modo permisivo (no usar por default)
        if w_norm in {v.lower() for v in vocabulary}:
            corrected_words.append([w, t])
            continue
        matches = get_close_matches(w_norm, [v.lower() for v in vocabulary],
                                     n=1, cutoff=cutoff)
        if matches:
            for v in vocabulary:
                if v.lower() == matches[0]:
                    corrections[w] = v
                    corrected_words.append([_con_puntuacion(w, v), t])
                    break
            continue
        corrected_words.append([w, t])

    return corrected_words, corrections


def correct_text(text: str, vocabulary: set[str], cutoff: float = 0.85,
                 explicit: dict[str, str] | None = None) -> str:
    """Aplica corrección palabra por palabra al texto plano.

    `explicit` va aparte del fuzzy y se aplica ANTES: son sustituciones exactas
    declaradas por el proyecto, y tienen que funcionar incluso sobre palabras
    del español estándar —"Jimena" es un nombre correcto, pero en ESTE rodaje
    la persona se llama Ximena. Sin esto, el campo `text` se quedaba sin
    corregir aunque `words` sí se arreglara, y el transcript acababa
    contradiciéndose consigo mismo.
    """
    if not vocabulary and not explicit:
        return text
    explicit = explicit or {}
    vocab_lower = {v.lower(): v for v in vocabulary}
    out_words = []
    for w in text.split():
        # Preservar puntuación
        leading = ""
        trailing = ""
        core = w
        while core and core[0] in "¿¡":
            leading += core[0]
            core = core[1:]
        while core and core[-1] in ".,?!;:":
            trailing = core[-1] + trailing
            core = core[:-1]
        core_norm = core.lower()
        if core_norm in explicit:
            out_words.append(leading + explicit[core_norm] + trailing)
            continue
        if (len(core_norm) >= 4 and core_norm not in COMMON_SPANISH
                and core_norm not in vocab_lower):
            matches = get_close_matches(core_norm, list(vocab_lower.keys()),
                                          n=1, cutoff=cutoff)
            if matches:
                match = matches[0]
                if (_similarity(core_norm, match) >= cutoff and
                    abs(len(core_norm) - len(match)) <= 2):
                    core = vocab_lower[match]
        out_words.append(leading + core + trailing)
    return " ".join(out_words)
