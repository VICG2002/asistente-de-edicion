#!/usr/bin/env python3
"""Lee lo que dejo `resolve/diagnostico.lua` y arma la matriz de capacidades.

PARA QUE SIRVE
El diagnostico de la Fase 0 corre DENTRO de Resolve (menu Utility, submenu,
Edit y Consola) y reporta cada prueba por todos los canales que puede, porque
no sabemos cual funciona en Free: markers, bins, Fusion prefs, print, io y un
.drt. Este script junta esos canales FUERA de Resolve, los reconcilia por
(clave, id) y dice el veredicto de cada prueba.

LA REGLA QUE MANDA (seccion 9 del contrato)
Un OK de Resolve es lo que el script CREYO. Solo cuenta como OK en la matriz
si el efecto se comprueba fuera cuando se puede comprobar:
  EXP    el archivo existe, pesa mas de 0 bytes y se deja leer
  SAVE   Project.db tiene la timeline de resumen de esa corrida
  IO     el archivo de la prueba de escritura (<clave>_io.txt) existe
  PREFS  el valor aparece en Fusion.prefs
Si Resolve dijo OK y el archivo no esta, el veredicto es NO: es exactamente el
caso que el diagnostico existe para atrapar (un `Export` que devuelve true y no
escribe nada). Si no hay con que comprobar, sale SIN CONFIRMAR, no OK.

CANALES
  resumen     timeline `DIAG RESUMEN <clave> <ok> de <total>` en Project.db
  marcas-db   markers de `DIAG MARCAS <clave>` leidos de Project.db
  bin         subcarpetas de `DIEZ50 DIAG <clave>` en Project.db
  prefs       Global.Diez50.Diag.<ctx> en Fusion.prefs (tabla Lua, JSON dentro)
  io          KIT/recibos/<clave>.json
  drt-manual  el .drt que el editor exporto a mano (KIT/manual/*.drt)
  drt-export  el .drt que escribio `Timeline:Export` (recibos/<clave>_drt.drt)
  print       la salida de la Consola pegada a mano (--print), si se quiere

LO QUE NUNCA HACE
  - Abrir un Project.db original: `lib.timeline_resolve` lo copia a un
    temporal y abre la copia en `mode=ro`.
  - Escribir fuera de KIT/matriz/. Lo demas (recibos, prefs, .drt, la carpeta
    de --desde, /Volumes/DIEZ50DIAG) solo se lee.

Uso:
    python3 bin/leer_diagnostico.py
    python3 bin/leer_diagnostico.py --desde ~/Desktop/Diez50-diag-resultado-mac
    python3 bin/leer_diagnostico.py --ojo menu=si,submenu=no,print_consola=si,error_visible=no
    python3 bin/leer_diagnostico.py --projectdb X/Project.db --prefs X/Fusion.prefs --drt X.drt
"""

from __future__ import annotations

import argparse
import csv
import io as _io
import json
import os
import platform
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import drt as DRT  # noqa: E402
from lib import timeline_resolve as TR  # noqa: E402

FORMATO = 1
PROYECTO_DEFAULT = "DIEZ50_DIAG"
CTX_ORDEN = ("menu_utility", "menu_sub", "menu_edit", "consola")
ESTADOS = ("OK", "NO", "INFO", "ERR")
CASILLAS = ("menu", "submenu", "print_consola", "error_visible")
PREFS_RAIZ = (Path.home() / "Library/Application Support/Blackmagic Design"
              / "DaVinci Resolve/Fusion/Profiles")

RE_ID = re.compile(r"^[A-Z]+-\d+$")
# clave = <sello>-<ctx>-r<r>. El sello puede ser cualquier cosa: si config.lua no
# se leyo dentro de Resolve, diagnostico.lua pone "sinsello". El ctx sale de
# gsub("[^%w_]", "_"), asi que nunca lleva guion: es el ultimo tramo sin guion
# antes de -r<r>, y el (.+) codicioso deja todo lo demas al sello.
RE_CLAVE = re.compile(r"^(.+)-([A-Za-z0-9_]+)-r(\d+)$")
# El resumen nace "DIAG RESUMEN <clave> - de <total>" y se renombra al final con
# los numeros: si el renombre fallo, el "-" se queda y la corrida sigue contando.
# Hasta el 2026-10-01 llevaba "-/<total>" y "<ok>/<total>", pero Resolve rechaza
# la '/' en un nombre de timeline (Studio 21.1.0.17) y ese resumen nunca nacia.
# La forma vieja se sigue leyendo por si una build distinta si la acepto.
RE_RESUMEN = re.compile(r"^DIAG RESUMEN (\S+)(?: (\d+|-)(?: de |/)(\d+))?\s*$")
RE_MARCAS = re.compile(r"^DIAG MARCAS (\S+)\s*$")
RE_ERROR = re.compile(r"^DIAG ERROR (\S+)\s*(.*)$")
RE_BIN = re.compile(r"^DIEZ50 DIAG (\S+)\s*$")
RE_SUB = re.compile(r"^([A-Z]+-\d+)\s+(OK|NO|INFO|ERR)\b\s*(.*)$")
RE_PRINT = re.compile(r"DIEZ50DIAG\s+(\S+)\s+(\S+)\s*(.*)$")

# Prioridad cuando dos canales dicen cosas distintas: primero los que traen el
# documento completo tal cual lo armo el diagnostico, al final los que recortan.
PRIORIDAD = ("io", "prefs", "marcas-db", "drt-export", "drt-manual", "bin", "print")

# Archivo de cada exporte, segun lo que aparece en el `valor` de la prueba EXP.
# diagnostico.lua escribe "<CONSTANTE> <destino> devolvio true" (EXPORT_DRT,
# EXPORT_TEXT_TAB...) y el archivo PLANO como <recibos>/<clave>_<tipo>.<ext>
# (contrato §8). "METADATA" va primero y sin frontera izquierda: su valor es
# "ExportMetadata recibos true", pegado.
EXPORTES = (("METADATA", "metadata.csv"), ("DRT", "drt.drt"), ("OTIO", "otio.otio"),
            ("FCPXML", "fcpxml.fcpxml"), ("EDL", "edl.edl"), ("AAF", "aaf.aaf"),
            ("CSV", "csv.csv"), ("TAB", "tab.txt"))

# Copia del catalogo EXP de diagnostico.lua, en su orden: TIPOS_EXP registra
# primero los 7 exportes "a recibos" (EXP-01..07), despues los 7 "al volumen"
# (EXP-08..14) y al final ExportMetadata (EXP-15). Es solo el RESPALDO: el
# valor de cada EXP nombra el exporte y el destino, y eso manda. El catalogo
# sirve cuando el valor no dice nada (un ERR "attempt to index a nil value"),
# y solo si no contradice al valor: si el Lua cambia el orden de TIPOS_EXP,
# esta copia se equivoca en silencio, asi que nunca pisa lo que el valor dice.
CATALOGO_EXP = ("drt.drt", "otio.otio", "fcpxml.fcpxml", "edl.edl", "aaf.aaf",
                "csv.csv", "tab.txt")


