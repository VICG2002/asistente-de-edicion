#!/usr/bin/env python3
"""Pipeline completo de sync sin ground truth del editor (Zezzions iter8+).

Orquesta el flujo voice-first → transcript-sync → segment-analysis →
clasificación → escritura selectiva.

Para cada video con audio:
  1. **Filtro temporal**: candidatos = audios cuyo mtime contiene al video.
  2. **Voice-first**: score candidatos por voice_match.score_audio_candidate.
  3. **Sync por transcript** (top candidatos): compute_offset físico.
  4. **Análisis por segmentos**: classify_pair.
  5. **Política de escritura**:
       - 'ok' o 'bias_refinable' con confianza alta → audio_sync_pairs
       - resto → sync_candidates con status='needs_review'
  6. **Propagación a hermanos lavalier** (lavalier_pairs.applicable=1).

Doctrina: `~/memoria-asistente-edicion/metodologia/sync-sin-ground-truth.md`

Uso:
    bin/sync_pipeline_full.py --root <disk>
    bin/sync_pipeline_full.py --root <disk> --dry-run
    bin/sync_pipeline_full.py --root <disk> --videos 2571,2572  # subset
    bin/sync_pipeline_full.py --root <disk> --audio-like '%Wireless PRO%'
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sync_segment_analysis import (
    load_words, compute_offset, segment_offsets,
    multitake_score, classify_pair, temporal_validity,
    STATUS_LABEL, STATUS_PRIORITY,
)
from lib.voice_match import (
    expected_voice_embedding_for_video, score_audio_candidate,
)
from lib.transcript_sync import align as transcript_align
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists(): return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1: return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def parse_video_creation(iso_str: str) -> float:
    """Parse ISO 'YYYY-MM-DDTHH:MM:SS[.ssssss]Z' → unix timestamp (UTC)."""
    if not iso_str: return 0.0
    import datetime
    try:
        s = iso_str.rstrip("Z")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def ya_medido(conn, video_id: int, audio_id: int) -> bool:
    """¿Otro metodo ya midio este par? Entonces sync-v2 no lo vuelve a escribir.

    `--skip-existing` solo salta videos con pares manual/locked; un par por
    transcript, refinado y confirmado por ventana larga, se duplicaba con el
    offset crudo al re-correr el pipeline para sumar un dia (Asistente,
    2026-09-29: 10 pares, 98 ms de diferencia en VICG_9470).
    """
    return conn.execute(
        "SELECT 1 FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
        (video_id, audio_id)).fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--videos", help="CSV de video_clip ids o substring de filename")
    ap.add_argument("--audio-like", help="LIKE pattern para filtrar audios")
    ap.add_argument("--temporal-slack", type=float, default=3600.0,
                    help="Slack (seg) para filtro temporal video↔audio")
    ap.add_argument("--min-voice-sim", type=float, default=0.45,
                    help="Voice sim mínimo para considerar candidato")
    ap.add_argument("--top-k-candidates", type=int, default=3,
                    help="Cuántos top audios procesar con transcript")
    ap.add_argument("--min-anchors-write", type=int, default=50,
                    help="Anchors mínimos para escribir a audio_sync_pairs")
    ap.add_argument("--max-mad-write", type=float, default=0.15,
                    help="MAD máximo entre segmentos para escribir directo")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Saltar videos que ya tienen sync_pair con method='manual%%' o '-locked'")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_pipeline_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    conn = manifest.conectar(str(db))
    now = time.time()

    # Verificar schema
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audio_sync_pairs)")}
    if "classification" not in cols:
        sys.exit("ERROR: schema sync v2 no inicializado. Correr bin/init_sync_schema.py primero.")

    # 1. Obtener videos a procesar
    where = "WHERE c.file_kind='video' AND c.has_audio=1 AND c.index_status='ok'"
    params: list = []
    if args.videos:
        # Interpretar como substring(s) del filename (separados por coma)
        substrings = [s.strip() for s in args.videos.split(",") if s.strip()]
        conds = " OR ".join(["c.filename LIKE ?" for _ in substrings])
        where += f" AND ({conds})"
        for s in substrings:
            params.append(f"%{s}%")
    videos = conn.execute(f"""
        SELECT c.id, c.filename, c.path, c.duration_sec, c.creation_time
        FROM clips c {where}
        ORDER BY c.filename
    """, params).fetchall()
    logging.info(f"Videos a procesar: {len(videos)}")

    # 2. Obtener audios candidatos (con mtime)
    audio_where = "WHERE file_kind='audio' AND index_status='ok' AND duration_sec >= 60"
    audio_params: list = []
    if args.audio_like:
        audio_where += " AND rel_path LIKE ?"
        audio_params.append(args.audio_like)
    audio_rows = conn.execute(
        f"SELECT id, path, filename, duration_sec FROM clips {audio_where}",
        audio_params
    ).fetchall()
    audios = []
    for aid, apath, afn, adur in audio_rows:
        try: mtime = os.stat(apath).st_mtime
        except OSError: mtime = 0.0
        audios.append({"id": aid, "path": apath, "filename": afn,
                       "dur": adur or 0.0, "mtime": mtime})
    logging.info(f"Audios candidatos: {len(audios)}")

    # Detectar si los mtimes son post-copia (no fiables para validación temporal)
    # Heurística: si todos los mtimes están agrupados en una ventana < 30 días
    # Y la mediana es > 1 año después del primer video, los mtimes son post-copia
    mtimes = sorted(a["mtime"] for a in audios if a["mtime"] > 0)
    timestamps_trustworthy = True
    if mtimes:
        mtime_span = mtimes[-1] - mtimes[0]
        median_mtime = mtimes[len(mtimes)//2]
        # Comparar con primer video creation_time
        first_v = conn.execute(
            "SELECT creation_time FROM clips WHERE file_kind='video' "
            "AND creation_time IS NOT NULL ORDER BY creation_time LIMIT 1"
        ).fetchone()
        if first_v and first_v[0]:
            first_video_unix = parse_video_creation(first_v[0])
            if first_video_unix and median_mtime - first_video_unix > 365 * 86400:
                timestamps_trustworthy = False
                logging.info(f"  ⚠ mtimes audio agrupados {(median_mtime - first_video_unix)/86400:.0f} "
                             f"días después del primer video — SKIP filtro temporal "
                             f"(timestamps post-copia)")
    if timestamps_trustworthy:
        logging.info(f"  ✓ timestamps confiables; filtro temporal ACTIVO")

    stats = {"written_pairs": 0, "candidates": 0, "skip_existing": 0,
             "no_candidates": 0, "rejected": 0}

    for vid_id, vfn, vpath, vdur, vcreation_iso in videos:
        v_creation_unix = parse_video_creation(vcreation_iso or "")
        if not v_creation_unix:
            try: v_creation_unix = os.stat(vpath).st_mtime
            except OSError: v_creation_unix = 0.0

        # Skip si ya tiene sync manual/locked y --skip-existing
        if args.skip_existing:
            existing = conn.execute("""
                SELECT method FROM audio_sync_pairs
                WHERE video_clip_id=? AND (method LIKE 'manual%' OR method LIKE '%-locked')
            """, (vid_id,)).fetchall()
            if existing:
                stats["skip_existing"] += 1
                continue

        # 1. Filtro temporal: audios cuyo mtime puede contener al video
        # (skip si timestamps no son confiables — post-copia)
        if not timestamps_trustworthy:
            temporal_ok = list(audios)
        else:
            temporal_ok = []
            for a in audios:
                if not v_creation_unix or not a["mtime"]:
                    temporal_ok.append(a); continue
                ok, _ = temporal_validity(a["mtime"], a["dur"], v_creation_unix,
                                           slack_sec=args.temporal_slack)
                if ok:
                    temporal_ok.append(a)
            if not temporal_ok:
                stats["no_candidates"] += 1
                continue

        # 2. Voice-first: rankear por voice_sim
        expected_emb = expected_voice_embedding_for_video(conn, vid_id)
        scored = []
        if expected_emb is not None:
            for a in temporal_ok:
                sim, status = score_audio_candidate(conn, a["id"], expected_emb)
                scored.append((a, sim, status))
            scored.sort(key=lambda x: -x[1])
        else:
            # Sin voice match disponible: dejar candidatos en orden temporal
            scored = [(a, 0.0, "no-voice-match") for a in temporal_ok]

        # Tomar top-K
        top_candidates = scored[:args.top_k_candidates]
        logging.info(f"\n[{vid_id}] {vfn}: {len(temporal_ok)} temporal-ok, "
                     f"top-{len(top_candidates)} candidates")

        # 3. Para cada candidato top: sync por transcript
        wv = load_words(tr_dir, vid_id)
        results_per_video = []
        for a, sim, vm_status in top_candidates:
            wa = load_words(tr_dir, a["id"])
            if not wv or not wa:
                results_per_video.append((a, sim, None, "no_data",
                                         {"reason": "sin transcripts"}))
                continue
            # Offset rough con transcript_sync.align (cluster denso, robusto)
            rough = transcript_align(wv, wa, ngram=4, tol=0.6)
            if rough is None:
                # Probar con n=3 si n=4 no encontró
                rough = transcript_align(wv, wa, ngram=3, tol=1.0)
            if rough is None:
                results_per_video.append((a, sim, None, "no_data",
                                         {"reason": "no anchors comunes suficientes",
                                          "voice_sim": round(sim,3)}))
                continue
            rough_off, rough_conf, rough_n = rough
            if rough_conf < 0.05:  # menos del 5% de n-gramas votaron mismo cluster
                results_per_video.append((a, sim, None, "no_data",
                                         {"reason": f"rough_conf={rough_conf:.3f} muy bajo",
                                          "voice_sim": round(sim,3),
                                          "rough_offset": rough_off}))
                continue
            # 4. Segment offsets centrados en rough_off
            segs = segment_offsets(wv, wa, expected=rough_off,
                                    seg_sec=60.0, n_gram=3, window=3.0)
            multi_score, _ = multitake_score(wv, wa, n=4)
            status, info = classify_pair(segs, bd_off=None,
                                          multi_score=multi_score)
            info["voice_sim"] = round(sim, 3)
            info["rough_offset"] = round(rough_off, 3)
            results_per_video.append((a, sim, segs, status, info))

        # 5. Decidir el ganador
        # Prioridad: voice_sim alto + status 'ok'/'bias_refinable'
        def rank_key(r):
            a, sim, segs, status, info = r
            pri = STATUS_PRIORITY.get(status, 99)
            # Premiar voice_sim alto, penalizar status problemático
            return (pri, -sim)
        results_per_video.sort(key=rank_key)
        if not results_per_video:
            continue
        best = results_per_video[0]
        a, sim, segs, status, info = best

        if status == "no_data" or info.get("reason","").startswith("sin"):
            logging.info(f"  ⊘ NO_DATA: best candidate {a['filename']} sim={sim:.2f}")
            stats["rejected"] += 1
            continue

        # 6. Escritura
        offset_phys = info.get("median_offset")
        if offset_phys is None and segs:
            offset_phys = float(np.median([s["offset"] for s in segs]))
        if offset_phys is None:
            offset_phys = info.get("rough_offset", 0.0)

        write_to_pairs = (status in ("ok", "bias_refinable") and
                          info.get("total_anchors", 0) >= args.min_anchors_write and
                          info.get("mad_segments", 99) <= args.max_mad_write)

        signals = {
            "voice_sim": float(sim),
            "voice_match_status": vm_status,
            "transcript_segments": len(segs) if segs else 0,
            "total_anchors": info.get("total_anchors", 0),
            "mad_segments": info.get("mad_segments"),
            "multitake_score": info.get("multitake_score"),
            "classification": status,
            "rough_offset": info.get("rough_offset"),
        }
        signals_json = json.dumps(signals)

        # Un par que otro metodo ya midio no se duplica. Al sumar el segundo dia
        # (Asistente, 2026-09-29) esto escribia de nuevo 10 pares del primero,
        # ya refinados y confirmados por ventana larga, con el offset crudo:
        # 98 ms de diferencia en VICG_9470, y dos filas para el mismo par.
        if ya_medido(conn, vid_id, a["id"]):
            stats["ya_medidos"] = stats.get("ya_medidos", 0) + 1
            logging.info(f"  = YA MEDIDO por otro metodo: {a['filename']} (no se duplica)")
            continue
        if write_to_pairs:
            method = "voice-first+transcript-segments"
            confidence = min(0.95, 0.50 + sim * 0.3 + (0.15 if status=="ok" else 0.0))
            if not args.dry_run:
                conn.execute("""
                    INSERT INTO audio_sync_pairs
                    (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                     identity_score, classification, signals_json, notes, created_at,
                     verifier_passed)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (vid_id, a["id"], method, offset_phys, confidence, sim,
                      status, signals_json,
                      f"Auto: {STATUS_LABEL[status]} {info.get('reason','')}",
                      now, 1))
            stats["written_pairs"] += 1
            logging.info(f"  ✓ WRITTEN: {a['filename']} off={offset_phys:+.3f} "
                         f"status={STATUS_LABEL[status]} sim={sim:.2f}")
        else:
            # A sync_candidates
            if not args.dry_run:
                conn.execute("""
                    INSERT INTO sync_candidates
                    (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                     identity_score, classification, status, signals_json, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,'needs_review',?,?,?)
                """, (vid_id, a["id"], "voice-first+transcript-segments",
                      offset_phys, min(0.80, 0.30 + sim*0.3),
                      sim, status, signals_json,
                      f"Needs review: {STATUS_LABEL[status]} {info.get('reason','')}",
                      now))
            stats["candidates"] += 1
            logging.info(f"  → CANDIDATE: {a['filename']} off={offset_phys:+.3f} "
                         f"status={STATUS_LABEL[status]} sim={sim:.2f}")

    # 7. Propagación a hermanos
    if not args.dry_run:
        sib_propagated = propagate_siblings(conn, now, logging)
        stats["siblings_propagated"] = sib_propagated

    if not args.dry_run:
        conn.commit()

    logging.info(f"\n=== RESUMEN ===")
    for k, v in stats.items():
        logging.info(f"  {k}: {v}")
    conn.close()


