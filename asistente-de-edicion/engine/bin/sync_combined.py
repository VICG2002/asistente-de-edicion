#!/usr/bin/env python3
"""Sync unificado multi-señal con fusión por convergencia.

Caso fundador (Zezzions 2026-05-26): el sync histórico corría 5 scripts
separados (transcript, questions, phrases, acoustic, chromaprint), cada
uno con su propio umbral fijo. Resultado: 2645 (ENTREVISTADO_13 y ENTREVISTADO_10 dual-lavalier)
fue ignorado por todos porque cada método individual quedaba debajo de
0.30, aunque las 4 medidas convergían en offset≈-19.5s.

Este script:
  1. Lee TODOS los pares video-audio candidatos.
  2. Para cada candidato, recolecta señales de TODOS los métodos disponibles.
  3. Usa `lib.sync_fusion.fuse()` para combinar las señales y decidir.
  4. Escribe en `audio_sync_pairs` los pares aceptados con method='combined',
     notes con detalle de cuántos métodos votaron.

NO sobreescribe pares existentes con conf >= 0.85 (que ya son confiables).
SÍ sobreescribe pares con conf < 0.50 si el combinado da mejor score.

Uso:
    bin/sync_combined.py --root /Volumes/.../Zezzions VICG
    bin/sync_combined.py --root ... --location video --replace-marginal
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sync_fusion import SyncSignal, fuse
from lib.waveform_sync import extract_envelope, correlate as env_correlate
from lib.chromaprint_sync import extract_fingerprint, correlate_fingerprints
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def collect_transcript_signal(conn, vid: int, aid: int) -> SyncSignal | None:
    """Lee señal de sync por transcript que pueda existir en audio_sync_pairs
    (creada por sync_transcript / sync_by_phrases / sync_by_questions)."""
    row = conn.execute(
        "SELECT method, offset_sec, confidence FROM audio_sync_pairs "
        "WHERE video_clip_id=? AND audio_clip_id=? "
        "AND method IN ('transcript','phrase-match','question-anchor','phrases','questions') "
        "ORDER BY confidence DESC LIMIT 1",
        (vid, aid),
    ).fetchone()
    if not row:
        return None
    method, off, conf = row
    return SyncSignal(method=method or "transcript", offset=off or 0.0, conf=conf or 0.0)


def collect_envelope_signal(vpath: Path, apath: Path,
                            cache: dict[str, object]) -> SyncSignal | None:
    """Envelope FFT cross-correlation. Cache por path."""
    ve = cache.get(("env", str(vpath)))
    if ve is None:
        ve = extract_envelope(vpath)
        cache[("env", str(vpath))] = ve
    ae = cache.get(("env", str(apath)))
    if ae is None:
        ae = extract_envelope(apath)
        cache[("env", str(apath))] = ae
    if ve is None or ae is None:
        return None
    off, conf = env_correlate(ve, ae)
    return SyncSignal(method="envelope", offset=off, conf=conf)


def collect_chromaprint_signal(vpath: Path, apath: Path,
                               cache: dict[str, object]) -> SyncSignal | None:
    vfp = cache.get(("fp", str(vpath)))
    if vfp is None:
        vfp = extract_fingerprint(vpath)
        cache[("fp", str(vpath))] = vfp
    afp = cache.get(("fp", str(apath)))
    if afp is None:
        afp = extract_fingerprint(apath)
        cache[("fp", str(apath))] = afp
    if not vfp or not afp:
        return None
    r = correlate_fingerprints(vfp, afp)
    if r is None:
        return None
    off, conf = r
    return SyncSignal(method="chromaprint", offset=off, conf=conf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--video-location", default="video",
                    help="rel_path substring (case-insensitive) para filtrar videos")
    ap.add_argument("--audio-like", default="%lavas%")
    ap.add_argument("--min-video-dur", type=float, default=30.0)
    ap.add_argument("--replace-marginal", action="store_true",
                    help="Sobrescribe pares existentes con conf < 0.50 si el combinado los mejora")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_combined_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Candidatos: videos con audio + suficiente duración.
    videos = conn.execute(
        """SELECT c.id, c.filename, c.path
            FROM clips c WHERE c.file_kind='video' AND c.index_status='ok'
              AND c.has_audio=1 AND c.duration_sec >= ?
              AND lower(c.rel_path) LIKE ?""",
        (args.min_video_dur, f"%{args.video_location.lower()}%"),
    ).fetchall()

    audios = conn.execute(
        "SELECT id, filename, rel_path, path FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' AND duration_sec >= 30 "
        "AND lower(rel_path) LIKE ?",
        (args.audio_like,),
    ).fetchall()

    logging.info(f"Videos: {len(videos)}, Audios: {len(audios)}")

    cache: dict[str, object] = {}
    n_new = 0
    n_replaced = 0
    n_rejected_divergent = 0

    for vid, vfn, vpath in videos:
        signals_by_audio: dict[int, list[SyncSignal]] = {}
        for aid, afn, arel, apath in audios:
            sigs: list[SyncSignal] = []
            # 1. Señales de transcript/phrases/questions (ya en BD)
            tsig = collect_transcript_signal(conn, vid, aid)
            if tsig:
                sigs.append(tsig)
            # 2. Envelope
            esig = collect_envelope_signal(Path(vpath), Path(apath), cache)
            if esig:
                sigs.append(esig)
            # 3. Chromaprint
            csig = collect_chromaprint_signal(Path(vpath), Path(apath), cache)
            if csig:
                sigs.append(csig)
            if sigs:
                signals_by_audio[aid] = sigs

        if not signals_by_audio:
            continue

        # Decidir por audio
        for aid, sigs in signals_by_audio.items():
            fused = fuse(sigs)
            if not fused or fused.diverged:
                if fused and fused.diverged:
                    n_rejected_divergent += 1
                continue

            # Política de escritura
            cur = conn.execute(
                "SELECT id, confidence FROM audio_sync_pairs "
                "WHERE video_clip_id=? AND audio_clip_id=? "
                "ORDER BY confidence DESC LIMIT 1",
                (vid, aid),
            ).fetchone()
            now = time.time()
            note = (f"fusión: {fused.notes}; "
                    f"vs anteriores={[f'{s.method}={s.offset:+.1f}/c{s.conf:.2f}' for s in sigs]}")
            if cur is None:
                if args.dry_run:
                    logging.info(f"  [DRY] NEW {vfn}↔aid={aid} off={fused.offset:+.1f} conf={fused.conf:.2f}")
                else:
                    conn.execute(
                        "INSERT INTO audio_sync_pairs "
                        "(video_clip_id, audio_clip_id, method, offset_sec, confidence, notes, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (vid, aid, "combined", fused.offset, fused.conf, note, now),
                    )
                    n_new += 1
                logging.info(f"  + {vfn}↔aid={aid} off={fused.offset:+.1f} conf={fused.conf:.2f} votos={fused.votes}")
            elif args.replace_marginal and cur[1] < 0.50 and fused.conf > cur[1]:
                if args.dry_run:
                    logging.info(f"  [DRY] REPL {vfn}↔aid={aid} {cur[1]:.2f}→{fused.conf:.2f}")
                else:
                    conn.execute(
                        "UPDATE audio_sync_pairs SET method=?, offset_sec=?, confidence=?, notes=?, created_at=? WHERE id=?",
                        ("combined", fused.offset, fused.conf, note, now, cur[0]),
                    )
                    n_replaced += 1
                logging.info(f"  ↑ {vfn}↔aid={aid} {cur[1]:.2f}→{fused.conf:.2f} off={fused.offset:+.1f}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"sync_combined: nuevos={n_new}, mejorados={n_replaced}, "
                 f"rechazados-divergentes={n_rejected_divergent}")


if __name__ == "__main__":
    main()
