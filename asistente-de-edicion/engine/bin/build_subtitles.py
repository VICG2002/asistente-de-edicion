#!/usr/bin/env python3
"""Genera subtítulos SRT de un export (o cualquier video) — 100% local.

Doctrina (The Avalanches 2026-07-21): subtitular un export con música alta
tiene tres trampas que Whisper NO resuelve solo, y que este script sí:

  1. ARRANQUE ESTIRADO. Si el video abre con música/ambiente, Whisper estira
     el primer subtítulo desde 00:00 hasta que empieza el habla (25 s en BROD)
     y a veces alucina un "¡Suscríbete al canal!" encima. Se detecta por CPS
     absurdo (<3 car/s) y se corrige re-transcribiendo ventanas cortas hasta
     hallar el onset real del habla.
  2. ALUCINACIONES DE COLA. Sobre música sin voz Whisper inventa "Gracias por
     ver el video". Se recortan con las frases de lib/transcript_quality.
  3. GARBLES DE NOMBRES PROPIOS. "Sinside F.U." = "Since I Left You". Se
     corrigen con el vocabulario del proyecto (project_config.json).

Además aplica reglas de subtitulaje que Whisper ignora: ≤42 car/línea, ≤2
líneas, CPS ≤ 17, duración 1–6 s, y GAP-OUT (un subtítulo no se queda colgado
en pantalla durante una pausa larga: sale 0.4 s después de la última palabra).

  4. EL CORTE MANDA (--timeline, desde 2026-08-19). Con `--timeline` lee de la
     base de disco de Resolve la timeline de la que salió el export y usa sus
     cortes. No todos: solo los que TAMBIÉN cortan el diálogo. Medido en Morsa
     sobre 995.7 s de corte —

         corte sincrónico (imagen+sonido)   77   cada 7.44 s   cae en pausa 31 %
         corte de solo imagen              289   cada 2.04 s   cae en pausa 15 %
         punto al azar                       —        —        cae en pausa  9 %

     — el sincrónico acierta la pausa 3.4 veces más que el azar y llega con la
     cadencia de un cue; el de solo imagen está al nivel del azar y llega cada
     dos segundos, porque ahí el audio sigue corriendo por debajo: eso es el
     voice over, y ahí el corte no sabe nada del habla.

     Son dos reglas: ningún cue CRUZA un corte sincrónico, y un borde que ya
     caía a menos de 10 fotogramas de uno se CLAVA a su fotograma. Nunca se
     crea un borde donde el habla no lo pedía.

Uso:
    bin/build_subtitles.py --video export.mov
    bin/build_subtitles.py --video export.mov --offset-tc 01:06:06:04 --fps 23.976
    bin/build_subtitles.py --video export.mov --config <disco>/.cinema_assistant/project_config.json
    bin/build_subtitles.py --video export.mov --config … --timeline "Cut 1.2 sonido ayan"
    bin/build_subtitles.py --video export.mov --config … --desde 00:00:23:03

Salidas: <video>.srt (o --out) y, con --lua, el .lua para colocación exacta
en Resolve vía resolve/place_subtitles.lua (evita el arrastre a ojo).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.transcript_quality import HALLUCINATION_PHRASES  # noqa: E402
from lib.subtitulos import (                                    # noqa: E402
    AIRE_CORTE_F, ALTO_LINEA_REL, COLGANTES, GAP_OUT, MARGEN_SEGURO,
    MAX_CHARS_LINE, MAX_CPS, MAX_DUR,
    MAX_LINES, MIN_DUR, TOLERANCIA_CORTE_F, Cue, cabe_en_dos_lineas,
    cap_total, chars_por_linea, configurar, cuelga,
    imantar, mejor_reparto, partir_en_cortes, wrap_lines,
)

# Las reglas de reparto y las constantes viven en lib/subtitulos.py desde
# 2026-08-19, para que `verify_subtitulos.py` compruebe EXACTAMENTE las mismas
# que este script aplica. Se reexportan con el nombre de siempre.
_cuelga = cuelga

MODEL_DEFAULT = Path.home() / "cinema-assistant/models/ggml-large-v3-turbo.bin"
VAD_MODEL = Path.home() / "cinema-assistant/models/ggml-silero-v5.1.2.bin"

SUSPECT_CPS = 3.0      # por debajo de esto, el timing no es de fiar


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def extract_audio(video: Path, out: Path, start: float = 0.0,
                  dur: float | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def duracion_video(video: Path) -> float:
    """Segundos del archivo, para poder decir cuanto quedo sin subtitular."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "0").strip() or 0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0.0


def dimensiones_video(video: Path) -> tuple[int, int]:
    """Ancho y alto en pixeles. Deciden cuantos caracteres caben por linea sin
    llegar al borde: en 9:16 el ancho ata muy por debajo de los 42 de lectura."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=120)
        w, _, h = (r.stdout or "").strip().partition(",")
        return int(w), int(h)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0, 0


def fps_video(video: Path) -> float:
    """Frame rate real del archivo. El TC de pegado se calcula con esto: con un
    fps supuesto, el playhead cae en el fotograma equivocado y el subtitulo
    entra corrido (0.130 s son el frame 3 a 23.976 y el 4 a 29.97)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=120)
        num, _, den = (r.stdout or "").strip().partition("/")
        f = float(num) / float(den or 1)
        return f if 1.0 < f < 1000.0 else 0.0
    except (subprocess.TimeoutExpired, FileNotFoundError,
            ValueError, ZeroDivisionError):
        return 0.0


