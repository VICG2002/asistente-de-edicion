#!/usr/bin/env python3
"""Detecta silencios reales en el audio de las entrevistas con ffmpeg.

Por que no basta con el transcript
----------------------------------
Los word-timings cacheados son `[palabra, t_inicio]` — NO traen `t_fin`. El
hueco entre dos palabras consecutivas mezcla dos cosas distintas: el tiempo que
dura la primera palabra y el silencio que viene despues. Estimar la duracion de
la palabra da pausas fantasma en las palabras largas y se come las pausas
cortas. `ffmpeg -af silencedetect` mide el silencio de verdad.

De donde sale el audio
----------------------
Del **lavalier sincronizado** cuando existe (audio limpio del entrevistado), y
si no, del A1 de la camara. La diferencia importa: en el A1 el ruido de sala
tapa las pausas y `silencedetect` no ve ninguna.

Los tiempos del lavalier se convierten a tiempo de VIDEO con el offset del
manifest (convencion del motor: `offset = audio_start - video_start`, asi que
`t_video = t_audio + offset`).

Salida: tabla `clip_silences(clip_id, start_sec, end_sec, dur_sec, from_kind,
from_path, threshold_db, created_at)`.

Uso:
    python3 bin/detect_pauses.py --root <disco>
    python3 bin/detect_pauses.py --root <disco> --min-dur 1.5 --umbral-db -35
    python3 bin/detect_pauses.py --root <disco> --clip-ids 2573,2574
"""

from __future__ import annotations

import argparse
import glob
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.guards import assert_selected, exigir_tablas, report_done  # noqa: E402
from lib import manifest  # noqa: E402

