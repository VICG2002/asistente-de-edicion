#!/usr/bin/env python3
"""Auditoría exhaustiva de transcripts cacheados — calcula calidad y decide
qué clips necesitan re-transcripción antes del sync.

Caso fundador (Zezzions 2026-05-26): de 274 transcripts, 59 (22%) están
completamente alucinados ("Suscríbete al canal" ×1517 en un audio de 1h),
15 (5%) son degradados parcial (zona limpia corta + alucinación masiva
después). Cualquier sync transcript-based sobre estos produce offsets
inválidos.

Este script:
  1. Para cada transcript en `<root>/.cinema_assistant/transcripts/*.json`,
     llama `lib.transcript_quality.analyze_transcript(text, words)`.
  2. Persiste en tabla `transcript_quality` con todos los detalles.
  3. Marca `needs_retranscribe=1` para los críticos.
  4. Imprime reporte de cobertura.

Uso:
    bin/audit_transcripts.py --root /Volumes/.../Zezzions VICG
    bin/audit_transcripts.py --root ... --print-needs    # listar los que faltan
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.transcript_quality import analyze_transcript
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def needs_retranscription(is_hallucinated: bool, total_words: int,
                          clean_words: int, clip_kind: str,
                          clip_duration: float, clip_category: str) -> bool:
    """Política de re-transcripción:
       - Hallucinated + clip relevante (audio externo o video largo) → SÍ.
       - clean_words >= 60 → NO (zona limpia suficiente para sync).
       - total_words < 30 → NO (B-roll corto, no se sync por transcript).
    """
    if not is_hallucinated:
        # Aún sin alucinación, si tiene menos de 30 palabras totales pero el
        # clip es largo (>120s) Y tiene audio activo, sospechoso. Re-transcribir.
        if total_words < 30 and clip_duration > 120 and clip_kind == "audio":
            return True
        return False
    # Hallucinated: re-transcribir si es audio externo O si el clip es
    # largo y categorizado como entrevista.
    if clip_kind == "audio":
        return True
    if clip_duration >= 60 and (clip_category or "").startswith("entrevista"):
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--print-needs", action="store_true",
                    help="Solo listar clips que necesitan re-transcripción")
    ap.add_argument("--reset", action="store_true",
                    help="Borrar transcript_quality y rehacer todo el audit")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")
    if not tr_dir.exists():
        sys.exit(f"No transcripts at {tr_dir}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcript_quality (
            clip_id INTEGER PRIMARY KEY,
            is_hallucinated INTEGER,
            clean_until_sec REAL,
            clean_words_count INTEGER,
            total_words_count INTEGER,
            garbage_ratio REAL,
            reasons_json TEXT,
            needs_retranscribe INTEGER,
            last_audited REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    if args.reset:
        conn.execute("DELETE FROM transcript_quality")
        conn.commit()

    if args.print_needs:
        rows = conn.execute("""
            SELECT tq.clip_id, c.filename, c.file_kind, c.duration_sec,
                   tq.total_words_count, tq.clean_words_count, tq.is_hallucinated
            FROM transcript_quality tq JOIN clips c ON c.id=tq.clip_id
            WHERE tq.needs_retranscribe=1
            ORDER BY c.file_kind, c.duration_sec DESC
        """).fetchall()
        print(f"Clips que necesitan re-transcripción: {len(rows)}")
        for cid, fn, kind, dur, tw, cw, hal in rows:
            flag = "HAL" if hal else "    "
            print(f"  [{kind:5}] cid={cid:<4} {fn:<40} dur={dur:6.0f}s "
                  f"total_words={tw:<5} clean={cw:<5} {flag}")
        return

    # Auditoría completa
    clips = {row[0]: row for row in conn.execute(
        "SELECT id, filename, file_kind, duration_sec FROM clips WHERE index_status='ok'"
    ).fetchall()}
    # clip_descriptions la crean pasos posteriores del pipeline; en un
    # proyecto fresco el audit corre antes (detectado en FCC 2026-07-09).
    try:
        descriptions = {row[0]: row[1] or "" for row in conn.execute(
            "SELECT clip_id, category FROM clip_descriptions"
        ).fetchall()}
    except sqlite3.OperationalError:
        descriptions = {}

    now = time.time()
    counters = {
        "empty_text": 0, "no_words": 0,
        "hallucinated_full": 0, "degraded_partial": 0,
        "short_clean": 0, "good_clean": 0,
        "needs_retranscribe": 0,
    }
    samples_hal: list = []
    samples_partial: list = []

    files = sorted(tr_dir.glob("*.json"))
    for tp in files:
        if tp.name.startswith("._"):
            continue
        try:
            cid = int(tp.stem)
        except ValueError:
            continue
        if cid not in clips:
            continue
        clip_id, fn, kind, dur = clips[cid]
        category = descriptions.get(cid, "")

        try:
            with tp.open() as f:
                tr = json.load(f)
        except Exception:
            continue
        text = (tr.get("text") or "").strip()
        words = tr.get("words") or []

        if not text:
            counters["empty_text"] += 1
            needs = (kind == "audio" or (dur >= 60 and category.startswith("entrevista")))
            conn.execute(
                "INSERT OR REPLACE INTO transcript_quality "
                "(clip_id, is_hallucinated, clean_until_sec, clean_words_count, "
                "total_words_count, garbage_ratio, reasons_json, "
                "needs_retranscribe, last_audited) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, 0, 0.0, 0, 0, 0.0, json.dumps(["empty_text"]),
                 int(needs), now)
            )
            if needs:
                counters["needs_retranscribe"] += 1
            continue

        if not words:
            counters["no_words"] += 1
            # transcript con text pero sin word timestamps — inútil para sync
            conn.execute(
                "INSERT OR REPLACE INTO transcript_quality "
                "(clip_id, is_hallucinated, clean_until_sec, clean_words_count, "
                "total_words_count, garbage_ratio, reasons_json, "
                "needs_retranscribe, last_audited) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, 0, 0.0, 0, 0, 0.0, json.dumps(["no_words_timestamps"]),
                 1, now)
            )
            counters["needs_retranscribe"] += 1
            continue

        result = analyze_transcript(text, words)
        is_hal = bool(result.get("is_hallucinated"))
        clean_until = float(result.get("clean_until_sec") or 0.0)
        clean_words = int(result.get("clean_words") or 0)
        total_words = len(words)
        garbage_ratio = float(result.get("garbage_ratio") or 0.0)
        reasons = result.get("reasons", [])

        if is_hal:
            counters["hallucinated_full"] += 1
            if len(samples_hal) < 5:
                samples_hal.append((cid, fn, reasons[:2]))
        elif clean_words < total_words * 0.85 and clean_until > 0:
            counters["degraded_partial"] += 1
            if len(samples_partial) < 5:
                samples_partial.append((cid, fn, clean_until, total_words - clean_words))
        elif clean_words >= 60:
            counters["good_clean"] += 1
        else:
            counters["short_clean"] += 1

        needs = needs_retranscription(is_hal, total_words, clean_words,
                                       kind, dur or 0.0, category)
        if needs:
            counters["needs_retranscribe"] += 1

        conn.execute(
            "INSERT OR REPLACE INTO transcript_quality "
            "(clip_id, is_hallucinated, clean_until_sec, clean_words_count, "
            "total_words_count, garbage_ratio, reasons_json, "
            "needs_retranscribe, last_audited) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, int(is_hal), clean_until, clean_words, total_words,
             garbage_ratio, json.dumps(reasons[:8]),
             int(needs), now)
        )

    conn.commit()

    print(f"Transcripts auditados: {sum(counters.values()) - counters['needs_retranscribe']} "
          f"(de {len(files)} JSON)")
    print("Distribución:")
    for k in ["empty_text", "no_words", "hallucinated_full", "degraded_partial",
              "short_clean", "good_clean"]:
        print(f"  {k:<25} {counters[k]}")
    print()
    print(f"⚠ NECESITAN RE-TRANSCRIPCIÓN: {counters['needs_retranscribe']}")
    print()
    print("Ejemplos alucinados completos (cid, filename, reasons):")
    for s in samples_hal:
        print(f"  {s[0]:<4} {s[1]:<40} {s[2]}")
    print()
    print("Ejemplos degradados parcial (cid, filename, clean_until_sec, hallucinated_words):")
    for s in samples_partial:
        print(f"  {s[0]:<4} {s[1]:<40} clean_until={s[2]:.0f}s  hal_words={s[3]}")
    conn.close()


if __name__ == "__main__":
    main()
