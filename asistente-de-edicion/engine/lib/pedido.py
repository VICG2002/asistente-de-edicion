"""El pedido y el buzon del aplicador de Resolve (plan Free, §3, contrato v2).

POR QUE EXISTE (2026-10-05)
En el menu de Resolve Free 21.1 no hay globals de Consola que leer ni `io`: el
aplicador (`resolve/aplicar.lua`) solo puede CARGAR datos con loadfile. Lo que
el editor decidio (que construir, con que politica) viaja en un archivo Lua de
solo datos, el buzon, con un pedido por proyecto de Resolve:

    ~/Library/Application Support/Diez50/buzon.lua

Lo escriben el motor, el CLI o Claude, siempre con este modulo. Nunca a mano.

QUE GARANTIZA
  - Escritura atomica: primero un .tmp en la misma carpeta y luego un rename.
    Resolve nunca lee un buzon a medias.
  - El buzon tiene un gemelo JSON (buzon.json), que es el que lee Python. Lua
    no se parsea desde Python; se genera. Agregar un proyecto no borra los
    pedidos de los demas.
  - El pedido se valida antes de escribirse: formato, sello, rutas absolutas,
    acciones que el aplicador conoce y una politica que nunca borra.
  - Cada pedido deja su copia en <recibos>/pedido.json. Es lo que el motor
    compara despues contra lo que volvio por el canal de regreso
    (lib/regreso.py).

EL SELLO
`AAAAMMDD-HHMM-xxxx`, con xxxx al azar. Los ultimos cuatro caracteres son el
sello corto que sale en el nombre de la timeline de reporte. Lua no calcula
fechas: el sello lo pone Python.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

FORMATO = 2
SEP = " · "                 # " · ", el mismo que A.SEP en aplicar.lua
PREFIJO = "Diez50"
RE_SELLO = re.compile(r"^\d{8}-\d{4}-[0-9A-Za-z]{4}$")
# Resolve rechaza estos nueve en nombres de timeline y de bin (LANG-11).
PROHIBIDOS = re.compile(r'[/\\:*?"<>|]')

# Las que aplicar.lua sabe correr hoy. Un pedido con otra saldria en ERROR
# seguro, y es mejor negarse aqui. `construir` entro el 2026-10-06 (Fase 1),
# solo para proyectos documentales.
ACCIONES_APLICADOR = {"prueba_regreso", "construir", "exportar"}
ANTERIORES = {"renombrar", "conservar"}    # nunca "borrar"

# Los campos que entiende `construir` (resolve/construir.lua). Uno que no este
# aqui es casi siempre una errata, y con una errata el aplicador usaria el
# valor por defecto sin decir nada.
CAMPOS_CONSTRUIR = {"tipo", "dia", "timelines", "cronologia_broll", "multicam",
                    "mover_a_bins", "modo_merge", "orden_tx", "cortar_silencios",
                    "marcar_cortes"}
TIMELINES_CONSTRUIR = {"aroll", "broll", "audios_externos", "por_subcarpeta"}
NOMBRE_TIMELINE = {"aroll": "A-ROLL", "broll": "B-ROLL",
                   "audios_externos": "AUDIOS EXTERNOS"}
MESES = ("ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic")

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Diez50"
BUZON_POR_DEFECTO = APP_SUPPORT / "buzon.lua"
STUB_NOMBRE = "Diez50 Aplicar.lua"
DIR_STUBS = (Path.home() / "Library" / "Application Support" / "Blackmagic Design"
             / "DaVinci Resolve" / "Fusion" / "Scripts" / "Utility")


class ErrorPedido(ValueError):
    """El pedido no se puede escribir. El mensaje dice por que."""


# --------------------------------------------------------------------------
# nombres y sello
# --------------------------------------------------------------------------

def nombre_seguro(s: str) -> str:
    """Lo mismo que A.nombreSeguro en aplicar.lua."""
    return PROHIBIDOS.sub("_", str(s))


def sello_corto(sello: str) -> str:
    return str(sello)[-4:]


def archivo_de_proyecto(proyecto: str) -> str:
    """Lo mismo que A.archivoDeProyecto en aplicar.lua, byte a byte: ASCII
    alfanumerico, '-' y '_' se quedan; todo otro byte (un acento son dos) es '_'.
    Es el nombre de <errores>/error_<proyecto>.drt."""
    ok = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return "".join(chr(b) if b in ok else "_" for b in str(proyecto).encode("utf-8"))


def nuevo_sello(ahora: datetime | None = None, azar: str | None = None) -> str:
    ahora = ahora or datetime.now()
    azar = azar or secrets.token_hex(2)
    s = f"{ahora:%Y%m%d-%H%M}-{azar}"
    if not RE_SELLO.match(s):
        raise ErrorPedido(f"sello mal formado: {s!r}")
    return s


# --------------------------------------------------------------------------
# Python -> Lua
# --------------------------------------------------------------------------

_RESERVADAS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
    "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return", "then",
    "true", "until", "while",
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def lua_cadena(s: str) -> str:
    """Cadena Lua entre comillas dobles. Los bytes UTF-8 viajan tal cual;
    los de control van como \\ddd, que entienden 5.1 y 5.5."""
    out = []
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def a_lua(v, sangria: int = 0) -> str:
    """Un valor de Python como literal Lua. Solo datos: dict, list, str,
    int, float, bool y None (que en una tabla se omite)."""
    pad = "  " * (sangria + 1)
    fin = "  " * sangria
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ErrorPedido(f"numero no representable en Lua: {v!r}")
        return repr(v)
    if isinstance(v, str):
        return lua_cadena(v)
    if isinstance(v, (list, tuple)):
        if not v:
            return "{}"
        return "{\n" + "".join(f"{pad}{a_lua(x, sangria + 1)},\n" for x in v) + fin + "}"
    if isinstance(v, dict):
        partes = []
        for k in sorted(v, key=str):
            if v[k] is None:
                continue
            if isinstance(k, str) and _IDENT.match(k) and k not in _RESERVADAS:
                clave = k
            elif isinstance(k, str):
                clave = f"[{lua_cadena(k)}]"
            elif isinstance(k, int) and not isinstance(k, bool):
                clave = f"[{k}]"
            else:
                raise ErrorPedido(f"clave no representable en Lua: {k!r}")
            partes.append(f"{pad}{clave} = {a_lua(v[k], sangria + 1)},\n")
        if not partes:
            return "{}"
        return "{\n" + "".join(partes) + fin + "}"
    raise ErrorPedido(f"tipo no representable en Lua: {type(v).__name__}")


def buzon_lua(buzon: dict) -> str:
    cab = ("-- Diez50 - buzon del aplicador de Resolve. Lo escribe lib/pedido.py\n"
           "-- (.tmp + rename). NO editar a mano. aplicar.lua lo carga con loadfile\n"
           "-- en un entorno vacio: solo datos.\n")
    return cab + "return " + a_lua(buzon) + "\n"


# --------------------------------------------------------------------------
# validar
# --------------------------------------------------------------------------

def _absoluta(r) -> bool:
    return isinstance(r, str) and r.startswith("/")


def validar_pedido(pedido: dict, *, validar_rutas: bool = True) -> list[str]:
    """Los problemas del pedido, o [] si se puede escribir.

    validar_rutas=False sirve cuando el pedido es para OTRA maquina (la VM de
    prueba): las rutas tienen que ser absolutas, pero desde aqui no se ven."""
    errores: list[str] = []
    if not isinstance(pedido, dict):
        return ["el pedido no es un diccionario"]
    sello = pedido.get("sello")
    if not isinstance(sello, str) or not RE_SELLO.match(sello):
        errores.append(f"sello invalido: {sello!r} (se espera AAAAMMDD-HHMM-xxxx)")
    rutas = pedido.get("rutas") or {}
    if not isinstance(rutas, dict):
        errores.append("rutas no es un diccionario")
        rutas = {}
    for k, r in rutas.items():
        if r is None:           # se omite al escribir el Lua: es como no darla
            continue
        if not _absoluta(r):
            errores.append(f"rutas.{k} no es absoluta: {r!r}")
        elif validar_rutas and k != "recibos" and not Path(r).exists():
            errores.append(f"rutas.{k} no existe: {r}")
    acciones = pedido.get("acciones")
    if not isinstance(acciones, list) or not acciones:
        errores.append("el pedido no trae acciones")
        acciones = []
    construye = False
    for i, a in enumerate(acciones, start=1):
        tipo = a.get("tipo") if isinstance(a, dict) else None
        if tipo not in ACCIONES_APLICADOR:
            errores.append(f"accion {i}: '{tipo}' no la conoce aplicar.lua "
                           f"(conoce: {', '.join(sorted(ACCIONES_APLICADOR))})")
            continue
        if tipo == "prueba_regreso":
            construye = True
            errores += _validar_prueba(i, a, validar_rutas)
        if tipo == "construir":
            construye = True
            errores += _validar_construir(i, a, pedido, rutas)
        if tipo == "exportar":
            if not construye:
                errores.append(f"accion {i}: exportar va despues de lo que construye")
            if not rutas.get("recibos"):
                errores.append(f"accion {i}: exportar necesita rutas.recibos")
            for f in a.get("formatos") or ["drt"]:
                if f != "drt":
                    errores.append(f"accion {i}: formato '{f}' no soportado (solo drt)")
    pol = pedido.get("politica") or {}
    if pol.get("anteriores", "renombrar") not in ANTERIORES:
        errores.append(f"politica.anteriores = {pol.get('anteriores')!r}: solo "
                       f"{' o '.join(sorted(ANTERIORES))}. Nunca se borra lo que "
                       "el editor pudo haber tocado")
    return errores


def sufijo_dia(dia: str | None) -> str:
    """" 28-sep" para "2026-09-28", como C.sufijoDe en construir.lua. ValueError
    si no es AAAA-MM-DD."""
    if not dia:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(dia))
    if not m or not 1 <= int(m[2]) <= 12:
        raise ValueError(f"dia {dia!r} no es AAAA-MM-DD")
    return f" {int(m[3])}-{MESES[int(m[2]) - 1]}"


def _validar_construir(i: int, a: dict, pedido: dict, rutas: dict) -> list[str]:
    errores = []
    if not rutas.get("data"):
        errores.append(f"accion {i}: construir necesita rutas.data (el horneado)")
    pfx = pedido.get("pfx")
    if not isinstance(pfx, str) or not pfx.strip():
        errores.append(f"accion {i}: construir necesita pfx (el prefijo de las timelines)")
    elif PROHIBIDOS.search(pfx):
        errores.append(f"accion {i}: pfx {pfx!r} lleva / \\ : * ? \" < > |: Resolve "
                       "rechazaria todas las timelines")
    raros = sorted(set(a) - CAMPOS_CONSTRUIR)
    if raros:
        errores.append(f"accion {i}: construir no entiende {', '.join(raros)}")
    try:
        sufijo_dia(a.get("dia"))
    except ValueError as e:
        errores.append(f"accion {i}: {e}")
    tl = a.get("timelines") or {}
    if not isinstance(tl, dict):
        errores.append(f"accion {i}: timelines tiene que ser un diccionario")
    else:
        raras = sorted(set(tl) - TIMELINES_CONSTRUIR)
        if raras:
            errores.append(f"accion {i}: timelines que construir no conoce: "
                           + ", ".join(raras))
        for k, v in tl.items():
            if not isinstance(v, bool):
                errores.append(f"accion {i}: timelines.{k} tiene que ser true o false")
    for k in ("cronologia_broll", "multicam", "modo_merge"):
        if a.get(k) is not None and not isinstance(a[k], bool):
            errores.append(f"accion {i}: {k} tiene que ser true o false")
    if a.get("mover_a_bins"):
        errores.append(f"accion {i}: mover_a_bins esta fuera de Free v1 (decision del "
                       "2026-10-05)")
    if a.get("cortar_silencios") or a.get("marcar_cortes"):
        errores.append(f"accion {i}: cortar_silencios y marcar_cortes llegan con los "
                       "proyectos comerciales")
    otx = a.get("orden_tx")
    if otx is not None and (not isinstance(otx, list)
                            or not all(isinstance(x, str) and x for x in otx)):
        errores.append(f"accion {i}: orden_tx tiene que ser una lista de nombres de TX")
    return errores


def _validar_prueba(i: int, a: dict, validar_rutas: bool) -> list[str]:
    errores = []
    clave = a.get("clave", "")
    if not re.match(r"^[a-z0-9_]+$", str(clave)):
        errores.append(f"accion {i}: clave {clave!r} no sirve como nombre de archivo")
    nombre = a.get("nombre", "")
    if not nombre or PROHIBIDOS.search(nombre):
        errores.append(f"accion {i}: nombre {nombre!r} vacio o con / \\ : * ? \" < > |")
    media = a.get("media") or {}
    if not media:
        errores.append(f"accion {i}: sin media")
    for k, r in media.items():
        if not _absoluta(r):
            errores.append(f"accion {i}: media.{k} no es absoluta: {r!r}")
        elif validar_rutas and not Path(r).exists():
            errores.append(f"accion {i}: media.{k} no existe: {r}")
    pistas = a.get("pistas") or {}
    if not pistas.get("video") and not pistas.get("audio"):
        errores.append(f"accion {i}: sin pistas")
    for tipo in ("video", "audio"):
        for j, p in enumerate(pistas.get(tipo) or [], start=1):
            if p.get("media") not in media:
                errores.append(f"accion {i}: {tipo} {j} usa media '{p.get('media')}', "
                               "que no esta en media")
            if not p.get("nombre"):
                errores.append(f"accion {i}: {tipo} {j} sin nombre")
    return errores


# --------------------------------------------------------------------------
# leer y escribir
# --------------------------------------------------------------------------

def _gemelo(ruta_lua: Path) -> Path:
    return ruta_lua.with_suffix(".json")


def _atomico(ruta: Path, texto: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{ruta.name}.", suffix=".tmp", dir=ruta.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def leer_buzon(ruta: Path | str = BUZON_POR_DEFECTO) -> dict:
    """El buzon actual, desde su gemelo JSON. Vacio si no hay ninguno de los dos.

    Si hay buzon.lua sin buzon.json, se niega: empezar de un buzon vacio
    reescribiria buzon.lua sin los pedidos de los demas proyectos (revision del
    PR #1, 2026-10-05). Lua no se parsea desde aqui."""
    j = _gemelo(Path(ruta))
    if not j.exists():
        if Path(ruta).exists():
            raise ErrorPedido(
                f"{ruta} existe sin su {j.name}: no se reescribe, porque se perderian "
                f"sus pedidos. Si ya no sirven, quitalo a mano; si sirven, vuelve a "
                f"escribirlos con bin/pedido.py.")
        return {"formato": FORMATO, "pedidos": {}}
    datos = json.loads(j.read_text(encoding="utf-8"))
    if datos.get("formato") != FORMATO:
        raise ErrorPedido(f"{j} esta en formato {datos.get('formato')!r}; este motor "
                          f"escribe el {FORMATO}")
    datos.setdefault("pedidos", {})
    return datos


def escribir_buzon(buzon: dict, ruta: Path | str = BUZON_POR_DEFECTO) -> Path:
    """Escribe buzon.lua y buzon.json, cada uno de forma atomica. El JSON va
    primero: si el Lua fallara, el siguiente intento parte del estado nuevo."""
    ruta = Path(ruta)
    _atomico(_gemelo(ruta), json.dumps(buzon, indent=2, ensure_ascii=False) + "\n")
    _atomico(ruta, buzon_lua(buzon))
    return ruta


@contextmanager
def _candado(ruta: Path | str):
    """Exclusion entre quienes escriben el buzon (motor, CLI, Claude). El rename
    atomico evita un buzon a medias, no la actualizacion perdida: dos escritores
    leen N pedidos, cada uno agrega el suyo y el ultimo rename gana (revision del
    PR #1, 2026-10-05). flock se suelta solo si el proceso muere."""
    lock = Path(ruta).parent / f".{Path(ruta).name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def poner_pedido(proyecto: str, pedido: dict, *, ruta: Path | str = BUZON_POR_DEFECTO,
                 motor_version: str = "dev", validar_rutas: bool = True,
                 crear_recibos: bool = True) -> dict:
    """Agrega (o reemplaza) el pedido de un proyecto sin tocar los demas.

    Con crear_recibos crea la carpeta de recibos y deja ahi pedido.json: Lua
    no puede crear carpetas, y Export falla si la carpeta no existe."""
    if not proyecto:
        raise ErrorPedido("falta el nombre del proyecto de Resolve")
    errores = validar_pedido(pedido, validar_rutas=validar_rutas)
    if errores:
        raise ErrorPedido("pedido invalido:\n  - " + "\n  - ".join(errores))
    escrito = datetime.now().astimezone().isoformat(timespec="seconds")
    pedido = dict(pedido, escrito=pedido.get("escrito") or escrito)
    with _candado(ruta):
        buzon = leer_buzon(ruta)
        buzon["formato"] = FORMATO
        buzon["motor_version"] = motor_version
        buzon["escrito"] = escrito
        buzon["pedidos"][proyecto] = para_buzon(pedido)
        recibos = (pedido.get("rutas") or {}).get("recibos")
        if crear_recibos and recibos:
            d = Path(recibos)
            d.mkdir(parents=True, exist_ok=True)
            _atomico(d / "pedido.json", json.dumps(
                copia_pedido(proyecto, pedido, motor_version), indent=2,
                ensure_ascii=False) + "\n")
        escribir_buzon(buzon, ruta)
    return buzon


