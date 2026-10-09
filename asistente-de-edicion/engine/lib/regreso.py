"""Verificar lo que volvio de Resolve por el canal de regreso (el .drt).

POR QUE EXISTE (2026-10-05)
El canal de regreso elegido para Resolve Free es `Timeline:Export` del .drt: el
aplicador (`resolve/aplicar.lua`) exporta cada timeline que construyo, y su
reporte, a la carpeta de recibos del pedido. Lua no tiene `io` en el menu de
Free, pero el archivo lo escribe Resolve. Aqui el motor lo lee con
`lib/drt.py` y compara contra `<recibos>/pedido.json`, la copia que dejo
`lib/pedido.py` al escribir el pedido. Nadie mira Resolve.

QUE SE EXIGE (un resultado solo cuenta si el efecto se comprueba fuera)
  Por cada timeline que el pedido construye (<recibos>/<clave>.drt):
    - que el archivo exista y se deje leer;
    - el nombre que el pedido pidio;
    - el marker del frame 0 con customData "diez50:sello=<sello>";
    - cada pista en su indice, con su nombre y al menos un clip;
    - la regla dura: ningun audio externo (LAVA) por encima de un audio de
      camara (CAM), la misma que LIB.verificarOrdenPistas;
    - que el .drt sea posterior al pedido (si no, es un recibo viejo).
  Del reporte (<recibos>/reporte.drt):
    - el marker "diez50:reporte=<sello>" en el frame 0;
    - el nombre "Diez50 · <sello_corto> · <estado> · <n> de <m>", con OK y
      n igual a m. AVISOS deja el veredicto en amarillo; ERROR, en rojo.

  Por cada `construir` (Fase 1, 2026-10-06), ademas:
    - que lleguen A-ROLL, B-ROLL y AUDIOS EXTERNOS si el pedido las espera
      (aroll.drt, broll.drt, audios_externos.drt), y las sub_<n>.drt que haya;
    - en cada una: nombre, sello en el frame 0 y la regla dura;
    - COBERTURA FUERA DE RESOLVE: las rutas de los clips en las pistas de video
      de A-ROLL y B-ROLL contra la lista del pedido (esperado.clips), y los WAV
      de AUDIOS EXTERNOS contra esperado.audios. Lo que falta y el reporte
      explica ("faltan en el pool: N (nombres)", "clips sin hora utilizable
      (fuera del B-ROLL): M (nombres)") deja amarillo; lo que falta sin
      explicacion, rojo. Se cruza por NOMBRE con lo que el reporte nombra, y
      solo lo que no nombra se cuenta (revision del 2026-10-07);
    - un clip que esta en la timeline con OTRA ruta (el aplicador lo encontro
      por nombre: media reubicada, el disco con otro nombre de volumen) cuenta
      como presente, con un aviso. Antes daba rojo con la timeline completa;
    - una timeline esperada que no llego es amarilla si el reporte dice
      "timeline sin material (...): <nombre>", y roja si no.

Veredicto: verde, amarillo, rojo, o pendiente si todavia no llego nada.

Si no llego nada pero en la carpeta de errores hay un error_<proyecto>.drt
posterior al pedido, el veredicto es rojo con el error que dejo Resolve
(E01 buzon ilegible, E02 sin pedido para el proyecto abierto...). Medido en
Free 21.1.1.10 el 2026-10-05: una timeline vacia tambien se exporta. El archivo
es por proyecto y solo cuenta si es POSTERIOR al pedido, sin tolerancia: un E02
de un clic anterior, o de otro proyecto, no es el resultado de este pedido
(revision del PR #1).
"""

from __future__ import annotations

import json
import posixpath
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from lib.drt import Drt, ErrorDrt, leer_drt
from lib.pedido import (NOMBRE_TIMELINE, SEP, archivo_de_proyecto, nombre_seguro,
                        sello_corto, sufijo_dia)

CD_SELLO = "diez50:sello="
CD_REPORTE = "diez50:reporte="
SIN_MARKER = " | sin marker: "     # el mismo A.SIN_MARKER de aplicar.lua
# Tolerancia entre el reloj de quien escribio el pedido y el de quien exporto
# (la VM de prueba y su anfitrion no estan sincronizados al segundo).
TOLERANCIA_S = 120

