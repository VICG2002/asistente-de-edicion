#!/usr/bin/env python3
"""Index a project's media into a SQLite manifest under <disk>/.cinema_assistant/.

Usage:
    python3 bin/index_project.py --root /Volumes/MI_DISCO --scope "ESCALANDO MEXICO/JILOTEPEC"

Walks the scope directory, classifies files, runs ffprobe (fallback mediainfo),
hashes partially for dedup, and writes everything to manifest.sqlite.

Never modifies source files.
"""

import argparse
import glob
import json
import logging
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(ENGINE_ROOT))

from config import defaults
from lib import cameras, classify, manifest, probe


def resolve_root(root_arg: str) -> Path:
    """Accept either an exact path or a glob (helps with hidden Unicode in volume names)."""
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    if len(matches) > 1:
        sys.exit(f"Ambiguous root '{root_arg}' — matches: {matches}")
    sys.exit(f"Root not found: {root_arg}")


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


def walk_files(scope: Path):
    """Yield Path for every non-skipped file under scope."""
    for root, dirs, files in __import__("os").walk(scope):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not classify.should_skip_dir(root_path / d)]
        for name in files:
            p = root_path / name
            if classify.should_skip(p):
                continue
            yield p


def build_record(path: Path, scope: Path, root: Path) -> dict:
    """Probe a single file and return a clip record dict."""
    rec = {c: None for c in manifest.CLIP_COLUMNS}
    try:
        st = path.stat()
    except OSError as e:
        rec.update({
            "path": unicodedata.normalize("NFC", str(path)),
            "rel_path": unicodedata.normalize("NFC", str(path.relative_to(root))),
            "parent_folder": unicodedata.normalize("NFC", _top_subfolder(path, scope)),
            "filename": unicodedata.normalize("NFC", path.name),
            "ext": path.suffix.lower(),
            "index_status": "error", "error": f"stat: {e}",
            "indexed_at": time.time(),
        })
        return rec

    kind = classify.file_kind(path)
    rec.update({
        "path": unicodedata.normalize("NFC", str(path)),
        "rel_path": unicodedata.normalize("NFC", str(path.relative_to(root))),
        "parent_folder": unicodedata.normalize("NFC", _top_subfolder(path, scope)),
        "filename": unicodedata.normalize("NFC", path.name),
        "ext": path.suffix.lower(),
        "size_bytes": st.st_size,
        "mtime": st.st_mtime,
        "file_kind": kind,
        "indexed_at": time.time(),
    })

    if kind in ("video", "audio"):
        summary, raw, source = probe.probe(path)
        rec.update(summary)
        rec["probe_source"] = source
        if raw is not None:
            rec["raw_metadata_json"] = json.dumps(raw)[:200_000]
        rec["sha256_partial"] = manifest.partial_hash(path)
        rec["index_status"] = "ok" if source != "none" else "probe_failed"

        # Segunda pasada de clasificacion, ya con la duracion medida: un `.dng`
        # suelto es una foto, no una toma CinemaDNG. Ver lib/classify.tras_probe.
        kind = classify.tras_probe(kind, rec["ext"], rec.get("duration_sec"))
        rec["file_kind"] = kind

        # v0.2.0: BRAW, R3D y ARRIRAW no los decodifica ffmpeg (no hay decoder
        # libre). Resolve SI los conforma nativo, asi que se indexan y se llevan
        # a timeline con normalidad, pero los pasos que necesitan decodificar
        # (analyze_segments, transcribe_clips) deben SALTARLOS registrando el
        # motivo. Un salto silencioso es la leccion 50 con otro disfraz.
        if kind == "video":
            perfil = cameras.classify(rec.get("filename"), rec.get("camera_model"),
                                      rec.get("camera_make"), rec.get("ext"))
            rec["decodable"] = 1 if perfil.decodable else 0
            if not perfil.decodable:
                rec["skip_reason"] = (
                    f"{perfil.label}: ffmpeg no lo decodifica. Resolve lo conforma "
                    f"nativo; se salta analisis y transcripcion.")
        else:
            rec["decodable"] = 1
    elif kind == "image":
        rec["sha256_partial"] = manifest.partial_hash(path)
        rec["index_status"] = "ok"
    else:
        rec["index_status"] = "ok"

    return rec


