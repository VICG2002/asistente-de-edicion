"""Beats de entrevista: separar PREGUNTA de RESPUESTA, y marcar pausas,
palabras clave y candidatos a momento emocional.

El problema que resuelve
------------------------
Hoy `question_segments` emite UN tramo por pregunta que va desde el inicio de
esa pregunta hasta el inicio de la siguiente. Es decir: un solo marker morado
cubre pregunta Y respuesta, y el texto de la respuesta va comprimido en la nota.
El editor no puede saltar a donde empieza a responder — que es justo el punto
donde va a cortar.

Como se separa (tres fuentes, de mas fuerte a mas debil)
-------------------------------------------------------
1. `master_src` — el master transcript trae `words = [[palabra, t, fuente]]`.
   Con dos TX (entrevistador y entrevistado con lavalier propio) el cambio de
   hablante es FISICO, no una heuristica: cambia la fuente. Es el caso de
   FILM CLUB CAFE y FANTASTICO COMICS.
2. `silencio` — el primer silencio real (ffmpeg silencedetect, tabla
   `clip_silences`) despues de la pregunta. En una entrevista siempre hay un
   hueco entre pregunta y respuesta.
3. `estimado` — ultimo recurso: se estima cuanto tardo en decirse la pregunta
   por su numero de palabras. Queda marcado con confianza baja para que el
   editor sepa que ese limite es aproximado.

Todo lo que sale de aqui es DERIVADO de datos que ya existen en el manifest.
No se inventa nada: sin transcript verificable no hay beats.
"""

from __future__ import annotations

import re
import unicodedata

# --- umbrales ------------------------------------------------------------
MIN_PALABRAS_OTRA_FUENTE = 3    # para creerle a un cambio de fuente
VENTANA_CAMBIO_FUENTE = 25.0    # s tras la pregunta donde buscar la respuesta
MIN_PREGUNTA_SEC = 0.8          # una pregunta no dura menos que esto
MIN_RESPUESTA_SEC = 1.0
PAUSA_MIN_SEC = 2.0             # pausa que vale un marker
PAUSA_EMOCION_SEC = 3.5         # pausa larga dentro de una respuesta
VELOCIDAD_HABLA = 2.8           # palabras/segundo (español conversacional)
MAX_KEYWORDS_POR_CLIP = 12

KINDS = ("pregunta", "respuesta", "pausa", "keyword", "emocion")

# Grupos de `sound_events` que sugieren carga emocional. NO son verdad: son
# candidatos que el editor confirma al ver el clip.
GRUPOS_EMOCION = {
    "laughter": ("risa", 0.6),
    "applause": ("aplauso", 0.5),
    "crying":   ("llanto", 0.7),
}


