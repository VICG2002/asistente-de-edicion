"""Que Input Color Space y que Input Gamma le toca a cada camara en Resolve.

POR QUE EXISTE
--------------
Peticion del editor (2026-08-18): "necesito que puedas decirme el input color
space e input gamma de cada camara". Es lo primero que hay que rellenar al abrir
un proyecto con Color Management, y equivocarse ahi no da error: da una imagen
plana o quemada que parece un problema de etalonaje.

QUE ES DATO Y QUE ES DECLARACION
--------------------------------
La distincion vertebra este modulo, igual que con el rol de una camara:

  · La GAMMA se puede medir casi siempre. Sony la escribe literal en el sidecar
    XAVC (`CaptureGammaEquation`); el resto de camaras la dejan (o no) en los
    tags del contenedor.
  · El GAMUT casi nunca. Sony pone `CaptureColorPrimaries: rec709` en el XML
    aunque el perfil de imagen sea S-Gamut3.Cine: ese campo describe la
    codificacion del contenedor, no el espacio de captura. Quien puso la camara
    en PP8 o en PP9 lo sabe; el archivo no.

Asi que este modulo MIDE lo que se puede medir, PROPONE lo mas probable para lo
que no, y lo dice con esas palabras. Nunca afirma un gamut que no puede ver.

TERCERA SEÑAL: LA IMAGEN
------------------------
Un contenedor puede mentir. DJI etiqueta `bt709` tanto en Normal como en D-Log M,
asi que el tag no distingue. La luma si: una curva log levanta el negro y
comprime el rango. En 10 bits, el negro legal es 64 y el blanco 940; S-Log3 deja
el negro sobre ~95. Medido en el dia 2 de esa cobertura:

    Sony (s-log3 confirmado por XML)   YMIN 94-110   -> negro levantado
    DJI Osmo Pocket 3                  YMIN 73-86, YMAX hasta 990  -> Rec.709

Es corroboracion, no veredicto: un plano nocturno tambien tiene el negro alto.
"""

from __future__ import annotations

# Nombres EXACTOS de los desplegables de Resolve. Si no coinciden letra por
# letra, el editor no los encuentra en la lista y el modulo no sirve de nada.
RESOLVE = {
    "slog3_cine":  ("S-Gamut3.Cine", "S-Log3"),
    "slog3":       ("S-Gamut3", "S-Log3"),
    "slog2":       ("S-Gamut", "S-Log2"),
    "rec709":      ("Rec.709", "Rec.709 Gamma 2.4"),
    "rec709_srgb": ("Rec.709", "sRGB"),
    "hlg":         ("Rec.2020", "Rec.2100 HLG"),
    "pq":          ("Rec.2020", "Rec.2100 ST2084"),
    "dlog":        ("DJI D-Gamut", "DJI D-Log"),
    "vlog":        ("V-Gamut", "V-Log"),
    "clog3":       ("Cinema Gamut", "Canon Log 3"),
    "clog2":       ("Cinema Gamut", "Canon Log 2"),
    "logc3":       ("ARRI Wide Gamut", "ARRI LogC3"),
    "bmd_film":    ("Blackmagic Design", "Blackmagic Design Film"),
}

# Lo que escribe Sony en `CaptureGammaEquation` del sidecar XAVC.
GAMMA_SONY = {
    "s-log3": "slog3_cine",   # PP8 de fabrica; PP9 usa S-Gamut3 — se pregunta
    "s-log2": "slog2",
    "hlg": "hlg", "hlg1": "hlg", "hlg2": "hlg", "hlg3": "hlg",
    "rec709": "rec709", "itu709": "rec709", "same-as-rec709": "rec709",
    "cine1": "rec709", "cine2": "rec709", "cine4": "rec709",
}

# Tags de transferencia del contenedor (ffprobe `color_transfer`).
TRANSFER = {
    "bt709": "rec709", "smpte170m": "rec709", "bt470bg": "rec709",
    "iec61966-2-1": "rec709_srgb",
    "arib-std-b67": "hlg",
    "smpte2084": "pq",
}


def por_sidecar_sony(gamma_xml: str):
    """(clave, evidencia) desde CaptureGammaEquation. La gamma es DATO."""
    g = (gamma_xml or "").strip().lower()
    clave = GAMMA_SONY.get(g)
    if not clave:
        return None, ""
    return clave, f"sidecar XAVC: CaptureGammaEquation = {g}"