def para_buzon(pedido: dict) -> dict:
    """El pedido sin las listas que solo usa el motor al verificar.

    `esperado.clips` y `esperado.audios` (de `construir`) pueden ser cientos de
    rutas que aplicar.lua no lee: van solo en <recibos>/pedido.json. En el
    buzon queda `esperado.timelines`, que es lo que el reporte nombra."""
    esp = pedido.get("esperado")
    if not isinstance(esp, dict) or not ({"clips", "audios"} & set(esp)):
        return pedido
    return dict(pedido, esperado={k: v for k, v in esp.items()
                                  if k not in ("clips", "audios")})


def copia_pedido(proyecto: str, pedido: dict, motor_version: str = "dev") -> dict:
    """Lo que va a <recibos>/pedido.json: lo que el motor compara al volver."""
    return {"formato": FORMATO, "proyecto": proyecto, "motor_version": motor_version,
            "pedido": pedido}


def quitar_pedido(proyecto: str, *, ruta: Path | str = BUZON_POR_DEFECTO) -> bool:
    with _candado(ruta):
        buzon = leer_buzon(ruta)
        if proyecto not in buzon["pedidos"]:
            return False
        del buzon["pedidos"][proyecto]
        escribir_buzon(buzon, ruta)
    return True


# --------------------------------------------------------------------------
# la prueba de ida y vuelta
# --------------------------------------------------------------------------

