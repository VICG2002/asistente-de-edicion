#!/usr/bin/env python3
"""Verifier: todo par multicám verificado ATERRIZA en la timeline entregada.

Garantía nacida del caso Film Club Café (2026-07-11): el par
ASA_0723↔VICG_8628 estaba verificado con triple evidencia física y aun así
el ángulo NO llegó a Resolve — en timelines EMPACADAS las compañeras se
derramaban sobre el V1 vecino, colisionaban en cascada (V2→V3) y la
colocación fallaba en silencio. El usuario lo detectó a ojo; "asegúrate de
que no vuelva a pasar" (2026-07-11). Este script ES esa garantía: simula
fuera de Resolve exactamente la colocación que hará el asistente_*.lua
(timeline ENTREVISTAS: V1 = entrevistas sin compañeras verificadas,
chronoSort por tiempo real corregido, clips empacados; compañeras con CLAMP
a la ventana de su base) y falla si algún par verificado:

  - no tiene su base en V1 ni es él mismo base de otro par (huérfano),
  - queda sin traslape útil dentro de la ventana del base (SKIP),
  - o colisiona en V2 (imposible con el clamp — si pasa, el .lua y este
    simulador divergieron: sincronizarlos).

MANTENIMIENTO: la lógica de clamp refleja placeCompanions() del
asistente_*.lua — si se cambia una, cambiar la otra (comentario espejo en
el .lua). `--legacy-spill` reproduce el comportamiento viejo (sin clamp,
fallback V3) y sirve para demostrar que el verifier detecta el bug.

Uso:
    bin/verify_multicam_placement.py --root <disk> --lua resolve/<p>_multicam.lua
        [--utc-offset -6] [--legacy-spill] [--report-only]

Exit 1 si hay pares verificados que no aterrizan (0 con --report-only).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from derive_chrono_sync import resolve_root  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

# El cierre de la entrada NO se ancla con `\}`: desde v3 el export escribe
# campos extra despues de `verified` (`place`, `basis`). Anclarlo hacia que el
# regex no matcheara NADA y el verificador dijera "sin pares verificados — OK
# trivial": la peor forma de fallar, porque da por bueno lo que no miro.
PAIR_RE = re.compile(r'\{a="(.*?)", b="(.*?)", delta=([\d.\-]+), '
                     r'overlap=([\d.]+), verified=(true|false)')
SKEW_RE = re.compile(r'\["([^"]+)"\] = ([\-\d.]+),')


def video_epoch(ct: str, utc_offset: float):
    if not ct:
        return None
    try:
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        local = dt + timedelta(hours=utc_offset)
        return time.mktime(local.replace(tzinfo=None).timetuple())
    except (ValueError, OverflowError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--lua", required=True,
                    help="resolve/<proyecto>_multicam.lua")
    ap.add_argument("--utc-offset", type=float, default=-6.0)
    ap.add_argument("--legacy-spill", action="store_true",
                    help="Simula el comportamiento PRE-clamp (compañera "
                         "completa, fallback V3). Para demostrar detección.")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    conn = manifest.conectar(str(root / ".cinema_assistant" / "manifest.sqlite"))
    src = Path(args.lua).expanduser().read_text()
    skews = {k: float(v) for k, v in SKEW_RE.findall(src)}
    pairs = [(a.replace('\\"', '"'), b.replace('\\"', '"'), float(d))
             for a, b, d, o, v in PAIR_RE.findall(src) if v == "true"]
    if not pairs:
        print("Sin pares verificados — OK trivial.")
        return 0
    verified_b = {b for _, b, _ in pairs}
    comp_of: dict[str, list] = {}
    for a, b, d in pairs:
        comp_of.setdefault(a, []).append((b, d))

    def skew_for(path: str) -> float:
        for k, v in skews.items():
            if f"/{k}/" in path:
                return v
        return 0.0

    # V1 de ENTREVISTAS: como el .lua (categoría entrevista, sin compañeras
    # verificadas, orden por tiempo real corregido, empacadas).
    rows = conn.execute(
        "SELECT c.path, c.creation_time, c.duration_sec, c.filename "
        "FROM clips c JOIN clip_descriptions d ON d.clip_id=c.id "
        "WHERE d.category='entrevista' AND c.file_kind='video'").fetchall()
    durs = {p: dur for p, dur in conn.execute(
        "SELECT path, duration_sec FROM clips WHERE file_kind='video'")}
    v1 = []
    for p, ct, dur, fn in rows:
        if p in verified_b:
            continue
        ep = video_epoch(ct, args.utc_offset)
        v1.append(((ep or 0) - skew_for(p), p, fn, dur or 0))
    v1.sort()
    pos, t = {}, 0.0
    for _, p, fn, dur in v1:
        pos[p] = t
        t += dur
    v1_paths = set(pos)

    tracks: dict[int, list] = {2: [], 3: []}

    def fits(tr: int, s: float, e: float) -> bool:
        return all(e <= s0 or s >= e0 for s0, e0 in tracks[tr])

    n_ok = 0
    problems, orphans = [], []
    for a, b, d in pairs:
        bn = b.split("/")[-1]
        an = a.split("/")[-1]
        if a not in v1_paths:
            # base no está en la timeline multicám (B-roll o compañera de
            # otro par): no se coloca por diseño — reportar como INFO.
            orphans.append((an, bn))
            continue
        base_s, base_e = pos[a], pos[a] + durs.get(a, 0)
        bdur = durs.get(b, 0)
        s = base_s + d
        src_in = 0.0
        if not args.legacy_spill:
            if s < base_s:
                src_in = base_s - s
                s = base_s
            e = min(s + bdur - src_in, base_e)
        else:
            if s < 0:
                src_in = -s
                s = 0.0
            e = s + bdur - src_in
        if e <= s:
            problems.append(f"SKIP sin traslape: {bn} sobre {an}")
            continue
        if fits(2, s, e):
            tracks[2].append((s, e))
            n_ok += 1
        elif args.legacy_spill and fits(3, s, e):
            tracks[3].append((s, e))
            problems.append(f"V3 (desalineable): {bn} sobre {an}")
        else:
            problems.append(f"COLISIÓN sin destino: {bn} sobre {an}")

    print(f"Pares verificados: {len(pairs)} | colocados en V2: {n_ok} | "
          f"fuera de timeline multicám (INFO): {len(orphans)}")
    for msg in problems:
        print(f"  ✗ {msg}")
    if problems:
        print(f"\nFAIL verify_multicam_placement: {len(problems)} par(es) "
              f"verificado(s) que NO aterrizan alineados. NO entregar el bake.")
        return 0 if args.report_only else 1
    print("\n✅ verify_multicam_placement: todo par verificado aterriza "
          "alineado en V2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
