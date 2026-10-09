"""A que pieza (capsula, reel, spot) pertenece cada toma de un rodaje con guion.

EL PROBLEMA
Un rodaje de campaña no graba una pieza: graba cinco o seis, el mismo dia, con
el mismo equipo y la misma ropa. Al volver, el material es una sola cronologia
plana y el editor no tiene forma de saber donde acaba una capsula y empieza la
siguiente si no es abriendo clips. En IMODAE (2026-08-07) eran seis capsulas
declaradas en el brief contra 178 clips en tres camaras.

COMO SE DECIDE
El brief de cada capsula trae titulo, tema, escenario y —si alguien las
escribio— palabras clave. Eso es un bolson de terminos. El transcript de cada
grupo de tomas se puntua contra los bolsones y gana el que mas puntos saca.

Un termino de varias palabras vale mas que uno suelto: que aparezca "mexican
chic" dice muchisimo mas que que aparezca "chic". Por eso los puntos de un
termino son sus palabras, y por eso los terminos se consumen de mas largo a mas
corto sin solaparse — sin eso, "dos mujeres" y "mujeres" contarian dos veces la
misma evidencia y el score dejaria de significar nada.

LO QUE NO HACE
No lee el guion literal. En IMODAE el brief traia frases de hook cortas
("¿Que buscas primero cuando armas un look?") y en el set se escribieron textos
de prompter mucho mas largos que no comparten una sola frase con el brief. Un
matcheo por texto literal habria dado cero en las seis capsulas. Por eso el
vocabulario del TEMA es la señal, no el guion.

Tampoco decide sola: `bin/derive_reels.py` escribe un informe con la evidencia
de cada asignacion y con el segundo candidato, y el editor corrige con
`reel_overrides` en project_config.json. La IA ejecuta, el editor decide.
"""

from __future__ import annotations

import calendar
import re
import unicodedata

# Puntos minimos para dar una capsula por buena. Con "puntos = palabras del
# termino", un solo nombre propio suelto ("karla") vale 1 y no alcanza; un tema
# de verdad ("mexican chic" + "lujo mexicano") pasa de largo. Deliberadamente
# bajo: un falso negativo deja al grupo sin marcador y el editor no se entera de
# que existia la duda; un falso positivo sale en el informe con su evidencia al
# lado y se corrige en un renglon.
MIN_PUNTOS_DEFECTO = 3

# Si el segundo candidato queda a menos de esto del ganador, la asignacion se
# marca como ambigua. NO se descarta: se asigna y se señala, para que el editor
# tenga algo que corregir en vez de un hueco que nadie ve.
MARGEN_AMBIGUO = 0.20

# Campos del brief de los que sale el bolson de terminos, en orden de fuerza.
CAMPOS_TERMINOS = ("palabras_clave", "tema", "titulo", "escenario", "plataforma")


def normalizar(s: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, con un espacio de guarda a cada
    lado para poder buscar terminos como palabras completas y no como subcadenas
    ('joya' no debe casar dentro de 'joyas' por accidente... y 'joyas' se pone
    como termino aparte si hace falta)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).lower().strip()
    return f" {s} " if s else " "


def bolsa_de_terminos(capsula: dict) -> list[str]:
    """Los terminos de una capsula, normalizados, sin repetidos, de mas largo a
    mas corto. El orden importa: al puntuar se consumen en ese orden."""
    crudos: list[str] = []
    for campo in CAMPOS_TERMINOS:
        v = capsula.get(campo)
        if isinstance(v, str):
            crudos.extend(re.split(r"[,;/]| — |\. ", v))
        elif isinstance(v, (list, tuple)):
            crudos.extend(str(x) for x in v)
    vistos, out = set(), []
    for t in crudos:
        n = normalizar(t).strip()
        if len(n) < 3 or n in vistos:
            continue
        vistos.add(n)
        out.append(n)
    out.sort(key=lambda t: (-len(t.split()), -len(t)))
    return out


