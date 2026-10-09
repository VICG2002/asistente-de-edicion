"""Medir el sonido de un export con ffmpeg. Solo medida, ninguna decision.

POR QUE EXISTE (Morsa, 2026-08-19)
El motor sabia medir el RMS de un tramo para decidir un corte, y nada mas. Nadie
miraba el archivo entregado. El resultado, medido sobre los cinco exports de
Morsa: cinco loudness distintos con 15.2 LU entre el mas alto y el mas bajo, un
`Cut 1.mov` a -6.4 LUFS con 1,031,972 muestras clavadas a fondo de escala, y un
master a -13.4 LUFS que todavia clipea 32,159. Ninguno de esos numeros aparecio
en ningun sitio hasta que se busco.

LO QUE MIDE Y POR QUE CADA COSA
  LUFS integrado  cuanto suena de verdad. Es lo que normalizan las plataformas.
  LRA             el rango. Un LRA de 20.6 LU quiere decir que el espectador
                  sube el volumen para oir la entrevista y se lo comen los
                  ultimos dos minutos de concierto.
  True peak       picos entre muestras. Por encima de 0 dBTP el codec de
                  entrega distorsiona aunque el archivo se vea limpio.
  Muestras a 0 dBFS  el clipping DURO, que el true peak no distingue de un pico
                  legitimo. Es el dato que delato a `Cut 1.mov`.
  Short-term      la curva. Sin ella no se sabe DONDE esta el problema.

TRES TRAMPAS DE INSTRUMENTO, YA PAGADAS
  1. `astats` y `silencedetect` escriben a nivel `info`, no `error`. Con
     `-v error` el script sale limpio y sin una sola medida (leccion de IMODAE).
  2. `-vn` obligatorio: medir con el video dentro de un 4K HEVC cuesta dos
     ordenes de magnitud mas.
  3. El "Noise floor" de `astats` es un minimo instantaneo, no un piso. No sirve
     para calibrar nada.

NO HAY OBJETIVO AQUI. Decision de Victor (2026-08-19): el motor mide y avisa; el
numero al que se entrega lo firma el editor.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Por debajo de esto un tramo de habla no se oye contra el resto de la mezcla.
UMBRAL_BAJO_LUFS = -26.0
# Por encima de esto un tramo tapa cualquier dialogo que venga antes o despues.
UMBRAL_ALTO_LUFS = -10.0
# Un salto asi en un segundo obliga a tocar el volumen.
SALTO_LU = 6.0

BANDAS = (
    ("<80 Hz", "lowpass=f=80:poles=2"),
    ("80-300 Hz", "highpass=f=80:poles=2,lowpass=f=300:poles=2"),
    ("300-3400 Hz (voz)", "highpass=f=300:poles=2,lowpass=f=3400:poles=2"),
    ("3.4-8 kHz", "highpass=f=3400:poles=2,lowpass=f=8000:poles=2"),
    (">8 kHz", "highpass=f=8000:poles=2"),
)

_RE_I = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_RE_LRA = re.compile(r"^\s*LRA:\s*(-?[\d.]+)\s*LU", re.M)
_RE_TP = re.compile(r"Peak:\s*(-?[\d.]+)\s*dBFS")
_RE_RMS = re.compile(r"RMS level dB:\s*(-?[\d.inf]+)")
_RE_PEAK = re.compile(r"Peak level dB:\s*(-?[\d.inf]+)")
_RE_ABS = re.compile(r"Abs Peak count:\s*([\d.]+)")
_RE_FLAT = re.compile(r"Flat factor:\s*([\d.]+)")
_RE_SIL_INI = re.compile(r"silence_start:\s*(-?[\d.]+)")
_RE_SIL_FIN = re.compile(r"silence_end:\s*([\d.]+)")


def _ff(args: list[str], timeout: int = 1800) -> str:
    """ffmpeg con `-v info`: las medidas viven en stderr a nivel info."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-v", "info"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RuntimeError(f"ffmpeg fallo: {e}") from e
    return (r.stderr or "") + (r.stdout or "")


def _rango(ini: float | None, dur: float | None) -> list[str]:
    a = []
    if ini:
        a += ["-ss", f"{ini:.3f}"]
    return a + (["-t", f"{dur:.3f}"] if dur else [])


