#!/usr/bin/env python3
"""Indexa las PISTAS de los audios externos y, opcionalmente, separa cada canal.

El problema
-----------
El motor asumia que un WAV externo es **un lavalier en estereo**. Las grabadoras
de campo (Zoom F3/F6/H6, MixPre, Tascam) escriben un WAV **polifonico** de 2 a 8
canales donde cada canal es un microfono distinto. Con ese supuesto:

  - el sync empareja el ARCHIVO con el video, no el canal, asi que el "lavalier
    de ESCALADOR_A" trae encima la voz de todos los demas;
  - los nombres de pista (`iXML`) se pierden, y con ellos el unico dato que
    dice que canal es de quien;
  - el `bext.TimeReference` se ignora, que es timecode de grabacion REAL y
    permite sincronizar por resta en vez de por correlacion de waveform.

Que hace
--------
1. Lee `bext` e `iXML` de cada WAV del manifest (`lib/bwf.py`, sin dependencias).
2. Escribe una fila por canal en `audio_tracks`, con su nombre de pista.
3. Con `--split`, extrae cada canal a un WAV mono en
   `<disco>/.cinema_assistant/audio_split/`. **No toca el archivo original** —
   respeta la regla de no modificar material fuente.
4. Rellena `clips.timecode` desde `bext` cuando ffprobe no lo trajo.

Los monos son lo que despues se sincroniza y se mergea: `AutoSyncAudio` liga el
archivo completo, y el mapeo de canales es de solo lectura por API, asi que
separar antes es la unica via.

Uso:
    python3 bin/index_audio_tracks.py --root <disco>
    python3 bin/index_audio_tracks.py --root <disco> --split
    python3 bin/index_audio_tracks.py --root <disco> --split --solo-polifonicos
"""

from __future__ import annotations

