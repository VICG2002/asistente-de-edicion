#!/usr/bin/env python3
"""Run technical analysis on indexed video clips and store results.

Usage:
    python3 bin/analyze_clips.py --root /Volumes/MI_DISCO \
        [--include "Jilotepec 2,jilo 4"] [--exclude "Edición,LUJURIA MASTER"] \
        [--limit 50] [--workers 2]

Reads clip list from manifest.sqlite, runs ffmpeg-based analysis on each video clip,
writes to clip_analysis table. Never touches source files.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(ENGINE_ROOT))

from lib import analysis
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


ANALYSIS_COLUMNS = [
    "clip_id", "analyzed_at", "analysis_status", "analysis_notes",
    "exposure_y_avg_mean", "exposure_y_avg_median",
    "exposure_y_max_mean", "exposure_y_min_mean",
    "exposure_y_high_mean", "exposure_y_low_mean",
    "exposure_over_pct", "exposure_under_pct",
    "highlights_clipped_pct", "shadows_crushed_pct",
    "motion_score_mean", "motion_score_max",
    "saturation_mean",
    "blur_mean",
    "audio_peak_db", "audio_rms_db", "audio_clip_likely", "audio_silent_likely",
    "frame_samples", "error",
]

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_analysis (
    clip_id INTEGER PRIMARY KEY,
    analyzed_at REAL,
    analysis_status TEXT,
    analysis_notes TEXT,
    exposure_y_avg_mean REAL,
    exposure_y_avg_median REAL,
    exposure_y_max_mean REAL,
    exposure_y_min_mean REAL,
    exposure_y_high_mean REAL,
    exposure_y_low_mean REAL,
    exposure_over_pct REAL,
    exposure_under_pct REAL,
    highlights_clipped_pct REAL,
    shadows_crushed_pct REAL,
    motion_score_mean REAL,
    motion_score_max REAL,
    saturation_mean REAL,
    blur_mean REAL,
    audio_peak_db REAL,
    audio_rms_db REAL,
    audio_clip_likely INTEGER,
    audio_silent_likely INTEGER,
    frame_samples INTEGER,
    error TEXT,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_analysis_status ON clip_analysis(analysis_status);
"""


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )


def pick_clips(conn, includes: list[str], excludes: list[str], limit, reanalyze: bool) -> list[dict]:
    sql = (
        "SELECT c.id, c.path, c.rel_path, c.parent_folder, c.fps, c.has_audio, c.duration_sec, c.size_bytes "
        "FROM clips c "
        "LEFT JOIN clip_analysis a ON a.clip_id = c.id "
        "WHERE c.file_kind = 'video' AND c.index_status = 'ok' "
    )
    params: list = []
    if includes:
        placeholders = ",".join(["?"] * len(includes))
        sql += f" AND c.parent_folder IN ({placeholders}) "
        params += includes
    if excludes:
        placeholders = ",".join(["?"] * len(excludes))
        sql += f" AND c.parent_folder NOT IN ({placeholders}) "
        params += excludes
    if not reanalyze:
        sql += " AND a.clip_id IS NULL "
    sql += " ORDER BY c.size_bytes ASC "
    if limit:
        sql += " LIMIT ? "
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r[0], "path": r[1], "rel_path": r[2], "parent_folder": r[3],
         "fps": r[4], "has_audio": bool(r[5]), "duration_sec": r[6], "size_bytes": r[7]}
        for r in rows
    ]


def upsert_analysis(conn, record: dict) -> None:
    cols = ANALYSIS_COLUMNS
    placeholders = ",".join(["?"] * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "clip_id")
    sql = (
        f"INSERT INTO clip_analysis ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(clip_id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [record.get(c) for c in cols])


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--include", default=None, help="Comma-separated parent_folder values to include")
    p.add_argument("--exclude", default=None, help="Comma-separated parent_folder values to exclude")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--reanalyze", action="store_true")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--reset-schema", action="store_true", help="Drop and recreate clip_analysis table")
    return p.parse_args()


def main():
    args = parse_args()
    root = resolve_root(args.root)
    workspace = root / ".cinema_assistant"
    db_path = workspace / "manifest.sqlite"
    if not db_path.exists():
        sys.exit(f"No manifest found at {db_path}. Run index_project.py first.")

    log_file = workspace / "logs" / f"analyzer_{int(time.time())}.log"
    setup_logging(log_file)

    conn = manifest.conectar(str(db_path))
    if args.reset_schema:
        conn.execute("DROP TABLE IF EXISTS clip_analysis")
        logging.info("Dropped existing clip_analysis table.")
    conn.executescript(ANALYSIS_SCHEMA)

    includes = [s.strip() for s in args.include.split(",")] if args.include else []
    excludes = [s.strip() for s in args.exclude.split(",")] if args.exclude else []
    clips = pick_clips(conn, includes, excludes, args.limit, args.reanalyze)
    logging.info(f"DB: {db_path}")
    logging.info(f"Include: {includes or '(all)'} | Exclude: {excludes or 'none'}")
    logging.info(f"Workers: {args.workers}, Timeout: {args.timeout}s")
    logging.info(f"Selected {len(clips)} clip(s) for analysis.")

    if not clips:
        logging.info("Nothing to do.")
        return

    t0 = time.time()
    done = errors = 0

    def task(c):
        path = Path(c["path"])
        if not path.exists():
            return {"clip_id": c["id"], "analyzed_at": time.time(),
                    "analysis_status": "missing", "error": "file not found"}
        try:
            metrics = analysis.analyze(
                path,
                fps=c.get("fps") or 24.0,
                has_audio=c.get("has_audio") or False,
                timeout=args.timeout,
            )
            metrics["clip_id"] = c["id"]
            metrics["analyzed_at"] = time.time()
            return metrics
        except Exception as e:
            return {"clip_id": c["id"], "analyzed_at": time.time(),
                    "analysis_status": "error", "error": str(e)[:500]}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(task, clips), 1):
            try:
                upsert_analysis(conn, rec)
                if rec.get("analysis_status") in ("keep", "review", "cull"):
                    done += 1
                else:
                    errors += 1
                    if rec.get("error"):
                        logging.warning(f"{rec.get('clip_id')}: {rec.get('error')}")
            except Exception as e:
                errors += 1
                logging.exception(f"DB insert failed: {e}")
            if i % 10 == 0:
                conn.commit()
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(clips) - i) / rate if rate > 0 else 0
                logging.info(
                    f"Progress {i}/{len(clips)} ok={done} err={errors} "
                    f"rate={rate:.2f}/s eta={eta/60:.1f}min"
                )

    conn.commit()
    elapsed = time.time() - t0
    logging.info(f"Done. ok={done} errors={errors} elapsed={elapsed/60:.1f}min")
    conn.close()


if __name__ == "__main__":
    main()
