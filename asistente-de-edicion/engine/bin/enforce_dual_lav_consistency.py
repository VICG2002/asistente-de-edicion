#!/usr/bin/env python3
"""Impone la consistencia física entre los dos lavalieres de cada video.

Caso Film Club Café (2026-07-10, reporte del usuario: "el lava de la chica
está bien synqueado pero el del señor no, se alcanza a colar el audio entre
ambos lavas"): con dos TX grabando continuo, el delta de reloj entre las dos
cadenas es CONSTANTE (medido por waveform: ±8 ms a lo largo de 3 horas) y los
archivos de cada cadena son contiguos muestra a muestra. Eso da una ecuación
exacta por video:

    off_chain(TX_A) − off_chain(TX_B) = D_dia      (±~50 ms)

donde off_chain = offset_del_par − prefijo_del_archivo_en_su_cadena
(prefijo = suma de duraciones de los archivos previos de la cadena).

El script:
  1. Reconstruye cadenas (build_chains de derive_chrono_sync) + prefijos.
  2. Mide D por par de cadenas co-día: mediana de (off_chainA − off_chainB)
     sobre los videos cuyas DOS patas están medidas físicamente (method con
     longwin/transcript/manual); fallback a todos los duales.
  3. Para cada video dual que viole D más que --tolerance:
       - pata ANCLA = la medida físicamente (longwin/transcript/manual);
       - la pata débil (chrono puro) se re-deriva: off = ancla ± D
         (method='lavalier-derived-locked', conf 0.90).
       - ambas medidas y aun así violan → reporta, NO toca (falso dual o
         drift real: decide el humano).
       - ambas débiles → ancla = la pata cuya cadena tiene más anchors de
         transcript para ese grupo de cámara (heurística conservadora).

Equivale al "auto sync por waveform" de DaVinci que pidió el usuario, pero
del lado del motor: mismo principio físico, sin romper el layout de pistas
(A1 intocable, lavs al final).

Uso:
    bin/enforce_dual_lav_consistency.py --root <disk>
        [--audio-like '%wireless%'] [--tolerance 0.08] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from derive_chrono_sync import build_chains, resolve_root  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

MEASURED_HINTS = ("longwin", "transcript", "manual")


def is_measured(method: str, notes: str) -> bool:
    m = (method or "").lower()
    n = (notes or "").lower()
    return any(h in m for h in MEASURED_HINTS) or "fisico" in n or "longwin" in n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%wireless%")
    ap.add_argument("--tolerance", type=float, default=0.08,
                    help="Violación mínima (s) de D para corregir.")
    ap.add_argument("--chain-tolerance", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    conn = manifest.conectar(str(root / ".cinema_assistant" / "manifest.sqlite"))

    # --- cadenas + prefijos (tiempo-de-cadena) ------------------------------
    wavs = {}
    for aid, fn, rel, mt, dur in conn.execute(
        "SELECT id, filename, rel_path, mtime, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(filename) LIKE ?", (args.audio_like,)
    ):
        if mt and dur:
            wavs[aid] = {"fn": fn, "rel": rel, "start": mt - dur, "dur": dur}
    chain_of = build_chains(wavs, args.chain_tolerance)
    prefix = {}
    for cid in set(chain_of.values()):
        members = sorted((wavs[a]["start"], a) for a, c in chain_of.items()
                         if c == cid)
        acc = 0.0
        for _, aid in members:
            prefix[aid] = acc
            acc += wavs[aid]["dur"]

    # --- pares por video ----------------------------------------------------
    rows = conn.execute(
        "SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec, "
        "sp.method, COALESCE(sp.notes,''), v.filename "
        "FROM audio_sync_pairs sp JOIN clips v ON v.id=sp.video_clip_id "
        "WHERE sp.audio_clip_id IN (%s)" % ",".join(map(str, wavs))
    ).fetchall()
    by_vid: dict[int, dict] = {}
    for pid, vid, aid, off, method, notes, vfn in rows:
        by_vid.setdefault(vid, {})[chain_of[aid]] = {
            "pid": pid, "aid": aid, "off": off, "method": method,
            "notes": notes, "vfn": vfn,
            "chain_off": off - prefix[aid],
            "measured": is_measured(method, notes),
        }

    duals = {v: d for v, d in by_vid.items() if len(d) == 2}
    print(f"Videos con ambos lavs: {len(duals)}")

    # --- D por par de cadenas (mediana de duales medidos) --------------------
    diffs_measured: dict[tuple, list[float]] = {}
    diffs_all: dict[tuple, list[float]] = {}
    for v, d in duals.items():
        (c1, l1), (c2, l2) = sorted(d.items())
        key = (c1, c2)
        diff = l1["chain_off"] - l2["chain_off"]
        diffs_all.setdefault(key, []).append(diff)
        if l1["measured"] and l2["measured"]:
            diffs_measured.setdefault(key, []).append(diff)

    D: dict[tuple, float] = {}
    for key in diffs_all:
        src = diffs_measured.get(key, [])
        used = src if len(src) >= 5 else diffs_all[key]
        med = statistics.median(used)
        clean = [x for x in used if abs(x - med) <= 1.0]
        D[key] = statistics.median(clean)
        print(f"D {key[0].split('#')[-1]} − {key[1].split('#')[-1]} = "
              f"{D[key]:+.3f}s (n={len(clean)} "
              f"{'medidos' if used is src else 'todos'}, "
              f"std={statistics.pstdev(clean):.3f})")

    # anchors de transcript por cadena (para desempatar duales ambos-débiles)
    tr_anchor_count: dict[str, int] = {}
    for (aid,) in conn.execute(
            "SELECT audio_clip_id FROM audio_sync_pairs "
            "WHERE method LIKE 'transcript%'"):
        if aid in chain_of:
            c = chain_of[aid]
            tr_anchor_count[c] = tr_anchor_count.get(c, 0) + 1

    now = time.time()
    n_fix = n_ok = n_conflict = 0
    for v, d in sorted(duals.items(), key=lambda kv: kv[1][list(kv[1])[0]]["vfn"]):
        (c1, l1), (c2, l2) = sorted(d.items())
        viol = (l1["chain_off"] - l2["chain_off"]) - D[(c1, c2)]
        if abs(viol) <= args.tolerance:
            n_ok += 1
            continue
        if l1["measured"] and l2["measured"]:
            n_conflict += 1
            print(f"  ⚠ AMBAS MEDIDAS y violan D por {viol:+.3f}s: "
                  f"{l1['vfn']} — revisar a mano (¿drift o falso dual?)")
            continue
        if l1["measured"] or l2["measured"]:
            anchor, weak = (l1, l2) if l1["measured"] else (l2, l1)
        else:
            # ambos débiles: ancla = cadena con más anchors de transcript
            anchor, weak = ((l1, l2) if tr_anchor_count.get(c1, 0) >=
                            tr_anchor_count.get(c2, 0) else (l2, l1))
        # resolver la ecuación off_chain(l1) − off_chain(l2) = D
        if anchor is l1:
            new_chain_off = l1["chain_off"] - D[(c1, c2)]
        else:
            new_chain_off = l2["chain_off"] + D[(c1, c2)]
        new_off = new_chain_off + prefix[weak["aid"]]
        delta = new_off - weak["off"]
        print(f"  ✓ {weak['vfn']}: {wavs[weak['aid']]['fn']} "
              f"{weak['off']:+.3f} → {new_off:+.3f} (Δ{delta*1000:+.0f}ms, "
              f"ancla={'medida' if anchor['measured'] else 'heurística'})")
        if not args.dry_run:
            conn.execute(
                "UPDATE audio_sync_pairs SET offset_sec=?, "
                "method='lavalier-derived-locked', confidence=0.90, "
                "notes=? WHERE id=?",
                (new_off, weak["notes"] +
                 f" | consistencia dual-lav: {weak['off']:+.3f}->{new_off:+.3f} "
                 f"(D={D[(c1, c2)]:+.3f}s desde pata "
                 f"{'medida' if anchor['measured'] else 'heurística'}, "
                 f"FCC 2026-07-10)", weak["pid"]))
        n_fix += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"\nConsistentes: {n_ok} | corregidos: {n_fix} | "
          f"conflicto ambas-medidas: {n_conflict}")
    if args.dry_run:
        print("(dry-run: nada escrito)")


if __name__ == "__main__":
    main()
