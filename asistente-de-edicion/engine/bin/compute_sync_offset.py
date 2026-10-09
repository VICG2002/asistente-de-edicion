#!/usr/bin/env python3
"""Compute sync offset para candidatos voice-matched — Fase 4 del rebuild.

Para cada candidato en sync_candidates (status='auto' o 'needs_review'),
calcula el offset por cross-correlation envelope log-RMS en banda voz.

Si voice_match es perfecto (identity_score ≥0.95) Y offset converge con
buena prominence:
  → escribe a audio_sync_pairs con method='voice+envelope', conf alta

Si identity_score es medio (0.65-0.95) pero offset acústico es robusto:
  → escribe con method='voice+envelope', conf moderada (~0.70)

Si offset acústico es débil o divergente:
  → mantiene en sync_candidates, status='needs_review'

Uso:
    bin/compute_sync_offset.py --root <disk>
    bin/compute_sync_offset.py --root <disk> --include-needs-review
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sync_refiner import _load_pcm, _envelope, WORK_SR
from lib import manifest  # noqa: E402


# Cross-correlation parameters
ENV_HOP_MS = 10
ENV_SR = 1000 // ENV_HOP_MS  # 100 Hz


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def compute_offset_acoustic(video_path: Path, audio_path: Path,
                            video_dur: float, audio_dur: float,
                            analysis_dur: float = 60.0,
                            search_max: float = 1200.0):
    """Cross-correlate envelope log-RMS en banda voz para encontrar offset.

    Returns (offset_sec, prominence, peak_norm) o None si no hay match claro.
    No usa offset_seed — busca en TODO el rango ±search_max.
    """
    # Centro del video (~40% para evitar instalación de micro)
    v_start = max(0.0, video_dur * 0.40 - analysis_dur / 2)
    v_dur = min(analysis_dur, max(5.0, video_dur - v_start))
    v_pcm = _load_pcm(video_path, v_start, v_dur, voice_bandpass=True)
    if v_pcm is None or v_pcm.size < WORK_SR * 5:
        return None
    v_env = _envelope(v_pcm, sr=WORK_SR, hop_ms=ENV_HOP_MS)
    if v_env.size < 100:
        return None

    # Audio: cargar hasta search_max desde 0 (suficiente para encontrar match)
    a_dur = min(audio_dur, analysis_dur + 2 * search_max + 60)
    a_pcm = _load_pcm(audio_path, 0.0, a_dur, voice_bandpass=True)
    if a_pcm is None or a_pcm.size < WORK_SR * 5:
        return None
    a_env = _envelope(a_pcm, sr=WORK_SR, hop_ms=ENV_HOP_MS)
    if a_env.size < v_env.size:
        return None

    # Cross-correlate
    from scipy.signal import correlate
    corr = correlate(a_env, v_env, mode="full", method="fft")
    n_v = len(v_env)

    # Peak: lag de a_env vs v_env, en samples del env (100Hz)
    peak_local = int(np.argmax(corr))
    delta_samples = peak_local - (n_v - 1)
    # Convención del proyecto (compatible con asistente_*.lua):
    #   offset = audio_start - video_start
    #   audio_t = video_t - offset
    # En la cross-correlation, peak en delta_samples significa:
    #   a_env[delta_samples..delta_samples+len(v_env)] ≈ v_env[..]
    # i.e., el momento `delta_samples / ENV_SR` del audio (medido desde
    # el inicio del audio_t=0) corresponde al momento `v_start` del video.
    # Entonces: video_t = v_start ↔ audio_t = delta_samples/ENV_SR
    # offset = video_t - audio_t = v_start - delta_samples/ENV_SR
    offset = v_start - (delta_samples / ENV_SR)

    # Prominence (peak vs second-best, excluyendo ±200ms del peak)
    peak_val = corr[peak_local]
    half = max(int(0.2 * ENV_SR), 5)
    mask = np.ones_like(corr, dtype=bool)
    lo = max(0, peak_local - half)
    hi = min(len(corr), peak_local + half + 1)
    mask[lo:hi] = False
    if mask.any():
        runner_up = float(np.max(corr[mask]))
        prom = (peak_val - runner_up) / (peak_val + 1e-9)
    else:
        prom = 1.0

    # Normalizado
    v_norm = float(np.linalg.norm(v_env))
    a_norm_max = float(np.linalg.norm(a_env))
    peak_norm = float(peak_val / (v_norm * a_norm_max + 1e-9))

    return float(offset), float(prom), peak_norm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--include-needs-review", action="store_true", default=True,
                    help="Procesar candidatos needs_review también (default True)")
    ap.add_argument("--min-prominence", type=float, default=0.15)
    ap.add_argument("--min-identity-score", type=float, default=0.65)
    ap.add_argument("--analysis-dur", type=float, default=60.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"compute_offset_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    statuses = "('auto','needs_review')" if args.include_needs_review else "('auto')"
    rows = conn.execute(f"""
        SELECT sc.id, sc.video_clip_id, sc.audio_clip_id, sc.identity_score,
               cv.path, cv.duration_sec, ca.path, ca.duration_sec,
               cv.filename, ca.filename
        FROM sync_candidates sc
        JOIN clips cv ON cv.id=sc.video_clip_id
        JOIN clips ca ON ca.id=sc.audio_clip_id
        WHERE sc.status IN {statuses}
          AND sc.identity_score >= ?
        ORDER BY sc.identity_score DESC, sc.video_clip_id
    """, (args.min_identity_score,)).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    logging.info(f"Candidatos a procesar: {len(rows)}")

    # Por video, agrupar candidatos para escoger el mejor offset
    by_video: dict[int, list] = {}
    for r in rows:
        by_video.setdefault(r[1], []).append(r)

    n_written = 0
    n_marked_review = 0
    n_failed = 0
    now = time.time()

    for vid, candidates in by_video.items():
        # Procesar cada candidato y elegir el con mejor offset
        results = []
        for sc_id, _vid, aid, id_score, vpath, vdur, apath, adur, vfn, afn in candidates:
            res = compute_offset_acoustic(Path(vpath), Path(apath),
                                          vdur or 0, adur or 0,
                                          analysis_dur=args.analysis_dur)
            if res is None:
                continue
            offset, prom, peak_norm = res
            results.append({
                "sc_id": sc_id, "aid": aid, "afn": afn, "apath": apath,
                "id_score": id_score, "offset": offset,
                "prominence": prom, "peak_norm": peak_norm,
            })
        if not results:
            n_failed += 1
            continue

        # Ordenar por prominence × identity_score (combinación)
        for r in results:
            r["combined_score"] = r["id_score"] * (0.5 + 0.5 * r["prominence"])
        results.sort(key=lambda r: -r["combined_score"])
        top = results[0]
        # contar identidades distintas en cuadro (de face_identities)
        n_persons = conn.execute("""
            SELECT COUNT(*) FROM (
              SELECT fi.identity_id, COUNT(*) AS n
              FROM face_detections fd JOIN face_identities fi ON fi.detection_id=fd.id
              WHERE fd.clip_id=? GROUP BY fi.identity_id HAVING n >= 3
            )""", (vid,)).fetchone()
        n_persons = (n_persons[0] if n_persons and n_persons[0] else 1)

        # Decisión: si top.prominence >= min_prominence Y top.id_score >= 0.65 → escribir
        # Si dual-lavalier (n_persons >= 2): permitir hasta 2 candidatos con voces distintas
        max_audios = 2 if n_persons >= 2 else 1
        accepted_audio_ids: list[int] = []

        # Fix iter2 (2026-05-26): endurecer filtros para casos sin caras
        # claras (n_persons=0) o cuando los candidatos son indistinguibles
        # (señal ambient genérica, no identidad).
        #
        # 1. Si no hay caras detectadas (n_persons=0), exigir id_score >= 0.80
        #    (umbral más estricto que el default 0.65).
        # 2. Si la diferencia entre top1 y top3 en combined_score es < 0.05,
        #    es probable señal ambient — todos los audios parecen iguales.
        #    Rechazar.
        if n_persons == 0:
            effective_min_id = max(args.min_identity_score, 0.80)
        else:
            effective_min_id = args.min_identity_score

        if len(results) >= 3:
            spread = results[0]["combined_score"] - results[2]["combined_score"]
            if spread < 0.05:
                logging.info(f"[{vid}] AMBIENT-LIKE (spread top1-top3={spread:.3f}<0.05) "
                             f"— no escribir, marcar review")
                n_marked_review += 1
                continue

        for r in results[:max_audios]:
            if r["prominence"] < args.min_prominence:
                continue
            if r["id_score"] < effective_min_id:
                continue
            accepted_audio_ids.append(r["aid"])

        if not accepted_audio_ids:
            # Marcar todos como needs_review
            n_marked_review += 1
            ranked = "; ".join(
                f"aid={r['aid']} off={r['offset']:+.2f} prom={r['prominence']:.2f} "
                f"id={r['id_score']:.2f} comb={r['combined_score']:.2f}"
                for r in results[:4]
            )
            logging.info(f"[{vid}] {top['afn'][:30]} REVIEW (prom={top['prominence']:.2f} "
                         f"id={top['id_score']:.2f}): {ranked}")
            continue

        # Escribir los aceptados a audio_sync_pairs
        for r in results:
            if r["aid"] not in accepted_audio_ids:
                continue
            signals = {
                "voice_match_sim": r["id_score"],
                "envelope_prominence": r["prominence"],
                "envelope_peak_norm": r["peak_norm"],
                "method": "voice+envelope",
            }
            note = (f"voice-first sim={r['id_score']:.3f} + envelope "
                    f"prom={r['prominence']:.2f}")
            conf = min(0.99, r["combined_score"])
            if args.dry_run:
                logging.info(f"[{vid}] [DRY] {r['afn'][:30]}: off={r['offset']:+.2f} "
                             f"conf={conf:.2f}")
            else:
                conn.execute("""
                    INSERT INTO audio_sync_pairs
                    (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                     notes, created_at, identity_score, verifier_passed,
                     n_persons_in_frame, signals_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (vid, r["aid"], "voice+envelope", r["offset"], conf,
                      note, now, r["id_score"], None, n_persons,
                      json.dumps(signals)))
                n_written += 1
                logging.info(f"[{vid}] ✓ {r['afn'][:30]}: off={r['offset']:+.2f}s "
                             f"conf={conf:.2f} sim={r['id_score']:.2f} "
                             f"prom={r['prominence']:.2f}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nResumen: written={n_written}, marked_review={n_marked_review}, "
                 f"failed_acoustic={n_failed}")


if __name__ == "__main__":
    main()