import argparse
import glob
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import bwf                                      # noqa: E402
from lib.guards import assert_selected, report_done      # noqa: E402
from lib import manifest  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    channel_idx INTEGER,        -- 1-based, como el iXML
    track_name TEXT,            -- nombre de pista del iXML, o 'ch<n>'
    role TEXT,                  -- lavalier | boom | ambiente | mix | ?
    mono_path TEXT,             -- WAV mono extraido, si se corrio --split
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_atracks_clip ON audio_tracks(clip_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_atracks_unico
    ON audio_tracks(clip_id, channel_idx);
"""

# Pistas que las grabadoras suelen nombrar asi. Sirve para no mandar el mix
# estereo ni el ambiente al sync de dialogo.
_ROLES = (
    ("lavalier", ("lav", "lava", "lapel", "tx", "wireless", "radio")),
    ("boom", ("boom", "shotgun", "cana", "caña", "perch")),
    ("ambiente", ("amb", "ambien", "room", "atmos", "nat", "wild")),
    ("mix", ("mix", "master", "lr", "l/r", "stereo", "mezcla")),
)


def rol_de_pista(nombre: str) -> str:
    n = (nombre or "").strip().lower()
    if not n:
        return "?"
    for rol, claves in _ROLES:
        for k in claves:
            if k in n:
                return rol
    return "?"


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def extraer_canal(src: str, canal_1based: int, destino: Path,
                  timeout: int = 900) -> bool:
    """Extrae un canal a WAV mono con ffmpeg. No toca el original."""
    if destino.exists() and destino.stat().st_size > 1024:
        return True   # idempotente: no re-extraer
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".parcial.wav")
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src,
        "-filter_complex", f"pan=mono|c0=c{canal_1based - 1}",
        "-c:a", "pcm_s24le", str(tmp),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if r.returncode != 0 or not tmp.exists():
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.replace(destino)   # atomico: nunca queda un mono a medias
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--split", action="store_true",
                    help="Extraer cada canal a un WAV mono (no toca el original).")
    ap.add_argument("--solo-polifonicos", action="store_true",
                    help="Con --split, separar solo los de mas de 2 canales.")
    ap.add_argument("--audio-like", default="",
                    help="Filtro LIKE sobre rel_path. Vacio = todos los audios.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")
    if args.split and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg no encontrado — necesario para --split.")

    conn = manifest.conectar(str(db))
    conn.executescript(SCHEMA)

    like = args.audio_like or "%"
    rows = conn.execute("""
        SELECT id, path, filename, audio_channels, timecode
        FROM clips
        WHERE file_kind='audio' AND index_status='ok'
          AND (? = '%' OR lower(rel_path) LIKE ?)
        ORDER BY id
    """, (like, like.lower())).fetchall()
    assert_selected(rows, "audios externos en el manifest", filters={
        "--root": str(root), "--audio-like": args.audio_like or "(todos)",
    }, hint="Correr antes index_audios.py.")

    split_dir = root / ".cinema_assistant" / "audio_split"
    now = time.time()
    n_leidos = n_poly = n_con_ixml = n_con_tc = 0
    n_pistas = n_monos = n_fallos = 0
    por_rol = Counter()
    tc_rellenados = 0

    for cid, path, fname, canales_db, tc_db in rows:
        if not Path(path).exists():
            continue
        info = bwf.leer(path)
        if info.channels is None:
            continue   # no es un WAV legible; ffprobe ya lo habra reportado
        n_leidos += 1
        if info.es_polifonico:
            n_poly += 1
        if info.track_names:
            n_con_ixml += 1
        if info.tc_segundos is not None:
            n_con_tc += 1

        # Rellenar el timecode del manifest desde bext cuando ffprobe no lo dio.
        # Es lo que habilita el sync por resta en vez de por correlacion.
        if not tc_db and info.tc_segundos is not None and not args.dry_run:
            seg = info.tc_segundos
            tc = (f"{int(seg // 3600):02d}:{int(seg % 3600 // 60):02d}:"
                  f"{int(seg % 60):02d}:00")
            conn.execute("UPDATE clips SET timecode=? WHERE id=?", (tc, cid))
            tc_rellenados += 1

        separar = args.split and (info.es_polifonico or not args.solo_polifonicos)
        for ch in range(1, info.channels + 1):
            nombre = info.nombre_de_canal(ch)
            rol = rol_de_pista(nombre)
            por_rol[rol] += 1
            mono = None
            if separar:
                destino = split_dir / f"{cid}_ch{ch}.wav"
                if extraer_canal(path, ch, destino):
                    mono = str(destino)
                    n_monos += 1
                else:
                    n_fallos += 1
                    print(f"  ⚠ no se pudo extraer canal {ch} de {fname}")
            if not args.dry_run:
                conn.execute(
                    "INSERT INTO audio_tracks (clip_id, channel_idx, track_name,"
                    " role, mono_path, created_at) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(clip_id, channel_idx) DO UPDATE SET"
                    " track_name=excluded.track_name, role=excluded.role,"
                    " mono_path=COALESCE(excluded.mono_path, audio_tracks.mono_path)",
                    (cid, ch, nombre, rol, mono, now))
            n_pistas += 1

        if info.es_polifonico or info.track_names:
            pistas = ", ".join(f"{i}:{n}" for i, n in
                               sorted(info.track_names.items())) or "sin nombres"
            print(f"  {fname}: {info.channels}ch  [{pistas}]"
                  + (f"  TC {info.tc_segundos:.0f}s" if info.tc_segundos else ""))

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'(dry-run) ' if args.dry_run else ''}Audios leidos: {n_leidos}")
    print(f"  polifonicos (>2 canales) : {n_poly}")
    print(f"  con nombres de pista iXML: {n_con_ixml}")
    print(f"  con timecode bext        : {n_con_tc}")
    if tc_rellenados:
        print(f"  timecode rellenado en el manifest: {tc_rellenados} "
              f"(habilita sync por timecode)")
    print(f"  pistas registradas       : {n_pistas}")
    if por_rol:
        print("  por rol de pista:")
        for rol, n in por_rol.most_common():
            print(f"    {rol:<10} {n}")
    if args.split:
        print(f"  monos extraidos          : {n_monos}"
              + (f"  ({n_fallos} fallos)" if n_fallos else ""))
        print(f"  destino                  : {split_dir}")
    if n_poly and not args.split:
        print(f"\n·  Hay {n_poly} audio(s) polifonico(s). Sin --split, el sync "
              f"empareja el ARCHIVO\n   completo con el video: el 'lavalier de "
              f"ESCALADOR_A' llevara encima la voz de todos.\n   Correr de nuevo con "
              f"--split para separar los canales.")

    report_done("index_audio_tracks", audios=n_leidos, polifonicos=n_poly,
                pistas=n_pistas, monos=n_monos, fallos=n_fallos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
