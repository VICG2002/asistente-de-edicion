#!/usr/bin/env python3
"""Genera el par video+lavalier sintetico con OFFSET CONOCIDO para probar el
merge de audio (`resolve/spike_merge.lua`).

Para que sirve
--------------
El merge en el Media Pool (`AutoSyncAudio`) no se deshace por API. Antes de
soltarlo sobre material real hay que comprobar, en LA MAQUINA donde se va a
usar, que:

  - AutoSyncAudio existe y corre en esa build de Resolve;
  - RETAIN_EMBEDDED_AUDIO conserva el audio de camara;
  - el offset que calcula Resolve coincide con el real.

Lo tercero solo se puede comprobar con material cuyo offset se conozca de
antemano. Eso es lo que genera este script.

Que produce
-----------
  camara.mov   30 s. Su audio de camara es la señal maestra DESDE el segundo 5
               (la camara arranco 5 s despues que la grabadora).
  lav.wav      40 s. La señal maestra completa.

  offset esperado, con la convencion del motor (audio_start - video_start):
      0 - 5 = -5.00 s

La señal son beeps de frecuencia distinta cada 3 s: la correlacion no puede
confundirse consigo misma en otro punto del archivo.

Uso:
    python3 bin/generar_media_prueba.py
    python3 bin/generar_media_prueba.py --dir /ruta/donde/dejarlo --offset 5.0
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

SR = 48000
DUR_LAV = 40.0
DUR_VIDEO = 30.0


def generar_maestra(destino: Path, dur: float) -> None:
    n = int(SR * dur)
    beeps = [(i * 3.0, 300 + i * 137) for i in range(int(dur // 3) + 1)]
    buf = bytearray()
    for k in range(n):
        t = k / SR
        v = 0.0
        for t0, f in beeps:
            if t0 <= t < t0 + 0.25:
                env = math.sin(math.pi * (t - t0) / 0.25)   # ventana suave
                v += 0.7 * env * math.sin(2 * math.pi * f * t)
        v += 0.004 * math.sin(2 * math.pi * 57 * t)         # ruido de sala tenue
        buf += struct.pack("<h", max(-32767, min(32767, int(v * 32767))))
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(buf))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="",
                    help="Destino. Default: <motor>/tests/media_prueba/")
    ap.add_argument("--offset", type=float, default=5.0,
                    help="Segundos que la grabadora arranco ANTES que la camara. "
                         "Default 5.0 → offset esperado -5.00 s.")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg no encontrado — necesario para generar el video.")

    destino = Path(args.dir) if args.dir else (
        Path(__file__).resolve().parent.parent / "tests" / "media_prueba")
    destino.mkdir(parents=True, exist_ok=True)

    maestra = destino / "master.wav"
    lav = destino / "lav.wav"
    video = destino / "camara.mov"

    print(f"Generando la señal maestra ({DUR_LAV:.0f} s)...")
    generar_maestra(maestra, DUR_LAV)
    shutil.copy(maestra, lav)

    print(f"Generando el video ({DUR_VIDEO:.0f} s, audio desde el segundo "
          f"{args.offset:g})...")
    r = subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i",
        f"testsrc=size=640x360:rate=24:duration={DUR_VIDEO:g}",
        "-ss", str(args.offset), "-t", str(DUR_VIDEO), "-i", str(maestra),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
        "-shortest", str(video),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg fallo:\n{r.stderr}")
    maestra.unlink()

    print(f"\nListo en: {destino}")
    print(f"  camara.mov  {DUR_VIDEO:.0f} s  (audio de camara desde el segundo "
          f"{args.offset:g} de la maestra)")
    print(f"  lav.wav     {DUR_LAV:.0f} s  (la maestra completa)")
    print(f"\noffset esperado (audio_start - video_start) = "
          f"-{args.offset:.2f} s")
    print(f"\nEn la Consola de Resolve:")
    print(f'  MERGE_TEST_MEDIA = "{destino}"')
    print(f'  dofile("{Path(__file__).resolve().parent.parent}/resolve/spike_merge.lua")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