def puntuar(texto: str, terminos: list[str]) -> tuple[int, list[str]]:
    """(puntos, terminos que hicieron match). Un termino vale sus palabras.

    Los matches no se solapan: cada tramo del texto lo consume el termino mas
    largo que lo cubra. Asi "dos mujeres creativas" suma por "dos mujeres" o por
    "creativas", pero nunca por los dos encima del mismo texto.
    """
    plano = normalizar(texto)
    consumido = bytearray(len(plano))
    puntos, evidencia = 0, []
    for t in terminos:
        aguja = f" {t} "
        libre = False
        desde = 0
        while True:
            i = plano.find(aguja, desde)
            if i < 0:
                break
            # El espacio de guarda de la izquierda puede ser el de la derecha
            # del match anterior: se comprueba el nucleo, no los bordes.
            ini, fin = i + 1, i + len(aguja) - 1
            if not any(consumido[ini:fin]):
                for k in range(ini, fin):
                    consumido[k] = 1
                libre = True
                break
            desde = i + 1
        if libre:
            puntos += len(t.split())
            evidencia.append(t)
    return puntos, evidencia


def elegir_capsula(texto: str, capsulas: list[dict],
                   min_puntos: int = MIN_PUNTOS_DEFECTO) -> dict:
    """Puntua el texto contra todas las capsulas y devuelve el veredicto.

    Siempre devuelve el ranking completo, tambien cuando no alcanza el minimo:
    el informe lo necesita para que el editor vea por que NO se asigno.
    """
    ranking = []
    for c in capsulas:
        p, ev = puntuar(texto, bolsa_de_terminos(c))
        ranking.append({"n": c.get("n"), "titulo": c.get("titulo", ""),
                        "puntos": p, "evidencia": ev})
    ranking.sort(key=lambda r: (-r["puntos"], r["n"] or 0))
    mejor = ranking[0] if ranking else None
    segundo = ranking[1] if len(ranking) > 1 else None
    if not mejor or mejor["puntos"] < min_puntos:
        return {"reel_n": None, "titulo": "", "evidencia": [],
                "puntos": mejor["puntos"] if mejor else 0,
                "ambiguo": False, "ranking": ranking,
                "motivo": f"ningun tema alcanza los {min_puntos} puntos minimos"}
    ambiguo = bool(segundo and segundo["puntos"] > 0
                   and (mejor["puntos"] - segundo["puntos"])
                   < MARGEN_AMBIGUO * mejor["puntos"])
    return {"reel_n": mejor["n"], "titulo": mejor["titulo"],
            "puntos": mejor["puntos"], "evidencia": mejor["evidencia"],
            "ambiguo": ambiguo, "ranking": ranking,
            "motivo": "vocabulario del tema: " + ", ".join(mejor["evidencia"][:6])}


# --- segmentacion dentro del clip -----------------------------------------
#
# POR QUE EXISTE
# Un grupo de tomas caia entero en UNA capsula, y si un clip cubria dos, la
# segunda desaparecia del reparto sin que nada lo dijera. Caso fundador: IMODAE
# C1194, 69 s que arrancan con el look de viaje (capsula 3) y siguen con los
# maxi accesorios (capsula 1). El vocabulario de viaje gano por puntos, el grupo
# entero fue a la 3, y se reporto que la capsula 1 no tenia material.
#
# PRECISION: la ventana usa los timestamps de palabra de Whisper, que ESTAN
# MEDIDOS y derivan (ver metodologia/reels-y-cortes.md § 4). Sirven para decir
# "por aqui empieza otro tema", que es un apoyo de navegacion, NO para cortar.
# Nada de lo que sale de aqui alimenta un corte.
VENTANA_PALABRAS = 30
PASO_PALABRAS = 15
MIN_PUNTOS_VENTANA = 2      # una ventana es corta: el umbral del grupo la mataria
MIN_DUR_SEGMENTO = 4.0

# Cuanto tiene que superar una capsula SECUNDARIA al tema dominante del clip
# para que se acepte que ahi se cambio de tema.
#
# Sin esto, una frase suelta que casa con otra capsula abre un segmento falso.
# Los dos casos que fijaron el numero (IMODAE, 2026-08-13):
#
#   C1194 31-46s  capsula 1: 1 pto · capsula 3 (dominante): 0 ptos  -> VERDADERO
#     "Y aqui entran los maxi accesorios. Esta coleccion..." El tema dominante
#     DESAPARECE de la ventana: es un cambio de tema de verdad.
#
#   C1170 35-53s  capsula 6: 4 ptos · capsula 4 (dominante): 3 ptos -> FALSO
#     "Es hacerla parte de tu vida" casa con el cierre del hero video, pero el
#     clip sigue hablando de la capsula 4 ahi mismo. Es coincidencia de frase.
#
# Con factor 2: 1 > 2*0 pasa; 4 > 2*3 no pasa. Un tema que de verdad cambia deja
# de mencionar el anterior.
FACTOR_SECUNDARIA = 2.0