def whisper(wav: Path, model: Path, lang: str, *, vad: bool,
            word_level: bool = False) -> list[dict]:
    """Devuelve [{start, end, text}] en segundos."""
    with tempfile.TemporaryDirectory() as td:
        of = Path(td) / "out"
        cmd = [
            "whisper-cli", "-m", str(model), "-f", str(wav), "-l", lang,
            "-oj", "-of", str(of),
            "-ml", "1" if word_level else "42",
        ]
        if not word_level:
            cmd.append("-sow")
        if vad and VAD_MODEL.exists():
            cmd += ["--vad", "-vm", str(VAD_MODEL), "-vt", "0.5",
                    "-vsd", "200", "-vp", "60"]
        subprocess.run(cmd, capture_output=True, text=True)
        jf = of.with_suffix(".json")
        if not jf.exists():
            return []
        data = json.loads(jf.read_text())
    out = []
    for s in data.get("transcription", []):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        o = s["offsets"]
        out.append({"start": o["from"] / 1000, "end": o["to"] / 1000, "text": txt})
    return out


def is_hallucination(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in HALLUCINATION_PHRASES)


def find_speech_onset(video: Path, model: Path, lang: str,
                      first_words: str, hi: float, *, step: float = 2.0) -> float:
    """Busca el inicio REAL del habla probando ventanas de 4 s.

    `first_words` = primeras palabras de contenido del primer subtítulo.
    Devuelve el t más tardío cuya ventana todavía contiene esas palabras
    (= el habla empieza ahí), o 0.0 si no se puede determinar.
    """
    key = normalize(first_words)[:18]
    if not key:
        return 0.0
    best = 0.0
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "probe.wav"
        t = 0.0
        while t <= hi:
            extract_audio(video, w, start=t, dur=4.0)
            segs = whisper(w, model, lang, vad=False)
            txt = normalize(" ".join(s["text"] for s in segs))
            if key in txt:
                best = t
                t += step
            else:
                # ya pasamos el onset: afinar hacia atrás con medio paso
                if best and step > 0.5:
                    return find_speech_onset(video, model, lang, first_words,
                                             min(hi, best + step), step=step / 2)
                break
    return best


def normalize(s: str) -> str:
    s = s.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9ñ ]+", "", s).strip()


def load_vocab(config: Path | None) -> list[tuple[re.Pattern, str]]:
    """Reglas de corrección desde el project_config.

    Lee las TRES formas en que el vocabulario de un proyecto puede estar escrito,
    porque los scripts del motor no coincidian y este se quedaba sin ninguna:

      "vocabulary_fixes": {"Jimena": "Ximena"}   <- lo que usa
                                                    correct_transcripts_vocab.py
      "vocabulary_hints": ["Mexican Chic", ...]  <- nombres propios del proyecto
      "vocabulary_notes": "garble1/garble2 = Correcto"  <- prosa de la curaduría

    Hasta 2026-08-07 sólo leía `vocabulary_notes`. IMODAE tenía sus correcciones
    en `vocabulary_fixes` y los subtítulos salieron con "Jimena" donde el
    proyecto canoniza "Ximena", diciendo "0 reglas de corrección" sin que eso
    pareciera un problema.
    """
    rules = []
    if not config or not config.exists():
        return rules
    try:
        cfg = json.loads(config.read_text())
    except (json.JSONDecodeError, OSError):
        return rules

    for bad, good in (cfg.get("vocabulary_fixes") or {}).items():
        bad, good = str(bad).strip(), str(good).strip()
        # La comparacion es EXACTA, no normalizada. Comparar sin acentos
        # descartaba en silencio las correcciones de tilde —"closet" ->
        # "clóset"— que en español son la mitad del trabajo: la regla se leia
        # como identidad y se tiraba, y el contador anunciaba una regla menos
        # sin decir cual (IMODAE, Reel 4, 2026-08-13).
        if len(bad) >= 3 and good and bad != good:
            # \b para no destrozar palabras que contengan el garble.
            rules.append((re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE), good))

    # Los hints no son reglas de sustitución: son la grafía canónica. Sirven para
    # arreglar la CAPITALIZACIÓN y los acentos de un término que Whisper escribió
    # bien pero mal escrito ("mexican chic" -> "Mexican Chic").
    for hint in (cfg.get("vocabulary_hints") or []):
        hint = str(hint).strip()
        if len(hint) >= 4:
            rules.append((re.compile(rf"\b{re.escape(hint)}\b", re.IGNORECASE), hint))

    # Las notas se parten primero por `;` y saltos de linea. Partir tambien por
    # ". " —como se hacia— destroza justo los garbles que llevan abreviatura,
    # que son los mas frecuentes en nombres propios: el ejemplo del docstring de
    # este archivo ("Sinside F.U. = Since I Left You") nunca llego a aplicarse
    # porque se partia en dos mitades sin `=` utilizable. Solo se recurre al
    # punto cuando un trozo trae mas de una regla.
    notes = cfg.get("vocabulary_notes", "")
    trozos = []
    for t in re.split(r"[;\n]+", notes):
        trozos.extend(re.split(r"\.\s+", t) if t.count("=") > 1 else [t])
    for chunk in trozos:
        if "=" not in chunk:
            continue
        left, right = chunk.split("=", 1)
        good = right.strip().strip("'\"").split(" (")[0].strip()
        if not good or len(good) < 3:
            continue
        for bad in left.split("/"):
            bad = bad.strip().strip("'\"")
            if len(bad) < 4 or normalize(bad) == normalize(good):
                continue
            rules.append((re.compile(re.escape(bad), re.IGNORECASE), good))
    return rules


def apply_vocab(text: str, rules) -> str:
    for pat, good in rules:
        text = pat.sub(good, text)
    return text