# Lo que construir.lua anota cuando un clip o un WAV no llega a su timeline por
# una razon conocida ("<tipo>: <n> (<nombre>; <nombre>; ...)"). El verificador
# cuenta con eso para no dar rojo por algo que el aplicador ya explico. Los
# tipos son los C.NOTA_* de construir.lua.
EXPLICA_CLIPS = ("faltan en el pool", "clips sin hora utilizable (fuera del B-ROLL)")
EXPLICA_WAV = ("WAV que no se pudieron importar",)
RE_SIN_MATERIAL = re.compile(r"^timeline sin material \([^)]*\): (.+)$")

RE_REPORTE = re.compile(
    r"^Diez50(?: · | - )(?P<sc>\w{4})(?: · | - )(?P<estado>OK|AVISOS|ERROR)"
    r"(?: · | - )(?P<n>\d+) de (?P<m>\d+)(?:(?: · | - )anterior .*)?$")


@dataclass
class Hallazgo:
    nivel: str          # 'ok' | 'aviso' | 'error'
    donde: str          # 'prueba', 'reporte', 'pedido'
    que: str

    def linea(self) -> str:
        marca = {"ok": "  ok ", "aviso": "  !! ", "error": "  XX "}[self.nivel]
        return f"{marca}[{self.donde}] {self.que}"


@dataclass
class Verificacion:
    recibos: str
    sello: str = ""
    proyecto: str = ""
    veredicto: str = "pendiente"    # verde | amarillo | rojo | pendiente
    hallazgos: list[Hallazgo] = field(default_factory=list)
    reporte: dict = field(default_factory=dict)

    def agregar(self, nivel: str, donde: str, que: str) -> None:
        self.hallazgos.append(Hallazgo(nivel, donde, que))

    @property
    def errores(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "error"]

    @property
    def avisos(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "aviso"]

    def a_dict(self) -> dict:
        d = asdict(self)
        d["hallazgos"] = [asdict(h) for h in self.hallazgos]
        return d


def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _marker_en_cero(d: Drt, cd: str) -> bool:
    return any(abs(m.frame) < 0.5 and m.custom_data == cd for m in d.markers)


def orden_de_audio(d: Drt) -> str | None:
    """El motivo si un audio externo queda por encima de uno de camara, o None.
    Por nombre de pista, como LIB.verificarOrdenPistas: lo que cuenta es lo
    que el editor ve."""
    ultima_cam, primer_ext = 0, None
    for p in d.pistas:
        if p.tipo != "A" or not p.items:
            continue
        if p.nombre.startswith("CAM "):
            ultima_cam = p.indice
        elif p.nombre.startswith("LAVA ") and primer_ext is None:
            primer_ext = p.indice
    if primer_ext is not None and primer_ext < ultima_cam:
        return (f"audio externo en A{primer_ext} por ENCIMA de audio de camara "
                f"en A{ultima_cam}")
    return None


