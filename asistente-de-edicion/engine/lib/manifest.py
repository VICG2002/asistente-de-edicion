"""SQLite manifest of indexed media. Lives at <disk>/.cinema_assistant/manifest.sqlite."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from config import defaults


SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    rel_path TEXT NOT NULL,
    parent_folder TEXT,
    filename TEXT NOT NULL,
    ext TEXT,
    size_bytes INTEGER,
    mtime REAL,
    sha256_partial TEXT,
    file_kind TEXT,
    codec_name TEXT,
    width INTEGER,
    height INTEGER,
    fps REAL,
    duration_sec REAL,
    bit_rate INTEGER,
    color_space TEXT,
    pixel_format TEXT,
    has_audio INTEGER,
    audio_codec TEXT,
    audio_channels INTEGER,
    audio_sample_rate INTEGER,
    timecode TEXT,
    creation_time TEXT,
    camera_make TEXT,
    camera_model TEXT,
    probe_source TEXT,
    raw_metadata_json TEXT,
    indexed_at REAL,
    index_status TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_parent_folder ON clips(parent_folder);
CREATE INDEX IF NOT EXISTS idx_file_kind ON clips(file_kind);
CREATE INDEX IF NOT EXISTS idx_sha256_partial ON clips(sha256_partial);
CREATE INDEX IF NOT EXISTS idx_ext ON clips(ext);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    finished_at REAL,
    scope_path TEXT,
    files_found INTEGER,
    files_indexed INTEGER,
    files_skipped INTEGER,
    errors INTEGER,
    notes TEXT
);
"""

CLIP_COLUMNS = [
    "path", "rel_path", "parent_folder", "filename", "ext",
    "size_bytes", "mtime", "sha256_partial", "file_kind",
    "codec_name", "width", "height", "fps", "duration_sec", "bit_rate",
    "color_space", "pixel_format",
    "has_audio", "audio_codec", "audio_channels", "audio_sample_rate",
    "timecode", "creation_time", "camera_make", "camera_model",
    "probe_source", "raw_metadata_json",
    "indexed_at", "index_status", "error",
    "decodable", "skip_reason",
]


