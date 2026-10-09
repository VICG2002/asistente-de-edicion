#!/usr/bin/env python3
"""Index every audio file under AUDIOS/ into the manifest.

Walks the whole external-audio tree (interviews, ambiences, room tone, SFX) so
the files can be transcribed and catalogued for editing and sound design.
Reuses sync_audio.ensure_audio_indexed; never modifies source files.
"""

from __future__ import annotations

import glob
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sync_audio  # noqa: E402  — reuse ensure_audio_indexed
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
    sys.exit(f"Root not resolvable: {root_arg}")


def main():
    # Unico script del motor que no usa argparse: lee sys.argv[1] directo. Sin
    # esta guarda, `--help` se tomaba como si fuera la ruta del disco y respondia
    # "Root not resolvable: --help". Todos los demas responden a --help; que uno
    # no lo haga es justo el tropiezo que se lleva quien no escribio el motor.
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("uso: index_audios.py <root>\n"
              "  <root>  disco o carpeta del proyecto (la que contiene "
              ".cinema_assistant/)")
        sys.exit(0 if len(sys.argv) > 1 else 2)
    root = resolve_root(sys.argv[1])
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")
    conn = manifest.conectar(str(db))
    t0 = time.time()
    before = conn.execute("SELECT COUNT(*) FROM clips WHERE file_kind='audio'").fetchone()[0]
    added = sync_audio.ensure_audio_indexed(conn, root, "AUDIOS")
    total = conn.execute("SELECT COUNT(*) FROM clips WHERE file_kind='audio'").fetchone()[0]
    conn.close()
    print(f"Audios: {before} antes -> {total} ahora  (+{added} nuevos)  "
          f"{(time.time() - t0) / 60:.1f}min")


if __name__ == "__main__":
    main()
