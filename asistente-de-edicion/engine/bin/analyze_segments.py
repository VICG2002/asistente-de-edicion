#!/usr/bin/env python3
"""Multi-signal segment analysis — distill each clip to its technically solid,
non-dead-air core. Produces tighter DURATION markers than the exposure-only pass.

Signals (all from ffmpeg signalstats, per keyframe for speed):
  - Exposure: YAVG in range, refined with blown/crushed checks.
  - Activity: YDIF (frame-to-frame luma change). Trims sustained dead-air at the
    head/tail of an otherwise-usable run; a fully-static-but-exposed shot is kept
    whole (could be an intentional establishing shot).

Usage:
    python3 bin/analyze_segments.py --root /Volumes/MI_DISCO [--workers 4]
    python3 bin/analyze_segments.py --root /Volumes/MI_DISCO --sector JILOTEPEC

Sin --sector procesa TODO el material bajo `ESCALANDO MEXICO/`. Con --sector
solo re-procesa ese sector y preserva el resto en `clip_segments`.
"""

from __future__ import annotations

import argparse
import glob
import logging
import subprocess
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(ENGINE_ROOT))

from lib import analysis
from lib.guards import assert_prefix_casa, exigir_prefix  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER,
    seg_index INTEGER,
    start_sec REAL,
    end_sec REAL,
    avg_quality REAL,
    avg_activity REAL,
    kind TEXT,
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_seg_clip ON clip_segments(clip_id);
"""

# Thresholds
EXP_MIN, EXP_MAX = 25.0, 215.0   # acceptable mean luma window
STATIC_YDIF = 2.0                 # YDIF below this = essentially frozen
MIN_SEGMENT_SEC = 3.0


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def run_ffmpeg_keyframes(path: Path, timeout: int) -> str:
    """Decode only keyframes (fast) and emit per-keyframe signalstats metadata."""
    cmd = [
        "ffmpeg", "-nostats", "-hide_banner",
        "-skip_frame", "nokey", "-i", str(path),
        # format=yuv420p: normaliza material 10-bit (FX30 Main10) a la escala
        # 8-bit que asumen los thresholds EXP_MIN/EXP_MAX (errores-comunes §32)
        "-an", "-vf", "format=yuv420p,signalstats,metadata=mode=print:file=-",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.stdout + proc.stderr


def detect_segments(frames: list[dict]) -> list[dict]:
    """Multi-signal: usable runs by exposure, dead-air trimmed at the edges."""
    samples = []
    for f in frames:
        t = f.get("pts_time")
        yavg = f.get("signalstats.YAVG")
        if t is None or yavg is None:
            continue
        ymin = f.get("signalstats.YMIN")
        ymax = f.get("signalstats.YMAX")
        ydif = f.get("signalstats.YDIF") or 0.0
        exposure_ok = (
            EXP_MIN <= yavg <= EXP_MAX
            and not (ymax is not None and ymax >= 250 and yavg > 175)   # blown
            and not (ymin is not None and ymin <= 3 and yavg < 40)      # crushed
        )
        samples.append({"t": t, "ok": exposure_ok, "ydif": ydif, "yavg": yavg})
    samples.sort(key=lambda s: s["t"])

    # group into runs of exposure-usable keyframes
    runs, cur = [], []
    for s in samples:
        if s["ok"]:
            cur.append(s)
        elif cur:
            runs.append(cur); cur = []
    if cur:
        runs.append(cur)

    segments = []
    for run in runs:
        if len(run) < 2:
            continue
        # trim sustained-static frames from head and tail
        i, j = 0, len(run) - 1
        while i < j and run[i]["ydif"] < STATIC_YDIF:
            i += 1
        while j > i and run[j]["ydif"] < STATIC_YDIF:
            j -= 1
        trimmed = run[i:j + 1]
        if len(trimmed) >= 2 and (trimmed[-1]["t"] - trimmed[0]["t"]) >= MIN_SEGMENT_SEC:
            seg, kind = trimmed, "activo"
        else:
            seg, kind = run, "estatico"   # fully-static-but-exposed: keep whole
        dur = seg[-1]["t"] - seg[0]["t"]
        if dur < MIN_SEGMENT_SEC:
            continue
        avg_y = mean(s["yavg"] for s in seg)
        avg_act = mean(s["ydif"] for s in seg)
        # quality: closeness of mean luma to a healthy mid (118)
        quality = max(0.0, 1.0 - abs(avg_y - 118.0) / 118.0)
        segments.append({
            "start": seg[0]["t"], "end": seg[-1]["t"],
            "quality": quality, "activity": avg_act, "kind": kind,
        })
    return segments


def analyze_one(clip: dict, timeout: int) -> dict:
    path = Path(clip["path"])
    if not path.exists():
        return {"clip_id": clip["id"], "segments": [], "error": "missing"}
    try:
        out = run_ffmpeg_keyframes(path, timeout)
        frames = analysis.parse_video_frames(out)
        return {"clip_id": clip["id"], "segments": detect_segments(frames), "error": None}
    except subprocess.TimeoutExpired:
        return {"clip_id": clip["id"], "segments": [], "error": "timeout"}
    except Exception as e:
        return {"clip_id": clip["id"], "segments": [], "error": str(e)[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--sector", default=None,
                    help="Restringe a un sector (sub-folder bajo --project-prefix). "
                         "Sin este flag se procesa todo el material del prefix.")
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. Prefijo de rel_path que delimita el "
                         "proyecto. Pasar cadena vacia para procesar TODOS los "
                         "videos del manifest (proyecto plano). Sin default a "
                         "proposito: uno heredado analiza cero clips y sale bien.")
    ap.add_argument("--only-missing", action="store_true",
                    help="Solo procesa clips que NO tienen aun ningun row en "
                         "clip_segments. Modo resume: no borra lo existente.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("analyze_segments"))

    root = resolve_root(args.root)
    ws = root / ".cinema_assistant"
    db_path = ws / "manifest.sqlite"
    if not db_path.exists():
        sys.exit(f"No manifest at {db_path}")

    log_file = ws / "logs" / f"segments_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db_path))
    conn.executescript(SCHEMA)
    assert_prefix_casa(conn, args.project_prefix, "analyze_segments")

    prefix = args.project_prefix or ""
    if args.sector:
        like = f"{prefix}{args.sector}/%"
        scope_label = args.sector
    elif prefix:
        like = f"{prefix}%"
        scope_label = f"all under '{prefix.rstrip('/')}'"
    else:
        like = "%"
        scope_label = "all videos in manifest (flat project)"

    if args.only_missing:
        # Modo resume: no borra nada, solo procesa los clips faltantes
        scope_label += " [resume: solo faltantes]"
        clips = [
            {"id": r[0], "path": r[1]}
            for r in conn.execute(
                "SELECT id, path FROM clips "
                "WHERE file_kind = 'video' AND index_status = 'ok' "
                "  AND rel_path LIKE ? "
                "  AND COALESCE(decodable, 1) = 1 "
                "  AND id NOT IN (SELECT DISTINCT clip_id FROM clip_segments) "
                "ORDER BY size_bytes ASC",
                (like,)
            )
        ]
    else:
        # Modo full: borra el scope y reprocesa todo
        conn.execute(
            "DELETE FROM clip_segments WHERE clip_id IN ("
            "  SELECT id FROM clips WHERE rel_path LIKE ?)",
            (like,)
        )
        conn.commit()
        clips = [
            {"id": r[0], "path": r[1]}
            for r in conn.execute(
                "SELECT id, path FROM clips "
                "WHERE file_kind = 'video' AND index_status = 'ok' "
                "  AND rel_path LIKE ? "
                "  AND COALESCE(decodable, 1) = 1 "
                "ORDER BY size_bytes ASC",
                (like,)
            )
        ]
    logging.info(f"Multi-signal segment analysis on {len(clips)} clip(s) in {scope_label}.")

    t0 = time.time()
    done = total_segs = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(lambda c: analyze_one(c, args.timeout), clips), 1):
            if rec["error"]:
                errors += 1
            for idx, seg in enumerate(rec["segments"]):
                conn.execute(
                    "INSERT INTO clip_segments (clip_id, seg_index, start_sec, end_sec, "
                    "avg_quality, avg_activity, kind, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rec["clip_id"], idx, seg["start"], seg["end"],
                     seg["quality"], seg["activity"], seg["kind"], time.time()),
                )
                total_segs += 1
            done += 1
            if i % 25 == 0:
                conn.commit()
                el = time.time() - t0
                rate = i / el if el else 0
                eta = (len(clips) - i) / rate if rate else 0
                logging.info(f"Progress {i}/{len(clips)} segs={total_segs} err={errors} eta={eta/60:.1f}min")

    conn.commit()
    conn.close()
    logging.info(f"Done. {done} clips, {total_segs} segments, {errors} errors, {(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
