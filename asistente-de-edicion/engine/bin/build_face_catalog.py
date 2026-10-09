#!/usr/bin/env python3
"""Cluster las caras detectadas y genera contact sheets por cluster para
identificación interactiva. Después permite mapear cluster → nombre canónico
y consolida el catálogo de personajes.

Pipeline:
  1. Lee TODOS los embeddings de `face_detections`.
  2. Clustering DBSCAN sobre los embeddings normalizados (eps configurable).
  3. Para cada cluster, genera mosaic PNG con los crops representativos.
  4. Si se da `--assign cluster=Nombre cluster=Otro`, persiste en `face_catalog`
     y `face_identities` los nombres canónicos y los miembros del cluster.

Pasos de uso típicos:
  python3 bin/build_face_catalog.py --root <disk>                  # genera mosaicos
  python3 bin/build_face_catalog.py --root <disk> --assign 0=ESCALADOR_A 1=ESCALADORA_B 2=ESCALADORA_D
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.guards import dep_faltante  # noqa: E402
from lib import manifest  # noqa: E402

# Import tolerante: scikit-learn es opcional. Ver detect_faces.py.
try:
    from sklearn.cluster import DBSCAN
    _DEP_FALTANTE = None
except ModuleNotFoundError as _e:
    DBSCAN = None
    _DEP_FALTANTE = _e.name


DBSCAN_EPS = 0.55          # distancia para considerar misma identidad
DBSCAN_MIN_SAMPLES = 3     # mínimo 3 detecciones para formar cluster
MOSAIC_CELL = 120          # tamaño de celda en mosaico
MOSAIC_COLS = 8


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def load_detections(conn):
    rows = conn.execute(
        "SELECT id, clip_id, embedding, crop_path, det_score, frame_t_sec "
        "FROM face_detections WHERE embedding IS NOT NULL"
    ).fetchall()
    ids, clip_ids, embeddings, paths, scores, ts = [], [], [], [], [], []
    for r in rows:
        emb = np.frombuffer(r[2], dtype=np.float32)
        if emb.size != 512:
            continue
        ids.append(r[0])
        clip_ids.append(r[1])
        embeddings.append(emb)
        paths.append(r[3])
        scores.append(r[4])
        ts.append(r[5])
    if not ids:
        return None
    return {
        "ids": np.array(ids),
        "clip_ids": np.array(clip_ids),
        "embeddings": np.vstack(embeddings),
        "paths": paths,
        "scores": np.array(scores),
        "ts": np.array(ts),
    }


def cluster_faces(embeddings: np.ndarray) -> np.ndarray:
    """Cosine distance — embeddings ya están L2-normalizados desde InsightFace."""
    # Para cosine: distance = 1 - dot. DBSCAN usa metric='cosine'.
    clust = DBSCAN(eps=DBSCAN_EPS, min_samples=DBSCAN_MIN_SAMPLES, metric="cosine")
    return clust.fit_predict(embeddings)


def render_cluster_mosaic(root: Path, paths: list, out_path: Path, label: str):
    """Compone un mosaico de los crops para identificación visual."""
    n = min(len(paths), MOSAIC_COLS * 6)  # cap 48 caras por mosaico
    if n == 0:
        return False
    rows = (n + MOSAIC_COLS - 1) // MOSAIC_COLS
    cell = MOSAIC_CELL
    header_h = 30
    img = Image.new("RGB", (cell * MOSAIC_COLS, cell * rows + header_h), (20, 20, 20))
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except Exception:
        font = ImageFont.load_default()
    draw.text((8, 6), label, fill=(255, 255, 255), font=font)
    for i, rel in enumerate(paths[:n]):
        p = root / rel
        if not p.exists():
            continue
        try:
            face = Image.open(p).convert("RGB")
        except Exception:
            continue
        face = face.resize((cell, cell), Image.LANCZOS)
        r = i // MOSAIC_COLS
        c = i % MOSAIC_COLS
        img.paste(face, (c * cell, header_h + r * cell))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=88)
    return True


def cluster_centroid(embeddings: np.ndarray) -> np.ndarray:
    c = embeddings.mean(axis=0)
    return c / np.linalg.norm(c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="/tmp/jilo_faces",
                    help="Donde escribir los mosaicos por cluster.")
    ap.add_argument("--assign", nargs="*", default=None,
                    help="Pares cluster_id=Nombre, ej --assign 0=ESCALADOR_A 1=ESCALADORA_B")
    ap.add_argument("--canonical-list", default=None,
                    help="JSON con lista de nombres + aliases para validar.")
    args = ap.parse_args()

    if _DEP_FALTANTE:
        sys.exit(dep_faltante(_DEP_FALTANTE, "El catalogo de caras (clustering)",
                              "scikit-learn"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_catalog (
            identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT UNIQUE,
            aliases TEXT,
            notes TEXT,
            n_detections INTEGER,
            n_clips INTEGER,
            centroid BLOB,
            cluster_id INTEGER,
            updated_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_identities (
            detection_id INTEGER PRIMARY KEY,
            identity_id INTEGER,
            confidence REAL,
            FOREIGN KEY (detection_id) REFERENCES face_detections(id)
        )
    """)

    data = load_detections(conn)
    if not data:
        sys.exit("No hay detecciones. Corre detect_faces.py primero.")
    print(f"Cargadas {len(data['ids'])} detecciones.")

    print("Clustering DBSCAN…")
    labels = cluster_faces(data["embeddings"])
    n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    n_noise = int((labels == -1).sum())
    print(f"Clusters: {n_clusters}  |  noise (sin cluster): {n_noise}")

    # Resumen por cluster
    summary = []
    for cid in sorted(set(labels)):
        if cid < 0:
            continue
        mask = labels == cid
        clip_ids_unique = set(data["clip_ids"][mask].tolist())
        summary.append({
            "cluster": int(cid),
            "n_detections": int(mask.sum()),
            "n_clips": len(clip_ids_unique),
            "centroid": cluster_centroid(data["embeddings"][mask]),
            "rep_paths": [data["paths"][i] for i in np.where(mask)[0][:48]],
            "rep_scores": [float(s) for s in data["scores"][mask][:48]],
        })
    summary.sort(key=lambda c: c["n_detections"], reverse=True)

    # Genera mosaicos
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nGenerando mosaicos en {out_dir}/")
    for c in summary:
        label = f"Cluster {c['cluster']}  —  {c['n_detections']} detecciones / {c['n_clips']} clips"
        out_p = out_dir / f"cluster_{c['cluster']:03d}.jpg"
        render_cluster_mosaic(root, c["rep_paths"], out_p, label)
        print(f"  → {out_p.name}  ({c['n_detections']} det, {c['n_clips']} clips)")

    # Persist cluster summary para que --assign pueda referirse a cluster_id
    summary_for_disk = [{
        "cluster": c["cluster"],
        "n_detections": c["n_detections"],
        "n_clips": c["n_clips"],
        "rep_paths": c["rep_paths"][:24],
    } for c in summary]
    (out_dir / "clusters_index.json").write_text(
        json.dumps(summary_for_disk, indent=2, ensure_ascii=False))

    # --assign: persiste el catálogo
    if args.assign:
        assigns = {}
        for a in args.assign:
            if "=" not in a:
                continue
            k, v = a.split("=", 1)
            assigns[int(k.strip())] = v.strip()
        print(f"\nAplicando asignación: {assigns}")
        now = time.time()

        # Mapa: cluster_id → centroid + miembros (detection_ids)
        cluster_data = {c["cluster"]: c for c in summary}

        # Soporte multi-cluster → mismo nombre canónico:
        # - Si el nombre ya existe, conserva identity_id (no sobrescribir filas
        #   que ya tienen face_identities apuntando a este ID).
        # - El centroid se mantiene como el del cluster con más detecciones
        #   visto hasta ahora (ya está ordenado por tamaño desc).
        for cid_target, name in assigns.items():
            if cid_target not in cluster_data:
                print(f"  ⚠ cluster {cid_target} no existe, saltado")
                continue
            c = cluster_data[cid_target]
            centroid_bytes = c["centroid"].astype(np.float32).tobytes()

            existing = conn.execute(
                "SELECT identity_id, n_detections FROM face_catalog WHERE canonical_name=?",
                (name,)).fetchone()

            if existing:
                identity_id, prev_n = existing
                # Solo actualiza el centroid si el cluster nuevo es más grande
                if c["n_detections"] > (prev_n or 0):
                    conn.execute(
                        "UPDATE face_catalog SET n_detections=?, n_clips=?, "
                        "centroid=?, cluster_id=?, updated_at=? WHERE identity_id=?",
                        (c["n_detections"], c["n_clips"], centroid_bytes,
                         cid_target, now, identity_id)
                    )
                else:
                    # Suma conteo (aunque no cambie centroid)
                    conn.execute(
                        "UPDATE face_catalog SET n_detections=?, updated_at=? "
                        "WHERE identity_id=?",
                        (prev_n + c["n_detections"], now, identity_id)
                    )
            else:
                conn.execute(
                    "INSERT INTO face_catalog (canonical_name, aliases, "
                    "n_detections, n_clips, centroid, cluster_id, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, name.lower(), c["n_detections"], c["n_clips"],
                     centroid_bytes, cid_target, now)
                )
                identity_id = conn.execute(
                    "SELECT identity_id FROM face_catalog WHERE canonical_name=?",
                    (name,)).fetchone()[0]

            # Mapeo de cada detection del cluster a la identidad
            mask = labels == cid_target
            for det_id in data["ids"][mask]:
                conn.execute(
                    "INSERT OR REPLACE INTO face_identities "
                    "(detection_id, identity_id, confidence) VALUES (?, ?, ?)",
                    (int(det_id), identity_id, 1.0)
                )
            print(f"  cluster {cid_target} → {name}  ({c['n_detections']} caras asignadas)")
        conn.commit()
        print("Catálogo persistido.")
    else:
        print("\n(Sin --assign — solo se generaron los mosaicos.)")
        print(f"\nPara identificar, revisa los mosaicos en {out_dir}/ y luego corre:")
        print(f"  python3 bin/build_face_catalog.py --root {args.root} "
              f"--assign 0=ESCALADOR_A 1=ESCALADORA_B 2=ESCALADORA_D …")

    conn.close()


if __name__ == "__main__":
    main()
