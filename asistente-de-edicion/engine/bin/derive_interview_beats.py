#!/usr/bin/env python3
"""Deriva los beats de entrevista: PREGUNTA y RESPUESTA separadas, mas pausas,
palabras clave y candidatos a momento emocional.

Que cambia respecto a lo de antes
---------------------------------
`question_segments` emite un tramo por pregunta que va de una pregunta a la
siguiente: un solo marker morado cubre pregunta Y respuesta, con el texto de la
respuesta comprimido en la nota. El editor no puede saltar a donde empieza a
responder, que es donde va a cortar.

Aqui se parte en dos beats y se agregan las senales que ya teniamos pero no
estaban llegando a Resolve:

  pregunta   morado   — de donde arranca la pregunta a donde arranca la respuesta
  respuesta  azul     — el tramo util; lleva el resumen en la nota
  pausa      arena    — silencio real DENTRO de una respuesta (punto de corte)
  keyword    menta    — primera aparicion de un termino del proyecto
  emocion    (marca)  — candidato: risa, aplauso, llanto o pausa larga

NO inventa nada: `question_segments` no se toca, y sin transcript verificable
(`transcript_quality.is_hallucinated = 1`) el clip no produce beats.

Requiere antes:  derive_question_segments.py  y  detect_pauses.py

Uso:
    python3 bin/derive_interview_beats.py --root <disco>
    python3 bin/derive_interview_beats.py --root <disco> --clip-ids 2573
    python3 bin/derive_interview_beats.py --root <disco> --sin-keywords
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import interview_beats as ib                    # noqa: E402
from lib.guards import assert_selected, report_done      # noqa: E402
from lib import manifest  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS interview_beats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    beat_index INTEGER,
    kind TEXT,               -- pregunta | respuesta | pausa | keyword | emocion
    start_sec REAL,
    end_sec REAL,
    speaker TEXT,            -- fuente del master (video_a1, lavalier_146, ...)
    text TEXT,
    confidence REAL,
    source TEXT,             -- master_src | silencio | estimado | panns | vocabulario
    q_index INTEGER,         -- a que pregunta pertenece (NULL si suelto)
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_beats_clip ON interview_beats(clip_id);
CREATE INDEX IF NOT EXISTS idx_beats_kind ON interview_beats(kind);
"""


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def cargar_alucinados(conn) -> set:
    """Clips cuyo transcript es basura: no producen beats.

    Doctrina `metodologia/marcadores.md`: Whisper sobre musica ambient produce
    texto que parece denso ("Suscribete al canal" x100). Un marker sobre eso
    engana al editor.
    """
    try:
        return {r[0] for r in conn.execute(
            "SELECT clip_id FROM transcript_quality WHERE is_hallucinated=1")}
    except sqlite3.OperationalError:
        return set()


def cargar_vocabulario(conn, root: Path) -> set:
    try:
        from lib import project_vocabulary as pv
    except ImportError:
        return set()
    vocab = set()
    for fn in (lambda: pv.load_vocab_hints_from_config(root),
               lambda: pv.load_cast_json(root),
               lambda: pv.load_cast_from_manifest(conn)):
        try:
            vocab |= {t for t in (fn() or set()) if isinstance(t, str) and len(t) >= 3}
        except Exception:
            continue
    # Los mexicanismos son ruido como marker: aparecen en cada frase.
    return vocab - set(getattr(pv, "MEXICANISMOS", set()))


def guardar_beats(conn, cid: int, beats: list) -> None:
    """Reemplaza los beats del clip, AUNQUE los nuevos sean cero.

    Antes se saltaba el borrado cuando el clip ya no daba beats: un clip cuyas
    preguntas descarto la curaduria conservaba su pregunta y su respuesta
    viejas, y salian en los markers (Asistente, 2026-10-01: 9463, 9467, 9773 y
    ASA_9764, descartadas el 28-sep).
    """
    conn.execute("DELETE FROM interview_beats WHERE clip_id=?", (cid,))
    now = time.time()
    for b in beats:
        conn.execute(
            "INSERT INTO interview_beats (clip_id, beat_index, kind,"
            " start_sec, end_sec, speaker, text, confidence, source,"
            " q_index, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, b["beat_index"], b["kind"], b["start_sec"], b["end_sec"],
             b["speaker"], b["text"], b["confidence"], b["source"],
             b["q_index"], now))


