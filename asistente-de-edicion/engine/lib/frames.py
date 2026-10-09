"""De un tramo de video a los frames que de verdad hay que mirar.

POR QUE EXISTE
Hasta ahora los frames de un tramo salian por porcentaje fijo: 10, 30, 50, 70,
90. Es un reparto ciego. En una entrevista a plano fijo los cinco frames son la
misma imagen, y cada uno se paga tres veces: en disco, en el batch de YOLO y en
la descripcion del vision LLM, que sobre 1700 tramos son de tres a cinco horas.
En un tramo con movimiento de camara pasa lo contrario: los cinco caen donde
caen y el corte que importaba queda fuera.

QUE HACE EN SU LUGAR
Tres decisiones, en este orden:

  1. PRESUPUESTO POR DURACION. Cuantos frames merece el tramo por lo que dura,
     no un numero fijo por porcentaje.
  2. DONDE. Por cambio de escena (`select=gt(scene,...)`), que es donde la
     imagen cambia de verdad. Si el tramo resulta ser estatico —menos de
     MIN_ESCENAS cortes detectados— se cae a muestreo uniforme, Y SE DICE.
  3. QUE SOBREVIVE. Los casi identicos se tiran comparando la diferencia media
     por pixel de una miniatura en gris. Conservador a proposito: colapsa el
     mismo plano sostenido y respeta un plano que se abre.

LO QUE NUNCA SE TIRA
Los cues —los tiempos que el HABLA senala: el inicio de una respuesta, una
pausa medida, un corte de la timeline— se reservan contra el tope ANTES de
correr el motor de seleccion. La seleccion visual se los pierde justo porque
senalar algo apenas cambia la imagen. Un cue no compite con un frame de escena:
gana siempre.

FAIL-OPEN EN EL DEDUP
Si ffmpeg falla al hacer las miniaturas, o los bytes no cuadran uno a uno con
los frames, no se deduplica nada y se devuelve la lista intacta. Un dedup que
adivina es peor que no deduplicar: tiraria frames buenos en silencio.

EL COSTE, PARA QUE ESTE ESCRITO
Ochenta frames a 512 px de ancho son del orden de 50-80k tokens de imagen.
Doblar el ancho los cuadruplica. Por eso el ancho por defecto de este motor es
720 px —lo que ya usaba `extract_segment_frames.py`, medido como suficiente
para el vision LLM— y por eso el presupuesto es por duracion y no a ojo.

TODO ES FFMPEG Y STDLIB. Ni una dependencia nueva.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Nunca mas de dos frames por segundo, aunque el presupuesto lo pidiera. Por
# encima de eso se esta muestreando movimiento, no contenido.
MAX_FPS = 2.0

# Umbral de `select=gt(scene,...)`. 0.20 es el punto donde ffmpeg marca un corte
# real y no un cambio de luz o un paneo.
UMBRAL_ESCENA = 0.20

# Por debajo de estas escenas detectadas el tramo es estatico (plano fijo de
# entrevista, prompter) y la deteccion no tiene de donde elegir: se cae a
# uniforme. NO es el presupuesto — es el piso que decide que motor manda.
MIN_ESCENAS = 8

# Miniatura en gris de LADO x LADO con la que se comparan dos frames, y umbral
# de diferencia media por pixel (0-255) por debajo del cual se consideran el
# mismo plano. A diferencia de un hash perceptual dentro del frame, esto si
# distingue dos frames planos por su luminancia (un fundido, un cielo).
DEDUP_LADO = 16
DEDUP_UMBRAL = 2.0

# Ancho por defecto. Coincide con el que ya usaba extract_segment_frames.py.
ANCHO = 720

_RE_PTS = re.compile(r"pts_time:([0-9.]+)")


def presupuesto(dur_sec: float) -> int:
    """Cuantos frames merece un tramo por lo que dura.

    Los numeros salen de lo que cuesta describir un tramo, no de una formula:
    por debajo de 10 s no hay dos momentos distintos que contar, y por encima de
    dos minutos el vision LLM ya no mejora su descripcion con mas imagenes.
    """
    d = max(0.0, float(dur_sec))
    if d <= 0:
        return 1
    if d < 10:
        return 1
    if d < 30:
        return 3
    if d < 60:
        return 5
    if d < 120:
        return 8
    return 12


def indices_repartidos(total: int, n: int) -> list[int]:
    """Indices de `n` elementos repartidos entre `total`, con el primero y el
    ultimo siempre dentro."""
    if n >= total:
        return list(range(total))
    if n <= 1:
        return [0]
    return [round(i * (total - 1) / (n - 1)) for i in range(n)]


def repartir(cands: list[dict], n: int) -> list[dict]:
    """Deja `n` candidatos repartidos y BORRA el JPEG de los que se van.

    El borrado no es limpieza cosmetica: si un frame descartado se queda en el
    directorio, el consumidor lo encuentra con su glob y lo describe igual.
    """
    if n >= len(cands):
        return _reindexar(cands)
    elegidos = [cands[i] for i in indices_repartidos(len(cands), n)]
    _borrar(c for c in cands if c not in elegidos)
    return _reindexar(elegidos)


def miniaturas(paths: list[Path]) -> list[bytes]:
    """Una pasada de ffmpeg sobre la secuencia y una miniatura gris por frame.

    Devuelve [] ante cualquier duda —error de ffmpeg, nombres que no forman
    secuencia, bytes que no cuadran— para que el llamador no deduplique.
    """
    if not paths:
        return []
    paths = [Path(p) for p in paths]
    m = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", paths[0].name)
    if m is None:
        return []
    prefijo, digitos, ext = m.group(1), m.group(2), m.group(3)
    patron = str(paths[0].parent / f"{prefijo}%0{len(digitos)}d{ext}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-start_number", str(int(digitos)),
        "-i", patron,
        "-vf", f"scale={DEDUP_LADO}:{DEDUP_LADO},format=gray",
        "-f", "rawvideo", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    trozo = DEDUP_LADO * DEDUP_LADO
    datos = r.stdout
    if len(datos) != trozo * len(paths):
        return []
    return [datos[i * trozo:(i + 1) * trozo] for i in range(len(paths))]


def delta(a: bytes, b: bytes) -> float:
    """Diferencia media por pixel (0-255) entre dos miniaturas.

    Longitudes distintas devuelven infinito: ante un decode raro, dos frames se
    tratan como distintos. Equivocarse hacia conservar es barato; hacia tirar,
    no.
    """
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def deduplicar(cands: list[dict],
               umbral: float = DEDUP_UMBRAL) -> tuple[list[dict], int]:
    """Tira los casi identicos al ULTIMO CONSERVADO, no al anterior.

    Comparar contra el anterior deja pasar la deriva: veinte frames que cambian
    poquisimo cada uno acaban siendo otra imagen y ninguno se tira. Contra el
    ultimo conservado, la cadena se corta cuando el plano ya cambio de verdad.
    """
    if len(cands) <= 1:
        return _reindexar(cands), 0
    thumbs = miniaturas([Path(c["path"]) for c in cands])
    return _deduplicar_con(cands, thumbs, umbral)


def _deduplicar_con(cands: list[dict], thumbs: list[bytes],
                    umbral: float = DEDUP_UMBRAL) -> tuple[list[dict], int]:
    if len(thumbs) != len(cands) or len(cands) <= 1:
        return _reindexar(cands), 0
    quedan = [cands[0]]
    ultimo = thumbs[0]
    caen: list[dict] = []
    for cand, thumb in zip(cands[1:], thumbs[1:]):
        if delta(thumb, ultimo) <= umbral:
            caen.append(cand)
        else:
            quedan.append(cand)
            ultimo = thumb
    _borrar(caen)
    return _reindexar(quedan), len(caen)


def extraer_en_tiempos(video: str, out_dir: Path, tiempos: list[float],
                       *, ancho: int = ANCHO, motivo: str = "cue",
                       prefijo: str = "q") -> list[dict]:
    """Un frame exacto en cada tiempo pedido (segundos absolutos del clip)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for t in sorted(set(round(float(x), 3) for x in tiempos)):
        destino = out_dir / f"{prefijo}{len(out):04d}.jpg"
        if _extraer_uno(video, t, destino, ancho):
            out.append({"indice": len(out), "t_sec": t,
                        "path": str(destino), "motivo": motivo})
    return out


