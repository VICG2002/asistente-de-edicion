#!/usr/bin/env python3
"""Indexa speakers (voces) de cada audio del proyecto.

Para cada audio externo:
  1. Diariza (lib.diarization_local) → segmentos por speaker.
  2. Por speaker dominante, extrae embedding GE2E representativo.
  3. Guarda en tabla `audio_speakers` (clip_id, speaker_label, start, end,
     dominant_emb BLOB, n_segments).

Output similar al de face_detections: cada speaker_label de cada audio es
un "punto" en el espacio de voz. Posteriormente `build_voice_catalog
--cluster` agrupa puntos similares cross-audio para descubrir identidades
recurrentes (la misma persona en múltiples audios).

Uso:
    bin/build_voice_catalog.py --root /Volumes/.../Zezzions VICG
    bin/build_voice_catalog.py --root ... --cluster        # paso 2: agrupar

Requiere el venv: ~/cinema-assistant/.venv/bin/python
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.diarization_local import diarize_local, LocalDiarizationResult
from lib.voice_embeddings import compute_embedding, cosine_similarity
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def ensure_schema(conn):
    # audio_speakers = una fila por (audio, speaker_label) con resumen +
    # embedding. start_sec/end_sec son del rango total (envolvente).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_speakers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            speaker_label TEXT,      -- "SPEAKER_00", etc, local al audio
            start_sec REAL,           -- min(start) de todos los segs del speaker
            end_sec REAL,             -- max(end) de todos los segs del speaker
            duration_sec REAL,
            n_segments INTEGER,
            embedding BLOB,           -- 256-D float32 normalizado
            catalog_id INTEGER,       -- vincula a voice_catalog tras clustering
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_as_clip ON audio_speakers(clip_id)")
    # audio_speaker_segments = una fila POR SEGMENTO (granular).
    # Imprescindible para identity_fusion que pregunta "¿quién habla en t=X?".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_speaker_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_speaker_id INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            FOREIGN KEY (audio_speaker_id) REFERENCES audio_speakers(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ass_speaker ON audio_speaker_segments(audio_speaker_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ass_time ON audio_speaker_segments(start_sec, end_sec)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT,     -- "ENTREVISTADO_13", "ENTREVISTADO_1", null = sin asignar
            n_members INTEGER,
            mean_embedding BLOB,
            created_at REAL
        )
    """)
    conn.commit()


def index_audios(conn, root: Path, audio_like: str, force: bool):
    rows = conn.execute(
        "SELECT id, filename, path, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' AND duration_sec >= 10 "
        "AND lower(rel_path) LIKE ?",
        (audio_like,)
    ).fetchall()
    logging.info(f"Audios a indexar: {len(rows)}")

    if not force:
        # Skip los que ya tienen speakers indexados
        indexed = set(r[0] for r in conn.execute(
            "SELECT DISTINCT clip_id FROM audio_speakers").fetchall())
        rows = [r for r in rows if r[0] not in indexed]
        if not rows:
            logging.info("Todos los audios ya están indexados (usar --force para re-indexar)")
            return

    now = time.time()
    total_speakers = 0
    for i, (aid, afn, apath, adur) in enumerate(rows, 1):
        logging.info(f"[{i}/{len(rows)}] {Path(apath).parent.name}/{afn} ({adur:.0f}s)")
        result = diarize_local(Path(apath), max_speakers=4)
        if result is None or result.n_speakers == 0:
            logging.info("  sin voz detectada")
            continue

        # Para cada speaker, extraer el chunk más largo y embebedar
        for sp in result.speakers:
            sp_segs = [s for s in result.segments if s.speaker_label == sp]
            if not sp_segs:
                continue
            sp_dur = sum(s.duration for s in sp_segs)
            # Embedding del audio completo restringido a los segmentos del speaker
            # Por simplicidad usamos `compute_embedding` sobre el archivo entero
            # con preprocess_wav VAD interno — funciona razonablemente para el
            # speaker dominante. Una mejora futura: chunk-by-chunk weighted avg.
            # Aquí usamos el primer chunk largo (>3s) como representativo.
            longest = max(sp_segs, key=lambda s: s.duration)
            # Re-embed con resemblyzer sobre solo ese tramo si dura >=3s
            emb = None
            if longest.duration >= 3.0:
                try:
                    import librosa
                    from resemblyzer import VoiceEncoder, preprocess_wav
                    wav, _ = librosa.load(str(apath), sr=16000, mono=True,
                                          offset=longest.start,
                                          duration=longest.duration)
                    wav = preprocess_wav(wav.astype(np.float32), source_sr=16000)
                    if len(wav) >= 16000 * 0.5:
                        enc = VoiceEncoder(verbose=False)
                        emb = enc.embed_utterance(wav)
                except Exception as e:
                    logging.warning(f"    embed falló para {sp}: {e}")
            emb_blob = emb.astype(np.float32).tobytes() if emb is not None else None
            # Envolvente: rango desde el primer hasta el último segmento del speaker
            envelope_start = min(s.start for s in sp_segs)
            envelope_end = max(s.end for s in sp_segs)

            cur = conn.execute(
                "INSERT INTO audio_speakers "
                "(clip_id, speaker_label, start_sec, end_sec, duration_sec, "
                "n_segments, embedding, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (aid, sp, envelope_start, envelope_end, sp_dur, len(sp_segs),
                 emb_blob, now)
            )
            speaker_id = cur.lastrowid
            # Guardar TODOS los segmentos granulares para que identity_fusion
            # pueda preguntar "¿está activo este speaker en t=X?"
            for s in sp_segs:
                conn.execute(
                    "INSERT INTO audio_speaker_segments "
                    "(audio_speaker_id, start_sec, end_sec) VALUES (?,?,?)",
                    (speaker_id, s.start, s.end)
                )
            total_speakers += 1
        conn.commit()
        logging.info(f"  {result.n_speakers} speakers (dom={result.dominant_speaker} "
                     f"{100*result.dominant_ratio:.0f}%)")
    logging.info(f"Total speakers indexados: {total_speakers}")