def quitar_beats_sin_origen(conn, cids) -> int:
    """Beats de clips que ya no tienen nada de que derivarlos: fuera."""
    vigentes = set(cids)
    viejos = [c for (c,) in conn.execute("SELECT DISTINCT clip_id FROM interview_beats")
              if c not in vigentes]
    n = 0
    for c in viejos:
        n += conn.execute("DELETE FROM interview_beats WHERE clip_id=?", (c,)).rowcount
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--clip-ids", default="",
                    help="Lista de clip_id separados por coma. Vacio = todos.")
    ap.add_argument("--sin-keywords", action="store_true",
                    help="No emitir beats de palabra clave.")
    ap.add_argument("--sin-emocion", action="store_true",
                    help="No emitir candidatos a momento emocional.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row

    solo = set()
    if args.clip_ids.strip():
        solo = {int(x) for x in args.clip_ids.split(",") if x.strip().isdigit()}

    # --- preguntas por clip ----------------------------------------------
    cols_q = {r[1] for r in conn.execute("PRAGMA table_info(question_segments)")}
    answer = "answer_sec" if "answer_sec" in cols_q else "NULL AS answer_sec"
    try:
        qrows = conn.execute(f"""
            SELECT clip_id, seg_index, start_sec, end_sec, question_text,
                   COALESCE(question_short,'') AS question_short,
                   COALESCE(response_summary,'') AS response_summary,
                   {answer}
            FROM question_segments ORDER BY clip_id, seg_index
        """).fetchall()
    except sqlite3.OperationalError:
        # manifests viejos sin question_short
        qrows = conn.execute("""
            SELECT clip_id, seg_index, start_sec, end_sec, question_text,
                   '' AS question_short,
                   COALESCE(response_summary,'') AS response_summary
            FROM question_segments ORDER BY clip_id, seg_index
        """).fetchall()
    preguntas = {}
    for r in qrows:
        preguntas.setdefault(r["clip_id"], []).append(dict(r))

    alucinados = cargar_alucinados(conn)
    vocabulario = set() if args.sin_keywords else cargar_vocabulario(conn, root)

    # --- silencios por clip ----------------------------------------------
    silencios = {}
    try:
        for cid, s, e in conn.execute(
                "SELECT clip_id, start_sec, end_sec FROM clip_silences "
                "ORDER BY clip_id, start_sec"):
            silencios.setdefault(cid, []).append((s, e))
    except sqlite3.OperationalError:
        print("  · sin tabla clip_silences — correr bin/detect_pauses.py para "
              "tener pausas. Sigo sin ellas.")

    # --- eventos sonoros por clip ----------------------------------------
    eventos = {}
    if not args.sin_emocion:
        try:
            grupos = tuple(ib.GRUPOS_EMOCION)
            ph = ",".join("?" * len(grupos))
            for cid, s, e, g, c in conn.execute(
                    f"SELECT clip_id, start_sec, end_sec, event_group, confidence "
                    f"FROM sound_events WHERE event_group IN ({ph}) "
                    f"AND confidence >= 0.30 ORDER BY clip_id, start_sec", grupos):
                eventos.setdefault(cid, []).append((s, e, g, c))
        except sqlite3.OperationalError:
            pass  # proyecto sin PANNs

    # --- clips candidatos --------------------------------------------------
    cids = sorted(set(preguntas) | set(silencios) | set(eventos))
    if solo:
        cids = [c for c in cids if c in solo]
    duraciones = {r[0]: r[1] for r in conn.execute(
        "SELECT id, duration_sec FROM clips")}
    con_master = {int(p.stem.replace("master_", ""))
                  for p in tr_dir.glob("master_*.json")} if tr_dir.exists() else set()

    assert_selected(cids, "clips con preguntas/silencios/eventos", filters={
        "--root": str(root), "--clip-ids": args.clip_ids or "(todos)",
    }, hint="Correr antes derive_question_segments.py y detect_pauses.py.")

    # --- derivar -----------------------------------------------------------
    n_clips = n_beats = 0
    n_saltados = 0
    por_kind = Counter()
    por_metodo = Counter()
    for cid in cids:
        if cid in alucinados and cid not in con_master:
            # Excepcion documentada: con master, las preguntas vienen del
            # lavalier limpio y SI son corroborables aunque el A1 este alucinado.
            n_saltados += 1
            continue

        words_src = words_plain = None
        mp = tr_dir / f"master_{cid}.json"
        if mp.exists():
            try:
                words_src = json.loads(mp.read_text(encoding="utf-8")).get("words")
            except (json.JSONDecodeError, OSError):
                words_src = None
        pp = tr_dir / f"{cid}.json"
        if pp.exists():
            try:
                words_plain = json.loads(pp.read_text(encoding="utf-8")).get("words")
            except (json.JSONDecodeError, OSError):
                words_plain = None

        beats = ib.beats_de_clip(
            questions=preguntas.get(cid, []),
            words_src=words_src,
            words_plain=words_plain,
            silences=silencios.get(cid, []),
            vocabulario=vocabulario,
            sound_events=eventos.get(cid, []),
            clip_dur=float(duraciones.get(cid) or 0),
        )
        if not args.dry_run:
            guardar_beats(conn, cid, beats)
        if not beats:
            continue
        n_clips += 1
        n_beats += len(beats)
        for b in beats:
            por_kind[b["kind"]] += 1
            if b["kind"] == "pregunta":
                por_metodo[b["source"]] += 1

    if not args.dry_run:
        if not args.clip_ids.strip():
            n_viejos = quitar_beats_sin_origen(conn, cids)
            if n_viejos:
                print(f"  {n_viejos} beat(s) de clips que ya no tienen preguntas, "
                      f"silencios ni eventos: fuera")
        conn.commit()
    conn.close()

    print(f"\n{'(dry-run) ' if args.dry_run else ''}Beats derivados: "
          f"{n_beats} en {n_clips} clips")
    for k in ib.KINDS:
        if por_kind[k]:
            print(f"  {k:<10} {por_kind[k]:>5}")
    if n_saltados:
        print(f"  saltados por transcript alucinado (y sin master): {n_saltados}")
    if por_metodo:
        print("\nComo se separo pregunta de respuesta:")
        etiquetas = {
            "master_src": "cambio de fuente en el master (fisico, el mas fiable)",
            "silencio":   "primer silencio real tras la pregunta",
            "estimado":   "estimado por numero de palabras — limite aproximado",
        }
        total = sum(por_metodo.values())
        for m, n in por_metodo.most_common():
            print(f"  {n:>5} ({100*n/total:4.0f}%)  {etiquetas.get(m, m)}")
        if por_metodo.get("estimado"):
            print(f"\n  · {por_metodo['estimado']} preguntas quedaron con limite "
                  f"ESTIMADO. Suelen ser entrevistas sin master transcript o sin "
                  f"silencios medidos: correr build_master_transcripts.py y "
                  f"detect_pauses.py sube la precision.")
    report_done("derive_interview_beats", clips=n_clips, beats=n_beats,
                saltados=n_saltados)
    return 0


if __name__ == "__main__":
    sys.exit(main())
