#!/usr/bin/env python3
"""Sync por eventos sonoros únicos — aplausos, risas, bangs, etc.

Caso fundador: para clips donde transcript/envelope/chromaprint fallan
(música muy alta, transcript alucinado, fingerprints débiles), los EVENTOS
ÚNICOS detectados por PANNs son anclas muy confiables: un aplauso en el
video corresponde a un aplauso en el audio con offset constante.

Lógica:
  1. Lee `sound_events` de cada video con audio embebido.
  2. Lee `sound_events` de cada audio externo.
  3. Para cada par (video, audio):
     a. Toma eventos de los grupos "anchor": applause, laughter, bang.
     b. Para cada combinación (video_event, audio_event), offset =
        audio.start - video.start.
     c. Cluster offsets en bins de ±2s. El cluster ganador (más eventos)
        es el sync candidato.
     d. Confianza = (n_events_in_cluster / max_events) × similarity_label.
  4. Inserta como par "events" en `audio_sync_pairs` si conf > min.

Pre-requisito: `bin/index_sound_events.py --kind both` ya corrido.

Uso:
    bin/sync_by_events.py --root <disk> --min-anchor-events 2
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

# Grupos de eventos que actúan como ANCHOR para sync (eventos discretos
# y distinguibles, no el speech/music que es continuo).
# Default = solo los muy discretos. `laughter` + `singing` se pueden agregar
# vía --include-groups laughter,singing si el proyecto lo permite.
ANCHOR_GROUPS = {"applause", "bang", "whistle"}


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def get_anchor_events(conn: sqlite3.Connection, clip_id: int) -> list[tuple[float, str]]:
    """Devuelve eventos anchor de un clip como [(start_sec, group), ...]."""
    placeholders = ",".join("?" * len(ANCHOR_GROUPS))
    rows = conn.execute(
        f"SELECT start_sec, event_group FROM sound_events "
        f"WHERE clip_id=? AND event_group IN ({placeholders}) "
        "ORDER BY start_sec",
        (clip_id, *ANCHOR_GROUPS)
    ).fetchall()
    return [(s, g) for s, g in rows]


def find_offset(video_events: list[tuple[float, str]],
                audio_events: list[tuple[float, str]],
                tol: float = 2.0) -> tuple[float, float, int] | None:
    """Encuentra el offset que maximiza eventos coincidentes.

    Para cada combinación (v, a) con MISMO grupo, computa offset =
    a.start - v.start. Cluster offsets en bins de ±tol.

    Returns (offset, conf, n_matches) o None.
    """
    if not video_events or not audio_events:
        return None

    candidates: list[float] = []
    for vt, vg in video_events:
        for at, ag in audio_events:
            if vg != ag:
                continue
            candidates.append(at - vt)

    if not candidates:
        return None

    # Cluster: para cada offset_candidato, contar cuántos otros caen en ±tol
    candidates.sort()
    best_offset = candidates[0]
    best_count = 0
    for c in candidates:
        count = sum(1 for x in candidates if abs(x - c) <= tol)
        if count > best_count:
            best_count = count
            best_offset = c
    cluster = [x for x in candidates if abs(x - best_offset) <= tol]
    avg_offset = sum(cluster) / len(cluster)

    # Confianza recalibrada (2026-05-26 tras observar muchos falsos positivos):
    # - Si el cluster ganador no es CLARAMENTE mayor que el segundo, conf baja.
    # - Necesitamos n_matches ≥ 3 para conf ≥ 0.5, ≥ 5 para conf ≥ 0.7.
    if best_count < 3:
        return None
    # Distinción del cluster: ratio del ganador vs total de candidates
    distinctiveness = best_count / len(candidates)
    # Eventos disponibles: min(video, audio) — qué tanto del posible se cubrió
    max_possible = min(len(video_events), len(audio_events))
    coverage = best_count / max_possible if max_possible else 0

    # Score combinado: distinctiveness (cuánto se aleja del 2do cluster) +
    # coverage (cuántos eventos cubre) + bonus por escala (cuántos matches)
    base = 0.3 * distinctiveness + 0.5 * coverage
    scale_bonus = min(0.15, best_count / 50.0)  # +0.15 si tenemos 50+ matches
    conf = min(0.95, base + scale_bonus)
    if conf < 0.30:
        return None
    return avg_offset, conf, best_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%lavas%")
    ap.add_argument("--min-anchor-events", type=int, default=2,
                    help="mínimo de eventos anchor en cada lado")
    ap.add_argument("--min-confidence", type=float, default=0.30)
    ap.add_argument("--only-missing", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_events_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Videos con suficientes anchor events
    video_rows = conn.execute(f"""
        SELECT c.id, c.filename FROM clips c
        WHERE c.file_kind='video' AND c.index_status='ok' AND c.has_audio=1
          AND c.id IN (
              SELECT clip_id FROM sound_events
              WHERE event_group IN ({",".join("?"*len(ANCHOR_GROUPS))})
              GROUP BY clip_id HAVING COUNT(*) >= ?
          )
    """, (*ANCHOR_GROUPS, args.min_anchor_events)).fetchall()

    audio_rows = conn.execute(f"""
        SELECT c.id, c.filename FROM clips c
        WHERE c.file_kind='audio' AND c.index_status='ok'
          AND lower(c.rel_path) LIKE ?
          AND c.id IN (
              SELECT clip_id FROM sound_events
              WHERE event_group IN ({",".join("?"*len(ANCHOR_GROUPS))})
              GROUP BY clip_id HAVING COUNT(*) >= ?
          )
    """, (args.audio_like, *ANCHOR_GROUPS, args.min_anchor_events)).fetchall()

    logging.info(f"Videos con anchors: {len(video_rows)}, audios con anchors: {len(audio_rows)}")

    n_new = 0
    n_skipped_existing = 0
    n_skipped_no_match = 0
    now = time.time()

    for vid, vfn in video_rows:
        v_events = get_anchor_events(conn, vid)
        for aid, afn in audio_rows:
            if args.only_missing:
                existing = conn.execute(
                    "SELECT 1 FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
                    (vid, aid)
                ).fetchone()
                if existing:
                    n_skipped_existing += 1
                    continue
            a_events = get_anchor_events(conn, aid)
            result = find_offset(v_events, a_events)
            if not result:
                n_skipped_no_match += 1
                continue
            offset, conf, n_matches = result
            if conf < args.min_confidence:
                n_skipped_no_match += 1
                continue
            notes = f"events: {n_matches} anchors coincidentes (grupos: {ANCHOR_GROUPS})"
            if args.dry_run:
                logging.info(f"  [DRY] {vfn} ↔ {afn}: off={offset:+.1f}s conf={conf:.2f} matches={n_matches}")
            else:
                conn.execute(
                    "INSERT INTO audio_sync_pairs "
                    "(video_clip_id, audio_clip_id, method, offset_sec, confidence, "
                    "notes, created_at) VALUES (?,?,?,?,?,?,?)",
                    (vid, aid, "events", offset, conf, notes, now)
                )
                n_new += 1
                logging.info(f"  + {vfn} ↔ {afn}: off={offset:+.1f}s conf={conf:.2f} matches={n_matches}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"sync_by_events: nuevos={n_new}, skipped(existing)={n_skipped_existing}, "
                 f"no_match={n_skipped_no_match}")


if __name__ == "__main__":
    main()