# ==========================================================================
# Lua: Fusion.prefs y config.lua son tablas Lua, no JSON
# ==========================================================================

class ErrorLua(ValueError):
    """Texto que no es una tabla Lua legible. Se dice donde."""


class _Lua:
    """Parser minimo de tablas Lua: lo que escribe Fusion y lo que escribe preparar.

    Por que no una expresion regular: el JSON del diagnostico viaja DENTRO de un
    string Lua, con sus comillas escapadas (`\\"`) y, si Fusion lo decide, con
    bytes en decimal (`\\195\\177`). Una regex que corte en la primera comilla
    parte el JSON a la mitad. Tambien acepta la forma de Fusion `Nombre { ... }`
    y `ordered() { ... }`, que es una llamada con una tabla: se lee la tabla.
    """

    def __init__(self, texto: str):
        self.t = texto
        self.p = 0

    def error(self, msg: str):
        linea = self.t.count("\n", 0, self.p) + 1
        raise ErrorLua(f"{msg} (linea {linea})")

    def blancos(self):
        t = self.t
        while self.p < len(t):
            c = t[self.p]
            if c in " \t\r\n":
                self.p += 1
            elif t.startswith("--", self.p):
                self.p += 2
                m = re.match(r"\[(=*)\[", t[self.p:])
                if m:
                    fin = t.find("]" + m.group(1) + "]", self.p)
                    self.p = len(t) if fin < 0 else fin + len(m.group(1)) + 2
                else:
                    fin = t.find("\n", self.p)
                    self.p = len(t) if fin < 0 else fin + 1
            else:
                break

    def valor(self):
        self.blancos()
        t = self.t
        if self.p >= len(t):
            self.error("fin inesperado")
        c = t[self.p]
        if c == "{":
            return self.tabla()
        if c in "\"'":
            return self.cadena()
        if c == "[" and re.match(r"\[=*\[", t[self.p:]):
            return self.larga()
        m = re.match(r"-?(0[xX][0-9a-fA-F]+|(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?)",
                     t[self.p:])
        if m:
            self.p += m.end()
            s = m.group(0)
            try:
                return int(s, 0) if re.fullmatch(r"-?(0[xX][0-9a-fA-F]+|\d+)", s) \
                    else float(s)
            except ValueError:
                return float.fromhex(s)
        m = re.match(r"[A-Za-z_]\w*", t[self.p:])
        if m:
            self.p += m.end()
            nombre = m.group(0)
            if nombre in ("true", "false"):
                return nombre == "true"
            if nombre == "nil":
                return None
            self.blancos()
            if self.t.startswith("()", self.p):      # ordered() { ... }
                self.p += 2
                self.blancos()
            if self.p < len(t) and t[self.p] == "{":    # Nombre { ... }
                return self.tabla()
            return nombre
        self.error(f"valor inesperado {t[self.p:self.p + 20]!r}")

    def tabla(self):
        self.p += 1                                    # {
        out: dict = {}
        n = 1
        while True:
            self.blancos()
            if self.p >= len(self.t):
                self.error("tabla sin cerrar")
            if self.t[self.p] == "}":
                self.p += 1
                return out
            if self.t[self.p] == "[" and not re.match(r"\[=*\[", self.t[self.p:]):
                self.p += 1
                k = self.valor()
                self.blancos()
                if not self.t.startswith("]", self.p):
                    self.error("falta ]")
                self.p += 1
                self.blancos()
                if not self.t.startswith("=", self.p):
                    self.error("falta =")
                self.p += 1
                out[k] = self.valor()
            else:
                m = re.match(r"([A-Za-z_]\w*)\s*=(?!=)", self.t[self.p:])
                if m:
                    self.p += m.end()
                    out[m.group(1)] = self.valor()
                else:
                    out[n] = self.valor()
                    n += 1
            self.blancos()
            if self.p < len(self.t) and self.t[self.p] in ",;":
                self.p += 1

    def larga(self):
        m = re.match(r"\[(=*)\[", self.t[self.p:])
        cierre = "]" + m.group(1) + "]"
        ini = self.p + m.end()
        fin = self.t.find(cierre, ini)
        if fin < 0:
            self.error("string largo sin cerrar")
        self.p = fin + len(cierre)
        s = self.t[ini:fin]
        return s[1:] if s.startswith("\n") else s

    def cadena(self):
        q = self.t[self.p]
        self.p += 1
        buf = bytearray()
        t = self.t
        while True:
            if self.p >= len(t):
                self.error("string sin cerrar")
            c = t[self.p]
            if c == q:
                self.p += 1
                return buf.decode("utf-8", errors="replace")
            if c == "\n":
                self.error("salto de linea dentro de un string")
            if c != "\\":
                buf += c.encode("utf-8")
                self.p += 1
                continue
            self.p += 1
            e = t[self.p] if self.p < len(t) else ""
            simples = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
                       "\\": 92, '"': 34, "'": 39, "\n": 10}
            if e in simples:
                buf.append(simples[e])
                self.p += 1
            elif e == "x":
                buf.append(int(t[self.p + 1:self.p + 3], 16))
                self.p += 3
            elif e == "z":
                self.p += 1
                while self.p < len(t) and t[self.p] in " \t\r\n":
                    self.p += 1
            elif e == "u":
                m = re.match(r"u\{([0-9a-fA-F]+)\}", t[self.p:])
                if not m:
                    self.error("escape \\u mal formado")
                buf += chr(int(m.group(1), 16)).encode("utf-8")
                self.p += m.end()
            elif e.isdigit():
                m = re.match(r"\d{1,3}", t[self.p:])
                buf.append(int(m.group(0)) & 0xFF)
                self.p += m.end()
            else:
                self.error(f"escape desconocido \\{e}")


def leer_lua(texto: str):
    """Tabla Lua (con o sin `return` delante) -> dict de Python."""
    p = _Lua(texto)
    p.blancos()
    if texto.startswith("return", p.p):
        p.p += len("return")
    v = p.valor()
    p.blancos()
    return v


def strings_lua(texto: str) -> list[str]:
    """Todos los strings entre comillas de un texto Lua, ya desescapados.

    Respaldo para cuando la tabla entera no se deja parsear (un Fusion.prefs
    con una construccion que este parser no conoce): el JSON del diagnostico
    se reconoce por su contenido, aunque se pierda la ruta de claves.
    """
    out = []
    for m in re.finditer(r'"(?:[^"\\\n]|\\.)*"', texto):
        try:
            out.append(_Lua(m.group(0)).cadena())
        except ErrorLua:
            pass
    return out


# ==========================================================================
# fuentes: de donde sale cada canal
# ==========================================================================

