#!/usr/bin/env python3
"""Verificación DEFINITIVA de deltas multicámara (MAB 2026-06-12, iter3).

Las ventanas cortas (45s) con música de plaza periódica producen picos en el
beat equivocado que incluso "coinciden" entre dos ventanas. Este verificador
re-mide cada delta con tres defensas:

  1. VENTANA LARGA: correlación de envelope (banda voz, hop 10ms) sobre TODO
     el traslape (hasta 150s). La música periódica se promedia; el patrón de
     habla es aperiódico y domina el pico global.
  2. ETAPA FINA: alrededor del pico grueso, cross-correlación de PCM crudo
     (banda voz, 4kHz) en ±250ms → precisión de milisegundos.
  3. TRIANGULACIÓN: si ambos clips tienen offset al MISMO WAV en el manifest,
     delta_chain = off_a − off_b debe coincidir con el delta directo (±0.25s).
     Acuerdo de dos rutas independientes = verificado. Sin cadena disponible,
     se exige prominence ≥ --solo-prominence en la ruta directa.

Reescribe el .lua de multicam con los deltas re-medidos y verified
actualizado. Los pares que fallan quedan verified=false (el Lua no los
coloca) con la razón en el reporte.

Uso:
    bin/verify_multicam_deltas.py --root <disk> --lua resolve/mab_multicam.lua
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.sync_refiner import _load_pcm, _envelope, WORK_SR
from lib import manifest  # noqa: E402

HOP_MS = 10
HOP_SR = 1000 // HOP_MS  # 100 muestras de envelope por segundo


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def xcorr_best(a: np.ndarray, b: np.ndarray):
    """Lag (muestras de b sobre a) que maximiza correlación. FFT, full mode.

    Returns (lag, peak, prominence) donde prominence compara el pico contra
    el mejor pico FUERA de ±300ms a su alrededor.
    """
    if a.size < 50 or b.size < 50:
        return None
    n = a.size + b.size - 1
    nfft = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    cc = np.fft.irfft(fa * np.conj(fb), nfft)[:n]
    # normalización aproximada
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
    cc = cc / denom
    ipeak = int(np.argmax(cc))
    peak = float(cc[ipeak])
    guard = int(0.3 * HOP_SR)  # ±300ms alrededor del pico
    mask = np.ones(n, dtype=bool)
    mask[max(0, ipeak - guard):ipeak + guard + 1] = False
    runner = float(cc[mask].max()) if mask.any() else 0.0
    prom = (peak - runner) / (abs(peak) + 1e-9)
    # lag en muestras: cc índice k corresponde a desplazamiento (k) con wrap
    lag = ipeak if ipeak < b.size else ipeak - n
    return lag, peak, prom


def measure_delta(a_path: Path, b_path: Path, a_dur: float, b_dur: float,
                  delta_seed: float, search: float = 3.0):
    """Delta re-medido (b empieza delta s después de a) con ventana larga +
    etapa fina. Returns (delta, prom_coarse, fine_ok) o None."""
    # región de traslape en tiempo local de a
    a0 = max(0.0, delta_seed)
    a1 = min(a_dur, delta_seed + b_dur)
    L = min(a1 - a0, 150.0)
    if L < 8.0:
        return None
    a0 = a0 + (a1 - a0 - L) / 2  # centrar
    b0 = a0 - delta_seed  # mismo instante en tiempo local de b
    # margen de búsqueda en b
    bs = max(0.0, b0 - search)
    blen = min(b_dur - bs, L + 2 * search)
    if blen < L * 0.6:
        return None
    a_pcm = _load_pcm(a_path, a0, L)
    b_pcm = _load_pcm(b_path, bs, blen)
    if a_pcm is None or b_pcm is None:
        return None
    a_env = _envelope(a_pcm, sr=WORK_SR, hop_ms=HOP_MS)
    b_env = _envelope(b_pcm, sr=WORK_SR, hop_ms=HOP_MS)
    r = xcorr_best(b_env, a_env)
    if r is None:
        return None
    lag, peak, prom = r
    # b_env[lag:] alinea con a_env[0:] → el instante a0 de a cae en
    # tiempo local de b en bs + lag/HOP_SR
    b_at_a0 = bs + lag / HOP_SR
    delta_coarse = a0 - b_at_a0 + 0.0  # b_local = a_local - delta
    # delta = a_local − b_local del MISMO instante
    delta_coarse = a0 - b_at_a0
    # --- etapa fina: PCM 4kHz crudo ±250ms en el segmento más enérgico ---
    seg = 12.0
    if L > seg:
        # ventana de mayor energía del envelope de a
        win = int(seg * HOP_SR)
        e2 = np.convolve(a_env ** 2, np.ones(win), mode="valid")
        off = int(np.argmax(e2)) / HOP_SR
    else:
        off, seg = 0.0, L
    fa0 = a0 + off
    fb0 = fa0 - delta_coarse
    fine_ok = False
    if fb0 - 0.3 >= 0 and fb0 + seg + 0.3 <= b_dur:
        ap = _load_pcm(a_path, fa0, seg)
        bp = _load_pcm(b_path, fb0 - 0.3, seg + 0.6)
        if ap is not None and bp is not None:
            n = ap.size + bp.size - 1
            nfft = 1 << (n - 1).bit_length()
            cc = np.fft.irfft(np.fft.rfft(bp, nfft) * np.conj(np.fft.rfft(ap, nfft)), nfft)[:n]
            k = int(np.argmax(cc))
            lagf = k if k < ap.size else k - n
            # b_at_fa0 = (fb0 - 0.3) + lagf/WORK_SR = fb0 + shift
            # delta = fa0 - b_at_fa0 = delta_coarse - shift  (¡signo!)
            shift = lagf / WORK_SR - 0.3
            if abs(shift) <= 0.28:
                delta_coarse -= shift
                fine_ok = True
    return delta_coarse, prom, fine_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--lua", required=True)
    ap.add_argument("--max-correction", type=float, default=3.0)
    ap.add_argument("--chain-tolerance", type=float, default=0.25)
    ap.add_argument("--solo-prominence", type=float, default=0.45,
                    help="prominence mínima cuando NO hay cadena WAV.")
    ap.add_argument("--repair-chain", action="store_true",
                    help="Si directa y cadena difieren, re-medir AMBAS patas "
                         "video<->WAV con ventana larga y recalcular la cadena. "
                         "Si converge, verifica el par Y CORRIGE los offsets "
                         "en el manifest (arregla también el delay del lava).")
    ap.add_argument("--physical-bridge", action="store_true",
                    help="Regla del usuario (FCC 2026-07-11): si AMBOS videos "
                         "tienen pata FÍSICA al mismo WAV, el puente de reloj "
                         "da el delta a ±ms; la 'misma escena' se prueba por "
                         "4-gramas de transcript compartidos (>= --min-4gram). "
                         "Verifica pares donde el envelope A1<->A1 es débil. "
                         "Directa fuerte contradictoria (prom>=0.5, >0.3s) veta.")
    ap.add_argument("--bridge-tolerance", type=float, default=0.10,
                    help="Spread máximo (s) entre puentes múltiples.")
    ap.add_argument("--min-4gram", type=float, default=0.05,
                    help="Overlap mínimo de 4-gramas para 'misma escena'.")
    ap.add_argument("--rescue-unverified", action="store_true",
                    help="Procesa también pares verified=false (rescate con "
                         "ventana larga + triangulación). Sin este flag solo "
                         "se re-verifican los true — una corrida previa sin "
                         "--repair-chain los deja irrecuperables (FCC "
                         "2026-07-09).")
    args = ap.parse_args()

    root = resolve_root(args.root)
    conn = manifest.conectar(str(root / ".cinema_assistant" / "manifest.sqlite"))
    clips = {p: (cid, dur) for cid, p, dur in conn.execute(
        "SELECT id, path, duration_sec FROM clips WHERE file_kind='video'")}
    media_by_id = {cid: (p, dur) for cid, p, dur in conn.execute(
        "SELECT id, path, duration_sec FROM clips WHERE index_status='ok'")}
    offs = {}
    offs_meta = {}  # (vid, aid) -> pata medida físicamente
    for vid, aid, off, method, notes in conn.execute(
            "SELECT video_clip_id, audio_clip_id, offset_sec, method, "
            "COALESCE(notes,'') FROM audio_sync_pairs"):
        offs.setdefault(vid, {})[aid] = off
        ml, nl = (method or "").lower(), notes.lower()
        offs_meta[(vid, aid)] = (
            any(h in ml for h in ("longwin", "transcript", "manual",
                                  "lavalier-derived"))
            or "fisico" in nl or "consistencia dual-lav" in nl)

    tr_dir = root / ".cinema_assistant" / "transcripts"
    _gcache: dict[int, set] = {}

    def fourgrams(cid: int) -> set:
        if cid not in _gcache:
            try:
                t = json.loads((tr_dir / f"{cid}.json").read_text())
                w = [x[0] if isinstance(x, (list, tuple)) else x.get("w", "")
                     for x in (t.get("words") or [])]
                _gcache[cid] = {" ".join(w[i:i + 4]).lower()
                                for i in range(len(w) - 3)}
            except Exception:
                _gcache[cid] = set()
        return _gcache[cid]

    lua_path = Path(args.lua).expanduser()
    src = lua_path.read_text()
    pat = re.compile(r'\{a="(.*?)", b="(.*?)", delta=([\d.\-]+), '
                     r'overlap=([\d.]+), verified=(true|false)\}')

    n_ok = n_fail = n_kept_false = 0
    report = []

    def fix(m):
        nonlocal n_ok, n_fail, n_kept_false
        a, b = m.group(1).replace('\\"', '"'), m.group(2).replace('\\"', '"')
        delta, overlap, ver = float(m.group(3)), float(m.group(4)), m.group(5)
        if ver != "true" and not args.rescue_unverified:
            n_kept_false += 1
            return m.group(0)
        an, bn = a.split("/")[-1], b.split("/")[-1]
        (aid_a, dur_a), (aid_b, dur_b) = clips[a], clips[b]
        r = measure_delta(Path(a), Path(b), dur_a, dur_b, delta)
        verdict, why = False, "sin pico"
        new_delta = delta
        if r is not None:
            nd, prom, fine = r
            if abs(nd - delta) > args.max_correction:
                why = f"salto {nd - delta:+.2f}s > max"
            else:
                # triangulación con cadena WAV
                chain = None
                for aud, oa in offs.get(aid_a, {}).items():
                    ob = offs.get(aid_b, {}).get(aud)
                    if ob is not None:
                        chain = oa - ob
                        break
                if chain is not None:
                    if abs(nd - chain) <= args.chain_tolerance:
                        verdict, why = True, (f"directa+cadena coinciden "
                                              f"({nd - chain:+.2f}s), prom={prom:.2f}, "
                                              f"fine={'sí' if fine else 'no'}")
                        new_delta = nd
                    elif args.repair_chain:
                        # Reparar la cadena: re-medir ambas patas vs el WAV
                        # compartido con ventana larga + etapa fina.
                        wav_aid = None
                        for aud, oa in offs.get(aid_a, {}).items():
                            if offs.get(aid_b, {}).get(aud) is not None:
                                wav_aid = aud
                                break
                        wp, wdur = media_by_id[wav_aid]
                        ra = measure_delta(Path(a), Path(wp), dur_a, wdur,
                                           offs[aid_a][wav_aid])
                        rb = measure_delta(Path(b), Path(wp), dur_b, wdur,
                                           offs[aid_b][wav_aid])
                        if ra and rb and ra[1] >= 0.5 and rb[1] >= 0.35:
                            chain2 = ra[0] - rb[0]
                            if abs(nd - chain2) <= args.chain_tolerance:
                                verdict = True
                                new_delta = nd
                                why = (f"cadena REPARADA: off_a {offs[aid_a][wav_aid]:+.2f}→{ra[0]:+.2f} "
                                       f"(prom {ra[1]:.2f}), off_b {offs[aid_b][wav_aid]:+.2f}→{rb[0]:+.2f} "
                                       f"(prom {rb[1]:.2f}); directa-cadena2 {nd - chain2:+.2f}s")
                                # corregir el manifest (arregla el delay del lava)
                                for vid_id, newoff in ((aid_a, ra[0]), (aid_b, rb[0])):
                                    old = offs[vid_id][wav_aid]
                                    if abs(newoff - old) > 0.03:
                                        conn.execute(
                                            "UPDATE audio_sync_pairs SET offset_sec=?, "
                                            "method=method || '-longwin', "
                                            "notes=IFNULL(notes,'') || ?, confidence=0.95 "
                                            "WHERE video_clip_id=? AND audio_clip_id=? "
                                            "AND method NOT LIKE '%-longwin'",
                                            (newoff, f" [longwin {old:+.2f}->{newoff:+.2f}]",
                                             vid_id, wav_aid))
                                        offs[vid_id][wav_aid] = newoff
                            else:
                                why = (f"cadena reparada {chain2:+.2f} sigue lejos de "
                                       f"directa {nd:+.2f} (proms {ra[1]:.2f}/{rb[1]:.2f})")
                        else:
                            pa = f"{ra[1]:.2f}" if ra else "None"
                            pb = f"{rb[1]:.2f}" if rb else "None"
                            why = f"patas WAV débiles (prom a={pa}, b={pb})"
                    else:
                        why = f"directa {nd:+.2f} vs cadena {chain:+.2f} difieren"
                elif prom >= args.solo_prominence:
                    verdict, why = True, f"sin cadena; prom={prom:.2f} fine={'sí' if fine else 'no'}"
                    new_delta = nd
                else:
                    why = f"sin cadena y prom {prom:.2f} < {args.solo_prominence}"
        # Regla del puente físico-físico (teoría del usuario, FCC 2026-07-11):
        # el reloj identifica, el audio clava — con ambas patas al mismo WAV
        # medidas físicamente, el delta multicám sale a ±ms sin depender del
        # envelope A1<->A1; la misma-escena se prueba por contenido.
        if not verdict and args.physical_bridge:
            bridges = []
            for w_aid in set(offs.get(aid_a, {})) & set(offs.get(aid_b, {})):
                if (offs_meta.get((aid_a, w_aid))
                        and offs_meta.get((aid_b, w_aid))):
                    bridges.append(offs[aid_a][w_aid] - offs[aid_b][w_aid])
            if bridges and (max(bridges) - min(bridges)) <= args.bridge_tolerance:
                bd = sorted(bridges)[len(bridges) // 2]
                ga, gb = fourgrams(aid_a), fourgrams(aid_b)
                ov = (len(ga & gb) / max(1, min(len(ga), len(gb)))
                      if ga and gb else 0.0)
                if ov < args.min_4gram:
                    why += (f"; puente físico {bd:+.2f} preciso pero sin "
                            f"escena compartida (4gram {ov:.0%})")
                elif r is not None and prom >= 0.5 and abs(nd - bd) > 0.3:
                    why = (f"puente físico {bd:+.2f} contradice directa "
                           f"fuerte {nd:+.2f} (prom {prom:.2f}) — no colocar")
                else:
                    verdict, new_delta = True, bd
                    why = (f"PUENTE físico-físico {bd:+.2f} (n={len(bridges)}, "
                           f"spread {(max(bridges) - min(bridges)) * 1000:.0f}ms)"
                           f" + escena por transcript ({ov:.0%})")
        if verdict:
            n_ok += 1
        else:
            n_fail += 1
        report.append((an, bn, delta, new_delta, verdict, why))
        return (f'{{a="{m.group(1)}", b="{m.group(2)}", delta={new_delta:.3f}, '
                f'overlap={overlap:.1f}, verified={"true" if verdict else "false"}}}')

    new_src = pat.sub(fix, src)
    lua_path.write_text(new_src)
    conn.commit()

    print(f"Re-verificados: {n_ok} OK, {n_fail} degradados a no-colocar, "
          f"{n_kept_false} ya estaban sin verificar")
    for an, bn, old, new, ok, why in report:
        tag = "✓" if ok else "✗"
        print(f"  {tag} {an}+{bn}: {old:+.2f} → {new:+.2f} (Δ{(new - old) * 1000:+.0f}ms) — {why}")


if __name__ == "__main__":
    main()
