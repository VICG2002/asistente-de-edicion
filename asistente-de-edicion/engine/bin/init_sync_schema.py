#!/usr/bin/env python3
"""Schema aditivo para sync pipeline v2 (Zezzions iter8, 2026-05-26).

Agrega:
- Tabla `sync_candidates`: candidatos no escritos a audio_sync_pairs.
  Status: 'auto' | 'needs_review' | 'rejected' | 'accepted'.
- Columnas opcionales en audio_sync_pairs (si no existen):
    identity_score, signals_json, classification
- Tabla `lavalier_pairs` (si no existe).

Es **idempotente**: solo agrega lo que falta. NO destruye datos.

Uso:
    bin/init_sync_schema.py --root <disk>
"""

from __future__ import annotations
import argparse
import glob
import sqlite3
import sys
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

SCHEMA = """
-- Candidatos de sync que NO se escribieron a audio_sync_pairs por baja confianza
CREATE TABLE IF NOT EXISTS sync_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_clip_id INTEGER NOT NULL,
    audio_clip_id INTEGER NOT NULL,
    method TEXT,
    offset_sec REAL,
    confidence REAL,
    identity_score REAL,
    classification TEXT,           -- ok | bias_refinable | bias_uncertain |
                                   -- multitake | drift_partial | drift_severe |
                                   -- no_data | minor_bias | insufficient
    status TEXT DEFAULT 'needs_review',  -- needs_review | rejected | accepted
    signals_json TEXT,             -- audit trail: { transcript: {...}, envelope: {...}, voice: {...} }
    notes TEXT,
    created_at REAL,
    reviewed_at REAL,
    reviewed_by TEXT,              -- 'user' | 'auto' | 'human-cli'
    FOREIGN KEY (video_clip_id) REFERENCES clips(id),
    FOREIGN KEY (audio_clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_synccand_video ON sync_candidates(video_clip_id);
CREATE INDEX IF NOT EXISTS idx_synccand_audio ON sync_candidates(audio_clip_id);
CREATE INDEX IF NOT EXISTS idx_synccand_status ON sync_candidates(status);

-- Lavalier pairs (hermanos Dr↔Izq del mismo Wireless PRO RX)
CREATE TABLE IF NOT EXISTS lavalier_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_a_id INTEGER NOT NULL,
    audio_b_id INTEGER NOT NULL,
    name_base TEXT,
    overlap_4gram REAL,
    delta_sec REAL,
    prominence REAL,
    voice_sim REAL,
    applicable INTEGER DEFAULT 0,
    notes TEXT,
    created_at REAL,
    FOREIGN KEY (audio_a_id) REFERENCES clips(id),
    FOREIGN KEY (audio_b_id) REFERENCES clips(id),
    UNIQUE(audio_a_id, audio_b_id)
);

-- Migración: agregar 'notes' a lavalier_pairs viejas que no la tenían
-- (idempotente; ignora error si ya existe)
CREATE INDEX IF NOT EXISTS idx_lavpair_a ON lavalier_pairs(audio_a_id);
CREATE INDEX IF NOT EXISTS idx_lavpair_b ON lavalier_pairs(audio_b_id);
"""

# Las columnas adicionales de audio_sync_pairs viven en lib/manifest.py
# (AUDIO_SYNC_PAIRS_EXTRA). Se re-exporta aquí sólo por compatibilidad con quien
# la importara; la lista buena es la de manifest.
EXTRA_COLUMNS = manifest.AUDIO_SYNC_PAIRS_EXTRA


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No existe manifest: {db}")

    conn = manifest.conectar(str(db))
    print(f"Init schema sync v2 en: {db}")

    # 1. Crear nuevas tablas (idempotente)
    conn.executescript(SCHEMA)
    print("  ✓ tablas sync_candidates + lavalier_pairs creadas/verificadas")

    # 2. audio_sync_pairs: crear si falta + migrar columnas.
    #    Delega en lib/manifest.py, que es el dueño del esquema. Antes esto
    #    hacía PRAGMA + ALTER sobre una tabla que daba por existente: en un
    #    manifest fresco donde ningún script de sync había corrido todavía,
    #    el ALTER tronaba.
    agregadas = manifest.ensure_audio_sync_pairs(conn)
    for col in agregadas:
        print(f"  ✓ audio_sync_pairs.{col} agregada")
    if not agregadas:
        print("  · audio_sync_pairs ya tenía todas sus columnas")

    # 2b. Migrar lavalier_pairs si le falta 'notes' (proyectos viejos)
    lav_cols = {row[1] for row in conn.execute("PRAGMA table_info(lavalier_pairs)")}
    if "notes" not in lav_cols:
        conn.execute("ALTER TABLE lavalier_pairs ADD COLUMN notes TEXT")
        print("  ✓ lavalier_pairs.notes TEXT agregada (migración)")

    conn.commit()

    # 3. Verificar
    print("\nEstado final:")
    for tbl in ("audio_sync_pairs", "sync_candidates", "lavalier_pairs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
        print(f"  {tbl}: {n} filas, {len(cols)} columnas")

    conn.close()
    print("✅ schema sync v2 listo")


if __name__ == "__main__":
    main()