def to_sentences(segs: list[dict]) -> list[dict]:
    """Reagrupa los segmentos de Whisper (que cortan por nº de caracteres)
    en FRASES completas. Sin esto salen cortes como '…por la pureza del' /
    'sampleo.' — el defecto más visible de un SRT automático.

    Cada frase se lleva sus `piezas`: los segmentos originales con sus tiempos.
    Los necesita `partir_en_cortes` para saber DÓNDE dentro de la frase cae un
    corte de la timeline. Sin ellas, el reparto del texto es una regla de tres
    sobre el tiempo, y una frase con una pausa larga dentro la desmiente."""
    out = []
    for s in segs:
        txt = s["text"].strip()
        pieza = {"start": s["start"], "end": s["end"], "text": txt}
        if out and not re.search(r"[.!?…]$", out[-1]["text"]):
            out[-1]["text"] = (out[-1]["text"] + " " + txt).strip()
            out[-1]["end"] = s["end"]
            out[-1]["piezas"].append(pieza)
        else:
            out.append({"start": s["start"], "end": s["end"], "text": txt,
                        "piezas": [pieza]})
    return out


def split_long(sent: dict) -> list[dict]:
    """Parte una frase larga en varios cues, cortando en coma/espacio y
    repartiendo el tiempo en proporción a los caracteres."""
    text = " ".join(sent["text"].split())
    cap = cap_total()
    # El tope de 84 caracteres es necesario pero NO suficiente: un texto de 78
    # con las palabras mal repartidas tampoco entra en dos líneas de 42 (78
    # caracteres que sólo se pueden cortar en 34+43). Por eso se pregunta si de
    # verdad cabe, en vez de fiarse del conteo.
    n = max(1, -(-len(text) // cap))               # nº de trozos por longitud
    # Un texto que no llega al tope pero tampoco entra en dos líneas necesita
    # partirse igual. Contar sólo por `cap` dejaba n=1 y el trozo salía intacto,
    # con su línea de 48 caracteres (IMODAE 2026-08-07).
    if n == 1 and not cabe_en_dos_lineas(text):
        n = 2
    if n == 1:
        return [dict(sent, text=text)]
    target = len(text) / n
    parts, pos = [], 0
    for k in range(n):
        if k == n - 1:
            parts.append(text[pos:].strip())
            break
        want = pos + int(target)
        # Prioridad de corte: fin de frase > coma > espacio. Y si el trozo
        # quedara terminado en una palabra colgante ("…y la"), se retrocede al
        # espacio anterior hasta encontrar uno que no cuelgue.
        #
        # LA COMA SE QUEDA. Hasta 2026-08-19 el trozo izquierdo salía con
        # `.strip(",")` y "…en San Ángel," se entregaba sin la coma. Netflix ES
        # y la UNE 153010 dicen lo contrario: la puntuación de la frase se
        # conserva, y una coma al final de un cue es justo lo que avisa de que
        # la frase continúa. Decisión de Victor, 2026-08-19.
        fin = max(text.rfind(". ", pos + 10, min(want + 15, len(text))),
                  text.rfind("? ", pos + 10, min(want + 15, len(text))),
                  text.rfind("! ", pos + 10, min(want + 15, len(text))))
        cut = fin if fin > pos else max(
            text.rfind(", ", pos + 10, min(want + 15, len(text))),
            text.rfind(" ", pos + 10, min(want + 10, len(text))))
        for _ in range(6):
            if cut <= pos or not _cuelga(text[pos:cut + 1]):
                break
            cut = text.rfind(" ", pos + 10, cut)
        if cut <= pos:
            cut = min(pos + cap, len(text))
        parts.append(text[pos:cut + 1].strip())
        pos = cut + 1
    parts = [p for p in parts if p]

    # El reparto proporcional corta por longitud, no por dónde caen las palabras:
    # un trozo puede salir por debajo del tope y aun así no entrar en dos líneas
    # de 42. Ese es el que llegaba intacto al SRT con su línea larga, después de
    # arreglar el corte y la fusión (IMODAE 2026-08-07). Se vuelve a partir hasta
    # que todos caben, con tope de vueltas por si algún texto no admite corte.
    for _ in range(4):
        if all(cabe_en_dos_lineas(p) for p in parts):
            break
        afinado = []
        for p in parts:
            r = None if cabe_en_dos_lineas(p) else mejor_reparto(p)
            afinado.extend(list(r) if r else [p])
        if afinado == parts:
            break
        parts = afinado
    # El reparto del TIEMPO entre los trozos. Por proporción de caracteres solo
    # es correcto si el hablante mantiene el ritmo dentro de toda la frase, y no
    # lo mantiene: en cuanto hay una pausa dentro, la proporción le da tiempo de
    # la pausa a las palabras y deja el trozo siguiente sin aire. Medido en el
    # máster de Morsa: "más importantes de nuestro país" salía con 31 caracteres
    # en 1.12 s, o sea 27.7 CPS, que nadie habla.
    #
    # Cuando la frase trae sus `piezas` —los segmentos originales de Whisper,
    # cada uno con sus tiempos— se usa el tiempo REAL de cada palabra.
    piezas = sent.get("piezas") or []
    # El offset de cada trozo se BUSCA en el texto, no se acumula sumando
    # longitudes: los trozos se recortan con `.strip()`, así que pueden salir
    # más cortos que el original y el acumulado se desfasa trozo a trozo. Con
    # seis trozos el desfase ya movía el reparto del tiempo lo bastante como
    # para dejar un cue de 43 caracteres en 1.33 s — 31.6 CPS (Morsa,
    # 2026-08-19).
    out, cursor = [], 0
    for p in parts:
        clave = p[:12]
        ini_c = text.find(clave, cursor)
        if ini_c < 0:
            ini_c = cursor
        fin_c = min(ini_c + len(p), len(text))
        cursor = fin_c
        out.append({"start": _tiempo_de(sent, piezas, ini_c, len(text)),
                    "end": _tiempo_de(sent, piezas, fin_c, len(text)),
                    "text": p})
    # Candado: los tiempos tienen que ir en orden y ningún trozo puede salir de
    # la frase. Si las piezas mienten (Whisper deriva), esto lo contiene.
    t0, t1 = sent["start"], sent["end"]
    prev = t0
    for o in out:
        o["start"] = min(max(o["start"], prev), t1)
        o["end"] = min(max(o["end"], o["start"] + 0.05), t1)
        prev = o["end"]
    return out


def _tiempo_de(sent: dict, piezas: list[dict], idx: int, total: int) -> float:
    """Segundo en que cae el carácter `idx` del texto de la frase."""
    if not piezas:
        dur = sent["end"] - sent["start"]
        return sent["start"] + dur * (idx / max(total, 1))
    off = 0
    for pz in piezas:
        n = len(" ".join(pz["text"].split()))
        if idx <= off + n:
            dentro = (idx - off) / max(n, 1)
            return pz["start"] + (pz["end"] - pz["start"]) * dentro
        off += n + 1                      # el espacio que las une
    return piezas[-1]["end"]


def build_cues(segs: list[dict], rules, cortes_s: list[float] | None = None,
               *, tolerancia_s: float = 0.0, aire_s: float = 0.0,
               diag: dict | None = None) -> list[dict]:
    """Frases completas + cortes de la timeline + vocabulario + duración/CPS/gap.

    `cortes_s` son los cortes SINCRÓNICOS de la timeline (los que cortan imagen
    y sonido a la vez), en segundos desde el inicio del export. Si no se pasan,
    el script se comporta como antes de 2026-08-19: segmenta solo por puntuación
    y por longitud.
    """
    frases = to_sentences(segs)

    # 1. El corte manda: ninguna frase cruza un corte sincrónico. Se parte ANTES
    #    de trocear por longitud, para que cada trozo se reparta con su propio
    #    largo y no herede el del texto entero.
    sin_partir: list[dict] = []
    if cortes_s:
        antes = len(frases)
        frases, sin_partir = partir_en_cortes(frases, cortes_s, min_dur=MIN_DUR)
        if diag is not None:
            diag["partidos_por_corte"] = len(frases) - antes
            diag["no_partidos"] = sin_partir

    sents = []
    for f in frases:
        sents.extend(split_long(f))

    cues = []
    for i, s in enumerate(sents):
        text = apply_vocab(s["text"].strip(), rules)
        if not text:
            continue
        start, end = s["start"], s["end"]
        nxt = sents[i + 1]["start"] if i + 1 < len(sents) else None
        limit = (nxt - 0.05) if nxt is not None else None   # nunca solapar
        # gap-out: no colgar el subtítulo durante una pausa larga
        if limit is not None and limit - end > 0.05:
            end = min(end + GAP_OUT, limit)
        end = max(end, start + MIN_DUR)          # mínimo legible
        # El tope de duración va ANTES del ajuste de CPS. Al revés —como estaba
        # hasta 2026-08-19— el recorte a 6 s volvía a subir los caracteres por
        # segundo DESPUÉS de haberlos ajustado, y el cue salía por encima del
        # tope sin que nada lo dijera: 18 de los 107 de `cut 1.3.srt` de Morsa,
        # hasta 20.5 CPS con MAX_CPS=17. Diecisiete de ellos estaban clavados
        # exactamente en 6.000 s, que es la firma del error.
        if end - start > MAX_DUR:
            end = start + MAX_DUR
        cps = len(text) / max(end - start, 0.01)
        if cps > MAX_CPS:
            # Solo se puede extender hasta donde el siguiente cue lo permita y
            # sin pasarse del tope. Si aun así no baja de 17, el texto es
            # demasiado para el tiempo que dura: se dice, no se aprieta.
            want = min(start + len(text) / MAX_CPS, start + MAX_DUR)
            end = want if limit is None else min(want, limit)
            end = max(end, start + MIN_DUR)
        cues.append({"start": start, "end": end, "text": wrap_lines(text)})

    # 2. fusionar cues consecutivos muy cortos del mismo aliento
    merged = []
    for c in cues:
        # El tope de 84 caracteres no basta para decidir si dos cues se pueden
        # juntar: hay textos de 78 que no entran en dos líneas de 42 porque las
        # palabras no caen donde harían falta. Se pregunta si el resultado CABE,
        # no cuánto mide (IMODAE 2026-08-07).
        junto = (merged[-1]["text"] + " " + c["text"]) if merged else ""
        # Con cortes declarados, dos cues separados por un corte NO se fusionan
        # aunque el hueco sea mínimo: el corte es justo la razón de que estén
        # separados.
        hay_corte = bool(cortes_s) and merged and any(
            merged[-1]["end"] - 0.05 <= x <= c["start"] + 0.05 for x in cortes_s)
        # La fusión NO comprobaba el tope de duración. Es de donde salía el cue
        # de 9.91 s de `cut 1.3.srt` de Morsa —con MAX_DUR en 6.0— y nadie lo
        # veía porque el recorte a 6 s se aplica antes de fusionar, no después.
        cabe_en_tiempo = merged and (c["end"] - merged[-1]["start"]) <= MAX_DUR
        # DOS ORACIONES NO COMPARTEN CUE (Espinosa, 2026-08-27).
        # La fusion solo miraba tiempo y ancho, asi que pegaba el FINAL de una
        # frase con el PRINCIPIO de otra. Victor partio a mano los dos cues que
        # salieron asi, y son las dos unicas correcciones que hizo a los SRT de
        # la tanda:
        #   reel 0013  «Alice, jamas tradicional.» + «¿Quien dijo eso?»
        #   reel 0011  «...historica de nuestro pais.» + «Mexico no se viste literal.»
        # Mismo sintoma, causas distintas — y por eso la regla se escribe sobre
        # la PUNTUACION y no sobre la voz: en el 0013 hay cambio de interlocutor
        # (Resemblyzer lo situa a 0.10 s del corte que hizo Victor), pero en el
        # 0011 habla la misma persona y el defecto es igual de real. Una regla
        # basada solo en diarizacion habria arreglado uno de los dos.
        # ...pero una oracion de una o dos palabras NO sostiene un cue sola.
        # Sin este guarda, la regla parte «Verde. Poder, naturaleza. Blanco.» en
        # cuatro cues de una palabra que parpadean, y Victor no partio ninguno de
        # esos: son una ENUMERACION retorica, no turnos de palabra. El corte de 3
        # palabras separa los cuatro casos reales observados —«Alice, jamas
        # tradicional.» / «¿Quien dijo eso?» (3 y 3) si, «Ahora si, Dani.» /
        # «¡Rompelos!» (3 y 1) no— y esta ajustado a ellos: es un punto de
        # partida medido sobre cuatro casos, no una constante universal.
        MIN_PALABRAS_ORACION = 3
        cierra = bool(re.search(r"[.!?…][\"'»\)]?\s*$", merged[-1]["text"])) if merged else False
        abre = bool(re.match(r"^[¿¡\"'«\(]?[A-ZÁÉÍÓÚÜÑ]", c["text"].strip()))
        if cierra and abre:
            izq = merged[-1]["text"].replace("\n", " ").split()
            der = c["text"].replace("\n", " ").split()
            if len(izq) < MIN_PALABRAS_ORACION or len(der) < MIN_PALABRAS_ORACION:
                cierra = False        # demasiado corta para ir sola: se fusiona
        if (merged and not hay_corte and cabe_en_tiempo and not (cierra and abre)
                and c["start"] - merged[-1]["end"] < 0.12
                and len(junto) <= cap_total()
                and cabe_en_dos_lineas(junto)
                and "\n" not in merged[-1]["text"] and "\n" not in c["text"]):
            merged[-1]["text"] = wrap_lines(junto)
            merged[-1]["end"] = c["end"]
        else:
            merged.append(c)

    # 3. candado final: MIN_DUR pudo empujar un cue sobre el siguiente.
    #
    # BUG (Espinosa dia 3, reel 0012, 2026-08-27): el candado viejo
    # resolvia el solape SIEMPRE encogiendo `a`, con un piso de 0.4s — la mitad
    # del MIN_DUR de 1.0s que la propia linea de arriba (`end = max(end, start
    # + MIN_DUR)`) acababa de exigir. Un cue corto pegado a otro (aqui, "¿Por
    # que una moneda?" entre dos frases largas, huecos de 20ms a cada lado)
    # salia empujado a 1.0s, chocaba con el siguiente, y el candado lo volvia
    # a aplastar a 0.88s: EXACTAMENTE el minimo que se acababa de imponer tres
    # lineas antes. `verify_subtitulos.py` lo caza ("dura 0.88s, minimo 1.0"),
    # pero el SRT ya habia salido asi.
    #
    # Antes de aplastar `a` por debajo del minimo se intenta RETRASAR `b` —
    # la misma estrategia que el imán aplica mas abajo (paso 5) cuando
    # `cortes_s` esta declarado. Aqui corria SIEMPRE, sin ese respaldo: la
    # mayoria de los reels se generan SIN --timeline (el uso normal para subir
    # a redes, doctrina "sin --offset-tc da tiempos desde 0"), asi que el
    # bloque del imán nunca llegaba a ejecutarse y este caso quedaba sin red.
    for a, b in zip(merged, merged[1:]):
        if a["end"] > b["start"] - 0.02:
            objetivo = b["start"] - 0.02
            if objetivo - a["start"] >= MIN_DUR:
                a["end"] = objetivo
            else:
                nuevo_b = a["start"] + MIN_DUR + 0.02
                if b["end"] - nuevo_b >= MIN_DUR:
                    a["end"] = a["start"] + MIN_DUR
                    b["start"] = nuevo_b
                else:
                    a["end"] = max(a["start"] + 0.4, objetivo)

    # 4. imán: los bordes que ya caían cerca de un corte se clavan al corte.
    #    Medido en los reels de Morsa: el editor ya dejaba el 33 % de sus bordes
    #    en el fotograma exacto del corte, contra un 2 % de azar.
    if cortes_s and tolerancia_s > 0:
        objs = [Cue(c["start"], c["end"], c["text"]) for c in merged]
        movidos = imantar(objs, cortes_s, tolerancia_s=tolerancia_s,
                          aire_s=aire_s, min_dur=MIN_DUR, max_dur=MAX_DUR)
        merged = [{"start": o.inicio, "end": o.fin, "text": o.texto} for o in objs]
        if diag is not None:
            diag["imantados"] = movidos
        # 5. los candados se vuelven a cerrar DESPUÉS del imán. Moverse a un
        #    corte puede alargar un cue por encima del tope o empujarlo sobre el
        #    siguiente, y un imán que rompe las reglas que el resto del script
        #    respeta no es una mejora.
        for a, b in zip(merged, merged[1:]):
            if a["end"] > b["start"] - 0.02:
                objetivo = b["start"] - 0.02
                if objetivo - a["start"] >= MIN_DUR:
                    a["end"] = objetivo
                else:
                    # Antes de aplastar `a` por debajo del mínimo se intenta
                    # RETRASAR `b`: un subtítulo que entra un pelo tarde se lee;
                    # uno que dura 0.4 s parpadea y no se lee.
                    nuevo_b = a["start"] + MIN_DUR + 0.02
                    if b["end"] - nuevo_b >= MIN_DUR:
                        a["end"] = a["start"] + MIN_DUR
                        b["start"] = nuevo_b
                    else:
                        a["end"] = max(a["start"] + 0.4, objetivo)
        for c in merged:
            if c["end"] - c["start"] > MAX_DUR:
                c["end"] = c["start"] + MAX_DUR

        # 6. alivio de CPS con lo que el imán haya dejado libre. Mover un borde
        #    a un corte abre hueco por delante del cue anterior, y ese hueco es
        #    tiempo de lectura regalado. El techo es triple: el siguiente cue,
        #    el tope de duración y —lo importante— EL SIGUIENTE CORTE, porque
        #    ganar medio segundo de lectura cruzando un corte no es ganar nada.
        import bisect as _b
        for k, c in enumerate(merged):
            texto = c["text"].replace("\n", "")
            dur = c["end"] - c["start"]
            if len(texto) / max(dur, 0.01) <= MAX_CPS:
                continue
            sig = merged[k + 1]["start"] - 0.02 if k + 1 < len(merged) else None
            i = _b.bisect_right(cortes_s, c["end"])
            corte = (cortes_s[i] - aire_s) if i < len(cortes_s) else None
            techo = c["start"] + MAX_DUR
            for t in (sig, corte):
                if t is not None:
                    techo = min(techo, t)
            quiere = c["start"] + len(texto) / MAX_CPS
            nuevo_fin = min(quiere, techo)
            if nuevo_fin > c["end"]:
                c["end"] = nuevo_fin

    # 7. Los cues que aun no llegan al mínimo se absorben en el vecino, si el
    #    texto cabe en dos líneas y NO hay un corte de por medio. Un subtítulo
    #    de 0.44 s no se lee: parpadea. Prefiero un cue de dos líneas que dura
    #    lo suyo antes que dos destellos.
    absorbidos = 0
    i = 0
    while i < len(merged):
        c = merged[i]
        if c["end"] - c["start"] >= MIN_DUR - 0.005:
            i += 1
            continue
        for j in (i + 1, i - 1):
            if not (0 <= j < len(merged)):
                continue
            a, b = (merged[i], merged[j]) if j > i else (merged[j], merged[i])
            if cortes_s and any(a["end"] - 0.05 <= x <= b["start"] + 0.05
                                for x in cortes_s):
                continue          # el corte es justo la razón de que estén separados
            junto = " ".join((a["text"] + " " + b["text"]).split())
            if not cabe_en_dos_lineas(junto):
                continue
            # Absorber no puede crear un cue por encima del tope: cambiaría un
            # destello ilegible por un subtítulo que se queda clavado.
            if b["end"] - a["start"] > MAX_DUR:
                continue
            a["text"] = wrap_lines(junto)
            a["end"] = b["end"]
            merged.remove(b)
            absorbidos += 1
            break
        else:
            i += 1
            continue
        i = max(0, i - 1)
    if diag is not None and absorbidos:
        diag["absorbidos"] = absorbidos
    return merged


def escribir_srt(out: Path, cues: list[dict], off: float = 0.0) -> Path:
    """Escribe el SRT con CRLF y UTF-8 EXPLICITOS.

    No es un detalle de estilo. `open(out, "w")` a secas usa el encoding del
    locale y escribe LF. En IMODAE (2026-08-07) el import de subtítulos de
    Resolve no puso nada en la pista —sin dar ningún error— y el editor lo
    reportó como "no se pasó nada".

    HONESTIDAD SOBRE LA CAUSA: para arreglarlo se cambiaron DOS cosas a la vez
    —esto y la comprobación real de que la pista ST existiera— y después el
    import funcionó. No está aislado cuál de las dos era. Lo que sí es seguro es
    que CRLF es el estándar de facto de SubRip, que todo lo demás (VLC, YouTube,
    ffmpeg, Premiere) lo acepta igual, y que por tanto **no hay nada que ganar
    volviendo a LF** aunque algún día se demuestre que la culpable era la pista.

    Sin BOM: hay parsers que se comen el BOM como parte del primer índice.
    """
    out = Path(out)
    with open(out, "w", encoding="utf-8", newline="\r\n") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{ts(c['start'] + off)} --> {ts(c['end'] + off)}\n"
                    f"{c['text']}\n\n")
    return out


def ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def tc_to_frames(tc: str, fps: float) -> int:
    """TC no-drop 'HH:MM:SS:FF' → frames (etiquetas por segundo = round(fps))."""
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 3600 + m * 60 + s) * round(fps)) + f