def _pares_palabra_tiempo(words) -> list[tuple[str, float]]:
    """Normaliza el formato del cache: `[palabra, t]` o dict. Ignora lo demas."""
    out = []
    for w in words or []:
        if isinstance(w, (list, tuple)) and len(w) >= 2 and isinstance(w[1], (int, float)):
            out.append((str(w[0]), float(w[1])))
        elif isinstance(w, dict):
            for k in ("s", "start", "t", "time"):
                if isinstance(w.get(k), (int, float)):
                    out.append((str(w.get("w") or w.get("word") or ""), float(w[k])))
                    break
    return out


def segmentar_por_capsula(words, capsulas: list[dict], *,
                          dominante: int | None = None,
                          ventana: int = VENTANA_PALABRAS,
                          paso: int = PASO_PALABRAS,
                          min_puntos: int = MIN_PUNTOS_VENTANA,
                          min_dur: float = MIN_DUR_SEGMENTO,
                          factor: float = FACTOR_SECUNDARIA) -> list[dict]:
    """Que capsula se habla en cada tramo del clip.

    `dominante` es la capsula a la que ya esta asignado el clip. Una capsula
    DISTINTA solo abre segmento si supera al tema dominante en esa ventana por
    `factor` — o sea, si el tema dominante practicamente ha desaparecido. Sin esa
    condicion, una frase suelta que casa con otra capsula abre un segmento falso.

    Devuelve [{s, e, reel_n, puntos, evidencia}, ...] en orden y SIN solaparse.
    """
    pares = _pares_palabra_tiempo(words)
    if len(pares) < 5 or not capsulas:
        return []
    bolsas = [(c.get("n"), c.get("titulo", ""), bolsa_de_terminos(c)) for c in capsulas]

    ventanas = []
    for i in range(0, max(1, len(pares) - 1), max(1, paso)):
        trozo = pares[i:i + ventana]
        if len(trozo) < 5:
            break
        texto = " ".join(p[0] for p in trozo)
        marcador, dom_pts = None, 0
        for n, titulo, terminos in bolsas:
            p, ev = puntuar(texto, terminos)
            if n == dominante:
                dom_pts = p
            if marcador is None or p > marcador[0]:
                marcador = (p, n, titulo, ev)
        pts, n, titulo, ev = marcador
        # Una ventana necesita `min_puntos` de verdad. Se probo bajarlo a 1 con
        # la idea de que una señal debil pero sostenida bastara, y sobre el
        # material real aparecieron dos falsos positivos inmediatos: "accesorios"
        # dicho de pasada dentro del discurso de otra capsula, y "cafeteria" en
        # una charla de rodaje. Un termino suelto no es un cambio de tema.
        vale = pts >= min_puntos
        if vale and dominante is not None and n != dominante:
            vale = pts > factor * dom_pts
        ventanas.append({
            "s": trozo[0][1], "e": trozo[-1][1],
            "reel_n": n if vale else None, "titulo": titulo if vale else "",
            "puntos": pts, "evidencia": ev, "n_ventanas": 1,
        })

    # Fusionar ventanas consecutivas de la misma capsula. Se solapan a
    # proposito: una frase a caballo entre dos ventanas se ve en las dos.
    segmentos: list[dict] = []
    for v in ventanas:
        if v["reel_n"] is None:
            continue
        if segmentos and segmentos[-1]["reel_n"] == v["reel_n"] \
                and v["s"] <= segmentos[-1]["e"] + 1e-6:
            u = segmentos[-1]
            u["e"] = max(u["e"], v["e"])
            u["puntos"] = max(u["puntos"], v["puntos"])
            u["n_ventanas"] += 1
            for t in v["evidencia"]:
                if t not in u["evidencia"]:
                    u["evidencia"].append(t)
        else:
            segmentos.append(dict(v, evidencia=list(v["evidencia"])))

    # Recortar solapes entre capsulas distintas: las ventanas se solapan por
    # diseño, y dos segmentos que se pisan son una contradiccion ("aqui se habla
    # de la 4 y tambien de la 6"). Gana el que empieza antes; el siguiente
    # arranca donde acaba aquel.
    for a, b in zip(segmentos, segmentos[1:]):
        if b["s"] < a["e"]:
            b["s"] = a["e"]

    return [s for s in segmentos if s["e"] - s["s"] >= min_dur]


