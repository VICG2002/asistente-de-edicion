#!/usr/bin/env python3
"""Verifier: ningún bake sale con offsets de lavalier sin validar.

Garantía nacida del caso Film Club Café (2026-07-09/10): el primer bake
salió con offsets chrono crudos (aritmética de reloj, jitter ±0.3-1 s) y el
usuario detectó A OJO el delay en las entrevistas 3-8 y el eco entre lavs
en la entrevista de la banda. "Asegúrate de que algo así no vuelva a
pasar" (usuario, 2026-07-10) — este script ES esa garantía; corre antes de
declarar cualquier bake listo (playbook §12b) y falla si:

  1. VIOLACIÓN DUAL-LAV: un video con ambos lavs viola la ecuación física
     off_chain(TX_A) − off_chain(TX_B) = D_día (tolerancia --tolerance).
     D se mide con la mediana de los duales con AMBAS patas medidas.
  2. ENTREVISTA SIN VALIDAR: un clip categoría 'entrevista%' tiene un par
     de lav que NI está medido físicamente (longwin / transcript / manual /
     lavalier-derived / físico-segmentos) NI queda validado por la ecuación
     dual. Ver a ojo un delay en una entrevista implica exactamente esto.

  Reporta como INFO (sin fallar) los pares chrono-crudos de B-roll no-dual:
  sin habla ni pata gemela no hay señal para validarlos — quedan contados
  honestamente.

Uso:
    bin/verify_lav_offsets.py --root <disk> [--audio-like '%wireless%']
        [--tolerance 0.10] [--report-only]

Exit 1 si hay violaciones (0 con --report-only).
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from derive_chrono_sync import build_chains, resolve_root  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402
from lib import proyecto  # noqa: E402


def tolerancia_de_cadena(flag: float | None, cfg: dict) -> float:
    """El flag manda; si no viene, lo que declara el proyecto; si no, 5 s.

    Se declara por proyecto porque depende de COMO grabaron los TX: los cortes
    automaticos de un Rode son continuos a nivel de muestra, pero un stop/start
    a mano deja un hueco de 1-2 s que 5 s de tolerancia se come. En Asistente
    (2026-09-28) eso unio drc/00035 y drc/00036 y fabrico 7 'DUAL VIOLA D'
    falsas, y el acta, que llama a este script sin flags, las volvia a ver.
    """
    if flag is not None:
        return flag
    return float(cfg.get("cadena_tolerancia_s", 5.0))

MEASURED_HINTS = ("longwin", "transcript", "manual", "lavalier-derived")


def is_measured(method: str, notes: str) -> bool:
    m = (method or "").lower()
    n = (notes or "").lower()
    return (any(h in m for h in MEASURED_HINTS)
            or "fisico" in n or "longwin" in n or "consistencia dual-lav" in n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%wireless%")
    ap.add_argument("--tolerance", type=float, default=0.10,
                    help="Violación máxima (s) de la ecuación dual-lav.")
    ap.add_argument("--chain-tolerance", type=float, default=None,
                    help="Hueco maximo (s) para unir WAV en una cadena. Default: "
                         "'cadena_tolerancia_s' del project_config.json, o 5.0.")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    args.chain_tolerance = tolerancia_de_cadena(args.chain_tolerance,
                                                proyecto.leer_config(root))
    conn = manifest.conectar(str(root / ".cinema_assistant" / "manifest.sqlite"))

    wavs = {}
    for aid, fn, rel, mt, dur in conn.execute(
        "SELECT id, filename, rel_path, mtime, duration_sec FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(filename) LIKE ?", (args.audio_like,)
    ):
        if mt and dur:
            wavs[aid] = {"fn": fn, "rel": rel, "start": mt - dur, "dur": dur}
    if not wavs:
        print("Sin audios continuos que verificar — OK trivial.")
        return 0
    chain_of = build_chains(wavs, args.chain_tolerance)
    prefix = {}
    for cid in set(chain_of.values()):
        members = sorted((wavs[a]["start"], a) for a, c in chain_of.items()
                         if c == cid)
        acc = 0.0
        for _, aid in members:
            prefix[aid] = acc
            acc += wavs[aid]["dur"]

    interviews = {v for (v,) in conn.execute(
        "SELECT clip_id FROM clip_descriptions "
        "WHERE category LIKE 'entrevista%' AND category != 'entrevista-solo-video'")}

    rows = conn.execute(
        "SELECT sp.video_clip_id, sp.audio_clip_id, sp.offset_sec, sp.method, "
        "COALESCE(sp.notes,''), v.filename "
        "FROM audio_sync_pairs sp JOIN clips v ON v.id=sp.video_clip_id "
        "WHERE sp.audio_clip_id IN (%s)" % ",".join(map(str, wavs))
    ).fetchall()
    by_vid: dict[int, dict] = {}
    for vid, aid, off, method, notes, vfn in rows:
        by_vid.setdefault(vid, {})[chain_of[aid]] = {
            "aid": aid, "off": off, "vfn": vfn,
            "chain_off": off - prefix[aid],
            "measured": is_measured(method, notes),
        }

    duals = {v: d for v, d in by_vid.items() if len(d) == 2}
    diffs_measured: dict[tuple, list[float]] = {}
    for v, d in duals.items():
        (c1, l1), (c2, l2) = sorted(d.items())
        if l1["measured"] and l2["measured"]:
            diffs_measured.setdefault((c1, c2), []).append(
                l1["chain_off"] - l2["chain_off"])
    D = {}
    for key, vals in diffs_measured.items():
        med = statistics.median(vals)
        clean = [x for x in vals if abs(x - med) <= 1.0]
        if len(clean) >= 3:
            D[key] = statistics.median(clean)

    viol_dual, viol_interview, info_broll = [], [], 0
    dual_ok_vids = set()
    for v, d in duals.items():
        (c1, l1), (c2, l2) = sorted(d.items())
        if (c1, c2) not in D:
            continue
        err = (l1["chain_off"] - l2["chain_off"]) - D[(c1, c2)]
        if abs(err) <= args.tolerance:
            dual_ok_vids.add(v)
        else:
            viol_dual.append((l1["vfn"], err))

    for vid, d in by_vid.items():
        for cid, leg in d.items():
            validated = leg["measured"] or vid in dual_ok_vids
            if validated:
                continue
            if vid in interviews:
                viol_interview.append((leg["vfn"], wavs[leg["aid"]]["fn"]))
            else:
                info_broll += 1

    print(f"Duales: {len(duals)} | D medidas: "
          + ", ".join(f"{k[0].split('#')[-1]}−{k[1].split('#')[-1]}"
                      f"={v:+.3f}s" for k, v in D.items()))
    for vfn, err in sorted(viol_dual):
        print(f"  ✗ DUAL VIOLA D: {vfn} (err {err:+.3f}s) — correr "
              f"enforce_dual_lav_consistency.py")
    for vfn, afn in sorted(viol_interview):
        print(f"  ✗ ENTREVISTA SIN VALIDAR: {vfn} ↔ {afn} — correr "
              f"refine_pairs_longwin.py --only-interviews")
    print(f"  (INFO) pares chrono-crudos de B-roll no validables: {info_broll}")

    n_bad = len(viol_dual) + len(viol_interview)
    if n_bad:
        print(f"\nFAIL verify_lav_offsets: {n_bad} violación(es). "
              f"NO declarar el bake listo.")
        return 0 if args.report_only else 1
    print("\n✅ verify_lav_offsets: todos los lavs medidos o validados por "
          "la ecuación dual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
