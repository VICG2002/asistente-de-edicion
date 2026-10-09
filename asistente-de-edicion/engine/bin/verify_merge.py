#!/usr/bin/env python3
"""Comprueba que el merge quedo DONDE EL MOTOR DIJO. Garantias, no promesas.

Que verifica
------------
`merge_pool.lua` vuelca el `GetAudioMapping()` de cada clip mergeado. Aqui se
compara el offset que reporta Resolve contra el `offset_sec` del manifest.

Convencion de signos (medida en Resolve 21.0.2 Free, 2026-07-30, con un par
sintetico de offset conocido):

    manifest:  offset = audio_start - video_start      (ej. -5.000 s)
    Resolve :  linked_audio[n].offset en MUESTRAS      (ej. +240000 = +5.000 s)

    => offset_resolve_seg == -offset_manifest_seg

Una desviacion mayor a un frame significa que Resolve ligo el audio en otro
lugar del que el motor calculo. Eso NO se ve a simple vista: el clip parece
sincronizado y el desfase aparece a mitad del corte.

Uso:
    python3 bin/verify_merge.py --root <disco>
    python3 bin/verify_merge.py --root <disco> --fps 23.976 --verbose
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
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


_RE_LINKED = re.compile(
    r'"(\d+)"\s*:\s*\{[^}]*?"offset"\s*:\s*(-?\d+)[^}]*?"path"\s*:\s*"([^"]+)"',
    re.DOTALL)
_RE_LINKED_ALT = re.compile(
    r'"(\d+)"\s*:\s*\{[^}]*?"path"\s*:\s*"([^"]+)"[^}]*?"offset"\s*:\s*(-?\d+)',
    re.DOTALL)
_RE_EMBEDDED = re.compile(r'"embedded_audio_channels"\s*:\s*(\d+)')


def parsear_mapping(m: str):
    """Devuelve (embedded_channels, [(path, offset_muestras), ...])."""
    emb = 0
    me = _RE_EMBEDDED.search(m or "")
    if me:
        emb = int(me.group(1))
    ligados = []
    # el orden de las llaves dentro de linked_audio no esta garantizado
    seccion = m or ""
    i = seccion.find('"linked_audio"')
    if i >= 0:
        seccion = seccion[i:]
        j = seccion.find('"track_mapping"')
        if j > 0:
            seccion = seccion[:j]
    for mo in _RE_LINKED.finditer(seccion):
        ligados.append((mo.group(3), int(mo.group(2))))
    if not ligados:
        for mo in _RE_LINKED_ALT.finditer(seccion):
            ligados.append((mo.group(2), int(mo.group(3))))
    return emb, ligados


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--resultado", default="",
                    help="JSON que dejo merge_pool.lua. Default: el "
                         "<proy>_merge_resultado.json del disco.")
    ap.add_argument("--fps", type=float, default=24.0,
                    help="Frame rate de la timeline, para el margen de 1 frame.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    if args.resultado:
        res_path = Path(args.resultado)
    else:
        candidatos = sorted((root / ".cinema_assistant" / "resolve")
                            .glob("*_merge_resultado.json"))
        if not candidatos:
            print("No encuentro el informe de merge_pool.lua.")
            print("Correr primero, en la Consola de Resolve:")
            print('  MERGE_PLAN = "<disco>/.cinema_assistant/resolve/<proy>_merge.lua"')
            print('  dofile("~/cinema-assistant/resolve/merge_pool.lua")')
            return 1
        res_path = candidatos[-1]

    try:
        informe = json.loads(res_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"Informe ilegible ({res_path}): {e}")

    # offsets esperados desde el manifest
    conn = manifest.conectar(str(db))
    esperado = {}
    sample_rate = {}
    for vpath, apath, off, conf, sr in conn.execute("""
        SELECT cv.path, ca.path, p.offset_sec, p.confidence, ca.audio_sample_rate
        FROM audio_sync_pairs p
        JOIN clips cv ON cv.id = p.video_clip_id
        JOIN clips ca ON ca.id = p.audio_clip_id
    """):
        esperado[(vpath, apath)] = (off or 0.0, conf or 0.0)
        sample_rate[apath] = sr or 48000
    conn.close()

    margen = 1.0 / max(args.fps, 1.0)
    ok = desviados = sin_referencia = sin_ligar = 0
    peor = 0.0
    detalles = []

    for fila in informe:
        vpath = fila.get("video") or ""
        emb, ligados = parsear_mapping(fila.get("mapping") or "")

        if emb <= 0:
            detalles.append(("REGLA", vpath, "",
                             "el clip NO conserva audio de camara — "
                             "RETAIN_EMBEDDED_AUDIO no se aplico"))
            desviados += 1
            continue
        if not ligados:
            sin_ligar += 1
            detalles.append(("SIN LIGAR", vpath, "",
                             "no hay audio ligado en el mapping"))
            continue

        for apath, off_muestras in ligados:
            ref = esperado.get((vpath, apath))
            if ref is None:
                sin_referencia += 1
                detalles.append(("SIN REF", vpath, apath,
                                 "ligado en Resolve pero sin par en el manifest"))
                continue
            off_manifest, conf = ref
            sr = sample_rate.get(apath, 48000) or 48000
            off_resolve = off_muestras / float(sr)
            # signo invertido: ver el docstring
            err = abs(off_resolve - (-off_manifest))
            peor = max(peor, err)
            if err <= margen:
                ok += 1
                if args.verbose:
                    detalles.append(("ok", vpath, apath,
                                     f"manifest {off_manifest:+.3f}s · "
                                     f"Resolve {off_resolve:+.3f}s · "
                                     f"error {err*1000:.0f} ms"))
            else:
                desviados += 1
                detalles.append(("DESVIADO", vpath, apath,
                                 f"manifest {off_manifest:+.3f}s · "
                                 f"Resolve {off_resolve:+.3f}s · "
                                 f"error {err:.3f}s = {err*args.fps:.1f} frames "
                                 f"(conf del par {conf:.2f})"))

    print(f"\nVerificacion del merge — {root.name}")
    print("=" * 66)
    print(f"informe          : {res_path.name}")
    print(f"clips en informe : {len(informe)}")
    print(f"margen           : 1 frame a {args.fps:g} fps = {margen*1000:.0f} ms")
    print("")
    print(f"  ligados correctos : {ok}")
    if desviados:
        print(f"  DESVIADOS         : {desviados}")
    if sin_ligar:
        print(f"  sin ligar         : {sin_ligar}")
    if sin_referencia:
        print(f"  sin par en manifest: {sin_referencia}")
    if ok or desviados:
        print(f"  peor error        : {peor*1000:.0f} ms "
              f"({peor*args.fps:.1f} frames)")

    mostrar = [d for d in detalles if d[0] != "ok"] if not args.verbose else detalles
    if mostrar:
        print("")
        for tipo, vpath, apath, msg in mostrar[:40]:
            nombre = Path(vpath).name if vpath else "?"
            aud = Path(apath).name if apath else ""
            print(f"  [{tipo}] {nombre}" + (f" ← {aud}" if aud else ""))
            print(f"          {msg}")
        if len(mostrar) > 40:
            print(f"  ... y {len(mostrar)-40} mas")

    print("")
    if desviados or sin_ligar:
        print("✗ El merge NO coincide con lo que calculo el motor en todos los casos.")
        print("  Un desfase asi no se ve a simple vista: el clip parece sincronizado")
        print("  y el problema aparece a mitad del corte. Revisar antes de seguir.")
        return 1
    print("✓ Todos los clips mergeados coinciden con el offset del manifest,")
    print("  y todos conservan su audio de camara.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
