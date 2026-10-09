#!/usr/bin/env python3
"""Un tramo de material, mirado: imagen, sonido y palabras en la misma regla.

POR QUE EXISTE
Este motor vive del audio —sync, pausas, offsets de lavalier, cortes— y hasta
ahora todo lo que devolvia eran numeros. `verify_lav_offsets.py` dice que un par
esta a 4.19 s y el editor tiene que creerselo o irse a Resolve a comprobarlo a
mano. Un desfase no se discute con una tabla: se VE. Dos ondas una encima de la
otra, con las palabras encima, y la respuesta esta en la primera mirada.

QUE DIBUJA
  1. Filmstrip del rango, con la hora de cada frame.
  2. Una cinta de forma de onda por fuente de audio pedida. El A1 de la camara
     es la de arriba, el lavalier sincronizado va debajo con su offset escrito.
  3. Las palabras del transcript cacheado, cada una en su tiempo.
  4. Los silencios de `clip_silences` sombreados.
  5. Una regla de tiempo comun a todo lo anterior.

COMO SE LEE UN OFFSET AQUI
Con `--audio ambos`, las dos cintas comparten la regla y estan ya convertidas a
tiempo de VIDEO (convencion del motor: `offset = audio_start - video_start`, asi
que `t_audio = t_video - offset`). Si el sync esta bien, los picos coinciden en
vertical. Si no, se ve cuanto y hacia donde.

ES UNA HERRAMIENTA DE PUNTO DE DECISION, NO DE BARRIDO
No se corre en bucle sobre cada tramo del proyecto: cada vista cuesta una
extraccion de frames y un decode de audio, y mirar mil imagenes no es mirar. Se
corre cuando algo esta EN DUDA — un par de sync que no cuadra, una pausa que no
se sabe si es contenido, un corte que suena raro. Para el barrido estan los
verificadores, que devuelven numeros y son baratos.

NO ESCRIBE EN EL MANIFEST. Lee y dibuja.

Uso:
    python3 bin/vista_tramo.py --root <disco> --clip-id 1234 --desde 12 --hasta 24
    python3 bin/vista_tramo.py --root <disco> --clip-id 1234 --desde 12 --hasta 24 \
        --audio ambos
    python3 bin/vista_tramo.py --video <ruta.mov> --desde 0 --hasta 30 --out vista.png
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import frames as F  # noqa: E402
from lib import manifest  # noqa: E402
from lib.master_transcript import load_master_or_fallback  # noqa: E402

ANCHO = 1600
MARGEN = 16
ALTO_ONDA = 110
ALTO_PALABRAS = 42
ALTO_REGLA = 26
ALTO_TITULO = 26

FONDO = (18, 18, 20)
TINTA = (232, 232, 236)
TENUE = (128, 128, 136)
ONDA = (108, 196, 232)
ONDA_B = (232, 168, 96)
SILENCIO = (52, 52, 60)
CUADRICULA = (60, 60, 68)


def cargar_fuente(size: int):
    from PIL import ImageFont
    for ruta in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(ruta, size)
        except OSError:
            continue
    return ImageFont.load_default()


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resoluble: {root_arg}")


def envolvente(media: str, inicio: float, dur: float,
               muestras: int = 1600) -> np.ndarray | None:
    """RMS por ventana del audio del rango. None si no hay audio decodificable.

    El decode va a WAV mono 16 kHz —lo mismo que come whisper-cli— y se lee con
    el `wave` de la stdlib. Sin librosa ni scipy: esta vista tiene que abrir en
    cualquier maquina donde ya corra el motor.
    """
    if dur <= 0:
        return None
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = Path(f.name)
    try:
        cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
               "-ss", f"{max(0.0, inicio):.3f}", "-i", media,
               "-t", f"{dur:.3f}", "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
        except Exception:
            return None
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return None
        with wave.open(str(tmp), "rb") as w:
            crudo = w.readframes(w.getnframes())
        pcm = np.frombuffer(crudo, dtype=np.int16).astype(np.float32) / 32768.0
        if pcm.size == 0:
            return None
        ventana = max(1, pcm.size // muestras)
        util = (pcm.size // ventana) * ventana
        if util == 0:
            return None
        env = np.sqrt(np.mean(pcm[:util].reshape(-1, ventana) ** 2, axis=1))
        if env.size < muestras:
            env = np.pad(env, (0, muestras - env.size))
        return env[:muestras]
    finally:
        tmp.unlink(missing_ok=True)


def normalizar(env: np.ndarray, pico: float) -> np.ndarray:
    """Escala contra un pico COMUN a todas las cintas.

    Normalizar cada cinta contra su propio maximo es lo que hace que un lavalier
    limpio y un A1 de sala se dibujen igual de altos y parezcan la misma senal.
    Con un pico comun, la que grabo mas bajo se ve mas baja, que es la verdad.
    """
    if pico <= 0:
        return np.zeros_like(env)
    return np.clip(env / pico, 0.0, 1.0)


def palabras_del_rango(root: Path, clip_id: int, inicio: float,
                       fin: float) -> list[tuple[str, float]]:
    d = load_master_or_fallback(root / ".cinema_assistant" / "transcripts",
                                clip_id)
    if not d:
        return []
    out = []
    for w in d.get("words") or []:
        if not isinstance(w, (list, tuple)) or len(w) < 2:
            continue
        try:
            t = float(w[1])
        except (TypeError, ValueError):
            continue
        if inicio <= t <= fin:
            out.append((str(w[0]), t))
    return sorted(out, key=lambda x: x[1])


def silencios_del_rango(conn, clip_id: int, inicio: float,
                        fin: float) -> list[tuple[float, float]]:
    try:
        filas = conn.execute(
            "SELECT start_sec, end_sec FROM clip_silences WHERE clip_id=? "
            "AND end_sec > ? AND start_sec < ?",
            (clip_id, inicio, fin)).fetchall()
    except Exception:
        return []
    return [(float(a), float(b)) for a, b in filas
            if a is not None and b is not None]


def lavalier_del_clip(conn, clip_id: int) -> tuple[str, float, str] | None:
    """(ruta, offset, nombre) del audio externo mejor sincronizado del clip."""
    try:
        fila = conn.execute(
            "SELECT ca.path, p.offset_sec, ca.filename FROM audio_sync_pairs p "
            "JOIN clips ca ON ca.id = p.audio_clip_id "
            "WHERE p.video_clip_id=? ORDER BY p.confidence DESC LIMIT 1",
            (clip_id,)).fetchone()
    except Exception:
        return None
    if not fila or not fila[0] or fila[1] is None:
        return None
    return str(fila[0]), float(fila[1]), str(fila[2] or Path(fila[0]).name)


def _x(t: float, inicio: float, dur: float, x0: int, ancho: int) -> int:
    if dur <= 0:
        return x0
    return x0 + int(round((t - inicio) / dur * ancho))


def dibujar_cinta(dib, env: np.ndarray, x0: int, y0: int, ancho: int,
                  alto: int, color) -> None:
    medio = y0 + alto // 2
    dib.line([(x0, medio), (x0 + ancho, medio)], fill=CUADRICULA, width=1)
    if env is None or env.size == 0:
        return
    for i in range(ancho):
        v = float(env[min(int(i / ancho * env.size), env.size - 1)])
        h = int(v * (alto // 2 - 2))
        if h > 0:
            dib.line([(x0 + i, medio - h), (x0 + i, medio + h)],
                     fill=color, width=1)


def construir(*, video: str, inicio: float, fin: float, n_frames: int,
              env_a, etq_a: str, env_b, etq_b: str, pico: float,
              palabras: list[tuple[str, float]],
              silencios: list[tuple[float, float]],
              titulo: str, destino: Path) -> Path:
    dur = max(0.001, fin - inicio)
    fuente = cargar_fuente(13)
    fuente_chica = cargar_fuente(11)

    with tempfile.TemporaryDirectory() as d:
        tiras = F.extraer_uniforme(video, Path(d), inicio, fin, n_frames,
                                   ancho=480, prefijo="v")
        imgs = []
        for t in tiras:
            try:
                im = Image.open(t["path"])
                im.load()
                imgs.append((im, t["t_sec"]))
            except Exception:
                continue

        ancho_util = ANCHO - 2 * MARGEN
        if imgs:
            celda = ancho_util // len(imgs)
            alto_tira = int(celda * imgs[0][0].height / imgs[0][0].width)
            imgs = [(im.resize((celda, alto_tira)), t) for im, t in imgs]
        else:
            celda, alto_tira = 0, 0

        alto = (ALTO_TITULO + alto_tira + 18 + ALTO_ONDA + ALTO_PALABRAS
                + (ALTO_ONDA + 18 if env_b is not None else 0) + ALTO_REGLA
                + 3 * MARGEN)
        lienzo = Image.new("RGB", (ANCHO, alto), FONDO)
        dib = ImageDraw.Draw(lienzo)

        y = MARGEN
        dib.text((MARGEN, y), titulo, fill=TINTA, font=fuente)
        y += ALTO_TITULO

        for i, (im, t) in enumerate(imgs):
            x = MARGEN + i * celda
            lienzo.paste(im, (x, y))
            dib.text((x + 3, y + alto_tira - 14), f"{t:.2f}s",
                     fill=TINTA, font=fuente_chica)
        y += alto_tira + 18

        x0 = MARGEN
        for (a, b) in silencios:
            xa = _x(max(a, inicio), inicio, dur, x0, ancho_util)
            xb = _x(min(b, fin), inicio, dur, x0, ancho_util)
            if xb > xa:
                alto_sombra = ALTO_ONDA + (ALTO_ONDA + 18 if env_b is not None else 0)
                dib.rectangle([xa, y, xb, y + alto_sombra], fill=SILENCIO)

        dib.text((x0, y - 14), etq_a, fill=TENUE, font=fuente_chica)
        dibujar_cinta(dib, normalizar(env_a, pico) if env_a is not None else None,
                      x0, y, ancho_util, ALTO_ONDA, ONDA)
        y += ALTO_ONDA

        ultimo_x = -999
        for palabra, t in palabras:
            px = _x(t, inicio, dur, x0, ancho_util)
            if px - ultimo_x < 8:
                continue
            dib.line([(px, y - 6), (px, y + 4)], fill=TENUE, width=1)
            dib.text((px + 2, y + 6), palabra[:18], fill=TINTA,
                     font=fuente_chica)
            ultimo_x = px
        y += ALTO_PALABRAS

        if env_b is not None:
            dib.text((x0, y - 13), etq_b, fill=TENUE, font=fuente_chica)
            dibujar_cinta(dib, normalizar(env_b, pico), x0, y, ancho_util,
                          ALTO_ONDA, ONDA_B)
            y += ALTO_ONDA + 18

        paso = _paso_regla(dur)
        t = inicio - (inicio % paso) + paso
        while t < fin:
            px = _x(t, inicio, dur, x0, ancho_util)
            dib.line([(px, y), (px, y + 6)], fill=TENUE, width=1)
            dib.text((px + 2, y + 8), f"{t:.1f}s", fill=TENUE,
                     font=fuente_chica)
            t += paso

        destino.parent.mkdir(parents=True, exist_ok=True)
        lienzo.save(destino, "PNG")
    return destino


def _paso_regla(dur: float) -> float:
    """El paso mas fino que deje doce marcas o menos. Mas de doce no se lee.

    El ultimo recurso no es una constante: es `dur/10`. Una lista fija de pasos
    siempre se queda corta para algun rango, y quedarse corta aqui significa
    dibujar una regla ilegible sin que nada avise.
    """
    for paso in (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600):
        if dur / paso <= 12:
            return float(paso)
    return max(0.5, float(dur) / 10.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="Disco del proyecto")
    ap.add_argument("--clip-id", type=int, default=0)
    ap.add_argument("--video", default="",
                    help="Ruta directa a un video, sin manifest")
    ap.add_argument("--desde", type=float, required=True)
    ap.add_argument("--hasta", type=float, required=True)
    ap.add_argument("--audio", choices=("camara", "lavalier", "ambos"),
                    default="camara")
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--out", default="",
                    help="PNG destino. Por defecto, "
                         "<disco>/.cinema_assistant/vistas/<clip>_<desde>_<hasta>.png")
    ap.add_argument("--sin-transcript", action="store_true")
    args = ap.parse_args()

    if args.hasta <= args.desde:
        sys.exit("--hasta tiene que ser mayor que --desde")
    if not args.video and not (args.root and args.clip_id):
        sys.exit("Hace falta --video, o --root con --clip-id")

    inicio, fin = float(args.desde), float(args.hasta)
    dur = fin - inicio
    conn = None
    root = None
    palabras: list[tuple[str, float]] = []
    silencios: list[tuple[float, float]] = []
    lav = None

    if args.video:
        video = args.video
        titulo = f"{Path(video).name}  ·  {inicio:.2f}–{fin:.2f} s"
        destino = Path(args.out or f"vista_{int(inicio)}_{int(fin)}.png")
    else:
        root = resolve_root(args.root)
        db = root / ".cinema_assistant" / "manifest.sqlite"
        if not db.exists():
            sys.exit(f"No hay manifest en {db}")
        conn = manifest.conectar(str(db))
        fila = conn.execute("SELECT path, filename FROM clips WHERE id=?",
                            (args.clip_id,)).fetchone()
        if not fila or not fila[0]:
            sys.exit(f"No hay clip {args.clip_id} en el manifest")
        video, nombre = str(fila[0]), str(fila[1] or Path(fila[0]).name)
        if not Path(video).exists():
            sys.exit(f"El clip {args.clip_id} apunta a un archivo que no esta "
                     f"montado: {video}")
        titulo = (f"clip {args.clip_id}  ·  {nombre}  ·  "
                  f"{inicio:.2f}–{fin:.2f} s")
        silencios = silencios_del_rango(conn, args.clip_id, inicio, fin)
        if not args.sin_transcript:
            palabras = palabras_del_rango(root, args.clip_id, inicio, fin)
        if args.audio in ("lavalier", "ambos"):
            lav = lavalier_del_clip(conn, args.clip_id)
            if lav is None:
                print(f"AVISO: el clip {args.clip_id} no tiene par en "
                      f"audio_sync_pairs — se dibuja solo el A1 de camara.")
        destino = Path(args.out) if args.out else (
            root / ".cinema_assistant" / "vistas"
            / f"{args.clip_id}_{inicio:.0f}_{fin:.0f}.png")

    env_a = env_b = None
    etq_a = "A1 cámara"
    etq_b = ""
    if args.audio in ("camara", "ambos") or lav is None:
        env_a = envolvente(video, inicio, dur)
        if env_a is None:
            print("AVISO: no se pudo decodificar el A1 de cámara.")
    if lav is not None:
        ruta_lav, offset, nombre_lav = lav
        if not Path(ruta_lav).exists():
            print(f"AVISO: el lavalier apunta a un archivo que no esta "
                  f"montado: {ruta_lav}")
        else:
            # t_audio = t_video - offset. Un offset negativo (el audio empezo
            # antes que el video) mueve la ventana HACIA ADELANTE en el WAV.
            env_lav = envolvente(ruta_lav, inicio - offset, dur)
            etiqueta = f"lavalier {nombre_lav}  ·  offset {offset:+.3f} s"
            if args.audio == "lavalier":
                env_a, etq_a = env_lav, etiqueta
            else:
                env_b, etq_b = env_lav, etiqueta
            if env_lav is None:
                print("AVISO: no se pudo decodificar el lavalier.")

    if env_a is None and env_b is None:
        print("AVISO: sin audio que dibujar. La vista sale solo con imagen.")

    # Pico comun: las dos cintas se comparan entre si, no consigo mismas.
    picos = [float(e.max()) for e in (env_a, env_b) if e is not None and e.size]
    pico = max(picos) if picos else 1.0

    out = construir(video=video, inicio=inicio, fin=fin,
                    n_frames=max(1, args.frames), env_a=env_a, etq_a=etq_a,
                    env_b=env_b, etq_b=etq_b, pico=pico, palabras=palabras,
                    silencios=silencios, titulo=titulo, destino=destino)
    if conn is not None:
        conn.close()

    print(f"{out}")
    print(f"  frames={args.frames}  palabras={len(palabras)}  "
          f"silencios={len(silencios)}  "
          f"ondas={sum(1 for e in (env_a, env_b) if e is not None)}")


if __name__ == "__main__":
    main()
