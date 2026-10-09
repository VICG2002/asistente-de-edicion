#!/usr/bin/env python3
"""Limpia clip_characters: conserva SOLO nombres que aparecen explícitamente
en el transcript del clip. Las identificaciones por face_recognition (que
podrían ser inferencias del catálogo) se descartan a menos que el transcript
del mismo clip confirme el nombre.

Razón: hasta tener mejor curaduría de identidades por una persona que
conozca el material, conservar metadata SOLO cuando hay evidencia textual
clara — entrevistas con introducción ("Me llamo ESCALADOR_A"), o pegues
donde el equipo grita el nombre del escalador ("¡Venga ESCALADORA_D!").

Los rows en `face_detections` y `face_catalog` NO se borran — quedan como
datos crudos para uso futuro. Solo se limpia `clip_characters` que es lo
que viaja a la metadata del MediaPoolItem.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cast as cast_lib  # noqa: E402
from lib import manifest  # noqa: E402


# El cast NO vive aqui. Vivia: era la whitelist de personas reales del rodaje de
# JILOTEPEC, escrita dentro del motor. Y estaba DUPLICADA en cinco archivos
# (enrich_curated_segments, derive_characters, clean_character_identity,
# describe_segments_local_llm, attribute_faces_via_transcript), que para el
# 2026-07-31 ya habian divergido: esta copia tenia 20 nombres y las otras 25, asi
# que siete personas nunca se detectaban aqui y si en el resto. Mismo patron que
# `classify_camera` escrita cinco veces.
#
# Ahora sale de `<disco>/.cinema_assistant/cast.json` via lib/cast.py. Una fuente,
# por proyecto, fuera del codigo que se publica.

# Los entrevistadores son DATOS DEL PROYECTO: <disco>/.cinema_assistant/cast.json,
# cargados con lib/cast.py en main(). Estuvieron aqui como nombres reales; el saneo
# del 2026-07-31 los volvio seudonimos y el filtro `if k not in INTERVIEWERS` dejo
# de excluir a nadie, sin avisar.


def normalize(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", w)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def transcript_mentions(words, cast) -> Counter:
    counts = Counter()
    if not words:
        return counts
    for w, _t in words:
        nw = normalize(w)
        for name, aliases in cast.items():
            if nw in aliases:
                counts[name] += 1
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--keep-interviewer-when-its-own-clip", action="store_true",
                    help="Si ESCALADORA_B es la entrevistadora pero el clip es DE ESCALADORA_B, mantenerla. Por default se filtra.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    interviewers = cast_lib.load_interviewers(root)
    if not interviewers:
        print('  aviso: sin "interviewers" en cast.json — no se excluye a nadie',
              file=sys.stderr)
    cast = cast_lib.load_cast(root)
    if not cast:
        print(cast_lib.aviso_sin_cast(root, "clean_character_identity"), file=sys.stderr)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    # Wipe clip_characters; lo re-poblamos SOLO con evidencia del transcript
    conn.execute("DELETE FROM clip_characters")

    rows = conn.execute("""
        SELECT c.id, c.file_kind, IFNULL(d.category,'') as category
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id = c.id
        WHERE c.index_status = 'ok'
    """).fetchall()

    now = time.time()
    n_with = 0
    n_without = 0
    for cid, kind, cat in rows:
        tp = tr_dir / f"{cid}.json"
        if not tp.exists():
            n_without += 1
            continue
        try:
            with tp.open() as f:
                tr = json.load(f)
        except Exception:
            n_without += 1
            continue
        words = tr.get("words", [])
        if not words:
            n_without += 1
            continue
        mentions = transcript_mentions(words, cast)
        if not mentions:
            n_without += 1
            continue
        # En entrevistas video, excluir entrevistadores del sujeto en cuadro
        if cat == "entrevista":
            mentions = Counter({k: v for k, v in mentions.items()
                                if k not in interviewers})
        if not mentions:
            n_without += 1
            continue
        chars_str = ", ".join(f"{n} ({c})" for n, c in mentions.most_common())
        conn.execute(
            "INSERT OR REPLACE INTO clip_characters (clip_id, characters, context, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (cid, chars_str, "", now)
        )
        n_with += 1

    conn.commit()
    conn.close()
    print(f"clip_characters reescrito a partir de evidencia del transcript:")
    print(f"  con personajes mencionados: {n_with}")
    print(f"  sin evidencia (vacío)     : {n_without}")
    print(f"\nLos datos de face_detections/face_catalog se conservan pero")
    print(f"NO se usan para metadata hasta que otra persona los valide.")


if __name__ == "__main__":
    main()
