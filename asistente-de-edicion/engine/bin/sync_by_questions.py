#!/usr/bin/env python3
"""Sync por preguntas de entrevista compartidas (question-anchors).

Caso fundador (Zezzions 2026-05-26): `sync_by_phrases` cubre videos donde
el transcript del video tiene auto-id ("Yo soy X"), pero cuando el audio
de cámara está totalmente tapado por música, las auto-IDs no aparecen en
el video transcript. SIN EMBARGO, las **preguntas del entrevistador** sí
suelen ser audibles tanto en el A1 de cámara (en zonas tranquilas) como
en el lavalier del entrevistado.

Este script usa `question_segments` (ya extraídas por
`derive_question_segments.py`) como anchors:

  1. Para cada video con `question_segments`, identifica preguntas
     **distintivas** (≥ 5 palabras, no repetidas masivamente).
  2. Para cada audio con `question_segments`, hace lo mismo.
  3. Empareja preguntas que compartan ≥ 4 palabras consecutivas
     (n-grama 4).
  4. El offset = audio_q.start_sec - video_q.start_sec.
  5. Si ≥ 2 preguntas dan offsets consistentes (cluster ±15s — más
     estricto que phrase-match porque las preguntas son anchors fuertes),
     emitir el par con confianza alta.

Política recomendada en el pipeline:
  1. sync_transcript    (n-gramas largos del transcript completo)
  2. sync_by_questions  (preguntas como anchors — caso entrevista densa)
  3. sync_by_phrases    (auto-IDs + nombres distintivos)
  4. sync_acoustic      (envelope FFT — música ambiente)
"""

from __future__ import annotations

import argparse
import glob
import logging
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


def normalize_phrase(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", "", s).strip()


def shared_4gram(q1: str, q2: str) -> bool:
    """True si q1 y q2 comparten al menos un 4-grama de palabras."""
    w1 = normalize_phrase(q1).split()
    w2 = normalize_phrase(q2).split()
    if len(w1) < 4 or len(w2) < 4:
        return False
    grams1 = {tuple(w1[i:i+4]) for i in range(len(w1) - 3)}
    grams2 = {tuple(w2[i:i+4]) for i in range(len(w2) - 3)}
    return bool(grams1 & grams2)


def is_distinctive_question(qtext: str, min_words: int = 5,
                            max_repetition_ratio: float = 0.6) -> bool:
    """Una pregunta es distintiva si tiene ≥ min_words y no es alucinación
    obvia (palabras repetidas masivamente)."""
    if not qtext:
        return False
    norm = normalize_phrase(qtext)
    words = norm.split()
    if len(words) < min_words:
        return False
    if len(set(words)) / len(words) < (1 - max_repetition_ratio):
        return False  # ej. "si si si si si si si"
    return True


def cluster_median_offset(offsets: list[float], radius: float = 15.0):
    """Devuelve (mean_of_cluster, cluster_size) o (None, 0)."""
    if not offsets:
        return None, 0
    med = statistics.median(offsets)
    cluster = [d for d in offsets if abs(d - med) <= radius]
    if not cluster:
        return None, 0
    return statistics.mean(cluster), len(cluster)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-shared-questions", type=int, default=2,
                    help="Mínimas preguntas compartidas en cluster para emitir par.")
    ap.add_argument("--cluster-radius-sec", type=float, default=15.0,
                    help="Tolerancia de offset entre preguntas anchor (±sec).")
    ap.add_argument("--max-pairs-per-video", type=int, default=2)
    ap.add_argument("--only-missing", action=argparse.BooleanOptionalAction, default=False,
                    help="Si True, solo videos sin par existente. Default False — "
                         "permite agregar pares adicionales (ej. 2do TX en dual-lavalier).")
    ap.add_argument("--skip-existing-pair", action="store_true", default=True,
                    help="Si el par (video, audio) ya existe, no duplicar.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_questions_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Cargar preguntas por clip (video o audio)
    qs_by_clip: dict[int, list] = defaultdict(list)
    for cid, sec, qtext in conn.execute(
        "SELECT clip_id, start_sec, question_text FROM question_segments"
    ):
        if is_distinctive_question(qtext):
            qs_by_clip[cid].append((sec, qtext))
    logging.info(f"Clips con preguntas distintivas: {len(qs_by_clip)}")

    # Separar en videos y audios
    file_kind = dict(conn.execute("SELECT id, file_kind FROM clips"))
    video_ids = {cid for cid in qs_by_clip if file_kind.get(cid) == "video"}
    audio_ids = {cid for cid in qs_by_clip if file_kind.get(cid) == "audio"}
    logging.info(f"  Videos: {len(video_ids)}  Audios: {len(audio_ids)}")

    already_synced = set(
        r[0] for r in conn.execute("SELECT DISTINCT video_clip_id FROM audio_sync_pairs")
    )
    existing_pairs = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT video_clip_id, audio_clip_id FROM audio_sync_pairs"
        )
    )

    # Cache de nombres
    fn_by_clip = dict(conn.execute("SELECT id, filename FROM clips"))

    n_pairs = 0
    for vid in video_ids:
        if args.only_missing and vid in already_synced:
            continue
        v_qs = qs_by_clip[vid]
        if not v_qs:
            continue
        scored = []
        for aid in audio_ids:
            a_qs = qs_by_clip[aid]
            # Buscar matches: pares (vt, at, qtext) donde las dos preguntas comparten 4-grama
            offsets = []
            shared = []
            for vt, vq in v_qs:
                for at, aq in a_qs:
                    if shared_4gram(vq, aq):
                        offsets.append(at - vt)
                        shared.append(vq[:50])
            if not offsets:
                continue
            avg_off, cluster_size = cluster_median_offset(offsets, args.cluster_radius_sec)
            if avg_off is None or cluster_size < args.min_shared_questions:
                continue
            conf = min(0.95, 0.55 + 0.08 * cluster_size)
            scored.append({
                "aid": aid, "afn": fn_by_clip.get(aid, "?"),
                "offset": avg_off, "cluster": cluster_size, "shared": shared,
            })
        scored.sort(key=lambda m: -m["cluster"])
        # Filtrar pares ya existentes
        if args.skip_existing_pair:
            scored = [s for s in scored if (vid, s["aid"]) not in existing_pairs]
        kept = scored[:args.max_pairs_per_video]
        for rank, s in enumerate(kept, 1):
            conf = min(0.95, 0.55 + 0.08 * s["cluster"])
            conn.execute(
                "INSERT INTO audio_sync_pairs "
                "(video_clip_id, audio_clip_id, method, offset_sec, confidence, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (vid, s["aid"], "question-anchor", s["offset"], conf,
                 f"q-anchors={s['cluster']} ej={s['shared'][0][:40]!r}", time.time())
            )
            n_pairs += 1
            logging.info(f"  {fn_by_clip.get(vid)} -> {s['afn']} #{rank} "
                         f"offset={s['offset']:+.1f}s conf={conf:.2f} q-cluster={s['cluster']}")

    conn.commit()
    conn.close()
    logging.info(f"sync_by_questions: {n_pairs} pares escritos.")


if __name__ == "__main__":
    main()
