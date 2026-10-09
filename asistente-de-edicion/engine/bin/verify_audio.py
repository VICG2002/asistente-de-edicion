#!/usr/bin/env python3
"""Mide el sonido de los exports y lo escribe. No aprueba ni reprueba.

POR QUE NO FALLA CONTRA UN NUMERO
Decision de Victor, 2026-08-19: "solo medir y avisar, sin objetivo". El loudness
al que se entrega una pieza es una decision de autor —no es lo mismo un reel de
Instagram que un corte largo con un concierto dentro— y la firma el editor. Lo
que el motor no puede seguir haciendo es no mirar.

QUE SACO ESTO LA PRIMERA VEZ QUE SE CORRIO (Morsa, 2026-08-19, 5 exports)
    Cut 1.mov               -6.4 LUFS   LRA 17.1   +2.4 dBTP   1,031,972 muestras a 0 dBFS
    Morsa ultimate cut.mov -13.4 LUFS   LRA 20.6   +1.0 dBTP      32,159
    Reel 1 inicio.mov      -21.6 LUFS   LRA 11.9   -4.4 dBFS           1
    Reel 2.mov             -19.7 LUFS   LRA 11.6   -3.6 dBFS           1
    Reel 3.mov             -21.1 LUFS   LRA  6.6   -2.7 dBFS           1
Quince LU entre el primero y el ultimo, y un millon de muestras destruidas en el
corte que ya se habia entregado. Ninguno de esos numeros existia antes.

COMO LEER LA TABLA
  muestras a fondo de escala > 0  -> clipping DURO, ya paso, no se arregla
                                     bajando el fader del master: hay que ir a
                                     las pistas.
  true peak > 0 dBTP              -> distorsionara al comprimir a AAC aunque el
                                     archivo se vea limpio.
  LRA alto                        -> el espectador va a tocar el volumen.
Con --detalle ademas dice DONDE: los tramos altos, los bajos, los saltos de mas
de 6 LU en un segundo y la energia por banda de una ventana de dialogo contra
una de musica.

Uso:
    python3 bin/verify_audio.py --root <disco>
    python3 bin/verify_audio.py --root <disco> --detalle
    python3 bin/verify_audio.py --archivo <export.mov> --detalle
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib import loudness as L                                  # noqa: E402
from lib.guards import assert_selected, report_done            # noqa: E402

EXTS = (".mov", ".mp4", ".mxf", ".wav", ".m4a", ".mkv")


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def duracion(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "0").strip() or 0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0.0


def ficha(path: Path, detalle: bool) -> dict:
    d = {"archivo": path.name, "ruta": str(path), "dur": duracion(path)}
    d.update(L.r128(path))
    d.update(L.astats(path))
    if detalle and d["dur"] > 10:
        serie = L.short_term(path)
        d["p05"], d["p50"], d["p95"] = L.percentiles(serie, .05, .50, .95)
        d["altos"] = L.tramos(serie, lambda v: v > L.UMBRAL_ALTO_LUFS)
        d["bajos"] = L.tramos(serie, lambda v: -70 < v < L.UMBRAL_BAJO_LUFS)
        d["saltos"] = L.saltos(serie)
        # Dos ventanas de 60 s: la mas baja y la mas alta de la pieza. Comparar
        # su espectro dice si la musica se come al dialogo y en que banda.
        if serie:
            baja = min(serie, key=lambda x: x[1] if x[1] > -70 else 999)[0]
            alta = max(serie, key=lambda x: x[1])[0]
            d["banda_baja"] = L.por_banda(path, max(0, baja - 30), 60)
            d["banda_alta"] = L.por_banda(path, max(0, alta - 30), 60)
            d["ventana_baja"], d["ventana_alta"] = baja, alta
    return d


def fmt(v, n=1, suf=""):
    return "—" if v is None else f"{v:.{n}f}{suf}"


def escribir(fichas: list[dict], out: Path, detalle: bool) -> None:
    L_ = []
    hoy = dt.date.today().isoformat()
    L_.append(f"# Sonido de los exports — {hoy}\n")
    L_.append("Medido con `bin/verify_audio.py`. **Sin objetivo declarado**: el motor\n"
              "mide, el numero al que se entrega lo firma el editor.\n")
    L_.append("| Export | dur | LUFS-I | LRA | true peak | muestras a 0 dBFS |")
    L_.append("|---|---:|---:|---:|---:|---:|")
    for f in fichas:
        L_.append(f"| `{f['archivo']}` | {f['dur']/60:.2f} min | "
                  f"{fmt(f['lufs'])} | {fmt(f['lra'],1,' LU')} | "
                  f"{fmt(f['true_peak'],1,' dB')} | {f['muestras_a_fondo']:,} |")
    clip = [f for f in fichas if f["muestras_a_fondo"] > 1]
    if clip:
        L_.append("\n## Clipping duro\n")
        for f in clip:
            L_.append(f"- `{f['archivo']}`: **{f['muestras_a_fondo']:,} muestras** "
                      f"clavadas a fondo de escala, flat factor {fmt(f['flat_factor'])}. "
                      f"Ya esta en el archivo; no se arregla en el master.")
    vals = [f["lufs"] for f in fichas if f["lufs"] is not None]
    if len(vals) > 1:
        L_.append(f"\n**Dispersion entre entregables: {max(vals)-min(vals):.1f} LU** "
                  f"({min(vals):.1f} a {max(vals):.1f} LUFS).\n")
    if detalle:
        for f in fichas:
            if "p50" not in f:
                continue
            L_.append(f"\n## `{f['archivo']}`\n")
            L_.append(f"Short-term p05 {f['p05']:.1f} · mediana {f['p50']:.1f} · "
                      f"p95 {f['p95']:.1f} LUFS")
            if f["altos"]:
                tot = sum(b - a for a, b in f["altos"])
                L_.append(f"\nTramos por encima de {L.UMBRAL_ALTO_LUFS:.0f} LUFS "
                          f"({len(f['altos'])}, {tot:.0f} s):")
                for a, b in f["altos"][:10]:
                    L_.append(f"- {L.mmss(a)} → {L.mmss(b)} ({b-a:.1f} s)")
            if f["bajos"]:
                tot = sum(b - a for a, b in f["bajos"])
                L_.append(f"\nTramos por debajo de {L.UMBRAL_BAJO_LUFS:.0f} LUFS "
                          f"({len(f['bajos'])}, {tot:.0f} s):")
                for a, b in f["bajos"][:10]:
                    L_.append(f"- {L.mmss(a)} → {L.mmss(b)} ({b-a:.1f} s)")
            if f["saltos"]:
                L_.append(f"\nSaltos de {L.SALTO_LU:.0f} LU o mas en un segundo: "
                          + ", ".join(f"{L.mmss(t)} ({d:+.1f} LU)" for t, d in f["saltos"][:12]))
            if f.get("banda_baja"):
                L_.append(f"\n| banda | ventana baja ({L.mmss(f['ventana_baja'])}) "
                          f"| ventana alta ({L.mmss(f['ventana_alta'])}) | Δ |")
                L_.append("|---|---:|---:|---:|")
                for k in f["banda_baja"]:
                    a, b = f["banda_baja"][k], f["banda_alta"][k]
                    dd = f"{b-a:+.1f} dB" if (a is not None and b is not None) else "—"
                    L_.append(f"| {k} | {fmt(a,1,' dB')} | {fmt(b,1,' dB')} | {dd} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L_) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="raiz del proyecto en el disco")
    ap.add_argument("--carpeta", default="Exports",
                    help="subcarpeta a medir (por defecto Exports)")
    ap.add_argument("--archivo", action="append", default=[],
                    help="medir un archivo suelto; se puede repetir")
    ap.add_argument("--detalle", action="store_true",
                    help="ademas, curva short-term, tramos, saltos y bandas")
    ap.add_argument("--out", help="markdown de salida")
    args = ap.parse_args()

    archivos: list[Path] = [Path(a) for a in args.archivo]
    root = None
    if args.root:
        root = resolve_root(args.root)
        d = root / args.carpeta
        if d.is_dir():
            archivos += sorted(p for p in d.iterdir()
                               if p.suffix.lower() in EXTS and not p.name.startswith("._"))
    if not archivos and not args.root:
        sys.exit("Hace falta --root o --archivo")
    assert_selected(archivos, "exports que medir", filters={
        "--root": str(root or ""), "--carpeta": args.carpeta},
        hint="Si los renders viven en otra carpeta, pasala con --carpeta.")

    fichas = []
    for p in archivos:
        print(f"  midiendo {p.name} …", flush=True)
        try:
            fichas.append(ficha(p, args.detalle))
        except RuntimeError as e:
            print(f"  ! {p.name}: {e}", file=sys.stderr)

    if not fichas:
        sys.exit("Ninguna medida salio. Revisa que ffmpeg este instalado.")

    ancho = max(len(f["archivo"]) for f in fichas)
    print()
    for f in fichas:
        aviso = "  <- CLIPPING" if f["muestras_a_fondo"] > 1 else ""
        print(f"  {f['archivo']:<{ancho}}  {fmt(f['lufs']):>7} LUFS  "
              f"LRA {fmt(f['lra']):>5}  pico {fmt(f['true_peak']):>5} dB  "
              f"{f['muestras_a_fondo']:>9,} a 0 dBFS{aviso}")
    vals = [f["lufs"] for f in fichas if f["lufs"] is not None]
    if len(vals) > 1:
        print(f"\n  dispersion entre entregables: {max(vals)-min(vals):.1f} LU")

    out = Path(args.out) if args.out else (
        (root / ".cinema_assistant/reports" / f"audio-{dt.date.today():%Y%m%d}.md")
        if root else Path(f"audio-{dt.date.today():%Y%m%d}.md"))
    escribir(fichas, out, args.detalle)
    print(f"\n  informe: {out}")
    report_done("verify_audio", exports=len(fichas),
                con_clipping=sum(1 for f in fichas if f["muestras_a_fondo"] > 1))


if __name__ == "__main__":
    main()
