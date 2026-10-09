#!/usr/bin/env python3
"""Build labeled contact sheets of a location's clips for visual description.

Extracts one mid-point frame per clip (chronological), tiles them into labeled
grids, and writes a JSON index mapping each cell back to the manifest clip. The
model reads the sheets and produces a one-line visual description per clip.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
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


def extract_frame(src: Path, dst: Path, t_sec: float, width: int) -> bool:
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{max(t_sec, 0.0):.2f}",
        "-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2",
        str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and dst.exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--location", default="",
                    help="rel_path substring. Default '' = todo el proyecto "
                         "(el histórico 'jilo' era hardcode Jilotepec — "
                         "3er caso del mismo bug, fix FCC 2026-07-09).")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cell-w", type=int, default=420)
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--rows", type=int, default=5)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    out = Path(args.out)
    frames_dir = out / "frames"
    sheets_dir = out / "sheets"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)

    conn = manifest.conectar(str(db))
    rows = conn.execute(
        "SELECT id, path, filename, duration_sec, creation_time, camera_model "
        "FROM clips WHERE file_kind='video' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND lower(rel_path) NOT LIKE '%repetido%' "
        "ORDER BY creation_time, filename",
        (f"%{args.location.lower()}%",),
    ).fetchall()
    conn.close()
    print(f"Clips: {len(rows)}")

    cell_w = args.cell_w
    cell_h = cell_w * 9 // 16
    placeholder = Image.new("RGB", (cell_w, cell_h), (40, 0, 0))

    extracted = []
    t0 = time.time()
    for i, (cid, path, fn, dur, ct, cam) in enumerate(rows, 1):
        fp = frames_dir / f"{i:03d}_{cid}.jpg"
        if not fp.exists():
            mid = (dur or 60.0) / 2.0
            if not extract_frame(Path(path), fp, mid, cell_w):
                placeholder.save(fp, quality=85)
        extracted.append({
            "index": i, "clip_id": cid, "filename": fn,
            "frame": str(fp), "creation_time": ct, "camera_model": cam,
            "duration_sec": dur,
        })
        if i % 25 == 0:
            print(f"  frames {i}/{len(rows)}  ({(time.time() - t0) / 60:.1f}min)")
    print(f"Frames listos. {(time.time() - t0) / 60:.1f}min")

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()

    label_h = 30
    per_sheet = args.cols * args.rows
    n_sheets = (len(extracted) + per_sheet - 1) // per_sheet
    sheet_index = []
    for s in range(n_sheets):
        chunk = extracted[s * per_sheet:(s + 1) * per_sheet]
        sheet_w = cell_w * args.cols
        sheet_h = (cell_h + label_h) * args.rows
        sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        for j, c in enumerate(chunk):
            row, col = divmod(j, args.cols)
            x = col * cell_w
            y = row * (cell_h + label_h)
            try:
                img = Image.open(c["frame"]).convert("RGB")
                img.thumbnail((cell_w, cell_h))
                sheet.paste(img,
                            (x + (cell_w - img.width) // 2,
                             y + (cell_h - img.height) // 2))
            except Exception:
                pass
            ct_short = (c["creation_time"] or "")[:10]
            dur_s = f"{int(c['duration_sec'] or 0)}s"
            label = f"#{c['index']:03d}  {c['filename'][:26]}  {ct_short}  {dur_s}"
            draw.text((x + 4, y + cell_h + 4), label,
                      fill=(220, 220, 220), font=font)
        sf = sheets_dir / f"sheet_{s + 1:02d}.jpg"
        sheet.save(sf, quality=88)
        sheet_index.append({
            "sheet": s + 1, "file": str(sf),
            "clips": [
                {"index": c["index"], "clip_id": c["clip_id"],
                 "filename": c["filename"],
                 "creation_time": c["creation_time"],
                 "camera_model": c["camera_model"],
                 "duration_sec": c["duration_sec"],
                 "cell_row": divmod(j, args.cols)[0],
                 "cell_col": divmod(j, args.cols)[1]}
                for j, c in enumerate(chunk)
            ],
        })
        print(f"  sheet {s + 1}/{n_sheets}: {sf}")

    (out / "sheets_index.json").write_text(
        json.dumps(sheet_index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Listo. {len(extracted)} clips, {n_sheets} sheets en {sheets_dir}")


if __name__ == "__main__":
    main()