def _norm(w: str) -> str:
    w = unicodedata.normalize("NFKD", w or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", w.lower())


def beat(kind, start, end, *, text="", speaker="", confidence=1.0,
         source="", q_index=None) -> dict:
    return {
        "kind": kind,
        "start_sec": float(start),
        "end_sec": float(end),
        "text": text or "",
        "speaker": speaker or "",
        "confidence": float(confidence),
        "source": source or "",
        "q_index": q_index,
    }


# --------------------------------------------------------------------------
# 1. Donde empieza la respuesta
# --------------------------------------------------------------------------

def onset_por_fuente(words_src, q_start: float, q_end: float):
    """Primer momento en que habla una fuente distinta a la de la pregunta.

    `words_src` es `master["words"]` = [[palabra, t, fuente], ...].
    Devuelve (onset, fuente_pregunta, fuente_respuesta) o None.

    Exige `MIN_PALABRAS_OTRA_FUENTE` palabras seguidas de la otra fuente: una
    sola palabra suelta suele ser sangrado del microfono, no un cambio de
    hablante.
    """
    if not words_src:
        return None
    # Fuente de la pregunta: la de la primera palabra en/despues de q_start.
    src_q = None
    for w in words_src:
        if not (isinstance(w, (list, tuple)) and len(w) >= 3):
            continue
        if w[1] >= q_start - 0.01:
            src_q = w[2]
            break
    if src_q is None:
        return None

    limite = min(q_end, q_start + VENTANA_CAMBIO_FUENTE)
    corrida = []
    for w in words_src:
        if not (isinstance(w, (list, tuple)) and len(w) >= 3):
            continue
        _pal, t, src = w[0], w[1], w[2]
        if t <= q_start:
            continue
        if t > limite:
            break
        if src != src_q:
            corrida.append((t, src))
            if len(corrida) >= MIN_PALABRAS_OTRA_FUENTE:
                return corrida[0][0], src_q, corrida[0][1]
        else:
            corrida = []
    return None


def onset_por_silencio(silences, q_start: float, q_end: float):
    """Fin del primer silencio util despues de que arranca la pregunta."""
    mejor = None
    for s, e in silences:
        if s <= q_start + 0.5:
            continue
        if s >= q_end:
            break
        if e - s < 0.6:
            continue
        mejor = e
        break
    if mejor is None:
        return None
    return min(mejor, q_end)


def onset_estimado(q_texto: str, q_start: float, q_end: float) -> float:
    """Cuanto tarda en decirse la pregunta, por numero de palabras."""
    n = len([p for p in (q_texto or "").split() if p])
    dur = max(MIN_PREGUNTA_SEC, n / VELOCIDAD_HABLA)
    return min(q_start + dur, q_end)


def separar_pregunta_respuesta(q, words_src, silences):
    """Devuelve (onset, confianza, fuente_metodo, hablante_pregunta,
    hablante_respuesta) para una fila de `question_segments`."""
    q_start, q_end = float(q["start_sec"]), float(q["end_sec"])

    # El arranque CURADO manda. Con dos lavalieres en la misma mesa cada uno oye
    # a los dos, y Whisper transcribe tambien lo que entra de sangrado: en el
    # master cada fuente trae las palabras de todos, el "cambio de fuente"
    # aparece al segundo de empezar la pregunta y el azul cae a media pregunta
    # (Asistente, 2026-09-29). El curador lo fija con el nivel de cada TX.
    curado = q.get("answer_sec")
    if curado is not None and q_start < float(curado) < q_end:
        return float(curado), 1.0, "curado", "", ""

    r = onset_por_fuente(words_src, q_start, q_end)
    if r is not None:
        onset, src_q, src_r = r
        if q_start + MIN_PREGUNTA_SEC <= onset <= q_end - MIN_RESPUESTA_SEC:
            return onset, 0.9, "master_src", src_q, src_r

    onset = onset_por_silencio(silences, q_start, q_end)
    if onset is not None and q_start + MIN_PREGUNTA_SEC <= onset <= q_end - MIN_RESPUESTA_SEC:
        return onset, 0.6, "silencio", "", ""

    onset = onset_estimado(q.get("question_text") or q.get("question") or "",
                           q_start, q_end)
    if onset > q_end - MIN_RESPUESTA_SEC:
        onset = q_start + (q_end - q_start) * 0.35
    return onset, 0.3, "estimado", "", ""


# --------------------------------------------------------------------------
# 2. Palabras clave del proyecto
# --------------------------------------------------------------------------

def buscar_keywords(words, vocabulario, *, maximo=MAX_KEYWORDS_POR_CLIP):
    """Primera aparicion de cada termino del vocabulario en el transcript.

    `words` = [[palabra, t], ...]. `vocabulario` = set de terminos (pueden ser
    de 1 a 3 palabras, p.ej. "ENTREVISTADO_1"). Solo la PRIMERA aparicion de cada
    termino: marcar las 40 veces que alguien dice un nombre es ruido.
    """
    if not words or not vocabulario:
        return []
    # Indexar terminos por su forma normalizada y por cuantas palabras tienen.
    por_n = {}
    for termino in vocabulario:
        partes = [_norm(p) for p in str(termino).split() if _norm(p)]
        if not partes or len(partes) > 3:
            continue
        por_n.setdefault(len(partes), {})["".join(partes)] = str(termino)

    planas = []
    for w in words:
        if isinstance(w, (list, tuple)) and len(w) >= 2:
            planas.append((_norm(w[0]), float(w[1]), str(w[0])))

    vistos = set()
    out = []
    for i in range(len(planas)):
        for n in sorted(por_n):
            if i + n > len(planas):
                continue
            clave = "".join(p[0] for p in planas[i:i + n])
            if not clave:
                continue
            termino = por_n[n].get(clave)
            if termino and termino not in vistos:
                vistos.add(termino)
                t0 = planas[i][1]
                t1 = planas[i + n - 1][1] + 0.5
                out.append((t0, t1, termino))
                break
        if len(out) >= maximo:
            break
    return out


# --------------------------------------------------------------------------
# 3. Beats de un clip
# --------------------------------------------------------------------------

def beats_de_clip(*, questions, words_src=None, words_plain=None,
                  silences=None, vocabulario=None, sound_events=None,
                  clip_dur=0.0) -> list:
    """Arma todos los beats de un clip.

    questions    filas de question_segments como dicts (start_sec, end_sec,
                 question_text, question_short, response_summary)
    words_src    master["words"] = [[palabra, t, fuente]] (opcional)
    words_plain  [[palabra, t]] del transcript llano (opcional)
    silences     [(start, end)] en tiempo de VIDEO
    vocabulario  set de terminos del proyecto
    sound_events [(start, end, grupo, conf)]
    """
    questions = sorted(questions or [], key=lambda q: float(q["start_sec"]))
    silences = sorted(silences or [])
    out = []

    spans_respuesta = []
    for qi, q in enumerate(questions):
        q_start, q_end = float(q["start_sec"]), float(q["end_sec"])
        if q_end <= q_start:
            continue
        onset, conf, metodo, src_q, src_r = separar_pregunta_respuesta(
            q, words_src, silences)

        texto_q = (q.get("question_short") or "").strip() or \
                  (q.get("question_text") or "").strip()
        out.append(beat("pregunta", q_start, onset, text=texto_q,
                        speaker=src_q, confidence=conf, source=metodo,
                        q_index=qi))
        out.append(beat("respuesta", onset, q_end,
                        text=(q.get("response_summary") or "").strip(),
                        speaker=src_r, confidence=conf, source=metodo,
                        q_index=qi))
        spans_respuesta.append((onset, q_end, qi))

    # --- pausas: solo DENTRO de una respuesta ---------------------------
    # Una pausa mientras se formula la pregunta no es un punto de corte.
    for s, e in silences:
        if e - s < PAUSA_MIN_SEC:
            continue
        for r0, r1, qi in spans_respuesta:
            if s >= r0 and e <= r1:
                out.append(beat("pausa", s, e,
                                text=f"pausa de {e - s:.1f}s",
                                confidence=0.8, source="silencedetect",
                                q_index=qi))
                if e - s >= PAUSA_EMOCION_SEC:
                    out.append(beat(
                        "emocion", s, e,
                        text=f"candidato: pausa larga ({e - s:.1f}s) en la "
                             f"respuesta — duda o quiebre",
                        confidence=0.4, source="silencio_largo", q_index=qi))
                break

    # --- palabras clave --------------------------------------------------
    palabras = words_plain
    if not palabras and words_src:
        palabras = [[w[0], w[1]] for w in words_src
                    if isinstance(w, (list, tuple)) and len(w) >= 2]
    for t0, t1, termino in buscar_keywords(palabras or [], vocabulario or set()):
        out.append(beat("keyword", t0, t1, text=termino,
                        confidence=0.7, source="vocabulario"))

    # --- eventos sonoros con carga emocional -----------------------------
    for ev in (sound_events or []):
        s, e, grupo, conf = ev[0], ev[1], ev[2], (ev[3] if len(ev) > 3 else 0.0)
        etiqueta = GRUPOS_EMOCION.get(grupo)
        if not etiqueta:
            continue
        nombre, base = etiqueta
        out.append(beat("emocion", s, e, text=f"candidato: {nombre}",
                        confidence=min(1.0, base * max(conf, 0.3) * 2),
                        source="panns"))

    # Recortar al rango del clip y ordenar.
    limpios = []
    for b in out:
        if clip_dur > 0:
            b["start_sec"] = max(0.0, min(b["start_sec"], clip_dur))
            b["end_sec"] = max(0.0, min(b["end_sec"], clip_dur))
        if b["end_sec"] <= b["start_sec"]:
            continue
        limpios.append(b)
    limpios.sort(key=lambda b: (b["start_sec"], KINDS.index(b["kind"])))
    for i, b in enumerate(limpios):
        b["beat_index"] = i
    return limpios
