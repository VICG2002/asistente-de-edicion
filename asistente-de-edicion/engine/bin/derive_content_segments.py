#!/usr/bin/env python3
"""Derive content-aware segments for spoken clips from word-level transcripts.

For clips classified as `entrevista`, `charla-equipo`, or `accion-dialogo`,
the visual `clip_segments` (motion/quality) are not the most useful marker
locations — the editor wants to jump to where someone says something.

This script reads the cached word-level transcripts (`<disk>/.cinema_assistant/
transcripts/<clip_id>.json`) and emits content segments based on:

- **Lexical density per sliding window** (60 s window, step 10 s).
- A segment starts when density crosses a threshold (≥ 8 distinct
  normalized words / minute) and ends when density falls below for ≥ 15 s.
- Adjacent segments closer than 15 s apart are merged.
- Segments shorter than 8 s are dropped.

Output: rows in a new table `content_segments` (clip_id, seg_index,
start_sec, end_sec, distinct_words, density).
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
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


WINDOW_SEC = 60.0
STEP_SEC = 10.0
MIN_DENSITY = 8.0           # distinct words per minute
GAP_TOLERANCE = 15.0        # merge segments within this gap (seconds)
MIN_SEG_LEN = 8.0           # drop segments shorter than this


def normalize_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def derive_segments(words: list[tuple[str, float]], clip_dur: float) -> list[dict]:
    """Sliding-window lexical density → segments."""
    if not words or clip_dur <= 0:
        return []

    # Bucketize words into windows
    norm_words = [(normalize_word(w), t) for w, t in words]
    norm_words = [(w, t) for w, t in norm_words if w]

    if not norm_words:
        return []

    # For each window center, compute distinct count
    starts = []
    t = 0.0
    while t < clip_dur:
        ws = t
        we = min(t + WINDOW_SEC, clip_dur)
        bucket = {w for w, wt in norm_words if ws <= wt < we}
        density = len(bucket) / ((we - ws) / 60.0) if we > ws else 0.0
        starts.append((ws, we, density, len(bucket)))
        t += STEP_SEC

    # Build raw segments where density crosses threshold
    raw = []
    in_seg = False
    seg_start = 0.0
    seg_distinct = 0
    last_above = 0.0
    for ws, we, dens, n in starts:
        if dens >= MIN_DENSITY:
            if not in_seg:
                seg_start = ws
                seg_distinct = n
                in_seg = True
            else:
                seg_distinct = max(seg_distinct, n)
            last_above = we
        elif in_seg and (ws - last_above) >= GAP_TOLERANCE:
            raw.append((seg_start, last_above, seg_distinct))
            in_seg = False
    if in_seg:
        raw.append((seg_start, last_above, seg_distinct))

    # Merge close, drop short
    merged = []
    for s, e, n in raw:
        if merged and s - merged[-1][1] <= GAP_TOLERANCE:
            ps, pe, pn = merged[-1]
            merged[-1] = (ps, e, max(pn, n))
        else:
            merged.append((s, e, n))

    out = []
    for s, e, n in merged:
        if e - s < MIN_SEG_LEN:
            continue
        density = n / ((e - s) / 60.0) if e > s else 0.0
        out.append({"start": s, "end": e, "distinct": n, "density": density})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-distinct", type=int, default=20,
                    help="Skip clips whose total distinct word count is below this.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            seg_index INTEGER,
            start_sec REAL,
            end_sec REAL,
            distinct_words INTEGER,
            density REAL,
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cseg_clip ON content_segments(clip_id)")

    # Wipe + rederive
    conn.execute("DELETE FROM content_segments")

    rows = conn.execute("""
        SELECT id, duration_sec FROM clips
        WHERE file_kind = 'video' AND index_status = 'ok'
    """).fetchall()

    n_clips_processed = 0
    n_segs = 0
    now = time.time()
    for clip_id, dur in rows:
        tr_path = tr_dir / f"{clip_id}.json"
        if not tr_path.exists():
            continue
        try:
            with tr_path.open() as f:
                tr = json.load(f)
        except Exception:
            continue
        words = tr.get("words") or []
        if len(words) < args.min_distinct:
            continue
        segs = derive_segments(words, dur or 0.0)
        if not segs:
            continue
        for i, s in enumerate(segs):
            conn.execute(
                "INSERT INTO content_segments(clip_id, seg_index, start_sec, end_sec, "
                "distinct_words, density, created_at) VALUES (?,?,?,?,?,?,?)",
                (clip_id, i, s["start"], s["end"], s["distinct"], s["density"], now)
            )
            n_segs += 1
        n_clips_processed += 1
        if args.verbose:
            print(f"  clip {clip_id}: {len(segs)} content segs")

    conn.commit()
    conn.close()
    print(f"Content segments derived: {n_clips_processed} clips, {n_segs} segs total")


if __name__ == "__main__":
    main()