def cluster_catalog(conn, sim_threshold: float = 0.85):
    """Clustering aglomerativo de speakers cross-audio para construir
    voice_catalog (un cluster = una identidad recurrente).
    """
    rows = conn.execute(
        "SELECT id, clip_id, speaker_label, duration_sec, embedding FROM audio_speakers "
        "WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        logging.info("No hay embeddings — corre el indexing primero")
        return
    logging.info(f"Speakers con embedding: {len(rows)}")

    embs = np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
    # Normalizar (deberían ya estarlo, pero por si acaso)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    embs = embs / np.clip(norms, 1e-9, None)

    from sklearn.cluster import AgglomerativeClustering
    # cosine distance threshold = 1 - sim_threshold
    cl = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=1.0 - sim_threshold,
        metric="cosine",
        linkage="average",
    ).fit(embs)
    labels = cl.labels_

    # Construir voice_catalog
    conn.execute("DELETE FROM voice_catalog")  # rebuild fresh
    now = time.time()
    catalog_id_by_cluster: dict[int, int] = {}
    for cluster in sorted(set(labels)):
        idxs = [i for i, lab in enumerate(labels) if lab == cluster]
        n_members = len(idxs)
        mean_emb = embs[idxs].mean(axis=0)
        mean_emb = mean_emb / max(np.linalg.norm(mean_emb), 1e-9)
        cur = conn.execute(
            "INSERT INTO voice_catalog (canonical_name, n_members, mean_embedding, created_at) "
            "VALUES (NULL, ?, ?, ?)",
            (n_members, mean_emb.astype(np.float32).tobytes(), now)
        )
        catalog_id_by_cluster[int(cluster)] = cur.lastrowid

    # Update audio_speakers.catalog_id
    for i, (sp_id, *_rest) in enumerate(rows):
        cid = catalog_id_by_cluster[int(labels[i])]
        conn.execute("UPDATE audio_speakers SET catalog_id=? WHERE id=?", (cid, sp_id))
    conn.commit()

    # Reporte
    logging.info(f"Catalog: {len(catalog_id_by_cluster)} identidades de voz inferidas")
    for cluster, cat_id in catalog_id_by_cluster.items():
        idxs = [i for i, lab in enumerate(labels) if lab == cluster]
        sources = []
        for i in idxs:
            sp_id, clip_id, sp_label, dur, _ = rows[i]
            r = conn.execute(
                "SELECT filename, rel_path FROM clips WHERE id=?", (clip_id,)
            ).fetchone()
            sources.append(f"{Path(r[1]).parent.name}/{r[0]}:{sp_label}({dur:.0f}s)")
        logging.info(f"  catalog_id={cat_id} miembros={len(idxs)}: {', '.join(sources[:4])}"
                     + (f" +{len(sources)-4} más" if len(sources) > 4 else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%lavas%")
    ap.add_argument("--force", action="store_true",
                    help="Re-indexar todos (default: skip los ya indexados)")
    ap.add_argument("--cluster", action="store_true",
                    help="Después de indexar, hacer clustering cross-audio")
    ap.add_argument("--sim-threshold", type=float, default=0.85)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"voice_catalog_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    ensure_schema(conn)
    index_audios(conn, root, args.audio_like, args.force)
    if args.cluster:
        cluster_catalog(conn, args.sim_threshold)
    conn.close()


if __name__ == "__main__":
    main()
