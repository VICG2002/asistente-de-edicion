#!/usr/bin/env python3
"""Prepara el kit de diagnostico de la Fase 0 (Resolve Free 21.1).

PARA QUE SIRVE
El plan del plugin de Resolve Free se apoya en cosas que nadie ha medido en
Free: que el menu Workspace > Scripts ejecute Lua, que `dofile` lea desde `~`,
desde un volumen y desde una ruta con acentos, que `Timeline:Export` escriba,
que `print` se vea... Lo que dicen terceros de la build .17 no alcanza para
construir encima. `resolve/diagnostico.lua` lo mide, y este script le arma el
terreno: todo lo que el diagnostico necesita encontrar en disco para que cada
prueba mida la plataforma y no la falta de un archivo.

EL CONTRATO
Nombres, formatos y rutas salen del contrato del kit (compartido con
`resolve/diagnostico.lua` y `bin/leer_diagnostico.py`). Resumen:

  KIT/config.lua          tabla Lua pura que el diagnostico lee con dofile
  KIT/diagnostico.lua     COPIA del motor: los stubs apuntan aqui, no al motor,
                          para que el kit viaje solo a otra Mac
  KIT/media/              negro de 10 min (ffmpeg) y el par de
                          generar_media_prueba.py
  KIT/fixtures/           ok, error, acentos, grande (horneado SINTETICO)
  KIT/stubs/              los 4 stubs del menu, con rutas absolutas del KIT
  KIT/DIEZ50DIAG.dmg      imagen APFS de 20 MB montada en /Volumes/DIEZ50DIAG
  ~/Library/Application Support/Diez50/diag/fixture_ok.lua

LO QUE NUNCA HACE
  - Escribir en /Volumes fuera de /Volumes/DIEZ50DIAG. Y ahi solo despues de
    comprobar con `hdiutil info -plist` que ese punto de montaje es NUESTRA
    imagen. Un disco que se llame igual por accidente seria material rodado:
    si el nombre esta ocupado por otra cosa, se aborta sin escribir. La
    guarda no se fia del texto de la ruta (/volumes en minusculas, el
    firmlink /System/Volumes/Data/Volumes): compara tambien el dispositivo, y
    fuera del disco de la carpeta de inicio no se escribe.
  - Usar como kit una carpeta con contenido ajeno: solo una vacia o una que
    lleve el sello .diez50-kit. Y el zip lleva una lista blanca, no la carpeta.
  - Tocar un script del menu que no se llame `Diez50*`. En Fusion/Scripts
    conviven scripts viejos de mayo que no son nuestros.
  - Borrar o pisar algo que no haya creado el propio kit. `--quitar-stubs`
    (y quitar_kit.sh en la otra Mac) borra exactamente los 4 stubs, solo si
    su primera linea lleva la marca del generador, y la carpeta
    Utility/Diez50 solo si queda vacia. `--instalar-stubs` tampoco pisa un
    destino sin la marca: macOS no distingue mayusculas, y el nombre solo no
    dice de quien es un archivo.

IDEMPOTENTE. Cada archivo se escribe solo si su contenido cambia, y la media
solo se genera si falta. Dos corridas dan el mismo arbol salvo el sello, que
es la hora de la preparacion (`DIEZ50_SELLO` lo fija para los tests).

Uso:
    python3 bin/preparar_diagnostico.py
    python3 bin/preparar_diagnostico.py --dry-run
    python3 bin/preparar_diagnostico.py --instalar-stubs
    python3 bin/preparar_diagnostico.py --quitar-stubs
    python3 bin/preparar_diagnostico.py --kit-zip ~/Desktop/Diez50-diag.zip

Variables de entorno (para tests):
    DIEZ50_SCRIPTS_DIR   carpeta Fusion/Scripts donde se instalan los stubs
    DIEZ50_SELLO         sello fijo AAAAMMDD-HHMM en vez de la hora local
"""

from __future__ import annotations

import argparse
import filecmp
import os
import plistlib
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
DIAG_MOTOR = ENGINE / "resolve" / "diagnostico.lua"
GENERADOR_PAR = ENGINE / "bin" / "generar_media_prueba.py"

FORMATO = 1
PROYECTO_REQUERIDO = "DIEZ50_DIAG"
VOLNAME = "DIEZ50DIAG"
VOLUMEN = Path("/Volumes") / VOLNAME
NOMBRE_DMG = f"{VOLNAME}.dmg"
LARGO_N = 500

# La fecha fija de la prueba de paridad de `os.time`. Lua la convierte con la
# TZ de la maquina donde corre Resolve; aqui se convierte con la de la maquina
# que prepara. Si difieren, es la TZ, no un bug: por eso el valor es informativo.
OS_TIME_FIJO = (("year", 2026), ("month", 6), ("day", 11),
                ("hour", 19), ("min", 55), ("sec", 13))

# Los 4 stubs del menu: (carpeta relativa a Fusion/Scripts, nombre, ctx).
# ctx None = el stub con error de sintaxis a proposito. Todos los nombres
# empiezan con "Diez50": es la unica marca que permite instalar y quitar sin
# tocar los scripts ajenos que ya viven en esas carpetas.
STUBS = (
    ("Utility", "Diez50 Diagnostico.lua", "menu_utility"),
    ("Utility/Diez50", "Diez50 Diagnostico sub.lua", "menu_sub"),
    ("Edit", "Diez50 Diagnostico edit.lua", "menu_edit"),
    ("Utility", "Diez50 Diagnostico error.lua", None),
)
PREFIJO_STUB = "Diez50"

# El zip para otra Mac lleva una lista blanca (archivos_del_zip). Queda fuera,
# entre otras cosas, la .dmg: la crea instalar_kit.sh alla (una imagen montada
# aqui no se copia bien, y 20 MB vacios no valen el viaje). recibos/, manual/ y
# matriz/ son resultados de ESTA Mac y viajan vacios: mezclarlos con los de la
# otra haria que el lector reconcilie corridas de dos maquinas.
CARPETAS_DE_RESULTADOS = ("recibos", "manual", "matriz")

FIXTURE_OK = "return { suma = 12345 }\n"

CASILLAS = """\
# Diez50 - casillas a ojo del diagnostico. Contesta si o no despues de cada =
# menu: aparecio "Diez50 Diagnostico" en Workspace > Scripts
menu=
# submenu: aparecio el submenu "Diez50" con "Diez50 Diagnostico sub" dentro
submenu=
# print_consola: al pegar la linea en la Consola aparecieron lineas DIEZ50DIAG
print_consola=
# error_visible: al correr "Diez50 Diagnostico error" Resolve mostro algun mensaje
error_visible=
"""


class Aborto(RuntimeError):
    """Una condicion que obliga a parar sin escribir. El mensaje dice cual."""


# --------------------------------------------------------------------------
# rutas que dependen de HOME (los tests lo cambian por un temporal)
# --------------------------------------------------------------------------

def casa() -> Path:
    # expanduser lee $HOME primero: es lo que deja a los tests apuntar a un
    # temporal sin riesgo de escribir en el HOME real.
    return Path(os.path.expanduser("~"))


def dir_scripts() -> Path:
    env = os.environ.get("DIEZ50_SCRIPTS_DIR")
    if env:
        return Path(env)
    return (casa() / "Library/Application Support/Blackmagic Design"
            / "DaVinci Resolve/Fusion/Scripts")


def ruta_appsupport() -> Path:
    return casa() / "Library/Application Support/Diez50/diag/fixture_ok.lua"


def sello_actual() -> str:
    env = os.environ.get("DIEZ50_SELLO")
    if env:
        if not re.fullmatch(r"\d{8}-\d{4}", env):
            raise Aborto(f"DIEZ50_SELLO={env!r} no tiene la forma AAAAMMDD-HHMM")
        return env
    return time.strftime("%Y%m%d-%H%M")


def os_time_esperado_local() -> int:
    d = dict(OS_TIME_FIJO)
    return int(time.mktime((d["year"], d["month"], d["day"], d["hour"],
                            d["min"], d["sec"], 0, 0, -1)))


# --------------------------------------------------------------------------
# el escritor: el UNICO lugar que toca disco o ejecuta algo
# --------------------------------------------------------------------------

# El firmlink de macOS: /Volumes, /Users, /private... son tambien
# /System/Volumes/Data/Volumes, /System/Volumes/Data/Users... El mismo disco
# se alcanza por los dos caminos, y realpath NO lo normaliza.
FIRMLINK_DATA = "/system/volumes/data"


def _forma_comparable(p: str) -> str:
    """La ruta como la ve APFS al comparar: sin mayusculas (APFS no las
    distingue: /volumes/T9 ES /Volumes/T9), sin barras dobles y sin el prefijo
    del firmlink de Data. Solo sirve para comparar, nunca para escribir."""
    q = re.sub(r"^/+", "/", os.path.normpath(p)).casefold()
    if q == FIRMLINK_DATA:
        return "/"
    if q.startswith(FIRMLINK_DATA + "/"):
        q = q[len(FIRMLINK_DATA):]
    return q


def bajo_volumes(p: str) -> bool:
    q = _forma_comparable(p)
    return q == "/volumes" or q.startswith("/volumes/")


def dentro_del_volumen(p: str) -> bool:
    q, v = _forma_comparable(p), _forma_comparable(str(VOLUMEN))
    return q == v or q.startswith(v + "/")


def _dispositivo(ruta: str) -> int | None:
    """st_dev del primer ancestro que existe (os.stat sigue los enlaces).

    Es lo que no se deja enganar por la forma de escribir la ruta: un disco
    montado tiene su propio dispositivo, se llegue a el por /Volumes, por
    /volumes, por el firmlink o por un enlace simbolico."""
    p = os.path.abspath(ruta)
    while True:
        try:
            return os.stat(p).st_dev
        except OSError:
            padre = os.path.dirname(p)
            if padre == p:
                return None
            p = padre


def dispositivos_de_casa() -> set[int]:
    """Los discos donde el kit SI puede escribir: el volumen de datos del
    sistema (donde viven HOME, /tmp y la carpeta de scripts de Resolve) y el de
    HOME por si esta en otro lado. Todo lo demas es un disco ajeno."""
    devs = set()
    datos = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    for p in (datos, str(casa())):
        d = _dispositivo(p)
        if d is not None:
            devs.add(d)
    return devs


