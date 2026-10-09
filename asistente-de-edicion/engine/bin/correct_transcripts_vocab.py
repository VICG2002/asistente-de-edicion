#!/usr/bin/env python3
"""Aplica corrección de vocabulario del proyecto a transcripts existentes.

Doctrina (Zezzions iter9.4, 2026-05-27): después de transcribir con
Whisper (con o sin prompt contextual), aplicar corrección fuzzy contra
el vocabulario del proyecto (cast + términos del dominio) corrige errores
sutiles que Whisper no captura aun con prompt.

Ejemplos típicos:
  - "Vici" → "Avicii"
  - "Mariana Ruis" → "Mariana Ruiz" (nombre inventado: un apellido del cast)
  - "Vías de Leon" → "Vías de León"

Strategy:
  1. Construir vocab del proyecto desde face_catalog + voice_catalog + cast.json
  2. Para cada palabra del transcript, si NO es palabra española común Y
     match fuzzy (Levenshtein ≤ 2 ó ratio ≥ 0.85) con vocab, sustituir.
  3. Crear archivo de respaldo `legacy_vocab_{cid}.json`.
  4. Guardar transcript corregido en su lugar.

Uso:
    bin/correct_transcripts_vocab.py --root <disk>
    bin/correct_transcripts_vocab.py --root <disk> --clip-ids 151,158
    bin/correct_transcripts_vocab.py --root <disk> --dry-run
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.project_vocabulary import (
    build_project_vocabulary, correct_transcript_words, correct_text,
    load_explicit_corrections
)
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists(): return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1: return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def correct_transcript_file(path: Path, vocab: set, cutoff: float = 0.85,
                            explicit: dict | None = None
                              ) -> tuple[int, dict[str, str]]:
    """Carga, corrige, guarda. Returns (n_corrections, dict)."""
    if not path.exists():
        return 0, {}
    try:
        d = json.load(path.open())
    except Exception as e:
        logging.warning(f"  parse error: {e}")
        return 0, {}
    words = []
    for w in d.get("words", []) or []:
        if isinstance(w, list) and len(w) >= 2:
            words.append((w[0], w[1]))
    if not words:
        return 0, {}
    corrected_words, corrections = correct_transcript_words(
        words, vocab, cutoff, explicit=explicit)
    # El texto se corrige SIEMPRE, no solo cuando cambiaron las palabras.
    # Antes se salia aqui con `if not corrections: return`, asi que un
    # transcript con `words` ya corregido en una pasada previa se quedaba con el
    # campo `text` sin tocar — y acababa contradiciendose consigo mismo: las
    # palabras decian Ximena y el texto seguia diciendo Jimena. Todo lo que lee
    # `text` (agrupador de tomas, informes, busquedas) veia la version vieja.
    viejo_texto = d.get("text", "")
    new_text = correct_text(viejo_texto, vocab, cutoff, explicit=explicit)
    if not corrections and new_text == viejo_texto:
        return 0, {}
    d["text"] = new_text
    d["words"] = corrected_words
    with path.open("w") as f:
        json.dump(d, f, ensure_ascii=False)
    # Se devuelve al menos 1 si el archivo se escribio: si no, una corrida que
    # solo arregla el campo `text` reporta "0 files cambiados" habiendo
    # reescrito quince. Un contador que miente sobre el trabajo hecho es la
    # misma clase de fallo silencioso que el resto de esta auditoria.
    return max(len(corrections), 1), corrections


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--clip-ids", help="CSV de clip_ids a procesar (default: todos)")
    ap.add_argument("--cutoff", type=float, default=0.85,
                    help="Similaridad mínima (0-1) para corregir")
    ap.add_argument("--include-masters", action="store_true",
                    help="También aplicar a master_*.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"correct_vocab_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    vocab = build_project_vocabulary(conn, root)
    explicit = load_explicit_corrections(root)
    logging.info(f"Vocabulario del proyecto: {len(vocab)} términos")
    if explicit:
        logging.info(f"  correcciones exactas del proyecto: {len(explicit)} "
                     f"({', '.join(list(explicit)[:6])})")
    # Mostrar muestra
    sample = sorted(vocab)[:20]
    logging.info(f"  muestra: {sample}")

    # Determinar transcripts a procesar
    if args.clip_ids:
        ids = [int(x.strip()) for x in args.clip_ids.split(",") if x.strip()]
        paths = [tr_dir / f"{cid}.json" for cid in ids]
    else:
        # Todos los transcripts (skip legacy_pre_context_* y master_*)
        paths = []
        for p in tr_dir.glob("*.json"):
            name = p.stem
            if name.startswith("legacy_") or name.startswith("master_"):
                continue
            if p.name.startswith("._"):  # AppleDouble de macOS en discos externos
                continue
            paths.append(p)
        if args.include_masters:
            paths.extend(tr_dir.glob("master_*.json"))

    logging.info(f"Transcripts a procesar: {len(paths)}")

    total_corrections = 0
    files_changed = 0
    all_correction_pairs: dict[str, dict[str, int]] = {}
    skipped_bad = 0
    for p in paths:
        if not p.exists():
            continue
        cid = p.stem.replace("master_", "")
        if args.dry_run:
            # Solo contar sin modificar
            try:
                d = json.load(p.open())
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logging.warning(f"  SKIP {p.name}: no decodificable ({e.__class__.__name__})")
                skipped_bad += 1
                continue
            words = [(w[0], w[1]) for w in d.get("words", []) or []
                     if isinstance(w, list) and len(w) >= 2]
            from lib.project_vocabulary import correct_transcript_words
            _, corrs = correct_transcript_words(
                words, vocab, args.cutoff, explicit=explicit)
            if corrs:
                total_corrections += len(corrs)
                files_changed += 1
                logging.info(f"  [DRY] {p.name}: {len(corrs)} correcciones")
                for k, v in list(corrs.items())[:5]:
                    logging.info(f"    '{k}' → '{v}'")
        else:
            # Crear backup si no existe
            backup = p.parent / f"legacy_vocab_{cid}.json"
            if not backup.exists():
                shutil.copy2(p, backup)
            try:
                n_corr, corrs = correct_transcript_file(
                    p, vocab, args.cutoff, explicit=explicit)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logging.warning(f"  SKIP {p.name}: no decodificable ({e.__class__.__name__})")
                skipped_bad += 1
                continue
            if n_corr:
                total_corrections += n_corr
                files_changed += 1
                logging.info(f"  ✓ {p.name}: {n_corr} correcciones")
                for k, v in list(corrs.items())[:5]:
                    logging.info(f"    '{k}' → '{v}'")
                # Agregar al historial global
                for k, v in corrs.items():
                    pair_key = f"{k.lower()}→{v}"
                    all_correction_pairs.setdefault(pair_key, {"k": k, "v": v, "n": 0})
                    all_correction_pairs[pair_key]["n"] += 1

    # Reporte global de correcciones más comunes
    if all_correction_pairs:
        logging.info(f"\n=== Top 20 correcciones más frecuentes ===")
        sorted_pairs = sorted(all_correction_pairs.values(), key=lambda x: -x["n"])
        for p in sorted_pairs[:20]:
            logging.info(f"  '{p['k']}' → '{p['v']}': {p['n']} veces")

    if skipped_bad:
        logging.warning(f"{skipped_bad} archivos saltados por encoding/JSON corrupto")
    logging.info(f"\n✅ {files_changed} files cambiados, {total_corrections} correcciones totales")
    conn.close()


if __name__ == "__main__":
    main()