def extraer_uniforme(video: str, out_dir: Path, inicio: float, fin: float,
                     n: int, *, ancho: int = ANCHO,
                     prefijo: str = "c") -> list[dict]:
    """`n` frames repartidos por igual en [inicio, fin]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = max(0.0, float(fin) - float(inicio))
    n = max(1, int(n))
    if n == 1:
        tiempos = [inicio + dur / 2.0]
    else:
        paso = dur / (n - 1) if dur > 0 else 0.0
        tiempos = [inicio + i * paso for i in range(n)]
    out: list[dict] = []
    for t in tiempos:
        destino = out_dir / f"{prefijo}{len(out):04d}.jpg"
        if _extraer_uno(video, t, destino, ancho):
            out.append({"indice": len(out), "t_sec": round(t, 3),
                        "path": str(destino), "motivo": "uniforme"})
    return out


def extraer_escenas(video: str, out_dir: Path, inicio: float, fin: float,
                    *, ancho: int = ANCHO, umbral: float = UMBRAL_ESCENA,
                    prefijo: str = "c") -> list[dict]:
    """Primer frame mas cada cambio de escena del rango, SIN tope.

    Sin tope a proposito: capar la deteccion con `-frames:v` conserva los
    primeros cortes y tira la cola del tramo. Se detecta todo y se recorta
    despues, que es lo unico que garantiza cobertura de punta a punta.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    patron = str(out_dir / f"{prefijo}%04d.jpg")
    vf = (f"select='eq(n\\,0)+gt(scene\\,{umbral})',"
          f"scale={ancho}:-2,showinfo")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-y",
        "-ss", f"{float(inicio):.3f}", "-to", f"{float(fin):.3f}",
        "-i", video, "-vf", vf, "-vsync", "vfr", "-q:v", "3", patron,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    tiempos = [round(float(inicio) + float(m.group(1)), 3)
               for m in _RE_PTS.finditer(r.stderr)]
    archivos = sorted(out_dir.glob(f"{prefijo}*.jpg"))
    out: list[dict] = []
    for i, p in enumerate(archivos):
        t = tiempos[i] if i < len(tiempos) else float(inicio)
        out.append({"indice": i, "t_sec": t, "path": str(p),
                    "motivo": "primer-frame" if i == 0 else "escena"})
    return out


