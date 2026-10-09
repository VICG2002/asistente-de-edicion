"""La forma del proyecto: prefijo, sectores y mapa sector-audio. Una sola fuente.

POR QUE EXISTE (2026-08-05). Cada script del motor se contestaba por su cuenta
"donde empieza el material y como se llaman los sectores", y las respuestas
divergieron: `verify_coverage.py` lo deducia bien del manifest, mientras
`run_full_pipeline.sh` lo tenia escrito a mano —`LIKE 'ESCALANDO MEXICO/%'`,
`substr(rel_path, 18, ...)`, `for SECTOR in GUADALAJARA GUADALCAZAR ...`— y
`describe_segments_local_llm.py` lo sacaba con un regex atado al nombre del
proyecto, que en cualquier otro devuelve None sin avisar.

La deteccion de sectores de aqui es la de `verify_coverage.py:144-160`, portada
tal cual y no reinventada: es la unica version que ya estaba probada contra los
dos layouts reales (proyecto plano y proyecto bajo carpeta raiz).

REGLA QUE GOBIERNA EL MODULO: nunca elegir en silencio.

El prefijo NO se autodetecta. Se resuelve por cascada explicita —flag, luego
`project_config.json`— y si no hay ninguno se aborta con el mensaje de
`guards.exigir_prefix()` mas los candidatos calculados de ESE manifest, para que
el editor elija con datos delante. Ver `sugerir_prefix()` para el motivo: las dos
formas reales de proyecto son indistinguibles por estructura, y adivinarlas es
reintroducir el default silencioso que dejo a FANTASTICO COMICS a medias.

Lo que si se deduce solo es lo que NO es ambiguo: los sectores y el mapa
sector-audio, ambos derivables del manifest una vez que se sabe el prefijo.
"""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import tempfile
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402

# Un sector con menos de esto no es un sector: es una carpeta suelta, un
# "repetido/" o una prueba. Mismo criterio que verify_coverage.
SECTOR_MIN_CLIPS = 3

# Similitud minima para emparejar "GUADALCAZAR" con "AUDIOS/AUDIOS GUADALCAZAR 2".
SIMILITUD_MIN_AUDIO = 0.75


@dataclass
class FormaDeProyecto:
    """Lo que hay que saber del proyecto antes de tocar nada."""
    prefix: str                              # '' o 'MI PROYECTO/'
    sectores: list[str]
    mapa_audio: dict[str, list[str]] = field(default_factory=dict)
    origen_prefix: str = "detectado"         # flag | config | detectado
    origen_sectores: str = "manifest"        # manifest | config
    avisos: list[str] = field(default_factory=list)

    def like_prefijo(self) -> str:
        return f"{self.prefix}%"

    def like_de_sector(self, sector: str) -> str:
        return f"{self.prefix}{sector}/%"


class FormaAmbigua(Exception):
    """No se puede deducir el prefijo sin adivinar. El llamador debe abortar."""


# --------------------------------------------------------------------------
# Helpers de ruta
# --------------------------------------------------------------------------

def like_prefijo(prefix: str) -> str:
    return f"{prefix}%"


def like_de_sector(sector: str, prefix: str = "") -> str:
    return f"{prefix}{sector}/%"


def sector_de(rel_path: str, prefix: str = "") -> str | None:
    """El sector al que pertenece un rel_path, o None si no cuelga del prefijo.

    Reemplaza a los `re.match(r"ESCALANDO MEXICO/([^/]+)", ...)` de
    describe_segments_local_llm.py:231 y enrich_curated_segments.py:92, que
    devuelven None en cualquier proyecto que no sea aquel."""
    if not rel_path:
        return None
    if prefix:
        if not rel_path.startswith(prefix):
            return None
        resto = rel_path[len(prefix):]
    else:
        resto = rel_path
    partes = [p for p in resto.split("/") if p]
    # Plano: el sector es la carpeta contenedora completa (parent_folder), que
    # es lo que usa verify_coverage. Con prefijo: la primera subcarpeta.
    if prefix:
        return partes[0] if len(partes) >= 2 else None
    return "/".join(partes[:-1]) if len(partes) >= 2 else None


