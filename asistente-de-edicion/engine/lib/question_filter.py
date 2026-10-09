"""Filtro y extracción de preguntas de entrevista — ÚNICA FUENTE DE VERDAD.

Doctrina (iter10, 2026-05-28): antes este filtro vivía duplicado dentro de
`lib/master_question_detect.py` (estricto) y `bin/derive_question_segments.py`
(débil, `_is_real_question`). Eso causó que las entrevistas SOLO-VIDEO (sin
lavalier, p.ej. 2573 ENTREVISTADO_5 organizador) no recibieran markers:

  1. El detector base sólo extraía texto entre `¿` y `?`. Whisper a menudo
     OMITE el signo de apertura `¿` en preguntas del entrevistador grabadas
     en el audio de cámara (A1). Resultado: 0 preguntas detectadas.
  2. Pero Whisper SÍ ACENTÚA los interrogativos ("cómo", "qué", "cuándo")
     aunque omita los signos. Ese acento es el ancla lingüística: en español
     el interrogativo lleva tilde ("cómo empezaste") mientras el relativo NO
     ("como te decía"). Aprovechamos esa distinción.

Este módulo unifica:
  - Las constantes (interrogativos, verbos, fillers, primera persona).
  - `is_real_question_q()` — filtro estricto único.
  - `split_sentences()` — segmentación con offsets de carácter.
  - `split_questions(text, loose=True)` — extrae spans de pregunta:
      a) explícitas `¿...?` (alta confianza, siempre).
      b) (loose) oraciones cuyo interrogativo ACENTUADO aparece al frente
         (primeras N palabras) — captura prompts del entrevistador sin `¿?`.
    Todos los candidatos pasan por `is_real_question_q()`.

Tanto `derive_question_segments` (camino plano / solo-video / audio) como
`master_question_detect` (camino multi-source) importan de aquí.
"""
from __future__ import annotations

import re
from collections import Counter

# ── Constantes léxicas ──────────────────────────────────────────────────────

# Match "¿…?" (pregunta española explícita). Non-greedy. Allow newlines.
RE_QUESTION = re.compile(r"¿([^?¿]+)\?", re.DOTALL)

# Fillers conversacionales en español (no son preguntas reales)
EXTRA_FILLERS = {
    "qué tal", "que tal", "bueno", "sí", "si", "vale", "no", "ok",
    "qué", "que", "pues", "ah", "eh", "mm", "ajá", "aja",
    "verdad", "no sé", "no se", "claro", "cierto", "ya", "ok ok",
    "qué onda", "que onda", "y luego", "y tú", "verdad si te suena",
    "te suena", "ahí va", "ahí está", "está bien", "ok va",
    "no?", "okey",
}

# Interrogativos con acento (interrogación clara en cualquier posición)
STRONG_INTERROGATIVES_ACCENTED = {
    "qué", "cómo", "cuándo", "dónde", "quién", "cuál", "cuáles",
    "cuánto", "cuánta", "cuántos", "cuántas",
}
# Interrogativos sin acento (sólo cuentan al INICIO de la pregunta)
STRONG_INTERROGATIVES_FIRST = STRONG_INTERROGATIVES_ACCENTED | {
    "que", "como", "cuando", "donde", "quien", "cual", "cuales",
    "cuanto", "cuanta", "cuantos", "cuantas",
}
# Backward-compat alias
STRONG_INTERROGATIVES = STRONG_INTERROGATIVES_FIRST

