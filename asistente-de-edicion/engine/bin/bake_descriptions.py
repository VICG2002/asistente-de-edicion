#!/usr/bin/env python3
"""Ingest per-clip descriptions (from desc_*.json contact-sheet review) into
the manifest as a `clip_descriptions` table.

Expected input: a directory of `desc_*.json` files, each containing a list of
dicts with at minimum `clip_id`, plus either `category`/`cat` and
`description`/`desc` fields.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--desc-dir", required=True,
                    help="Directory containing desc_*.json files.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    desc_dir = Path(args.desc_dir).expanduser()
    if not desc_dir.exists():
        sys.exit(f"No desc directory at {desc_dir}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clip_descriptions (
            clip_id INTEGER PRIMARY KEY,
            category TEXT,
            description TEXT,
            source TEXT,
            updated_at REAL
        )
    """)

    files = sorted(desc_dir.glob("desc_*.json"))
    if not files:
        sys.exit(f"No desc_*.json files in {desc_dir}")

    now = time.time()
    n_total = 0
    n_files = 0
    for fp in files:
        with fp.open() as f:
            arr = json.load(f)
        for entry in arr:
            cid = entry.get("clip_id")
            if cid is None:
                continue
            cat = entry.get("category") or entry.get("cat") or ""
            desc = entry.get("description") or entry.get("desc") or ""
            conn.execute(
                "INSERT OR REPLACE INTO clip_descriptions(clip_id, category, description, source, updated_at) "
                "VALUES (?,?,?,?,?)",
                (cid, cat, desc, fp.name, now)
            )
            n_total += 1
        n_files += 1

    conn.commit()
    conn.close()
    print(f"Ingested {n_total} descriptions from {n_files} files into clip_descriptions")


if __name__ == "__main__":
    main()
