"""Classify files by extension and decide whether to index them."""

from pathlib import Path
from config import defaults


def file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in defaults.VIDEO_EXTS:
        return "video"
    if ext in defaults.AUDIO_EXTS:
        return "audio"
    if ext in defaults.IMAGE_EXTS:
        return "image"
    if ext in defaults.PROJECT_EXTS:
        return "project"
    return "other"


def tras_probe(kind: str, ext: str, duration_sec) -> str:
    """Segunda pasada de `file_kind()`, ya con el probe hecho.

    `.dng` esta a la vez en VIDEO_EXTS y en IMAGE_EXTS, y `file_kind()` mira
    video primero: una foto RAW suelta del drone entra como si fuera una toma.
    En IMODAE (2026-08-07) eso metio 9 fotos del DJI en el manifest como video,
    camino de aterrizar en la timeline de B-ROLL entre clips de verdad.

    La duracion es la señal que sobra para decidir: una toma CinemaDNG la trae,
    una foto no. Se aplica SOLO a las extensiones ambiguas — para el resto la
    clasificacion por extension es correcta y esta funcion no la toca.
    """
    if kind != "video" or ext.lower() not in defaults.AMBIGUOS_VIDEO_IMAGEN:
        return kind
    try:
        return "video" if float(duration_sec) > 0 else "image"
    except (TypeError, ValueError):
        return "image"


def should_skip(path: Path) -> bool:
    name = path.name
    if name in defaults.SKIP_FILENAMES:
        return True
    if any(name.startswith(p) for p in defaults.SKIP_PREFIXES):
        return True
    return False


_SKIP_DIRS_CASEFOLD = {d.casefold() for d in defaults.SKIP_DIRS}


def should_skip_dir(path: Path) -> bool:
    return path.name.casefold() in _SKIP_DIRS_CASEFOLD or path.name.startswith(".")


def is_media(path: Path) -> bool:
    return file_kind(path) in ("video", "audio", "image")