def frames_to_tc(fr: int, fps: float) -> str:
    n = round(fps)
    f = fr % n
    total = fr // n
    return f"{total//3600:02d}:{(total//60)%60:02d}:{total%60:02d}:{f:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out")
    ap.add_argument("--config", help="project_config.json para el vocabulario")
    ap.add_argument("--model", default=str(MODEL_DEFAULT))
    ap.add_argument("--lang", default="es")
    ap.add_argument("--fps", type=float, default=None,
                    help="fps del export; por defecto se lee del propio "
                         "archivo (con --timeline manda el de la timeline)")
    ap.add_argument("--offset-tc", help="TC de timeline donde arranca el export "
                                        "(p.ej. 01:06:06:04); desplaza el SRT")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="caracteres por linea; por defecto se calcula del\n                         formato del cuadro (42 en 16:9, ~31 en 9:16)")
    ap.add_argument("--offset-frames", type=int, default=0)
    ap.add_argument("--lua", help="además, escribir datos para place_subtitles.lua")
    ap.add_argument("--no-onset-fix", action="store_true")
    ap.add_argument("--timeline", help="nombre de la timeline de Resolve de la que "
                                       "salió el export; sus cortes mandan sobre "
                                       "los bordes de los subtítulos")
    ap.add_argument("--proyecto", help="proyecto de Resolve (por defecto, el "
                                       "`project_name` del project_config)")
    ap.add_argument("--base", help="base de datos de disco de Resolve (por "
                                   "defecto, la activa)")
    ap.add_argument("--tolerancia-corte", type=int, default=TOLERANCIA_CORTE_F,
                    help="fotogramas que un borde puede moverse para clavarse "
                         "a un corte (por defecto 10)")
    ap.add_argument("--desde", help="escribir solo los cues posteriores a este TC, "
                                    "renumerados desde 1 pero CON su timecode "
                                    "original (para reimportar solo lo que falta)")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"No existe: {video}")
    model = Path(args.model)
    if not model.exists():
        sys.exit(f"Falta el modelo Whisper: {model}")
    rules = load_vocab(Path(args.config) if args.config else None)
    print(f"Vocabulario: {len(rules)} reglas de corrección")

    ancho_px, alto_px = dimensiones_video(video)
    sub_cfg = {}
    if args.config and Path(args.config).exists():
        try:
            sub_cfg = json.loads(Path(args.config).read_text()).get("subtitulos") or {}
        except (json.JSONDecodeError, OSError):
            sub_cfg = {}
    if args.max_chars:
        lim, motivo = int(args.max_chars), "--max-chars"
    elif sub_cfg.get("max_chars_linea"):
        lim, motivo = int(sub_cfg["max_chars_linea"]), "project_config"
    else:
        lim = chars_por_linea(
            ancho_px, alto_px,
            margen=float(sub_cfg.get("margen_seguro", MARGEN_SEGURO)),
            alto_linea_rel=float(sub_cfg.get("alto_linea_rel", ALTO_LINEA_REL)))
        motivo = (f"{ancho_px}x{alto_px}" if ancho_px else "sin medidas del cuadro")
    configurar(lim)
    forma = ("vertical" if alto_px > ancho_px else
             "horizontal" if ancho_px > alto_px else "cuadrado") if ancho_px else "?"
    print(f"Cuadro: {ancho_px}x{alto_px} ({forma}) - {lim} caracteres por linea ({motivo})")

    if args.fps is None:
        args.fps = fps_video(video)
        if not args.fps:
            args.fps = 23.976
            print("  ⚠ no se pudo leer el frame rate del archivo: se supone "
                  "23.976. El TC de pegado puede caer en otro fotograma; "
                  "declaralo con --fps si no es ese.")

    # --- los cortes de la timeline ---
    cortes_s: list[float] = []
    tl = None
    if args.timeline:
        from lib.timeline_resolve import ErrorTimeline, leer_timeline
        proyecto = args.proyecto
        if not proyecto and args.config and Path(args.config).exists():
            try:
                proyecto = json.loads(Path(args.config).read_text()).get("project_name")
            except (json.JSONDecodeError, OSError):
                proyecto = None
        if not proyecto:
            sys.exit("Con --timeline hace falta --proyecto (o un project_config "
                     "con project_name).")
        try:
            tl = leer_timeline(proyecto, args.timeline, config=args.config,
                               base=args.base)
        except ErrorTimeline as e:
            sys.exit(str(e))
        for a in tl.avisos():
            print(f"  ⚠ {a}")
        args.fps = tl.fps
        # Dónde empieza el export dentro de la timeline. Casi todos los renders
        # arrancan en el TC de inicio de la timeline; si no, se dice con
        # --offset-tc, que es el mismo dato.
        ini_f = tc_to_frames(args.offset_tc, tl.fps) if args.offset_tc else tl.origen
        dur_v = duracion_video(video)
        fin_f = ini_f + int(round(dur_v * tl.fps)) if dur_v else tl.origen + 10**9
        rango = (ini_f, fin_f)
        cortes_s = [(c - ini_f) / tl.fps for c in tl.cortes_sync(rango)]
        print(f"Timeline «{tl.nombre}»: {tl.ancho}x{tl.alto} @ {tl.fps:.3f} fps · "
              f"{len(tl.cortes_imagen(rango))} cortes de imagen, "
              f"{len(cortes_s)} sincrónicos (mandan), "
              f"{len(tl.cortes_solo_imagen(rango))} de solo imagen (voice over, "
              f"no mandan)")

    with tempfile.TemporaryDirectory() as td:
        wav = extract_audio(video, Path(td) / "a.wav")
        print("Transcribiendo (VAD Silero)…")
        segs = whisper(wav, model, args.lang, vad=True)
    if not segs:
        sys.exit("Whisper no devolvió nada.")
    print(f"  {len(segs)} segmentos crudos")

    # 1. recortar alucinaciones de cabeza y cola
    while segs and is_hallucination(segs[0]["text"]):
        print(f"  ⊘ cabeza alucinada: {segs[0]['text'][:50]!r}")
        segs.pop(0)
    while segs and is_hallucination(segs[-1]["text"]):
        print(f"  ⊘ cola alucinada: {segs[-1]['text'][:50]!r}")
        segs.pop()
    if not segs:
        sys.exit("Todo el transcript era alucinación.")

    # 2. arranque estirado
    first = segs[0]
    cps = len(first["text"]) / max(first["end"] - first["start"], 0.01)
    if not args.no_onset_fix and cps < SUSPECT_CPS and first["end"] > 6:
        print(f"  ⚠ primer cue con CPS={cps:.1f} (estirado) — buscando onset real…")
        onset = find_speech_onset(video, model, args.lang, first["text"],
                                  hi=min(first["end"], 120))
        if onset > first["start"] + 0.5:
            print(f"  ✓ onset real ≈ {onset:.2f}s (era {first['start']:.2f}s)")
            first["start"] = onset

    diag: dict = {}
    cues = build_cues(segs, rules, cortes_s or None,
                      tolerancia_s=args.tolerancia_corte / args.fps,
                      aire_s=AIRE_CORTE_F / args.fps, diag=diag)
    off = args.offset_frames / args.fps
    if args.offset_tc:
        off = tc_to_frames(args.offset_tc, args.fps) / args.fps
        print(f"  offset timeline: {args.offset_tc} = {off:.3f}s")

    out = Path(args.out) if args.out else video.with_suffix(".srt")

    todos = cues
    if args.desde:
        corte = tc_to_frames(args.desde, args.fps) / args.fps - off
        cues = [c for c in cues if c["start"] >= corte - 0.001]
        print(f"  --desde {args.desde}: {len(cues)} de {len(todos)} cues "
              f"(renumerados desde 1, con su TC original)")
        if not cues:
            sys.exit("No queda ningún cue después de ese TC.")

    escribir_srt(out, cues, off)
    dur = cues[-1]["end"] - cues[0]["start"]
    peak = max(len(c["text"].replace("\n", "")) / (c["end"] - c["start"]) for c in cues)
    print(f"\n{out}: {len(cues)} subtítulos, {dur:.1f}s de habla, CPS máx {peak:.1f}")

    if cortes_s:
        from lib.subtitulos import Cue as _Cue, cruces as _cruces, densos as _densos
        objs = [_Cue(c["start"], c["end"], c["text"]) for c in cues]
        cruza = _cruces(objs, cortes_s)
        dens = _densos(objs)
        bordes = 2 * len(objs)
        pegs = sum(1 for o in objs for t in (o.inicio, o.fin)
                   if any(abs(x - t) <= 2 / args.fps for x in cortes_s))
        print(f"  cortes: {diag.get('partidos_por_corte', 0)} frases partidas en un "
              f"corte, {diag.get('imantados', 0)} bordes clavados al fotograma, "
              f"{pegs}/{bordes} bordes pegados a un corte ({100*pegs/bordes:.0f}%)")
        for f in diag.get("no_partidos", [])[:6]:
            print(f"  ⚠ sin partir en {ts(f['start'])}: partirlo dejaba un trozo "
                  f"ilegible — «{f['text'][:44]}…»")
        n = len(diag.get("no_partidos", []))
        if n > 6:
            print(f"  ⚠ … y {n-6} frases más que tampoco se pudieron partir")
        if dens:
            print(f"  · {len(dens)} cues por encima de {MAX_CPS:.0f} CPS "
                  f"(máx {max(c.cps for _, c in dens):.1f}). Bajarlos exige "
                  f"CONDENSAR el texto, y este script no condensa.")
        if cruza:
            print(f"  · quedan {len(cruza)} cues cruzando un corte sincrónico: son "
                  f"los que no se podían partir. Para verificar, decláralos:")
            print(f"      python3 bin/verify_subtitulos.py --srt {out} \\")
            print(f"          --timeline \"{args.timeline}\" --max-cruces {len(cruza)}")
    # Cobertura: decir hasta dónde llega el subtitulado. `cut 1.3.srt` de Morsa
    # terminaba en 799.5 s de un máster de 996.4 y nadie lo dijo: 3:17 finales
    # sin un solo subtítulo, que resultaron ser la coda de concierto.
    dur_v = duracion_video(video)
    if dur_v and todos:
        cola = dur_v - todos[-1]["end"]
        if cola > 5:
            print(f"  ⚠ sin subtítulos desde {ts(todos[-1]['end'])} hasta el final "
                  f"({cola:.0f}s, {100*cola/dur_v:.0f}% de la pieza)")

    # Colocación EXACTA en Resolve. La API libre no crea subtítulos por script
    # y el drag&drop los suelta donde cae el mouse (no en su timecode), así que
    # el único método al frame es cortar y pegar en el playhead.
    paste = frames_to_tc(round((cues[0]["start"] + off) * args.fps), args.fps)
    print("\nPara colocarlo al frame en Resolve:")
    print("  1. File → Import → Subtitle… y arrastra el SRT a la pista ST1")
    print("     (clic derecho en el encabezado de pistas → Add Subtitle Track).")
    print("  2. Selecciona los subtítulos (Timeline → Select Clips → Forward on")
    print("     Track con el primero elegido) y ⌘X.")
    print(f"  3. Pon el playhead en {paste} — escribiendo el TC en el visor —")
    print("     y ⌘V. Pega alineado al playhead, sin arrastrar ni nudge.")
    print("  OJO: tras teclear el TC pulsa Escape o haz clic en la timeline; si el")
    print("  campo de TC conserva el foco, se traga las teclas (incluido el nudge).")

    if args.lua:
        with open(args.lua, "w") as f:
            f.write("-- generado por build_subtitles.py — frames de timeline\n")
            f.write(f"return {{ fps={args.fps}, cues={{\n")
            for c in cues:
                sf = round((c["start"] + off) * args.fps)
                ef = round((c["end"] + off) * args.fps)
                txt = c["text"].replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                f.write(f'  {{s={sf}, e={ef}, t="{txt}"}},\n')
            f.write("}}\n")
        print(f"{args.lua}: datos para place_subtitles.lua")


if __name__ == "__main__":
    main()