def pedido_prueba(*, negro: str, camara: str, lav: str, recibos_base: str,
                  sello: str | None = None, reaplicar: bool = False) -> dict:
    """Pedido de la prueba de ida y vuelta (Fase 0): una timeline con un orden
    de pistas conocido, exportada a .drt, y su reporte.

    La media es la del kit de diagnostico: el negro de 10 min (video y audio
    estereo) es la camara A, camara.mov (audio mono) la B y lav.wav el lavalier.
    El orden es la regla dura: video de camara, audio de camara y al final el
    audio externo."""
    sello = sello or nuevo_sello()
    return {
        "sello": sello,
        "rutas": {"recibos": str(Path(recibos_base) / sello) + "/"},
        "acciones": [
            {"tipo": "prueba_regreso", "clave": "prueba",
             "nombre": PREFIJO + SEP + "prueba de regreso",
             "media": {"cam_a": negro, "cam_b": camara, "lava": lav},
             "pistas": {
                 "video": [{"nombre": "CAM A", "media": "cam_a"},
                           {"nombre": "CAM B", "media": "cam_b"}],
                 "audio": [{"nombre": "CAM A", "media": "cam_a", "tipo": "stereo"},
                           {"nombre": "CAM B", "media": "cam_b", "tipo": "mono"},
                           {"nombre": "LAVA izq", "media": "lava", "tipo": "mono"}],
             }},
            {"tipo": "exportar", "formatos": ["drt"]},
        ],
        "politica": {"reaplicar": reaplicar, "anteriores": "renombrar",
                     "guardar_al_final": True},
        "reporte": {"abrir_al_terminar": True},
    }