class Escritor:
    """Centraliza cada escritura y cada comando externo.

    Por que una clase y no llamadas sueltas: `--dry-run` tiene que listar TODO
    lo que haria sin hacer nada, y la guarda de /Volumes tiene que valer para
    TODAS las escrituras. Si una sola escritura se saltara esta clase, ninguna
    de las dos garantias se sostendria.
    """

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        # Se pone en True solo despues de verificar que /Volumes/DIEZ50DIAG es
        # nuestra imagen. Antes de eso, cualquier escritura en /Volumes truena.
        self.volumen_verificado = False
        # El st_dev de la imagen verificada: dentro de /Volumes/DIEZ50DIAG solo
        # se escribe si la ruta cae en ESE dispositivo.
        self.dev_volumen: int | None = None

    def _vigilar(self, ruta: Path) -> None:
        """Truena (Aborto) antes de cualquier escritura fuera de lugar.

        Dos defensas, porque cada una sola tiene un hueco:
          - Por texto, sin mayusculas y sin el firmlink: /volumes/T9 y
            /System/Volumes/Data/Volumes/T9 son /Volumes/T9. Atrapa tambien
            rutas en /Volumes que todavia no existen (un disco sin montar).
          - Por dispositivo: el primer ancestro que existe tiene que estar en
            el disco de datos del sistema (o en la imagen ya verificada). Atrapa
            lo que el texto no ve: un enlace, una forma de escribir la ruta que
            nadie previo. Un disco con material rodado es otro dispositivo.
        """
        r = os.path.realpath(str(ruta))
        s = str(ruta)
        en_volumen = False
        for p in (r, s):
            if bajo_volumes(p):
                if not dentro_del_volumen(p):
                    raise Aborto(f"Se intento escribir en {p}: fuera de "
                                 f"{VOLUMEN} nunca se escribe en /Volumes.")
                if not self.volumen_verificado and not self.dry_run:
                    raise Aborto(f"Se intento escribir en {p} sin haber "
                                 f"verificado que {VOLUMEN} es la imagen del kit.")
                en_volumen = True
        dev = _dispositivo(s)
        if en_volumen:
            # En dry-run sin verificar no hay dispositivo con que comparar, y
            # nada se escribe: basta con la guarda por texto.
            if self.volumen_verificado and dev != self.dev_volumen:
                raise Aborto(f"{s} no esta en la imagen verificada {VOLUMEN}.")
            return
        if dev is not None and dev not in dispositivos_de_casa():
            raise Aborto(f"Se intento escribir en {s}, que esta en otro disco "
                         "(no en el de tu carpeta de inicio). El kit solo "
                         f"escribe en tu disco y en {VOLUMEN}.")

    def aviso(self, texto: str) -> None:
        print(f"[dry-run] {texto}" if self.dry_run else texto)

    def carpeta(self, ruta: Path) -> None:
        self._vigilar(ruta)
        if ruta.is_dir():
            return
        if self.dry_run:
            print(f"[dry-run] crearia carpeta {ruta}")
            return
        ruta.mkdir(parents=True, exist_ok=True)

    def escribir(self, ruta: Path, texto: str, *, ejecutable: bool = False,
                 solo_si_falta: bool = False) -> None:
        """Escribe solo si el contenido cambia: asi dos corridas no tocan ni
        el mtime, y el zip sale igual."""
        self._vigilar(ruta)
        datos = texto.encode("utf-8")
        if ruta.exists() and (solo_si_falta or ruta.read_bytes() == datos):
            if ejecutable and not self.dry_run and not os.access(ruta, os.X_OK):
                ruta.chmod(0o755)
            return
        if self.dry_run:
            print(f"[dry-run] escribiria {ruta}")
            return
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(datos)
        if ejecutable:
            ruta.chmod(0o755)

    def copiar(self, origen: Path, destino: Path) -> None:
        self._vigilar(destino)
        if destino.is_symlink():
            raise Aborto(f"{destino} es un enlace simbolico: no se escribe a "
                         "traves de el.")
        if self.dry_run and not origen.is_file():
            # En dry-run el kit puede no existir todavia (nadie genero sus
            # stubs): no hay contra que comparar, y lo honesto es decir que
            # se escribiria. Sin esto, filecmp truena con un traceback.
            print(f"[dry-run] escribiria {destino}  (copia de {origen}, que "
                  "se generaria antes)")
            return
        if destino.is_file() and filecmp.cmp(origen, destino, shallow=False):
            return
        if self.dry_run:
            print(f"[dry-run] escribiria {destino}  (copia de {origen})")
            return
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origen, destino)

    def borrar_archivo(self, ruta: Path) -> None:
        self._vigilar(ruta)
        if self.dry_run:
            print(f"[dry-run] borraria {ruta}")
            return
        ruta.unlink()

    def borrar_carpeta_vacia(self, ruta: Path) -> None:
        self._vigilar(ruta)
        if self.dry_run:
            print(f"[dry-run] borraria la carpeta vacia {ruta}")
            return
        ruta.rmdir()                  # rmdir falla si no esta vacia: es la idea

    def correr(self, cmd: list[str], *, capturar: bool = False,
               ) -> subprocess.CompletedProcess | None:
        if self.dry_run:
            print(f"[dry-run] ejecutaria: {shlex.join(cmd)}")
            return None
        return subprocess.run(cmd, capture_output=capturar, text=True)


# --------------------------------------------------------------------------
# textos generados
# --------------------------------------------------------------------------

def lua_str(s) -> str:
    """Cadena Lua entre comillas dobles. Las rutas pueden llevar espacios y
    acentos (Application Support, Año Ñ): los bytes UTF-8 viajan tal cual."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s.replace("\n", "\\n").replace("\r", "\\r") + '"'


def rutas_kit(kit: Path) -> dict[str, Path]:
    return {
        "config": kit / "config.lua",
        "diagnostico": kit / "diagnostico.lua",
        "negro": kit / "media" / "negro_10min.mov",
        "par": kit / "media" / "par",
        # Nombres que produce generar_media_prueba.py (camara.mov + lav.wav).
        "video": kit / "media" / "par" / "camara.mov",
        "wav": kit / "media" / "par" / "lav.wav",
        "ok": kit / "fixtures" / "fixture_ok.lua",
        "error": kit / "fixtures" / "fixture_error.lua",
        "acentos": kit / "fixtures" / "Año Ñ" / "fixture ok.lua",
        "grande": kit / "fixtures" / "grande_data.lua",
        "recibos": kit / "recibos",
        "manual": kit / "manual",
        "stubs": kit / "stubs",
        "matriz": kit / "matriz",
        "dmg": kit / NOMBRE_DMG,
        "linea": kit / "linea_consola.txt",
        "instalar": kit / "instalar_kit.sh",
        "recoger": kit / "recoger_kit.sh",
        "quitar": kit / "quitar_kit.sh",
        "leeme": kit / "LEEME.txt",
        "sello_kit": kit / ".diez50-kit",
    }


def config_lua(kit: Path, sello: str, con_volumen: bool) -> str:
    r = rutas_kit(kit)
    vol_fix = (lua_str(VOLUMEN / "Diez50-diag" / "fixture_ok.lua")
               if con_volumen else "nil")
    vol_rec = (lua_str(VOLUMEN / "Diez50-diag" / "recibos")
               if con_volumen else "nil")
    fijo = ", ".join(f"{k} = {v}" for k, v in OS_TIME_FIJO)
    return f"""\
-- Diez50 - configuracion del diagnostico de la Fase 0.
-- Generado por preparar_diagnostico.py: no editar. Lua NO calcula fechas: el
-- sello lo pone la preparacion.
return {{
  formato   = {FORMATO},
  kit       = {lua_str(kit)},
  sello     = {lua_str(sello)},
  proyecto_requerido = {lua_str(PROYECTO_REQUERIDO)},
  media = {{
    negro = {lua_str(r["negro"])},
    video = {lua_str(r["video"])},
    wav   = {lua_str(r["wav"])},
  }},
  fixtures = {{
    ok         = {lua_str(r["ok"])},
    error      = {lua_str(r["error"])},
    acentos    = {lua_str(r["acentos"])},
    grande     = {lua_str(r["grande"])},
    volumen    = {vol_fix},
    appsupport = {lua_str(ruta_appsupport())},
  }},
  recibos = {lua_str(r["recibos"])},
  recibos_volumen = {vol_rec},
  permitir_pool = true,
  largo_n = {LARGO_N},
  os_time_fijo = {{ {fijo} }},
  os_time_esperado_local = {os_time_esperado_local()},
}}
"""


FIXTURE_ERROR = """\
-- Diez50 - fixture con error de sintaxis A PROPOSITO: mide que dofile falla
-- de forma atrapable (pcall) y no tumba el script que lo llama.
return { suma = = 12345 }
"""


# La marca que dice "este archivo lo genero el kit". Instalar y quitar stubs la
# buscan en la PRIMERA linea del destino antes de pisarlo o borrarlo: en APFS
# (que no distingue mayusculas) "diez50 diagnostico edit.lua" de otra persona
# es el mismo archivo que nuestro stub, y el nombre solo no basta para saber
# de quien es. Es ASCII a proposito: grep -F con LC_ALL=C la encuentra igual.
MARCA_GENERADO = "Generado por preparar_diagnostico.py"


def cabecera_stub() -> str:
    # Texto literal del contrato (seccion 4); lleva UTF-8 en el comentario.
    return ("-- Diez50 · diagnóstico de la Fase 0. " + MARCA_GENERADO
            + ": no editar.\n")


def es_generado_por_el_kit(ruta: Path, lineas: int = 1) -> bool:
    """True si alguna de las primeras `lineas` de `ruta` lleva MARCA_GENERADO
    (los stubs la llevan en la primera; config.lua, en la segunda)."""
    try:
        with open(ruta, "rb") as fh:
            cabeza = [fh.readline(4096) for _ in range(lineas)]
    except OSError:
        return False
    return any(MARCA_GENERADO.encode("ascii") in l for l in cabeza)


# El sello de la carpeta del kit. Se escribe ANTES que cualquier otra cosa, asi
# que hasta un kit que se aborto a medias (solo con la .dmg, por ejemplo) se
# reconoce como nuestro en la siguiente corrida.
SELLO_KIT = f"-- Diez50 - carpeta del kit de diagnostico. {MARCA_GENERADO}: no editar.\n"


def revisar_carpeta_del_kit(kit: Path) -> None:
    """Aborta si `kit` es una carpeta ajena con contenido.

    Por que: el preparador escribe config.lua, LEEME.txt, fixtures/... sin
    preguntar, porque en SU carpeta son suyos. Un `--kit ~/Escritorio` pisaria
    un LEEME.txt ajeno. Una carpeta vacia o que no existe se puede usar; una
    con contenido, solo si lleva el sello del kit (o, en un kit anterior al
    sello, un config.lua con la marca)."""
    if not kit.exists():
        return
    if not kit.is_dir():
        raise Aborto(f"{kit} existe y no es una carpeta.")
    contenido = [p for p in kit.iterdir() if p.name != ".DS_Store"]
    if not contenido:
        return
    r = rutas_kit(kit)
    if es_generado_por_el_kit(r["sello_kit"]) or es_generado_por_el_kit(r["config"], 3):
        return
    raise Aborto(f"{kit} ya tiene archivos y no es un kit de Diez50 (no lleva "
                 f"{r['sello_kit'].name}). No se escribe ahi para no pisar nada: "
                 "usa una carpeta nueva o vacia con --kit.")


def stub_lua(ctx: str, kit: Path) -> str:
    """El stub del menu. Sigue el cuerpo del contrato (seccion 4) con tres
    ajustes en el camino de error E01, que no cambian nombres ni formatos:

      - La timeline E01 solo se crea si el proyecto abierto empieza con
        DIEZ50_DIAG. La proteccion por nombre vive en diagnostico.lua; si ese
        archivo no carga (kit borrado, movido, roto), el stub era lo unico que
        quedaba y creaba la timeline en el proyecto abierto, aunque fuera uno
        real del colega. Los stubs pueden sobrevivir al kit en el menu.
      - Se quita el prefijo "ruta:linea: " del error antes de recortar a 60:
        la ruta del kit se comia los 60 caracteres y la causa no se veia.
      - Una tabla sin `correr` tambien es E01: antes no hacia nada ni dejaba
        rastro.
      - Del mensaje salen / \\ : * ? " < > |, que Resolve rechaza en un nombre
        de timeline (Studio 21.1.0.17, medido 2026-10-01). Con el kit borrado
        el error es "cannot open /Users/...": sin esto la E01 no nacia justo
        en el caso para el que existe.

    Y uno en la llamada: `fusion = fusion or fu` viaja dentro de ctx. En
    fuscript el script que llama corre en su propio entorno y lo cargado con
    dofile corre en _G (medido 2026-09-24): un global que solo ve el stub no
    le llega al diagnostico. Si aqui tambien es nil, diagnostico.lua prueba
    resolve:Fusion().
    """
    r = rutas_kit(kit)
    n = len(PROYECTO_REQUERIDO)
    return cabecera_stub() + f"""\
