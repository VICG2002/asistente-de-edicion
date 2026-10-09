"""Densidad de dialogo y clasificacion A-roll / B-roll.

Que resuelve
------------
Hoy el asistente entrega una timeline "TODO EL MATERIAL" con 250 clips. El
editor tiene que separar a mano lo que sostiene la narrativa (A-roll) de lo que
la ilustra (B-roll).

Reglas del usuario
------------------
- **Documental**: las entrevistas son A-roll automaticamente, sin discutir.
- **Ficcion**: las tomas con mayor densidad de dialogo son A-roll.
- Todo lo demas es B-roll.

Todo lo que se usa aqui YA esta en el manifest. No se calcula nada nuevo ni se
llama a ningun modelo: se combinan senales existentes y se deja escrito POR QUE
cayo cada clip donde cayo, en español, para que se pueda auditar y corregir.
"""

from __future__ import annotations

import re

# --- roles ---------------------------------------------------------------
A_ROLL = "A"
B_ROLL = "B"
# Material de detras de camara: making-of, la camara de bolsillo que anda por el
# set, el registro del equipo trabajando. NO es B-roll — el B-roll ilustra la
# pieza y entra en el montaje; esto es otra pieza, o no es ninguna. Meterlo en
# B-roll obliga al editor a saltarselo clip a clip cada vez que busca un recurso.
# Se declara por camara en project_config.json (`bts_camaras`), porque cual es la
# camara de BTS solo lo sabe quien estuvo en el rodaje.
BTS = "BTS"
DESCARTE = "descarte"

# --- categorias ----------------------------------------------------------
# Documental: entrevista = A-roll, sin mirar la densidad. Regla del usuario.
CATEGORIAS_ENTREVISTA = {
    "entrevista", "entrevista-audio", "entrevista-solo-video", "dialogo-audio",
}
# Testimonio informal: A-roll solo si de verdad tiene contenido hablado.
CATEGORIAS_HABLA_INFORMAL = {"charla-equipo", "discurso", "accion-dialogo", "making-of"}
# Ni A ni B: no son material fuente.
CATEGORIAS_FUERA = {"export", "discard", "descarte"}

# --- pesos de la metrica -------------------------------------------------
PESO_COBERTURA = 0.50   # que fraccion del clip tiene habla densa
PESO_LEXICO = 0.35      # cuanto vocabulario distinto por minuto
PESO_INTENCION = 0.15   # alguien puso un lavalier / hay preguntas detectadas
REF_TASA_LEXICA = 60.0  # palabras distintas/min = el umbral de derive_video_categories

UMBRAL_HABLA_INFORMAL = 0.35   # documental, regla 2
UMBRAL_FICCION_PISO = 0.30     # ficcion: el percentil nunca baja de aqui
PERCENTIL_FICCION = 60         # ficcion: corte adaptativo
MARGEN_EMPATE = 0.05           # dentro de esto, gana A-roll


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def dialogue_density(*, duration_sec, speech_sec=0.0, distinct_words=0,
                     tiene_sync=False, tiene_preguntas=False,
                     is_hallucinated=False) -> tuple:
    """Devuelve (densidad, motivo_legible).

    CANDADO DURO: si el transcript esta alucinado, la densidad es 0. Sin esto,
    Whisper sobre musica ambient produce texto que parece denso ("Suscribete al
    canal" x100) y manda paisajes a A-roll. Es la leccion fundadora de Zezzions.
    """
    dur = float(duration_sec or 0)
    if dur <= 0:
        return 0.0, "sin duracion conocida"
    if is_hallucinated:
        return 0.0, "transcript no confiable (alucinado)"

    cobertura = clamp01(float(speech_sec or 0) / dur)
    tasa = float(distinct_words or 0) / (dur / 60.0)
    tasa_norm = clamp01(tasa / REF_TASA_LEXICA)
    intencion = 1.0 if (tiene_sync or tiene_preguntas) else 0.0

    d = clamp01(PESO_COBERTURA * cobertura
                + PESO_LEXICO * tasa_norm
                + PESO_INTENCION * intencion)

    partes = [f"habla {cobertura*100:.0f}% del clip",
              f"{tasa:.0f} palabras distintas/min"]
    if intencion:
        partes.append("con lavalier o preguntas detectadas")
    return d, ", ".join(partes)