# Verbos comunes en preguntas de entrevista (whole-word match, NO substring:
# "es" no debe matchear dentro de "este"/"eventos"). Lista curada y generosa
# de 2ª/3ª persona + cópulas. Ampliar aquí cuando un proyecto revele un verbo
# de entrevista frecuente que se estaba perdiendo.
INTERVIEW_VERBS = {
    # arranque / trayectoria
    "empezaste", "empezaron", "empezó", "comenzaste", "iniciaste", "naciste",
    "estudiaste", "estudias", "estudiaron", "trabajaste", "trabajas", "trabajan",
    "llegaste", "llegas", "llegó", "decidiste", "decides", "conociste",
    "conocieron", "conoces", "conocen",
    # hacer / dedicarse
    "hace", "haces", "hacen", "hiciste", "tocas", "toca", "tocan",
    "dedicas", "dedican", "encargas", "encarga", "organizas", "organiza",
    "manejas", "maneja", "presentas", "presenta", "produces", "creas",
    "compones", "grabas", "mezclas",
    # opinión / sentir
    "gusta", "gustan", "gustaría", "prefieres", "prefieren", "quieres",
    "quieren", "piensas", "piensan", "crees", "creen", "sientes", "sienten",
    "opinas", "consideras", "imaginas", "recuerdas", "sueñas", "esperas",
    # decir / describir
    "dirías", "dirían", "definirías", "defines", "describirías", "describes",
    "explicarías", "explicas", "cuentas", "cuenta", "platicas", "platica",
    "hablas", "habla", "ves", "sabes", "sabe",
    # poder / buscar / vivir
    "podrías", "puedes", "pueden", "buscas", "buscan", "vives", "vive",
    "sigues", "sigue", "eliges", "escoges", "lograste", "lograron", "logras",
    "vienes", "vienen", "viniste", "tienes", "tienen", "promueves", "promover",
    # cópulas / ser-estar (whole-word)
    "es", "son", "está", "están", "estás", "eras", "fue", "fuiste", "fueron",
    "será", "sería",
}

# Frases verbales multi-palabra (se buscan como substring porque incluyen clítico)
INTERVIEW_VERB_PHRASES = {
    "te dedicas", "se dedican", "te encargas", "te dedicaste",
    "te inspira", "te inspiras", "te define", "te llevó", "te gusta",
}

FIRST_PERSON_MARKERS = {"me", "mi", "mis", "yo", "mío", "míos", "mía"}

LEGAL_SHORT = {"y", "a", "o", "u", "e", "i", "no", "sí", "si",
               "ya", "yo", "tú", "tu", "el", "la", "lo", "te",
               "se", "me", "le", "es", "ir", "ah", "eh", "en",
               "de", "un", "ti", "mi", "ni", "al"}

# Palabras con patrones claramente no españoles (alucinaciones de Whisper)
SUSPICIOUS_PATTERNS = [
    "ww", "jj", "qq", "vv", "kk", "yy", "uu", "ii", "xx",
    "kh", "wh", "th", "ph", "gh", "ck",
]

# Cuántas palabras iniciales de una oración se inspeccionan para aceptarla
# como pregunta "loose" (interrogativo front-loaded). Los prompts del
# entrevistador ponen el interrogativo al frente ("de cómo empezaste...");
# un interrogativo embebido tardío suele ser oración relativa del entrevistado
# ("...les vengo a explicar cómo va a estar el evento") y NO debe contar.
LOOSE_HEAD_WINDOW = 6

# Tope de palabras para una pregunta "loose" sin signos `¿?`. Las preguntas
# reales del entrevistador son concisas; un "interrogativo" dentro de una
# oración de 30-50 palabras es monólogo descriptivo del entrevistado
# ("cómo va cambiando, cómo se abren muros, cómo el piso es distinto..."),
# no una pregunta. Las `¿...?` explícitas NO llevan este tope.
LOOSE_MAX_WORDS = 22


# ── Normalización y filtro estricto ─────────────────────────────────────────

def normalize_question(q: str) -> str:
    """Normaliza para deduplicación. Lowercase, strip, sin signos.

    OJO con el orden (arreglado 2026-07-27): hay que colapsar los espacios
    ANTES de quitar los signos. Al revés, un espacio sobrante al inicio o al
    final deja el `¿`/`?` sin quitar —porque el espacio no está en el set de
    strip— y dos escrituras de la misma pregunta NO se deduplican.
    """
    return " ".join(q.lower().split()).strip(".,?¿!¡;:'\"-")


