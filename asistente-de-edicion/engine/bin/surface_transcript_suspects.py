#!/usr/bin/env python3
"""Saca a revisión los SOSPECHOSOS de alucinación/garble del transcript.

Origen (FCC 2026-07-11): la curaduría de preguntas destapó garble que el
audit estadístico NO puede ver porque parece texto válido: "Satriacit
Drive" (Satyajit Ray), "Regin Bull" (Raging Bull), "la plodería" (la
curaduría), "el P.O.P./el peón" (nombre del club). Instrucción del usuario:
*"falta un proceso de verdadera curaduría del transcript para verificar que
no haya alucinaciones"*. El filtro estadístico caza la alucinación masiva;
el garble plausible solo lo caza la COMPRENSIÓN — este script hace esa
curaduría ejecutable: junta en un solo reporte todo lo que un curador
(Claude) debe leer y validar, en vez de releer horas de transcript.

Secciones del reporte (markdown en <root>/.cinema_assistant/reports/):
  1. PREGUNTAS sin question_short (curaduría pendiente) y todas las
     question_short existentes (para re-validación).
  2. AUTO-IDs y menciones de nombre ("soy X", "me llamo X", "gracias X") —
     el cast se contamina si el nombre viene garblado.
  3. TOKENS RAROS CAPITALIZADOS: palabras tipo nombre-propio con frecuencia
     baja en el corpus — candidatos #1 a garble (títulos de películas,
     nombres, lugares). Con contexto para poder juzgar.
  4. TRANSCRIPTS degradados/alucinados según transcript_quality (recap del
     audit estadístico).

El curador corrige en: question_segments.question_short, cast.json,
vocabulary_hints del project_config + re-corre correct_transcripts_vocab.

Uso:
    bin/surface_transcript_suspects.py --root <disk> [--max-freq 3]
        [--min-len 4] [--out reports/curaduria-transcript-<fecha>.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from derive_chrono_sync import resolve_root  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

AUTOID_RE = re.compile(
    r"(?:yo soy|soy|me llamo|mi nombre es|gracias[,.]?|bienvenid[oa])\s+"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)?)", re.I)
CAP_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{3,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{3,})?\b")
STOP = {"Bueno", "Gracias", "Hola", "Entonces", "Pero", "Cuando", "Porque",
        "Ahora", "Este", "Esta", "Osea", "Pues", "Como", "Para", "Aquí",
        "Muchas", "Claro", "Igual", "Nada", "Todo", "Creo", "Sinceramente"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--max-freq", type=int, default=3,
                    help="Frecuencia máxima en el corpus para considerar "
                         "raro un token capitalizado.")
    ap.add_argument("--min-len", type=int, default=4)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    conn = manifest.conectar(str(ws / "manifest.sqlite"))
    out_path = Path(args.out) if args.out else (
        ws / "reports" / f"curaduria-transcript-{time.strftime('%Y-%m-%d')}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    L = []

    # --- 1. preguntas -------------------------------------------------------
    try:
        rows = conn.execute(
            "SELECT id, clip_id, question_text, COALESCE(question_short,'') "
            "FROM question_segments ORDER BY clip_id, seg_index").fetchall()
    except sqlite3.OperationalError:
        rows = []
    sin_short = [(i, c, t) for i, c, t, s in rows if not s.strip()]
    L.append("# Curaduría de transcript — sospechosos a revisar\n")
    L.append(f"## 1. Preguntas ({len(rows)} filas)\n")
    if sin_short:
        L.append(f"**⚠ {len(sin_short)} SIN question_short (curar YA):**\n")
        for i, c, t in sin_short:
            L.append(f"- [q{i} clip {c}] {t}")
    else:
        L.append("Todas tienen question_short ✓. Re-validar las únicas:\n")
        vistos = set()
        for i, c, t, s in rows:
            if s not in vistos:
                vistos.add(s)
                lit = t.replace("[del video sync] ", "")
                flag = "  ⟵ REVISAR" if s == lit or len(s) > 80 else ""
                L.append(f"- {s}{flag}")

    # --- 2. auto-IDs / menciones de nombre ----------------------------------
    cache = ws / "transcripts"
    corpus_tokens: Counter = Counter()
    autoids = []
    ctx_of: dict[str, str] = {}
    clips = conn.execute(
        "SELECT id, filename, file_kind FROM clips WHERE index_status='ok' "
        "AND file_kind IN ('video','audio')").fetchall()
    for cid, fn, kind in clips:
        p = cache / f"{cid}.json"
        if not p.exists():
            continue
        try:
            text = json.loads(p.read_text()).get("text", "") or ""
        except Exception:
            continue
        for m in AUTOID_RE.finditer(text):
            nm = m.group(1).strip()
            if nm.split()[0] not in STOP:
                autoids.append((nm, fn))
        for m in CAP_RE.finditer(text):
            tok = m.group(0)
            if tok.split()[0] in STOP:
                continue
            corpus_tokens[tok] += 1
            if tok not in ctx_of:
                a = max(0, m.start() - 45)
                ctx_of[tok] = text[a:m.end() + 45].replace("\n", " ")
    L.append(f"\n## 2. Auto-IDs y menciones de nombre ({len(autoids)})\n")
    for nm, cnt in Counter(n for n, _ in autoids).most_common():
        L.append(f"- {nm} ({cnt}×)")

    # --- 3. tokens raros capitalizados --------------------------------------
    raros = [(tok, n) for tok, n in corpus_tokens.items()
             if n <= args.max_freq and len(tok) >= args.min_len]
    raros.sort(key=lambda x: (-x[1], x[0]))
    L.append(f"\n## 3. Tokens capitalizados raros (freq ≤ {args.max_freq}) — "
             f"candidatos a garble ({len(raros)})\n")
    L.append("Nombres propios, títulos y lugares: verificar contra contexto; "
             "corregir vía question_short / cast.json / vocabulary_hints.\n")
    for tok, n in raros:
        L.append(f"- **{tok}** ({n}×): …{ctx_of.get(tok, '')}…")

    # --- 4. recap del audit estadístico --------------------------------------
    try:
        deg = conn.execute(
            "SELECT c.filename, tq.is_hallucinated, tq.clean_until_sec "
            "FROM transcript_quality tq JOIN clips c ON c.id=tq.clip_id "
            "WHERE tq.is_hallucinated=1 OR tq.clean_until_sec IS NOT NULL "
            "ORDER BY c.filename").fetchall()
        L.append(f"\n## 4. Audit estadístico: {len(deg)} transcripts "
                 f"alucinados/degradados (ya excluidos aguas abajo)\n")
    except sqlite3.OperationalError:
        L.append("\n## 4. (sin tabla transcript_quality)\n")

    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"Reporte de curaduría: {out_path}")
    print(f"  preguntas sin curar: {len(sin_short)} | auto-IDs: "
          f"{len(set(n for n, _ in autoids))} | tokens raros: {len(raros)}")
    if sin_short:
        print("⚠ Hay preguntas sin question_short — curar antes del bake.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