# Columnas agregadas despues de que hubiera manifests en produccion. Se aplican
# con ALTER TABLE al abrir: los discos de proyectos viejos siguen funcionando
# sin re-indexar.
MIGRACIONES = [
    # v0.2.0 — ffmpeg NO decodifica BRAW ni R3D. Los pasos que necesitan
    # decodificar tienen que SALTARLOS registrando el motivo, no reventar ni
    # fingir que los procesaron. Sale del registro de camaras (lib/cameras.py).
    ("decodable", "INTEGER DEFAULT 1"),
    ("skip_reason", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> list:
    """Agrega las columnas que falten. Idempotente. Devuelve las agregadas."""
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(clips)")}
    agregadas = []
    for col, tipo in MIGRACIONES:
        if col not in existentes:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {tipo}")
            agregadas.append(col)
    if agregadas:
        conn.commit()
    return agregadas


# --- audio_sync_pairs: UNA definicion, no cinco -------------------------------
#
# Hasta 2026-08-28 cinco scripts creaban esta tabla por su cuenta con
# `CREATE TABLE IF NOT EXISTS` y una lista de columnas propia, mas corta que la
# de bin/init_sync_schema.py. Ninguno declaraba `identity_score`, que
# bin/sync_siblings_rule.py lee por SELECT explicito.
#
# El `IF NOT EXISTS` garantiza justamente lo peor: la primera version que corra
# gana y las demas callan. En un proyecto fresco, si cualquiera de los cinco
# corria antes que init_sync_schema.py, la tabla nacia incompleta y la regla de
# hermanos tronaba. Lo destapo la bifurcacion de Adrian (The Shelter,
# 2026-08-02); ver la leccion "identity_score falta en cinco CREATE TABLE".
#
# Ahora hay una sola definicion y una sola migracion aditiva.

AUDIO_SYNC_PAIRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_sync_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_clip_id INTEGER,
    audio_clip_id INTEGER,
    method TEXT,
    offset_sec REAL,
    confidence REAL,
    video_start_iso TEXT,
    audio_start_iso TEXT,
    overlap_sec REAL,
    notes TEXT,
    created_at REAL,
    identity_score REAL,
    signals_json TEXT,
    classification TEXT,
    verifier_passed INTEGER,
    FOREIGN KEY (video_clip_id) REFERENCES clips(id),
    FOREIGN KEY (audio_clip_id) REFERENCES clips(id)
);
"""

AUDIO_SYNC_PAIRS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_sync_video ON audio_sync_pairs(video_clip_id);
CREATE INDEX IF NOT EXISTS idx_sync_audio ON audio_sync_pairs(audio_clip_id);
"""

# Columnas que las bases creadas antes de 2026-08-28 no tienen. Se agregan por
# ALTER TABLE; la lista TIENE que coincidir con las declaradas arriba.
AUDIO_SYNC_PAIRS_EXTRA = [
    ("identity_score", "REAL"),
    ("signals_json", "TEXT"),
    ("classification", "TEXT"),
    ("verifier_passed", "INTEGER"),
]


def ensure_audio_sync_pairs(conn: sqlite3.Connection) -> list:
    """Crea audio_sync_pairs si falta y migra las columnas que no esten.

    Idempotente. Devuelve las columnas agregadas. Todo script que escriba o lea
    audio_sync_pairs llama a esto en vez de traer su propio CREATE TABLE.
    """
    # Orden: tabla -> columnas que falten -> indices. Los indices van AL FINAL
    # porque nombran columnas: sobre una tabla vieja a la que aun no se le han
    # agregado, `CREATE INDEX` truena con "no such column".
    conn.executescript(AUDIO_SYNC_PAIRS_SCHEMA)
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(audio_sync_pairs)")}
    agregadas = []
    for col, tipo in AUDIO_SYNC_PAIRS_EXTRA:
        if col not in existentes:
            conn.execute(f"ALTER TABLE audio_sync_pairs ADD COLUMN {col} {tipo}")
            agregadas.append(col)
    conn.executescript(AUDIO_SYNC_PAIRS_INDICES)
    conn.commit()
    return agregadas


# Cuanto espera una conexion a que otra suelte el lock antes de dar
# "database is locked". 30 s es mucho para una consulta y poco para una
# transaccion de indexado sobre un disco externo lento.
BUSY_TIMEOUT_MS = 30_000


def conectar(db_path: Path, *, timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Punto unico de apertura del manifest, con la disciplina puesta.

    WAL permite UN escritor y N lectores a la vez; sin el, cualquier lectura
    bloquea a un escritor y dos pasos concurrentes se pisan. `busy_timeout`
    absorbe los solapes en vez de reventar.

    OJO CON LA ASIMETRIA: `journal_mode=WAL` es una propiedad DEL ARCHIVO y
    persiste — se activa una vez y todas las conexiones futuras lo heredan,
    incluso las que no lo piden. `busy_timeout`, en cambio, es POR CONEXION.

    Y UNA PRECISION QUE COSTO UN INTENTO FALLIDO (2026-08-05): el parametro
    `timeout` de `sqlite3.connect` ES el busy_timeout, y su default en Python
    son **5000 ms, no cero**. O sea que ningun script del motor estuvo nunca
    desprotegido: los 45 que pasaban `timeout=60.0` tenian 60 s y el resto
    tenia 5. Lo que aporta este punto unico no es "arreglar" una ausencia, es
    uniformar el valor (30 s), tener un solo sitio donde cambiar un PRAGMA, y
    dejar de repetir `PRAGMA journal_mode=WAL` suelto en seis scripts.
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout_ms / 1000.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    except sqlite3.Error:
        pass        # un manifest de solo lectura sigue siendo utilizable
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = conectar(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def partial_hash(path: Path) -> str | None:
    """Hash first N + last N bytes + size. Fast dedup signal for large files."""
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(defaults.HASH_HEAD_BYTES))
            if size > defaults.HASH_HEAD_BYTES + defaults.HASH_TAIL_BYTES:
                f.seek(max(0, size - defaults.HASH_TAIL_BYTES))
                h.update(f.read(defaults.HASH_TAIL_BYTES))
        return h.hexdigest()
    except OSError:
        return None


def existing_signature(conn: sqlite3.Connection, path: str) -> tuple | None:
    row = conn.execute(
        "SELECT size_bytes, mtime FROM clips WHERE path = ?", (path,)
    ).fetchone()
    return row


def misma_ruta_otra_grafia(conn: sqlite3.Connection, path: str) -> tuple | None:
    """(path_guardado, size_bytes, mtime) si el manifest ya tiene esta ruta escrita
    con otras mayúsculas.

    Los discos de la Mac (exFAT, APFS por defecto) no distinguen mayúsculas:
    `Video 02/x.MP4` y `VIDEO 02/x.MP4` son el MISMO archivo. Como texto no lo
    son, y el indexador los tomaba por nuevos (Asistente, 2026-09-29: renombrar
    `Video 02` a `VIDEO 02` duplicó 30 clips, y el Whisper y el sync se iban a
    repetir sobre filas sin nada de lo curado).
    """
    for guardada, size, mtime in conn.execute(
            "SELECT path, size_bytes, mtime FROM clips WHERE path = ? COLLATE NOCASE "
            "AND path != ?", (path, path)):
        if guardada.casefold() == path.casefold():
            return guardada, size, mtime
    return None


def upsert_clip(conn: sqlite3.Connection, record: dict) -> None:
    placeholders = ",".join(["?"] * len(CLIP_COLUMNS))
    updates = ",".join(f"{c}=excluded.{c}" for c in CLIP_COLUMNS if c != "path")
    sql = (
        f"INSERT INTO clips ({','.join(CLIP_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [record.get(c) for c in CLIP_COLUMNS])


def start_run(conn: sqlite3.Connection, scope_path: str, files_found: int) -> int:
    cur = conn.execute(
        "INSERT INTO scan_runs (started_at, scope_path, files_found) VALUES (?, ?, ?)",
        (time.time(), scope_path, files_found),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, indexed: int, skipped: int, errors: int, notes: str = "") -> None:
    conn.execute(
        "UPDATE scan_runs SET finished_at=?, files_indexed=?, files_skipped=?, errors=?, notes=? WHERE id=?",
        (time.time(), indexed, skipped, errors, notes, run_id),
    )
    conn.commit()