def is_real_question_q(q: str) -> bool:
    """Filtro estricto: rechaza fillers, frases ininteligibles, preguntas
    semánticamente inválidas.

    Reglas (iter9.4 → iter10):
    1. Norm = lowercase + sin signos. Rechazar si está en EXTRA_FILLERS.
    2. Mínimo 4 palabras significativas Y debe empezar con interrogativo,
       O mínimo 5 palabras Y contener interrogativo ACENTUADO en alguna posición.
    3. Rechazar palabras duplicadas adyacentes ("te te", "qué qué").
    4. Rechazar >25% palabras cortas no comunes (short_garbage).
    5. Debe contener verbo de entrevista.
    6. Rechazar primera persona (entrevistado, no entrevistador).
    7. Rechazar duplicación de trigramas + palabras no españolas.
    """
    norm = q.strip().lower().rstrip(".!?¿¡ ")
    if norm in EXTRA_FILLERS:
        return False
    words = norm.split()
    n = len(words)
    if n < 3:
        return False

    # 3. Duplicación adyacente — basura de Whisper
    for i in range(len(words) - 1):
        if words[i] == words[i + 1] and len(words[i]) >= 2:
            return False

    # 2. Debe COMENZAR con interrogativo claro, o tener interrogativo acentuado mid.
    #    "que" sin tilde es relativo, no interrogativo → sólo acentuados cuentan mid.
    first = words[0]
    starts_with_interrog = first in STRONG_INTERROGATIVES_FIRST or (
        first == "y" and len(words) > 1 and words[1] in STRONG_INTERROGATIVES_FIRST
    )
    has_interrog_mid = bool(set(words) & STRONG_INTERROGATIVES_ACCENTED)

    if not starts_with_interrog and not has_interrog_mid:
        return False

    # Preguntas cortas (3-4 palabras): deben EMPEZAR con interrogativo
    if n <= 4 and not starts_with_interrog:
        return False

    # 4. Short garbage check (permitir números)
    def looks_legal(w: str) -> bool:
        if len(w) > 2:
            return True
        if w in LEGAL_SHORT:
            return True
        if w.replace(",", "").replace(".", "").isdigit():
            return True
        return False
    short_garbage = sum(1 for w in words if not looks_legal(w))
    if short_garbage / n > 0.25:
        return False

    # 5. La pregunta debe contener verbo de entrevista. WHOLE-WORD para los
    #    verbos sueltos ("es" NO debe matchear dentro de "este"/"eventos");
    #    substring sólo para las frases con clítico ("te dedicas").
    clean = {w.strip(",.;:!?¿¡()\"'«»…") for w in words}
    has_verb = bool(clean & INTERVIEW_VERBS) or any(p in norm for p in INTERVIEW_VERB_PHRASES)
    if not has_verb:
        return False

    # 6. Rechazar primera persona singular (del entrevistado)
    first_person_count = sum(1 for w in words if w in FIRST_PERSON_MARKERS)
    if first_person_count >= 2:
        return False
    if "me gustaría" in norm or "me gusta" in norm or "yo creo" in norm:
        return False

    # 7a. Rechazar duplicación de trigrama (no adyacente)
    if n >= 6:
        trigrams = [" ".join(words[i:i + 3]) for i in range(n - 2)]
        tc = Counter(trigrams)
        if any(c > 1 for c in tc.values()):
            return False

    # 7b. Rechazar palabras no españolas (alucinaciones de Whisper)
    for w in words:
        for pat in SUSPICIOUS_PATTERNS:
            if pat in w:
                return False

    return True


# ── Segmentación de oraciones ───────────────────────────────────────────────