# Categorias donde una pausa es informacion util para el editor. En b-roll mudo
# "todo es silencio" y los markers serian ruido puro.
CATEGORIAS_CON_HABLA = (
    "entrevista", "entrevista-audio", "entrevista-solo-video",
    "dialogo-audio", "charla-equipo", "discurso", "accion-dialogo",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_silences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    start_sec REAL,
    end_sec REAL,
    dur_sec REAL,
    from_kind TEXT,          -- 'lavalier' | 'camara'
    from_path TEXT,
    threshold_db REAL,
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_silences_clip ON clip_silences(clip_id);
"""

_RE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_RE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_RE_RMS = re.compile(r"RMS level dB:\s*(-?[\d.]+)")

# Calibracion automatica del umbral. Un numero fijo de dB no vale para todo:
# el A1 de una camara grabando a 0 dB de ganancia y una grabadora de campo
# conservadora estan a 20 dB de distancia, y el mismo -35 dB que en una caza
# todas las pausas en la otra no ve ninguna.
#
# La regla sale de medir IMODAE (2026-08-07): RMS del clip -14.8 dB (es casi
# todo habla), y el umbral que reproducia las pausas reales estaba en -25 dB.
# O sea, 10 dB por debajo del nivel al que habla la gente. Comprobado en cuatro
# tomas largas de dos locutores distintos.
MARGEN_DB_DEFECTO = 10.0
UMBRAL_MIN_DB = -45.0   # mas abajo solo entra el ruido electronico
UMBRAL_MAX_DB = -18.0   # mas arriba se empieza a cortar habla baja
UMBRAL_SIN_MEDIDA = -30.0


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def nivel_rms(path: str, timeout: int = 600) -> float | None:
    """RMS del archivo en dB, con `ffmpeg -af astats`. None si no se puede.

    Se usa el RMS y no el 'Noise floor' que astats tambien imprime: el noise
    floor es el minimo instantaneo (en IMODAE daba -41 dB) y el ambiente real
    durante una pausa esta muy por encima. Calibrar con el minimo deja el umbral
    tan bajo que no detecta ninguna pausa.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-vn", "-i", path,
             "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    m = _RE_RMS.search(r.stderr or "")
    return float(m.group(1)) if m else None


def umbral_para(path: str, pedido, margen_db: float) -> tuple[float, str]:
    """(umbral en dB, como se decidio). `pedido` es un numero o 'auto'."""
    if pedido != "auto":
        return float(pedido), "fijado a mano"
    rms = nivel_rms(path)
    if rms is None:
        return UMBRAL_SIN_MEDIDA, "sin medida de RMS — default"
    u = max(UMBRAL_MIN_DB, min(UMBRAL_MAX_DB, rms - margen_db))
    return u, f"RMS {rms:.1f} dB − {margen_db:.0f} dB"


def detectar_silencios(path: str, umbral_db: float, min_dur: float,
                       timeout: int = 600) -> list:
    """Corre ffmpeg silencedetect y devuelve [(start, end), ...] en tiempo del
    ARCHIVO. Devuelve [] si ffmpeg falla (archivo no decodificable, etc)."""
    # `-vn`: sin esto ffmpeg decodifica tambien el video, y sobre 4K eso es
    # minutos por clip para medir algo que solo mira el audio. Con lavaliers no
    # se noto nunca —son WAV— pero al medir sobre el A1 de camara la diferencia
    # es de dos ordenes de magnitud (IMODAE 2026-08-07).
    cmd = [
        "ffmpeg", "-nostdin", "-v", "info", "-vn", "-i", path,
        "-af", f"silencedetect=noise={umbral_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    # silencedetect escribe a stderr
    out = []
    pendiente = None
    for linea in (r.stderr or "").splitlines():
        ms = _RE_START.search(linea)
        if ms:
            pendiente = float(ms.group(1))
            continue
        me = _RE_END.search(linea)
        if me and pendiente is not None:
            fin = float(me.group(1))
            if fin > pendiente:
                out.append((pendiente, fin))
            pendiente = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--umbral-db", default="auto",
                    help="Nivel bajo el cual se considera silencio (dB). "
                         "'auto' (default) lo calibra por clip a partir de su "
                         "RMS; un numero lo fija a mano para todos.")
    ap.add_argument("--margen-db", type=float, default=MARGEN_DB_DEFECTO,
                    help=f"Solo con --umbral-db auto: cuantos dB por debajo "
                         f"del RMS del clip esta el silencio "
                         f"(default {MARGEN_DB_DEFECTO:.0f}).")
    ap.add_argument("--min-dur", type=float, default=1.2,
                    help="Duracion minima del silencio en segundos. Default 1.2 "
                         "— por debajo son respiraciones, no puntos de corte.")
    ap.add_argument("--clip-ids", default="",
                    help="Lista de clip_id separados por coma. Vacio = todas "
                         "las entrevistas del proyecto.")
    ap.add_argument("--seleccion", choices=["categorias", "takes"],
                    default="categorias",
                    help="Que clips medir. 'categorias' (default) = los que "
                         "clip_descriptions marca como habla — el caso "
                         "documental. 'takes' = los que dicen un texto "
                         "guionizado (clip_takes), para comercial y capsulas: "
                         "ahi no hay categorias porque no hay entrevistas.")
    ap.add_argument("--solo-camara", action="store_true",
                    help="Ignorar los lavaliers y medir sobre el A1 de camara.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    conn.executescript(SCHEMA)

    solo = set()
    if args.clip_ids.strip():
        solo = {int(x) for x in args.clip_ids.split(",") if x.strip().isdigit()}

    if args.seleccion == "takes":
        exigir_tablas(conn, {
            "clip_takes": "python3 bin/derive_takes.py --root <disco>",
        }, "detect_pauses --seleccion takes")
        rows = conn.execute("""
            SELECT c.id, c.path, c.duration_sec, 'toma guionizada'
            FROM clips c
            JOIN clip_takes t ON t.clip_id = c.id
            WHERE c.file_kind = 'video' AND c.index_status = 'ok'
            ORDER BY c.id
        """).fetchall()
        pista = ("Correr antes derive_takes.py — sin grupos de tomas no hay "
                 "de donde saber que clips dicen el texto.")
    else:
        exigir_tablas(conn, {
            "clip_descriptions": "python3 bin/derive_video_categories.py --root <disco>",
        }, "detect_pauses --seleccion categorias")
        placeholders = ",".join("?" * len(CATEGORIAS_CON_HABLA))
        rows = conn.execute(f"""
            SELECT c.id, c.path, c.duration_sec, IFNULL(d.category,'')
            FROM clips c
            JOIN clip_descriptions d ON d.clip_id = c.id
            WHERE c.file_kind = 'video' AND c.index_status = 'ok'
              AND d.category IN ({placeholders})
            ORDER BY c.id
        """, CATEGORIAS_CON_HABLA).fetchall()
        pista = ("Correr antes derive_video_categories.py — sin categorias no "
                 "hay de donde saber que clips son entrevista. En una pieza "
                 "con guion no habra ninguna: usa --seleccion takes.")
    if solo:
        rows = [r for r in rows if r[0] in solo]
    assert_selected(rows, "clips con habla para medir silencios", filters={
        "--root": str(root), "--seleccion": args.seleccion,
        "--clip-ids": args.clip_ids or "(todos)",
    }, hint=pista)

    # Mejor par de sync por video: el lavalier da el audio limpio.
    lav = {}
    if not args.solo_camara:
        try:
            for vid, apath, off, conf in conn.execute("""
                SELECT p.video_clip_id, ca.path, p.offset_sec, p.confidence
                FROM audio_sync_pairs p
                JOIN clips ca ON ca.id = p.audio_clip_id
                ORDER BY p.confidence DESC
            """):
                lav.setdefault(vid, (apath, off or 0.0, conf or 0.0))
        except sqlite3.OperationalError:
            pass  # proyecto sin sync todavia

    n_clips = n_sil = 0
    n_lav = n_cam = n_fallo = 0
    t0 = time.time()
    for cid, vpath, dur, cat in rows:
        conn.execute("DELETE FROM clip_silences WHERE clip_id=?", (cid,))
        par = lav.get(cid)
        if par:
            apath, offset, _conf = par
            kind, src_path = "lavalier", apath
        else:
            offset = 0.0
            kind, src_path = "camara", vpath

        umbral, como = umbral_para(src_path, args.umbral_db, args.margen_db)
        crudos = detectar_silencios(src_path, umbral, args.min_dur)
        if not crudos and not Path(src_path).exists():
            n_fallo += 1
            print(f"  clip {cid}: fuente no encontrada ({src_path})")
            continue

        # A tiempo de VIDEO y recorte al rango del clip.
        vdur = float(dur or 0)
        puestos = 0
        for s_a, e_a in crudos:
            s_v = s_a + offset
            e_v = e_a + offset
            if vdur > 0:
                s_v = max(0.0, s_v)
                e_v = min(vdur, e_v)
            if e_v - s_v < args.min_dur:
                continue
            conn.execute(
                "INSERT INTO clip_silences (clip_id, start_sec, end_sec, dur_sec,"
                " from_kind, from_path, threshold_db, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (cid, s_v, e_v, e_v - s_v, kind, src_path,
                 umbral, time.time()))
            puestos += 1
        n_sil += puestos
        n_clips += 1
        if kind == "lavalier":
            n_lav += 1
        else:
            n_cam += 1
        print(f"  clip {cid} [{cat}] {kind}: {puestos} silencio(s) "
              f"de {len(crudos)} crudos  (umbral {umbral:.1f} dB, {como})")

    conn.commit()
    conn.close()
    print(f"\nSilencios detectados en {n_clips} clips ({time.time()-t0:.0f}s)")
    print(f"  desde lavalier : {n_lav}")
    print(f"  desde camara   : {n_cam}  (sin par de sync — pausas menos fiables)")
    if n_fallo:
        print(f"  fuentes ausentes: {n_fallo}")
    report_done("detect_pauses", clips=n_clips, silencios=n_sil,
                lavalier=n_lav, camara=n_cam, fallos=n_fallo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
