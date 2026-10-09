#!/usr/bin/env python3
"""Corre MediaPipe Pose sobre los frames de cada tramo y clasifica la
postura humana dominante en cada frame.

Pose classes inferidas a partir de landmarks (heurística):
  - vertical_climbing — cuerpo vertical, manos arriba (escalada)
  - standing          — vertical, manos en posición normal
  - sitting           — caderas dobladas, cuerpo plegado
  - lying             — cuerpo horizontal (acostado)
  - moving            — desplazamiento (poses cambiantes entre frames)
  - no_person         — sin landmarks detectados

Se guarda en `segment_poses(seg_id, frame_pct, pose_class, conf,
person_count, landmark_visibility)`.
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

warnings.filterwarnings("ignore")

try:
    import cv2  # noqa: E402
    import mediapipe as mp  # noqa: E402
except ImportError:
    cv2 = None
    mp = None


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def classify_pose(landmarks) -> tuple[str, float]:
    """Heurística sobre los 33 landmarks de MediaPipe Pose."""
    if not landmarks:
        return ("no_person", 0.0)
    LM = landmarks
    # Indices clave
    NOSE = 0
    LSH, RSH = 11, 12     # left/right shoulder
    LHIP, RHIP = 23, 24
    LWR, RWR = 15, 16     # wrists
    LANK, RANK = 27, 28   # ankles

    def y(i): return LM[i].y
    def x(i): return LM[i].x
    def v(i): return LM[i].visibility

    nose_y = y(NOSE)
    sh_y = (y(LSH) + y(RSH)) / 2
    hip_y = (y(LHIP) + y(RHIP)) / 2
    ank_y = (y(LANK) + y(RANK)) / 2
    wr_y = (y(LWR) + y(RWR)) / 2

    visib = sum(v(i) for i in [LSH, RSH, LHIP, RHIP]) / 4
    if visib < 0.4:
        return ("no_person", visib)

    # Vertical body axis: shoulder-hip-ankle aligned & ankle below shoulder
    body_height = abs(ank_y - sh_y)
    body_width = abs(x(LSH) - x(RSH))

    if body_height > 2 * body_width:
        # Cuerpo vertical
        if wr_y < sh_y - 0.05:
            return ("vertical_climbing", visib)
        return ("standing", visib)
    elif hip_y > sh_y + 0.1 and ank_y > hip_y - 0.05:
        return ("sitting", visib)
    elif abs(nose_y - hip_y) < 0.15:
        return ("lying", visib)
    return ("standing", visib)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--max-segs", type=int, default=0)
    args = ap.parse_args()

    if cv2 is None or mp is None:
        sys.exit("ERROR: instala opencv-python-headless y mediapipe primero.")

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    frames_dir = root / ".cinema_assistant" / "seg_frames"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS segment_poses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seg_id INTEGER NOT NULL,
            frame_pct INTEGER,
            pose_class TEXT,
            conf REAL,
            person_count INTEGER,
            created_at REAL,
            FOREIGN KEY (seg_id) REFERENCES clip_curated_segments(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sp_seg ON segment_poses(seg_id)")

    seg_ids = [r[0] for r in conn.execute(
        "SELECT id FROM clip_curated_segments ORDER BY id"
    )]
    if args.max_segs > 0:
        seg_ids = seg_ids[: args.max_segs]

    done_ids = set(r[0] for r in conn.execute(
        "SELECT DISTINCT seg_id FROM segment_poses"
    ))
    seg_ids = [s for s in seg_ids if s not in done_ids]
    print(f"Tramos por procesar: {len(seg_ids)}")

    pose = mp.solutions.pose.Pose(
        static_image_mode=True, min_detection_confidence=0.5
    )

    now = time.time()
    n_ok = 0
    by_class = {}
    for seg_id in seg_ids:
        seg_dir = frames_dir / str(seg_id)
        if not seg_dir.exists():
            continue
        for fp in sorted(seg_dir.glob("*.jpg")):
            try:
                pct = int(fp.stem)
            except ValueError:
                continue
            img = cv2.imread(str(fp))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            r = pose.process(rgb)
            if r.pose_landmarks:
                pclass, conf = classify_pose(r.pose_landmarks.landmark)
                pcount = 1
            else:
                pclass, conf, pcount = ("no_person", 0.0, 0)
            conn.execute(
                "INSERT INTO segment_poses (seg_id, frame_pct, pose_class, "
                "conf, person_count, created_at) VALUES (?,?,?,?,?,?)",
                (seg_id, pct, pclass, conf, pcount, now)
            )
            by_class[pclass] = by_class.get(pclass, 0) + 1
        conn.commit()
        n_ok += 1
        if n_ok % 50 == 0:
            print(f"  [{n_ok}] tramos | classes: {by_class}")

    pose.close()
    conn.close()
    print(f"\nDone. Tramos: {n_ok}")
    for k, v in sorted(by_class.items(), key=lambda x: -x[1]):
        print(f"  {k:20s}: {v}")


if __name__ == "__main__":
    main()