def _verificar_construida(v: Verificacion, recibos: Path, accion: dict, sello: str,
                          desde: float | None) -> None:
    clave = accion["clave"]
    ruta = recibos / f"{clave}.drt"
    if not ruta.exists():
        v.agregar("error", clave, f"no llego {ruta.name}: Resolve no exporto esa timeline")
        return
    if desde is not None and ruta.stat().st_mtime < desde - TOLERANCIA_S:
        v.agregar("error", clave, f"{ruta.name} es anterior al pedido: recibo viejo")
    try:
        d = leer_drt(ruta)
    except ErrorDrt as e:
        v.agregar("error", clave, f"{ruta.name} ilegible: {e}")
        return
    v.agregar("ok", clave, f"{ruta.name} leido (Resolve {d.db_app_ver or '?'})")

    # El nombre pedido, su version ASCII (si Resolve rechazo el punto medio) o,
    # con la politica 'conservar', el pedido mas el sello corto. Nada mas: un
    # 'anterior <sello>' es la timeline vieja, no la de este pedido.
    esperado = nombre_seguro(accion["nombre"])
    con_sello = esperado + SEP + sello_corto(sello)
    validos = {esperado, con_sello, esperado.replace(SEP, " - "),
               con_sello.replace(SEP, " - ")}
    if d.nombre in validos:
        v.agregar("ok", clave, f"timeline '{d.nombre}'")
    else:
        v.agregar("error", clave, f"la timeline se llama '{d.nombre}' y no '{esperado}'")

    if _marker_en_cero(d, CD_SELLO + sello):
        v.agregar("ok", clave, f"sello {sello} en el frame 0")
    else:
        otros = [m.custom_data for m in d.markers if m.custom_data.startswith(CD_SELLO)]
        v.agregar("error", clave, "sin el sello de este pedido en el frame 0"
                  + (f" (trae {', '.join(otros)})" if otros else ""))

    for tipo, letra in (("video", "V"), ("audio", "A")):
        reales = [p for p in d.pistas if p.tipo == letra]
        pedidas = (accion.get("pistas") or {}).get(tipo) or []
        for i, p in enumerate(pedidas, start=1):
            etq = f"{letra}{i}"
            if i > len(reales):
                v.agregar("error", clave, f"{etq} ({p['nombre']}) no existe en el .drt")
                continue
            real = reales[i - 1]
            if real.nombre != p["nombre"]:
                v.agregar("error", clave, f"{etq} se llama '{real.nombre}' y no "
                          f"'{p['nombre']}': el orden de pistas no es el pedido")
            elif not real.items:
                v.agregar("error", clave, f"{etq} ({p['nombre']}) llego vacia")
            else:
                v.agregar("ok", clave, f"{etq} {p['nombre']}: {len(real.items)} clip(s)")
        sobran = [p for p in reales[len(pedidas):] if p.items]
        if sobran:
            v.agregar("aviso", clave, f"{len(sobran)} pista(s) de {tipo} con clips que el "
                      "pedido no pidio: " + ", ".join(p.etiqueta for p in sobran))

    motivo = orden_de_audio(d)
    if motivo:
        v.agregar("error", clave, f"regla de orden rota: {motivo}")
    else:
        v.agregar("ok", clave, "orden: video de camara > audio de camara > audio externo")
    for a in d.avisos:
        v.agregar("aviso", clave, f"lector: {a}")


def _nfc(ruta: str) -> str:
    """macOS puede devolver una ruta con acentos descompuesta (NFD) y el
    manifest guardarla compuesta: se comparan normalizadas."""
    return unicodedata.normalize("NFC", ruta or "")


def _nombres_validos(nombre: str, sello: str) -> set[str]:
    esperado = nombre_seguro(nombre)
    con_sello = esperado + SEP + sello_corto(sello)
    return {esperado, con_sello, esperado.replace(SEP, " - "), con_sello.replace(SEP, " - ")}


def _verificar_construir(v: Verificacion, recibos: Path, accion: dict, pedido: dict,
                         desde: float | None) -> tuple[dict[str, Drt], list[tuple[str, str]]]:
    """Cada .drt que dejo `construir`. Devuelve los que se leyeron, por clave, y
    las timelines esperadas que no llegaron (clave, nombre): esas se juzgan
    despues, con el reporte leido (_verificar_faltantes)."""
    sello = pedido.get("sello", "")
    pfx = pedido.get("pfx", "")
    try:
        sufijo = sufijo_dia(accion.get("dia"))
    except ValueError:
        sufijo = ""
    esperadas = set((pedido.get("esperado") or {}).get("timelines") or [])
    nombre_de = {clave: pfx + n + sufijo for clave, n in NOMBRE_TIMELINE.items()}
    claves = sorted(p.stem for p in recibos.glob("*.drt") if p.stem != "reporte")
    faltantes = [(clave, nombre) for clave, nombre in nombre_de.items()
                 if nombre in esperadas and clave not in claves]
    leidos: dict[str, Drt] = {}
    for clave in claves:
        ruta = recibos / f"{clave}.drt"
        if desde is not None and ruta.stat().st_mtime < desde - TOLERANCIA_S:
            v.agregar("error", clave, f"{ruta.name} es anterior al pedido: recibo viejo")
        try:
            d = leer_drt(ruta)
        except ErrorDrt as e:
            v.agregar("error", clave, f"{ruta.name} ilegible: {e}")
            continue
        leidos[clave] = d
        if clave in nombre_de:
            ok_nombre = d.nombre in _nombres_validos(nombre_de[clave], sello)
        else:   # sub_<n>: una por subcarpeta; el nombre lo pone el material
            ok_nombre = d.nombre.startswith(pfx) and (not sufijo or sufijo in d.nombre)
        if ok_nombre:
            v.agregar("ok", clave, f"timeline '{d.nombre}'")
        else:
            v.agregar("error", clave, f"la timeline se llama '{d.nombre}', que no es de este "
                      "pedido")
        if _marker_en_cero(d, CD_SELLO + sello):
            v.agregar("ok", clave, f"sello {sello} en el frame 0")
        else:
            v.agregar("error", clave, "sin el sello de este pedido en el frame 0")
        motivo = orden_de_audio(d)
        if motivo:
            v.agregar("error", clave, f"regla de orden rota: {motivo}")
        vacias = [p.etiqueta for p in d.pistas if p.tipo in ("V", "A") and p.nombre.startswith(
            ("CAM ", "LAVA ")) and not p.items]
        if vacias:
            v.agregar("aviso", clave, "pistas con nombre y sin clips: " + ", ".join(vacias))
        n_v = sum(len(p.items) for p in d.pistas if p.tipo == "V")
        n_a = sum(len(p.items) for p in d.pistas if p.tipo == "A")
        v.agregar("ok", clave, f"{n_v} clip(s) de video y {n_a} de audio")
        for a in d.avisos:
            v.agregar("aviso", clave, f"lector: {a}")
    return leidos, faltantes