def propagate_siblings(conn: sqlite3.Connection, now: float, log) -> int:
    """Para cada audio en audio_sync_pairs (recién escrito), buscar su hermano
    en lavalier_pairs.applicable=1 y agregar el sync derivado.
    """
    n_propagated = 0
    # Recorrer pairs recién escritos
    rows = conn.execute("""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec,
               sp.confidence, sp.identity_score
        FROM audio_sync_pairs sp
        WHERE sp.method = 'voice-first+transcript-segments'
          AND sp.created_at >= ?
    """, (now - 60,)).fetchall()  # los de esta corrida

    for sp_id, vid, aid, off, conf, idscore in rows:
        # Buscar hermano
        sib_row = conn.execute("""
            SELECT
              CASE WHEN audio_a_id = ? THEN audio_b_id ELSE audio_a_id END AS sibling_id,
              CASE WHEN audio_a_id = ? THEN delta_sec ELSE -delta_sec END AS delta
            FROM lavalier_pairs
            WHERE (audio_a_id=? OR audio_b_id=?) AND applicable=1
        """, (aid, aid, aid, aid)).fetchone()
        if not sib_row:
            continue
        sib_id, delta = sib_row
        # ¿Ya existe sync para sib?
        exists = conn.execute(
            "SELECT id FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
            (vid, sib_id)
        ).fetchone()
        if exists:
            continue
        # offset_sib = offset_aid + delta
        offset_sib = off + delta
        conn.execute("""
            INSERT INTO audio_sync_pairs
            (video_clip_id, audio_clip_id, method, offset_sec, confidence,
             identity_score, classification, notes, created_at, verifier_passed)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (vid, sib_id, "lavalier-sibling-derived", offset_sib,
              max(0.70, conf * 0.9), idscore, "ok",
              f"Derivado de aid={aid} (delta_sec={delta:+.3f}s)",
              now, 1))
        n_propagated += 1
        log.info(f"  ↪ sibling: vid={vid} aid_orig={aid} → aid_sib={sib_id} "
                 f"off={offset_sib:+.3f}")
    return n_propagated


if __name__ == "__main__":
    main()