@dataclass
class Fuentes:
    kit: Path
    desde: Path | None = None
    project_db: Path | None = None
    prefs: Path | None = None
    drts: list[Path] = field(default_factory=list)
    recibos: Path | None = None
    recibos_volumen: Path | None = None
    maquina: Path | None = None
    casillas: Path | None = None
    print_txt: Path | None = None
    config: dict = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)


def es_basura_mac(p: Path) -> bool:
    """Archivos que macOS agrega al copiar o comprimir y que no son del kit.

    `._<nombre>` (AppleDouble: atributos extendidos en un volumen o zip que no
    los soporta) y todo lo que cuelga de `__MACOSX/` (lo que deja el Archive
    Utility o `unzip` al abrir un zip con recursos). Un `._<clave>.json` no es
    un recibo: leerlo daria un JSON ilegible y un aviso falso.
    """
    return p.name.startswith("._") or "__MACOSX" in p.parts


def _mas_reciente(rutas) -> Path | None:
    rutas = [r for r in rutas if r.exists() and not es_basura_mac(r)]
    return max(rutas, key=lambda r: r.stat().st_mtime) if rutas else None


def buscar_project_db(proyecto: str, prefs: Path = TR.PREFS) -> Path | None:
    """El Project.db del proyecto en las bases de DISCO de esta Mac.

    Se busca en todas, no solo en la activa: el LEEME pide crear una base nueva
    Diez50-Diag, y puede no ser la que quedo abierta. Si hay varios proyectos
    que empiezan con el nombre, gana el guardado mas reciente.
    """
    candidatos = []
    for raiz in TR.bases_disco(prefs).values():
        d = raiz / "Resolve Projects/Users/guest/Projects"
        if d.is_dir():
            candidatos += [x / "Project.db" for x in d.iterdir()
                           if x.name.startswith(proyecto) and (x / "Project.db").exists()]
    return _mas_reciente(candidatos)


def resolver_fuentes(a: argparse.Namespace) -> Fuentes:
    kit = Path(os.path.expanduser(a.kit)).resolve()
    desde = Path(os.path.expanduser(a.desde)).resolve() if a.desde else None
    f = Fuentes(kit=kit, desde=desde)
    base = desde or kit

    cfg_path = (desde / "config.lua") if desde and (desde / "config.lua").exists() \
        else kit / "config.lua"
    if cfg_path.exists():
        try:
            c = leer_lua(cfg_path.read_text(errors="replace"))
            f.config = c if isinstance(c, dict) else {}
        except ErrorLua as e:
            f.avisos.append(f"{cfg_path}: no se pudo leer ({e})")

    # Project.db
    if a.projectdb:
        f.project_db = Path(os.path.expanduser(a.projectdb))
    elif desde:
        f.project_db = _mas_reciente(
            [desde / "Project.db"]
            + list(desde.glob(f"project_db/*/{a.proyecto}*/Project.db")))
        otros = [x for x in desde.glob("project_db/*/*/Project.db") if not es_basura_mac(x)]
        if len(otros) > 1:
            f.avisos.append(f"{len(otros)} Project.db en {desde}/project_db: "
                            f"se leyo el mas reciente ({f.project_db})")
    else:
        f.project_db = buscar_project_db(a.proyecto)
    if f.project_db is None:
        f.avisos.append(f"sin Project.db de {a.proyecto}: no hay canales resumen, "
                        "marcas-db ni bin")

    # Fusion.prefs
    if a.prefs:
        f.prefs = Path(os.path.expanduser(a.prefs))
    elif desde:
        f.prefs = desde / "Fusion.prefs" if (desde / "Fusion.prefs").exists() else None
    else:
        f.prefs = _mas_reciente(list(PREFS_RAIZ.glob("*/Fusion.prefs")))
    if f.prefs is not None and not f.prefs.exists():
        # Una ruta mal tecleada no es un Fusion.prefs sin la clave: si se
        # dejara, PREFS-01 saldria NO como si SetPrefs no persistiera.
        f.avisos.append(f"no existe {f.prefs}: no hay canal prefs")
        f.prefs = None
    elif f.prefs is None:
        f.avisos.append("sin Fusion.prefs: no hay canal prefs")

    # recibos (io y exportes)
    f.recibos = base / "recibos" if (base / "recibos").is_dir() else None
    if desde:
        # En la carpeta recogida, los recibos del volumen solo estan si
        # recoger_kit.sh los copio a <desde>/recibos_volumen.
        rv = desde / "recibos_volumen"
        f.recibos_volumen = rv if rv.is_dir() else None
    else:
        # El volumen solo cuenta en ESTA Mac: con --desde, el /Volumes/DIEZ50DIAG
        # que hay aqui no es el de la Mac donde corrio el diagnostico.
        rv = f.config.get("recibos_volumen")
        if isinstance(rv, str) and Path(rv).is_dir():
            f.recibos_volumen = Path(rv)

    # .drt manual
    if a.drt:
        f.drts = [Path(os.path.expanduser(x)) for x in a.drt]
    else:
        f.drts = (sorted(x for x in (base / "manual").glob("*.drt") if not es_basura_mac(x))
                  if (base / "manual").is_dir() else [])

    for nombre in ("maquina.txt", "info.txt"):
        if desde and (desde / nombre).exists():
            f.maquina = desde / nombre
            break
    cas = base / "manual" / "casillas.txt"
    f.casillas = cas if cas.exists() else None
    f.print_txt = Path(os.path.expanduser(a.print)) if a.print else None
    return f


# ==========================================================================
# lectura de canales
# ==========================================================================

@dataclass
class Obs:
    clave: str
    id: str
    estado: str
    valor: str
    canal: str
    familia: str = ""


@dataclass
class Lectura:
    obs: list[Obs] = field(default_factory=list)
    corridas: dict[str, dict] = field(default_factory=dict)
    timelines: set[str] = field(default_factory=set)
    prefs_docs: dict[str, dict] = field(default_factory=dict)   # ctx -> doc
    errores: list[str] = field(default_factory=list)
    abortos: list[dict] = field(default_factory=list)
    versiones: dict[str, str] = field(default_factory=dict)
    canales: dict[str, str] = field(default_factory=dict)      # canal -> estado
    avisos: list[str] = field(default_factory=list)
    # Lo que confirmar() necesita saber para no confundir "no se pudo mirar"
    # con "se miro y no esta": solo lo segundo es un NO.
    db_leido: bool = False           # listar_timelines leyo el Project.db
    prefs_leido: bool = False        # la tabla de Fusion.prefs se parseo entera
    prefs_abortados: set[str] = field(default_factory=set)   # ctx pisados por un aborto
    prefs_ilegibles: set[str] = field(default_factory=set)   # ctx con JSON roto

    def corrida(self, clave: str) -> dict:
        if clave not in self.corridas:
            m = RE_CLAVE.match(clave)
            self.corridas[clave] = {
                "clave": clave, "sello": m.group(1) if m else "",
                "ctx": m.group(2) if m else "?", "r": int(m.group(3)) if m else 0,
                "resumen": None, "canales": set()}
        return self.corridas[clave]

    def anotar(self, clave: str, id_: str, estado: str, valor: str, canal: str,
               familia: str = ""):
        if not RE_ID.match(id_ or "") or estado not in ESTADOS:
            return
        self.obs.append(Obs(clave, id_, estado, valor.strip(), canal,
                            familia or id_.split("-")[0]))
        self.corrida(clave)["canales"].add(canal)


