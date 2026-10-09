#!/usr/bin/env python3
"""Auditoría multi-señal de entrevistas/escenas habladas PERDIDAS.

Doctrina (iter9.8, 2026-05-27): el caso 2644 (entrevista de 11 min perdida
por A1 alucinado) reveló que la detección de entrevistas con UN solo filtro
secuencial deja grietas. Este script aplica CUATRO lentes independientes
en paralelo (todos deterministas, queries SQL + lectura de transcripts) y
combina sus hallazgos.

NO usa subagentes LLM para la parte mecánica (queries) — eso sería caro y
redundante. Los subagentes/Claude se reservan para el JUICIO sobre los
candidatos ambiguos que este script marca.

Los 4 lentes:
  A. Caras en cuadro: face_detections >= 15, sin sync
  B. Audio activo: audio_rms_db > -45dB, sin sync
  C. Transcript: clean_words >= 30 O alucinado pero dur >= 120s, sin sync
  D. Posición temporal: clip sin sync rodeado de entrevistas con sync

Cada candidato recibe un SCORE = nº de lentes que lo señalan. Score alto
= alta probabilidad de entrevista perdida. Score 1 = revisar manualmente.

Uso:
    bin/audit_lost_interviews.py --root <disk>
    bin/audit_lost_interviews.py --root <disk> --min-score 2
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def load_text(tr_dir: Path, cid: int, limit: int = 2000) -> str:
    p = tr_dir / f"{cid}.json"
    if not p.exists():
        return ""
    try:
        return (json.load(p.open()).get("text", "") or "")[:limit]
    except Exception:
        return ""


QUESTION_RE = re.compile(r"¿[^?]{6,}\?")
SELF_ID_RE = re.compile(r"\b(yo soy|mi nombre es|mi proyecto|me llamo|soy hermano de)\b", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-score", type=int, default=1,
                    help="Mínimo de lentes que deben señalar un clip para reportarlo")
    ap.add_argument("--min-dur", type=int, default=60)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"❌ No existe manifest: {db}\n"
                 f"   ¿Está montado el disco? Volúmenes: "
                 f"{[p.name for p in Path('/Volumes').iterdir()] if Path('/Volumes').exists() else '?'}")

    conn = manifest.conectar(str(db))

    # signals[cid] = {"clip": (..), "lenses": set(), "evidence": [..]}
    signals: dict[int, dict] = {}

    def mark(cid, lens, evidence, clipinfo):
        s = signals.setdefault(cid, {"clip": clipinfo, "lenses": set(), "evidence": []})
        s["lenses"].add(lens)
        s["evidence"].append(f"[{lens}] {evidence}")

    # ---- LENTE A: caras en cuadro ----
    for cid, fn, dur, faces, personas in conn.execute(f"""
        SELECT c.id, c.filename, ROUND(c.duration_sec) AS dur,
               (SELECT COUNT(*) FROM face_detections WHERE clip_id=c.id) AS faces,
               (SELECT COUNT(DISTINCT fi.identity_id) FROM face_detections fd
                  JOIN face_identities fi ON fi.detection_id=fd.id WHERE fd.clip_id=c.id) AS personas
        FROM clips c
        WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= {args.min_dur}
          AND (SELECT COUNT(*) FROM face_detections WHERE clip_id=c.id) >= 15
          AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
    """):
        mark(cid, "CARAS", f"{faces} detecciones, {personas} personas", (fn, dur))

    # ---- LENTE B: audio activo ----
    try:
        for cid, fn, dur, rms in conn.execute(f"""
            SELECT c.id, c.filename, ROUND(c.duration_sec) AS dur, ROUND(ca.audio_rms_db,1)
            FROM clips c JOIN clip_analysis ca ON ca.clip_id=c.id
            LEFT JOIN clip_descriptions d ON d.clip_id=c.id
            WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= 90
              AND ca.audio_rms_db > -45
              AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
              AND (d.category IS NULL OR d.category != 'entrevista-solo-video')
        """):
            mark(cid, "AUDIO", f"rms={rms}dB (voz activa)", (fn, dur))
    except sqlite3.OperationalError:
        pass

    # ---- LENTE C: transcript (denso o alucinado largo) ----
    for cid, fn, dur, halluc, clean_w in conn.execute(f"""
        SELECT c.id, c.filename, ROUND(c.duration_sec) AS dur,
               COALESCE(tq.is_hallucinated,0), COALESCE(tq.clean_words_count,0)
        FROM clips c LEFT JOIN transcript_quality tq ON tq.clip_id=c.id
        WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= {args.min_dur}
          AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
          AND (tq.clean_words_count >= 30 OR (tq.is_hallucinated=1 AND c.duration_sec >= 120))
    """):
        text = load_text(tr_dir, cid)
        n_q = len(QUESTION_RE.findall(text))
        has_selfid = bool(SELF_ID_RE.search(text))
        if halluc == 1:
            mark(cid, "TRANSCRIPT", f"alucinado pero dur {dur}s (A1 contaminado?)", (fn, dur))
        elif n_q >= 2 or has_selfid:
            ev = f"{n_q} preguntas" + (", auto-ID presente" if has_selfid else "")
            mark(cid, "TRANSCRIPT", ev, (fn, dur))

    # ---- LENTE D: posición temporal (rodeado de entrevistas con sync) ----
    synced = {}
    for vid, fn, dur in conn.execute("""
        SELECT DISTINCT v.id, v.filename, ROUND(v.duration_sec)
        FROM audio_sync_pairs sp JOIN clips v ON v.id=sp.video_clip_id
    """):
        m = re.search(r"_(\d{4})\.", fn)
        if m:
            synced[int(m.group(1))] = dur
    synced_long = sorted(n for n, d in synced.items() if d >= 200)

    for cid, fn, dur in conn.execute(f"""
        SELECT c.id, c.filename, ROUND(c.duration_sec)
        FROM clips c
        WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= 120
          AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
    """):
        m = re.search(r"_(\d{4})\.", fn)
        if not m:
            continue
        num = int(m.group(1))
        # ¿Hay entrevista larga con sync dentro de ±5 números?
        nearby = [n for n in synced_long if abs(n - num) <= 5 and n != num]
        if nearby:
            mark(cid, "POSICION", f"rodeado de entrevistas sync: {nearby}", (fn, dur))

    # ---- Combinar y reportar ----
    results = []
    for cid, s in signals.items():
        fn, dur = s["clip"]
        results.append({
            "cid": cid, "fn": fn, "dur": dur,
            "score": len(s["lenses"]),
            "lenses": sorted(s["lenses"]),
            "evidence": s["evidence"],
        })
    results.sort(key=lambda r: (-r["score"], -r["dur"]))

    print(f"\n=== AUDITORÍA MULTI-SEÑAL DE ENTREVISTAS PERDIDAS ===")
    print(f"(clips de video sin sync señalados por ≥{args.min_score} lente(s))\n")
    n_reported = 0
    for r in results:
        if r["score"] < args.min_score:
            continue
        n_reported += 1
        bars = "🔴" if r["score"] >= 3 else ("🟡" if r["score"] == 2 else "⚪")
        print(f"{bars} score={r['score']}/4  clip={r['cid']}  {r['fn']}  ({r['dur']}s)")
        print(f"    lentes: {', '.join(r['lenses'])}")
        for ev in r["evidence"]:
            print(f"      {ev}")
        print()

    print(f"Total candidatos (score≥{args.min_score}): {n_reported}")
    print(f"  🔴 score≥3 (alta confianza): {sum(1 for r in results if r['score']>=3)}")
    print(f"  🟡 score=2 (probable): {sum(1 for r in results if r['score']==2)}")
    print(f"  ⚪ score=1 (revisar): {sum(1 for r in results if r['score']==1)}")
    print(f"\nPara rescatar: ver doctrina pasos-a-seguir §6c (waveform A1↔audios sin sync).")
    conn.close()


if __name__ == "__main__":
    main()