def _textos_reporte(v: Verificacion) -> list[str]:
    """Las anotaciones del reporte: las de sus markers y las que viajan en la
    nota del frame 0 porque no cupieron como marker."""
    textos = [a.get("texto", "") for a in (v.reporte or {}).get("anotaciones", [])]
    for parte in str((v.reporte or {}).get("sin_marker", "")).split(" / "):
        if parte.strip():
            textos.append(re.sub(r"^(?:AVISO|ERROR|INFO) ", "", parte.strip()))
    return textos


def _explicacion(v: Verificacion, tipos: tuple[str, ...]) -> tuple[Counter, int]:
    """Lo que el reporte explica: los nombres que da (con repeticion) y cuantos
    mas cuenta sin nombrarlos (los que pasan del tope de ejemplos)."""
    nombres: Counter = Counter()
    resto = 0
    for t in _textos_reporte(v):
        for tipo in tipos:
            if not t.startswith(tipo + ": "):
                continue
            m = re.match(r"^(\d+)(?: \((.*)\))?$", t[len(tipo) + 2:])
            if not m:
                continue
            lista = [x for x in (m[2] or "").split("; ") if x and x != "..."]
            nombres.update(lista)
            resto += max(0, int(m[1]) - len(lista))
    return nombres, resto


def _sin_explicar(faltan: list[dict], nombres: Counter, resto: int) -> list[dict]:
    """Los que faltan y el reporte no explica: primero se cruzan por nombre;
    lo que no tiene nombre en el reporte solo cubre por cuenta lo que sobra."""
    nombres = Counter(nombres)
    sin = []
    for c in faltan:
        if nombres[c["nombre"]] > 0:
            nombres[c["nombre"]] -= 1
        elif resto > 0:
            resto -= 1
        else:
            sin.append(c)
    return sin


def _verificar_faltantes(v: Verificacion, faltantes: list[tuple[str, str]]) -> None:
    """Una timeline esperada que no llego: amarilla si el reporte dice que no
    tenia material, roja si no lo dice."""
    declaradas = set()
    for t in _textos_reporte(v):
        m = RE_SIN_MATERIAL.match(t)
        if m:
            declaradas.add(m[1].strip())
    for clave, nombre in faltantes:
        if nombre in declaradas:
            v.agregar("aviso", clave, f"no llego {clave}.drt: el reporte dice que '{nombre}' "
                      "no tenia material")
        else:
            v.agregar("error", clave, f"no llego {clave}.drt: '{nombre}' no se construyo o "
                      "no se exporto")


