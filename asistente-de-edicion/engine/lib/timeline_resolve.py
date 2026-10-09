"""Leer una timeline ya montada en DaVinci Resolve, sin abrir Resolve.

QUE RESUELVE
Hasta ahora el motor solo sabia ESCRIBIR en Resolve (los .lua que arman
timelines, colocan markers y hornean el reparto). No sabia LEER un montaje que
ya existe. Eso dejaba fuera el dato mas valioso que produce el editor: donde
decidio cortar.

Se puede leer, y es barato. La base de disco de Resolve guarda cada proyecto en
un `Project.db` que es SQLite corriente. Este modulo lo abre en solo lectura y
devuelve la lista de cortes de una timeline con precision de fotograma.

DE DONDE SALE LA RUTA
  ~/Library/Preferences/Blackmagic Design/DaVinci Resolve/activedb.conf
      -> "disk*:VICG"  (nombre de la base activa)
  .../dblist.conf
      -> "VICG:$HOME/Movies/Resolve Project Library:*:::DISK"
  <ruta>/Resolve Projects/Users/guest/Projects/<Proyecto>/Project.db

ESQUEMA, MEDIDO EL 2026-08-19 SOBRE Resolve 21.0.2 (DbPrjVer 17)
  Sm2Timeline   Name, Sequence, CreateTimeInSecs, ModTimeInSecs
  Sm2Sequence   FrameRate    = un double LITTLE-endian
                Resolution   = dos int64 BIG-endian (ancho, alto)
                MediaExtents = dos doubles LITTLE-endian (inicio_s, duracion_s)
                NumOutputAudioChannels
  Sm2SequenceContainer         una fila por secuencia
  Sm2SequenceContainer_Sm2TiTrack
                DbOwner = contenedor, DbAssociate = pista,
                DbPropertyName in VideoTrackVec|AudioTrackVec|SubtitleTrackVec,
                DbIndex = numero de pista (0-based)
  Sm2TiItem     Name, Start, Duration, In (texto, en FOTOGRAMAS),
                MediaFilePath, MediaStartTime

DOS TRAMPAS QUE COSTARON TIEMPO
  1. Existe una tabla `Sm2Sequence_Sm2TiTrack` con pinta de ser la union
     secuencia->pista. Esta VACIA (0 filas en Morsa, con 243 pistas reales). La
     buena es la del CONTENEDOR. Consultar la equivocada no da error: devuelve
     cero filas, que se lee como "esta timeline no tiene pistas".
  2. El frame rate es little-endian y la resolucion big-endian, en el mismo
     registro. Leer los dos igual da 23.976 y una resolucion de 4222124650659840.

ESQUEMA, RE-MEDIDO EL 2026-09-23 SOBRE Resolve 21.1.0 build 17 (DbPrjVer 17)
  Se leyo el 2026-09-24 sobre COPIAS de tres Project.db que Resolve Studio
  21.1.0.0017 guardo el 2026-09-23 (dos proyectos personales y una cobertura),
  abiertas con `mode=ro`. El original no se abrio.

  Versiones. `DbAppVer` y `DbPrjVer` NO viven en el Project.db: estan en el XML
  de preferencias (`config.user.xml` y `user.data.xml`, atributos
  DbAppVer="21.1.0.0017" DbPrjVer="17") y en el comentario de cabecera de cada
  XML de un .drt/.drp (`<!--DbAppVer="..." DbPrjVer="..."-->`). Dentro del .db
  el numero equivalente es `SM_Project.ProjectVersion` = 17, igual que en
  21.0.2. `database_upgrade_log` llega a 18.1.0.003: son parches del ESQUEMA
  SQL, no la version de la app; no sirve para saber que Resolve guardo.

  Sin cambios respecto a 21.0.2: Sm2Timeline, Sm2Sequence (mismos blobs y el
  mismo orden de bytes), Sm2SequenceContainer, Sm2SequenceContainer_Sm2TiTrack
  (DbPropertyName VideoTrackVec|AudioTrackVec|SubtitleTrackVec) y Sm2TiItem
  (Start/Duration/In siguen siendo TEXTO en fotogramas). La trampa 1 sigue
  viva: `Sm2Sequence_Sm2TiTrack` tiene 0 filas en los tres proyectos.
  Novedad menor: una timeline que usa la resolucion del PROYECTO guarda
  `Resolution` en ceros (uno de los tres -> 0x0). No es un error de lectura.

  CARPETAS DEL MEDIA POOL (bins)
  Sm2MpFolder   Name, Sm2MpFolder_Owner_id (= MpFolder) = id del padre.
                La raiz ("Master") tiene el padre vacio y Sm2MediaPool_id
                puesto. La tabla de union `Sm2MpFolder_Sm2MpFolder` existe pero
                esta VACIA (0 filas en los tres): la jerarquia sale del
                Owner_id, no de la union. Es la misma trampa que la de pistas.

  MARKERS: NO estan en ninguna tabla con columnas propias.
  `Sm2TiItem.MarkersBA` existe pero esta vacio en todos los items (7492 en
  Morsa). Los markers viven en `BtLockableBlob`:
      DbType = 'Sm2SequenceLockableBlob'  -> markers de la TIMELINE;
                BlobOwner = Sm2Timeline.Sequence (= Sm2Sequence_id)
      DbType = 'Sm2TiItemLockableBlob'    -> markers de un ITEM;
                BlobOwner = Sm2TiItem_id
      DbType = 'BtMetadataLockableBlob'   -> metadata de clips del pool
                (otro formato; no se lee aqui)
  El blob se decodifica asi (ver `decodificar_markers`):
    1. FieldsBlob es un QVariantMap de Qt (QDataStream, big-endian) con una
       sola clave "BlobData" de tipo QByteArray.
    2. BlobData = uint32 etiqueta (10001) + uint32 largo + 1 byte de bandera
       + datos. Bandera 0x80 = crudo, 0x81 = comprimido con zstd.
    3. Los datos son protobuf: campo 2 = lista; cada campo 2 dentro = un
       marker: {1: {1: posicion}, 2: uint32 2 + uint32 largo + protobuf
       {1: {1: color, 3: nota, 3: duracion, 3: nombre, 6: customData}}}.
  La posicion esta en MEDIOS fotogramas: comprobado con los markers de item
  que el asistente pone en el frame 1 ("Revisar", guardado 2) y en el 0
  ("Posible descarte", guardado 0), y con una timeline en tiempo real cuyos
  markers cada 15 min a 24 fps estan separados 43200, no 21600. En la timeline
  es relativa al inicio (Morsa: 15574 en una timeline que arranca en 86400).
  Colores: codigo = 1 << k. Comprobados contra el .lua que los puso: Blue 2,
  Cyan 4, Yellow 16, Red 32, Purple 128, Sky 4096, Mint 8192. Green 8 y Pink
  64 caen en su lugar del orden de la paleta. El 2026-10-06, en los .drt que
  exporto `construir` en Studio 21.1.0.17: Lemon 16384 (el candidato
  emocional), Sand 32768 (la pausa) y Cream 131072 (el sello del pedido). Los
  demas (Fuchsia, Rose, Lavender, Cocoa) NO se midieron: el orden no cuadra
  (Sky esperaba 2048 y es 4096), asi que no se adivinan: salen "?<codigo>".
  La "duracion" es el segundo string: "1" en todos los medidos, que es la
  duracion 1 con la que el asistente pone todo; con duracion > 1 no se midio.
  Un .drp/.drt de 21.x lleva los MISMOS blobs, en project.xml, dentro de
  <LockableBlobMap> (ver lib/drt.py).

QUE NO ESTA AQUI
Los settings de PROYECTO (color science, gamma, monitorizacion). Viven en
`SM_Config.SetupBA`, un struct binario de 2704 bytes sin nombres de campo:
comprobado el 2026-08-19 que no hay forma honesta de leerlos de ahi. Para eso
esta `resolve/auditar_settings.lua`, que los pide por la API.

NUNCA SE ESCRIBE. El .db se copia a un temporal y se abre en modo `ro` sobre la
copia. Resolve solo vuelca a disco al GUARDAR: si el proyecto esta abierto con
cambios sin guardar, lo que se lee va por detras de lo que el editor ve. Por eso
`Timeline.modificada` viaja con el dato y `avisos()` lo dice.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PREFS = Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve"

# Un corte de imagen y uno de sonido separados por mas de esto ya no son el
# mismo corte. Dos fotogramas es lo que aguanta un splice hecho a mano en la
# timeline: por debajo es el mismo gesto del editor, por encima son dos.
TOLERANCIA_SYNC = 2

_TIPO = {"VideoTrackVec": "V", "AudioTrackVec": "A", "SubtitleTrackVec": "ST"}

# Las dos clases de fuente que hacen que un corte de audio cuente como corte de
# dialogo. Se derivan de lo DECLARADO en project_config.json, no se adivinan.
CLASES_DIALOGO = ("camara", "lavalier")


class ErrorTimeline(RuntimeError):
    """Falta la base, el proyecto o la timeline. Se dice cual, no se adivina."""


# --------------------------------------------------------------------------
# localizar la base de disco
# --------------------------------------------------------------------------

def bases_disco(prefs: Path = PREFS) -> dict[str, Path]:
    """Bases de DISCO declaradas en dblist.conf: nombre -> ruta."""
    f = prefs / "dblist.conf"
    out: dict[str, Path] = {}
    if not f.exists():
        return out
    for linea in f.read_text(errors="replace").splitlines():
        partes = linea.strip().split(":")
        if len(partes) < 2 or not partes[0]:
            continue
        if partes[-1].upper() != "DISK":
            continue                      # PostgreSQL: no se puede leer asi
        out[partes[0]] = Path(partes[1])
    return out


def base_activa(prefs: Path = PREFS) -> str | None:
    """Nombre de la base que Resolve tiene abierta ('disk*:VICG' -> 'VICG')."""
    f = prefs / "activedb.conf"
    if not f.exists():
        return None
    txt = f.read_text(errors="replace").strip()
    return txt.split(":", 1)[1].strip() if ":" in txt else None


def ruta_project_db(proyecto: str, base: str | None = None,
                    prefs: Path = PREFS) -> Path:
    bases = bases_disco(prefs)
    if not bases:
        raise ErrorTimeline(
            f"No hay ninguna base de DISCO en {prefs/'dblist.conf'}. "
            "Si el proyecto vive en una base PostgreSQL, este lector no sirve.")
    nombre = base or base_activa(prefs)
    if nombre not in bases:
        raise ErrorTimeline(
            f"Base {nombre!r} no esta en dblist.conf. Hay: {', '.join(bases)}")
    p = bases[nombre] / "Resolve Projects/Users/guest/Projects" / proyecto / "Project.db"
    if not p.exists():
        raise ErrorTimeline(f"No existe {p}")
    return p


def proyectos(base: str | None = None, prefs: Path = PREFS) -> list[str]:
    bases = bases_disco(prefs)
    nombre = base or base_activa(prefs)
    raiz = bases.get(nombre)
    if not raiz:
        return []
    d = raiz / "Resolve Projects/Users/guest/Projects"
    if not d.is_dir():
        return []
    return sorted(x.name for x in d.iterdir()
                  if (x / "Project.db").exists())


# --------------------------------------------------------------------------
# blobs
# --------------------------------------------------------------------------

def _fps(blob: bytes | None) -> float:
    if not blob or len(blob) < 8:
        return 0.0
    return struct.unpack("<d", blob[:8])[0]


def _resolucion(blob: bytes | None) -> tuple[int, int]:
    if not blob or len(blob) < 16:
        return (0, 0)
    w, h = struct.unpack(">2q", blob[:16])       # BIG-endian, no es un descuido
    return (w, h)


def _frames(txt) -> float:
    """'86400' o '92956|00a0246afd9de83f' -> fotogramas, con la parte decimal.

    El audio se puede colocar con precision de MUESTRA, no de fotograma, y
    Resolve guarda esa posicion como el entero, una barra y la fraccion de
    fotograma serializada como un double little-endian en hexadecimal.

    Costo siete items en Morsa: `int("92956|00a0...")` revienta y `CAST(Start AS
    INTEGER)` en SQL devuelve 92956 tirando la fraccion en silencio. Comprobado
    el 2026-08-19: en un item cortado, la fraccion de Start (0.769286) y la de
    Duration (0.230714) suman exactamente 1.0 — el corte cae en fotograma
    entero aunque ninguno de los dos numeros lo sea.
    """
    if txt is None:
        return 0.0
    t = str(txt).strip()
    if not t:
        return 0.0
    ent, _, frac = t.partition("|")
    try:
        v = float(int(ent))
    except ValueError:
        return 0.0
    if frac:
        try:
            v += struct.unpack("<d", bytes.fromhex(frac))[0]
        except (ValueError, struct.error):
            pass
    return v


def _extents(blob: bytes | None) -> tuple[float, float]:
    """(inicio_en_segundos, duracion_en_segundos)."""
    if not blob or len(blob) < 16:
        return (0.0, 0.0)
    return struct.unpack("<2d", blob[:16])


# --------------------------------------------------------------------------
# clasificar la fuente de un item
# --------------------------------------------------------------------------

def cargar_clasificador(config: Path | str | None):
    """Devuelve ruta -> 'camara' | 'lavalier' | 'musica' | 'desconocido'.

    Solo clasifica lo que el proyecto DECLARO en su project_config.json:
    `cameras[*].folder` / `filename_prefix` y las claves de `audio_folders`.
    Lo demas sale 'desconocido' y se reporta. Meter por defecto a dialogo lo que
    no se reconoce es lo que convertiria un stem de musica en un corte de habla.

    `carpetas_musica` es opcional y solo sirve para etiquetar bonito el informe:
    ninguna decision del subtitulado depende de ella.
    """
    carpetas_cam: list[str] = []
    prefijos: list[str] = []
    carpetas_lav: list[str] = []
    carpetas_mus: list[str] = []
    cfg = {}
    if config:
        p = Path(config)
        if p.exists():
            try:
                cfg = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                cfg = {}
    for cam in (cfg.get("cameras") or {}).values():
        if isinstance(cam, dict):
            if cam.get("folder"):
                carpetas_cam.append("/" + str(cam["folder"]).strip("/") + "/")
            if cam.get("filename_prefix"):
                prefijos.append(str(cam["filename_prefix"]))
    for k in (cfg.get("audio_folders") or {}):
        if k == "note":
            continue
        carpetas_lav.append("/" + str(k).strip("/") + "/")
    for k in (cfg.get("carpetas_musica") or []):
        carpetas_mus.append("/" + str(k).strip("/") + "/")

    def clasifica(ruta: str | None) -> str:
        r = ruta or ""
        if not r:
            return "desconocido"
        base = r.rsplit("/", 1)[-1]
        for c in carpetas_lav:
            if c in r:
                return "lavalier"
        for c in carpetas_mus:
            if c in r:
                return "musica"
        for c in carpetas_cam:
            if c in r:
                return "camara"
        for pre in prefijos:
            if base.startswith(pre):
                return "camara"
        return "desconocido"

    return clasifica


# --------------------------------------------------------------------------
# modelo
# --------------------------------------------------------------------------

@dataclass
class Item:
    nombre: str
    inicio: int                  # fotograma de timeline (redondeado)
    fin: int                     # fotograma de salida (redondeado)
    ruta: str = ""
    entrada: int = 0             # fotograma de entrada en la fuente
    media_inicio_s: float = 0.0
    clase: str = "desconocido"
    inicio_exacto: float = 0.0   # con la fraccion de fotograma, si la habia

    @property
    def duracion(self) -> int:
        return self.fin - self.inicio


@dataclass
class Pista:
    tipo: str                    # 'V' | 'A' | 'ST'
    indice: int                  # 1-based, como se ve en Resolve
    items: list[Item] = field(default_factory=list)

    @property
    def etiqueta(self) -> str:
        return f"{self.tipo}{self.indice}"


@dataclass
class Timeline:
    proyecto: str
    nombre: str
    fps: float
    ancho: int
    alto: int
    origen: int                  # fotograma del TC de inicio (86400 = 01:00:00:00)
    duracion_s: float
    canales_salida: int
    modificada: int              # epoch del ultimo guardado
    db_mtime: int
    pistas: list[Pista] = field(default_factory=list)

    # ---- conversiones ----

    def seg(self, frame: int) -> float:
        """Fotograma de timeline -> segundos desde el inicio de la timeline."""
        return (frame - self.origen) / self.fps

    def frame(self, seg: float) -> int:
        return self.origen + int(round(seg * self.fps))

    def tc(self, frame: int) -> str:
        n = max(1, round(self.fps))
        f, total = frame % n, frame // n
        return f"{total//3600:02d}:{(total//60)%60:02d}:{total%60:02d}:{f:02d}"

    # ---- acceso ----

    def pistas_de(self, tipo: str) -> list[Pista]:
        return [p for p in self.pistas if p.tipo == tipo]

    def items_de(self, tipo: str, clases: tuple[str, ...] | None = None) -> list[Item]:
        out = []
        for p in self.pistas_de(tipo):
            for it in p.items:
                if clases is None or it.clase in clases:
                    out.append(it)
        return out

    # ---- cortes ----

    @staticmethod
    def _bordes(items: list[Item], rango: tuple[int, int] | None) -> list[int]:
        s: set[int] = set()
        for it in items:
            if rango and (it.fin <= rango[0] or it.inicio >= rango[1]):
                continue
            for f in (it.inicio, it.fin):
                if rango is None or rango[0] <= f <= rango[1]:
                    s.add(f)
        return sorted(s)

    def cortes_imagen(self, rango=None) -> list[int]:
        return self._bordes(self.items_de("V"), rango)

    def cortes_audio(self, rango=None, clases=None) -> list[int]:
        return self._bordes(self.items_de("A", clases), rango)

    def cortes_dialogo(self, rango=None) -> list[int]:
        """Bordes del audio que el proyecto declaro como camara o lavalier."""
        return self.cortes_audio(rango, CLASES_DIALOGO)

    def cortes_sync(self, rango=None, tolerancia: int = TOLERANCIA_SYNC) -> list[int]:
        """Cortes de imagen que TAMBIEN cortan el dialogo.

        Son los unicos que mandan sobre un subtitulo. Medido en Morsa
        (2026-08-19): 84 de 366 cortes de imagen, separados 7.26 s de mediana, y
        caen dentro de una pausa de habla el 31 % de las veces contra un 9 % de
        un punto al azar.
        """
        audio = self.cortes_dialogo(rango)
        return [c for c in self.cortes_imagen(rango) if _cerca(c, audio, tolerancia)]

    def cortes_solo_imagen(self, rango=None,
                           tolerancia: int = TOLERANCIA_SYNC) -> list[int]:
        """Cortes de imagen sobre dialogo que sigue corriendo: es el voice over.

        Medido en Morsa: 282 de 366, separados 2.00 s de mediana. Partir un
        subtitulo aqui produce cues por debajo del minimo legible.
        """
        audio = self.cortes_dialogo(rango)
        return [c for c in self.cortes_imagen(rango)
                if not _cerca(c, audio, tolerancia)]

    # ---- estructura ----

    def bloques(self, hueco_min_s: float = 1.0,
                tipos: tuple[str, ...] = ("V", "A")) -> list[tuple[int, int]]:
        """Tramos con contenido, separados por huecos reales.

        Una timeline de trabajo no es continua: en Morsa `Cut 1.2 sonido ayan`
        mide 78:10 pero solo 24:56 son contenido, en 7 bloques. Lo que se
        entrego fue el PRIMER bloque. Confundir la duracion de la timeline con
        la del corte es un error de 61 minutos.
        """
        iv = sorted((it.inicio, it.fin)
                    for t in tipos for it in self.items_de(t))
        hueco = hueco_min_s * self.fps
        out: list[list[int]] = []
        for a, b in iv:
            if out and a - out[-1][1] <= hueco:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    def clases_sin_declarar(self) -> Counter:
        """Carpetas de audio que el proyecto no declaro. Se dicen, no se asumen."""
        c: Counter = Counter()
        for it in self.items_de("A"):
            if it.clase == "desconocido":
                r = it.ruta or "(sin ruta)"
                c["/".join(r.split("/")[:-1]) or r] += 1
        return c

    def avisos(self) -> list[str]:
        av = []
        if self.db_mtime > self.modificada + 2:
            av.append(
                f"el .db se toco a las {self.db_mtime} pero la timeline dice "
                f"{self.modificada}: puede haber cambios sin guardar que no se ven")
        sin = self.clases_sin_declarar()
        if sin:
            tot = sum(sin.values())
            av.append(f"{tot} items de audio sin clasificar en {len(sin)} carpetas "
                      f"({', '.join(list(sin)[:3])}) — no cuentan como dialogo")
        return av

    def resumen(self) -> str:
        v = self.pistas_de("V"); a = self.pistas_de("A"); s = self.pistas_de("ST")
        return (f"{self.nombre}: {self.ancho}x{self.alto} @ {self.fps:.3f} fps, "
                f"{self.duracion_s/60:.2f} min, "
                f"V={len(v)}({sum(len(p.items) for p in v)} items) "
                f"A={len(a)}({sum(len(p.items) for p in a)}) "
                f"ST={len(s)}({sum(len(p.items) for p in s)})")


def _cerca(x: int, orden: list[int], tol: int) -> bool:
    """¿Hay algun valor de `orden` (ordenado) a <= tol de x?"""
    import bisect
    i = bisect.bisect_left(orden, x)
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(orden) and abs(orden[j] - x) <= tol:
            return True
    return False


# --------------------------------------------------------------------------
# lectura
# --------------------------------------------------------------------------

def _conectar(db: Path):
    """Copia el .db a un temporal y lo abre en solo lectura sobre la copia.

    Se copia en vez de abrir el original con `immutable=1` por una razon: si
    Resolve escribe a mitad de la lectura, `immutable` promete al motor que el
    archivo no cambia y el resultado es corrupcion silenciosa. La copia cuesta
    unos milisegundos y garantiza que el archivo del editor no se toca.

    Si junto al .db hay `-wal`/`-shm` se copian tambien: llevan lo que Resolve
    aun no paso al archivo principal. Hoy las bases de disco estan en modo
    `delete` (medido en 21.1) y no los hay, pero `recoger_kit.sh` los junta de
    otra Mac, y sin ellos se leeria un proyecto mas viejo que el real.
    """
    db = Path(db)
    td = tempfile.mkdtemp(prefix="resolve_db_")
    copia = Path(td) / "Project.db"
    shutil.copy2(db, copia)
    for ext in ("-wal", "-shm"):
        extra = db.with_name(db.name + ext)
        if extra.exists():
            shutil.copy2(extra, copia.with_name(copia.name + ext))
    con = sqlite3.connect(f"file:{copia}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con, td


def _db_de(proyecto_o_db, base: str | None = None, prefs: Path = PREFS) -> Path:
    """Acepta el NOMBRE de un proyecto o la RUTA de un Project.db.

    Las funciones de lectura nacieron recibiendo el nombre del proyecto y
    buscandolo en la base activa. El lector del diagnostico necesita otra
    cosa: leer un Project.db que llega en una carpeta recogida en OTRA Mac, que
    no esta en ninguna base de esta. Un Path, o un texto que termina en `.db`,
    se toma como ruta; lo demas es un nombre de proyecto, como siempre.
    """
    if isinstance(proyecto_o_db, Path) or str(proyecto_o_db).endswith(".db"):
        p = Path(proyecto_o_db)
        if not p.exists():
            raise ErrorTimeline(f"No existe {p}")
        return p
    return ruta_project_db(str(proyecto_o_db), base, prefs)


def _tablas(con) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def listar_timelines(proyecto, base: str | None = None,
                     prefs: Path = PREFS) -> list[dict]:
    """Todas las timelines del proyecto, con `modificada` = ModTimeInSecs.

    `proyecto` puede ser el nombre o la ruta de un Project.db (ver `_db_de`).
    Si la secuencia falta (un Project.db a medias, o uno sintetico de prueba),
    la timeline sale igual, con fps y formato en cero: para saber que EXISTE
    no hace falta su formato.
    """
    db = _db_de(proyecto, base, prefs)
    con, td = _conectar(db)
    try:
        hay_seq = "Sm2Sequence" in _tablas(con)
        out = []
        for r in con.execute(
                "SELECT Name, Sequence, CreateTimeInSecs, ModTimeInSecs "
                "FROM Sm2Timeline ORDER BY CreateTimeInSecs"):
            q = con.execute(
                "SELECT FrameRate, Resolution, MediaExtents FROM Sm2Sequence "
                "WHERE Sm2Sequence_id=?", (r["Sequence"],)).fetchone() if hay_seq else None
            fps = _fps(q["FrameRate"]) if q else 0.0
            w, h = _resolucion(q["Resolution"]) if q else (0, 0)
            _, dur = _extents(q["MediaExtents"]) if q else (0.0, 0.0)
            out.append({"nombre": r["Name"], "fps": fps, "ancho": w, "alto": h,
                        "duracion_s": dur, "creada": r["CreateTimeInSecs"],
                        "modificada": r["ModTimeInSecs"]})
        return out
    finally:
        con.close()
        shutil.rmtree(td, ignore_errors=True)


def carpetas_media_pool(proyecto, base: str | None = None,
                        prefs: Path = PREFS) -> list[tuple[str, str | None]]:
    """Carpetas (bins) del Media Pool como (nombre, nombre_del_padre).

    La raiz ("Master") sale con padre None. El orden es el del arbol, padre
    antes que hijos y hermanos por nombre, para que un informe se lea como el
    panel del Media Pool.

    El padre sale de `Sm2MpFolder_Owner_id` y no de la tabla de union
    `Sm2MpFolder_Sm2MpFolder`: esa esta VACIA en 21.1 (medido en tres
    proyectos). Consultarla no da error, da un pool sin subcarpetas.
    """
    db = _db_de(proyecto, base, prefs)
    con, td = _conectar(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(Sm2MpFolder)")}
        col_padre = ("Sm2MpFolder_Owner_id" if "Sm2MpFolder_Owner_id" in cols
                     else "MpFolder")
        filas = con.execute(
            f"SELECT Sm2MpFolder_id AS id, Name AS nombre, {col_padre} AS padre "
            "FROM Sm2MpFolder").fetchall()
    finally:
        con.close()
        shutil.rmtree(td, ignore_errors=True)

    nombre = {f["id"]: (f["nombre"] or "") for f in filas}
    hijos: dict[str | None, list] = {}
    for f in filas:
        padre = f["padre"] if f["padre"] in nombre else None
        hijos.setdefault(padre, []).append(f["id"])
    out: list[tuple[str, str | None]] = []
    vistos: set[str] = set()

    def bajar(pid):
        for fid in sorted(hijos.get(pid, []), key=lambda i: nombre[i]):
            if fid in vistos:            # un ciclo no deberia existir; no cuelga
                continue
            vistos.add(fid)
            out.append((nombre[fid], nombre[pid] if pid else None))
            bajar(fid)

    bajar(None)
    return out


# --------------------------------------------------------------------------
# markers: blobs de BtLockableBlob (ver el docstring del modulo)
# --------------------------------------------------------------------------

# Codigo de color -> nombre. SOLO los comprobados o los que caen sin duda en su
# lugar de la paleta. Lo demas sale "?<codigo>": adivinar un color es peor que
# decir que no se sabe, porque el diagnostico usa el color como veredicto.
COLOR_MARKER = {2: "Blue", 4: "Cyan", 8: "Green", 16: "Yellow", 32: "Red",
                64: "Pink", 128: "Purple", 4096: "Sky", 8192: "Mint",
                # medidos el 2026-10-06 en los .drt de `construir` (Studio 21.1.0.17)
                16384: "Lemon", 32768: "Sand", 131072: "Cream"}


class ErrorBlob(ValueError):
    """Un blob que no tiene la forma medida. Se dice, no se inventa."""


def _varint(b: bytes, p: int) -> tuple[int, int]:
    r = s = 0
    while True:
        if p >= len(b):
            raise ErrorBlob("varint cortado")
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, p


def _protobuf(b: bytes) -> list[tuple[int, int, object]]:
    """Campos de un mensaje protobuf, sin esquema: (numero, tipo, valor)."""
    p, out = 0, []
    while p < len(b):
        clave, p = _varint(b, p)
        num, tipo = clave >> 3, clave & 7
        if tipo == 0:
            v, p = _varint(b, p)
        elif tipo == 2:
            n, p = _varint(b, p)
            if p + n > len(b):
                raise ErrorBlob("campo mas largo que el mensaje")
            v, p = b[p:p + n], p + n
        elif tipo == 1:
            v, p = b[p:p + 8], p + 8
        elif tipo == 5:
            v, p = b[p:p + 4], p + 4
        else:
            raise ErrorBlob(f"tipo de campo protobuf {tipo} no esperado")
        out.append((num, tipo, v))
    return out


def _zstd(datos: bytes) -> bytes:
    """Descomprime zstd con lo que haya, sin instalar nada.

    Python 3.14 lo trae (`compression.zstd`). En uno mas viejo se prueba el
    paquete `zstandard` y, al final, el binario `zstd`. Si no hay ninguno se
    dice: un marker que no se pudo leer no es un marker que no existe.
    """
    try:
        from compression import zstd  # type: ignore
        return zstd.decompress(datos)
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore
        return zstandard.ZstdDecompressor().decompress(datos)
    except ImportError:
        pass
    import subprocess
    try:
        r = subprocess.run(["zstd", "-dc"], input=datos, capture_output=True,
                           check=True)
        return r.stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise ErrorBlob(f"blob comprimido con zstd y no hay descompresor ({e})")


def _qvariantmap(fb: bytes) -> dict[str, bytes]:
    """QVariantMap de QDataStream (big-endian), solo con valores QByteArray."""
    if len(fb) < 8:
        raise ErrorBlob("FieldsBlob demasiado corto")
    _, n = struct.unpack(">II", fb[:8])
    p, out = 8, {}
    for _ in range(n):
        (kl,) = struct.unpack(">I", fb[p:p + 4]); p += 4
        k = fb[p:p + kl].decode("utf-16-be", errors="replace"); p += kl
        (tipo,) = struct.unpack(">I", fb[p:p + 4]); p += 4
        p += 1                                    # bandera de nulo
        if tipo != 12:                            # 12 = QByteArray
            raise ErrorBlob(f"clave {k!r} con tipo Qt {tipo}, se esperaba 12")
        (vl,) = struct.unpack(">I", fb[p:p + 4]); p += 4
        if vl == 0xFFFFFFFF:                      # QByteArray nulo
            out[k] = b""
            continue
        out[k] = fb[p:p + vl]; p += vl
    return out


def _desempacar(v: bytes) -> bytes:
    """uint32 etiqueta + uint32 largo + bandera (0x80 crudo, 0x81 zstd) + datos."""
    if len(v) < 9:
        return b""
    _, largo, bandera = struct.unpack(">IIB", v[:9])
    datos = v[9:8 + largo] if largo else v[9:]
    return _zstd(datos) if bandera & 1 else datos


def _sin_cabecera(v: bytes) -> bytes:
    """Quita el `uint32 version + uint32 largo` que precede al marker."""
    if len(v) >= 8:
        _, largo = struct.unpack(">II", v[:8])
        if largo == len(v) - 8:
            return v[8:]
    return v


def _marker(entrada: bytes) -> dict | None:
    pos = 0
    color = 0
    textos: list[str] = []
    custom = ""
    hay_datos = False
    for num, tipo, v in _protobuf(entrada):
        if num == 1 and tipo == 2:
            for n2, t2, v2 in _protobuf(v):       # vacio = posicion 0
                if n2 == 1 and t2 == 0:
                    pos = v2
        elif num == 2 and tipo == 2:
            for n2, t2, v2 in _protobuf(_sin_cabecera(v)):
                if n2 != 1 or t2 != 2:
                    continue
                hay_datos = True
                for n3, t3, v3 in _protobuf(v2):
                    if n3 == 1 and t3 == 0:
                        color = v3
                    elif n3 == 3 and t3 == 2:
                        textos.append(v3.decode("utf-8", errors="replace"))
                    elif n3 == 6 and t3 == 2:
                        custom = v3.decode("utf-8", errors="replace")
    if not hay_datos:
        return None
    nota = textos[0] if textos else ""
    dur_txt = textos[1] if len(textos) > 1 else ""
    nombre = textos[2] if len(textos) > 2 else ""
    try:
        duracion = int(dur_txt)
    except ValueError:
        duracion = None
    return {"frame": pos / 2.0, "posicion_cruda": pos,
            "color": COLOR_MARKER.get(color, f"?{color}"), "color_codigo": color,
            "nombre": nombre, "nota": nota, "duracion": duracion,
            "custom_data": custom}


def decodificar_markers(fields_blob: bytes | None) -> list[dict]:
    """FieldsBlob de un BtLockableBlob de secuencia o de item -> markers.

    Cada marker: frame (relativo, en fotogramas; puede ser x.5), color,
    color_codigo, nombre, nota, duracion, custom_data y posicion_cruda (lo que
    guarda Resolve, en medios fotogramas). Un blob sin markers da []. Uno con
    otra forma lanza ErrorBlob: callarlo se leeria como "no hay markers".
    """
    if not fields_blob:
        return []
    datos = _desempacar(_qvariantmap(bytes(fields_blob)).get("BlobData", b""))
    out: list[dict] = []
    for num, tipo, v in _protobuf(datos):
        if num != 2 or tipo != 2:
            continue
        for n2, t2, v2 in _protobuf(v):
            if n2 == 2 and t2 == 2:
                m = _marker(v2)
                if m:
                    out.append(m)
    out.sort(key=lambda m: m["posicion_cruda"])
    return out


def leer_markers(proyecto, timeline: str, *, base: str | None = None,
                 prefs: Path = PREFS, con_items: bool = True) -> list[dict]:
    """Markers de una timeline: los de la regla y, si `con_items`, los de sus items.

    Cada marker lleva `en` = "timeline" o "item", y `item` = nombre del item
    (vacio en los de la regla). `frame` es relativo: al inicio de la timeline
    en los de la regla, al inicio del clip en los de item.

    Si el Project.db no tiene `BtLockableBlob` (un esquema anterior a la
    medicion de 21.1) lanza NotImplementedError: ahi no se midio donde estan y
    devolver [] diria, en falso, que la timeline no tiene markers.
    """
    db = _db_de(proyecto, base, prefs)
    con, td = _conectar(db)
    try:
        tablas = _tablas(con)
        if "BtLockableBlob" not in tablas:
            raise NotImplementedError(
                "Este Project.db no tiene la tabla BtLockableBlob. En 21.1 los "
                "markers viven ahi (medido el 2026-09-23); en este esquema no se "
                "ha medido donde estan y no se adivina.")
        fila = con.execute("SELECT Sequence FROM Sm2Timeline WHERE Name=?",
                           (timeline,)).fetchone()
        if not fila:
            hay = [r[0] for r in con.execute("SELECT Name FROM Sm2Timeline")]
            raise ErrorTimeline(
                f"No hay timeline {timeline!r}. Hay: {', '.join(map(str, hay))}")
        seq = fila["Sequence"]
        out: list[dict] = []
        for r in con.execute(
                "SELECT FieldsBlob FROM BtLockableBlob "
                "WHERE DbType='Sm2SequenceLockableBlob' AND BlobOwner=?", (seq,)):
            for m in decodificar_markers(r["FieldsBlob"]):
                out.append({**m, "en": "timeline", "item": ""})
        if con_items and {"Sm2SequenceContainer", "Sm2SequenceContainer_Sm2TiTrack",
                          "Sm2TiItem"} <= tablas:
            for r in con.execute("""
                    SELECT i.Name AS item, b.FieldsBlob AS fb
                    FROM Sm2SequenceContainer c
                    JOIN Sm2SequenceContainer_Sm2TiTrack j
                         ON j.DbOwner = c.Sm2SequenceContainer_id
                    JOIN Sm2TiItem i ON i.Sm2TiTrack_id = j.DbAssociate
                    JOIN BtLockableBlob b
                         ON b.BlobOwner = i.Sm2TiItem_id
                        AND b.DbType = 'Sm2TiItemLockableBlob'
                    WHERE c.Sm2Sequence_id = ?""", (seq,)):
                for m in decodificar_markers(r["fb"]):
                    out.append({**m, "en": "item", "item": r["item"] or ""})
        return out
    finally:
        con.close()
        shutil.rmtree(td, ignore_errors=True)


def leer_timeline(proyecto: str, timeline: str, *, config: Path | str | None = None,
                  base: str | None = None, prefs: Path = PREFS) -> Timeline:
    """Devuelve la timeline con todas sus pistas e items, en fotogramas."""
    db = _db_de(proyecto, base, prefs)
    clasifica = cargar_clasificador(config)
    con, td = _conectar(db)
    try:
        fila = con.execute(
            "SELECT Name, Sequence, ModTimeInSecs FROM Sm2Timeline WHERE Name=?",
            (timeline,)).fetchone()
        if not fila:
            hay = [r[0] for r in con.execute("SELECT Name FROM Sm2Timeline")]
            raise ErrorTimeline(
                f"No hay timeline {timeline!r} en {proyecto!r}. Hay: {', '.join(hay)}")
        seq = fila["Sequence"]
        q = con.execute(
            "SELECT FrameRate, Resolution, MediaExtents, NumOutputAudioChannels "
            "FROM Sm2Sequence WHERE Sm2Sequence_id=?", (seq,)).fetchone()
        if not q:
            raise ErrorTimeline(f"La timeline {timeline!r} no tiene secuencia")
        fps = _fps(q["FrameRate"])
        if fps <= 0:
            raise ErrorTimeline(f"Frame rate ilegible en {timeline!r}")
        w, h = _resolucion(q["Resolution"])
        ini_s, dur_s = _extents(q["MediaExtents"])

        pistas: list[Pista] = []
        filas = con.execute("""
            SELECT j.DbPropertyName AS prop, j.DbIndex AS idx,
                   j.DbAssociate AS tid
            FROM Sm2SequenceContainer c
            JOIN Sm2SequenceContainer_Sm2TiTrack j
                 ON j.DbOwner = c.Sm2SequenceContainer_id
            WHERE c.Sm2Sequence_id = ?
            ORDER BY j.DbPropertyName, j.DbIndex""", (seq,)).fetchall()
        for f in filas:
            tipo = _TIPO.get(f["prop"])
            if not tipo:
                continue
            items = []
            for it in con.execute(
                    'SELECT Name, Start, Duration, "In", MediaFilePath, '
                    "MediaStartTime FROM Sm2TiItem WHERE Sm2TiTrack_id=? "
                    "ORDER BY CAST(Start AS INTEGER)", (f["tid"],)):
                ini_f = _frames(it["Start"])
                dur_f = _frames(it["Duration"])
                if dur_f <= 0:
                    continue
                ruta = it["MediaFilePath"] or ""
                # Se redondea el INICIO y el FIN por separado, no la duracion:
                # asi dos items pegados siguen pegados despues de redondear.
                items.append(Item(
                    nombre=it["Name"] or "",
                    inicio=int(round(ini_f)), fin=int(round(ini_f + dur_f)),
                    ruta=ruta, inicio_exacto=ini_f,
                    entrada=int(_frames(it["In"])) if it["In"] else 0,
                    media_inicio_s=float(it["MediaStartTime"] or 0.0),
                    clase=clasifica(ruta) if tipo == "A" else "video"))
            pistas.append(Pista(tipo=tipo, indice=f["idx"] + 1, items=items))

        return Timeline(
            proyecto=proyecto, nombre=timeline, fps=fps, ancho=w, alto=h,
            origen=int(round(ini_s * fps)), duracion_s=dur_s,
            canales_salida=int(q["NumOutputAudioChannels"] or 2),
            modificada=int(fila["ModTimeInSecs"] or 0),
            db_mtime=int(db.stat().st_mtime), pistas=pistas)
    finally:
        con.close()
        shutil.rmtree(td, ignore_errors=True)