def por_contenedor(transfer: str, primaries: str):
    """(clave, evidencia) desde los tags del MP4."""
    t = (transfer or "").strip().lower()
    if t in ("", "unknown", "reserved"):
        return None, ""
    clave = TRANSFER.get(t)
    if not clave:
        return None, ""
    ev = f"contenedor: color_transfer = {t}"
    if primaries and primaries.lower() not in ("", "unknown"):
        ev += f", color_primaries = {primaries.lower()}"
    return clave, ev


def leer_luma(ymin: float, ymax: float, bits: int = 10):
    """(veredicto, explicacion) a partir del negro y el blanco medidos.

    Corrobora, no decide. Devuelve 'log', 'directo' o 'dudoso'.
    """
    if ymin is None or ymax is None:
        return "dudoso", "sin medicion de luma"
    esc = 1023.0 if bits >= 10 else 255.0
    negro_legal = 64.0 * (esc / 1023.0)
    blanco_legal = 940.0 * (esc / 1023.0)
    if ymin >= negro_legal * 1.4 and ymax <= blanco_legal:
        return "log", (f"negro en {ymin:.0f} (legal {negro_legal:.0f}) y blanco "
                       f"en {ymax:.0f}: curva que levanta el pie")
    if ymin <= negro_legal * 1.25 or ymax > blanco_legal:
        return "directo", (f"negro en {ymin:.0f} y blanco en {ymax:.0f}: usa el "
                           "rango entero, no parece log")
    return "dudoso", f"negro en {ymin:.0f}, blanco en {ymax:.0f}: no concluyente"


def decidir(*, gamma_xml=None, transfer=None, primaries=None,
            luma=None, declarado=None):
    """Devuelve dict con espacio, gamma, confianza, evidencia y avisos.

    Orden de autoridad: lo DECLARADO por el editor > el sidecar > el contenedor.
    La luma corrobora o levanta la mano, nunca manda.
    """
    avisos = []

    if declarado:
        esp = declarado.get("input_color_space")
        gam = declarado.get("input_gamma")
        if esp and gam:
            return {"espacio": esp, "gamma": gam, "confianza": "declarado",
                    "evidencia": declarado.get("nota")
                                 or "declarado en project_config.json",
                    "avisos": []}

    clave, ev = por_sidecar_sony(gamma_xml)
    if not clave:
        clave, ev = por_contenedor(transfer, primaries)

    if not clave:
        return {"espacio": None, "gamma": None, "confianza": "sin datos",
                "evidencia": "ni el sidecar ni el contenedor dicen nada",
                "avisos": ["Hay que preguntarle a quien puso la camara."]}

    esp, gam = RESOLVE[clave]
    es_log = clave in ("slog3_cine", "slog3", "slog2", "dlog", "vlog",
                       "clog3", "clog2", "logc3", "bmd_film")

    # El gamut de Sony NO se puede leer: el XML dice rec709 aunque el perfil sea
    # S-Gamut3.Cine. Se propone el de fabrica y se pide confirmacion.
    if clave == "slog3_cine":
        avisos.append(
            "La GAMMA (S-Log3) es dato del sidecar. El GAMUT no: Sony escribe "
            "'rec709' en CaptureColorPrimaries aunque la camara este en "
            "S-Gamut3.Cine. Se propone S-Gamut3.Cine (PP8 de fabrica); si el "
            "rodaje uso PP9, es S-Gamut3. Lo sabe quien puso la camara.")

    if luma:
        veredicto, expl = luma
        if es_log and veredicto == "directo":
            avisos.append(
                f"CONTRADICCION: los metadatos dicen log y la imagen no ({expl}). "
                "Mirar un fotograma antes de fijar el color management.")
        elif (not es_log) and veredicto == "log":
            avisos.append(
                f"CONTRADICCION: los metadatos dicen Rec.709 y la imagen parece "
                f"log ({expl}). Algunas camaras (DJI) etiquetan bt709 tambien en "
                "D-Log; comprobar el modo de grabacion.")
        else:
            ev += f"; luma corrobora ({expl})"

    return {"espacio": esp, "gamma": gam,
            "confianza": "medido" if not avisos else "medido, con reservas",
            "evidencia": ev, "avisos": avisos}