def elegir_frames(video: str, out_dir: Path, inicio: float, fin: float, *,
                  motor: str = "auto", tope: int | None = None,
                  cues: list[float] | None = None, ancho: int = ANCHO,
                  dedup: bool = True) -> tuple[list[dict], dict]:
    """Los frames de un tramo y el acta de como se eligieron.

    `motor` es "escena" (con caida a uniforme si el tramo es estatico) o
    "uniforme" (directo, sin detectar). El acta que se devuelve lleva SIEMPRE
    con que motor se acabo eligiendo, cuantos candidatos habia, cuantos cayeron
    por duplicados y si hubo caida. Un consumidor que no sepa como se eligieron
    sus frames no puede juzgar lo que describe.
    """
    inicio, fin = float(inicio), float(fin)
    dur = max(0.0, fin - inicio)
    if tope is None:
        tope = presupuesto(dur)
    tope = max(1, int(tope))

    pedidos = list(cues or [])
    dentro = [t for t in pedidos if inicio <= t <= fin]
    fuera = len(pedidos) - len(dentro)
    frames_cue = (extraer_en_tiempos(video, out_dir, dentro, ancho=ancho)
                  if dentro else [])

    # Los cues se descuentan del tope ANTES de elegir. Si por si solos lo
    # llenan, no se detecta nada mas: lo que el habla senala manda.
    resto = max(0, tope - len(frames_cue))
    acta = {"motor": "solo-cues", "candidatos": 0, "duplicados": 0,
            "cues": len(frames_cue), "cues_fuera": fuera,
            "elegidos": len(frames_cue), "caida": False, "tope": tope}
    if resto == 0:
        return _reindexar(sorted(frames_cue, key=lambda f: f["t_sec"])), acta

    if motor == "uniforme":
        elegidos = extraer_uniforme(video, out_dir, inicio, fin,
                                    min(resto, _tope_por_fps(dur, resto)),
                                    ancho=ancho)
        n_dup = 0
        if dedup:
            elegidos, n_dup = deduplicar(elegidos)
        acta.update(motor="uniforme", candidatos=len(elegidos) + n_dup,
                    duplicados=n_dup)
    else:
        cands = extraer_escenas(video, out_dir, inicio, fin, ancho=ancho)
        if len(cands) >= MIN_ESCENAS:
            n_dup = 0
            if dedup:
                cands, n_dup = deduplicar(cands)
            elegidos = repartir(cands, resto)
            acta.update(motor="escena", candidatos=len(cands) + n_dup,
                        duplicados=n_dup)
        else:
            # Tramo estatico: la deteccion no tiene de donde elegir. Se dice.
            _borrar(cands)
            elegidos = extraer_uniforme(video, out_dir, inicio, fin,
                                        min(resto, _tope_por_fps(dur, resto)),
                                        ancho=ancho)
            n_dup = 0
            if dedup:
                elegidos, n_dup = deduplicar(elegidos)
            acta.update(motor="uniforme", candidatos=len(cands),
                        duplicados=n_dup, caida=True)

    todos = sorted([*frames_cue, *elegidos], key=lambda f: f["t_sec"])
    acta["elegidos"] = len(todos)
    return _reindexar(todos), acta


def _tope_por_fps(dur: float, n: int) -> int:
    """Nunca mas de MAX_FPS frames por segundo de tramo."""
    if dur <= 0:
        return 1
    return max(1, min(n, int(round(MAX_FPS * dur))))


def _extraer_uno(video: str, t: float, destino: Path, ancho: int) -> bool:
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, float(t)):.3f}", "-i", video,
        "-frames:v", "1", "-vf", f"scale={ancho}:-2", "-q:v", "3",
        str(destino),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception:
        return False
    return (r.returncode == 0 and destino.exists()
            and destino.stat().st_size > 0)


def _borrar(cands) -> None:
    for c in cands:
        try:
            Path(c["path"]).unlink()
        except OSError:
            pass


def _reindexar(cands: list[dict]) -> list[dict]:
    for i, c in enumerate(cands):
        c["indice"] = i
    return cands