def _verificar_cobertura(v: Verificacion, pedido: dict, leidos: dict[str, Drt]) -> None:
    """Que el material del dia este en las timelines, mirado en el .drt."""
    esperado = pedido.get("esperado") or {}
    if not leidos:
        return
    if "aroll" in leidos or "broll" in leidos:
        presentes = {_nfc(it.ruta) for clave in ("aroll", "broll") if clave in leidos
                     for p in leidos[clave].pistas if p.tipo == "V" for it in p.items}
        clips = esperado.get("clips") or []
        # Lo que esta en la timeline con una ruta que el pedido no conoce, por
        # nombre de archivo: asi lo encontro el aplicador si la media se movio.
        otras = Counter(posixpath.basename(r) for r in
                        presentes - {_nfc(c["ruta"]) for c in clips})
        faltan, por_nombre = [], []
        for c in clips:
            if _nfc(c["ruta"]) in presentes:
                continue
            if otras[_nfc(c["nombre"])] > 0:
                otras[_nfc(c["nombre"])] -= 1
                por_nombre.append(c)
            else:
                faltan.append(c)
        if por_nombre:
            v.agregar("aviso", "cobertura", f"{len(por_nombre)} clip(s) estan en la timeline "
                      "con otra ruta (el aplicador los encontro por nombre; la media se "
                      "movio?): " + ", ".join(c["nombre"] for c in por_nombre[:8]))
        if not faltan:
            v.agregar("ok", "cobertura", f"los {len(clips)} clip(s) del horneado estan en "
                      "A-ROLL o B-ROLL")
        else:
            sin = _sin_explicar(faltan, *_explicacion(v, EXPLICA_CLIPS))
            if not sin:
                nombres = ", ".join(c["nombre"] for c in faltan[:8]) + (
                    ", ..." if len(faltan) > 8 else "")
                v.agregar("aviso", "cobertura", f"{len(faltan)} de {len(clips)} clip(s) no "
                          f"estan en A-ROLL ni B-ROLL y el reporte dice por que: {nombres}")
            else:
                nombres = ", ".join(c["nombre"] for c in sin[:8]) + (
                    ", ..." if len(sin) > 8 else "")
                v.agregar("error", "cobertura", f"{len(sin)} de {len(clips)} clip(s) no "
                          f"estan en A-ROLL ni B-ROLL y el reporte no dice por que: {nombres}")
    if "audios_externos" in leidos:
        presentes = {_nfc(it.ruta) for p in leidos["audios_externos"].pistas if p.tipo == "A"
                     for it in p.items}
        audios = esperado.get("audios") or []
        faltan = [a for a in audios if _nfc(a["ruta"]) not in presentes]
        if not faltan:
            v.agregar("ok", "cobertura", f"los {len(audios)} WAV del dia estan en "
                      "AUDIOS EXTERNOS")
        else:
            nivel = "error" if _sin_explicar(faltan, *_explicacion(v, EXPLICA_WAV)) else "aviso"
            v.agregar(nivel, "cobertura", f"{len(faltan)} de {len(audios)} WAV no estan en "
                      "AUDIOS EXTERNOS: " + ", ".join(a["nombre"] for a in faltan[:8]))


def _verificar_reporte(v: Verificacion, recibos: Path, sello: str, total: int,
                       desde: float | None) -> None:
    ruta = recibos / "reporte.drt"
    if not ruta.exists():
        v.agregar("error", "reporte", "no llego reporte.drt")
        return
    if desde is not None and ruta.stat().st_mtime < desde - TOLERANCIA_S:
        v.agregar("error", "reporte", "reporte.drt es anterior al pedido: recibo viejo")
    try:
        d = leer_drt(ruta)
    except ErrorDrt as e:
        v.agregar("error", "reporte", f"reporte.drt ilegible: {e}")
        return
    if _marker_en_cero(d, CD_REPORTE + sello):
        v.agregar("ok", "reporte", f"marker del reporte con el sello {sello}")
    else:
        v.agregar("error", "reporte", "reporte.drt no trae el sello de este pedido")
    m = RE_REPORTE.match(d.nombre)
    if not m:
        v.agregar("error", "reporte", f"nombre de reporte que no se entiende: '{d.nombre}'")
        return
    n, tot, estado = int(m["n"]), int(m["m"]), m["estado"]
    v.reporte = {"nombre": d.nombre, "estado": estado, "n": n, "m": tot,
                 "anotaciones": [{"nivel": mk.custom_data.split(":", 1)[1].split("=", 1)[0],
                                  "texto": mk.nota}
                                 for mk in d.markers if mk.custom_data.startswith("diez50:")
                                 and not mk.custom_data.startswith(CD_REPORTE)]}
    if m["sc"] != sello_corto(sello):
        v.agregar("error", "reporte", f"el sello corto del nombre ({m['sc']}) no es el "
                  f"del pedido ({sello_corto(sello)})")
    if tot != total:
        v.agregar("error", "reporte", f"el reporte cuenta {tot} acciones y el pedido trae {total}")
    if estado == "ERROR" or n < tot:
        v.agregar("error", "reporte", f"Resolve dice {estado}, {n} de {tot}")
    elif estado == "AVISOS":
        v.agregar("aviso", "reporte", f"Resolve dice AVISOS, {n} de {tot}")
    else:
        v.agregar("ok", "reporte", f"Resolve dice OK, {n} de {tot}")
    for a in v.reporte["anotaciones"]:
        nivel = "error" if a["nivel"] == "error" else ("aviso" if a["nivel"] == "aviso" else "ok")
        v.agregar(nivel, "reporte", f"{a['nivel']} en Resolve: {a['texto']}")
    # Lo que no entro como marker viaja en la nota del marker del frame 0.
    for mk in d.markers:
        if mk.custom_data == CD_REPORTE + sello and SIN_MARKER in mk.nota:
            perdidas = mk.nota.split(SIN_MARKER, 1)[1]
            v.reporte["sin_marker"] = perdidas
            v.agregar("aviso", "reporte", f"anotaciones que no entraron como marker: {perdidas}")