def r128(path: str | Path, ini: float | None = None,
         dur: float | None = None) -> dict:
    """LUFS integrado, LRA y true peak."""
    txt = _ff(_rango(ini, dur) + ["-i", str(path), "-map", "0:a:0", "-vn",
                                  "-af", "ebur128=peak=true:framelog=verbose",
                                  "-f", "null", "-"])
    cola = txt.split("Summary:")[-1]
    i = _RE_I.search(cola)
    lra = _RE_LRA.search(cola)
    tp = _RE_TP.search(cola)
    return {"lufs": float(i.group(1)) if i else None,
            "lra": float(lra.group(1)) if lra else None,
            "true_peak": float(tp.group(1)) if tp else None}


def astats(path: str | Path, ini: float | None = None,
           dur: float | None = None, filtro: str = "") -> dict:
    """Pico, RMS, muestras a fondo de escala y flat factor (por canal)."""
    af = (filtro + ",") if filtro else ""
    txt = _ff(_rango(ini, dur) + ["-i", str(path), "-map", "0:a:0", "-vn",
                                  "-af", af + "astats", "-f", "null", "-"])
    def num(rx, conv=float):
        return [conv(m.group(1)) for m in rx.finditer(txt)]
    picos = num(_RE_PEAK)
    rms = num(_RE_RMS)
    abs_ = num(_RE_ABS, lambda x: int(float(x)))
    flat = num(_RE_FLAT)
    # astats imprime un bloque por canal y otro de resumen ("Overall"): los
    # ultimos valores son el resumen, los anteriores los canales.
    canales = max(0, len(picos) - 1)
    return {"pico_db": max(picos[:canales]) if canales else (picos[-1] if picos else None),
            "rms_db": rms[-1] if rms else None,
            "muestras_a_fondo": max(abs_[:canales]) if canales else (abs_[-1] if abs_ else 0),
            "flat_factor": max(flat[:canales]) if canales else (flat[-1] if flat else None),
            "canales": canales}


def short_term(path: str | Path) -> list[tuple[float, float]]:
    """[(segundo, LUFS short-term)] cada 100 ms."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "st.txt"
        _ff(["-i", str(path), "-map", "0:a:0", "-vn", "-af",
             f"ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.S:file={f}",
             "-f", "null", "-"])
        if not f.exists():
            return []
        out, t = [], None
        for linea in f.read_text(errors="replace").splitlines():
            m = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)", linea)
            if m:
                t = float(m.group(1))
                continue
            m = re.match(r"lavfi\.r128\.S=(-?[\d.]+)", linea.strip())
            if m and t is not None:
                out.append((t, float(m.group(1))))
        return out


def silencios(path: str | Path, umbral_db: float = -34.0,
              min_dur: float = 0.30) -> list[tuple[float, float]]:
    """Pausas medidas en el audio, no en el transcript."""
    txt = _ff(["-i", str(path), "-map", "0:a:0", "-vn", "-af",
               f"silencedetect=n={umbral_db}dB:d={min_dur}", "-f", "null", "-"])
    out, ini = [], None
    for linea in txt.splitlines():
        m = _RE_SIL_INI.search(linea)
        if m:
            ini = float(m.group(1))
            continue
        m = _RE_SIL_FIN.search(linea)
        if m and ini is not None:
            out.append((ini, float(m.group(1))))
            ini = None
    return out


def por_banda(path: str | Path, ini: float, dur: float) -> dict[str, float | None]:
    return {nombre: astats(path, ini, dur, filtro=f)["rms_db"]
            for nombre, f in BANDAS}


def tramos(serie: list[tuple[float, float]], pred, min_dur: float = 3.0):
    """Tramos consecutivos de la serie que cumplen `pred`, de al menos min_dur."""
    out, cur = [], None
    for t, v in serie:
        if pred(v):
            cur = [t, t] if cur is None else [cur[0], t]
        else:
            if cur and cur[1] - cur[0] >= min_dur:
                out.append((cur[0], cur[1]))
            cur = None
    if cur and cur[1] - cur[0] >= min_dur:
        out.append((cur[0], cur[1]))
    return out


def saltos(serie: list[tuple[float, float]], delta: float = SALTO_LU):
    """Cambios de >= delta LU de un segundo al siguiente."""
    rejilla = {}
    for t, v in serie:
        if v > -70:
            rejilla[round(t)] = v
    out = []
    for k in sorted(rejilla):
        if k - 1 in rejilla:
            d = rejilla[k] - rejilla[k - 1]
            if abs(d) >= delta:
                out.append((k, d))
    return out


def percentiles(serie: list[tuple[float, float]], *qs: float) -> list[float]:
    v = sorted(x for _, x in serie if x > -70)
    if not v:
        return [float("nan")] * len(qs)
    return [v[min(len(v) - 1, int(len(v) * q))] for q in qs]


def mmss(t: float) -> str:
    return f"{int(t)//60:02d}:{t % 60:05.2f}"
