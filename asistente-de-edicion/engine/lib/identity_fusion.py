"""Fusión de identidad cara ↔ voz cross-clip.

Objetivo: combinar las dos señales de identidad más fuertes (cara visible
en video + voz audible en audio sincronizado) para auto-identificar
personajes sin depender solo del transcript.

Lógica:
  Para cada `face_detection` con clip_id de un video que tiene sync_pair:
    1. Computar audio_t = face.frame_t_sec - sync.offset_sec
    2. Buscar `audio_speakers` del audio sync donde start <= audio_t <= end
    3. Si encuentra speaker activo en ese momento → link cara ↔ speaker.

  El link se guarda en `face_voice_links`.

  Después, agregamos por face_catalog.cluster vs voice_catalog.cluster:
  si N detecciones de cluster_F=K coinciden con speaker_V=M, K y M son la
  misma persona con alta confianza si N >= 3 (umbral configurable).

Convención: offset = audio_start - video_start (igual que en audio_sync_pairs),
de donde audio_t = video_t - offset.

OJO (2026-08-13): las dos frases estaban aqui juntas y son incompatibles — la
segunda decia `+ offset` y el codigo la implementaba. Con el offset tipico de un
lavalier continuo (negativo y grande) `at` salia fuera de rango y no encontraba
ningun speaker: cero enlaces cara-voz, en silencio. Con offsets pequenos
enlazaba, pero al speaker equivocado.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


@dataclass
class FaceVoiceLink:
    face_detection_id: int
    speaker_id: int
    video_t: float
    audio_t: float
    conf: float           # 1.0 si el speaker estaba activo en ese instante exacto
    notes: str = ""


def ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_voice_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_detection_id INTEGER NOT NULL,
            speaker_id INTEGER NOT NULL,
            video_t REAL,
            audio_t REAL,
            conf REAL,
            notes TEXT,
            created_at REAL,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections(id),
            FOREIGN KEY (speaker_id) REFERENCES audio_speakers(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fvl_face ON face_voice_links(face_detection_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fvl_speaker ON face_voice_links(speaker_id)")
    conn.commit()


def link_faces_to_voices(conn: sqlite3.Connection, min_sync_conf: float = 0.50,
                        log: Optional[logging.Logger] = None) -> int:
    """Construye `face_voice_links` para todos los videos con sync.

    Returns: número de links creados.
    """
    log = log or logging.getLogger(__name__)
    # Sync pairs aceptables
    syncs = conn.execute(
        "SELECT video_clip_id, audio_clip_id, offset_sec, confidence "
        "FROM audio_sync_pairs WHERE confidence >= ?",
        (min_sync_conf,)
    ).fetchall()
    log.info(f"sync pairs aceptables (conf>={min_sync_conf}): {len(syncs)}")

    # Borrar links viejos (re-build idempotente)
    conn.execute("DELETE FROM face_voice_links")
    conn.commit()

    n_links = 0
    now = time.time()
    for vid, aid, off, sconf in syncs:
        # Caras del video
        faces = conn.execute(
            "SELECT id, frame_t_sec FROM face_detections WHERE clip_id=?",
            (vid,)
        ).fetchall()
        if not faces:
            continue
        # Speakers + sus segmentos granulares (audio_speaker_segments).
        # Si la tabla granular no existe (datos viejos), fallback a envolvente.
        try:
            speakers = conn.execute(
                """SELECT asp.id, asp.speaker_label, ass.start_sec, ass.end_sec
                   FROM audio_speakers asp
                   JOIN audio_speaker_segments ass ON ass.audio_speaker_id = asp.id
                   WHERE asp.clip_id=?""",
                (aid,)
            ).fetchall()
        except sqlite3.OperationalError:
            speakers = conn.execute(
                "SELECT id, speaker_label, start_sec, end_sec "
                "FROM audio_speakers WHERE clip_id=?",
                (aid,)
            ).fetchall()
        if not speakers:
            continue
        for fid, vt in faces:
            at = vt - off        # audio_t = video_t - offset (ver cabecera)
            # Speaker activo en at: start <= at <= end (segmento granular).
            # Tolerancia 0.5s para absorber jitter de timestamps.
            candidates = [(sid, lbl, s, e) for sid, lbl, s, e in speakers
                          if s - 0.5 <= at <= e + 0.5]
            if not candidates:
                continue
            # Mejor candidate: el con duración del segmento mayor (más estable)
            sid, lbl, s, e = max(candidates, key=lambda x: x[3] - x[2])
            conf = 1.0 if s <= at <= e else 0.7  # tolerancia 0.5s
            conn.execute(
                "INSERT INTO face_voice_links "
                "(face_detection_id, speaker_id, video_t, audio_t, conf, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (fid, sid, vt, at, conf, f"sync_conf={sconf:.2f}, speaker={lbl}", now)
            )
            n_links += 1
        conn.commit()
        log.info(f"  video_clip={vid} ↔ audio_clip={aid}: {len(faces)} caras, "
                 f"{len(speakers)} speakers")
    log.info(f"Total links cara↔voz: {n_links}")
    return n_links


def cluster_cooccurrence(conn: sqlite3.Connection, min_links: int = 3
                          ) -> dict[tuple[int, int], int]:
    """Cuenta co-ocurrencias face_identity ↔ voice_catalog.

    Schema real (manifest):
      face_identities(detection_id PK, identity_id → face_catalog.identity_id, confidence)
      face_catalog(identity_id PK, canonical_name, cluster_id, ...)
      audio_speakers(id PK, clip_id, catalog_id → voice_catalog.id)
      voice_catalog(id PK, canonical_name, ...)

    Retorna {(face_identity_id, voice_catalog_id): n_co_occurrences}.
    """
    rows = conn.execute("""
        SELECT fi.identity_id, asp.catalog_id, COUNT(*) AS n
        FROM face_voice_links fvl
        JOIN face_identities fi ON fi.detection_id = fvl.face_detection_id
        JOIN audio_speakers asp ON asp.id = fvl.speaker_id
        WHERE fi.identity_id IS NOT NULL AND asp.catalog_id IS NOT NULL
        GROUP BY fi.identity_id, asp.catalog_id
        HAVING n >= ?
        ORDER BY n DESC
    """, (min_links,)).fetchall()
    return {(fi, vc): n for fi, vc, n in rows}


def propagate_names(conn: sqlite3.Connection, min_co: int = 3,
                    log: Optional[logging.Logger] = None) -> int:
    """Si una face_identity tiene canonical_name Y co-ocurre con un voice_catalog
    sin nombre, propagar — y viceversa.

    **Fix iter2 (2026-05-26)**: ahora exige co-ocurrencia consistente:
    - El voice_catalog destino debe co-ocurrir MAYORITARIAMENTE con UNA sola
      face_identity (no múltiples). Si está dividido entre 2+ caras, NO
      propagar (es ambient compartido, no identidad clara).
    - NO sobrescribir nombres canónicos ya asignados.
    - La face_identity NO debe estar mapeada a múltiples voice_catalog
      distintos con co-occurrence > min_co (caso de cara que aparece en
      múltiples audios con voces distintas = no es propietaria de ninguna).

    Returns: número de propagaciones.
    """
    log = log or logging.getLogger(__name__)
    co = cluster_cooccurrence(conn, min_links=min_co)
    if not co:
        log.info("No hay co-ocurrencias cara↔voz suficientes para propagar")
        return 0

    # Construir mapas auxiliares para verificar dominancia:
    # vc_to_faces[vc] = {face_id: count}  — qué caras co-ocurren con cada voz
    # fi_to_voices[fi] = {vc: count}      — qué voces co-ocurren con cada cara
    vc_to_faces: dict[int, dict[int, int]] = {}
    fi_to_voices: dict[int, dict[int, int]] = {}
    for (fi, vc), n in co.items():
        vc_to_faces.setdefault(vc, {})[fi] = n
        fi_to_voices.setdefault(fi, {})[vc] = n

    def is_dominant(d: dict, key, min_ratio: float = 0.70) -> bool:
        """key tiene >=min_ratio del total de votos en d."""
        if not d:
            return False
        total = sum(d.values())
        if total == 0:
            return False
        return d.get(key, 0) / total >= min_ratio

    n_prop = 0
    for (fi, vc), n in co.items():
        face_name = conn.execute(
            "SELECT canonical_name FROM face_catalog WHERE identity_id=?", (fi,)
        ).fetchone()
        voice_name = conn.execute(
            "SELECT canonical_name FROM voice_catalog WHERE id=?", (vc,)
        ).fetchone()
        face_name = face_name[0] if face_name else None
        voice_name = voice_name[0] if voice_name else None

        # Fix 1: NO sobrescribir nombre ya asignado en voice_catalog
        if voice_name:
            continue

        # Fix 2: cara→voz solo si la voz es DOMINADA por ESTA cara (no
        # mezclada con otras caras de forma significativa)
        if face_name and not voice_name:
            if not is_dominant(vc_to_faces.get(vc, {}), fi, min_ratio=0.70):
                log.info(f"  ⊘ voice_catalog={vc}: cara_id={fi} ({face_name}) "
                         f"NO dominante (co-occurs={vc_to_faces.get(vc, {})}). NO propagar.")
                continue
            # Fix 3: cara no debe estar repartida entre múltiples voces con co>=min_co
            n_voices_for_face = sum(1 for v in fi_to_voices.get(fi, {}).values() if v >= min_co)
            if n_voices_for_face > 2:
                log.info(f"  ⊘ face_id={fi} ({face_name}) está en {n_voices_for_face} "
                         f"voices distintas → no propagar (cara genérica/B-roll).")
                continue
            conn.execute("UPDATE voice_catalog SET canonical_name=? WHERE id=?", (face_name, vc))
            log.info(f"  propagado cara→voz: voice_catalog={vc} → '{face_name}' "
                     f"(co-ocurrencias={n}, dominancia OK)")
            n_prop += 1
        elif voice_name and not face_name:
            conn.execute("UPDATE face_catalog SET canonical_name=? WHERE identity_id=?",
                         (voice_name, fi))
            log.info(f"  propagado voz→cara: face_identity={fi} → '{voice_name}' "
                     f"(co-ocurrencias={n})")
            n_prop += 1
    conn.commit()
    return n_prop


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-sync-conf", type=float, default=0.50)
    ap.add_argument("--propagate", action="store_true",
                    help="También propagar nombres cara↔voz")
    args = ap.parse_args()

    import glob
    from pathlib import Path
    p = Path(args.root)
    if not p.exists():
        m = glob.glob(args.root + "*")
        if len(m) == 1:
            p = Path(m[0])
    db = p / ".cinema_assistant" / "manifest.sqlite"
    log_file = p / ".cinema_assistant" / "logs" / f"identity_fusion_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
    )

    conn = manifest.conectar(str(db))
    ensure_schema(conn)
    n = link_faces_to_voices(conn, min_sync_conf=args.min_sync_conf)
    if args.propagate and n > 0:
        propagate_names(conn)
    conn.close()