# --------------------------------------------------------------------------
# construir (Fase 1, 2026-10-06)
# --------------------------------------------------------------------------

def leer_indice(ruta: Path | str) -> dict:
    """El <slug>_indice.json que deja export_lua_data.py junto al horneado: que
    clips y que WAV tiene, con su rol y su dia. Es lo que el motor espera ver
    volver en el .drt. Python no parsea el .lua: lee su gemelo."""
    datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    if not isinstance(datos.get("clips"), dict) or not isinstance(datos.get("audios"), dict):
        raise ErrorPedido(f"{ruta} no tiene la forma de un indice de horneado")
    return datos


def armar_pedido_construir(indice: dict, *, data: str, recibos_base: str, pfx: str,
                           multicam: str | None = None, dia: str | None = None,
                           sello: str | None = None, reaplicar: bool = False,
                           timelines: dict | None = None,
                           modo_merge: bool | None = None,
                           orden_tx: list[str] | None = None) -> dict:
    """El pedido de `construir` + `exportar` para un horneado ya indexado.

    `orden_tx` es el orden de las pistas LAVA de AUDIOS EXTERNOS; sin el,
    construir.lua las ordena por nombre.

    `esperado` dice que tiene que volver: las timelines por nombre (las que el
    material del dia puede llenar) y, para la copia de los recibos, las rutas
    de los clips y de los WAV del dia."""
    sello = sello or nuevo_sello()
    try:
        sufijo = sufijo_dia(dia)
    except ValueError as e:
        raise ErrorPedido(str(e)) from None

    def del_dia(x: dict) -> bool:
        return not dia or x.get("dia", "") == dia

    clips = [{"ruta": r, "nombre": c.get("nombre", ""), "roll": c.get("roll", "")}
             for r, c in sorted(indice["clips"].items()) if del_dia(c)]
    audios = [{"ruta": r, "nombre": a.get("nombre", "")}
              for r, a in sorted(indice["audios"].items()) if del_dia(a)]
    quiere = {"aroll": True, "broll": True, "audios_externos": True,
              "por_subcarpeta": True, **(timelines or {})}
    nombres = []
    for clave, hay in (("aroll", any(c["roll"] == "A" for c in clips)),
                       ("broll", any(c["roll"] == "B" for c in clips)),
                       ("audios_externos", bool(audios))):
        if hay and quiere.get(clave):
            nombres.append(pfx + NOMBRE_TIMELINE[clave] + sufijo)
    accion = {"tipo": "construir", "dia": dia or None, "timelines": quiere,
              "cronologia_broll": True, "multicam": True, "mover_a_bins": False,
              "modo_merge": modo_merge, "orden_tx": list(orden_tx) if orden_tx else None}
    return {
        "sello": sello,
        "pfx": pfx,
        "rutas": {"data": data, "multicam": multicam,
                  "recibos": str(Path(recibos_base) / sello) + "/"},
        "acciones": [accion, {"tipo": "exportar", "formatos": ["drt"]}],
        "politica": {"reaplicar": reaplicar, "anteriores": "renombrar",
                     "guardar_al_final": True},
        "reporte": {"abrir_al_terminar": True},
        "esperado": {"timelines": nombres, "clips": clips, "audios": audios},
    }