def _de_marker(nombre: str, nota: str, custom: str) -> tuple[str, str, str] | None:
    """(id, estado, valor) de un marker del diagnostico, o None si no es suyo.

    customData manda (`diez50diag:<id>=<estado>|<valor>`): lleva el valor
    entero y no depende del nombre. Si Resolve lo perdio, se cae al nombre =
    id y la nota = "<estado> <valor>".
    """
    if custom.startswith("diez50diag:"):
        cuerpo = custom[len("diez50diag:"):]
        id_, _, resto = cuerpo.partition("=")
        estado, _, valor = resto.partition("|")
        if RE_ID.match(id_) and estado in ESTADOS:
            return id_, estado, valor
    if RE_ID.match(nombre or ""):
        estado, _, valor = (nota or "").partition(" ")
        if estado in ESTADOS:
            return nombre, estado, valor
    return None


def canal_project_db(db: Path, L: Lectura):
    try:
        tls = TR.listar_timelines(db)
    except Exception as e:   # un .db a medias no debe tumbar los demas canales
        L.canales["resumen"] = f"ilegible: {e}"
        return
    L.db_leido = True
    L.canales["resumen"] = f"{len(tls)} timelines"
    marcas = []
    for t in tls:
        n = t["nombre"] or ""
        L.timelines.add(n)
        m = RE_RESUMEN.match(n)
        if m:
            c = L.corrida(m.group(1))
            c["canales"].add("resumen")
            if m.group(2):
                c["resumen"] = f"{m.group(2)}/{m.group(3)}"
            continue
        m = RE_MARCAS.match(n)
        if m:
            marcas.append((n, m.group(1)))
            continue
        m = RE_ERROR.match(n)
        if m:
            L.errores.append(n)

    estado_marcas = []
    for nombre, clave in marcas:
        try:
            ms = TR.leer_markers(db, nombre)
        except NotImplementedError as e:
            L.canales["marcas-db"] = f"no disponible: {e}"
            break
        except Exception as e:
            estado_marcas.append(f"{nombre}: ilegible ({e})")
            continue
        n = 0
        for mk in ms:
            r = _de_marker(mk["nombre"], mk["nota"], mk["custom_data"])
            if r:
                L.anotar(clave, *r, canal="marcas-db")
                n += 1
        estado_marcas.append(f"{nombre}: {n} de {len(ms)} markers")
    if "marcas-db" not in L.canales:
        L.canales["marcas-db"] = "; ".join(estado_marcas) or "sin timelines DIAG MARCAS"

    try:
        carpetas = TR.carpetas_media_pool(db)
    except Exception as e:
        L.canales["bin"] = f"ilegible: {e}"
        return
    bins = {}
    for nombre, padre in carpetas:
        m = RE_BIN.match(nombre or "")
        if m:
            bins[nombre] = m.group(1)
    n = 0
    for nombre, padre in carpetas:
        if padre in bins:
            m = RE_SUB.match(nombre or "")
            if m:
                L.anotar(bins[padre], m.group(1), m.group(2), m.group(3), "bin")
                n += 1
    L.canales["bin"] = f"{len(bins)} bins, {n} subcarpetas"


def _doc_json(doc: dict, canal: str, L: Lectura):
    clave = doc.get("clave")
    if not isinstance(clave, str):
        return
    c = L.corrida(clave)
    # El documento trae sello, ctx y r tal como los calculo el Lua: mandan
    # sobre lo que se dedujo de la clave, que con un sello raro puede fallar.
    for k in ("sello", "ctx"):
        if isinstance(doc.get(k), str) and doc[k]:
            c[k] = doc[k]
    if isinstance(doc.get("r"), int):
        c["r"] = doc["r"]
    for k in ("version", "producto"):
        if doc.get(k):
            c[k] = str(doc[k])
            L.versiones.setdefault(f"{k} ({canal})", str(doc[k]))
    if c["resumen"] is None and "ok" in doc and "total" in doc:
        c["resumen_doc"] = f"{doc['ok']}/{doc['total']}"
    for r in doc.get("resultados") or []:
        if isinstance(r, dict):
            L.anotar(clave, str(r.get("id", "")), str(r.get("estado", "")),
                     str(r.get("valor", "")), canal, str(r.get("familia", "")))


def _prefs_diag(tabla) -> dict[str, object]:
    """Busca Global.Diez50.Diag.<ctx> en la tabla, anidada o con clave plana."""
    out: dict[str, object] = {}

    def bajar(t, ruta):
        if not isinstance(t, dict):
            return
        for k, v in t.items():
            r = ruta + [str(k)]
            plano = ".".join(r)
            m = re.search(r"Diez50\.Diag\.([A-Za-z0-9_]+)$", plano)
            if m and not isinstance(v, dict):
                out[m.group(1)] = v
            elif isinstance(v, dict):
                bajar(v, r)

    bajar(tabla, [])
    return out


def canal_prefs(ruta: Path, L: Lectura):
    try:
        texto = ruta.read_text(errors="replace")
    except OSError as e:
        L.canales["prefs"] = f"ilegible: {e}"
        return
    valores: dict[str, object] = {}
    try:
        valores = _prefs_diag(leer_lua(texto))
        # Solo con la tabla entera leida se puede decir "la clave no esta".
        L.prefs_leido = True
    except ErrorLua as e:
        L.avisos.append(f"{ruta.name}: la tabla no se dejo parsear ({e}); se "
                        "buscan los JSON por su contenido")
    if not valores and "Diez50" in texto:
        # Parseo fallido, o la clave quedo en una forma que _prefs_diag no
        # reconoce: el JSON del diagnostico se reconoce por su contenido.
        for s in strings_lua(texto):
            if s.startswith("{") and '"formato"' in s:
                try:
                    d = json.loads(s)
                except json.JSONDecodeError:
                    continue
                valores[str(d.get("ctx", f"?{len(valores)}"))] = s
    n = 0
    for ctx, v in valores.items():
        try:
            doc = json.loads(v) if isinstance(v, str) else None
        except json.JSONDecodeError as e:
            L.avisos.append(f"prefs {ctx}: JSON ilegible ({e})")
            L.prefs_ilegibles.add(ctx)
            continue
        if not isinstance(doc, dict):
            L.prefs_ilegibles.add(ctx)
            continue
        if doc.get("aborta"):
            # El guardia escribe el aborto en la MISMA clave del ctx: pisa el
            # documento de una corrida buena anterior. Se recuerda para que
            # PREFS de esa corrida no salga NO por algo que paso despues.
            L.abortos.append({"ctx": ctx, **doc})
            L.prefs_abortados.add(ctx)
            continue
        L.prefs_docs[ctx] = doc
        _doc_json(doc, "prefs", L)
        n += 1
    L.canales["prefs"] = f"{n} contextos en {ruta}"


