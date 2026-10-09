"""Reglas de subtitulaje: cuanto cabe, donde se parte y donde manda el corte.

Aqui viven las reglas puras —aritmetica de texto y de tiempos, sin ffmpeg, sin
Whisper y sin base de datos— para que se puedan probar solas y para que las
compartan `bin/build_subtitles.py` (que las aplica) y
`bin/verify_subtitulos.py` (que las comprueba sobre el SRT ya escrito).

LA REGLA NUEVA (Morsa, 2026-08-19)

Victor lo dijo asi: *"una referencia que puedes tomar son los cortes, a menos que
sea un voice over los cortes te dan la pauta ideal"*. Medido sobre el corte
entregado de Morsa, 995.7 s con 366 cortes de imagen:

    corte que TAMBIEN corta el dialogo   77 cortes   cada 7.44 s   cae en una pausa el 31 %
    corte de SOLO imagen                289 cortes   cada 2.04 s   cae en una pausa el 15 %
    un punto al azar                        —            —         cae en una pausa el  9 %

El corte sincronico acierta la pausa 3.4 veces mas que el azar y llega con la
cadencia de un cue. El de solo imagen esta al nivel del azar y llega cada dos
segundos: ahi el audio sigue corriendo por debajo —eso ES el voice over— y
partir el subtitulo produce cues por debajo del minimo legible.

    > Un corte solo manda sobre el subtitulo si tambien corta el sonido.

Y no es teoria: en la timeline `Reels verticales` de Morsa, **el 33 % de los
bordes de subtitulo cae en el fotograma EXACTO de un corte de imagen, contra un
2 % de azar** (16x). El editor ya lo estaba haciendo a mano.

POR QUE IMAN Y NO TABIQUE
El corte acierta la pausa el 31 % de las veces, no el 100 %. Forzar un borde en
cada corte sincronico partiria frases por la mitad el otro 69 %. Por eso son dos
reglas distintas:
  - PROHIBICION: un cue no cruza un corte sincronico (si el trozo resultante
    aguanta el minimo; si no, se deja y SE DICE).
  - IMAN: un borde que ya cae cerca de un corte se clava al fotograma del corte.
Nunca se crea un borde donde el habla no lo pedia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Reglas de subtitulaje (es-MX, lectura comoda)
#
# EL 42 ES UN LIMITE DE LECTURA, NO DE ANCHO (medido el 2026-08-25)
# Los 42 caracteres son la convencion de subtitulado (EBU) y salen de cuanto se
# lee comodo de una pasada, no de cuanto cabe en el cuadro. En 16:9 nunca los
# ata el ancho: con la fuente de subtitulo de Resolve caben ~100 por linea. En
# 9:16 el ancho SI ata, y ata muy por debajo de 42.
#
# Medido sobre cuatro reels 2160x3840 con subtitulos quemados, restando cada
# frame del mismo frame sin subtitular:
#
#     alto de linea            116 px  = 3.02 % del alto de cuadro
#     ancho medio de caracter   56 px  = 0.48 x el alto de linea
#     linea mas ancha    1812-1960 px  = 84-91 % del ancho de cuadro
#     margen que quedaba    98-174 px  = 4.5-8.1 % por lado
#
# Con 42 el texto llega al 4.5 % del borde. `chars_por_linea()` devuelve el
# minimo entre el limite de lectura y el que impone el ancho del cuadro.
MAX_CHARS_LINE = 42
MAX_LINES = 2

# Tipografia del subtitulo, en proporciones del cuadro (medidas, no supuestas).
# Son de la fuente de subtitulos de Resolve; si el proyecto usa otra, se
# declaran en project_config -> "subtitulos".
ALTO_LINEA_REL = 0.030   # alto de linea / alto de cuadro
ANCHO_CAR_REL = 0.48     # ancho medio de caracter / alto de linea
MARGEN_SEGURO = 0.10     # por lado, sobre el ancho de cuadro
MAX_CPS = 17.0
MIN_DUR = 1.0
MAX_DUR = 6.0
GAP_OUT = 0.4          # s que el subtitulo sobrevive a la ultima palabra

# Cuanto puede moverse un borde de cue para clavarse a un corte. 10 fotogramas
# a 23.976 son 0.42 s: menos que el gap-out, asi que el iman nunca contradice
# una decision de fraseo, solo la afina.
TOLERANCIA_CORTE_F = 10
# El cue sale dos fotogramas ANTES del corte. Un subtitulo que sobrevive al
# plano aunque sea un fotograma se lee como error de edicion.
AIRE_CORTE_F = 2

# Palabras que NO pueden quedarse al final de una linea ni de un subtitulo: se
# leen pegadas a lo que viene despues.
COLGANTES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del",
    "de", "a", "en", "con", "por", "para", "sin", "sobre", "entre", "hacia",
    "hasta", "desde", "tras", "y", "e", "o", "u", "ni", "que", "qué", "como",
    "cuando", "donde", "porque", "pero", "su", "sus", "mi", "mis", "tu", "tus",
    "nuestro", "nuestra", "se", "es", "muy", "más", "ese", "esa", "este",
    "esta", "esto", "eso",
}


@dataclass
class Cue:
    inicio: float
    fin: float
    texto: str

    @property
    def dur(self) -> float:
        return self.fin - self.inicio

    @property
    def plano(self) -> str:
        return " ".join(self.texto.split())

    @property
    def cps(self) -> float:
        return len(self.texto.replace("\n", "")) / max(self.dur, 0.01)

    @property
    def lineas(self) -> list[str]:
        return self.texto.split("\n")


# --------------------------------------------------------------------------
# reparto en lineas
# --------------------------------------------------------------------------

def cuelga(frase: str) -> bool:
    """¿La frase termina en una palabra que pide la siguiente?"""
    p = frase.rstrip().rstrip(",;:").split()
    if not p:
        return False
    if p[-1].endswith((".", "!", "?", "…")):
        return False
    return p[-1].lower().strip(".,;:¡!¿?\"'») ") in COLGANTES


def chars_por_linea(ancho: int | None, alto: int | None, *,
                    margen: float = MARGEN_SEGURO,
                    alto_linea_rel: float = ALTO_LINEA_REL,
                    ancho_car_rel: float = ANCHO_CAR_REL) -> int:
    """Caracteres por linea que caben sin llegar al borde del cuadro.

    Devuelve el MINIMO entre el limite de lectura (MAX_CHARS_LINE) y el que
    impone el ancho. En 16:9 gana siempre el de lectura —el ancho da para el
    doble— asi que el horizontal no cambia. En 9:16 gana el ancho.
    """
    if not ancho or not alto:
        return MAX_CHARS_LINE
    px_car = alto * alto_linea_rel * ancho_car_rel
    if px_car <= 0:
        return MAX_CHARS_LINE
    cabe = int((ancho * (1.0 - 2.0 * margen)) / px_car)
    return max(12, min(MAX_CHARS_LINE, cabe))


def configurar(max_chars: int) -> None:
    """Fija el limite de linea para TODO el modulo.

    Existe porque `MAX_CHARS_LINE` se lee desde las funciones puras de aqui y
    tambien se importa por nombre en los scripts: cambiar solo la global dejaria
    la mitad del sistema con el valor viejo y la otra mitad con el nuevo.
    """
    global MAX_CHARS_LINE
    MAX_CHARS_LINE = int(max_chars)


def cap_total() -> int:
    """Tope de caracteres de un cue entero, leido AHORA (no en el import)."""
    return MAX_CHARS_LINE * MAX_LINES


def mejor_reparto(text: str) -> tuple[str, str] | None:
    """El corte en dos lineas que deja la linea mas larga lo mas corta posible."""
    words = text.split()
    if len(words) < 2:
        return None
    mejor, mejor_coste = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        coste = max(len(a), len(b)) + (8 if cuelga(a) else 0)
        if mejor_coste is None or coste < mejor_coste:
            mejor, mejor_coste = (a, b), coste
    return mejor


def cabe_en_dos_lineas(text: str) -> bool:
    """Si no cabe, el arreglo NO es apretar el texto: es partirlo en dos cues."""
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS_LINE:
        return True
    r = mejor_reparto(text)
    return bool(r) and max(len(r[0]), len(r[1])) <= MAX_CHARS_LINE


def wrap_lines(text: str) -> str:
    """Parte en <=MAX_LINES lineas de <=MAX_CHARS_LINE. NUNCA descarta palabras."""
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS_LINE:
        return text
    r = mejor_reparto(text)
    return "\n".join(r) if r else text


# --------------------------------------------------------------------------
# partir por el corte
# --------------------------------------------------------------------------

def partir_texto(texto: str, frac: float,
                 desvio_max: int | None = None) -> tuple[str, str] | None:
    """Parte el texto cerca de `frac` (0-1), en el limite de palabra mas cercano.

    Prioriza fin de frase > coma > espacio, pero el criterio principal es la
    DISTANCIA al punto pedido: una clase mejor solo compensa seis caracteres de
    desvio. Y no cierra en una palabra colgante.

    La version del 2026-08-19 buscaba con `rfind` dentro de una ventana del
    25 % del texto, o sea que se quedaba con el ULTIMO limite de la ventana en
    vez de con el mas cercano. Sobre una frase larga del maestro de Morsa eso
    puso 45 caracteres donde tocaban 25 y el cue salio a 25.4 CPS. Buscar
    "cerca" no es lo mismo que buscar "el ultimo de por aqui".
    """
    texto = " ".join(texto.split())
    n = len(texto)
    if n < 4:
        return None
    want = max(1, min(n - 1, int(round(n * frac))))
    if desvio_max is None:
        desvio_max = max(12, int(n * 0.12))
    mejor = None
    for i in range(1, n):
        if texto[i] != " ":
            continue
        d = abs(i - want)
        if d > desvio_max:
            continue
        antes = texto[i - 1]
        clase = 0 if antes in ".!?…" else (1 if antes in ",;:" else 2)
        izq = texto[:i].strip()      # la coma se conserva (ver split_long)
        der = texto[i + 1:].strip()
        if not izq or not der:
            continue
        coste = d + clase * 6 + (10 if cuelga(izq) else 0)
        if mejor is None or coste < mejor[0]:
            mejor = (coste, izq, der)
    return (mejor[1], mejor[2]) if mejor else None


# Un corte de la timeline y un limite de segmento del transcript separados por
# mas de esto ya no hablan del mismo sitio del habla. Los tiempos de Whisper
# derivan —esta MEDIDO (IMODAE 2026-08-07)— pero medio segundo de margen basta:
# con un segundo entero, el texto de una pieza se metia en el tiempo de otra y
# el cue salia a 31 CPS (Morsa, 2026-08-19).
TOL_PIEZA = 0.5


def partir_en_cortes(frases: list[dict], cortes_s: list[float], *,
                     min_dur: float = MIN_DUR,
                     tol_pieza: float = TOL_PIEZA) -> tuple[list[dict], list[dict]]:
    """Parte cada frase en los cortes sincronicos que la atraviesan.

    EL TEXTO NO SE REPARTE POR REGLA DE TRES. Una frase de treinta segundos no
    lleva sus caracteres repartidos uniformemente en el tiempo: si el hablante
    se para cinco segundos, la proporcion miente. Se usa la propia segmentacion
    de Whisper (`piezas`), que ya trae tiempos por trozo:

      - si el corte cae cerca (< tol_pieza) del limite entre dos piezas, se
        parte AHI, y el reparto del texto es exacto;
      - si cae dentro de una pieza, se parte esa pieza —y solo esa— por
        proporcion, que a escala de un segmento de Whisper es fiable;
      - si la frase no trae piezas, se parte por proporcion como antes.

    Devuelve (frases_partidas, no_partidas). En `no_partidas` van las que
    tenian un corte dentro y no se pudieron partir sin dejar un trozo por
    debajo de `min_dur`: se dicen con su timecode en vez de romper la
    legibilidad en silencio.
    """
    fuera: list[dict] = []
    salida: list[dict] = []
    for f in frases:
        pend = [dict(f)]
        while pend:
            fr = pend.pop(0)
            dentro = [c for c in cortes_s
                      if fr["start"] + 0.05 < c < fr["end"] - 0.05]
            hecho = False
            for c in dentro:
                if c - fr["start"] < min_dur or fr["end"] - c < min_dur:
                    continue
                par = _partir_frase(fr, c, tol_pieza)
                if not par:
                    continue
                izq, der = par
                # El corte no puede empeorar la lectura. Si un trozo sale
                # bastante mas denso que la frase de la que viene, el reparto
                # del texto no cuadra con el del tiempo y partir ahi produce un
                # cue ilegible: 31.6 CPS donde la frase iba a 14 (Morsa,
                # 2026-08-19). Se deja entera y se dice.
                base = len(fr["text"]) / max(fr["end"] - fr["start"], 0.01)
                techo = max(MAX_CPS, base * 1.15)
                if any(len(x["text"]) / max(x["end"] - x["start"], 0.01) > techo
                       for x in (izq, der)):
                    continue
                salida.append(izq)
                pend.insert(0, der)
                hecho = True
                break
            if not hecho:
                if dentro:
                    fuera.append(dict(fr, cortes=dentro))
                salida.append(fr)
    salida.sort(key=lambda x: x["start"])
    return salida, fuera


def _densa(pz: dict, tope: float = MAX_CPS * 1.25) -> bool:
    """¿Esta pieza quedo con mas texto del que cabe en su tiempo?"""
    dur = pz["end"] - pz["start"]
    if dur <= 0.01:
        return True
    return len(" ".join(pz["text"].split())) / dur > tope


def _partir_frase(fr: dict, c: float, tol_pieza: float):
    """Parte la frase `fr` en el instante `c`. Devuelve (izquierda, derecha)."""
    piezas = fr.get("piezas") or []
    if len(piezas) >= 2:
        k = min(range(1, len(piezas)),
                key=lambda i: abs(piezas[i]["start"] - c))
        if abs(piezas[k]["start"] - c) <= tol_pieza:
            # Las piezas del borde se recortan AL CORTE. Si no se recortan, el
            # texto de una pieza se queda con el tiempo de la otra y el cue sale
            # comprimido: 43 caracteres en 1.35 s donde la pieza original tenia
            # 1.94 (Morsa, 2026-08-19). Recortadas, la compresion se ve — y si
            # es demasiada, no se parte.
            izq_p = [dict(p) for p in piezas[:k]]
            der_p = [dict(p) for p in piezas[k:]]
            izq_p[-1]["end"] = min(izq_p[-1]["end"], c)
            der_p[0]["start"] = max(der_p[0]["start"], c)
            if _densa(izq_p[-1]) or _densa(der_p[0]):
                return None
            izq = " ".join(p["text"].strip() for p in izq_p).strip()
            der = " ".join(p["text"].strip() for p in der_p).strip()
            if izq and der:
                return ({"start": fr["start"], "end": c, "text": izq,
                         "piezas": izq_p},
                        {"start": c, "end": fr["end"], "text": der,
                         "piezas": der_p})
        # el corte cae DENTRO de una pieza: se parte esa, por proporcion, que a
        # escala de un segmento de Whisper (unos segundos) si es fiable.
        j = max(0, next((i for i in range(len(piezas))
                         if piezas[i]["end"] > c), len(piezas) - 1))
        pz = piezas[j]
        dur = max(pz["end"] - pz["start"], 0.01)
        par = partir_texto(pz["text"], (c - pz["start"]) / dur)
        if par:
            a, b = par
            # La pieza partida no puede salir mas densa que la pieza entera. El
            # reparto por proporcion dentro de un segmento de Whisper es fiable,
            # pero no exacto, y una pieza sintetica demasiado apretada arrastra
            # su densidad hasta el cue final: 32.2 CPS donde la pieza original
            # iba a 20.1 (Morsa, 2026-08-19).
            base = len(" ".join(pz["text"].split())) / dur
            tope = max(MAX_CPS, base * 1.15)
            d_a, d_b = max(c - pz["start"], 0.01), max(pz["end"] - c, 0.01)
            if (len(a) / d_a > tope) or (len(b) / d_b > tope):
                return None
            izq_p = piezas[:j] + [{"start": pz["start"], "end": c, "text": a}]
            der_p = [{"start": c, "end": pz["end"], "text": b}] + piezas[j + 1:]
            izq = " ".join(p["text"].strip() for p in izq_p).strip()
            der = " ".join(p["text"].strip() for p in der_p).strip()
            if izq and der:
                return ({"start": fr["start"], "end": c, "text": izq,
                         "piezas": izq_p},
                        {"start": c, "end": fr["end"], "text": der,
                         "piezas": der_p})
        return None
    frac = (c - fr["start"]) / max(fr["end"] - fr["start"], 0.01)
    par = partir_texto(fr["text"], frac)
    if not par:
        return None
    return ({"start": fr["start"], "end": c, "text": par[0]},
            {"start": c, "end": fr["end"], "text": par[1]})


def imantar(cues: list[Cue], cortes_s: list[float], *,
            tolerancia_s: float, aire_s: float,
            min_dur: float = MIN_DUR, max_dur: float = MAX_DUR,
            max_cps: float = MAX_CPS) -> int:
    """Clava al corte los bordes que ya caian cerca. Devuelve cuantos movio.

    El inicio se va AL corte y el final se va al corte menos el aire. Nunca se
    mueve un borde si eso deja el cue por debajo del minimo, por encima del
    maximo, solapando con el vecino O MAS DENSO DE LO QUE YA ESTABA: el iman
    afina, no reescribe.

    Lo de la densidad no es teorico. Clavar el inicio de un cue a un corte que
    esta 60 ms mas adelante le quita 60 ms de lectura, y en un cue que ya iba
    apretado eso lo empuja de 31.6 a 33.1 CPS. Ganar un fotograma de precision
    a cambio de que no se pueda leer es un mal cambio (Morsa, 2026-08-19).
    """
    if not cortes_s:
        return 0
    import bisect
    movidos = 0

    def cerca(t: float) -> float | None:
        i = bisect.bisect_left(cortes_s, t)
        mejor, dist = None, tolerancia_s
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(cortes_s) and abs(cortes_s[j] - t) <= dist:
                mejor, dist = cortes_s[j], abs(cortes_s[j] - t)
        return mejor

    for k, c in enumerate(cues):
        prev = cues[k - 1] if k else None
        sig = cues[k + 1] if k + 1 < len(cues) else None
        largo = len(c.texto.replace("\n", ""))
        tope_cps = max(max_cps, c.cps)     # nunca se empeora lo que ya venia mal

        objetivo = cerca(c.inicio)
        if objetivo is not None and objetivo != c.inicio:
            piso = max((prev.fin + 0.02) if prev else 0.0, c.fin - max_dur)
            nueva_dur = c.fin - objetivo
            if (piso <= objetivo <= c.fin - min_dur + 1e-9
                    and largo / max(nueva_dur, 0.01) <= tope_cps + 1e-9):
                c.inicio = objetivo
                movidos += 1
        objetivo = cerca(c.fin + aire_s)
        if objetivo is not None:
            nuevo = objetivo - aire_s
            techo = min((sig.inicio - 0.02) if sig else nuevo,
                        c.inicio + max_dur)
            nueva_dur = nuevo - c.inicio
            if (c.inicio + min_dur - 1e-9 <= nuevo <= techo
                    and largo / max(nueva_dur, 0.01) <= tope_cps + 1e-9):
                c.fin = nuevo
                movidos += 1
    return movidos


# --------------------------------------------------------------------------
# leer un SRT ya escrito
# --------------------------------------------------------------------------

_RE_TIEMPO = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def leer_srt(path) -> list[Cue]:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        txt = f.read()
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    out: list[Cue] = []
    for bloque in re.split(r"\n{2,}", txt.strip()):
        m = _RE_TIEMPO.search(bloque)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lineas = [l for l in bloque.split("\n") if l.strip()]
        # el bloque es: indice / tiempos / texto...
        cuerpo = lineas[2:] if lineas and lineas[0].strip().isdigit() else lineas[1:]
        out.append(Cue(a, b, "\n".join(cuerpo).strip()))
    return out


# --------------------------------------------------------------------------
# diagnostico
# --------------------------------------------------------------------------

def cruces(cues: list[Cue], cortes_s: list[float], margen: float = 0.05) -> list[tuple[int, list[float]]]:
    """Cues que atraviesan un corte. (indice 1-based, cortes que cruza)."""
    out = []
    for i, c in enumerate(cues, 1):
        d = [x for x in cortes_s if c.inicio + margen < x < c.fin - margen]
        if d:
            out.append((i, d))
    return out


def pegados(cues: list[Cue], cortes_s: list[float], tol_s: float) -> int:
    """Cuantos bordes de cue caen a <= tol_s de un corte. Metrica de calidad."""
    if not cortes_s:
        return 0
    import bisect
    n = 0
    for c in cues:
        for t in (c.inicio, c.fin):
            i = bisect.bisect_left(cortes_s, t)
            if any(0 <= j < len(cortes_s) and abs(cortes_s[j] - t) <= tol_s
                   for j in (i - 1, i, i + 1)):
                n += 1
    return n


# Sitio libre por delante que convierte un CPS alto en culpa del motor.
HOLGURA_EXTENSIBLE = 0.20


def densos(cues: list[Cue]) -> list[tuple[int, Cue]]:
    """Cues por encima del CPS maximo, se pueda hacer algo o no."""
    return [(i, c) for i, c in enumerate(cues, 1) if c.cps > MAX_CPS + 0.05]


def infracciones(cues: list[Cue]) -> list[str]:
    """Reglas duras de legibilidad. Devuelve una linea por incumplimiento.

    EL CPS ALTO NO SIEMPRE ES UN FALLO. Medido en el master de Morsa: 47 de los
    171 segmentos que devuelve Whisper YA vienen por encima de 17 CPS, con una
    mediana de 15.0 y un p90 de 19.4. La gente habla asi. La unica forma de
    bajar de 17 seria CONDENSAR el texto, y este motor no condensa por doctrina
    —no inventa palabras ni le quita palabras a nadie—.

    Asi que el CPS solo cuenta como incumplimiento cuando habia SITIO por
    delante y no se uso: eso si es culpa del motor. El resto se reporta con
    `densos()` para que el editor lo sepa, sin poner el verificador en rojo
    permanente por como habla un senor de 73 anos.
    """
    fallos = []
    for i, c in enumerate(cues, 1):
        sig = cues[i] if i < len(cues) else None
        holgura = (sig.inicio - c.fin) if sig else (MAX_DUR - c.dur)
        if c.dur < MIN_DUR - 0.005:
            fallos.append(f"cue {i} ({c.inicio:.2f}s): dura {c.dur:.2f}s, minimo {MIN_DUR}")
        if c.dur > MAX_DUR + 0.005:
            fallos.append(f"cue {i} ({c.inicio:.2f}s): dura {c.dur:.2f}s, maximo {MAX_DUR}")
        if c.cps > MAX_CPS + 0.05 and holgura > HOLGURA_EXTENSIBLE and c.dur < MAX_DUR - 0.005:
            fallos.append(f"cue {i} ({c.inicio:.2f}s): {c.cps:.1f} CPS y tenia "
                          f"{holgura:.2f}s libres por delante sin usar")
        if len(c.lineas) > MAX_LINES:
            fallos.append(f"cue {i} ({c.inicio:.2f}s): {len(c.lineas)} lineas")
        for l in c.lineas:
            if len(l) > MAX_CHARS_LINE:
                fallos.append(f"cue {i} ({c.inicio:.2f}s): linea de {len(l)} caracteres")
    for i, (a, b) in enumerate(zip(cues, cues[1:]), 1):
        if b.inicio < a.fin - 0.005:
            fallos.append(f"cue {i} y {i+1} se solapan ({a.fin:.2f} > {b.inicio:.2f})")
    return fallos