def _normalizar(s: str) -> str:
    """Para comparar nombres de carpeta: sin acentos, sin 'AUDIOS', sin ruido."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = s.replace("AUDIOS", " ").replace("AUDIO", " ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


# --------------------------------------------------------------------------
# Config del proyecto
# --------------------------------------------------------------------------

def ruta_config(root: Path) -> Path:
    return Path(root) / ".cinema_assistant" / "project_config.json"


def leer_config(root: Path) -> dict:
    """Misma convencion que derive_roll.py:72 y los otros cinco lectores."""
    datos = _leer_config_estricto(root)
    return {} if datos is None else datos


def _leer_config_estricto(root: Path) -> dict | None:
    """El config, {} si no existe, o None si existe y no se deja leer como un
    objeto JSON. La diferencia importa al ESCRIBIR: leer_config devuelve {} en
    los dos casos, y fusionar sobre ese {} borraba el archivo del usuario."""
    p = ruta_config(root)
    if not p.exists():
        return {}
    try:
        datos = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return datos if isinstance(datos, dict) else None


def config_ilegible(root: Path) -> bool:
    """True si project_config.json existe y no es un objeto JSON valido."""
    return _leer_config_estricto(root) is None


def escribir_config(root: Path, cambios: dict) -> bool:
    """Fusiona claves en project_config.json. Nunca lo reescribe entero: es un
    archivo del usuario y tiene claves que este modulo no conoce.

    Si el archivo existe y no se deja leer (una coma de mas al editarlo a mano),
    NO se toca y se devuelve False. Hasta el 2026-10-05 se fusionaba sobre el {}
    que daba leer_config y el archivo quedaba solo con las claves nuevas: se
    perdian en silencio el skew de camaras, el vocabulario, las cadenas de
    lavalier (revision del PR #1). La escritura es atomica: .tmp y rename.
    """
    p = ruta_config(root)
    datos = _leer_config_estricto(root)
    if datos is None:
        return False
    datos.update(cambios)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".project_config.", suffix=".tmp",
                                   dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True
    except OSError:
        return False


def grupo_de_camara(parent_folder: str, rel_path: str, cfg: dict, *,
                    por_dirname: bool = False) -> str:
    """El grupo de camara de un clip: el declarado por prefijo, o su carpeta.

    La carpeta es la TARJETA, no la camara, y casi siempre coinciden. Cuando
    una tarjeta cambia de cuerpo a media jornada dejan de coincidir: en
    Asistente (2026-09-28) la tarjeta de `Video 02` grabo primero en la FX30 de
    Adrian (ASA_) y desde ~18:37 en la de Victor (VICG). Agrupada por carpeta,
    la cronologia le aplicaba a los clips de Adrian el skew de reloj de la
    camara de Victor, medido con los clips de Victor de esa misma carpeta, y
    nada lo decia.

    Se DECLARA en project_config.json, igual que las cadenas de lavalier:

        "camaras_por_prefijo": {"VICG": "Vic", "ASA_": "Ayan"}

    Sin la clave, el comportamiento es el de siempre.
    """
    nombre = posixpath.basename(rel_path or "")
    for prefijo, grupo in (cfg.get("camaras_por_prefijo") or {}).items():
        if prefijo and nombre.startswith(prefijo):
            return grupo
    if por_dirname:
        return posixpath.dirname(rel_path or "")
    return parent_folder


def tx_de_lavalier(rel_path: str, cfg: dict) -> str:
    """El TX que grabo un WAV de lavalier: el declarado, o su carpeta.

    Con un TX por carpeta la carpeta basta (`Audio izq/`). Un WAV suelto en la
    carpeta de audio ya no dice de que TX es: en Asistente (2026-09-29) los dos
    WAV del dia quedaron en `AUDIO/`, uno de cada TX. Se DECLARA por prefijo de
    rel_path y gana el mas largo, asi un archivo suelto puede corregir a su
    carpeta:

        "lavalier_tx": {"AUDIO/Audio izq/": "izq",
                        "AUDIO/00035_Wireless PRO.WAV": "izq"}

    Sin mayusculas: el disco no las distingue y las carpetas cambian de grafia.
    """
    rel = (rel_path or "").casefold()
    mejor, largo = "", -1
    for prefijo, tx in (cfg.get("lavalier_tx") or {}).items():
        if prefijo and rel.startswith(prefijo.casefold()) and len(prefijo) > largo:
            mejor, largo = str(tx), len(prefijo)
    if largo >= 0:
        return mejor
    return posixpath.basename(posixpath.dirname(rel_path or ""))


# --------------------------------------------------------------------------
# Deteccion
# --------------------------------------------------------------------------

def _primeros_componentes(conn: sqlite3.Connection) -> dict[str, int]:
    """Primer componente de cada rel_path de video, con su cuenta."""
    filas = conn.execute(
        "SELECT rel_path FROM clips WHERE file_kind='video' "
        "AND index_status='ok'").fetchall()
    out: dict[str, int] = {}
    for (rel,) in filas:
        if not rel:
            continue
        primero = rel.split("/")[0]
        if primero and primero != rel:      # tiene al menos una carpeta
            out[primero] = out.get(primero, 0) + 1
    return out


def _subcarpetas_con_material(conn: sqlite3.Connection, raiz: str) -> int:
    """Cuantas subcarpetas distintas de <raiz> tienen material propio."""
    pfx = f"{raiz}/"
    filas = conn.execute(
        "SELECT rel_path FROM clips WHERE file_kind='video' "
        "AND index_status='ok' AND rel_path LIKE ? || '%'", (pfx,)).fetchall()
    subs: set[str] = set()
    for (rel,) in filas:
        resto = rel[len(pfx):]
        partes = [p for p in resto.split("/") if p]
        if len(partes) >= 2:
            subs.add(partes[0])
    return len(subs)


def sugerir_prefix(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Candidatos a prefijo, cada uno con su motivo. NO elige: sugiere.

    POR QUE NO AUTODETECTA (decision del 2026-08-05). La primera version de este
    modulo deducia el prefijo sola. Se cayo contra el primer fixture: en un
    proyecto plano de dos camaras —`Video/FX30_A/`, `Video/FX30_B/`— habria
    devuelto `Video/` como carpeta raiz del proyecto, porque tiene un solo
    primer componente con dos subcarpetas con material. Exactamente la forma que
    tienen FCC, Fantastico y Morsa.

    Las dos formas reales son indistinguibles por estructura:

      PLANO      Video/FX30_A/C0001.MP4      -> prefix ''
      CON RAIZ   ESCALANDO MEXICO/JILOTEPEC/ -> prefix 'ESCALANDO MEXICO/'

    Los audios tampoco desempatan: en ESCALANDO MEXICO cuelgan de `AUDIOS/`, en
    la raiz del disco y fuera de la carpeta del proyecto, asi que "hay dos
    carpetas de primer nivel con material" es cierto en los dos layouts.

    La diferencia es semantica —si esa carpeta es el nombre del proyecto o el
    de un rol— y eso no esta en el manifest. Adivinarlo seria reintroducir el
    default silencioso que dejo a FANTASTICO COMICS a medias, que es justo lo
    que la auditoria del 2026-07-31 quito de los cinco scripts.
    """
    sugerencias: list[tuple[str, str]] = []
    comps = _primeros_componentes(conn)
    if not comps:
        return sugerencias

    if len(comps) == 1:
        raiz = next(iter(comps))
        n_subs = _subcarpetas_con_material(conn, raiz)
        if n_subs >= 2:
            sugerencias.append((
                f"{raiz}/",
                f"todo el video cuelga de '{raiz}/', que tiene {n_subs} "
                f"subcarpetas con material — encaja con una carpeta raiz de "
                f"proyecto cuyos sectores son esas subcarpetas"))
        sugerencias.append((
            "",
            f"si '{raiz}' es una carpeta de ROL (Video, Material, Rushes) y no "
            f"el nombre del proyecto, el prefijo es vacio y los sectores son "
            f"las carpetas de dentro"))
    else:
        sugerencias.append((
            "",
            f"hay {len(comps)} carpetas de primer nivel con video "
            f"({', '.join(sorted(comps)[:4])}), asi que no hay una sola carpeta "
            f"raiz de proyecto"))
    return sugerencias


def _mensaje_sin_prefix(root: Path, conn: sqlite3.Connection) -> str:
    """El mensaje de exigir_prefix, mas las sugerencias de ESTE manifest."""
    from lib.guards import exigir_prefix

    partes = [exigir_prefix("proyecto")]
    sug = sugerir_prefix(conn)
    if sug:
        partes.append("\nEn ESTE manifest, los candidatos son:")
        for pfx, motivo in sug:
            partes.append(f"    --project-prefix {pfx!r}\n        {motivo}")
    muestra = [r[0] for r in conn.execute(
        "SELECT rel_path FROM clips WHERE file_kind='video' "
        "AND index_status='ok' LIMIT 4").fetchall()]
    if muestra:
        partes.append("\nAsi se ven tus rutas:")
        partes.extend(f"    {m}" for m in muestra)
    partes.append(
        f"\nCuando lo decidas, se escribe una sola vez y no vuelve a preguntar:\n"
        f'    "project_prefix": "<lo que elijas>"\n'
        f"  en {ruta_config(root)}")
    return "\n".join(partes)


def detectar_sectores(conn: sqlite3.Connection, prefix: str) -> list[str]:
    """Portado de verify_coverage.py:144-160, sin cambios de comportamiento."""
    if prefix:
        n0 = len(prefix) + 1
        sectores = [r[0] for r in conn.execute(
            f"SELECT DISTINCT substr(rel_path, {n0}, "
            f"instr(substr(rel_path,{n0}),'/')-1) "
            "FROM clips WHERE rel_path LIKE ? || '%' AND file_kind='video' "
            "AND index_status='ok' ORDER BY 1", (prefix,))]
    else:
        sectores = [r[0] for r in conn.execute(
            "SELECT DISTINCT parent_folder FROM clips WHERE file_kind='video' "
            "AND index_status='ok' AND parent_folder != '' ORDER BY 1")]

    reales = []
    for s in sectores:
        if not s:
            continue
        n = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE file_kind='video' "
            "AND index_status='ok' AND rel_path LIKE ?",
            (like_de_sector(s, prefix),)).fetchone()[0]
        if n >= SECTOR_MIN_CLIPS:
            reales.append(s)
    return reales


