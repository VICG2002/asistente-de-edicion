#!/usr/bin/env python3
"""CLI interactivo para revisar manualmente pares de sync marginales.

Caso fundador (Zezzions 2026-05-26): después de los métodos automáticos
(transcript, questions, phrase-match, acoustic, chromaprint), algunos
pares quedan con confianza baja (conf < 0.40) y son sospechosos. Otros
videos no syncan con nadie. Este script:

  1. Lista pares con conf < threshold para revisión.
  2. Para cada par sospechoso o video sin sync:
     - Muestra info del video (filename, dur, identidades en clip_characters).
     - Muestra top-N audio candidatos (por chromaprint, envelope, phrase).
     - Para cada candidato, ofrece comando `ffplay` para escuchar el tramo
       que correspondería tras aplicar el offset propuesto.
  3. Acepta input del usuario:
     - `keep`   — mantener par actual
     - `delete` — borrar par (sync incorrecto)
     - `swap N` — reemplazar por candidato N
     - `manual <aid> <offset>` — usar audio_id + offset manualmente
     - `standalone` — marcar el audio como "sin video" (no más intentos)
     - `skip`   — pasar al siguiente

Uso:
    bin/sync_review.py --root /Volumes/.../Zezzions VICG \\
        --threshold 0.40 --include-orphans

Notas:
- No reproduce audio directamente — emite el comando ffplay para que el
  usuario lo corra en otra terminal.
- Edita audio_sync_pairs en bulk al final (transacción explícita).
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
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
    sys.exit(f"Root no resolvible: {root_arg}")


def get_candidates(conn, vid, top_n=5):
    """Lista los top-N audios candidatos para un video, usando todas las
    señales disponibles. Para uso interactivo, no escribe."""
    # Por ahora, mostrar los que ya tienen sync (de cualquier método) +
    # los que no, ordenados por método.
    rows = conn.execute("""
        SELECT a.id, a.filename, sp.method, sp.offset_sec, sp.confidence, sp.notes
        FROM audio_sync_pairs sp
        JOIN clips a ON a.id=sp.audio_clip_id
        WHERE sp.video_clip_id=?
        ORDER BY sp.confidence DESC
    """, (vid,)).fetchall()
    return rows


def list_marginal_pairs(conn, threshold):
    return conn.execute("""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, v.filename, a.filename,
               sp.method, sp.offset_sec, sp.confidence, sp.notes
        FROM audio_sync_pairs sp
        JOIN clips v ON v.id=sp.video_clip_id
        JOIN clips a ON a.id=sp.audio_clip_id
        WHERE sp.confidence < ?
        ORDER BY sp.confidence ASC
    """, (threshold,)).fetchall()


def list_orphan_videos(conn):
    """Videos marcados como entrevista que no tienen sync_pair."""
    return conn.execute("""
        SELECT c.id, c.filename, c.duration_sec, cc.characters
        FROM clips c
        LEFT JOIN clip_descriptions d ON d.clip_id=c.id
        LEFT JOIN clip_characters cc ON cc.clip_id=c.id
        WHERE c.file_kind='video' AND c.index_status='ok'
          AND d.category LIKE 'entrevista%'
          AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
    """).fetchall()


def list_orphan_audios(conn):
    """Audios sin sync a ningún video."""
    return conn.execute("""
        SELECT c.id, c.filename, c.duration_sec, cc.characters
        FROM clips c
        LEFT JOIN clip_characters cc ON cc.clip_id=c.id
        WHERE c.file_kind='audio' AND c.index_status='ok'
          AND c.id NOT IN (SELECT audio_clip_id FROM audio_sync_pairs)
    """).fetchall()


def ffplay_command(audio_path, start_sec, dur=10):
    return f"ffplay -nodisp -autoexit -ss {start_sec:.1f} -t {dur} {audio_path!r}"


def review_marginal(conn, args):
    pairs = list_marginal_pairs(conn, args.threshold)
    print(f"\n=== Pares marginales (conf < {args.threshold}): {len(pairs)} ===\n")
    for sp_id, vid, aid, vfn, afn, method, off, conf, notes in pairs:
        print(f"--- par sp{sp_id}: {vfn[:30]} ↔ {afn[:30]}")
        print(f"    method={method} offset={off:+.1f}s conf={conf:.2f} notes={notes}")
        # Datos contextuales
        v_chars = conn.execute("SELECT characters FROM clip_characters WHERE clip_id=?", (vid,)).fetchone()
        a_chars = conn.execute("SELECT characters FROM clip_characters WHERE clip_id=?", (aid,)).fetchone()
        if v_chars: print(f"    video identidades: {v_chars[0]}")
        if a_chars: print(f"    audio identidades: {a_chars[0]}")
        apath = conn.execute("SELECT path FROM clips WHERE id=?", (aid,)).fetchone()[0]
        # Sugerir un punto donde escuchar
        anchor = max(0, -off) if off < 0 else 0
        print(f"    Escuchar audio en pos {anchor:.0f}s (donde debería estar el inicio del video):")
        print(f"      {ffplay_command(apath, anchor)}")
        if args.dry_run:
            continue
        action = input("    [k]eep / [d]elete / [s]kip / [q]uit ? ").strip().lower()
        if action == "d":
            conn.execute("DELETE FROM audio_sync_pairs WHERE id=?", (sp_id,))
            print("    BORRADO.")
        elif action == "q":
            break
        else:
            print("    (mantenido)")


def review_orphans(conn, args):
    v_orphans = list_orphan_videos(conn)
    a_orphans = list_orphan_audios(conn)
    print(f"\n=== Videos entrevista sin sync: {len(v_orphans)} ===")
    for vid, vfn, vdur, vchars in v_orphans:
        print(f"  {vfn} ({vdur:.0f}s) chars={vchars}")
    print(f"\n=== Audios sin sync: {len(a_orphans)} ===")
    for aid, afn, adur, achars in a_orphans:
        marker = " STAND-ALONE" if achars and "stand-alone" in (achars.lower() if achars else "") else ""
        print(f"  {afn} ({adur:.0f}s) chars={achars}{marker}")


def list_sync_candidates(conn, status_filter='needs_review'):
    """Listar pairs en sync_candidates (sync_pipeline_full)."""
    rows = conn.execute("""
        SELECT sc.id, v.id, v.filename, v.duration_sec, ca.id, ca.filename,
               ca.duration_sec, sc.offset_sec, sc.confidence, sc.identity_score,
               sc.classification, sc.method, sc.signals_json, sc.notes
        FROM sync_candidates sc
        JOIN clips v ON v.id=sc.video_clip_id
        JOIN clips ca ON ca.id=sc.audio_clip_id
        WHERE sc.status=?
        ORDER BY sc.classification, v.filename
    """, (status_filter,)).fetchall()
    return rows


def review_sync_candidates(conn, args):
    """CLI para revisar sync_candidates (output del pipeline voice-first)."""
    rows = list_sync_candidates(conn, status_filter=args.status)
    if not rows:
        print(f"\nNo hay sync_candidates con status='{args.status}'")
        return

    print(f"\n=== sync_candidates (status='{args.status}') — {len(rows)} pairs ===\n")
    n_acc, n_rej, n_skip = 0, 0, 0
    import json as _json
    for r in rows:
        (sc_id, vid, vfn, vdur, aid, afn, adur,
         off, conf, idscore, classif, method, signals_json, notes) = r
        try:
            signals = _json.loads(signals_json) if signals_json else {}
        except: signals = {}
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"sc={sc_id}  {vfn} ({vdur:.0f}s) ↔ {afn} ({adur:.0f}s)")
        print(f"  classification: {classif}")
        print(f"  offset: {off:+.3f}s  conf: {conf:.2f}  identity_score: {idscore:.2f}")
        print(f"  notes: {notes}")
        if signals:
            print(f"  signals: total_anchors={signals.get('total_anchors')}, "
                  f"MAD={signals.get('mad_segments')}, multitake={signals.get('multitake_score')}")
        # ffplay command
        apath = conn.execute("SELECT path FROM clips WHERE id=?", (aid,)).fetchone()
        if apath and off:
            start = max(0, -off) if off < 0 else 0
            print(f"  ffplay -ss {start:.2f} -t 10 \"{apath[0]}\"")
        if args.dry_run:
            n_skip += 1
            continue
        cmd = input("[a=accept / r=reject / m <offset>=manual / s=skip] > ").strip()
        if not cmd or cmd == 's':
            n_skip += 1; continue
        if cmd == 'a':
            # Mover a audio_sync_pairs y marcar candidate como 'accepted'
            now = time.time()
            conn.execute("""
                INSERT INTO audio_sync_pairs
                (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                 identity_score, classification, signals_json, notes, created_at,
                 verifier_passed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (vid, aid, "manual-cli-accept", off, max(conf, 0.85),
                  idscore, classif, signals_json,
                  f"Accepted from sync_candidates sc={sc_id}: {notes}",
                  now, 1))
            conn.execute("UPDATE sync_candidates SET status='accepted', reviewed_at=?, "
                         "reviewed_by='human-cli' WHERE id=?", (now, sc_id))
            n_acc += 1
            print(f"  ✓ accepted (now in audio_sync_pairs)")
        elif cmd == 'r':
            conn.execute("UPDATE sync_candidates SET status='rejected', reviewed_at=?, "
                         "reviewed_by='human-cli' WHERE id=?", (time.time(), sc_id))
            n_rej += 1
            print(f"  ✗ rejected")
        elif cmd.startswith('m '):
            try:
                new_off = float(cmd.split()[1])
            except (ValueError, IndexError):
                print("  formato: m <offset_sec>")
                n_skip += 1; continue
            now = time.time()
            conn.execute("""
                INSERT INTO audio_sync_pairs
                (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                 identity_score, classification, notes, created_at, verifier_passed)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (vid, aid, "manual-cli-override", new_off, 1.0,
                  idscore, classif,
                  f"Manual override from sc={sc_id}, original off={off:+.3f}",
                  now, 1))
            conn.execute("UPDATE sync_candidates SET status='accepted', reviewed_at=?, "
                         "reviewed_by='human-cli' WHERE id=?", (now, sc_id))
            n_acc += 1
            print(f"  ✓ manual offset {new_off:+.3f}s grabado")
        conn.commit()
    print(f"\nResumen: {n_acc} accepted, {n_rej} rejected, {n_skip} skipped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--source", choices=("audio_sync_pairs", "sync_candidates"),
                    default="audio_sync_pairs",
                    help="Tabla fuente para review. sync_candidates = output de "
                         "sync_pipeline_full needs_review.")
    ap.add_argument("--status", default="needs_review",
                    help="Status filter para sync_candidates (default: needs_review)")
    ap.add_argument("--threshold", type=float, default=0.40,
                    help="Confianza máxima para considerar 'marginal' (modo audio_sync_pairs).")
    ap.add_argument("--include-orphans", action="store_true",
                    help="Listar también videos y audios sin sync.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo listar, no preguntar al usuario.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    conn = manifest.conectar(str(db))

    print(f"sync_review — manifest: {db}")
    print(f"  source: {args.source}")

    if args.source == "sync_candidates":
        review_sync_candidates(conn, args)
    else:
        review_marginal(conn, args)
        if args.include_orphans:
            review_orphans(conn, args)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