def leer_copia(recibos: Path | str) -> dict | None:
    """El pedido.json de esa carpeta de recibos, o None."""
    copia = Path(recibos) / "pedido.json"
    if not copia.exists():
        return None
    return json.loads(copia.read_text(encoding="utf-8"))


def error_reciente(errores: Path | str | None, copia: dict | None) -> str | None:
    """El nombre de la timeline de error que el aplicador exporto para el
    proyecto de ese pedido DESPUES de escribirlo, o None.

    Sin tolerancia hacia atras, a diferencia de los recibos: un error de antes
    del pedido es de un clic anterior, y el flujo normal (pulsar Aplicar, ver
    E02, escribir el pedido) lo dejaria a segundos de distancia."""
    if errores is None or not copia:
        return None
    desde = _epoch((copia.get("pedido") or {}).get("escrito", ""))
    proyecto = copia.get("proyecto")
    if desde is None or not proyecto:
        return None
    ruta = Path(errores) / f"error_{archivo_de_proyecto(proyecto)}.drt"
    if not ruta.exists() or ruta.stat().st_mtime <= desde:
        return None
    try:
        return leer_drt(ruta).nombre or ruta.name
    except ErrorDrt:
        return ruta.name


def verificar(recibos: Path | str, errores: Path | str | None = None) -> Verificacion:
    """Compara lo que hay en la carpeta de recibos contra su pedido.json."""
    recibos = Path(recibos)
    v = Verificacion(recibos=str(recibos))
    copia = recibos / "pedido.json"
    if not copia.exists():
        v.agregar("error", "pedido", f"no hay {copia}: esta carpeta no es de un pedido")
        v.veredicto = "rojo"
        return v
    datos = json.loads(copia.read_text(encoding="utf-8"))
    pedido = datos.get("pedido") or {}
    v.sello, v.proyecto = pedido.get("sello", ""), datos.get("proyecto", "")
    desde = _epoch(pedido.get("escrito", ""))
    if not any(recibos.glob("*.drt")):
        err = error_reciente(errores, datos)
        if err:
            v.agregar("error", "pedido", f"Resolve dejo un error en vez del pedido: '{err}'")
            v.veredicto = "rojo"
            return v
        v.agregar("aviso", "pedido", "todavia no llego ningun .drt: aplica el pedido en "
                  "Resolve (Workspace > Scripts > Diez50 Aplicar)")
        v.veredicto = "pendiente"
        return v
    acciones = pedido.get("acciones") or []
    exporta = any(a.get("tipo") == "exportar" for a in acciones)
    leidos, faltantes = None, []
    for a in acciones:
        if a.get("tipo") == "prueba_regreso" and exporta:
            _verificar_construida(v, recibos, a, v.sello, desde)
        if a.get("tipo") == "construir" and exporta:
            leidos, faltantes = _verificar_construir(v, recibos, a, pedido, desde)
    _verificar_reporte(v, recibos, v.sello, len(acciones), desde)
    # Lo que falta va despues del reporte: cuenta con lo que el aplicador explico.
    _verificar_faltantes(v, faltantes)
    if leidos is not None:
        _verificar_cobertura(v, pedido, leidos)
    v.veredicto = "rojo" if v.errores else ("amarillo" if v.avisos else "verde")
    return v


def escribir(v: Verificacion) -> Path:
    """Deja el veredicto junto a los recibos, para el CLI y para el acta."""
    p = Path(v.recibos) / "verificacion.json"
    p.write_text(json.dumps(v.a_dict(), indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return p