def _config(root: Path | str) -> dict:
    cfg = Path(root) / ".cinema_assistant" / "project_config.json"
    try:
        datos = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def pfx_de_proyecto(root: Path | str) -> str | None:
    """El `timeline_prefix` del project_config.json ("ASISTENTE — "), o None."""
    v = _config(root).get("timeline_prefix")
    return v if isinstance(v, str) and v.strip() else None


def orden_tx_de_proyecto(root: Path | str) -> list[str] | None:
    """El orden de los TX de lavalier, el de su primera aparicion en el
    `lavalier_tx` del project_config.json (Asistente: izq y luego drc), o None
    si el proyecto no lo declara. Es el orden de las pistas LAVA."""
    declarado = _config(root).get("lavalier_tx")
    if not isinstance(declarado, dict):
        return None
    orden: list[str] = []
    for tx in declarado.values():
        if isinstance(tx, str) and tx and tx not in orden:
            orden.append(tx)
    return orden or None


def pedido_construir(root: Path | str, *, pfx: str | None = None, nombre: str | None = None,
                     slug: str | None = None, dia: str | None = None,
                     sello: str | None = None, reaplicar: bool = False,
                     timelines: dict | None = None,
                     modo_merge: bool | None = None) -> dict:
    """El pedido `construir` de un proyecto, desde su carpeta de horneados
    (<root>/.cinema_assistant/resolve/). Los recibos van ahi mismo, en
    recibos/<sello>/.

    El prefijo de las timelines: `pfx` tal cual, o `nombre` + " — ", o el
    `timeline_prefix` del project_config.json."""
    pfx = pfx or (f"{nombre} — " if nombre else None) or pfx_de_proyecto(root)
    if not pfx:
        raise ErrorPedido("sin prefijo de timelines: da --nombre o declara "
                          "timeline_prefix en project_config.json")
    bake = Path(root) / ".cinema_assistant" / "resolve"
    if slug is None:
        datas = sorted(p for p in bake.glob("*_data.lua") if not p.name.startswith("._"))
        if len(datas) != 1:
            raise ErrorPedido(f"en {bake} hay {len(datas)} horneados (*_data.lua): "
                              "di cual con --slug")
        slug = datas[0].name[:-len("_data.lua")]
    data = bake / f"{slug}_data.lua"
    if not data.exists():
        raise ErrorPedido(f"no existe {data}: hornea con export_lua_data.py")
    indice = bake / f"{slug}_indice.json"
    if not indice.exists():
        raise ErrorPedido(f"no existe {indice.name}: re-hornea con export_lua_data.py, "
                          "que lo escribe junto al horneado desde el 2026-10-06")
    if indice.stat().st_mtime + 5 < data.stat().st_mtime:
        raise ErrorPedido(f"{indice.name} es anterior a {data.name}: re-hornea con "
                          "export_lua_data.py")
    multicam = bake / f"{slug}_multicam.lua"
    return armar_pedido_construir(
        leer_indice(indice), data=str(data), recibos_base=str(bake / "recibos"),
        pfx=pfx, multicam=str(multicam) if multicam.exists() else None, dia=dia,
        sello=sello, reaplicar=reaplicar, timelines=timelines, modo_merge=modo_merge,
        orden_tx=orden_tx_de_proyecto(root))


