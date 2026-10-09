#!/usr/bin/env python3
"""Deriva sync video↔audio por reloj de pared, anclado en pares de transcript.

Caso de uso (Más Allá del Balón 2026-06-11, generaliza Zezzions §8d): el
recorder graba CONTINUO todo el día (archivos consecutivos sin gap) y las
cámaras tienen reloj con desfase constante. Los pares de sync por transcript
(alta confianza) permiten medir el desfase reloj-cámara vs reloj-recorder
("skew") por grupo de cámara; con eso se deriva el offset de TODOS los videos
que caen dentro de la ventana de algún audio, no solo los que tienen diálogo
denso.

También detecta y corrige pares existentes cuyo skew se desvía de la norma de
su cámara (síntoma: el alineador de transcript matcheó contenido repetido en
otro momento del día — típico en vox pop donde el host repite preguntas).

Grupo de cámara (genérico, sin hardcodes):
  --group-by parent   → parent_folder del clip (default, comportamiento MAB).
  --group-by dirname  → directorio completo del rel_path (layouts anidados
                        tipo `Jueves/VIDEO/A/`, Film Club Café 2026-07-09).

Modo multi-TX (Film Club Café 2026-07-09): con `--per-chain`, los audios
continuos se agrupan en CADENAS de continuidad temporal (mismo directorio,
inicio ≈ fin del anterior ±tolerancia; soporta cadenas intercaladas de dos
TX grabando en paralelo). Cada TX tiene reloj propio → el skew se mide por
(grupo de cámara × cadena) y se deriva UN par por video × cadena traslapante,
cumpliendo la regla 8b ("ambos lavas siempre con sync"). Sin el flag, el
comportamiento clásico (mejor overlap único por video) queda intacto.

Los pares derivados se marcan `chrono-derived-locked` (blindados contra
refinement automático); los corregidos, `chrono-corrected-locked`.

Uso:
    bin/derive_chrono_sync.py --root <disk> [--audio-like '%wireless%']
        [--skew-tolerance 10] [--min-overlap 5] [--per-chain]
        [--chain-tolerance 5] [--group-by parent|dirname] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import posixpath
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402
from lib import proyecto  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def video_epoch(creation_time: str, utc_offset_hours: float) -> float | None:
    """Epoch local del inicio del video desde el tag creation_time (UTC)."""
    if not creation_time:
        return None
    try:
        dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
        local = dt + timedelta(hours=utc_offset_hours)
        return time.mktime(local.replace(tzinfo=None).timetuple())
    except (ValueError, OverflowError):
        return None


def build_chains(wavs: dict[int, dict], tol: float) -> dict[int, str]:
    """Agrupa los audios continuos en cadenas de continuidad temporal.

    Cadena = archivos del MISMO directorio cuyo inicio empalma con el fin de
    un archivo anterior (±tol segundos). Greedy por cercanía de fin, lo que
    soporta cadenas INTERCALADAS (dos TX grabando en paralelo dentro de la
    misma carpeta, caso Film Club Café: 00023.. y 00037.. en `Jueves/AUDIO`).

    Devuelve {audio_id: chain_id} con chain_id = "<dir>#<primer filename>".
    """
    chain_of: dict[int, str] = {}
    items = sorted(
        wavs.items(),
        key=lambda kv: (posixpath.dirname(kv[1]["rel"]), kv[1]["start"]),
    )
    open_chains: list[dict] = []  # {dir, id, end}
    for aid, w in items:
        d = posixpath.dirname(w["rel"])
        best = None
        for ch in open_chains:
            if ch["dir"] != d:
                continue
            gap = abs(w["start"] - ch["end"])
            if gap <= tol and (best is None or gap < best[0]):
                best = (gap, ch)
        if best is None:
            ch = {"dir": d, "id": f"{d}#{w['fn']}", "end": w["start"] + w["dur"]}
            open_chains.append(ch)
        else:
            ch = best[1]
            ch["end"] = w["start"] + w["dur"]
        chain_of[aid] = ch["id"]
    return chain_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%wireless%",
                    help="filename LIKE (lowercased) de los audios continuos.")
    ap.add_argument("--utc-offset", type=float, default=-6.0,
                    help="Horas a sumar al creation_time UTC del video para "
                         "llevarlo al reloj local del filesystem (CDMX=-6).")
    ap.add_argument("--anchor-methods", default="transcript",
                    help="CSV de methods confiables para medir skew.")
    ap.add_argument("--skew-tolerance", type=float, default=10.0,
                    help="Desviación máxima (s) vs la mediana del grupo antes "
                         "de marcar un par como outlier a corregir.")
    ap.add_argument("--min-overlap", type=float, default=5.0,
                    help="Overlap mínimo video∩audio (s) para escribir par.")
    ap.add_argument("--min-anchors", type=int, default=3,
                    help="Anchors mínimos por grupo (× cadena con --per-chain) "
                         "para derivar.")
    ap.add_argument("--per-chain", action="store_true",
                    help="Deriva un par por video × CADENA de audio continuo "
                         "(multi-TX). Skew por (grupo × cadena). Default: "
                         "comportamiento clásico (mejor overlap único).")
    ap.add_argument("--bridge-chains", action="store_true",
                    help="Con --per-chain: completa skews faltantes de "
                         "(grupo × cadena) por TRANSITIVIDAD de relojes "
                         "(skew(g,c) = skew(g,c0) + skew(g2,c) − skew(g2,c0)). "
                         "Se descarta si contradice anchors locales débiles. "
                         "Pares con skew puenteado llevan confidence 0.80. "
                         "Validado en FCC 2026-07-09 (predicción vs anchors "
                         "débiles coincide a ±0.3s).")
    ap.add_argument("--chain-tolerance", type=float, default=5.0,
                    help="Gap máximo (s) inicio↔fin para encadenar audios "
                         "continuos (solo con --per-chain).")
    ap.add_argument("--group-by", choices=("parent", "dirname"),
                    default="parent",
                    help="Grupo de cámara: parent_folder (default) o el "
                         "directorio completo del rel_path (layouts anidados).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    conn = manifest.conectar(str(db))
    cfg = proyecto.leer_config(root)

    def vgroup(parent_folder: str, rel_path: str) -> str:
        return proyecto.grupo_de_camara(parent_folder, rel_path, cfg,
                                        por_dirname=args.group_by == "dirname")

    # --- audios continuos: ventana wall-clock desde mtime - duracion --------
    wavs = {}
    for aid, fn, rel, mt, dur in conn.execute(
        "SELECT id, filename, rel_path, mtime, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(filename) LIKE ?", (args.audio_like,)
    ):
        if mt and dur:
            wavs[aid] = {"fn": fn, "rel": rel, "start": mt - dur, "dur": dur}
    if not wavs:
        sys.exit(f"Sin audios que matcheen {args.audio_like}")
    print(f"Audios continuos: {len(wavs)}")

    if args.per_chain:
        chain_of = build_chains(wavs, args.chain_tolerance)
        for cid in sorted(set(chain_of.values())):
            members = sorted((wavs[a]["fn"] for a, c in chain_of.items()
                              if c == cid))
            print(f"Cadena '{cid}': {len(members)} archivos "
                  f"({members[0]} … {members[-1]})")
    else:
        chain_of = {aid: "" for aid in wavs}

    # --- anchors: método confiable -> skew por (grupo × cadena, audio) ------
    has_pairs_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audio_sync_pairs'").fetchone() is not None
    if not has_pairs_table:
        sys.exit("Sin tabla audio_sync_pairs (aún no corre ningún sync que "
                 "genere anchors) — nada que derivar. Corre primero "
                 "sync_transcript.py.")
    anchor_methods = tuple(m.strip() for m in args.anchor_methods.split(","))
    q_marks = ",".join("?" * len(anchor_methods))
    anchors = conn.execute(f"""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec,
               v.parent_folder, v.rel_path, v.creation_time, v.filename
        FROM audio_sync_pairs sp JOIN clips v ON v.id = sp.video_clip_id
        WHERE sp.method IN ({q_marks})
    """, anchor_methods).fetchall()

    skew_by_gc: dict[tuple, list[float]] = {}      # (grupo, cadena)
    skew_by_group_wav: dict[tuple, list[float]] = {}  # (grupo, audio_id)
    anchor_rows = []
    for pid, vid, aid, off, pf, rel, ct, vfn in anchors:
        if aid not in wavs:
            continue
        grp = vgroup(pf, rel)
        v_ep = video_epoch(ct, args.utc_offset)
        if v_ep is None:
            continue
        theo = wavs[aid]["start"] - v_ep
        skew = off - theo
        anchor_rows.append((pid, vid, aid, off, grp, v_ep, skew, vfn))
        skew_by_gc.setdefault((grp, chain_of[aid]), []).append(skew)
        skew_by_group_wav.setdefault((grp, aid), []).append(skew)

    # mediana robusta por (grupo × cadena) + rechazo de outliers
    gc_median: dict[tuple, float] = {}
    for (grp, cid), vals in sorted(skew_by_gc.items()):
        med = statistics.median(vals)
        clean = [v for v in vals if abs(v - med) <= args.skew_tolerance]
        label = f"Grupo '{grp}'" + (f" cadena '{cid}'" if cid else "")
        if len(clean) >= args.min_anchors:
            gc_median[(grp, cid)] = statistics.median(clean)
            print(f"{label}: skew mediana={gc_median[(grp, cid)]:.2f}s "
                  f"(n={len(clean)}/{len(vals)}, "
                  f"std={statistics.pstdev(clean):.2f}s)")
        else:
            print(f"{label}: solo {len(clean)} anchors limpios "
                  f"(< {args.min_anchors}) — sin derivación")

    # --- puenteo transitivo de skews faltantes (multi-TX) -------------------
    bridged: set[tuple] = set()
    if args.per_chain and args.bridge_chains:
        changed = True
        while changed:
            changed = False
            grps = {g for (g, _) in gc_median}
            chains_all = {c for (_, c) in gc_median}
            for g in sorted(grps):
                have = {c for (gg, c) in gc_median if gg == g}
                for c in sorted(chains_all - have):
                    preds = []
                    for g2 in sorted(grps - {g}):
                        if (g2, c) not in gc_median:
                            continue
                        for c0 in sorted(have):
                            if (g2, c0) in gc_median:
                                preds.append(gc_median[(g, c0)]
                                             + gc_median[(g2, c)]
                                             - gc_median[(g2, c0)])
                    if not preds:
                        continue
                    med = statistics.median(preds)
                    note = ""
                    weak = skew_by_gc.get((g, c))
                    if weak:
                        wmed = statistics.median(weak)
                        if abs(wmed - med) > args.skew_tolerance:
                            print(f"  BRIDGE DESCARTADO grupo '{g}' cadena "
                                  f"'{c}': predicción {med:.2f}s contradice "
                                  f"anchors locales ({wmed:.2f}s)")
                            continue
                        note = (f"; {len(weak)} anchors locales débiles "
                                f"(med={wmed:.2f}s) confirman")
                    gc_median[(g, c)] = med
                    bridged.add((g, c))
                    print(f"Grupo '{g}' cadena '{c}': skew PUENTEADO="
                          f"{med:.2f}s (n_rutas={len(preds)}{note})")
                    changed = True

    def local_skew(grp: str, aid: int) -> float | None:
        med_gc = gc_median.get((grp, chain_of[aid]))
        if med_gc is None:
            return None
        vals = skew_by_group_wav.get((grp, aid))
        if vals:
            clean = [v for v in vals if abs(v - med_gc) <= args.skew_tolerance]
            if clean:
                return statistics.median(clean)
        return med_gc

    # --- 1) corregir anchors outliers (WAV equivocado / match repetido) -----
    now = time.time()
    n_fixed = 0
    for pid, vid, aid, off, grp, v_ep, skew, vfn in anchor_rows:
        med = gc_median.get((grp, chain_of[aid]))
        if med is None or abs(skew - med) <= args.skew_tolerance:
            continue
        # buscar el WAV correcto: mayor overlap con el reloj corregido.
        # Cada candidato usa la mediana de SU propia cadena (reloj propio).
        vdur = conn.execute("SELECT duration_sec FROM clips WHERE id=?",
                            (vid,)).fetchone()[0] or 0
        best = None
        for cand_aid, w in wavs.items():
            med_c = gc_median.get((grp, chain_of[cand_aid]))
            if med_c is None:
                continue
            cand_off = med_c + (w["start"] - v_ep)
            overlap = min(vdur, cand_off + w["dur"]) - max(0.0, cand_off)
            if overlap >= args.min_overlap and (best is None or overlap > best[2]):
                best = (cand_aid, cand_off, overlap)
        if best is None:
            print(f"  OUTLIER sin WAV correcto: par {pid} ({vfn}) — revisar a mano")
            continue
        cand_aid, cand_off, overlap = best
        print(f"  CORRIGIENDO par {pid} ({vfn}): {wavs[aid]['fn']} off={off:.2f} "
              f"-> {wavs[cand_aid]['fn']} off={cand_off:.2f} (skew anómalo {skew:.1f}s)")
        if not args.dry_run:
            conn.execute(
                "UPDATE audio_sync_pairs SET audio_clip_id=?, offset_sec=?, "
                "method='chrono-corrected-locked', confidence=0.85, "
                "notes=?, created_at=? WHERE id=?",
                (cand_aid, cand_off,
                 f"corregido por skew anómalo ({skew:.1f}s vs mediana {med:.1f}s del grupo {grp})",
                 now, pid))
        n_fixed += 1

    # --- 2) derivar pares para videos sin sync (por cadena con --per-chain) --
    if args.per_chain:
        # exclusión por (video, cadena): un video puede tener ya el TX-1 por
        # transcript y aun así necesitar derivar el TX-2 (regla 8b).
        existing_chains: dict[int, set] = {}
        for vid_, aid_ in conn.execute(
                "SELECT video_clip_id, audio_clip_id FROM audio_sync_pairs"):
            if aid_ in wavs:
                existing_chains.setdefault(vid_, set()).add(chain_of[aid_])
        videos = conn.execute("""
            SELECT c.id, c.parent_folder, c.rel_path, c.creation_time,
                   c.duration_sec, c.filename
            FROM clips c
            WHERE c.file_kind='video' AND c.index_status='ok'
        """).fetchall()
    else:
        existing_chains = {}
        videos = conn.execute("""
            SELECT c.id, c.parent_folder, c.rel_path, c.creation_time,
                   c.duration_sec, c.filename
            FROM clips c
            WHERE c.file_kind='video' AND c.index_status='ok'
              AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
        """).fetchall()

    n_derived = n_no_window = n_no_group = 0
    for vid, pf, rel, ct, vdur, vfn in videos:
        grp = vgroup(pf, rel)
        chains_for_grp = sorted({cid for (g, cid) in gc_median if g == grp})
        if not chains_for_grp:
            n_no_group += 1
            continue
        v_ep = video_epoch(ct, args.utc_offset)
        if v_ep is None:
            n_no_window += 1
            continue
        derived_any = had_pending_chain = False
        for cid in chains_for_grp:
            if cid in existing_chains.get(vid, set()):
                continue
            had_pending_chain = True
            best = None
            for aid, w in wavs.items():
                if chain_of[aid] != cid:
                    continue
                skew = local_skew(grp, aid)
                if skew is None:
                    continue
                off = skew + (w["start"] - v_ep)
                overlap = min(vdur or 0, off + w["dur"]) - max(0.0, off)
                if overlap >= args.min_overlap and (best is None or overlap > best[2]):
                    best = (aid, off, overlap, skew)
            if best is None:
                continue
            aid, off, overlap, skew = best
            chain_note = f", cadena {cid}" if cid else ""
            is_bridged = (grp, cid) in bridged
            if is_bridged:
                chain_note += ", skew puenteado por transitividad"
            if not args.dry_run:
                conn.execute(
                    "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, "
                    "method, offset_sec, confidence, overlap_sec, notes, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (vid, aid, "chrono-derived-locked", off,
                     0.80 if is_bridged else 0.85, overlap,
                     f"derivado por reloj (skew {skew:.2f}s, grupo {grp}{chain_note})",
                     now))
            n_derived += 1
            derived_any = True
        if had_pending_chain and not derived_any:
            n_no_window += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\nPares corregidos (outliers): {n_fixed}")
    print(f"Pares derivados por cronología: {n_derived}")
    print(f"Videos sin ventana de audio aplicable: {n_no_window}")
    print(f"Videos de grupos sin anchors suficientes: {n_no_group}")
    if args.dry_run:
        print("(dry-run: nada escrito)")


if __name__ == "__main__":
    main()
