"""De silencios medidos a tramos que se conservan. Aritmetica de intervalos.

QUE RESUELVE
En un rodaje con prompter, el locutor se para a esperar a que avance el texto.
Esas esperas no son ritmo, son tiempo muerto: con quince tomas del mismo texto
delante, el editor pasa la tarde esperando junto con el locutor. Quitarlas deja
las tomas comparables de un vistazo.

DONDE ESTA LA AUTORIDAD
En el audio, no en el transcript. `detect_pauses.py` mide el silencio real con
ffmpeg; aqui solo se hace la aritmetica. La tentacion es usar los timestamps de
palabra de Whisper para decidir donde hay habla, y esta MEDIDO que no se puede:
en IMODAE (2026-08-07) los word-timings acertaban en unos clips y derivaban
varios segundos en otros. En C1178 el transcript declaraba un hueco en 29-31 s
donde el audio tiene voz, y no veia el de 37-40 s que el audio si tiene. Un
corte guiado por esos tiempos habria cortado habla en unos clips y no habria
cortado nada en otros.

EL AIRE ES LA SEGURIDAD
De cada silencio no se quita todo: se dejan `aire` segundos a cada lado, del
lado del habla. `silencedetect` marca donde el nivel cruza el umbral, y el
ataque de una consonante cruza tarde. Sin colchon, el corte se come la 'p' de
la palabra siguiente y suena a error de edicion, no a corte.

NADA DE ESTO ES DESTRUCTIVO. Los tramos son coordenadas: viven en la timeline,
las fuentes no se tocan, y re-hornear con el corte apagado devuelve la toma
entera.
"""

from __future__ import annotations

# Silencio minimo que merece un corte. Por debajo son respiraciones y cambios de
# frase: quitarlas no ahorra tiempo y si rompe el fraseo.
MIN_SILENCIO = 1.5
# Colchon a cada lado, en segundos.
AIRE = 0.3
# Si despues del aire queda menos que esto por quitar, no se corta: dos splices
# para ahorrar dos decimas es peor que la pausa.
MIN_QUITAR = 0.4
# Un tramo conservado mas corto que esto no es un plano, es un parpadeo. En vez
# de dejarlo suelto se absorbe en el corte de al lado.
MIN_TRAMO = 0.5


def normalizar(rangos: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ordena, descarta los vacios y fusiona los que se solapan o se tocan."""
    limpios = sorted((float(a), float(b)) for a, b in rangos if float(b) > float(a))
    out: list[tuple[float, float]] = []
    for a, b in limpios:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def encoger(rangos: list[tuple[float, float]], aire: float,
            min_resto: float) -> list[tuple[float, float]]:
    """Deja `aire` segundos de cada silencio pegados al habla de cada lado.

    Lo que sobra es lo que se quita. Si sobra menos de `min_resto`, ese silencio
    no se toca: el corte no compensaria.
    """
    out = []
    for a, b in rangos:
        na, nb = a + aire, b - aire
        if nb - na >= min_resto:
            out.append((na, nb))
    return out


def fusionar_cercanos(rangos: list[tuple[float, float]],
                      hueco_min: float) -> list[tuple[float, float]]:
    """Une dos cortes separados por menos de `hueco_min` de sonido.

    Ese sonido de dos decimas entre dos pausas largas es una tos, un carraspeo o
    el 'eh' de arrancar otra vez. Dejarlo suelto produce un fotograma de nada
    entre dos cortes; absorberlo produce un corte limpio.
    """
    if not rangos:
        return []
    out = [rangos[0]]
    for a, b in rangos[1:]:
        if a - out[-1][1] < hueco_min:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def restar(total: tuple[float, float],
           quitar: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """El complemento: `total` menos los tramos de `quitar`."""
    ini, fin = total
    out, cursor = [], ini
    for a, b in normalizar(quitar):
        a, b = max(a, ini), min(b, fin)
        if b <= cursor:
            continue
        if a > cursor:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < fin:
        out.append((cursor, fin))
    return [(a, b) for a, b in out if b > a]


def plan_de_corte(dur: float, silencios: list[tuple[float, float]], *,
                  aire: float = AIRE, min_silencio: float = MIN_SILENCIO,
                  min_quitar: float = MIN_QUITAR, min_tramo: float = MIN_TRAMO,
                  entrar_desde: float | None = None) -> dict:
    """El plan completo de un clip.

    Devuelve {'conservar': [(s,e)…], 'quitar': [(s,e)…], 'entrada': float,
    'quitado_sec': float}. `conservar` es lo que baja a la timeline, en orden.

    `entrar_desde` recorta la cabeza del clip: es el `entrar_desde_sec` que
    `derive_takes.py` calcula cuando la toma buena viene DESPUES de una charla
    de rodaje. Se trata como un corte mas para que el editor vea un solo
    mecanismo y no dos.
    """
    dur = max(0.0, float(dur or 0.0))
    ini = 0.0
    if entrar_desde is not None and 0.0 < float(entrar_desde) < dur:
        ini = float(entrar_desde)

    utiles = [(a, b) for a, b in normalizar(silencios)
              if b - a >= min_silencio and b > ini]
    quitar = fusionar_cercanos(
        encoger(utiles, aire, min_quitar), min_tramo)
    quitar = [(max(a, ini), min(b, dur)) for a, b in quitar]
    quitar = [(a, b) for a, b in quitar if b - a >= min_quitar]

    conservar = restar((ini, dur), quitar)
    # Un tramo conservado ridiculo al principio o al final tampoco sirve. Lo
    # que se descarta pasa a `quitar`: todo segundo del clip desde la entrada
    # esta conservado o cortado, y lo cortado se re-mide en verify_cortes. Antes
    # el descarte no quedaba en ningun lado y quitado_sec no lo contaba
    # (revision del PR #1, 2026-10-05).
    cortos = [(a, b) for a, b in conservar if b - a < min_tramo]
    conservar = [(a, b) for a, b in conservar if b - a >= min_tramo]
    if cortos:
        quitar = normalizar(quitar + cortos)
    return {
        "conservar": conservar,
        "quitar": quitar,
        "entrada": ini,
        "quitado_sec": sum(b - a for a, b in quitar) + ini,
    }