def carpetas_de_audio(conn: sqlite3.Connection) -> list[str]:
    """Carpetas que contienen audio externo, como rel_path de la carpeta."""
    filas = conn.execute(
        "SELECT DISTINCT parent_folder FROM clips WHERE file_kind='audio' "
        "AND index_status='ok' AND parent_folder != '' ORDER BY 1").fetchall()
    return [r[0] for r in filas if r[0]]


def inferir_mapa_audio(sectores: list[str],
                       carpetas: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Empareja cada sector con sus carpetas de audio por parecido de nombre.

    Sustituye a `bin/sector_video_audio_map.tsv`, que es un archivo del MOTOR
    con datos de UN proyecto — el mismo defecto de capas que ya se corrigio con
    los horneados. Lo inferido se imprime para que el editor lo confirme y se
    escribe al project_config; a partir de ahi manda el config.

    Devuelve (mapa, avisos). Un sector sin pareja no es un error: hay sectores
    que solo usan el audio embebido de la camara.
    """
    mapa: dict[str, list[str]] = {}
    avisos: list[str] = []
    if not sectores or not carpetas:
        return mapa, avisos

    norm_carpetas = [(c, _normalizar(Path(c).name)) for c in carpetas]
    for s in sectores:
        ns = _normalizar(Path(s).name)
        if not ns:
            continue

        # DOS NIVELES, Y EL FUERTE GANA. Con un solo umbral de similitud,
        # "JORNADA1" casaba tambien con "AUDIOS JORNADA2" y "AUDIOS JORNADA3":
        # difflib les da 0.875 porque solo cambia el ultimo caracter. Cada
        # sector acababa emparejado con el audio de TODOS los demas. En
        # ESCALANDO MEXICO no se veia —GUADALAJARA y MONTERREY no se parecen en
        # nada— pero DIA1/DIA2 o JORNADA1/JORNADA2 es como se nombra media
        # cobertura.
        #
        #   fuerte: el nombre del sector esta CONTENIDO en el de la carpeta.
        #           Cubre el exacto y el caso real del TSV, donde GUADALCAZAR
        #           tenia "AUDIOS GUADALCAZAR" y "AUDIOS GUADALCAZAR 2".
        #   debil:  parecido difuso. Solo se mira si no hubo ni un fuerte.
        fuertes, debiles = [], []
        for carpeta, nc in norm_carpetas:
            if not nc:
                continue
            if ns in nc or nc in ns:
                fuertes.append(carpeta)
            elif difflib.SequenceMatcher(None, ns, nc).ratio() >= SIMILITUD_MIN_AUDIO:
                debiles.append(carpeta)

        if fuertes:
            mapa[s] = sorted(fuertes)
        elif debiles:
            mapa[s] = sorted(debiles)
            avisos.append(
                f"'{s}' se emparejo con {len(debiles)} carpeta(s) de audio solo "
                f"por parecido de nombre ({', '.join(debiles)}). Confirmalo en "
                f"'mapa_audio' del project_config.json.")

    sin_pareja = [s for s in sectores if s not in mapa]
    if sin_pareja:
        avisos.append(
            f"{len(sin_pareja)} sector(es) sin carpeta de audio propia "
            f"({', '.join(sin_pareja[:4])}{'...' if len(sin_pareja) > 4 else ''}). "
            "Si es correcto —usan el audio de camara— no hay nada que hacer; si "
            "no, declara 'mapa_audio' en project_config.json.")
    return mapa, avisos


# --------------------------------------------------------------------------
# Entrada principal
# --------------------------------------------------------------------------

def detectar(root: Path, conn: sqlite3.Connection | None = None, *,
             prefix_flag: str | None = None,
             persistir: bool = True) -> FormaDeProyecto:
    """La forma del proyecto, resuelta por cascada explicita.

    Prefijo:  --project-prefix  >  project_config.json  >  manifest  >  abortar
    Sectores: project_config.json  >  manifest

    `persistir` escribe lo detectado al project_config para que la segunda
    corrida no vuelva a deducir nada. Los tests lo apagan.
    """
    root = Path(root)
    cerrar = False
    if conn is None:
        db = root / ".cinema_assistant" / "manifest.sqlite"
        if not db.exists():
            raise FileNotFoundError(f"Manifest no encontrado: {db}")
        conn = manifest.conectar(str(db))
        cerrar = True

    try:
        cfg = leer_config(root)
        avisos: list[str] = []
        nuevo_en_config: dict = {}
        if config_ilegible(root):
            avisos.append(
                f"{ruta_config(root)} existe pero no es un objeto JSON valido: "
                f"se ignora entero y NO se reescribe. Arreglalo a mano (una coma "
                f"de mas, una comilla) o lo que declaraste ahi no se aplica.")

        # --- prefijo: flag > config > abortar con sugerencias ---
        if prefix_flag is not None:
            prefix, origen = prefix_flag, "flag"
            if "project_prefix" not in cfg:
                nuevo_en_config["project_prefix"] = prefix
        elif "project_prefix" in cfg:
            prefix, origen = cfg["project_prefix"], "config"
        else:
            raise FormaAmbigua(_mensaje_sin_prefix(root, conn))

        # --- sectores ---
        if cfg.get("sectores"):
            sectores = list(cfg["sectores"])
            origen_sec = "config"
            en_disco = set(detectar_sectores(conn, prefix))
            no_declarados = sorted(en_disco - set(sectores))
            if no_declarados:
                # Que "se me olvido un sector" sea imposible de pasar por alto:
                # es exactamente como ESCALANDO MEXICO quedo con seis sectores
                # sin sync ni markers.
                avisos.append(
                    f"HAY MATERIAL EN SECTORES NO DECLARADOS: "
                    f"{', '.join(no_declarados)}. El pipeline NO los va a tocar. "
                    f"Anadelos a 'sectores' en project_config.json o borra esa "
                    f"clave para que se detecten solos.")
        else:
            sectores = detectar_sectores(conn, prefix)
            origen_sec = "manifest"

        # --- mapa sector -> audio ---
        if cfg.get("mapa_audio"):
            mapa = {k: (v if isinstance(v, list) else [v])
                    for k, v in cfg["mapa_audio"].items()}
        else:
            mapa, av = inferir_mapa_audio(sectores, carpetas_de_audio(conn))
            avisos.extend(av)
            if mapa:
                nuevo_en_config["mapa_audio"] = mapa
                avisos.append(
                    "mapa sector-audio inferido por parecido de nombre. "
                    "Revisalo: va escrito en project_config.json.")

        if persistir and nuevo_en_config:
            if not escribir_config(root, nuevo_en_config):
                avisos.append(
                    f"no se pudo escribir {ruta_config(root)} — la deteccion se "
                    f"repetira en cada corrida.")

        return FormaDeProyecto(prefix=prefix, sectores=sectores, mapa_audio=mapa,
                               origen_prefix=origen, origen_sectores=origen_sec,
                               avisos=avisos)
    finally:
        if cerrar:
            conn.close()
