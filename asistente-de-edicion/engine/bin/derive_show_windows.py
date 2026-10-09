#!/usr/bin/env python3
"""Mide en que tramos del dia el audio esta dominado por musica/show fuerte.

Para que sirve
--------------
La multicamara solo coloca angulos confirmados por contenido (envelope A1<->A1),
porque cuando dos camaras ruedan escenas DISTINTAS a la misma hora, alinear por
reloj pone una copia sobre la escena equivocada — el caso "luchador cortado por
el ciclista" (MAB 2026-06-12).

Pero esa regla es demasiado estricta durante un concierto: ahi TODAS las camaras
apuntan al escenario, no hay escenas distintas que confundir, y exigir
confirmacion por contenido deja al editor sin segundo angulo justo donde mas lo
necesita. En Morsa eran 16 pares colocados de 136.

Este script mide donde ocurre eso, en vez de adivinarlo. Recorre las cadenas de
audio continuo, saca el nivel por segundo y marca los tramos sostenidos por
encima del umbral. `bin/export_multicam_lua.py --trust-clock-in-windows` los lee
y ahi, y solo ahi, acepta la alineacion por reloj.

El umbral es RELATIVO al propio dia, no un absoluto: el mismo concierto grabado
con otra ganancia daria otros dB y un numero fijo no viajaria de un proyecto al
siguiente. Se calcula con Otsu aplicado DOS veces — ver `umbral_show()` para por
que una sola pasada no basta.

Medido en Morsa (2026-08-02), que es de donde salen los defaults:

    umbral      -11.6 dB   (Otsu doble; el percentil 55 daba -14.2 y colaba las
                            entrevistas, que van a -16..-19 dB)
    5 ventanas  73 min de show entre 12:58 y 14:29, todas a -9.2..-9.9 dB
    control     el concierto arranca 12:58:05 y el presentador se oye en el
                transcript del lavalier a las 12:56:44 — dos medidas
                independientes que coinciden

Salida: `show_windows` en `<disco>/.cinema_assistant/project_config.json`, como
lista de {inicio_epoch, fin_epoch, inicio_local, fin_local, dur_min, nivel_db}.
Se reescribe cada corrida; el resto del config no se toca.

Uso:
    python3 bin/derive_show_windows.py --root <disco>
    python3 bin/derive_show_windows.py --root <disco> --percentil 70   # forzado
    python3 bin/derive_show_windows.py --root <disco> --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.guards import assert_selected, report_done      # noqa: E402
from lib import manifest  # noqa: E402

SR = 4000          # basta para medir energia; acelera x12 la lectura
VENTANA = 1.0      # segundos por muestra del perfil


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resoluble: {root_arg}")


def perfil_db(path: str) -> np.ndarray | None:
    """Nivel RMS en dB por segundo del archivo entero."""
    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.wav")
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "a:0",
             "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", wav],
            capture_output=True)
        if r.returncode != 0:
            return None
        b = Path(wav).read_bytes()
        if len(b) < 44 + SR:
            return None
        x = np.frombuffer(b[44:], dtype="<i2").astype(np.float32) / 32768.0
    n = int(SR * VENTANA)
    m = x.size // n
    if m < 2:
        return None
    rms = np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1) + 1e-12)
    return 20.0 * np.log10(rms)


def _otsu(v: np.ndarray) -> float:
    """Corte que mejor separa en DOS el histograma (Otsu clasico)."""
    lo, hi = float(np.min(v)), float(np.max(v))
    if hi - lo < 1e-6:
        return hi
    hist, bordes = np.histogram(v, bins=128, range=(lo, hi))
    p = hist.astype(np.float64) / max(1, hist.sum())
    centros = (bordes[:-1] + bordes[1:]) / 2.0
    w0 = np.cumsum(p)
    w1 = 1.0 - w0
    mu = np.cumsum(p * centros)
    mu_t = mu[-1]
    ok = (w0 > 1e-9) & (w1 > 1e-9)
    if not ok.any():
        return float(np.median(v))
    entre = np.zeros_like(w0)
    entre[ok] = (mu_t * w0[ok] - mu[ok]) ** 2 / (w0[ok] * w1[ok])
    return float(centros[int(np.argmax(entre))])


def umbral_show(v: np.ndarray) -> float:
    """Corte que separa la MUSICA de la voz, aplicando Otsu dos veces.

    Por que no un percentil: supone que ya sabes que proporcion del dia es show.
    En Morsa el percentil 55 dio -14.2 dB y se colaron las entrevistas.

    Por que no un Otsu solo: el dia no tiene dos modos, tiene TRES — silencio
    (~-30 dB), voz (~-18) y musica (~-10). Un Otsu de dos clases corta entre el
    silencio y todo lo demas (-18.0 dB medido en Morsa) y mete la voz dentro del
    "show". La segunda pasada, ya solo sobre lo que quedo arriba, es la que
    separa voz de musica.

    Si el dia no tiene show, la segunda pasada corta dentro de la voz y devuelve
    un umbral alto: quien llama sigue exigiendo duracion minima al tramo, asi
    que no se inventan ventanas.
    """
    if v.size < 16:
        return float(np.max(v)) if v.size else 0.0
    primero = _otsu(v)
    arriba = v[v >= primero]
    if arriba.size < 16:
        return primero
    return _otsu(arriba)


def tramos(mask: np.ndarray, min_len: int, merge_gap: int) -> list[tuple[int, int]]:
    """Indices [ini, fin) de los tramos True, uniendo huecos cortos."""
    idx = np.nonzero(mask)[0]
    if idx.size == 0:
        return []
    cortes = np.nonzero(np.diff(idx) > merge_gap)[0]
    grupos = np.split(idx, cortes + 1)
    return [(int(g[0]), int(g[-1]) + 1) for g in grupos if (g[-1] - g[0] + 1) >= min_len]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%",
                    help="filename LIKE (lowercased) para acotar los audios. El "
                         "default matchea TODO a proposito: el perfil del dia "
                         "quiere oir todas las cadenas, y un default atado a un "
                         "proyecto ('%%wireless%%') es la leccion 47 esperando "
                         "a repetirse.")
    ap.add_argument("--percentil", type=float, default=None,
                    help="Fuerza el umbral a un percentil del nivel del dia. "
                         "Sin esto se usa Otsu, que busca donde el histograma "
                         "se parte en dos y no supone cuanto del dia es show.")
    ap.add_argument("--min-minutos", type=float, default=2.0,
                    help="Duracion minima de un tramo para contar como show.")
    ap.add_argument("--merge-gap", type=float, default=60.0,
                    help="Huecos mas cortos que esto no parten el tramo (s).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Sin manifest en {db}")
    conn = manifest.conectar(str(db))

    wavs = conn.execute(
        "SELECT id, path, rel_path, duration_sec, mtime FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' AND lower(filename) LIKE ?",
        (args.audio_like,)).fetchall()
    assert_selected(wavs, "audios continuos", filters={
        "--root": str(root), "--audio-like": args.audio_like,
    }, hint="En proyecto plano (carpeta=cadena) probar --audio-like '%wireless%'")

    # Perfil por archivo, anclado en su hora real de inicio (mtime - duracion).
    # Los WAV del Rode no traen BEXT ni iXML: el mtime es el unico reloj que hay,
    # y esta guardado a segundo entero. Para delimitar un tramo de minutos esa
    # precision sobra; para sincronizar NO — de ahi no salen offsets.
    muestras: list[tuple[float, float]] = []   # (epoch, dB)
    leidos = 0
    for _cid, path, rel, dur, mtime in wavs:
        p = perfil_db(path)
        if p is None:
            print(f"  ⚠ ilegible: {rel}")
            continue
        leidos += 1
        inicio = mtime - dur
        for i, v in enumerate(p):
            muestras.append((inicio + i * VENTANA, float(v)))
        print(f"  {rel}: {len(p)} s de perfil, mediana {np.median(p):+.1f} dB")

    if not muestras:
        sys.exit("Ningun audio legible: no hay perfil que medir.")

    muestras.sort()
    t = np.array([m[0] for m in muestras])
    d = np.array([m[1] for m in muestras])
    # Dos cadenas paralelas cubren el mismo instante: nos quedamos con la mas
    # alta de las dos por segundo, que es la que describe lo que sonaba.
    t0 = t.min()
    seg = np.round(t - t0).astype(int)
    ancho = int(seg.max()) + 1
    nivel = np.full(ancho, -120.0, dtype=np.float32)
    np.maximum.at(nivel, seg, d)
    vistos = np.zeros(ancho, dtype=bool)
    vistos[seg] = True

    if args.percentil is not None:
        umbral = float(np.percentile(nivel[vistos], args.percentil))
        como = f"percentil {args.percentil:.0f} del dia"
    else:
        umbral = umbral_show(nivel[vistos])
        como = "Otsu doble (separa musica de voz)"
    mask = vistos & (nivel >= umbral)
    ventanas = tramos(mask, int(args.min_minutos * 60), int(args.merge_gap))

    def local(ep: float) -> str:
        return dt.datetime.fromtimestamp(ep).strftime("%Y-%m-%d %H:%M:%S")

    print()
    print(f"Umbral ({como}): {umbral:+.1f} dB")
    salida = []
    for ini, fin in ventanas:
        e0, e1 = t0 + ini, t0 + fin
        nv = float(np.mean(nivel[ini:fin][vistos[ini:fin]]))
        salida.append({
            "inicio_epoch": round(e0, 3), "fin_epoch": round(e1, 3),
            "inicio_local": local(e0), "fin_local": local(e1),
            "dur_min": round((e1 - e0) / 60.0, 1), "nivel_db": round(nv, 1),
        })
        print(f"  show: {local(e0)} -> {local(e1)}  "
              f"({(e1-e0)/60:.1f} min, {nv:+.1f} dB)")
    if not ventanas:
        print("  (ningun tramo supera el umbral el tiempo minimo)")

    cfg_path = root / ".cinema_assistant" / "project_config.json"
    if args.dry_run:
        print("\n(dry-run: no se escribe project_config.json)")
    else:
        cfg = {}
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["show_windows"] = salida
        cfg["show_windows_nota"] = (
            f"Medido por bin/derive_show_windows.py: umbral por {como} "
            f"({umbral:+.1f} dB), tramos de "
            f">= {args.min_minutos:g} min uniendo huecos < {args.merge_gap:g} s. "
            "Dentro de estas ventanas todas las camaras apuntan a lo mismo, asi "
            "que export_multicam_lua.py --trust-clock-in-windows acepta ahi la "
            "alineacion por reloj. Fuera, sigue exigiendo confirmacion por "
            "contenido."
        )
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\nEscrito en {cfg_path}")

    report_done("derive_show_windows", audios=leidos, ventanas=len(ventanas),
                umbral_db=round(umbral, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
