#!/usr/bin/env python3
"""Detecta caras en los clips de video y guarda crops + embeddings.

Pipeline:
  1. Para cada clip de video, sample N frames con ffmpeg.
  2. Para cada frame, corre InsightFace (detection + ArcFace embedding 512-D).
  3. Filtra caras pequeñas o de baja confianza.
  4. Guarda crops JPG en `<root>/.cinema_assistant/faces/<clip_id>/<t>_<i>.jpg`.
  5. Inserta una fila por detección en `face_detections` (con embedding como BLOB).

Las caras se identifican LUEGO con `build_face_catalog.py` (clustering) +
`identify_faces.py` (asignación).
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import sqlite3
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")  # silencia notopenssl warning de urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.guards import dep_faltante  # noqa: E402
from lib import manifest  # noqa: E402

# Import tolerante: cv2 e insightface son opcionales y pesados (~2 GB con los
# modelos). Antes se importaban directo y el script reventaba con un traceback
# hasta en `--help`, sin decir que eran opcionales ni que instalar. Ahora el
# fallo se aplaza a main(), asi `--help` sigue sirviendo.
try:
    import cv2  # noqa: E402
    from insightface.app import FaceAnalysis  # noqa: E402
    _DEP_FALTANTE = None
except ModuleNotFoundError as _e:
    cv2 = None
    FaceAnalysis = None
    _DEP_FALTANTE = _e.name


MIN_DET_SCORE = 0.55
MIN_FACE_PX = 40          # bbox area side mínimo en pixels
CROP_PADDING_PCT = 0.20   # padding alrededor del bbox al guardar el crop
TARGET_FRAME_W = 960      # ancho al que se downsample antes de detectar (rápido)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def sample_times(duration: float, max_samples: int = 12) -> list[float]:
    """Devuelve hasta `max_samples` timestamps espaciados uniformemente,
    omitiendo el primer y último 5% del clip (evita fade in/out)."""
    if duration <= 1.0:
        return [duration / 2]
    if duration < 60:
        # clips cortos: 4 muestras (25%, 50%, 75%, 90%)
        return [duration * p for p in (0.25, 0.5, 0.75, 0.9)]
    if duration < 180:
        # clips medianos: ~1 frame cada 15s
        n = max(4, min(max_samples, int(duration / 15)))
    else:
        # clips largos: 1 frame cada 30s, capped
        n = max(6, min(max_samples, int(duration / 30)))
    start = duration * 0.05
    end = duration * 0.95
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


def extract_frame(video_path: str, t_sec: float) -> np.ndarray | None:
    """Devuelve un frame BGR del video en t_sec, downsampled a TARGET_FRAME_W."""
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{t_sec:.3f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={TARGET_FRAME_W}:-2",
        "-f", "image2pipe", "-vcodec", "png", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0 or not result.stdout:
            return None
        arr = np.frombuffer(result.stdout, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def save_crop(img: np.ndarray, bbox: list[int], out_path: Path):
    """Guarda crop con padding alrededor del bbox."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    pad_x = int((x2 - x1) * CROP_PADDING_PCT)
    pad_y = int((y2 - y1) * CROP_PADDING_PCT)
    x1 = max(0, x1 - pad_x); y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x); y2 = min(h, y2 + pad_y)
    if x2 <= x1 or y2 <= y1:
        return False
    crop = img[y1:y2, x1:x2]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--max-clips", type=int, default=0,
                    help="0 = todos. Limit util para debug.")
    ap.add_argument("--location-like", default=None,
                    # argparse interpola '%' en los help: hay que escaparlo como
                    # '%%' o revienta con TypeError al construir --help. Estuvo
                    # roto y no se veia porque el ModuleNotFoundError de cv2
                    # mataba el script antes de llegar aqui.
                    help="Si se pasa, filtra clips por rel_path LIKE %%X%%.")
    ap.add_argument("--ctx", type=int, default=-1,
                    help="-1 CPU, 0 MPS/GPU si está disponible")
    args = ap.parse_args()

    if _DEP_FALTANTE:
        sys.exit(dep_faltante(_DEP_FALTANTE, "La deteccion de caras",
                              "opencv-python insightface onnxruntime"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    faces_dir = root / ".cinema_assistant" / "faces"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            frame_t_sec REAL,
            face_idx INTEGER,
            bbox TEXT,
            det_score REAL,
            embedding BLOB,
            crop_path TEXT,
            created_at REAL,
            FOREIGN KEY (clip_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fd_clip ON face_detections(clip_id)")

    # ¿qué clips procesamos? saltar los que ya tienen detecciones.
    sql = """
        SELECT c.id, c.path, c.duration_sec
        FROM clips c
        LEFT JOIN face_detections fd ON fd.clip_id = c.id
        WHERE c.file_kind = 'video' AND c.index_status = 'ok'
          AND c.has_audio IS NOT NULL
          AND c.rel_path NOT LIKE '%repetido%'
          AND fd.id IS NULL
    """
    params = []
    if args.location_like:
        sql += " AND lower(c.rel_path) LIKE ?"
        params.append(f"%{args.location_like.lower()}%")
    sql += " GROUP BY c.id ORDER BY c.id"
    if args.max_clips > 0:
        sql += f" LIMIT {args.max_clips}"

    clips = conn.execute(sql, params).fetchall()
    print(f"Clips por procesar: {len(clips)}")
    if not clips:
        return

    # Carga InsightFace
    print("Cargando InsightFace…")
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=args.ctx, det_size=(640, 640))

    now = time.time()
    total_faces = 0
    for clip_idx, (cid, path, dur) in enumerate(clips, 1):
        if not path or not os.path.exists(path):
            continue
        times = sample_times(dur or 30.0)
        n_in_clip = 0
        for t in times:
            img = extract_frame(path, t)
            if img is None:
                continue
            faces = app.get(img)
            for fi, f in enumerate(faces):
                if f.det_score < MIN_DET_SCORE:
                    continue
                bbox = f.bbox.tolist()
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                if w < MIN_FACE_PX or h < MIN_FACE_PX:
                    continue
                crop_p = faces_dir / str(cid) / f"{int(t*1000):08d}_{fi}.jpg"
                if not save_crop(img, bbox, crop_p):
                    continue
                emb = f.normed_embedding.astype(np.float32).tobytes()
                conn.execute(
                    "INSERT INTO face_detections (clip_id, frame_t_sec, face_idx, "
                    "bbox, det_score, embedding, crop_path, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (cid, float(t), fi, f"{bbox[0]:.0f},{bbox[1]:.0f},{bbox[2]:.0f},{bbox[3]:.0f}",
                     float(f.det_score), emb, str(crop_p.relative_to(root)), now)
                )
                n_in_clip += 1
                total_faces += 1
        if n_in_clip > 0:
            conn.commit()
        if clip_idx % 25 == 0:
            print(f"  [{clip_idx}/{len(clips)}] caras acumuladas: {total_faces}")

    conn.commit()
    conn.close()
    print(f"\nDetecciones totales: {total_faces} en {len(clips)} clips procesados.")


if __name__ == "__main__":
    main()