local R = resolve or (Resolve and Resolve())
local ok, D = pcall(dofile, {lua_str(r["diagnostico"])})
if ok and (type(D) ~= "table" or type(D.correr) ~= "function") then
  ok, D = false, "diagnostico.lua no devolvio una tabla con correr"
end
if ok then
  local ok2, err = pcall(D.correr, {{ ctx = {lua_str(ctx)}, config = {lua_str(r["config"])}, resolve = R, fusion = fusion or fu }})
  if not ok2 then ok, D = false, err end
end
if not ok then
  pcall(function()
    local p = R:GetProjectManager():GetCurrentProject()
    -- Solo en el proyecto de prueba: un proyecto real nunca se toca.
    if string.sub(tostring(p:GetName()), 1, {n}) ~= {lua_str(PROYECTO_REQUERIDO)} then return end
    local msg = string.gsub(tostring(D), "^.-:%d+: ", "")
    msg = string.gsub(msg, '[/\\\\:%*%?"<>|]', "_")
    local tl = p:GetMediaPool():CreateEmptyTimeline("DIAG ERROR E01 " .. string.sub(msg, 1, 60))
    if tl then p:SetCurrentTimeline(tl) end
  end)
end
"""


def stub_error() -> str:
    return cabecera_stub() + """\
-- Este stub NO compila a proposito: mide que ve el editor cuando un script del
-- menu tiene un error de sintaxis (un mensaje, nada, o un cuelgue).
local R = = resolve
"""


def linea_consola(kit: Path) -> str:
    r = rutas_kit(kit)
    return (f'dofile({lua_str(r["diagnostico"])}).correr({{ctx="consola", '
            f'config={lua_str(r["config"])}, resolve=resolve}})')


# Palabras para el horneado sintetico. Inventadas y genericas a proposito: el
# fixture no puede llevar ni un nombre, lugar o frase del material real.
_SUJETOS = ("Una persona", "Dos personas", "Un grupo", "Una mujer", "Un hombre",
            "Tres personas", "Un nino", "Una pareja")
_ACCIONES = ("camina por un sendero", "conversa frente a la camara",
             "mira hacia el horizonte", "prepara cafe en una cocina",
             "cruza una calle", "sube una escalera", "arma una tienda",
             "lee en voz alta", "ajusta un tripie", "rie con alguien fuera de cuadro")
_LUGARES = ("en un patio con plantas", "junto a una ventana", "en un mercado",
            "bajo un arbol", "en un pasillo angosto", "frente a una pared blanca",
            "en una azotea", "dentro de un auto")
_PLANOS = ("PG", "PA", "PM", "PMC", "PP", "PPP", "DET")
_ANGULOS = ("Normal", "Picado", "Contrapicado", "POV", "Cenital")
_CAMARAS = ("main", "gopro", "dron", "celular", "b-cam")
_CATEGORIAS = ("entrevista", "b-roll", "ambiente", "accion", "")


def grande_data(objetivo_bytes: int = 3_000_000) -> str:
    """Horneado SINTETICO de ~3 MB con la forma de export_lua_data.py (v11).

    Por que existe: los horneados reales pesan 3-4 MB y el diagnostico mide si
    `dofile` de uno de ese tamano cabe en el sandbox del menu (tiempo, memoria,
    limite de constantes de LuaJIT). No puede ser un horneado real: el kit
    viaja a Macs ajenas. Semilla fija: dos corridas dan el mismo archivo.
    """
    rnd = random.Random(1050)
    lineas = ["-- SINTETICO: generado por preparar_diagnostico.py. Ningun dato "
              "es real; solo imita la forma de un horneado (export_lua_data v11).",
              "return {", "  clips = {"]
    byname: list[tuple[str, str]] = []
    sync: list[tuple[str, str]] = []
    total = 0
    i = 0
    while total < objetivo_bytes:
        i += 1
        carpeta = f"Locacion {1 + i % 23:02d}"
        nombre = f"C{i:04d}.MP4"
        ruta = f"/Volumes/DISCO_SINTETICO/PROYECTO_FALSO/{carpeta}/{nombre}"
        dur = round(rnd.uniform(8, 900), 2)

        def frase() -> str:
            return (f"{rnd.choice(_SUJETOS)} {rnd.choice(_ACCIONES)} "
                    f"{rnd.choice(_LUGARES)}")
        desc = ". ".join(frase() for _ in range(rnd.randint(3, 6))) + "."
        segs = []
        t = 0.0
        for _ in range(rnd.randint(1, 5)):
            s = round(t + rnd.uniform(0, 20), 2)
            e = round(min(dur, s + rnd.uniform(3, 60)), 2)
            if e <= s:
                break
            segs.append(
                "{s=" + f"{s:.2f}" + ",e=" + f"{e:.2f}"
                + ",shot=" + lua_str(rnd.choice(_PLANOS))
                + ",angle=" + lua_str(rnd.choice(_ANGULOS))
                + ",chars=" + lua_str(frase())
                + ",text=" + lua_str(frase() + " | " + frase())
                + ",is_repr=" + ("true" if rnd.random() < .3 else "false")
                + ",note=" + lua_str(f"Tramo {len(segs) + 1} de prueba") + "}")
            t = e
        preguntas = ", ".join(
            "{s=" + f"{rnd.uniform(0, dur):.2f}" + ",e=" + f"{rnd.uniform(0, dur):.2f}"
            + ",q=" + lua_str(f"Pregunta inventada numero {k}?")
            + ",r=" + lua_str(frase()) + "}"
            for k in range(rnd.randint(0, 3)))
        cam = rnd.choice(_CAMARAS)
        linea = (
            f"    [{lua_str(ruta)}] = {{name={lua_str(nombre)}, "
            f"folder={lua_str(carpeta)}, status=\"\", notes=\"\", "
            f"dur={dur:.2f}, created=\"2026-01-{1 + i % 28:02d}T12:00:00.000000Z\", "
            f"camera={lua_str(cam)}, interview={'true' if cam == 'main' else 'false'}, "
            f"category={lua_str(rnd.choice(_CATEGORIAS))}, "
            f"meta_description={lua_str(desc)}, "
            f"meta_shot={lua_str(rnd.choice(_PLANOS))}, meta_scene=\"\", "
            f"meta_keywords={lua_str(', '.join(rnd.sample(_PLANOS, 2)))}, "
            f"meta_comments=\"\", "
            f"curated_segments={{{', '.join(segs)}}}, "
            f"questions={{{preguntas}}}, "
            f"roll={lua_str(rnd.choice(('A', 'B', '')))}, "
            f"roll_score={rnd.random():.2f}}},")
        lineas.append(linea)
        total += len(linea) + 1
        byname.append((nombre, ruta))
        if cam == "main" and rnd.random() < .5:
            sync.append((ruta, f"/Volumes/DISCO_SINTETICO/PROYECTO_FALSO/"
                               f"Audio/TX1/{i:05d}.WAV"))
    lineas.append("  },")
    lineas.append("  byname = {")
    lineas += [f"    [{lua_str(n)}] = {lua_str(p)}," for n, p in byname]
    lineas.append("  },")
    lineas.append("  sync = {")
    for k, (v, a) in enumerate(sync):
        lineas.append(f"    [{lua_str(v)}] = {{audio={lua_str(Path(a).name)}, "
                      f"audiopath={lua_str(a)}, offset={-1.5 - k % 7:.3f}, "
                      f"conf=0.{50 + k % 50}}},")
    lineas.append("  },")
    lineas.append("}")
    return "\n".join(lineas) + "\n"


# --------------------------------------------------------------------------
# instalar_kit.sh / recoger_kit.sh / LEEME.txt
# --------------------------------------------------------------------------

def media_relativa(kit: Path) -> list[str]:
    """La media que el zip tiene que llevar, relativa al KIT."""
    r = rutas_kit(kit)
    return [str(r[k].relative_to(kit)) for k in ("negro", "video", "wav")]


def instalar_kit_sh(kit: Path) -> str:
    stubs = "\n".join(f"instalar_stub {shlex.quote(d)} {shlex.quote(n)}"
                      for d, n, _ in STUBS)
    revisar = "\n".join(f"revisar_stub {shlex.quote(d)} {shlex.quote(n)}"
                        for d, n, _ in STUBS)
    media = " ".join(shlex.quote(m) for m in media_relativa(kit))
    return f"""\