_SENT_ENDERS = ".!?…"


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Parte `text` en oraciones devolviendo (char_start, char_end, texto).

    char_start apunta al primer carácter NO-espacio de la oración (para que
    el mapeo a timestamps por posición funcione igual que con `¿...?`).
    """
    out: list[tuple[int, int, str]] = []
    n = len(text)
    start = 0
    i = 0
    while i < n:
        if text[i] in _SENT_ENDERS:
            j = i + 1
            while j < n and text[j] in _SENT_ENDERS:
                j += 1
            seg = text[start:j]
            stripped = seg.strip()
            if stripped:
                lead = len(seg) - len(seg.lstrip())
                out.append((start + lead, j, stripped))
            while j < n and text[j].isspace():
                j += 1
            start = j
            i = j
        else:
            i += 1
    if start < n:
        seg = text[start:]
        stripped = seg.strip()
        if stripped:
            lead = len(seg) - len(seg.lstrip())
            out.append((start + lead, n, stripped))
    return out


def _head_has_interrogative(sentence: str, window: int = LOOSE_HEAD_WINDOW) -> bool:
    """True si un interrogativo ACENTUADO (qué/cómo/cuándo/dónde/quién/cuál/
    cuánto) aparece en las primeras `window` palabras.

    SÓLO acentuados: en español el interrogativo lleva tilde ("cómo empezaste")
    y el relativo NO ("como te decía", "que quiero resaltar"); Whisper respeta
    esa distinción aunque omita los signos `¿?`. Aceptar "como/que" sin tilde
    al inicio metía monólogo del entrevistado como falsas preguntas."""
    toks = [w.strip(".,?¿!¡;:'\"-()…").lower() for w in sentence.split()]
    toks = [w for w in toks if w]
    head = toks[:window]
    return bool(set(head) & STRONG_INTERROGATIVES_ACCENTED)


def _overlaps(s: int, e: int, spans: list[tuple[int, int, str]]) -> bool:
    for (os, oe, _q) in spans:
        if not (e <= os or s >= oe):
            return True
    return False


def split_questions(text: str, loose: bool = True) -> list[tuple[int, int, str]]:
    """Devuelve [(char_start, char_end, question_text), ...] de preguntas reales.

    Combina:
      a) Preguntas explícitas `¿...?` (siempre).
      b) Si `loose`: oraciones con interrogativo acentuado front-loaded
         (prompts del entrevistador que Whisper no marcó con `¿?`).

    Todos los candidatos se filtran con `is_real_question_q()`. El llamador
    recibe spans ordenados por posición; los timestamps se resuelven después
    vía `map_chars_to_words` / `find_question_start_time`.
    """
    explicit: list[tuple[int, int, str]] = []
    for m in RE_QUESTION.finditer(text):
        explicit.append((m.start(), m.end(), m.group(1).strip()))

    candidates = list(explicit)
    if loose:
        for (s, e, sent) in split_sentences(text):
            if _overlaps(s, e, explicit):
                continue
            if len(sent.split()) > LOOSE_MAX_WORDS:
                continue  # run-on = monólogo descriptivo, no pregunta
            if _head_has_interrogative(sent):
                candidates.append((s, e, sent))

    candidates.sort(key=lambda x: x[0])
    accepted = [(s, e, q) for (s, e, q) in candidates if is_real_question_q(q)]

    # Preguntas en INGLÉS (MAB 2026-06-12: vox pop mundialista es políglota —
    # los sudafricanos quedaron invisibles para los patrones españoles).
    # Aditivo: en texto español casi nunca dispara (exige head interrogativo
    # inglés + terminar en '?'), así que es seguro correrlo siempre.
    for (s, e, q) in _split_questions_en(text):
        if not _overlaps(s, e, accepted):
            accepted.append((s, e, q))
    accepted.sort(key=lambda x: x[0])
    return accepted


EN_HEADS = {
    "what", "how", "why", "where", "who", "when", "which", "whose",
    "do", "does", "did", "are", "is", "was", "were", "have", "has",
    "can", "could", "would", "will", "should", "any",
}

RE_QUESTION_EN = re.compile(r"([A-Z][^.?!¿]{4,160}\?)")


def _split_questions_en(text: str) -> list[tuple[int, int, str]]:
    """Preguntas en inglés: oración que termina en '?' con head interrogativo
    o auxiliar inglés en las primeras 2 palabras. Filtro ligero: >=3 palabras,
    sin duplicados adyacentes (basura Whisper)."""
    out = []
    for m in RE_QUESTION_EN.finditer(text):
        q = m.group(1).strip()
        words = [w.strip(".,?!'\"").lower() for w in q.split()]
        if len(words) < 3:
            continue
        if not (set(words[:2]) & EN_HEADS):
            continue
        if any(words[i] == words[i + 1] and len(words[i]) >= 2
               for i in range(len(words) - 1)):
            continue
        out.append((m.start(), m.end(), q))
    return out