def seleccionar_para_indexar(conn, files: list[Path], reindex: bool = False):
    """Devuelve (por_indexar, sin_cambio, otra_grafia).

    Un archivo sin cambio (mismo tamaño y mtime) no se vuelve a indexar. Eso
    incluye el que el manifest guarda con otras mayúsculas, SIEMPRE que esa otra
    grafía ya no esté en el disco: en un volumen que distingue mayúsculas pueden
    convivir dos archivos así, y ahí sí son dos. Se conserva la grafía del
    manifest porque es la que Resolve importó; cambiarla rompería la búsqueda
    por ruta del aplicador contra los clips que ya están en el Media Pool.
    """
    if reindex:
        return list(files), 0, []
    en_disco = {unicodedata.normalize("NFC", str(p)) for p in files}
    por_indexar, sin_cambio, otra_grafia = [], 0, []
    for p in files:
        ruta = unicodedata.normalize("NFC", str(p))
        sig = manifest.existing_signature(conn, ruta)
        guardada = None
        if sig is None:
            previa = manifest.misma_ruta_otra_grafia(conn, ruta)
            if previa is not None and previa[0] not in en_disco:
                guardada, sig = previa[0], previa[1:]
        if sig is not None:
            try:
                st = p.stat()
                if sig[0] == st.st_size and abs(sig[1] - st.st_mtime) < 1:
                    sin_cambio += 1
                    if guardada:
                        otra_grafia.append((ruta, guardada))
                    continue
            except OSError:
                pass
        por_indexar.append(p)
    return por_indexar, sin_cambio, otra_grafia


def _top_subfolder(path: Path, scope: Path) -> str:
    try:
        rel = path.relative_to(scope)
        parts = rel.parts
        return parts[0] if len(parts) > 1 else ""
    except ValueError:
        return ""


def parse_args():
    p = argparse.ArgumentParser(description="Index a film project into a manifest.")
    p.add_argument("--root", required=True, help="Disk root (e.g. /Volumes/MI_DISCO)")
    p.add_argument("--scope", default="", help="Subpath under root to index (relative)")
    p.add_argument("--workers", type=int, default=defaults.DEFAULT_WORKERS)
    p.add_argument("--reindex", action="store_true", help="Reindex even if size+mtime unchanged")
    return p.parse_args()


def main():
    args = parse_args()
    root = resolve_root(args.root)
    scope = root / args.scope if args.scope else root
    if not scope.exists():
        sys.exit(f"Scope not found: {scope}")

    workspace = root / ".cinema_assistant"
    db_path = workspace / "manifest.sqlite"
    log_file = workspace / "logs" / f"indexer_{int(time.time())}.log"
    setup_logging(log_file)

    logging.info(f"Root: {root}")
    logging.info(f"Scope: {scope}")
    logging.info(f"DB: {db_path}")
    logging.info(f"Workers: {args.workers}")
    logging.info(f"Reindex: {args.reindex}")

    conn = manifest.init_db(db_path)

    logging.info("Enumerating files...")
    files = list(walk_files(scope))
    logging.info(f"Found {len(files)} files under scope.")

    files_to_index, skipped_unchanged, otra_grafia = seleccionar_para_indexar(
        conn, files, reindex=args.reindex)
    for nueva, guardada in otra_grafia:
        logging.info(f"Misma ruta con otras mayúsculas (el disco no las distingue): "
                     f"{nueva} = {guardada}. Se conserva la del manifest.")

    logging.info(f"Skipping {skipped_unchanged} unchanged. Indexing {len(files_to_index)}.")
    run_id = manifest.start_run(conn, str(scope), len(files))

    indexed = errors = 0
    total = len(files_to_index)
    t0 = time.time()

    def task(p):
        return build_record(p, scope, root)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(task, files_to_index), 1):
            try:
                manifest.upsert_clip(conn, rec)
                if rec.get("index_status") == "ok":
                    indexed += 1
                else:
                    errors += 1
                    if rec.get("error"):
                        logging.warning(f"{rec.get('rel_path')}: {rec.get('error')}")
            except Exception as e:
                errors += 1
                logging.exception(f"DB insert failed: {e}")
            if i % defaults.PROGRESS_EVERY == 0:
                conn.commit()
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                logging.info(
                    f"Progress: {i}/{total} ({100*i/total:.1f}%) "
                    f"indexed={indexed} errors={errors} "
                    f"rate={rate:.1f}/s eta={eta/60:.1f}min"
                )

    conn.commit()
    manifest.finish_run(
        conn, run_id,
        indexed=indexed, skipped=skipped_unchanged, errors=errors,
    )
    conn.close()

    elapsed = time.time() - t0
    logging.info(
        f"Done. Indexed={indexed} Skipped(unchanged)={skipped_unchanged} "
        f"Errors={errors} Elapsed={elapsed/60:.1f}min"
    )


if __name__ == "__main__":
    main()
