#!/usr/bin/env python3
"""Importa offsets de sync desde un archivo .drt (DaVinci Resolve timeline export)
y los aplica como `method='manual-from-drt'` en `audio_sync_pairs`.

Caso fundador (Zezzions 2026-05-26): el usuario armó manualmente una
timeline en Resolve con los syncs correctos, exportó como .drt. Esa
timeline es ground truth — los offsets del editor son lo que debemos
replicar automáticamente.

Estructura del .drt:
  - Es un ZIP con project.xml + SeqContainer/<id>.xml + MediaPool/...
  - SeqContainer/<id>.xml contiene `Sm2TiVideoClip` y `Sm2TiAudioClip`.
  - Cada clip tiene:
    - `Start` (frames en timeline, fps=24 default Resolve)
    - `Duration` (frames)
    - `In` (subrange start del media — "8020|hex_metadata")
    - `MediaFilePath` (ruta del media file)
    - `MediaStartTime` (timecode interno)

Cálculo del offset:
  Cuando video y audio comparten el mismo `Start` en la timeline, y el
  audio tiene `In=X` (audio_t mostrado al inicio del clip), entonces:
    offset = audio_start_clock - video_start_clock
           = -audio_t_at_clip_start (porque video_t at clip_start = 0)
           = -(audio.In_frames / fps)

Resolución de paths: el .drt referencia paths del disco fuente (ej.
`/Volumes/MI_DISCO/...`). Mapeamos por basename + carpeta parent al
`clips` del manifest (`/Volumes/MI_DISCO/...`).

Uso:
    bin/import_drt_ground_truth.py --root <disk> --drt <archivo.drt>
    bin/import_drt_ground_truth.py --root <disk> --drt <archivo.drt> --dry-run
"""

from __future__ import annotations

import argparse
import glob
import logging
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


FPS_DEFAULT = 23.976023976023978  # NTSC — Sony FX30 grabó a esto en Zezzions.
# 🚨 CRÍTICO: si la timeline está a 24 pero los videos a 23.976, mantener 23.976
# (el .drt almacena frames de timeline, pero Resolve usa el rate del primer media
# importado como timeline rate). Verificar con `MediaFrameRate` en el SeqContainer:
#   - hex 872211b5dcf93740 (little-endian double) = 23.976023976023978
#   - hex 0000000000003840 (little-endian double) = 24.0
# Bug histórico (Zezzions iter5 2026-05-26): usando fps=24 con timeline @ 23.976
# introducía delay proporcional al offset (drift de 1.001 → delays de 100-500ms
# en clips con offset grande, imperceptible en offsets pequeños).
TIMELINE_OFFSET_FRAMES = 86400  # 01:00:00:00 (Resolve convention, invariante a fps en deltas)


def detect_timeline_fps(seq_root) -> float:
    """Detecta fps de la timeline del .drt parseando MediaFrameRate del primer
    Sm2TiVideoClip. Devuelve 23.976 por default si no encuentra."""
    import struct
    for vc in seq_root.iter("Sm2TiVideoClip"):
        mfr = vc.find("MediaFrameRate")
        if mfr is not None and mfr.text:
            hex_str = mfr.text[:16]  # primer double little-endian
            try:
                fps = struct.unpack('<d', bytes.fromhex(hex_str))[0]
                if 20.0 < fps < 60.0:
                    return float(fps)
            except Exception:
                pass
    return FPS_DEFAULT


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def parse_in_frames(in_text: Optional[str]) -> int:
    """El campo `In` tiene formato 'N|hex_metadata' (frames de timeline)."""
    if not in_text:
        return 0
    m = re.match(r"^(\d+)", in_text)
    return int(m.group(1)) if m else 0