#!/bin/bash
# Diez50 - instala el kit de diagnostico en ESTA Mac.
# Generado por preparar_diagnostico.py: no editar.
#
# Por que existe: el kit se preparo en otra Mac y config.lua y los stubs llevan
# rutas absolutas de alla (Resolve no expande ~ ni $HOME dentro de un dofile).
# Este script las reescribe a "$HOME/Diez50-diag", crea el volumen de prueba
# (una imagen de disco: nunca un disco real) y pone los stubs en el menu de
# Resolve. Solo usa herramientas que trae macOS: nada de Python ni ffmpeg.
#
# DIEZ50_SIN_VOLUMEN=1 salta la imagen de disco (lo usan los tests).
# DIEZ50_SCRIPTS_DIR cambia la carpeta Fusion/Scripts (lo usan los tests).
set -u
# Comparaciones byte a byte: las rutas llevan acentos (Año Ñ) y espacios.
export LC_ALL=C

KIT_ORIGINAL={shlex.quote(str(kit))}
HOME_ORIGINAL={shlex.quote(str(casa()))}

KIT="$HOME/Diez50-diag"
VOLNAME={VOLNAME}
VOLUMEN="/Volumes/$VOLNAME"
DMG="$KIT/$VOLNAME.dmg"
SCRIPTS="${{DIEZ50_SCRIPTS_DIR:-$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts}}"
SCRIPTS=${{SCRIPTS%/}}
APPSUPPORT="$HOME/Library/Application Support/Diez50/diag"

falla() {{ printf 'ERROR: %s\\n' "$*" >&2; exit 1; }}

# --- 1. el kit tiene que estar en ~/Diez50-diag ------------------------------
# Las rutas nuevas se escriben como "$HOME/Diez50-diag": si el kit vive en otro
# lado, los stubs apuntarian a una carpeta que no existe.
AQUI=$(cd "$(dirname "$0")" && pwd -P) || falla "no pude leer la carpeta del kit"
[ -d "$KIT" ] || falla "No existe $KIT. Mueve la carpeta Diez50-diag a tu carpeta de inicio y vuelve a correr: bash ~/Diez50-diag/instalar_kit.sh"
KIT_REAL=$(cd "$KIT" && pwd -P)
[ "$AQUI" = "$KIT_REAL" ] || falla "Este kit esta en $AQUI y tiene que estar en $KIT. Muevelo ahi y vuelve a correr."
[ -f "$KIT/config.lua" ] || falla "Falta $KIT/config.lua: el kit esta incompleto."
case "$HOME" in *'"'*|*'\\'*) falla "Tu carpeta de inicio tiene comillas o barras invertidas: no se puede escribir en Lua.";; esac
# Sin la media, las pruebas API, LARGO y AutoSyncAudio medirian la falta de un
# archivo y no a Resolve: mejor parar aqui que perder la corrida.
for m in {media}; do
  [ -s "$KIT/$m" ] || falla "Falta $KIT/$m: el kit esta incompleto. Pidele a Victor el zip completo."
done

