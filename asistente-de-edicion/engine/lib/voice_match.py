"""Voice-first identity matching para sync.

Caso fundador (Zezzions 2026-05-26): el sistema viejo elegía el audio
con mejor offset numérico, sin verificar que la VOZ del audio coincidía
con la CARA en cámara. Resultado: audio del entrevistador asignado al
video del entrevistado por coincidencia temporal.

Este módulo invierte el orden: PRIMERO determinar qué voz se espera en
el audio (vía la cara visible en el video → identity_fusion ya
construyó face_voice_links), DESPUÉS calcular offset.

API principal:

  expected_voice_embedding_for_video(conn, video_id) → np.ndarray | None
  score_audio_candidate(conn, audio_id, expected_emb) → float

Umbrales empíricos:
  ≥0.65 → match seguro (status='auto')
  0.45-0.65 → match dudoso (status='needs_review')
  <0.45 → rechazar
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


THRESH_AUTO = 0.65
THRESH_REVIEW = 0.45


def _emb_from_blob(blob) -> Optional[np.ndarray]:
    if not blob:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size == 0:
        return None
    n = np.linalg.norm(arr)
    if n < 1e-9:
        return None
    return arr / n


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Asume a, b ya normalizados a unidad."""
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


def expected_voice_embedding_for_video(conn: sqlite3.Connection,
                                       video_id: int) -> Optional[np.ndarray]:
    """Calcula el embedding de voz esperado para un video, vía la cara
    visible en cuadro.

    Estrategia (cascada):
      1. face_detections del video → cluster_id dominantes (face_catalog).
      2. cluster_id → face_voice_links → audio_speakers.embedding promedio.
      3. Si no hay links, fallback: voice_catalog.mean_embedding del
         catálogo cuyo canonical_name coincide con face_catalog.canonical_name.
      4. Si nada de eso, None — significa no podemos pre-validar este video.
    """
    # Paso 1: dominant face cluster del video. En un proyecto NUEVO las tablas
    # de caras aun no existen: `detectar-caras` corre DESPUES del sync en el
    # orquestador, y sync-v2 abortaba con `no such table: face_detections`
    # (Asistente, 2026-09-28). Sin caras no hay pre-validacion por voz, que
    # es justo lo que dice el contrato de arriba: None.
    try:
        rows = conn.execute("""
            SELECT fi.identity_id, COUNT(*) AS n
            FROM face_detections fd
            JOIN face_identities fi ON fi.detection_id = fd.id
            WHERE fd.clip_id = ?
            GROUP BY fi.identity_id
            ORDER BY n DESC
        """, (video_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    top_identity_id = rows[0][0]

    # Paso 2: vía face_voice_links: agregar embeddings de los speakers
    # vinculados a las caras de este cluster.
    try:
        emb_rows = conn.execute("""
            SELECT asp.embedding
            FROM face_voice_links fvl
            JOIN face_detections fd ON fd.id = fvl.face_detection_id
            JOIN face_identities fi ON fi.detection_id = fd.id
            JOIN audio_speakers asp ON asp.id = fvl.speaker_id
            WHERE fi.identity_id = ? AND asp.embedding IS NOT NULL
        """, (top_identity_id,)).fetchall()
    except sqlite3.OperationalError:
        emb_rows = []

    embs = []
    for (blob,) in emb_rows:
        e = _emb_from_blob(blob)
        if e is not None:
            embs.append(e)
    if embs:
        # promedio normalizado
        mean = np.mean(np.stack(embs), axis=0)
        n = np.linalg.norm(mean)
        if n > 1e-9:
            return mean / n

    # Paso 3: fallback vía voice_catalog si la identidad facial tiene nombre
    try:
        cn = conn.execute(
            "SELECT canonical_name FROM face_catalog WHERE identity_id=?",
            (top_identity_id,)
        ).fetchone()
        if cn and cn[0]:
            row = conn.execute(
                "SELECT mean_embedding FROM voice_catalog WHERE canonical_name=?",
                (cn[0],)
            ).fetchone()
            if row and row[0]:
                e = _emb_from_blob(row[0])
                if e is not None:
                    return e
    except sqlite3.OperationalError:
        pass

    # Paso 4 (2026-05-26): fallback extraer voz directamente del A1 del video.
    # Cuando ni face_voice_links ni voice_catalog tienen info, embedir el
    # audio embebido del video (zona con voz limpia) como referencia.
    # Usar el transcript para identificar zona con palabras densas y limpia.
    try:
        row = conn.execute(
            "SELECT path, duration_sec FROM clips WHERE id=?", (video_id,)
        ).fetchone()
        if not row:
            return None
        vpath, vdur = row
        # Zona limpia: hasta clean_until_sec si está disponible
        clean_until = conn.execute(
            "SELECT clean_until_sec, clean_words_count FROM transcript_quality WHERE clip_id=?",
            (video_id,)
        ).fetchone()
        if clean_until and clean_until[1] and clean_until[1] >= 30:
            cu = clean_until[0] or (vdur * 0.5)
            # Ventana de hasta 60s dentro de la zona limpia
            window_start = max(5.0, cu * 0.3)
            window_dur = min(60.0, max(15.0, cu - window_start - 2))
        else:
            # Fallback: centro del video
            window_start = max(5.0, vdur * 0.30)
            window_dur = min(45.0, max(15.0, vdur * 0.4))

        from lib.voice_embeddings import compute_embedding
        import tempfile, subprocess
        with tempfile.TemporaryDirectory() as td:
            tmp = f"{td}/v.wav"
            r = subprocess.run([
                "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                "-ss", f"{window_start:.2f}", "-i", str(vpath),
                "-t", f"{window_dur:.2f}",
                "-vn", "-ac", "1", "-ar", "16000",
                "-af", "highpass=f=300,lowpass=f=3400",
                "-c:a", "pcm_s16le", tmp,
            ], capture_output=True, timeout=60)
            if r.returncode != 0:
                return None
            from pathlib import Path as _P
            emb = compute_embedding(_P(tmp))
            if emb is not None:
                n = np.linalg.norm(emb)
                if n > 1e-9:
                    return emb / n
    except Exception as e:
        logging.warning(f"voice_match: fallback A1 falló para video_id={video_id}: {e}")

    return None


def score_audio_candidate(conn: sqlite3.Connection,
                          audio_id: int,
                          expected_emb: np.ndarray) -> tuple[float, str]:
    """Score un audio contra el embedding esperado. Retorna (sim, status)."""
    # Tomar el speaker dominante del audio (mayor duration_sec)
    row = conn.execute(
        "SELECT id, speaker_label, embedding, duration_sec FROM audio_speakers "
        "WHERE clip_id=? AND embedding IS NOT NULL "
        "ORDER BY duration_sec DESC LIMIT 1",
        (audio_id,)
    ).fetchone()
    if not row:
        return 0.0, "no_speaker_emb"
    sid, lbl, blob, dur = row
    aemb = _emb_from_blob(blob)
    if aemb is None:
        return 0.0, "invalid_emb"
    sim = _cosine(expected_emb, aemb)
    if sim >= THRESH_AUTO:
        status = "auto"
    elif sim >= THRESH_REVIEW:
        status = "needs_review"
    else:
        status = "rejected"
    return sim, status


def n_persons_in_frame(conn: sqlite3.Connection, video_id: int,
                       min_detections_per_cluster: int = 3) -> int:
    """Cuántos personajes distintos aparecen significativamente en cuadro."""
    rows = conn.execute("""
        SELECT fi.identity_id, COUNT(*) AS n
        FROM face_detections fd
        JOIN face_identities fi ON fi.detection_id = fd.id
        WHERE fd.clip_id = ?
        GROUP BY fi.identity_id
        HAVING n >= ?
    """, (video_id, min_detections_per_cluster)).fetchall()
    return len(rows)


def top_audio_candidates(conn: sqlite3.Connection,
                         video_id: int,
                         audio_ids: list[int],
                         expected_emb: np.ndarray,
                         top_k: int = 5) -> list[tuple[int, float, str]]:
    """Para un video con expected_emb conocido, score TODOS los audios y
    retorna top-K ordenados desc por sim. Lista de (audio_id, sim, status).
    """
    scored = []
    for aid in audio_ids:
        sim, status = score_audio_candidate(conn, aid, expected_emb)
        scored.append((aid, sim, status))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def prominence(top_candidates: list[tuple[int, float, str]]) -> float:
    """Diferencia entre top1 y top2 — qué tan claro es el ganador.

    > 0.10: top1 destaca, candidato confiable.
    < 0.05: muchos candidatos similares, ambiente musical confunde.
    """
    if len(top_candidates) < 2:
        return 1.0 if top_candidates else 0.0
    return float(top_candidates[0][1] - top_candidates[1][1])


def filter_candidates_by_distinctness(
        top_candidates: list[tuple[int, float, str]],
        min_prominence: float = 0.05) -> list[tuple[int, float, str]]:
    """Si prominence es baja, retornar TODOS los candidatos cerca del top
    (≥ top1 - 0.10) como needs_review. Si prominence es alta, retornar solo
    el top como 'auto'.
    """
    if not top_candidates:
        return []
    top1_sim = top_candidates[0][1]
    prom = prominence(top_candidates)
    if prom >= min_prominence and top1_sim >= THRESH_AUTO:
        return [top_candidates[0]]
    # prominence baja → marcar todos los cercanos como needs_review
    cluster = []
    for aid, sim, _status in top_candidates:
        if sim < top1_sim - 0.10:
            break
        new_status = "needs_review" if sim >= THRESH_REVIEW else "rejected"
        cluster.append((aid, sim, new_status))
    return cluster


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--video-id", type=int, required=True)
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()

    import glob as _glob
    p = Path(args.root)
    if not p.exists():
        m = _glob.glob(args.root + "*")
        if len(m) == 1:
            p = Path(m[0])
    db = p / ".cinema_assistant" / "manifest.sqlite"
    conn = manifest.conectar(str(db))

    expected = expected_voice_embedding_for_video(conn, args.video_id)
    if expected is None:
        print(f"No expected_voice_embedding para video_id={args.video_id}")
        sys.exit(1)
    n = n_persons_in_frame(conn, args.video_id)
    print(f"Video {args.video_id}: {n} personas en cuadro")
    audio_ids = [r[0] for r in conn.execute(
        "SELECT id FROM clips WHERE file_kind='audio' AND index_status='ok'"
    )]
    top = top_audio_candidates(conn, args.video_id, audio_ids, expected, top_k=args.top_k)
    print(f"\nTop-{args.top_k} audio candidatos (sim cos):")
    for aid, sim, status in top:
        fn = conn.execute("SELECT filename, rel_path FROM clips WHERE id=?", (aid,)).fetchone()
        print(f"  aid={aid:<4} sim={sim:+.3f}  status={status:<13} {Path(fn[1]).parent.name}/{fn[0]}")
