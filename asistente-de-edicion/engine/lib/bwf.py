"""Lector de metadata de Broadcast Wave (BWF): chunks `bext` e `iXML`.

Para que sirve
--------------
El motor asumia hasta ahora que un WAV externo es **un lavalier en estereo**.
Las grabadoras de campo (Zoom F3/F6/H6, Sound Devices MixPre, Tascam) escriben
otra cosa: un WAV **polifonico** de 2 a 8 canales donde cada canal es un
microfono distinto, con los nombres de pista en el chunk `iXML` y el timecode
real de grabacion en `bext`.

Dos cosas que esto habilita:

1. **Sync por timecode.** `bext.TimeReference` es la posicion de la primera
   muestra en la linea de tiempo del dia, en muestras. Si la camara tambien
   graba TC, el offset sale de una resta — mas barato y mas exacto que
   correlacionar waveforms.
2. **Un lavalier por canal.** Saber que el canal 3 se llama "ESCALADOR_A" convierte
   el sync de (video <-> archivo) en (video <-> canal).

Sin dependencias: `struct` sobre el RIFF. Soporta RF64 (los archivos de campo
pasan de 4 GB con facilidad) y no toca el archivo — solo lo lee.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

# Un bext v0 mide 602 bytes antes del CodingHistory.
_BEXT_FIJO = struct.Struct("<256s32s32s10s8sIIH64s")
_BEXT_RESERVADO = 190   # v0: 254; v1+: 190 + 6 campos de loudness. Ver abajo.

MAX_CHUNK_LEER = 8 * 1024 * 1024   # no cargar a memoria un chunk absurdo


class BwfInfo:
    """Lo que se pudo leer de un WAV. Todos los campos pueden ser None."""

    __slots__ = ("path", "sample_rate", "channels", "bits", "duration_sec",
                 "originator", "origination_date", "origination_time",
                 "time_reference", "coding_history", "track_names",
                 "project", "scene", "take", "note", "is_rf64", "chunks")

    def __init__(self, path):
        self.path = str(path)
        self.sample_rate = None
        self.channels = None
        self.bits = None
        self.duration_sec = None
        self.originator = None
        self.origination_date = None
        self.origination_time = None
        self.time_reference = None      # en MUESTRAS desde medianoche
        self.coding_history = None
        self.track_names = {}           # {indice_de_canal (1-based): nombre}
        self.project = None
        self.scene = None
        self.take = None
        self.note = None
        self.is_rf64 = False
        self.chunks = []

    @property
    def tc_segundos(self):
        """`bext.TimeReference` en segundos desde medianoche, o None.

        Es el ancla para el sync por timecode: si la camara tambien graba TC,
        el offset entre ambos es una resta.
        """
        if self.time_reference is None or not self.sample_rate:
            return None
        return self.time_reference / float(self.sample_rate)

    @property
    def es_polifonico(self):
        return bool(self.channels and self.channels > 2)

    def nombre_de_canal(self, idx):
        """Nombre de pista del canal `idx` (1-based), o 'ch<idx>'."""
        return self.track_names.get(idx) or f"ch{idx}"

    def __repr__(self):  # pragma: no cover - depuracion
        return (f"<BwfInfo {Path(self.path).name} {self.channels}ch "
                f"{self.sample_rate}Hz tc={self.tc_segundos} "
                f"tracks={len(self.track_names)}>")


def _leer_chunks(f, tam_total):
    """Itera (chunk_id, offset_datos, tamano) sin cargar los datos."""
    pos = f.tell()
    fin = tam_total
    while pos + 8 <= fin:
        f.seek(pos)
        cab = f.read(8)
        if len(cab) < 8:
            return
        cid = cab[:4]
        try:
            tam = struct.unpack("<I", cab[4:8])[0]
        except struct.error:
            return
        yield cid, pos + 8, tam
        # los chunks van alineados a par
        pos = pos + 8 + tam + (tam & 1)


def _texto(b):
    if not b:
        return None
    s = b.split(b"\x00", 1)[0]
    for enc in ("utf-8", "latin-1"):
        try:
            t = s.decode(enc).strip()
            return t or None
        except UnicodeDecodeError:
            continue
    return None


def _parse_ixml(xml_bytes):
    """Nombres de pista + escena/toma del iXML.

    Se parsea con regex a proposito: los iXML de las grabadoras traen namespaces
    inconsistentes y campos propietarios que hacen fallar a un parser estricto,
    y aqui interesa extraer datos, no validar el documento.
    """
    out = {"track_names": {}, "project": None, "scene": None, "take": None,
           "note": None}
    try:
        xml = xml_bytes.decode("utf-8", errors="replace")
    except Exception:
        return out

    for etiqueta, clave in (("PROJECT", "project"), ("SCENE", "scene"),
                            ("TAKE", "take"), ("NOTE", "note")):
        m = re.search(rf"<{etiqueta}>(.*?)</{etiqueta}>", xml,
                      re.IGNORECASE | re.DOTALL)
        if m:
            v = m.group(1).strip()
            if v:
                out[clave] = v

    # <TRACK><CHANNEL_INDEX>3</CHANNEL_INDEX><NAME>ESCALADOR_A</NAME></TRACK>
    for m in re.finditer(r"<TRACK>(.*?)</TRACK>", xml,
                         re.IGNORECASE | re.DOTALL):
        bloque = m.group(1)
        mi = re.search(r"<CHANNEL_INDEX>\s*(\d+)\s*</CHANNEL_INDEX>", bloque,
                       re.IGNORECASE)
        mn = re.search(r"<NAME>(.*?)</NAME>", bloque, re.IGNORECASE | re.DOTALL)
        if mi:
            idx = int(mi.group(1))
            nombre = (mn.group(1).strip() if mn else "")
            if nombre:
                out["track_names"][idx] = nombre
    return out


def leer(path) -> BwfInfo:
    """Lee un WAV/BWF/RF64. Nunca levanta: si algo no se puede leer, ese campo
    se queda en None. Un WAV corrupto no debe tumbar el indexado."""
    info = BwfInfo(path)
    p = Path(path)
    try:
        tam_archivo = p.stat().st_size
    except OSError:
        return info

    try:
        with p.open("rb") as f:
            cab = f.read(12)
            if len(cab) < 12 or cab[8:12] != b"WAVE":
                return info
            if cab[:4] == b"RF64":
                info.is_rf64 = True
            elif cab[:4] != b"RIFF":
                return info

            tam_datos_real = None
            for cid, off, tam in _leer_chunks(f, tam_archivo):
                info.chunks.append(cid.decode("ascii", "replace").strip())

                if cid == b"ds64" and tam >= 24:
                    # RF64: los tamanos reales de 64 bits viven aqui.
                    f.seek(off)
                    d = f.read(24)
                    if len(d) == 24:
                        _riff64, data64, _muestras = struct.unpack("<QQQ", d)
                        tam_datos_real = data64

                elif cid == b"fmt " and tam >= 16:
                    f.seek(off)
                    d = f.read(min(tam, 40))
                    if len(d) >= 16:
                        (_fmt, canales, sr, _bps, _align,
                         bits) = struct.unpack("<HHIIHH", d[:16])
                        info.channels = canales or None
                        info.sample_rate = sr or None
                        info.bits = bits or None

                elif cid == b"data":
                    real = tam_datos_real if (info.is_rf64 and tam == 0xFFFFFFFF
                                              and tam_datos_real) else tam
                    if info.sample_rate and info.channels and info.bits:
                        bytes_por_frame = info.channels * max(1, info.bits // 8)
                        if bytes_por_frame:
                            info.duration_sec = real / float(
                                bytes_por_frame * info.sample_rate)

                elif cid == b"bext" and tam >= _BEXT_FIJO.size:
                    f.seek(off)
                    d = f.read(min(tam, MAX_CHUNK_LEER))
                    if len(d) >= _BEXT_FIJO.size:
                        (desc, orig, origref, fecha, hora, tr_lo, tr_hi,
                         version, _umid) = _BEXT_FIJO.unpack(d[:_BEXT_FIJO.size])
                        info.originator = _texto(orig)
                        info.origination_date = _texto(fecha)
                        info.origination_time = _texto(hora)
                        info.time_reference = (tr_hi << 32) | tr_lo
                        # El CodingHistory viene despues del reservado; su largo
                        # depende de la version del bext.
                        reservado = 254 if version == 0 else _BEXT_RESERVADO
                        ini = _BEXT_FIJO.size + reservado
                        if len(d) > ini:
                            info.coding_history = _texto(d[ini:])
                        if not info.note:
                            info.note = _texto(desc)

                elif cid in (b"iXML", b"ixml", b"IXML") and tam:
                    f.seek(off)
                    d = f.read(min(tam, MAX_CHUNK_LEER))
                    ix = _parse_ixml(d)
                    info.track_names.update(ix["track_names"])
                    info.project = info.project or ix["project"]
                    info.scene = info.scene or ix["scene"]
                    info.take = info.take or ix["take"]
                    info.note = ix["note"] or info.note
    except OSError:
        return info
    return info


def offset_por_timecode(bwf: BwfInfo, video_tc_sec):
    """Offset en segundos con la convencion del motor: `audio_start - video_start`.

    Devuelve None si falta cualquiera de los dos timecodes. NO adivina: un
    offset inventado es peor que no tener offset — coloca el audio en el lugar
    equivocado y el editor lo descubre a mitad del corte.
    """
    a = bwf.tc_segundos if bwf else None
    if a is None or video_tc_sec is None:
        return None
    return a - float(video_tc_sec)


_TC_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{1,3})$")


def timecode_a_segundos(tc, fps=None):
    """'01:23:45:12' -> segundos. `fps` para los frames; sin el, se ignoran.

    Acepta ';' como separador (drop-frame). No compensa drop-frame: para las
    distancias que nos importan (segundos de offset) el error es de milesimas,
    y fingir precision que no tenemos seria peor.
    """
    if not tc:
        return None
    m = _TC_RE.match(str(tc).strip())
    if not m:
        return None
    h, mi, s, fr = (int(x) for x in m.groups())
    base = h * 3600 + mi * 60 + s
    if fps and fps > 0:
        base += fr / float(fps)
    return base