def extract_clips_from_drt(drt_path: Path) -> tuple[list[dict], float]:
    """Extrae todos los Sm2TiVideoClip + Sm2TiAudioClip del .drt.
    Returns (clips, detected_fps).
    """
    if not drt_path.exists():
        sys.exit(f"No existe: {drt_path}")
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(str(drt_path)) as zf:
            zf.extractall(td)
        # Buscar el SeqContainer XML
        seq_xmls = list((Path(td) / "SeqContainer").glob("*.xml"))
        if not seq_xmls:
            sys.exit(f"No SeqContainer XML en {drt_path}")
        tree = ET.parse(str(seq_xmls[0]))
        root = tree.getroot()
        detected_fps = detect_timeline_fps(root)

    clips = []
    for tag in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
        for c in root.iter(tag):
            name = c.find("Name")
            start = c.find("Start")
            dur = c.find("Duration")
            in_el = c.find("In")
            mfp = c.find("MediaFilePath")
            clips.append({
                "tag": tag,
                "name": name.text if name is not None else "",
                "start_fr": int(start.text) if start is not None and start.text else 0,
                "dur_fr": int(dur.text) if dur is not None and dur.text else 0,
                "in_fr": parse_in_frames(in_el.text if in_el is not None else None),
                "mfp": mfp.text if mfp is not None else "",
            })
    return clips, detected_fps


