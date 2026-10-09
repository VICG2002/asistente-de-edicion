#!/usr/bin/env python3
"""For each clip with a transcript, detect characters mentioned (by name) and
optional contextual hints (sector, action). Stores into clip_characters table.

Approach:
- Known-character whitelist (project-specific cast list) checked against the
  word-level transcript (case-insensitive, normalized).
- Each appearance counted; characters with ≥ MIN_MENTIONS retained.
- Speaker-vs-mentioned distinction is NOT attempted (would need diarization).
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cast as cast_lib  # noqa: E402
from lib import manifest  # noqa: E402


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


# El cast NO vive aqui. Vivia: era la whitelist de personas reales del rodaje de
# JILOTEPEC, escrita dentro del motor. Y estaba DUPLICADA en cinco archivos
# (enrich_curated_segments, derive_characters, clean_character_identity,
# describe_segments_local_llm, attribute_faces_via_transcript), que para el
# 2026-07-31 ya habian divergido: esta copia tenia 20 nombres y las otras 25, asi
# que siete personas nunca se detectaban aqui y si en el resto. Mismo patron que
# `classify_camera` escrita cinco veces.
#
# Ahora sale de `<disco>/.cinema_assistant/cast.json` via lib/cast.py. Una fuente,
# por proyecto, fuera del codigo que se publica.

MIN_MENTIONS = 1  # threshold to consider a character "present"


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def detect_in_words(words: list[tuple[str, float]],
                    cast: dict[str, set[str]]) -> list[tuple[str, int]]:
    counts = {name: 0 for name in cast}
    for w, _t in words:
        nw = normalize(w)
        for name, aliases in cast.items():
            if nw in aliases:
                counts[name] += 1
    return [(n, c) for n, c in counts.items() if c >= MIN_MENTIONS]


def context_from(clip: dict, transcript_text: str) -> str:
    """Build a short context hint from folder name + creation_time + transcript keywords."""
    parts = []
    folder = clip.get("folder") or ""
    # Sector keywords
    sector_keywords = [
        ("lujuria", "Lujuria (14a)"),
        ("encadene", "encadene"),
        ("entrevista", "entrevista"),
        ("cueva", "cueva"),
        ("pegue", "pegue"),
        ("campamento", "campamento"),
        ("topo", "topo/lectura"),
        ("rocoso", "rocoso"),
    ]
    text_lower = (transcript_text or "").lower()
    for kw, label in sector_keywords:
        if kw in text_lower or kw in folder.lower():
            parts.append(label)
    # Time of day
    created = clip.get("created") or ""
    if len(created) >= 16:
        hour = int(created[11:13]) if created[11:13].isdigit() else -1
        if 6 <= hour < 12:
            parts.append("mañana")
        elif 12 <= hour < 17:
            parts.append("tarde")
        elif 17 <= hour < 20:
            parts.append("atardecer")
        else:
            parts.append("noche")
    seen = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return " · ".join(uniq)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    root = resolve_root(args.root)
    cast = cast_lib.load_cast(root)
    if not cast:
        print(cast_lib.aviso_sin_cast(root, "derive_characters"), file=sys.stderr)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_characters (
            clip_id INTEGER PRIMARY KEY,
            characters TEXT,   -- "ESCALADOR_A (3), ESCALADORA_B (1)"
            context TEXT,      -- "Lujuria · encadene · atardecer"
            updated_at REAL
        )
    """)
    conn.execute("DELETE FROM clip_characters")

    rows = conn.execute("""
        SELECT c.id, c.parent_folder, c.creation_time
        FROM clips c
        WHERE c.file_kind = 'video' AND c.index_status = 'ok'
    """).fetchall()

    now = time.time()
    n_with = 0
    for clip_id, folder, created in rows:
        tp = tr_dir / f"{clip_id}.json"
        words = []
        text = ""
        if tp.exists():
            try:
                with tp.open() as f:
                    tr = json.load(f)
                words = [tuple(x) for x in tr.get("words", [])]
                text = tr.get("text", "")
            except Exception:
                pass
        chars = detect_in_words(words, cast)
        char_str = ", ".join(f"{n} ({c})" for n, c in sorted(chars, key=lambda x: -x[1]))
        ctx = context_from({"folder": folder, "created": created}, text)
        if char_str or ctx:
            conn.execute(
                "INSERT INTO clip_characters(clip_id, characters, context, updated_at) "
                "VALUES (?,?,?,?)",
                (clip_id, char_str, ctx, now)
            )
            if char_str:
                n_with += 1

    conn.commit()
    conn.close()
    print(f"Personajes detectados en {n_with} clips (umbral ≥ {MIN_MENTIONS} mención).")


if __name__ == "__main__":
    main()