# --------------------------------------------------------------------------
# Escena y toma (ficcion)
# --------------------------------------------------------------------------

# En ficcion los clips son TOMAS de una ESCENA. La decision se toma a nivel
# escena: si no, el inserto mudo de una escena de dialogo cae en B-roll y rompe
# la continuidad del A-roll.
_PATRONES_ESCENA = [
    # ESC12_T03 / ESC_12_T3 / ESCENA 12 TOMA 3
    re.compile(r"ESC(?:ENA)?[\s_-]*(\d+[A-Z]?)[\s_-]*(?:T(?:OMA)?[\s_-]*(\d+))?", re.I),
    # SC12_TK03 / SC 12 TK 3
    re.compile(r"\bSC[\s_-]*(\d+[A-Z]?)[\s_-]*(?:TK?[\s_-]*(\d+))?", re.I),
    # 12A-3 / 12A_3  (convencion americana: escena 12A, toma 3)
    re.compile(r"\b(\d{1,3}[A-Z]?)[-_](\d{1,2})\b"),
    # E12T03
    re.compile(r"\bE(\d+[A-Z]?)T(\d+)\b", re.I),
]


def detectar_escena(rel_path: str = "", filename: str = ""):
    """Devuelve (escena, toma) o (None, None).

    Busca en el nombre del archivo y en la ruta. Se queda con la coincidencia
    que ademas traiga TOMA: en `Escena 12/ESC12_T03.mov` la carpeta da la
    escena pero solo el archivo dice que toma es, y quedarse con la primera
    coincidencia perdia ese dato.
    """
    sin_toma = None
    for texto in (filename or "", rel_path or ""):
        if not texto:
            continue
        for pat in _PATRONES_ESCENA:
            for m in pat.finditer(texto):
                escena = (m.group(1) or "").upper()
                if not escena:
                    continue
                toma = m.group(2) if (m.lastindex or 0) >= 2 else None
                if toma:
                    return escena, toma
                if sin_toma is None:
                    sin_toma = (escena, None)
    return sin_toma if sin_toma else (None, None)