def canal_io(recibos: Path, L: Lectura):
    n = 0
    for f in sorted(recibos.glob("*.json")):
        if es_basura_mac(f):
            continue
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError) as e:
            L.avisos.append(f"recibo {f.name}: ilegible ({e})")
            continue
        if isinstance(doc, dict):
            _doc_json(doc, "io", L)
            n += 1
    L.canales["io"] = f"{n} recibos en {recibos}"


def canal_drt(ruta: Path, canal: str, L: Lectura, clave_por_defecto: str = ""):
    try:
        d = DRT.leer_drt(ruta)
    except Exception as e:
        L.avisos.append(f"{canal} {ruta.name}: ilegible ({e})")
        return 0
    if d.db_app_ver:
        L.versiones.setdefault(f"DbAppVer ({canal})", d.db_app_ver)
    m = RE_MARCAS.match(d.nombre) or RE_RESUMEN.match(d.nombre)
    clave = m.group(1) if m else clave_por_defecto
    if not clave:
        L.avisos.append(f"{canal} {ruta.name}: la timeline {d.nombre!r} no es DIAG "
                        "MARCAS/RESUMEN; sus markers no se atribuyen")
        return 0
    n = 0
    for mk in d.todos_los_markers():
        r = _de_marker(mk.nombre, mk.nota, mk.custom_data)
        if r:
            L.anotar(clave, *r, canal=canal)
            n += 1
    return n


def canal_print(ruta: Path, L: Lectura):
    """Lineas `DIEZ50DIAG <id> <estado> <valor>` hasta un `DIEZ50DIAG FIN <clave>`."""
    pendientes: list[tuple[str, str, str]] = []
    n = 0
    for linea in ruta.read_text(errors="replace").splitlines():
        m = RE_PRINT.search(linea)
        if not m:
            continue
        a, b, resto = m.groups()
        if a == "FIN":
            for id_, est, val in pendientes:
                L.anotar(b, id_, est, val, "print")
                n += 1
            pendientes = []
        elif a == "ABORTA":
            L.abortos.append({"ctx": "print", "linea": linea.strip()})
        else:
            pendientes.append((a, b, resto))
    if pendientes:
        L.avisos.append(f"print: {len(pendientes)} lineas sin su FIN; no se atribuyen")
    L.canales["print"] = f"{n} lineas en {ruta}"


# ==========================================================================
# confirmar el efecto fuera de Resolve
# ==========================================================================