def resolve_clip_id(conn: sqlite3.Connection, mfp_drt: str,
                    file_kind: str) -> Optional[int]:
    """Mapea `MediaFilePath` del .drt al `clip.id` del manifest, por basename
    + parent folder (porque el path raíz /T7 vs /T9 puede diferir).
    """
    if not mfp_drt:
        return None
    basename = mfp_drt.split("/")[-1]
    parent = mfp_drt.split("/")[-2] if "/" in mfp_drt else ""

    # Match exacto basename + parent en rel_path
    row = conn.execute(
        "SELECT id FROM clips WHERE filename=? AND file_kind=? AND rel_path LIKE ? "
        "AND index_status='ok' LIMIT 1",
        (basename, file_kind, f"%/{parent}/%")
    ).fetchone()
    if row:
        return row[0]
    # Fallback: solo basename
    row = conn.execute(
        "SELECT id FROM clips WHERE filename=? AND file_kind=? AND index_status='ok' LIMIT 1",
        (basename, file_kind)
    ).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--drt", required=True, help="Path al archivo .drt")
    ap.add_argument("--fps", type=float, default=None,
                    help="fps de la timeline del .drt. Por default detecta de MediaFrameRate "
                         "del primer Sm2TiVideoClip (NTSC 23.976 / 24 / 25 / 29.97 / 30).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-non-manual", action="store_true",
                    help="Borrar pairs no-manual de los videos importados antes de aplicar GT")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"import_drt_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    now = time.time()
    drt_path = Path(args.drt).expanduser()

    logging.info(f"Extrayendo clips de: {drt_path}")
    clips, detected_fps = extract_clips_from_drt(drt_path)
    if args.fps is None:
        args.fps = detected_fps
        logging.info(f"  fps detectado del .drt: {detected_fps:.6f}")
    else:
        logging.info(f"  fps explícito: {args.fps:.6f} (detectado: {detected_fps:.6f})")
    videos = [c for c in clips if c["tag"] == "Sm2TiVideoClip"]
    audios_all = [c for c in clips if c["tag"] == "Sm2TiAudioClip"]
    logging.info(f"  {len(videos)} videos, {len(audios_all)} audios")

    # Agrupar audios por Start (timeline_t) — los audios con mismo Start que
    # un video son sus syncs. Excluir audio scratch del propio video (MediaFilePath
    # apuntando al MP4 del video, no a un WAV externo).
    audios_by_start: dict[int, list[dict]] = {}
    for a in audios_all:
        # Skip audio scratch (mismo MediaFilePath que un video o no es WAV)
        ext = a["mfp"].split(".")[-1].lower() if a["mfp"] else ""
        if ext not in ("wav", "aif", "aiff", "mp3", "m4a"):
            continue
        audios_by_start.setdefault(a["start_fr"], []).append(a)

    n_applied = 0
    n_skipped_no_video = 0
    n_skipped_no_audio_in_manifest = 0
    n_purged = 0

    for v in videos:
        vid_id = resolve_clip_id(conn, v["mfp"], "video")
        if vid_id is None:
            logging.info(f"  ⊘ video no encontrado en manifest: {v['mfp']}")
            n_skipped_no_video += 1
            continue
        video_start_fr = v["start_fr"]
        video_end_fr = video_start_fr + v["dur_fr"]
        # Audios asociados al VIDEO: el audio.Start está dentro del rango
        # [video_start - 5s, video_start + video_duration/4] Y termina antes
        # del final del video. Esto captura:
        #   - audios alineados exactamente con video.Start (delta=0): caso normal.
        #   - audios con delta positivo razonable (ej. Diego +2.75s, Jorge +3.37s,
        #     ENTREVISTADO_5 +80s donde el video dura 413s y audio empieza a +80s).
        # PERO descarta audios cuya Start está mucho después del video (pertenece
        # al video siguiente en la timeline).
        candidate_audios = []
        max_positive_delta_fr = int(args.fps * 120)  # Audio puede empezar hasta 2 min después
        for a_start, a_list in audios_by_start.items():
            for a in a_list:
                delta_fr = a_start - video_start_fr
                if delta_fr < -int(args.fps * 5):
                    continue  # audio empieza más de 5s ANTES del video — sospechoso
                if delta_fr > max_positive_delta_fr:
                    continue  # audio empieza demasiado tarde
                a_end = a_start + a["dur_fr"]
                # El audio debe terminar antes (o cerca) del fin del video.
                # Si termina mucho después del video, pertenece al video siguiente.
                if a_end > video_end_fr + int(args.fps * 5):
                    continue
                a_copy = dict(a)
                a_copy["delta_from_video_fr"] = delta_fr
                candidate_audios.append(a_copy)

        if not candidate_audios:
            logging.info(f"  ⊘ {v['name']}: no audios externos asociados")
            continue

        # Purgar pairs no-manual existentes de este video si --purge
        if args.purge_non_manual and not args.dry_run:
            existing = conn.execute(
                "DELETE FROM audio_sync_pairs WHERE video_clip_id=? "
                "AND method != 'manual' AND method != 'manual-from-drt'",
                (vid_id,)
            ).rowcount
            if existing:
                n_purged += existing
                logging.info(f"  ⊘ purgados {existing} pairs no-manual de {v['name']}")

        for a in candidate_audios:
            aud_id = resolve_clip_id(conn, a["mfp"], "audio")
            if aud_id is None:
                logging.info(f"    ⊘ audio no en manifest: {a['mfp']}")
                n_skipped_no_audio_in_manifest += 1
                continue
            # Cálculo correcto del offset:
            #   audio_start_in_world = video_start_in_world + (audio.Start - video.Start)/fps - audio.In/fps
            #   offset = audio_start_in_world - video_start_in_world
            #          = (audio.Start - video.Start)/fps - audio.In/fps
            #          = delta_fr/fps - in_fr/fps
            offset = (a["delta_from_video_fr"] - a["in_fr"]) / args.fps
            notes = (f"GT del .drt: audio.In={a['in_fr']} fr, "
                     f"video.Start={v['start_fr']} fr, audio.Start={a['start_fr']} fr, "
                     f"timeline_t_video={(v['start_fr']-TIMELINE_OFFSET_FRAMES)/args.fps:.2f}s")
            # UPSERT (delete + insert)
            if args.dry_run:
                logging.info(f"  [DRY] {v['name']} ↔ {a['mfp'].split('/')[-2]}/{a['mfp'].split('/')[-1]}: "
                             f"offset={offset:+.3f}s")
            else:
                conn.execute(
                    "DELETE FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
                    (vid_id, aud_id)
                )
                conn.execute(
                    "INSERT INTO audio_sync_pairs "
                    "(video_clip_id, audio_clip_id, method, offset_sec, confidence, "
                    "notes, created_at, identity_score) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (vid_id, aud_id, "manual-from-drt", offset, 1.0,
                     notes, now, 1.0)
                )
                n_applied += 1
                logging.info(f"  ✓ {v['name']} ↔ {a['mfp'].split('/')[-2]}/{a['mfp'].split('/')[-1]}: "
                             f"offset={offset:+.3f}s")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nResumen: aplicados={n_applied}, "
                 f"sin_video_manifest={n_skipped_no_video}, "
                 f"sin_audio_manifest={n_skipped_no_audio_in_manifest}, "
                 f"purgados_no_manual={n_purged}")


if __name__ == "__main__":
    main()
