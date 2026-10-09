"""Engine-wide defaults. Per-project overrides go in <disk>/.cinema_assistant/project.py if needed."""

VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts",
    ".braw", ".r3d", ".ari", ".arx", ".dng",
    ".prores", ".dnxhd", ".dnxhr",
}

AUDIO_EXTS = {
    ".wav", ".bwf", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg",
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".cr2", ".cr3",
    ".nef", ".arw", ".raf", ".rw2", ".orf", ".dng",
}

PROJECT_EXTS = {
    ".prproj", ".drp", ".fcpxml", ".xml", ".aaf", ".edl", ".otio",
}

# Extensiones que son video Y foto segun el caso. `.dng` es el unico hoy: una
# carpeta de .dng numerados es una toma CinemaDNG (Blackmagic, drones en modo
# RAW cine), pero un .dng suelto es una foto RAW. Por extension no hay forma de
# distinguirlos, asi que `file_kind()` los deja en 'video' y la decision real la
# toma `lib.classify.tras_probe()` con la duracion del probe delante.
AMBIGUOS_VIDEO_IMAGEN = VIDEO_EXTS & IMAGE_EXTS

SKIP_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_PREFIXES = ("._",)
# "VOZ CLAUDE" es la voz sintetica del asistente (Piper), que vive junto al
# material por decision de Victor (Asistente, 2026-09-28). Si se indexara, el
# pipeline la tomaria por una tercera cadena de lavalier e intentaria
# sincronizarla contra las camaras.
# "Exports" son los renders del editor, no material (Asistente, 2026-09-29:
# V.1.0.mov entro al indice como un clip mas, rumbo a Whisper y al B-roll).
# Se comparan sin mayusculas: el disco no las distingue y las carpetas cambian
# de grafia entre un dia y otro.
SKIP_DIRS = {".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".cinema_assistant",
             "VOZ CLAUDE", "Exports"}

HASH_HEAD_BYTES = 4 * 1024 * 1024
HASH_TAIL_BYTES = 4 * 1024 * 1024

FFPROBE_TIMEOUT_SEC = 60
MEDIAINFO_TIMEOUT_SEC = 60

DEFAULT_WORKERS = 4
PROGRESS_EVERY = 50