# --- 2. reescribir las rutas -----------------------------------------------
# Sin sed a proposito: el sed de macOS (BSD) y el de GNU no aceptan las mismas
# opciones, y una ruta con espacios, puntos o corchetes se vuelve un patron.
# Aqui el reemplazo es literal, byte a byte.
RESULTADO=""
reemplazar() {{  # texto viejo nuevo -> RESULTADO
  local resto=$1 viejo=$2 nuevo=$3 out=""
  while [ -n "$viejo" ] && [[ $resto == *"$viejo"* ]]; do
    out="$out${{resto%%"$viejo"*}}$nuevo"
    resto=${{resto#*"$viejo"}}
  done
  RESULTADO="$out$resto"
}}

reescribir() {{  # archivo
  local f=$1 txt
  # "&&" y no ";": con ";" el estado seria el de printf y un cat fallido
  # pasaria de largo, dejando config.lua con las rutas de la otra Mac.
  txt=$(cat "$f" && printf x) || falla "no pude leer $f"
  txt=${{txt%x}}                         # conserva el salto de linea final
  # Solo dentro de cadenas Lua (con la comilla delante): una ruta nueva nunca
  # vuelve a casar con la vieja, asi que correr esto dos veces no hace nada.
  reemplazar "$txt" "\\"$KIT_ORIGINAL" "\\"$KIT"
  reemplazar "$RESULTADO" "\\"$HOME_ORIGINAL/Library/" "\\"$HOME/Library/"
  if [ "$RESULTADO" != "$txt" ]; then
    printf '%s' "$RESULTADO" > "$f" || falla "no pude escribir $f"
  fi
}}

REESCRITOS=("$KIT/config.lua")
[ -f "$KIT/linea_consola.txt" ] && REESCRITOS+=("$KIT/linea_consola.txt")
while IFS= read -r -d '' f; do
  REESCRITOS+=("$f")
done < <(find "$KIT/stubs" -type f -name '{PREFIJO_STUB}*.lua' -print0)
for f in "${{REESCRITOS[@]}}"; do
  reescribir "$f"
done
# Comprobacion final: que ya no quede ninguna ruta de la otra Mac. Si la ruta
# nueva contiene a la vieja (misma Mac, mismo HOME), no hay nada que buscar.
case "\\"$KIT" in
  "\\"$KIT_ORIGINAL"*) ;;
  *) for f in "${{REESCRITOS[@]}}"; do
       ! grep -Fq "\\"$KIT_ORIGINAL" "$f" || falla "$f sigue apuntando a $KIT_ORIGINAL"
     done;;
esac
echo "Rutas del kit apuntan a $KIT"

# --- 3. el volumen de prueba (imagen de disco) -----------------------------
ruta_real() {{
  (cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$1")")
}}

# Imprime los puntos de montaje de ESA imagen, uno por linea. Lee
# `hdiutil info -plist` con plutil (viene con macOS) en vez de adivinar por el
# nombre del volumen: un disco real puede llamarse igual.
montajes_de_imagen() {{
  local dmg_real info i j ruta mp
  dmg_real=$(ruta_real "$1")
  info=$(mktemp "${{TMPDIR:-/tmp}}/diez50diag.XXXXXX") || return 1
  hdiutil info -plist > "$info" || {{ rm -f "$info"; return 1; }}
  i=0
  while ruta=$(plutil -extract "images.$i.image-path" raw -o - "$info" 2>/dev/null); do
    if [ "$(ruta_real "$ruta")" = "$dmg_real" ]; then
      j=0
      while plutil -extract "images.$i.system-entities.$j" xml1 -o /dev/null "$info" 2>/dev/null; do
        mp=$(plutil -extract "images.$i.system-entities.$j.mount-point" raw -o - "$info" 2>/dev/null) \\
          && printf '%s\\n' "$mp"
        j=$((j + 1))
      done
    fi
    i=$((i + 1))
  done
  rm -f "$info"
}}

if [ "${{DIEZ50_SIN_VOLUMEN:-0}}" = 1 ]; then
  echo "Sin volumen de prueba (DIEZ50_SIN_VOLUMEN=1)."
else
  if [ ! -f "$DMG" ]; then
    hdiutil create -size 20m -fs APFS -volname "$VOLNAME" "$DMG" >/dev/null \\
      || falla "no pude crear $DMG"
  fi
  MONTAJES=$(montajes_de_imagen "$DMG")
  if [ -e "$VOLUMEN" ] && ! printf '%s\\n' "$MONTAJES" | grep -Fxq "$VOLUMEN"; then
    falla "Ya existe $VOLUMEN y NO es la imagen del kit. No se escribe nada ahi. Expulsa ese volumen o avisale a Victor."
  fi
  if [ -z "$MONTAJES" ]; then
    hdiutil attach "$DMG" >/dev/null || falla "no pude montar $DMG"
    MONTAJES=$(montajes_de_imagen "$DMG")
  fi
  printf '%s\\n' "$MONTAJES" | grep -Fxq "$VOLUMEN" \\
    || falla "La imagen se monto en '$MONTAJES' y no en $VOLUMEN. Expulsala y vuelve a correr."
  mkdir -p "$VOLUMEN/Diez50-diag/recibos" || falla "no pude escribir en $VOLUMEN"
  printf 'return {{ suma = 12345 }}\\n' > "$VOLUMEN/Diez50-diag/fixture_ok.lua" \\
    || falla "no pude escribir $VOLUMEN/Diez50-diag/fixture_ok.lua"
  echo "Volumen de prueba listo en $VOLUMEN"
fi

# --- 4. el fixture de Application Support ----------------------------------
mkdir -p "$APPSUPPORT" || falla "no pude crear $APPSUPPORT"
printf 'return {{ suma = 12345 }}\\n' > "$APPSUPPORT/fixture_ok.lua" \\
  || falla "no pude escribir $APPSUPPORT/fixture_ok.lua"

# --- 5. los stubs del menu (solo archivos {PREFIJO_STUB}*) --------------------------
# Nunca se toca otro script: en esas carpetas viven scripts ajenos. Y como
# macOS no distingue mayusculas, un "diez50 diagnostico.lua" ajeno ES nuestro
# destino: solo se pisa si su primera linea dice que lo genero el kit.
MARCA={shlex.quote(MARCA_GENERADO)}
minusculas() {{ printf '%s' "$1" | tr 'A-Z' 'a-z'; }}

# El nombre con el que el archivo esta de verdad en disco (APFS no distingue
# mayusculas: "diez50 diagnostico edit.lua" responde a nuestro nombre, y es el
# que la persona tiene que buscar en el Finder).
nombre_en_disco() {{  # carpeta nombre
  local e buscado
  buscado=$(minusculas "$2")
  for e in "$1"/* "$1"/.[!.]*; do
    [ -e "$e" ] || [ -L "$e" ] || continue
    if [ "$(minusculas "$(basename "$e")")" = "$buscado" ]; then
      basename "$e"; return 0
    fi
  done
  printf '%s' "$2"
}}

# Primero se revisan los 4 destinos y despues se copia: si uno es ajeno, no
# queda un menu a medias.
revisar_stub() {{  # carpeta nombre
  case "$2" in {PREFIJO_STUB}*) ;; *) falla "stub con nombre ajeno: $2";; esac
  local dir="$SCRIPTS/$1" destino="$SCRIPTS/$1/$2" p="$SCRIPTS/$1"
  while [ "$p" != "$SCRIPTS" ] && [ "$p" != / ] && [ "$p" != . ]; do
    if [ -e "$p" ] && [ ! -d "$p" ]; then falla "$p existe y no es una carpeta: no se toca."; fi
    p=$(dirname "$p")
  done
  if [ -L "$destino" ]; then
    falla "$dir/$(nombre_en_disco "$dir" "$2") es un enlace simbolico: no se toca"
  fi
  if [ -e "$destino" ] && [ ! -f "$destino" ]; then
    falla "$dir/$(nombre_en_disco "$dir" "$2") existe y no es un archivo: no se toca"
  fi
  if [ -e "$destino" ] && ! head -n 1 "$destino" | grep -Fq "$MARCA"; then
    falla "$dir/$(nombre_en_disco "$dir" "$2") ya existe y no lo genero el kit. No se pisa: cambiale el nombre y vuelve a correr."
  fi
  [ -s "$KIT/stubs/$1/$2" ] || falla "Falta $KIT/stubs/$1/$2: el kit esta incompleto."
}}

instalar_stub() {{  # carpeta nombre
  mkdir -p "$SCRIPTS/$1" || falla "no pude crear $SCRIPTS/$1"
  cp "$KIT/stubs/$1/$2" "$SCRIPTS/$1/$2" || falla "no pude copiar $2"
}}
{revisar}
{stubs}
echo "Stubs instalados en $SCRIPTS"

echo
echo "Listo. Reinicia DaVinci Resolve para que vea los scripts."
echo "Al terminar, bash $KIT/quitar_kit.sh los quita del menu."
echo "Linea para la Consola (Workspace > Console, modo Lua); tambien esta en $KIT/linea_consola.txt:"
echo
cat "$KIT/linea_consola.txt" || falla "no pude leer $KIT/linea_consola.txt"
echo
"""


def quitar_kit_sh() -> str:
    quitar = "\n".join(f"quitar_stub {shlex.quote(d)} {shlex.quote(n)}"
                       for d, n, _ in STUBS)
    return f"""\
#!/bin/bash
# Diez50 - quita del menu de Resolve los scripts del diagnostico.
# Generado por preparar_diagnostico.py: no editar.
#
# Por que existe: los stubs viven en la carpeta de scripts de Resolve, fuera
# del kit. Si alguien borra ~/Diez50-diag sin correr esto, el menu sigue
# mostrando "Diez50 Diagnostico" apuntando a nada. Borra EXACTAMENTE los 4
# stubs, y solo si su primera linea dice que los genero el kit (macOS no
# distingue mayusculas: un script ajeno con un nombre parecido se deja).
# Tambien quita el fixture_ok.lua de Application Support/Diez50/diag si es
# identico al del kit. No toca el kit, ni la base de datos, ni el volumen.
set -u
export LC_ALL=C

SCRIPTS="${{DIEZ50_SCRIPTS_DIR:-$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts}}"
MARCA={shlex.quote(MARCA_GENERADO)}

falla() {{ printf 'ERROR: %s\\n' "$*" >&2; exit 1; }}

quitar_stub() {{  # carpeta nombre
  case "$2" in {PREFIJO_STUB}*) ;; *) falla "stub con nombre ajeno: $2";; esac
  local f="$SCRIPTS/$1/$2"
  if [ -L "$f" ]; then echo "AVISO: $f es un enlace simbolico: no se toca"; return 0; fi
  [ -f "$f" ] || return 0
  if head -n 1 "$f" | grep -Fq "$MARCA"; then
    rm -f "$f" || falla "no pude quitar $f"
    echo "Quitado: $f"
  else
    echo "AVISO: $f no lo genero el kit: no se toca"
  fi
}}
{quitar}

# La subcarpeta Utility/{PREFIJO_STUB} es del kit, pero solo se quita si quedo vacia.
SUB="$SCRIPTS/Utility/{PREFIJO_STUB}"
if [ -d "$SUB" ] && [ ! -L "$SUB" ]; then
  if rmdir "$SUB" 2>/dev/null; then
    echo "Quitada la carpeta vacia $SUB"
  else
    echo "AVISO: $SUB tiene otros archivos: se deja"
  fi
fi

# El fixture de Application Support: solo si es EXACTAMENTE el que escribio el
# kit. Ahi mismo va a vivir el buzon del plugin, asi que las carpetas se
# quitan solo si quedan vacias (rmdir no borra nada que tenga contenido).
DIAG_AS="$HOME/Library/Application Support/Diez50/diag"
F="$DIAG_AS/fixture_ok.lua"
if [ -f "$F" ] && [ ! -L "$F" ]; then
  if printf 'return {{ suma = 12345 }}\\n' | cmp -s - "$F"; then
    rm -f "$F" && echo "Quitado: $F"
  else
    echo "AVISO: $F no es el del kit: no se toca"
  fi
fi
rmdir "$DIAG_AS" 2>/dev/null && echo "Quitada la carpeta vacia $DIAG_AS"
rmdir "$(dirname "$DIAG_AS")" 2>/dev/null && echo "Quitada la carpeta vacia $(dirname "$DIAG_AS")"
echo
echo "Listo. Reinicia DaVinci Resolve para que el menu se actualice."
echo "Ahora expulsa el disco DIEZ50DIAG (en el Finder, el boton de expulsar"
echo "junto a su nombre) y despues puedes borrar la carpeta ~/Diez50-diag."
exit 0
"""


def recoger_kit_sh() -> str:
    return f"""\
#!/bin/bash
# Diez50 - junta los resultados del diagnostico en un zip en el Escritorio.
# Generado por preparar_diagnostico.py: no editar.
#
# Todo se COPIA: nunca se mueve ni se borra nada de Resolve ni del kit. Lo
# unico que se borra al final es la carpeta temporal donde se armo el zip.
#
# Lo que viaja, y nada mas: el Project.db de cada proyecto cuyo nombre empieza
# con DIEZ50_DIAG, los recibos y casillas del kit, los recibos del volumen de
# prueba, config.lua, el Fusion.prefs mas reciente, activedb.conf (el nombre de
# la base abierta) y maquina.txt. NO viaja dblist.conf: lista nombres y rutas
# de todas las bases de la persona.
#
# DIEZ50_VOLUMEN_DIAG cambia el volumen de prueba que se lee (lo usan los tests).
set -u

KIT="$HOME/Diez50-diag"
PROYECTO={PROYECTO_REQUERIDO}
PREFS="$HOME/Library/Preferences/Blackmagic Design/DaVinci Resolve"
SOPORTE="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve"
APP="/Applications/DaVinci Resolve/DaVinci Resolve.app"

falla() {{ printf 'ERROR: %s\\n' "$*" >&2; exit 1; }}
limpio() {{ printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }}

HOST=$(scutil --get LocalHostName 2>/dev/null || hostname -s 2>/dev/null || echo mac)
HOST=$(limpio "$HOST")
[ -n "$HOST" ] || HOST=mac
SALIDA="$HOME/Desktop/Diez50-diag-resultado-$HOST.zip"
TMP=$(mktemp -d "${{TMPDIR:-/tmp}}/diez50diag.XXXXXX") || falla "no pude crear un temporal"
DEST="$TMP/Diez50-diag-resultado-$HOST"
mkdir -p "$DEST" || falla "no pude crear $DEST"

# --- 1. el Project.db del proyecto DIEZ50_DIAG ------------------------------
# Igual que lib/timeline_resolve.py: activedb.conf dice que base esta abierta
# ("disk*:NOMBRE") y dblist.conf da su ruta ("NOMBRE:RUTA:...:DISK"). Se
# buscan TODAS las bases de disco por si el proyecto quedo en otra. Se copia
# cada proyecto cuyo nombre EMPIEZA con DIEZ50_DIAG: es el mismo criterio con el
# que el diagnostico y los stubs deciden actuar, asi que un "DIEZ50_DIAG 2" en
# el que si corrio el diagnostico no se queda fuera del zip. Cada uno se nombra
# en pantalla, para que la persona vea que se lleva.
ACTIVA=""
if [ -f "$PREFS/activedb.conf" ]; then
  ACTIVA=$(head -n 1 "$PREFS/activedb.conf")
  ACTIVA=${{ACTIVA#*:}}
fi
mkdir -p "$DEST/resolve_prefs"
[ -f "$PREFS/activedb.conf" ] && cp -p "$PREFS/activedb.conf" "$DEST/resolve_prefs/activedb.conf"
printf '%s\\n' "$ACTIVA" > "$DEST/resolve_prefs/base_activa.txt"

N=0
if [ -f "$PREFS/dblist.conf" ]; then
  while IFS= read -r linea || [ -n "$linea" ]; do
    nombre=${{linea%%:*}}
    resto=${{linea#*:}}
    ruta=${{resto%%:*}}
    tipo=$(printf '%s' "${{linea##*:}}" | tr -d '\\r' | tr 'a-z' 'A-Z')
    [ -n "$nombre" ] && [ "$tipo" = DISK ] || continue
    case "$ruta" in
      '$HOME'*) ruta="$HOME${{ruta#'$HOME'}}";;
      '~'*) ruta="$HOME${{ruta#'~'}}";;
    esac
    proyectos="$ruta/Resolve Projects/Users/guest/Projects"
    [ -d "$proyectos" ] || continue
    for d in "$proyectos/$PROYECTO"*; do
      [ -f "$d/Project.db" ] || continue
      destino="$DEST/project_db/$(limpio "$nombre")/$(limpio "$(basename "$d")")"
      mkdir -p "$destino"
      # El -wal y el -shm llevan lo que Resolve aun no paso al .db.
      for ext in "" -wal -shm; do
        [ -f "$d/Project.db$ext" ] && cp -p "$d/Project.db$ext" "$destino/Project.db$ext"
      done
      echo "Copio el proyecto $(basename "$d") de la base $nombre"
      N=$((N + 1))
    done
  done < "$PREFS/dblist.conf"
fi
[ "$N" -gt 0 ] || echo "AVISO: no encontre ningun proyecto cuyo nombre empiece con $PROYECTO en las bases de disco."

# --- 2. lo que dejo el diagnostico en el kit --------------------------------
for c in recibos manual; do
  if [ -d "$KIT/$c" ]; then cp -R "$KIT/$c" "$DEST/$c"; else echo "AVISO: falta $KIT/$c"; fi
done
[ -f "$KIT/config.lua" ] && cp -p "$KIT/config.lua" "$DEST/config.lua"

# --- 2b. los recibos del volumen de prueba ----------------------------------
# La mitad de los exportes (EXP) van a {VOLUMEN}/Diez50-diag/recibos. Sin
# ellos, el lector no puede confirmar ninguno y esas pruebas salen SIN
# CONFIRMAR. Solo se LEE del volumen. Si no esta montado (la Mac se reinicio,
# alguien lo expulso), se monta la imagen del kit en SOLO LECTURA, en una
# carpeta temporal y sin que aparezca en el Finder, y se expulsa al terminar.
VOLUMEN="${{DIEZ50_VOLUMEN_DIAG:-{VOLUMEN}}}"
DMG="$KIT/{NOMBRE_DMG}"
MONTAJE=""
if [ ! -d "$VOLUMEN/Diez50-diag/recibos" ] && [ -f "$DMG" ]; then
  MONTAJE=$(mktemp -d "${{TMPDIR:-/tmp}}/diez50vol.XXXXXX") || MONTAJE=""
  if [ -n "$MONTAJE" ] && hdiutil attach -readonly -nobrowse -noautoopen \
       -mountpoint "$MONTAJE" "$DMG" >/dev/null 2>&1; then
    VOLUMEN=$MONTAJE
  else
    [ -n "$MONTAJE" ] && rmdir "$MONTAJE" 2>/dev/null
    MONTAJE=""
  fi
fi
if [ -d "$VOLUMEN/Diez50-diag/recibos" ]; then
  if cp -R "$VOLUMEN/Diez50-diag/recibos" "$DEST/recibos_volumen"; then
    echo "Copio los recibos del volumen de prueba"
  else
    echo "AVISO: no pude copiar los recibos del volumen de prueba"
  fi
else
  echo "AVISO: no encontre los recibos del volumen de prueba ({VOLUMEN}/Diez50-diag/recibos)."
  echo "       Las pruebas de exportar al volumen quedaran sin confirmar."
fi
if [ -n "$MONTAJE" ]; then
  # Nunca rm -rf aqui: si la expulsion falla, dentro sigue la imagen montada.
  if hdiutil detach "$MONTAJE" >/dev/null 2>&1; then
    rmdir "$MONTAJE" 2>/dev/null
  else
    echo "AVISO: no pude expulsar la imagen montada en $MONTAJE (solo lectura)."
  fi
fi

# --- 3. Fusion.prefs, el mas reciente ---------------------------------------
MAS_RECIENTE=""
T_MAX=0
while IFS= read -r -d '' f; do
  t=$(stat -f %m "$f" 2>/dev/null || echo 0)
  if [ "$t" -gt "$T_MAX" ]; then T_MAX=$t; MAS_RECIENTE=$f; fi
done < <(find "$SOPORTE" -name Fusion.prefs -type f -print0 2>/dev/null)
if [ -n "$MAS_RECIENTE" ]; then
  cp -p "$MAS_RECIENTE" "$DEST/Fusion.prefs"
  printf '%s\\n' "$MAS_RECIENTE" > "$DEST/Fusion.prefs.origen.txt"
else
  echo "AVISO: no encontre Fusion.prefs"
fi

# --- 4. la maquina y la version de Resolve ----------------------------------
{{
  echo "== sw_vers"; sw_vers 2>/dev/null
  echo "== modelo"; sysctl -n hw.model 2>/dev/null
  echo "== cpu"; sysctl -n machdep.cpu.brand_string 2>/dev/null
  echo "== memoria_bytes"; sysctl -n hw.memsize 2>/dev/null
  echo "== resolve"
  if [ -f "$APP/Contents/Info.plist" ]; then
    for k in CFBundleName CFBundleShortVersionString CFBundleVersion CFBundleGetInfoString; do
      printf '%s=%s\\n' "$k" "$(plutil -extract "$k" raw -o - "$APP/Contents/Info.plist" 2>/dev/null)"
    done
  else
    echo "no esta $APP"
  fi
}} > "$DEST/maquina.txt"

# --- 5. el zip ---------------------------------------------------------------
mkdir -p "$HOME/Desktop" || falla "no pude crear el Escritorio"
# --norsrc --noextattr --noacl: sin ellos ditto mete un "._X" AppleDouble junto
# a cada archivo (casi todo en macOS lleva el xattr com.apple.provenance), y el
# lector encontraria "._Project.db" y "._r1.json" que no son SQLite ni JSON.
ditto -c -k --norsrc --noextattr --noacl --keepParent "$DEST" "$SALIDA" \\
  || falla "no pude crear $SALIDA"
case "$TMP" in
  */diez50diag.*) rm -rf "$TMP";;
esac
echo
echo "Listo: $SALIDA"
echo "Proyectos $PROYECTO encontrados: $N"
echo "Manda ese zip a Victor."
"""


def leeme_txt() -> str:
    return """\
DIEZ50 - KIT DE DIAGNOSTICO PARA DAVINCI RESOLVE
================================================

Para que sirve: medir que puede hacer un script dentro de DaVinci Resolve en
esta Mac. No toca tu material ni tus proyectos: trabaja en una base de datos y
un proyecto nuevos. Al terminar, el paso 13 quita los scripts del menu; despues
se puede borrar todo. Toma unos 30 minutos.

ANTES DE EMPEZAR
1. Si vas a instalar o actualizar Resolve para esto, RESPALDA ANTES tus bases
   de datos (Project Manager, clic derecho sobre la base > Backup). Un
   proyecto que se abre en la version nueva ya no abre en la anterior.
2. Mueve la carpeta Diez50-diag a tu carpeta de inicio (la de la casita, con
   tu nombre). Tiene que quedar asi: ~/Diez50-diag

PASOS
1. Abre DaVinci Resolve. En el Project Manager crea una base de datos NUEVA
   llamada Diez50-Diag. Dentro de ella crea un proyecto llamado exactamente
   DIEZ50_DIAG (en mayusculas, con guion bajo). Cierra Resolve.
2. Abre Terminal (Aplicaciones > Utilidades > Terminal), escribe esto y dale
   Enter:
       bash ~/Diez50-diag/instalar_kit.sh
   Al final imprime una linea larga que empieza con dofile. Tambien queda en
   el archivo ~/Diez50-diag/linea_consola.txt.
   OJO: en el Escritorio aparece un disco llamado DIEZ50DIAG. Es un disco de
   prueba vacio que crea el kit. NO lo expulses hasta el paso 13: el
   diagnostico lee y escribe en el.
3. Abre Resolve de nuevo y abre el proyecto DIEZ50_DIAG.
4. Menu Workspace > Scripts > Diez50 Diagnostico. Correlo DOS veces. Cada vez
   espera a que aparezca una timeline que empieza con "DIAG RESUMEN" (puede
   tardar un par de minutos).
5. Workspace > Scripts > Diez50 > Diez50 Diagnostico sub. UNA vez.
6. Ve a la pagina Edit y corre Workspace > Scripts > Diez50 Diagnostico edit
   (puede estar dentro de un submenu Edit). UNA vez.
7. Workspace > Scripts > Diez50 Diagnostico error. UNA vez. Este falla a
   proposito: fijate si Resolve muestra algun mensaje.
8. Workspace > Console. Arriba elige Lua. Copia la linea de
   ~/Diez50-diag/linea_consola.txt, pegala abajo y dale Enter. UNA vez.
   Fijate si aparecen lineas que empiezan con DIEZ50DIAG.
9. En el Media Pool hay varias timelines que empiezan con "DIAG MARCAS".
   Busca la que TERMINA en "menu_utility-r1" (es la de la primera corrida
   del paso 4). Seleccionala y usa File > Export > Timeline, formato .drt, y
   guardala en la carpeta ~/Diez50-diag/manual/
10. Abre ~/Diez50-diag/manual/casillas.txt y contesta si o no a las 4
    preguntas: si aparecio el menu, si aparecio el submenu Diez50, si se
    vieron lineas DIEZ50DIAG en la Consola y si hubo mensaje con el script de
    error.
11. Guarda el proyecto (Cmd+S) y cierra Resolve.
12. En Terminal:
        bash ~/Diez50-diag/recoger_kit.sh
    Deja en tu Escritorio un archivo Diez50-diag-resultado-<tu Mac>.zip.
    Mandaselo a Victor.
13. Para limpiar, en ESTE orden:
    a. En Terminal:
           bash ~/Diez50-diag/quitar_kit.sh
       Quita los 4 scripts del diagnostico del menu de Resolve y el archivo
       de prueba que el kit dejo en ~/Library/Application Support/Diez50.
    b. Expulsa el disco DIEZ50DIAG (en el Finder, el boton de expulsar junto
       a su nombre).
    c. Borra la carpeta ~/Diez50-diag.
    d. Si quieres, borra la base Diez50-Diag en el Project Manager.
    Si borras la carpeta antes del paso a, los scripts se quedan en el menu
    apuntando a nada. Si la borras antes del paso b, el disco de prueba se
    queda montado sin su archivo.

SI ALGO NO PASA
- Si en el paso 4 pasan 3 minutos y no aparece ninguna timeline "DIAG
  RESUMEN": revisa el nombre del proyecto abierto (arriba, en el centro de
  la ventana de Resolve). Tiene que empezar con DIEZ50_DIAG, en mayusculas.
  Si se llama distinto, el diagnostico no hace nada a proposito, para no
  tocar un proyecto tuyo. Renombralo en el Project Manager y vuelve al paso
  4. Anota en casillas.txt lo que paso.
- Si en el menu Workspace > Scripts no aparece nada de Diez50: cierra y
  vuelve a abrir Resolve. Si sigue sin aparecer, anota menu=no en
  casillas.txt y sigue con el paso 8 (la Consola).
- Si instalar_kit.sh termina con una linea que empieza con ERROR, no sigas:
  mandale a Victor lo que dice esa linea.

QUE LLEVA EL ZIP DEL PASO 12
- El proyecto DIEZ50_DIAG (y cualquier otro cuyo nombre empiece con
  DIEZ50_DIAG; ningun otro proyecto tuyo).
- Lo que dejo el diagnostico en ~/Diez50-diag (recibos, casillas.txt y el
  .drt del paso 9) y config.lua.
- Lo que el diagnostico exporto al disco de prueba DIEZ50DIAG (su carpeta
  Diez50-diag/recibos).
- Fusion.prefs: las preferencias de Fusion, donde el diagnostico escribe un
  resultado. Pueden incluir rutas de archivos recientes.
- El nombre de la base de datos que estaba abierta.
- maquina.txt: modelo de Mac, version de macOS, procesador, memoria y version
  de Resolve. Sin numero de serie.
Nada de tu material.

Si algo sale distinto a lo que dice aqui, no pasa nada: anotalo en
casillas.txt. Lo que falla tambien es un resultado.
"""


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------

def preparar_media(esc: Escritor, kit: Path) -> None:
    r = rutas_kit(kit)
    esc.carpeta(r["negro"].parent)
    if not (r["negro"].is_file() and r["negro"].stat().st_size > 0):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            if not esc.dry_run:
                raise Aborto("ffmpeg no esta en el PATH: hace falta para el "
                             "clip negro (o usa --sin-media).")
            ffmpeg = "ffmpeg"
        # Se escribe a un .part y se renombra al final: si ffmpeg se corta a la
        # mitad, la siguiente corrida no confunde un archivo trunco con uno listo.
        parcial = r["negro"].with_name("negro_10min.part.mov")
        esc._vigilar(parcial)
        cmd = [ffmpeg, "-y", "-v", "error",
               "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=24:d=600",
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-t", "600", "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "51",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k",
               str(parcial)]
        res = esc.correr(cmd, capturar=True)
        if res is not None:
            if res.returncode != 0:
                raise Aborto(f"ffmpeg fallo con el negro:\n{res.stderr}")
            os.replace(parcial, r["negro"])
    if not (r["video"].is_file() and r["wav"].is_file()):
        # El generador escribe por su cuenta: la guarda se aplica a su carpeta.
        esc._vigilar(r["par"])
        res = esc.correr([sys.executable, str(GENERADOR_PAR),
                          "--dir", str(r["par"])])
        if res is not None and res.returncode != 0:
            raise Aborto("generar_media_prueba.py fallo (mira la salida de arriba).")
        if res is not None and not (r["video"].is_file() and r["wav"].is_file()):
            raise Aborto(f"generar_media_prueba.py no dejo {r['video'].name} y "
                         f"{r['wav'].name} en {r['par']}.")


# --------------------------------------------------------------------------
# volumen de prueba
# --------------------------------------------------------------------------

def _real(p) -> str:
    return os.path.realpath(str(p))


def estado_volumen(info: dict, dmg: Path, volumen_existe: bool,
                   volumen: Path = VOLUMEN) -> str:
    """Decide, a partir de `hdiutil info -plist` ya parseado, si se puede
    escribir en el volumen. Funcion pura: los tests la ejercitan sin hdiutil.

      "nuestra"      la imagen del kit esta montada justo en `volumen`
      "sin_montar"   la imagen no esta adjunta y `volumen` esta libre
      "ajeno"        `volumen` existe y NO es la imagen del kit: no se toca
      "otro_lugar"   la imagen esta montada, pero en otro punto
    """
    montajes: list[str] = []
    adjunta = False
    for img in info.get("images", []) or []:
        if _real(img.get("image-path", "")) != _real(dmg):
            continue
        adjunta = True
        for ent in img.get("system-entities", []) or []:
            mp = ent.get("mount-point")
            if mp:
                montajes.append(mp)
    if str(volumen) in montajes:
        return "nuestra"
    if volumen_existe:
        return "ajeno"
    if adjunta and montajes:
        return "otro_lugar"
    return "sin_montar"


def _hdiutil_info() -> dict:
    r = subprocess.run([shutil.which("hdiutil") or "hdiutil", "info", "-plist"],
                       capture_output=True)
    if r.returncode != 0:
        raise Aborto(f"hdiutil info fallo: {r.stderr.decode(errors='replace')}")
    return plistlib.loads(r.stdout)


def preparar_volumen(esc: Escritor, kit: Path) -> None:
    dmg = rutas_kit(kit)["dmg"]
    hdiutil = shutil.which("hdiutil")
    if not hdiutil:
        if not esc.dry_run:
            raise Aborto("hdiutil no esta en el PATH (es de macOS). Usa "
                         "--sin-volumen si esta maquina no lo tiene.")
        hdiutil = "hdiutil"
    if not dmg.exists():
        res = esc.correr([hdiutil, "create", "-size", "20m", "-fs", "APFS",
                          "-volname", VOLNAME, str(dmg)], capturar=True)
        if res is not None and res.returncode != 0:
            raise Aborto(f"hdiutil create fallo:\n{res.stderr}")

    destino_fix = VOLUMEN / "Diez50-diag" / "fixture_ok.lua"
    destino_rec = VOLUMEN / "Diez50-diag" / "recibos"
    if esc.dry_run:
        print(f"[dry-run] ejecutaria: {hdiutil} info -plist  (verifica que "
              f"{VOLUMEN} sea {dmg})")
        print(f"[dry-run] ejecutaria: {hdiutil} attach {shlex.quote(str(dmg))}  "
              "(solo si no esta montada)")
        print(f"[dry-run] crearia carpeta {destino_rec}")
        print(f"[dry-run] escribiria {destino_fix}")
        return

    estado = estado_volumen(_hdiutil_info(), dmg, VOLUMEN.exists())
    if estado == "ajeno":
        raise Aborto(f"{VOLUMEN} existe y NO es {dmg}. No se escribe nada ahi: "
                     "podria ser un disco con material. Expulsalo o usa "
                     "--sin-volumen.")
    if estado == "otro_lugar":
        raise Aborto(f"{dmg} esta montada, pero no en {VOLUMEN}. Expulsala "
                     "(hdiutil detach) y vuelve a correr.")
    if estado == "sin_montar":
        res = esc.correr([hdiutil, "attach", str(dmg)], capturar=True)
        if res is not None and res.returncode != 0:
            raise Aborto(f"hdiutil attach fallo:\n{res.stderr}")
        # Se vuelve a leer: attach monta donde puede, y si el nombre estaba
        # tomado acaba en "/Volumes/DIEZ50DIAG 1". Solo cuenta lo verificado.
        estado = estado_volumen(_hdiutil_info(), dmg, VOLUMEN.exists())
    if estado != "nuestra":
        raise Aborto(f"No pude verificar que {VOLUMEN} sea {dmg} "
                     f"(estado: {estado}). No se escribe nada en el volumen.")
    esc.volumen_verificado = True
    esc.dev_volumen = os.stat(VOLUMEN).st_dev
    esc.carpeta(destino_rec)
    esc.escribir(destino_fix, FIXTURE_OK)


# --------------------------------------------------------------------------
# stubs en Fusion/Scripts
# --------------------------------------------------------------------------

def destinos_stubs(scripts: Path) -> list[tuple[Path, Path]]:
    """(archivo del kit relativo a stubs/, destino en Fusion/Scripts)."""
    out = []
    for carpeta, nombre, _ in STUBS:
        assert nombre.startswith(PREFIJO_STUB), nombre
        out.append((Path(carpeta) / nombre, scripts / carpeta / nombre))
    return out


def instalar_stubs(esc: Escritor, kit: Path, scripts: Path) -> None:
    stubs = rutas_kit(kit)["stubs"]
    # Primero se revisan los 4 destinos y despues se escribe: si uno es ajeno,
    # no queda un menu a medias.
    for _, destino in destinos_stubs(scripts):
        # Una carpeta del camino que en realidad es un archivo (un
        # "Utility/Diez50" ajeno) haria tronar el mkdir con un stub ya puesto.
        p = destino.parent
        while p != scripts and scripts in p.parents:
            if p.exists() and not p.is_dir():
                raise Aborto(f"{p} existe y no es una carpeta: no se toca.")
            p = p.parent
        if destino.exists() and not destino.is_file():
            raise Aborto(f"{destino} existe y no es un archivo: no se toca.")
        if destino.is_file() and not es_generado_por_el_kit(destino):
            # En APFS esto incluye un nombre que solo difiere en mayusculas.
            raise Aborto(f"{destino} ya existe y no lo genero el kit (su "
                         "primera linea no lleva la marca). No se pisa: "
                         "cambiale el nombre y vuelve a correr.")
    for rel, destino in destinos_stubs(scripts):
        esc.carpeta(destino.parent)
        esc.copiar(stubs / rel, destino)


def quitar_stubs(esc: Escritor, scripts: Path) -> None:
    borrados: set[Path] = set()
    for _, destino in destinos_stubs(scripts):
        if destino.is_symlink() or not destino.is_file():
            continue
        if not es_generado_por_el_kit(destino):
            print(f"AVISO: {destino} no lo genero el kit: no se toca.")
            continue
        esc.borrar_archivo(destino)
        borrados.add(destino)
    # La subcarpeta es nuestra, pero alguien pudo dejar algo dentro: solo se
    # quita si no queda NADA (ni un .DS_Store) aparte de lo que acabamos de
    # borrar. En dry-run los stubs siguen ahi y se descuentan a mano.
    sub = scripts / "Utility" / PREFIJO_STUB
    if sub.is_dir() and not sub.is_symlink():
        if not [p for p in sub.iterdir() if p not in borrados]:
            esc.borrar_carpeta_vacia(sub)


# --------------------------------------------------------------------------
# zip para otra Mac
# --------------------------------------------------------------------------

def archivos_del_zip(kit: Path) -> list[Path]:
    """La lista blanca del zip: exactamente estos archivos y nada mas.

    Por que una lista y no recorrer la carpeta: el zip viaja a la Mac de otra
    persona. Si alguien dejo en el kit un archivo propio (o la .dmg, o los
    recibos de esta Mac), recorrer la carpeta lo mandaria sin que nadie lo
    note. Tambien es lo que el zip no puede no llevar: en la otra Mac no hay
    ffmpeg ni Python para regenerarlo, y sin la media las pruebas API, LARGO y
    AutoSyncAudio medirian la falta de un archivo y no a Resolve."""
    r = rutas_kit(kit)
    lista = [r[k] for k in ("sello_kit", "config", "diagnostico", "negro",
                            "video", "wav", "ok", "error", "acentos", "grande",
                            "linea", "instalar", "recoger", "quitar", "leeme")]
    lista += [r["stubs"] / rel for rel, _ in destinos_stubs(Path("/"))]
    return lista


def faltantes_para_zip(kit: Path) -> list[Path]:
    return [p for p in archivos_del_zip(kit)
            if not (p.is_file() and p.stat().st_size > 0)]


def empaquetar(esc: Escritor, kit: Path, salida: Path, sello: str) -> None:
    """Zip del kit para otra Mac.

    config.lua viaja SIEMPRE con las rutas de /Volumes/DIEZ50DIAG, aunque
    aqui se haya preparado con --sin-volumen: instalar_kit.sh crea y monta la
    imagen alla, y con volumen = nil las pruebas LOAD y EXP hacia un volumen
    se saltarian sin que nadie lo note.
    """
    esc._vigilar(salida)
    if esc.dry_run:
        print("[dry-run] verificaria que el kit este completo (media incluida)")
        print(f"[dry-run] escribiria {salida}  (zip del kit sin la .dmg ni "
              "los resultados de esta Mac)")
        return
    faltan = faltantes_para_zip(kit)
    if faltan:
        raise Aborto("El kit esta incompleto y no se empaqueta (en la otra Mac "
                     "no hay como regenerarlo). Falta:\n  "
                     + "\n  ".join(str(p) for p in faltan)
                     + "\nCorre el preparador sin --sin-media.")
    config_zip = config_lua(kit, sello, con_volumen=True).encode("utf-8")
    raiz = "Diez50-diag"
    salida.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(salida.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            # Las carpetas de resultados viajan vacias: el diagnostico y
            # recoger_kit.sh las esperan alla.
            for c in CARPETAS_DE_RESULTADOS:
                z.writestr(f"{raiz}/{c}/", "")
            config = rutas_kit(kit)["config"]
            for p in archivos_del_zip(kit):
                rel = p.relative_to(kit).as_posix()
                info = zipfile.ZipInfo.from_file(p, f"{raiz}/{rel}")
                info.compress_type = zipfile.ZIP_DEFLATED
                if p.name.endswith(".sh"):
                    info.external_attr = 0o100755 << 16
                if p == config:
                    z.writestr(info, config_zip)
                    continue
                with open(p, "rb") as fh:
                    z.writestr(info, fh.read())
            # Plantilla de casillas limpia: la de esta Mac puede estar llena.
            z.writestr(f"{raiz}/manual/casillas.txt", CASILLAS)
        os.replace(tmp, salida)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"Zip listo: {salida}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def preparar_kit(esc: Escritor, kit: Path, diag: Path, sello: str, *,
                 con_volumen: bool, con_media: bool) -> None:
    r = rutas_kit(kit)
    esc.carpeta(kit)
    esc.escribir(r["sello_kit"], SELLO_KIT)
    # El volumen va primero: si hay que abortar por un /Volumes ajeno, mejor
    # antes de escribir un config.lua que diga que el volumen existe.
    if con_volumen:
        preparar_volumen(esc, kit)
    for c in ("recibos", "manual", "matriz", "stubs"):
        esc.carpeta(r[c])
    esc.copiar(diag, r["diagnostico"])
    esc.escribir(r["config"], config_lua(kit, sello, con_volumen))

    esc.escribir(r["ok"], FIXTURE_OK)
    esc.escribir(r["error"], FIXTURE_ERROR)
    esc.escribir(r["acentos"], FIXTURE_OK)
    esc.escribir(r["grande"], grande_data())
    esc.escribir(ruta_appsupport(), FIXTURE_OK)

    for carpeta, nombre, ctx in STUBS:
        texto = stub_lua(ctx, kit) if ctx else stub_error()
        esc.escribir(r["stubs"] / carpeta / nombre, texto)

    esc.escribir(r["linea"], linea_consola(kit) + "\n")
    esc.escribir(r["manual"] / "casillas.txt", CASILLAS, solo_si_falta=True)
    esc.escribir(r["instalar"], instalar_kit_sh(kit), ejecutable=True)
    esc.escribir(r["recoger"], recoger_kit_sh(), ejecutable=True)
    esc.escribir(r["quitar"], quitar_kit_sh(), ejecutable=True)
    esc.escribir(r["leeme"], leeme_txt())

    if con_media:
        preparar_media(esc, kit)


def siguientes_pasos(kit: Path, instalo: bool) -> str:
    r = rutas_kit(kit)
    paso_stubs = ("" if instalo else
                  "  0. Instala los stubs: python3 bin/preparar_diagnostico.py "
                  "--instalar-stubs\n")
    return f"""
Linea para la Consola de Resolve (Workspace > Console, modo Lua):

{linea_consola(kit)}

Siguientes pasos:
{paso_stubs}  1. En Resolve crea el proyecto {PROYECTO_REQUERIDO} (en una base de prueba) y abrelo.
  2. Workspace > Scripts > Diez50 Diagnostico: DOS veces. Luego el del submenu
     Diez50, el de Edit y el de error, una vez cada uno.
  3. Pega la linea de arriba en la Consola: UNA vez.
  4. Exporta a mano la timeline "DIAG MARCAS <sello>-menu_utility-r1"
     (File > Export > Timeline, .drt) a {r["manual"]}/
  5. Anota lo que viste a ojo en {r["manual"]}/casillas.txt (menu, submenu,
     print_consola, error_visible), o pasalo con --ojo.
  6. python3 bin/leer_diagnostico.py --kit {kit}
     Opcional: --ojo menu=si,submenu=no,print_consola=si,error_visible=no
               --print <archivo con lo que salio en la Consola>
     Con el zip de recoger_kit.sh de otra Mac, ya descomprimido:
     python3 bin/leer_diagnostico.py --kit {kit} --desde <carpeta del zip>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kit", default="~/Diez50-diag",
                    help="Carpeta del kit. Default: ~/Diez50-diag")
    ap.add_argument("--diagnostico", default=str(DIAG_MOTOR),
                    help="diagnostico.lua a copiar al kit. Default: el del motor.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Imprime cada ruta y cada comando; no escribe ni ejecuta nada.")
    ap.add_argument("--sin-volumen", action="store_true",
                    help="No crea ni monta la .dmg; volumen y recibos_volumen quedan nil.")
    ap.add_argument("--sin-media", action="store_true",
                    help="No llama a ffmpeg ni a generar_media_prueba.py (tests).")
    ap.add_argument("--instalar-stubs", action="store_true",
                    help="Copia los stubs Diez50* a Fusion/Scripts (DIEZ50_SCRIPTS_DIR).")
    ap.add_argument("--quitar-stubs", action="store_true",
                    help="Borra exactamente los stubs que instalaria, y nada mas.")
    ap.add_argument("--kit-zip", metavar="SALIDA.zip", default="",
                    help="Empaqueta el kit (completo, con media) con instalar_kit.sh, "
                         "recoger_kit.sh, quitar_kit.sh y LEEME.txt.")
    args = ap.parse_args(argv)

    esc = Escritor(args.dry_run)
    scripts = dir_scripts()
    try:
        if args.quitar_stubs:
            if args.instalar_stubs:
                raise Aborto("--instalar-stubs y --quitar-stubs no van juntos.")
            quitar_stubs(esc, scripts)
            print(f"Stubs {'(simulado) ' if args.dry_run else ''}"
                  f"quitados de {scripts}")
            return 0

        kit = Path(os.path.expanduser(args.kit))
        if not kit.is_absolute():
            kit = Path.cwd() / kit
        kit = Path(os.path.normpath(str(kit)))
        # La misma prueba que el Escritor (texto sin mayusculas ni firmlink, y
        # dispositivo), pero mas estricta: el kit no vive en /Volumes, ni
        # siquiera en la imagen de prueba.
        dev_kit = _dispositivo(str(kit))
        if bajo_volumes(str(kit)) or bajo_volumes(_real(kit)) \
                or (dev_kit is not None and dev_kit not in dispositivos_de_casa()):
            raise Aborto(f"El kit no puede vivir en /Volumes ni en otro disco "
                         f"({kit}): ahi vive el material rodado.")
        revisar_carpeta_del_kit(kit)
        if " " in str(kit):
            print(f"AVISO: la ruta del kit tiene espacios ({kit}). El contrato "
                  "pide una sin espacios; funciona, pero es mas fragil.")
        diag = Path(os.path.expanduser(args.diagnostico))
        if not diag.is_file():
            raise Aborto(f"No existe {diag}. El diagnostico se escribe aparte "
                         "(resolve/diagnostico.lua); sin el, el kit no mide nada. "
                         "Vuelve a correr cuando exista, o pasa --diagnostico.")

        sello = sello_actual()
        preparar_kit(esc, kit, diag, sello, con_volumen=not args.sin_volumen,
                     con_media=not args.sin_media)
        if args.instalar_stubs:
            instalar_stubs(esc, kit, scripts)
            print(f"Stubs {'(simulado) ' if args.dry_run else ''}"
                  f"instalados en {scripts}")
        if args.kit_zip:
            salida = Path(os.path.expanduser(args.kit_zip))
            if not salida.is_absolute():
                salida = Path.cwd() / salida
            if args.sin_volumen:
                print("AVISO: config.lua del zip lleva las rutas de "
                      f"{VOLUMEN} aunque aqui se uso --sin-volumen: "
                      "instalar_kit.sh crea el volumen en la otra Mac.")
            empaquetar(esc, kit, salida, sello)
    except Aborto as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"\nKit {'(simulado) ' if args.dry_run else ''}en {kit}")
    print(siguientes_pasos(kit, args.instalar_stubs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
