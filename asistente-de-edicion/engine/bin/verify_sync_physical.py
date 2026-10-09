#!/usr/bin/env python3
"""Verificador físico de sync: detecta automáticamente pairs sospechosos.

Para cada pair en audio_sync_pairs:
  1. Divide el transcript del video en segmentos de 60s.
  2. En cada segmento, mide el offset físico por transcript ngram-alignment.
  3. Compara:
       - Consistencia ENTRE segmentos (MAD de offsets por segmento)
       - Bias vs offset BD (mediana global - BD)
  4. Clasifica el pair en:
       - 'ok'      : MAD entre segmentos < 0.15s Y bias vs BD < 0.10s
       - 'drift'   : MAD entre segmentos > 0.5s (frame rate mismatch o multi-take)
       - 'bias'    : MAD < 0.3s pero bias vs BD > 0.15s (offset wrong, no multi-take)
       - 'multitake': MAD entre segmentos > 0.3s pero existe consensus parcial
       - 'no_data' : pocos anchors (audio sin contenido equivalente al video)
       - 'unknown' : no se puede determinar

Reporta ordenado por severidad. NO modifica BD — solo diagnóstico.

Uso:
    bin/verify_sync_physical.py --root <disk>
    bin/verify_sync_physical.py --root <disk> --only-pairs 209,210
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def load_words(tr_dir: Path, cid: int) -> list[tuple[str, float]]:
    p = tr_dir / f"{cid}.json"
    if not p.exists():
        return []
    try:
        d = json.load(p.open())
    except Exception:
        return []
    out = []
    for w in d.get("words", []) or []:
        if isinstance(w, list) and len(w) >= 2:
            tx = str(w[0]).lower().strip(".,?¿!¡;:'\"-")
            t = float(w[1])
            if tx:
                out.append((tx, t))
    return out


def segment_offsets(words_v, words_a, expected: float, seg_sec: float = 60.0,
                    n_gram: int = 3, window: float = 3.0):
    """Por cada segmento de seg_sec del video, calcula offset físico.
    Returns: [(t_start, t_end, offset, n, mad), ...]
    """
    if not words_v or not words_a:
        return []
    v_end = words_v[-1][1]

    def to_ngrams(words, tmin=None, tmax=None):
        out = []
        for i in range(len(words) - n_gram + 1):
            text = " ".join(w[0] for w in words[i:i+n_gram])
            t_mid = words[i + n_gram//2][1]
            if tmin is not None and t_mid < tmin:
                continue
            if tmax is not None and t_mid > tmax:
                continue
            out.append((text, t_mid))
        return out

    amap = {}
    for text, t in to_ngrams(words_a):
        amap.setdefault(text, []).append(t)

    results = []
    t0 = 0.0
    while t0 < v_end:
        t1 = t0 + seg_sec
        shifts = []
        for text, t_v in to_ngrams(words_v, t0, t1):
            if text in amap:
                expected_t_a = t_v - expected
                cands = [t for t in amap[text] if abs(t - expected_t_a) < window]
                if not cands:
                    continue
                t_a = min(cands, key=lambda t: abs(t - expected_t_a))
                shifts.append(t_a - t_v)
        if len(shifts) >= 5:
            arr = np.array(shifts)
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med)))
            if mad > 0:
                keep = arr[(arr > med - 3*mad) & (arr < med + 3*mad)]
            else:
                keep = arr
            results.append((t0, t1, -float(np.median(keep)), len(keep), mad))
        t0 = t1
    return results


def detect_multitake(words_v, words_a, n=4):
    """Detecta multi-take: para cada 4-grama del video que aparece en el audio,
    cuenta cuántas veces aparece en el audio. Si muchos 4-gramas tienen >1
    ocurrencia en el audio, hay multi-take.

    Returns: (multitake_score, total_grams_common)
      multitake_score: 0.0 = sin multi-take, 1.0 = todo multi-take
    """
    def to_ngrams_set(words):
        out = []
        for i in range(len(words) - n + 1):
            text = " ".join(w[0] for w in words[i:i+n])
            out.append(text)
        return out
    from collections import Counter
    v_ngrams = set(to_ngrams_set(words_v))
    a_count = Counter(to_ngrams_set(words_a))
    multi_count = 0
    total = 0
    for ng in v_ngrams:
        if ng in a_count:
            total += 1
            if a_count[ng] > 1:
                multi_count += 1
    if total == 0:
        return 0.0, 0
    return multi_count / total, total


def classify(seg_offsets: list, bd_off: float, multitake_score: float = 0.0):
    """Clasifica el sync pair basado en offsets por segmento + multitake.
    Returns: (status, info_dict)
    """
    if not seg_offsets:
        return "no_data", {"reason": "sin transcripts útiles", "n_segs": 0,
                           "multitake_score": multitake_score}

    # NOTA: NO marcar como "audio equivocado" sólo por pocos anchors.
    # El video puede tener:
    # - Transcript muy corto (B-roll, video de 5s, etc.)
    # - Alucinación de Whisper (palabras repetidas)
    # - Sólo preguntas del entrevistador (sin solapamiento con audio del entrevistado)
    # En todos esos casos el AUDIO puede estar OK aunque haya pocos anchors comunes.
    # Detectar audio equivocado requiere voice-match, no transcript-overlap.
    pass

    n_segs = len(seg_offsets)
    if n_segs < 2:
        return "insufficient", {"reason": "solo 1 segmento", "n_segs": 1,
                                "seg_offset": seg_offsets[0][2],
                                "multitake_score": multitake_score}

    offsets = np.array([s[2] for s in seg_offsets])
    ns = np.array([s[3] for s in seg_offsets])

    median_global = float(np.median(offsets))
    mad_segments = float(np.median(np.abs(offsets - median_global)))
    bias_vs_bd = median_global - bd_off
    total_n = int(ns.sum())

    info = {
        "n_segs": n_segs,
        "median_offset": round(median_global, 3),
        "bias_vs_bd": round(bias_vs_bd, 3),
        "mad_segments": round(mad_segments, 3),
        "total_anchors": total_n,
        "seg_range": (round(float(offsets.min()), 3), round(float(offsets.max()), 3)),
        "multitake_score": round(multitake_score, 3),
    }

    # Multi-take check FIRST (más específico que drift)
    if multitake_score > 0.10:
        info["reason"] = (f"Multi-take detectado: {multitake_score:.0%} de 4-gramas del video "
                          f"aparecen >1 vez en el audio. Sync único NO aplica — "
                          f"editor debe ajustar manualmente.")
        return "multitake", info

    if mad_segments > 0.5:
        info["reason"] = (f"MAD entre segmentos = {mad_segments:.3f}s — "
                          f"sync NO lineal (drift severo, audio equivocado, "
                          f"o multi-take leve)")
        return "drift_severe", info

    if mad_segments > 0.3:
        info["reason"] = (f"MAD entre segmentos = {mad_segments:.3f}s — "
                          f"drift sutil, sync impreciso")
        return "drift_partial", info

    if abs(bias_vs_bd) > 0.150 and mad_segments < 0.15 and total_n >= 100:
        info["reason"] = (f"Bias confiable vs BD = {bias_vs_bd:+.3f}s — "
                          f"refinement seguro (MAD={mad_segments:.3f}, n={total_n})")
        info["refine_safe"] = True
        return "bias_refinable", info

    if abs(bias_vs_bd) > 0.150:
        info["reason"] = (f"Bias vs BD = {bias_vs_bd:+.3f}s pero confianza media "
                          f"(MAD={mad_segments:.3f}) — refinement con cautela")
        return "bias_uncertain", info

    if abs(bias_vs_bd) > 0.050:
        info["reason"] = f"Bias leve = {bias_vs_bd:+.3f}s"
        return "minor_bias", info

    info["reason"] = f"OK: MAD={mad_segments:.3f}, bias={bias_vs_bd:+.3f}s"
    return "ok", info


STATUS_PRIORITY = {
    "audio_mismatch": 0,
    "multitake": 1,
    "drift_severe": 2,
    "bias_refinable": 3,
    "bias_uncertain": 4,
    "drift_partial": 5,
    "no_data": 6,
    "minor_bias": 7,
    "insufficient": 8,
    "ok": 9,
}

STATUS_LABEL = {
    "audio_mismatch":  "❌ AUDIO EQUIVOCADO",
    "multitake":       "🎬 MULTI-TAKE",
    "drift_severe":    "🚨 DRIFT/AUDIO ERR",
    "bias_refinable":  "🔧 REFINABLE",
    "bias_uncertain":  "⚠ BIAS DUDOSO",
    "drift_partial":   "⚠ DRIFT-LEVE",
    "no_data":         "  no-data",
    "minor_bias":      "  minor",
    "insufficient":    "  insuficiente",
    "ok":              "✓ OK",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--seg-sec", type=float, default=60.0)
    ap.add_argument("--n-gram", type=int, default=3)
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--only-pairs", help="CSV de sync_pair ids (debug)")
    ap.add_argument("--apply-refinable", action="store_true",
                    help="Aplica el offset FÍSICO (mediana de segmentos de "
                         "transcript) a los pairs clasificados 🔧 REFINABLE. "
                         "Es la aplicación transcript-based que la doctrina "
                         "(sync-sin-ground-truth §Fase 6) prescribe — a "
                         "diferencia del refine por envelope, no lo engaña "
                         "la música (FCC 2026-07-09). Respeta method='manual'.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"verify_sync_phys_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))
    q = ("SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec, "
         "sp.method, v.filename, ca.rel_path "
         "FROM audio_sync_pairs sp "
         "JOIN clips v ON v.id=sp.video_clip_id "
         "JOIN clips ca ON ca.id=sp.audio_clip_id ")
    if args.only_pairs:
        ids = ",".join(args.only_pairs.split(","))
        q += f"WHERE sp.id IN ({ids}) "
    q += "ORDER BY v.filename, ca.rel_path"
    rows = conn.execute(q).fetchall()
    logging.info(f"Auditando {len(rows)} pairs (seg_sec={args.seg_sec}, n={args.n_gram})\n")

    results = []
    for sp_id, vid, aid, off, method, vfn, arel in rows:
        wv = load_words(tr_dir, vid)
        wa = load_words(tr_dir, aid)
        audio_short = arel.split("Lavas/")[-1] if "Lavas/" in arel else arel.split("/")[-1]
        segs = segment_offsets(wv, wa, off, args.seg_sec, args.n_gram, args.window)
        multi_score, _ = detect_multitake(wv, wa, n=4) if wv and wa else (0.0, 0)
        status, info = classify(segs, off, multi_score)
        results.append({
            "sp_id": sp_id, "video": vfn, "audio": audio_short,
            "bd_off": off, "method": method, "status": status, "info": info,
            "segs": segs,
        })

    # Sort by severity
    results.sort(key=lambda r: (STATUS_PRIORITY[r["status"]], r["video"]))

    logging.info(f"{'sp':<4} {'estado':<22} {'video':<24} {'audio':<26} {'bd_off':>9} {'detalle'}")
    logging.info("-" * 130)
    for r in results:
        info = r["info"]
        label = STATUS_LABEL[r["status"]]
        detail = info.get("reason", "")
        if "median_offset" in info:
            detail = (f"phys={info['median_offset']:+.3f}  bias={info['bias_vs_bd']:+.3f}  "
                      f"MAD={info['mad_segments']:.3f}  n_seg={info['n_segs']}  "
                      f"anchors={info['total_anchors']}  range={info['seg_range']}")
        logging.info(f"{r['sp_id']:<4} {label:<22} {r['video'][:23]:<24} "
                     f"{r['audio'][:25]:<26} {r['bd_off']:>+8.3f}  {detail}")

    # Summary
    from collections import Counter
    c = Counter(r["status"] for r in results)
    logging.info("\n=== RESUMEN ===")
    for st in sorted(c, key=lambda s: STATUS_PRIORITY[s]):
        logging.info(f"  {STATUS_LABEL[st]:<22}: {c[st]} pairs")

    # Aplicación transcript-based de los REFINABLE (opcional)
    if args.apply_refinable:
        n_app = 0
        for r in results:
            if r["status"] != "bias_refinable" or r["method"] == "manual":
                continue
            phys = r["info"].get("median_offset")
            if phys is None:
                continue
            old = r["bd_off"]
            notes, = conn.execute(
                "SELECT COALESCE(notes,'') FROM audio_sync_pairs WHERE id=?",
                (r["sp_id"],)).fetchone()
            conn.execute(
                "UPDATE audio_sync_pairs SET offset_sec=?, notes=? WHERE id=?",
                (phys, notes + f" | fisico-segmentos aplicado {old:+.3f}->"
                 f"{phys:+.3f} (MAD={r['info']['mad_segments']:.3f}, "
                 f"n_seg={r['info']['n_segs']})", r["sp_id"]))
            logging.info(f"  APLICADO sp={r['sp_id']} {r['video']}: "
                         f"{old:+.3f} -> {phys:+.3f}")
            n_app += 1
        conn.commit()
        logging.info(f"\nOffsets físicos aplicados a {n_app} pairs REFINABLE.")

    # Detalle de los problemáticos
    bad = [r for r in results if r["status"] in
           ("audio_mismatch", "multitake", "drift_severe", "bias_refinable",
            "bias_uncertain", "drift_partial")]
    if bad:
        logging.info(f"\n=== {len(bad)} PAIRS QUE NECESITAN ATENCIÓN ===\n")
        for r in bad:
            logging.info(f"sp={r['sp_id']}  {r['video']} ↔ {r['audio']}")
            logging.info(f"  status: {STATUS_LABEL[r['status']]}")
            logging.info(f"  BD offset: {r['bd_off']:+.3f}s")
            logging.info(f"  Físico (mediana segmentos): {r['info'].get('median_offset','?')}s")
            logging.info(f"  Razón: {r['info']['reason']}")
            logging.info(f"  Offsets por segmento:")
            for t0, t1, off, n, mad in r["segs"][:8]:
                logging.info(f"    [{t0:>5.0f}-{t1:>5.0f}]s: off={off:+.3f}  n={n:<4} mad={mad:.3f}")
            logging.info("")

    conn.close()


if __name__ == "__main__":
    main()
