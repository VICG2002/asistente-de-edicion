#!/usr/bin/env python3
"""Convierte los silencios medidos en los tramos que bajan a la timeline.

POR QUE EXISTE
En un rodaje con prompter el locutor se para a esperar a que avance el texto.
Con quince tomas del mismo parlamento delante, el editor pasa la tarde
esperando junto con el locutor. Quitando esas esperas las tomas quedan
comparables de un vistazo, que es justo lo que se necesita para elegir.

ESTO NO SIEMPRE APLICA — y por eso es un paso APARTE y opt-in. En una entrevista
la pausa es contenido: es donde el entrevistado piensa, se emociona o cambia de
idea, y el asistente la marca (marker Sand) precisamente para que el editor la
vea, no para quitarla. Aqui es al reves porque el material es distinto.

QUE HACE
Lee `clip_silences` (lo mide `detect_pauses.py` con ffmpeg) y escribe
`clip_keep_ranges`: la lista ordenada de tramos que se conservan de cada clip.
De cada silencio deja `--aire` segundos a cada lado, del lado del habla, para
que el corte no se coma el ataque de la palabra siguiente.

NADA DESTRUCTIVO. Los tramos son coordenadas. Las fuentes no se tocan y
re-hornear sin `CORTAR_SILENCIOS` devuelve las tomas enteras.

GARANTIA
La da `bin/verify_cortes.py`: vuelve al audio y re-mide el nivel DENTRO de cada
tramo que se va a quitar, con una medida distinta (RMS de la ventana) de la que
decidio el corte (deteccion por ventana movil). Si en un tramo hay voz, el RMS
lo delata y el verificador sale con 1.

Uso:
    python3 bin/derive_silence_cuts.py --root <disco>
    python3 bin/derive_silence_cuts.py --root <disco> --aire 0.4 --min-silencio 2.0
    python3 bin/derive_silence_cuts.py --root <disco> --dry-run
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cortes, manifest                                # noqa: E402
from lib.guards import assert_selected, exigir_tablas, report_done  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_keep_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    idx INTEGER,             -- orden dentro del clip, desde 1
    start_sec REAL,
    end_sec REAL,
    motivo TEXT,
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_keep_clip ON clip_keep_ranges(clip_id);
CREATE TABLE IF NOT EXISTS clip_cut_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    idx INTEGER,
    start_sec REAL,
    end_sec REAL,
    threshold_db REAL,       -- el umbral con que se midio ese silencio
    created_at REAL,
    FOREIGN KEY (clip_id) REFERENCES clips(id)
);
CREATE INDEX IF NOT EXISTS idx_cut_clip ON clip_cut_ranges(clip_id);
"""


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--aire", type=float, default=cortes.AIRE,
                    help=f"Colchon en segundos a cada lado del corte "
                         f"(default {cortes.AIRE}). Subirlo = cortes mas suaves "
                         f"y menos tiempo ahorrado.")
    ap.add_argument("--min-silencio", type=float, default=cortes.MIN_SILENCIO,
                    help=f"Silencio minimo que merece un corte, en segundos "
                         f"(default {cortes.MIN_SILENCIO}).")
    ap.add_argument("--min-quitar", type=float, default=cortes.MIN_QUITAR,
                    help=f"Si tras el aire queda menos que esto por quitar, no "
                         f"se corta (default {cortes.MIN_QUITAR}).")
    ap.add_argument("--min-tramo", type=float, default=cortes.MIN_TRAMO,
                    help=f"Tramo conservado minimo (default {cortes.MIN_TRAMO}).")
    ap.add_argument("--sin-entradas", action="store_true",
                    help="No aplicar el `entrar_desde_sec` de derive_takes "
                         "(la charla de rodaje previa a la toma buena).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    ca = root / ".cinema_assistant"
    db = ca / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    exigir_tablas(conn, {
        "clip_silences": "python3 bin/detect_pauses.py --root <disco> "
                         "--seleccion takes",
    }, "derive_silence_cuts")
    conn.executescript(SCHEMA)

    sil_por_clip: dict[int, list] = defaultdict(list)
    umbral_de: dict[int, float] = {}
    for cid, s, e, th in conn.execute(
            "SELECT clip_id, start_sec, end_sec, threshold_db FROM clip_silences "
            "ORDER BY clip_id, start_sec"):
        sil_por_clip[cid].append((float(s or 0), float(e or 0)))
        umbral_de[cid] = float(th if th is not None else 0)
    assert_selected(sil_por_clip, "clips con silencios medidos", filters={
        "--root": str(root),
    }, hint="Corre antes detect_pauses.py. En una pieza con guion, con "
            "--seleccion takes.")

    entradas: dict[int, float] = {}
    if not args.sin_entradas:
        try:
            for cid, t in conn.execute(
                    "SELECT clip_id, entrar_desde_sec FROM clip_takes "
                    "WHERE entrar_desde_sec IS NOT NULL"):
                entradas[cid] = float(t)
        except sqlite3.OperationalError:
            pass   # proyecto sin derive_takes

    duraciones = {cid: float(d or 0) for cid, d in conn.execute(
        "SELECT id, duration_sec FROM clips WHERE file_kind='video'")}
    nombres = {cid: fn for cid, fn in conn.execute(
        "SELECT id, filename FROM clips WHERE file_kind='video'")}

    planes, n_tramos, n_cortes = {}, 0, 0
    quitado_total = original_total = 0.0
    for cid in sorted(set(sil_por_clip) | set(entradas)):
        dur = duraciones.get(cid, 0.0)
        if dur <= 0:
            continue
        p = cortes.plan_de_corte(
            dur, sil_por_clip.get(cid, []),
            aire=args.aire, min_silencio=args.min_silencio,
            min_quitar=args.min_quitar, min_tramo=args.min_tramo,
            entrar_desde=entradas.get(cid))
        if not p["quitar"] and p["entrada"] <= 0:
            continue          # nada que cortar: el clip baja entero
        planes[cid] = p
        n_tramos += len(p["conservar"])
        n_cortes += len(p["quitar"])
        quitado_total += p["quitado_sec"]
        original_total += dur

    if args.dry_run:
        print(f"[dry-run] {len(planes)} clips con corte: {n_cortes} cortes -> "
              f"{n_tramos} tramos. Se quitan {quitado_total:.0f}s de "
              f"{original_total:.0f}s ({100*quitado_total/max(1, original_total):.0f}%).")
        return 0

    conn.execute("DELETE FROM clip_keep_ranges")  # lint:ok delete-global
    conn.execute("DELETE FROM clip_cut_ranges")   # lint:ok delete-global
    ahora = time.time()
    for cid, p in planes.items():
        for i, (a, b) in enumerate(p["conservar"], 1):
            motivo = ("entra despues de la charla de rodaje"
                      if i == 1 and p["entrada"] > 0 else
                      "tramo con habla entre dos esperas de prompter")
            conn.execute(
                "INSERT INTO clip_keep_ranges (clip_id, idx, start_sec, end_sec,"
                " motivo, created_at) VALUES (?,?,?,?,?,?)",
                (cid, i, a, b, motivo, ahora))
        for i, (a, b) in enumerate(p["quitar"], 1):
            conn.execute(
                "INSERT INTO clip_cut_ranges (clip_id, idx, start_sec, end_sec,"
                " threshold_db, created_at) VALUES (?,?,?,?,?,?)",
                (cid, i, a, b, umbral_de.get(cid), ahora))
    conn.commit()

    rep_dir = ca / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep = rep_dir / f"cortes-{time.strftime('%Y%m%d')}.md"
    with rep.open("w", encoding="utf-8") as f:
        f.write("# Corte de silencios\n\n")
        f.write(f"**{len(planes)} clips** con corte · {n_cortes} cortes · "
                f"{n_tramos} tramos conservados.\n\n")
        f.write(f"Se quitan **{quitado_total:.0f}s de {original_total:.0f}s** "
                f"({100*quitado_total/max(1, original_total):.0f}%) en esos "
                f"clips.\n\n")
        f.write(f"Parámetros: aire {args.aire}s a cada lado · silencio mínimo "
                f"{args.min_silencio}s · no se corta por menos de "
                f"{args.min_quitar}s.\n\n")
        f.write("El corte es **reversible y no toca las fuentes**: son "
                "coordenadas de timeline. Sin `CORTAR_SILENCIOS = true` en la "
                "Consola, las tomas bajan enteras.\n\n")
        f.write("| Clip | Dur | Cortes | Se quita | Queda | Tramos |\n")
        f.write("|---|---:|---:|---:|---:|---|\n")
        for cid, p in sorted(planes.items(),
                             key=lambda kv: -kv[1]["quitado_sec"]):
            dur = duraciones.get(cid, 0.0)
            q = p["quitado_sec"]
            tramos = " ".join(f"{a:.1f}–{b:.1f}" for a, b in p["conservar"][:6])
            if len(p["conservar"]) > 6:
                tramos += " …"
            f.write(f"| `{nombres.get(cid, cid)}` | {dur:.0f}s | "
                    f"{len(p['quitar'])} | {q:.1f}s | {dur - q:.0f}s | "
                    f"{tramos} |\n")
        f.write("\n## Sobre la medida\n\n")
        f.write("Los silencios los mide `ffmpeg silencedetect` sobre el audio, "
                "con el umbral calibrado por clip a partir de su RMS. **No se "
                "usan los tiempos de palabra de Whisper**: están medidos y "
                "derivan — en C1178 declaraban un hueco en 29–31 s donde hay "
                "voz y no veían el de 37–40 s que sí existe.\n\n")
        f.write("La garantía la da `bin/verify_cortes.py`, que vuelve al audio "
                "y re-mide el nivel dentro de cada tramo que se quita.\n")

    print(f"\nCorte de silencios — {root.name}")
    print("=" * 66)
    print(f"  clips con corte : {len(planes)}")
    print(f"  cortes          : {n_cortes}  ->  {n_tramos} tramos")
    print(f"  se quita        : {quitado_total:.0f}s de {original_total:.0f}s "
          f"({100*quitado_total/max(1, original_total):.0f}%)")
    print(f"\n  informe: {rep}")
    print("  Siguiente: python3 bin/verify_cortes.py --root <disco>")
    report_done("derive_silence_cuts", clips=len(planes), cortes=n_cortes,
                tramos=n_tramos, quitado_sec=round(quitado_total, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
