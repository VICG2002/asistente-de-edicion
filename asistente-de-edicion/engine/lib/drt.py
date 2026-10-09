"""Leer un .drt (timeline exportada por Resolve) sin abrir Resolve.

PARA QUE SIRVE
Es uno de los canales de regreso candidatos del plugin de Resolve Free: el
editor (o `Timeline:Export`) saca la timeline a un .drt y el motor la lee
fuera. El diagnostico de la Fase 0 lo usa para confirmar que un export existe
Y se puede leer, y para leer los markers de la timeline `DIAG MARCAS <clave>`
cuando Project.db no alcanza.

POR QUE NO SE REUSA `bin/import_drt_ground_truth.py`
Aquel lee solo clips sueltos (no sabe en que pista vive cada uno), usa
`int(Start)` (revienta con la fraccion de fotograma del audio) y escribe en el
manifest. Aqui solo se LEE, y se devuelve la timeline con su estructura.

EL FORMATO, MEDIDO EL 2026-09-24
Sobre un .drt real de 20.2.3 (DbPrjVer 15) y un .drp de 21.0.1 (DbPrjVer 17),
los dos leidos en solo lectura. Un .drt es un ZIP:

  project.xml                  SM_Project; en 21.x lleva <LockableBlobMap>
  MediaPool/Master/.../MpFolder.xml
                               Sm2MpFolder > MediaVec > Sm2MpTimelineClip
                               > TimelineSharedHandle > Sm2Timeline (Name)
                               > Sequence > Sm2Sequence DbId=... (FrameRate,
                               MediaExtents: los mismos blobs que Project.db)
  SeqContainer/<id>.xml        Sm2SequenceContainer > {Video,Audio,Subtitle}TrackVec
                               > Element > Sm2TiTrack (UserDefinedName, Sequence)
                               > Items > Element > Sm2TiVideoClip | Sm2TiAudioClip...
                               (Name, Start, Duration, In, MediaFilePath)

Cada XML abre con `<!--DbAppVer="20.2.3.0006" DbPrjVer="15"-->`: es la unica
forma de saber que Resolve lo escribio sin abrir Resolve.

DOS TRAMPAS
  1. No es XML bien formado: hay etiquetas como `<ListMgt::LmVersionTable>`.
     `ElementTree` revienta en la linea 104 del primer .drt real. Se reescribe
     `Prefijo::` como `Prefijo__` antes de parsear.
  2. `Start`/`Duration`/`In` son TEXTO y el audio puede traer fraccion de
     fotograma ("92956|00a0..."). Se lee con el mismo `_frames` que Project.db.

MARKERS
Los de la timeline y los de los items NO van en `MarkersBA` (vacio en todos
los items medidos): van en `<LockableBlobMap>` como `Sm2SequenceLockableBlob`
(BlobOwner = DbId de la Sm2Sequence) y `Sm2TiItemLockableBlob` (BlobOwner = el
DbId del item), con el MISMO blob que Project.db guarda en BtLockableBlob. Se
decodifican con `timeline_resolve.decodificar_markers`. Medido en el .drp de
21.0.1; que un .drt de 21.1 los lleve igual esta POR MEDIR CON EL DRT DEL
DIAGNOSTICO (el unico .drt real de esta Mac no tenia markers). Por eso el
lector es tolerante: si no encuentra el mapa devuelve cero markers y lo dice
en `avisos`, y si un `MarkersBA` trae datos lo reporta sin inventar su formato.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lib.timeline_resolve import ErrorBlob, _frames, decodificar_markers

_VECS = (("VideoTrackVec", "V", "Video"), ("AudioTrackVec", "A", "Audio"),
         ("SubtitleTrackVec", "ST", "Subtitle"))


class ErrorDrt(RuntimeError):
    """El archivo no es un .drt legible. Se dice por que."""


@dataclass
class MarkerDrt:
    frame: float                 # relativo: a la timeline o al inicio del clip
    color: str
    nombre: str
    nota: str
    custom_data: str = ""
    duracion: int | None = None
    color_codigo: int = 0
    en: str = "timeline"         # 'timeline' | 'item'
    item: str = ""               # nombre del item, si es de item


@dataclass
class ItemDrt:
    pista: str                   # 'V1', 'A2'...
    nombre: str
    inicio: float                # fotogramas de timeline (Start)
    duracion: float              # fotogramas (Duration)
    entrada: float               # fotograma de entrada en la fuente (In)
    ruta: str                    # MediaFilePath
    clase_xml: str = ""          # Sm2TiVideoClip, Sm2TiAudioClip...
    id: str = ""
    markers: list[MarkerDrt] = field(default_factory=list)


@dataclass
class PistaDrt:
    tipo: str                    # 'V' | 'A' | 'ST'
    indice: int                  # 1-based, como se ve en Resolve
    nombre: str                  # UserDefinedName, o el que pone Resolve
    nombre_propio: bool = False  # True si el editor la renombro
    items: list[ItemDrt] = field(default_factory=list)

    @property
    def etiqueta(self) -> str:
        return f"{self.tipo}{self.indice}"


@dataclass
class Drt:
    nombre: str                  # nombre de la timeline
    db_app_ver: str = ""
    db_prj_ver: str = ""
    fps: float = 0.0
    origen: int = 0              # fotograma del TC de inicio
    duracion_s: float = 0.0
    pistas: list[PistaDrt] = field(default_factory=list)
    markers: list[MarkerDrt] = field(default_factory=list)   # los de la regla
    avisos: list[str] = field(default_factory=list)

    @property
    def items(self) -> list[ItemDrt]:
        return [it for p in self.pistas for it in p.items]

    @property
    def markers_items(self) -> list[MarkerDrt]:
        return [m for it in self.items for m in it.markers]

    def todos_los_markers(self) -> list[MarkerDrt]:
        return list(self.markers) + self.markers_items


# --------------------------------------------------------------------------
# XML
# --------------------------------------------------------------------------

_PREFIJO = re.compile(r"(</?)([A-Za-z_]\w*)::")
_VERSION = re.compile(r'DbAppVer="([^"]*)"\s+DbPrjVer="([^"]*)"')


def _xml(texto: str) -> ET.Element:
    """Parsea tolerando `Prefijo::Etiqueta`, que no es XML valido."""
    return ET.fromstring(_PREFIJO.sub(r"\1\2__", texto))


def _txt(e: ET.Element | None, etiqueta: str) -> str:
    if e is None:
        return ""
    x = e.find(etiqueta)
    return (x.text or "").strip() if x is not None else ""


def _hex(txt: str) -> bytes:
    try:
        return bytes.fromhex(txt.strip())
    except ValueError:
        return b""


def _fps(txt: str) -> float:
    b = _hex(txt)
    return struct.unpack("<d", b[:8])[0] if len(b) >= 8 else 0.0


def _extents(txt: str) -> tuple[float, float]:
    b = _hex(txt)
    return struct.unpack("<2d", b[:16]) if len(b) >= 16 else (0.0, 0.0)


def _marker(m: dict, en: str, item: str = "") -> MarkerDrt:
    return MarkerDrt(frame=m["frame"], color=m["color"], nombre=m["nombre"],
                     nota=m["nota"], custom_data=m["custom_data"],
                     duracion=m["duracion"], color_codigo=m["color_codigo"],
                     en=en, item=item)


# --------------------------------------------------------------------------
# lectura
# --------------------------------------------------------------------------

def _abrir(ruta: Path) -> dict[str, str]:
    """Nombre interno -> texto de cada XML del zip. Nada se extrae a disco."""
    try:
        with zipfile.ZipFile(ruta) as z:
            return {n: z.read(n).decode("utf-8", errors="replace")
                    for n in z.namelist() if n.lower().endswith(".xml")}
    except zipfile.BadZipFile as e:
        raise ErrorDrt(f"{ruta} no es un zip ({e}); un .drt lo es")


def leer_drt(ruta: Path | str) -> Drt:
    """Timeline, pistas, items y markers de un .drt. Nunca escribe."""
    ruta = Path(ruta)
    if not ruta.exists():
        raise ErrorDrt(f"No existe {ruta}")
    xmls = _abrir(ruta)
    if not xmls:
        raise ErrorDrt(f"{ruta} no trae ningun XML")
    arboles: dict[str, ET.Element] = {}
    avisos: list[str] = []
    for n, t in xmls.items():
        try:
            arboles[n] = _xml(t)
        except ET.ParseError as e:
            avisos.append(f"{n}: XML ilegible ({e})")

    app = prj = ""
    for t in xmls.values():
        m = _VERSION.search(t[:400])
        if m:
            app, prj = m.groups()
            break

    # --- la timeline: Sm2Timeline en el MediaPool ---
    nombre, seq_id, fps, ini_s, dur_s = "", "", 0.0, 0.0, 0.0
    for n, r in arboles.items():
        for tl in r.iter("Sm2Timeline"):
            nm = _txt(tl, "Name")
            if not nm:
                continue
            s = tl.find("Sequence/Sm2Sequence")
            nombre = nm
            if s is not None:
                seq_id = s.get("DbId", "")
                fps = _fps(_txt(s, "FrameRate"))
                ini_s, dur_s = _extents(_txt(s, "MediaExtents"))
            break
        if nombre:
            break
    if not nombre:
        proj = next((r for n, r in arboles.items() if n.endswith("project.xml")), None)
        nombre = _txt(proj, "ProjectName")
        if nombre:
            avisos.append("sin Sm2Timeline en el MediaPool: el nombre sale de "
                          "project.xml/ProjectName")

    # --- pistas: el SeqContainer de esa secuencia ---
    contenedores = [r for n, r in arboles.items() if n.startswith("SeqContainer/")]
    cont = None
    for r in contenedores:
        seqs = {(_txt(t, "Sequence")) for t in r.iter("Sm2TiTrack")}
        if seq_id and seq_id in seqs:
            cont = r
            break
    if cont is None and contenedores:
        cont = contenedores[0]
        if len(contenedores) > 1:
            avisos.append(f"{len(contenedores)} SeqContainer y ninguno cuadra con "
                          "la secuencia: se leyo el primero")
    pistas: list[PistaDrt] = []
    por_id: dict[str, ItemDrt] = {}
    if cont is not None:
        for vec, tipo, largo in _VECS:
            v = cont.find(vec)
            if v is None:
                continue
            for i, el in enumerate(v.findall("Element"), start=1):
                tr = el.find("Sm2TiTrack")
                if tr is None:
                    continue
                propio = _txt(tr, "UserDefinedName")
                p = PistaDrt(tipo=tipo, indice=i, nombre=propio or f"{largo} {i}",
                             nombre_propio=bool(propio))
                for it_el in tr.findall("Items/Element"):
                    for c in it_el:
                        if not c.tag.startswith("Sm2Ti"):
                            continue
                        it = ItemDrt(
                            pista=p.etiqueta, nombre=_txt(c, "Name"),
                            inicio=_frames(_txt(c, "Start")),
                            duracion=_frames(_txt(c, "Duration")),
                            entrada=_frames(_txt(c, "In")),
                            ruta=_txt(c, "MediaFilePath"),
                            clase_xml=c.tag, id=c.get("DbId", ""))
                        mba = _txt(c, "MarkersBA")
                        if mba:
                            avisos.append(f"{p.etiqueta} {it.nombre!r}: MarkersBA con "
                                          f"{len(mba)//2} bytes, formato no medido")
                        p.items.append(it)
                        if it.id:
                            por_id[it.id] = it
                pistas.append(p)
    else:
        avisos.append("sin SeqContainer: la timeline no trae pistas")

    # --- markers: LockableBlobMap, en cualquier XML ---
    markers: list[MarkerDrt] = []
    vio_mapa = False
    for n, r in arboles.items():
        for tipo_blob, en in (("Sm2SequenceLockableBlob", "timeline"),
                              ("Sm2TiItemLockableBlob", "item")):
            for b in r.iter(tipo_blob):
                vio_mapa = True
                dueno = _txt(b, "BlobOwner")
                try:
                    ms = decodificar_markers(_hex(_txt(b, "FieldsBlob")))
                except (ErrorBlob, struct.error) as e:
                    avisos.append(f"{tipo_blob} de {dueno}: blob ilegible ({e})")
                    continue
                if en == "timeline":
                    if seq_id and dueno and dueno != seq_id:
                        continue      # otra secuencia (un compound clip, p. ej.)
                    markers.extend(_marker(m, "timeline") for m in ms)
                else:
                    it = por_id.get(dueno)
                    if it is None:
                        continue      # item que ya no esta en la timeline
                    it.markers.extend(_marker(m, "item", it.nombre) for m in ms)
    if not vio_mapa:
        avisos.append("sin LockableBlobMap: este .drt no trae markers legibles "
                      "(o no tiene markers)")
    markers.sort(key=lambda m: m.frame)

    return Drt(nombre=nombre, db_app_ver=app, db_prj_ver=prj, fps=fps,
               origen=int(round(ini_s * fps)) if fps else 0, duracion_s=dur_s,
               pistas=pistas, markers=markers, avisos=avisos)