def percentil(valores, p: float) -> float:
    """Percentil por interpolacion lineal. Sin numpy: el motor corre con la
    Python del sistema en maquinas donde numpy puede no estar."""
    vals = sorted(float(v) for v in valores if v is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def umbral_ficcion(densidades) -> tuple:
    """Umbral adaptativo: percentil 60 del proyecto, con piso en 0.30.

    Por que no un umbral fijo: la densidad absoluta depende del guion. Una
    escena de accion con tres lineas es A-roll *en su pelicula*. Un umbral fijo
    clasificaria mal proyectos enteros.
    """
    p = percentil(densidades, PERCENTIL_FICCION)
    u = max(p, UMBRAL_FICCION_PISO)
    if u > p:
        return u, (f"piso {UMBRAL_FICCION_PISO:.2f} (el percentil "
                   f"{PERCENTIL_FICCION} del proyecto daba {p:.2f})")
    return u, f"percentil {PERCENTIL_FICCION} del proyecto"


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

def decidir_documental(*, categoria, es_entrevista, densidad) -> tuple:
    """(roll, motivo). Regla del usuario: entrevista = A-roll, punto."""
    cat = (categoria or "").strip().lower()
    if cat in CATEGORIAS_FUERA:
        return DESCARTE, f"categoria '{cat}' — no es material fuente"
    if cat in CATEGORIAS_ENTREVISTA or es_entrevista:
        origen = f"categoria '{cat}'" if cat in CATEGORIAS_ENTREVISTA else "marcado como entrevista"
        return A_ROLL, f"entrevista ({origen}) — regla documental"
    if cat in CATEGORIAS_HABLA_INFORMAL:
        if densidad >= UMBRAL_HABLA_INFORMAL:
            return A_ROLL, (f"'{cat}' con densidad {densidad:.2f} "
                            f">= {UMBRAL_HABLA_INFORMAL:.2f}")
        return B_ROLL, (f"'{cat}' pero densidad {densidad:.2f} "
                        f"< {UMBRAL_HABLA_INFORMAL:.2f}")
    return B_ROLL, (f"categoria '{cat}'" if cat else "sin categoria de habla")


def decidir_comercial(*, categoria, es_toma, densidad=0.0,
                      es_entrevista=False) -> tuple:
    """(roll, motivo). Regla de pieza con guion: la toma a camara es A-roll.

    En un comercial, una capsula o cualquier pieza con texto a camara, el A-roll
    no hay que adivinarlo por densidad: es el material en que alguien dice el
    texto, y `derive_takes.py` ya lo sabe porque agrupa las tomas justamente por
    el texto que dicen. La señal es directa, no hace falta inventar un umbral.

    Todo lo demas es B-roll por definicion —recurso, detalle, dron, ambiente—
    tenga o no gente hablando de fondo: la charla de rodaje no sostiene la pieza.
    La `densidad` se recibe solo para dejarla escrita en el informe.

    SEGUNDA VIA A A-ROLL — `es_entrevista` (2026-08-18, cobertura de agosto dia 2).
    Un comercial puede traer entrevistas ademas de las tomas a camara: alguien
    responde preguntas y eso sostiene la pieza igual que el texto guionizado,
    pero NO se agrupa en `clip_takes` porque cada respuesta es distinta. Con la
    regla pura esas entrevistas caian en B-roll — en el dia 2 de esa cobertura
    eran cinco clips de 3 a 9 minutos, el material principal del encargo.

    Es OPT-IN y se declara: `"aroll_incluye_entrevistas": true` en el
    project_config. Apagado (el default) ningun proyecto anterior cambia de
    comportamiento. Se declara y no se infiere por la misma razon que
    `project_kind`: desde el manifest, una entrevista y una charla de rodaje
    larga se parecen demasiado.

    Nacio de IMODAE (2026-08-07), donde `project_kind: "comercial"` estaba
    declarado en el project_config y el motor lo ignoraba: caia al inferidor,
    decidia 'ficcion' y repartia por umbral adaptativo sin decir que habia
    descartado el campo.
    """
    cat = (categoria or "").strip().lower()
    if cat in CATEGORIAS_FUERA:
        return DESCARTE, f"categoria '{cat}' — no es material fuente"
    if es_toma:
        return A_ROLL, ("dice el texto guionizado (agrupado como toma por "
                        "derive_takes) — regla de pieza con guion")
    if es_entrevista:
        return A_ROLL, (f"entrevista (categoria '{cat}'): sostiene la pieza "
                        "aunque no repita un texto guionizado — "
                        "`aroll_incluye_entrevistas`")
    return B_ROLL, ("no dice texto guionizado: recurso"
                    + (f" (categoria '{cat}')" if cat else ""))


def decidir_ficcion(*, categoria, densidad, umbral, densidad_escena=None,
                    escena=None) -> tuple:
    """(roll, motivo). Regla del usuario: mayor densidad de dialogo = A-roll."""
    cat = (categoria or "").strip().lower()
    if cat in CATEGORIAS_FUERA:
        return DESCARTE, f"categoria '{cat}' — no es material fuente"

    if densidad_escena is not None:
        d, de_donde = densidad_escena, f"escena {escena} (mediana de sus tomas)"
    else:
        d, de_donde = densidad, "clip suelto (sin escena reconocible)"

    if d >= umbral:
        return A_ROLL, f"densidad {d:.2f} >= umbral {umbral:.2f} — {de_donde}"
    if d >= umbral - MARGEN_EMPATE:
        # Es mas barato revisar un clip de mas en A-roll que perder una toma
        # de dialogo en B-roll.
        return A_ROLL, (f"densidad {d:.2f} a menos de {MARGEN_EMPATE:.2f} del "
                        f"umbral {umbral:.2f} — empate, se resuelve a favor de "
                        f"A-roll ({de_donde})")
    return B_ROLL, f"densidad {d:.2f} < umbral {umbral:.2f} — {de_donde}"


def mediana(valores) -> float:
    vals = sorted(float(v) for v in valores if v is not None)
    if not vals:
        return 0.0
    n = len(vals)
    if n % 2:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2.0