# --------------------------------------------------------------------------
# el stub del menu
# --------------------------------------------------------------------------

def texto_stub(aplicar: str, buzon: str, errores: str, ctx: str = "menu_utility",
               motor: str | None = None) -> str:
    """El stub de Workspace > Scripts. Rutas absolutas LITERALES: en el menu
    de Free no hay os.getenv ni debug con que calcularlas.

    `motor` es la carpeta de aplicar.lua (por defecto, la suya): ahi busca
    `construir` sus modulos (construir.lua, asistente_lib.lua).

    Envuelve todo en pcall. Si aplicar.lua no carga, el error sale como E00; si
    truena dentro, como E05. En los dos casos queda una timeline
    "Diez50 · ERROR · <codigo> · <mensaje>" a la vista."""
    sep = "\\194\\183"
    motor = motor or str(Path(aplicar).parent)
    if not motor.endswith("/"):
        motor += "/"
    return f"""-- Diez50 Aplicar. Generado por lib/pedido.py: no editar.
-- Corre el pedido del proyecto abierto que dejo el motor en el buzon.
local R = resolve or (Resolve and Resolve())
local ok, A = pcall(dofile, {lua_cadena(aplicar)})
local codigo = "E00"
if ok and (type(A) ~= "table" or type(A.correr) ~= "function") then
  ok, A = false, "aplicar.lua no devolvio una tabla con correr"
end
if ok then
  codigo = "E05"
  local ok2, err = pcall(A.correr, {{ ctx = {lua_cadena(ctx)}, resolve = R,
    buzon = {lua_cadena(buzon)}, errores = {lua_cadena(errores)},
    motor = {lua_cadena(motor)} }})
  if not ok2 then ok, A = false, err end
end
if not ok then
  pcall(function()
    local p = R:GetProjectManager():GetCurrentProject()
    local msg = string.gsub(tostring(A), "^.-:%d+: ", "")
    msg = string.gsub(msg, '[/\\\\:%*%?"<>|%c]', "_")
    local tl = p:GetMediaPool():CreateEmptyTimeline("Diez50 {sep} ERROR {sep} " .. codigo
      .. " {sep} " .. string.sub(msg, 1, 60))
    if tl then p:SetCurrentTimeline(tl) end
  end)
end
"""