def verificar_archivo(p: Path) -> tuple[bool, str]:
    """¿Existe, pesa mas de 0 bytes y se deja leer como lo que dice ser?"""
    if not p.exists():
        return False, f"no existe {p.name}"
    # FCPXML 1.10 sale como PAQUETE: una carpeta con Info.fcpxml dentro (Studio
    # 21.1.0.17, medido 2026-10-01). Abrirla como archivo daba IsADirectoryError
    # y un NO falso; se comprueba lo de adentro.
    if p.is_dir() and p.name.lower().endswith((".fcpxml", ".fcpxmld")):
        info = p / "Info.fcpxml"
        if not info.is_file():
            return False, f"{p.name}: paquete FCPXML sin Info.fcpxml"
        ok, que = verificar_archivo(info)
        return ok, f"{p.name} (paquete): {que}"
    tam = p.stat().st_size
    if tam == 0:
        return False, f"{p.name} pesa 0 bytes"
    nombre = p.name.lower()
    try:
        if nombre.endswith(".drt"):
            d = DRT.leer_drt(p)
            if not (d.nombre or d.pistas):
                return False, f"{p.name}: .drt sin timeline"
        elif nombre.endswith(".otio"):
            json.loads(p.read_text(errors="replace"))
        elif nombre.endswith(".fcpxml"):
            r = ET.parse(p).getroot()
            if r.tag != "fcpxml":
                return False, f"{p.name}: la raiz es <{r.tag}>, no <fcpxml>"
        elif nombre.endswith(".edl"):
            t = p.read_text(errors="replace")
            if "TITLE:" not in t and not re.search(r"(?m)^\d{3,6}\s+\S+\s+[AVB]", t):
                return False, f"{p.name}: no parece una EDL (sin TITLE ni eventos)"
        elif nombre.endswith(".aaf"):
            if p.read_bytes()[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
                return False, f"{p.name}: sin la firma de un AAF (structured storage)"
        elif nombre.endswith(".csv"):
            filas = list(csv.reader(_io.StringIO(p.read_text(errors="replace"))))
            if not filas:
                return False, f"{p.name}: CSV vacio"
        elif nombre.endswith(".txt"):
            if "\t" not in p.read_text(errors="replace"):
                return False, f"{p.name}: sin tabuladores"
    except Exception as e:
        return False, f"{p.name}: no se deja leer ({type(e).__name__}: {e})"
    return True, f"{p.name} {tam} bytes, se lee"


def exporte_de(valor: str) -> str | None:
    """Nombre del archivo que deberia haber dejado una prueba EXP, por su valor."""
    v = valor.upper()
    for token, nombre in EXPORTES:
        izq = "" if token == "METADATA" else r"(?<![A-Z])"
        if re.search(rf"{izq}{token}(?![A-Z])", v):
            return nombre
    return None


def destino_exp(valor: str) -> str | None:
    """'recibos' o 'volumen' segun lo que dice el valor de una prueba EXP.

    diagnostico.lua nombra el destino en el valor ("EXPORT_DRT volumen
    devolvio true"). None si el valor no lo dice (un ERR, una corrida vieja).
    """
    # Con frontera: "devolvio" lleva "vol" adentro y no habla de un volumen.
    if re.search(r"(?<![a-z])vol(umen)?(?![a-z])|/Volumes/", valor, re.I):
        return "volumen"
    if re.search(r"(?<![a-z])recibos(?![a-z])", valor, re.I):
        return "recibos"
    return None


def exp_por_id(id_: str) -> tuple[str, str] | None:
    """(archivo, destino) que le toca a un EXP por su lugar en CATALOGO_EXP."""
    fam, _, n = id_.partition("-")
    if fam != "EXP" or not n.isdigit():
        return None
    k, tipos = int(n), len(CATALOGO_EXP)
    if 1 <= k <= tipos:
        return CATALOGO_EXP[k - 1], "recibos"
    if tipos < k <= 2 * tipos:
        return CATALOGO_EXP[k - tipos - 1], "volumen"
    if k == 2 * tipos + 1:
        return "metadata.csv", "recibos"
    return None


def ubicar_exp(id_: str, valor: str) -> tuple[tuple[str, str] | None, str]:
    """((archivo, destino), "") de una prueba EXP, o (None, por que no se sabe).

    Manda el valor. El catalogo por id solo rellena lo que el valor calla, y
    solo si no lo contradice (mismo exporte).
    """
    nombre = exporte_de(valor)
    destino = "recibos" if nombre == "metadata.csv" else destino_exp(valor)
    cat = exp_por_id(id_)
    if cat is not None and nombre in (None, cat[0]):
        nombre = nombre or cat[0]
        destino = destino or cat[1]
    if nombre is None:
        return None, "no se sabe que exporte es por su valor"
    if destino is None:
        return None, "el valor no dice si el exporte iba a recibos o al volumen"
    return (nombre, destino), ""


def _archivos_exp(id_: str, valor: str, clave: str, F: Fuentes) -> tuple[list[Path], str]:
    """Archivo que corresponde a una prueba EXP, o ([], por que no se sabe).

    Contrato §8: nombre PLANO <raiz>/<clave>_<tipo>.<ext>, sin carpeta por
    clave (nadie puede crearla antes de correr).
    """
    ubic, porque = ubicar_exp(id_, valor)
    if ubic is None:
        return [], porque
    nombre, destino = ubic
    raiz = F.recibos_volumen if destino == "volumen" else F.recibos
    if raiz is None:
        return [], f"sin la carpeta de recibos {'del volumen' if destino == 'volumen' else ''}".strip()
    return [raiz / f"{clave}_{nombre}"], ""


def confirmar(fam: str, estado: str, valor: str, clave: str, F: Fuentes,
              L: Lectura, id_: str = "") -> tuple[str, str]:
    """(veredicto, nota) de una prueba ya reconciliada.

    NO se reserva para "se miro y no esta". Si no se pudo mirar (Project.db
    ilegible, Fusion.prefs que no parsea o que piso un aborto), sale SIN
    CONFIRMAR: un NO falso diria que Free no sabe hacer algo que si hizo.
    """
    if estado != "OK":
        nota = ""
        if fam == "EXP":
            archivos, _ = _archivos_exp(id_, valor, clave, F)
            if any(verificar_archivo(a)[0] for a in archivos):
                nota = "Resolve dijo " + estado + " pero el archivo existe y se lee"
        return estado, nota

    if fam == "EXP":
        archivos, porque = _archivos_exp(id_, valor, clave, F)
        if not archivos:
            return "SIN CONFIRMAR", porque
        malos = [n for ok, n in (verificar_archivo(a) for a in archivos) if not ok]
        if malos:
            return "NO", "Resolve dijo OK pero " + "; ".join(malos)
        return "OK", "archivo comprobado"
    if fam == "SAVE":
        if F.project_db is None or not L.db_leido:
            estado_db = L.canales.get("resumen", "no se leyo")
            return "SIN CONFIRMAR", f"sin Project.db legible para comprobar el guardado ({estado_db})"
        if any((RE_RESUMEN.match(t) or [None, None])[1] == clave for t in L.timelines):
            # Confirmacion agregada: la timeline pudo quedar guardada por
            # SAVE-01, por el SaveProject silencioso del final de D.correr o
            # por el autoguardado de Resolve. No aisla el retorno de SAVE-01.
            return "OK", ("Project.db tiene la timeline de resumen (agregado: tambien "
                          "cuentan el SaveProject final y el autoguardado)")
        return "NO", "Resolve dijo OK pero Project.db no tiene DIAG RESUMEN " + clave
    if fam == "PREFS":
        ctx = L.corrida(clave)["ctx"]
        if F.prefs is None:
            return "SIN CONFIRMAR", "sin Fusion.prefs"
        doc = L.prefs_docs.get(ctx)
        if doc is None:
            if ctx in L.prefs_abortados:
                return "SIN CONFIRMAR", (f"un aborto posterior piso Diez50.Diag.{ctx} "
                                         "en Fusion.prefs")
            if ctx in L.prefs_ilegibles:
                return "SIN CONFIRMAR", f"Diez50.Diag.{ctx} tiene un JSON ilegible"
            if not L.prefs_leido:
                return "SIN CONFIRMAR", "Fusion.prefs no se dejo leer entero"
            return "NO", f"Resolve dijo OK pero Fusion.prefs no tiene Diez50.Diag.{ctx}"
        if doc.get("clave") != clave:
            return "SIN CONFIRMAR", (f"Fusion.prefs guarda solo la ultima corrida de "
                                     f"{ctx} ({doc.get('clave')})")
        return "OK", "el valor esta en Fusion.prefs"
    if fam == "IO" and re.search(r"escrit|escrib|write", valor, re.I):
        # La prueba de escritura deja <recibos>/<clave>_io.txt (diagnostico.lua).
        if F.recibos is None:
            return "SIN CONFIRMAR", "sin la carpeta de recibos"
        hecho = F.recibos / f"{clave}_io.txt"
        if hecho.exists() and clave in hecho.read_text(errors="replace"):
            return "OK", f"existe recibos/{hecho.name}"
        return "NO", f"Resolve dijo OK pero no existe recibos/{hecho.name}"
    return "OK", ""


# ==========================================================================
# reconciliar y armar la matriz
# ==========================================================================

def reconciliar(L: Lectura, F: Fuentes) -> list[dict]:
    grupos: dict[tuple[str, str], list[Obs]] = {}
    for o in L.obs:
        grupos.setdefault((o.clave, o.id), []).append(o)
    filas = []
    for (clave, id_), obs in sorted(grupos.items()):
        por_canal = {}
        for o in obs:
            por_canal.setdefault(o.canal, o)
        orden = sorted(por_canal, key=lambda c: PRIORIDAD.index(c)
                       if c in PRIORIDAD else len(PRIORIDAD))
        mejor = por_canal[orden[0]]
        estados = {c: por_canal[c].estado for c in orden}
        notas = []
        if len(set(estados.values())) > 1:
            notas.append("canales discrepan: " +
                         ", ".join(f"{c}={e}" for c, e in estados.items()))
        # el valor mas largo: bin y marker lo recortan, io y prefs no
        valor = max((por_canal[c].valor for c in orden), key=len)
        fam = mejor.familia or id_.split("-")[0]
        veredicto, nota = confirmar(fam, mejor.estado, valor, clave, F, L, id_)
        if nota:
            notas.append(nota)
        c = L.corrida(clave)
        filas.append({"clave": clave, "sello": c["sello"], "ctx": c["ctx"],
                      "r": c["r"], "id": id_,
                      "familia": fam, "estado_resolve": mejor.estado,
                      "valor": valor, "estados": estados, "canales": orden,
                      "veredicto": veredicto, "nota": "; ".join(notas)})
    return filas


def _orden_ctx(ctxs) -> list[str]:
    return sorted(ctxs, key=lambda c: (CTX_ORDEN.index(c) if c in CTX_ORDEN
                                       else len(CTX_ORDEN), c))


def _orden_id(id_: str):
    fam, _, n = id_.partition("-")
    return (fam, int(n) if n.isdigit() else 0)


def matriz(filas: list[dict]) -> tuple[list[str], list[dict]]:
    """Una fila por id; por contexto, la corrida MAS RECIENTE de ese contexto."""
    ultima: dict[str, tuple] = {}
    for f in filas:
        k = (f["sello"], f["r"])
        if f["ctx"] not in ultima or k > ultima[f["ctx"]]:
            ultima[f["ctx"]] = k
    ctxs = _orden_ctx(ultima)
    por_id: dict[str, dict] = {}
    for f in filas:
        if (f["sello"], f["r"]) != ultima[f["ctx"]]:
            continue
        fila = por_id.setdefault(f["id"], {"id": f["id"], "familia": f["familia"],
                                           "por_ctx": {}, "canales": set(),
                                           "notas": []})
        fila["por_ctx"][f["ctx"]] = {"veredicto": f["veredicto"],
                                     "estado": f["estado_resolve"],
                                     "valor": f["valor"], "clave": f["clave"]}
        fila["canales"].update(f["canales"])
        if f["nota"]:
            fila["notas"].append(f"{f['ctx']}: {f['nota']}")
    out = []
    for id_ in sorted(por_id, key=_orden_id):
        fila = por_id[id_]
        vs = {v["veredicto"] for v in fila["por_ctx"].values()}
        fila["veredicto"] = vs.pop() if len(vs) == 1 else "VARIA"
        fila["canales"] = sorted(fila["canales"], key=lambda c: PRIORIDAD.index(c)
                                 if c in PRIORIDAD else 99)
        fila["nota"] = " / ".join(fila.pop("notas"))
        out.append(fila)
    return ctxs, out


# ==========================================================================
# metadatos: build, edicion, macOS
# ==========================================================================

def leer_maquina(ruta: Path | None) -> dict[str, str]:
    """maquina.txt de recoger_kit.sh (o un info.txt): `k=v`, `k: v` y secciones."""
    out: dict[str, str] = {}
    if ruta is None or not ruta.exists():
        return out
    seccion = ""
    for linea in ruta.read_text(errors="replace").splitlines():
        s = linea.strip()
        if not s:
            continue
        if s.startswith("== "):
            seccion = s[3:].strip()
            continue
        m = re.match(r"([A-Za-z_][\w .-]*?)\s*[:=]\s*(.*)$", s)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
        elif seccion and seccion not in out:
            out[seccion] = s
    return out


def leer_ojo(texto: str | None, casillas: Path | None) -> dict[str, str]:
    """Las 4 casillas a ojo. --ojo manda sobre manual/casillas.txt."""
    out = {k: "" for k in CASILLAS}
    fuentes = []
    if casillas and casillas.exists():
        fuentes.append(casillas.read_text(errors="replace").splitlines())
    if texto:
        fuentes.append(texto.split(","))
    for lineas in fuentes:
        for linea in lineas:
            linea = linea.split("#", 1)[0].strip()
            k, sep, v = linea.partition("=")
            k, v = k.strip(), v.strip().lower()
            if sep and k in out and v:
                out[k] = {"s": "si", "si": "si", "sí": "si", "yes": "si",
                          "n": "no", "no": "no"}.get(v, v)
    return out


def _db_app_ver_local() -> str:
    """DbAppVer de las preferencias de Resolve de ESTA Mac (config.user.xml).

    Solo sirve sin --desde: con --desde, la version es la de la otra Mac.
    """
    f = TR.PREFS / "config.user.xml"
    try:
        m = re.search(r'DbAppVer="([^"]+)"', f.read_text(errors="replace")[:4000])
    except OSError:
        return ""
    return m.group(1) if m else ""


def metadatos(L: Lectura, F: Fuentes) -> dict:
    maq = leer_maquina(F.maquina)
    if F.desde is None and not L.versiones:
        local = _db_app_ver_local()
        if local:
            L.versiones["DbAppVer (config.user.xml de esta Mac)"] = local
    def de_json(prefijo: str) -> str:
        # "?" es lo que pone diagnostico.lua cuando la llamada no devolvio texto.
        return next((v for k, v in L.versiones.items()
                     if k.startswith(prefijo) and v.strip() not in ("", "?")), "")

    version = de_json("version")
    if not version:
        version = (next((v for k, v in L.versiones.items() if k.startswith("DbAppVer")), "")
                   or maq.get("CFBundleVersion", "") or maq.get("CFBundleShortVersionString", ""))
    # La edicion solo se sabe por GetProductName, que viaja en los JSON (io o
    # prefs): "DaVinci Resolve Studio" o "DaVinci Resolve". El CFBundleName del
    # .app dice "DaVinci Resolve" en las DOS ediciones, asi que de ahi no se
    # deduce Free: una columna Studio etiquetada Free arruinaria justo la
    # comparacion que la matriz existe para hacer.
    producto = de_json("producto")
    if producto:
        origen = "GetProductName"
        edicion = ("Studio" if "studio" in producto.lower()
                   else "Free" if producto.strip() == "DaVinci Resolve" else "")
    else:
        producto = maq.get("CFBundleName", "")
        origen = "CFBundleName" if producto else ""
        edicion = "Studio" if "studio" in producto.lower() else ""
    macos = maq.get("ProductVersion", "")
    if not macos and F.desde is None:
        macos = platform.mac_ver()[0]
    return {"version": version or "desconocida", "producto": producto or "desconocido",
            "producto_origen": origen or "ninguno",
            "edicion": edicion or "desconocida", "macos": macos or "desconocido",
            "versiones_vistas": L.versiones, "maquina": maq}


# ==========================================================================
# salida
# ==========================================================================

def _celda(s) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def markdown(doc: dict) -> str:
    m = doc["metadatos"]
    ctxs = doc["contextos"]
    out = [f"# Matriz del diagnostico — {m['producto']} {m['version']}", "",
           f"- Edicion: {m['edicion']} (producto segun {m.get('producto_origen', '?')})  ",
           f"- macOS: {m['macos']}  ",
           f"- Generada: {doc['generado']}  ",
           "- Regla: un OK solo cuenta si hay retorno verdadero Y efecto comprobado "
           "fuera de Resolve (EXP, SAVE, IO, PREFS).", "",
           "## Canales", "", "| canal | estado |", "|---|---|"]
    for c, e in doc["canales"].items():
        out.append(f"| {c} | {_celda(e)} |")
    out += ["", "## Casillas a ojo", "", "| casilla | respuesta |", "|---|---|"]
    for k, v in doc["ojo"].items():
        out.append(f"| {k} | {v or 'sin contestar'} |")
    out += ["", "## Corridas", "",
            "| clave | ctx | r | resumen | canales |", "|---|---|---|---|---|"]
    for c in doc["corridas"]:
        resumen = (c.get("resumen") or
                   (f"{c['resumen_doc']} (json)" if c.get("resumen_doc") else "-"))
        out.append(f"| {c['clave']} | {c['ctx']} | {c['r']} | {resumen} | "
                   f"{', '.join(c['canales'])} |")
    cab = ["id", "familia"] + ctxs + ["canales", "veredicto", "nota"]
    out += ["", "## Matriz", "",
            "Celda = veredicto de la corrida mas reciente de ese contexto. "
            "Un `*` marca que Resolve dijo otra cosa (su estado va entre parentesis).",
            "", "| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    for f in doc["matriz"]:
        celdas = []
        for c in ctxs:
            v = f["por_ctx"].get(c)
            if not v:
                celdas.append("-")
            elif v["veredicto"] != v["estado"]:
                celdas.append(f"{v['veredicto']}* ({v['estado']})")
            else:
                celdas.append(v["veredicto"])
        out.append("| " + " | ".join(_celda(x) for x in
                   [f["id"], f["familia"], *celdas, ", ".join(f["canales"]),
                    f["veredicto"], f["nota"]]) + " |")
    if doc["errores"] or doc["abortos"]:
        out += ["", "## Errores y abortos", ""]
        out += [f"- timeline de error: {_celda(e)}" for e in doc["errores"]]
        out += [f"- aborto: {_celda(json.dumps(a, ensure_ascii=False))}"
                for a in doc["abortos"]]
    if doc["avisos"]:
        out += ["", "## Avisos", ""] + [f"- {_celda(a)}" for a in doc["avisos"]]
    return "\n".join(out) + "\n"


def _limpio(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "x"


def escribir(kit: Path, doc: dict, md: str) -> tuple[Path, Path]:
    """Escribe SOLO en KIT/matriz. Cualquier otra ruta es un error de programa."""
    destino = (kit / "matriz").resolve()
    destino.mkdir(exist_ok=True)
    m = doc["metadatos"]
    base = (f"{_limpio(m['version'])}-{_limpio(m['producto'])}-"
            f"{time.strftime('%Y%m%d-%H%M', time.localtime(doc['generado_epoch']))}")
    pj, pm = destino / f"{base}.json", destino / f"{base}.md"
    for p in (pj, pm):
        if p.resolve().parent != destino:
            raise RuntimeError(f"ruta fuera de KIT/matriz: {p}")
    pj.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=list) + "\n")
    pm.write_text(md)
    return pj, pm


def leer(F: Fuentes) -> Lectura:
    L = Lectura(avisos=list(F.avisos))
    if F.project_db is not None:
        if F.project_db.exists():
            canal_project_db(F.project_db, L)
        else:
            L.avisos.append(f"no existe {F.project_db}")
    if F.prefs is not None and F.prefs.exists():
        canal_prefs(F.prefs, L)
    if F.recibos is not None:
        canal_io(F.recibos, L)
    n = 0
    # Los .drt de Export son planos (contrato §8): <clave>_drt.drt. La clave
    # es lo que va antes del sufijo, por si la timeline del .drt no la dice.
    for raiz in (F.recibos, F.recibos_volumen):
        if raiz is None:
            continue
        for f in sorted(raiz.glob("*_drt.drt")):
            if not es_basura_mac(f):
                n += canal_drt(f, "drt-export", L,
                               clave_por_defecto=f.name[:-len("_drt.drt")])
    if F.recibos is not None or F.recibos_volumen is not None:
        L.canales["drt-export"] = f"{n} markers en exportes .drt"
    n = 0
    for f in F.drts:
        n += canal_drt(f, "drt-manual", L)
    if F.drts:
        L.canales["drt-manual"] = f"{n} markers en {len(F.drts)} .drt"
    if F.print_txt is not None:
        canal_print(F.print_txt, L)
    return L


def correr(a: argparse.Namespace) -> tuple[dict, str, tuple[Path, Path] | None]:
    F = resolver_fuentes(a)
    if not F.kit.is_dir():
        raise SystemExit(f"No existe el kit {F.kit}. Pasa --kit con la carpeta del kit "
                         "(la matriz se escribe en KIT/matriz).")
    L = leer(F)
    for c in L.corridas.values():
        if c["sello"] == "sinsello":
            # diagnostico.lua no pudo leer config.lua: sin sello, la corrida no
            # se ordena en el tiempo con las demas del mismo ctx.
            L.avisos.append(f"{c['clave']}: corrida sin sello (config.lua no se leyo "
                            "dentro de Resolve)")
    filas = reconciliar(L, F)
    ctxs, filas_m = matriz(filas)
    ahora = time.time()
    doc = {
        "formato": FORMATO,
        "generado": time.strftime("%Y-%m-%d %H:%M", time.localtime(ahora)),
        "generado_epoch": ahora,
        "metadatos": metadatos(L, F),
        "fuentes": {"kit": str(F.kit), "desde": str(F.desde or ""),
                    "project_db": str(F.project_db or ""), "prefs": str(F.prefs or ""),
                    "recibos": str(F.recibos or ""),
                    "recibos_volumen": str(F.recibos_volumen or ""),
                    "drt": [str(x) for x in F.drts]},
        "canales": L.canales,
        "ojo": leer_ojo(a.ojo, F.casillas),
        "corridas": [{**c, "canales": sorted(c["canales"])}
                     for c in sorted(L.corridas.values(),
                                     key=lambda c: (c["sello"], _orden_ctx([c["ctx"]]), c["r"]))],
        "contextos": ctxs,
        "matriz": filas_m,
        "filas": filas,
        "errores": sorted(L.errores),
        "abortos": L.abortos,
        "avisos": L.avisos,
    }
    md = markdown(doc)
    rutas = None if a.no_escribir else escribir(F.kit, doc, md)
    return doc, md, rutas


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kit", default="~/Diez50-diag",
                    help="Carpeta del kit; la matriz va a KIT/matriz (default ~/Diez50-diag).")
    ap.add_argument("--desde", help="Carpeta que junto recoger_kit.sh en otra Mac.")
    ap.add_argument("--projectdb", help="Project.db a leer (se copia; nunca se abre el original).")
    ap.add_argument("--prefs", help="Fusion.prefs a leer.")
    ap.add_argument("--drt", action="append", help="Un .drt exportado a mano (repetible).")
    ap.add_argument("--ojo", help="Casillas a ojo: menu=si,submenu=no,print_consola=si,error_visible=no")
    ap.add_argument("--print", help="Archivo con la salida de la Consola pegada a mano.")
    ap.add_argument("--proyecto", default=PROYECTO_DEFAULT,
                    help=f"Nombre (o prefijo) del proyecto de diagnostico (default {PROYECTO_DEFAULT}).")
    ap.add_argument("--no-escribir", action="store_true",
                    help="Solo imprime el Markdown; no escribe KIT/matriz.")
    a = ap.parse_args(argv)
    doc, md, rutas = correr(a)
    sys.stdout.write(md)
    if rutas:
        print(f"\nEscrito: {rutas[0]}\n         {rutas[1]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