def capsulas_sin_material(capsulas: list[dict], asignadas, en_segmentos) -> list[dict]:
    """Las capsulas declaradas que no aparecen por ningun lado.

    Existe para que nadie vuelva a decir "esta capsula no se rodo" sin haberlo
    comprobado. Distingue tres estados, y solo el tercero es "no hay material":
    asignada, presente solo dentro de otro clip, o ausente de verdad.
    """
    out = []
    for c in capsulas:
        n = c.get("n")
        if n in asignadas:
            continue
        out.append({"n": n, "titulo": c.get("titulo", ""),
                    "solo_en_segmentos": n in en_segmentos})
    return out


_RE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def epoch_de(creation_time: str | None) -> float | None:
    """El `creation_time` del manifest a epoch. Se lee como UTC siempre: lo que
    importa es que la escala sea la misma para todos, no que sea la hora local
    correcta — de corregir cada camara se encarga el skew."""
    m = _RE_ISO.search(creation_time or "")
    if not m:
        return None
    return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))


def tiempo_real(creation_time: str | None, skew: float) -> float | None:
    """Reloj de la camara corregido. Misma convencion que el resto del motor
    (`asistente_*.lua`, `derive_chrono_sync.py`): negativo = la camara va
    atrasada, y el tiempo real se obtiene RESTANDO el skew."""
    ep = epoch_de(creation_time)
    return None if ep is None else ep - skew


def ventanas(asignados: list[tuple[int, float]]) -> dict[int, tuple[float, float]]:
    """{reel_n: (t0, t1)} a partir de [(reel_n, tiempo_real), ...].

    Es el tramo del dia en que se rodo esa capsula. Sirve para repartir el
    material sin dialogo —dron, planos de detalle, la segunda camara— que no
    tiene texto contra el que puntuar pero si tiene hora.
    """
    out: dict[int, tuple[float, float]] = {}
    for n, t in asignados:
        if n is None or t is None:
            continue
        t0, t1 = out.get(n, (t, t))
        out[n] = (min(t0, t), max(t1, t))
    return out


def reel_de_ventana(t: float | None,
                    vent: dict[int, tuple[float, float]]) -> tuple[int | None, str]:
    """(reel_n, motivo) para un clip sin dialogo, por la hora a la que se rodo.

    Si cae dentro de dos ventanas solapadas no se elige ninguna: dos capsulas
    rodandose a la vez es exactamente el caso en que la hora deja de ser
    evidencia, y adivinar ahi es peor que dejarlo sin asignar.
    """
    if t is None:
        return None, "sin hora de grabacion utilizable"
    dentro = [n for n, (t0, t1) in vent.items() if t0 <= t <= t1]
    if len(dentro) == 1:
        return dentro[0], "por hora, dentro de la ventana en que se rodo la capsula"
    if len(dentro) > 1:
        return None, ("cae en " + str(len(dentro)) + " ventanas solapadas "
                      + str(sorted(dentro)) + " — la hora no basta para decidir")
    return None, "fuera de las ventanas de todas las capsulas"


def leer_overrides(cfg: dict) -> tuple[dict[int, int | None], dict[int, int | None]]:
    """`reel_overrides` de project_config.json -> (por_grupo, por_clip).

        "reel_overrides": {"grupo:96": 4, "clip:1234": 2, "grupo:101": null}

    `null` (o "ninguno") fuerza SIN REEL: sirve para sacar del reparto la charla
    de rodaje que el vocabulario emparejo por casualidad.
    """
    por_grupo: dict[int, int | None] = {}
    por_clip: dict[int, int | None] = {}
    for k, v in (cfg.get("reel_overrides") or {}).items():
        texto = str(k).strip().lower()
        destino = por_clip if texto.startswith("clip:") else por_grupo
        cuerpo = texto.split(":", 1)[1] if ":" in texto else texto
        if not cuerpo.strip().isdigit():
            continue
        if v is None or str(v).strip().lower() in ("ninguno", "none", ""):
            destino[int(cuerpo)] = None
        else:
            try:
                destino[int(cuerpo)] = int(v)
            except (TypeError, ValueError):
                continue
    return por_grupo, por_clip
