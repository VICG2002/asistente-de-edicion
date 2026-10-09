"""Cast del proyecto: quien sale, y con que alias lo llaman en el audio.

POR QUE EXISTE (auditoria 2026-07-31)
-------------------------------------
`bin/enrich_curated_segments.py` llevaba la whitelist de personajes escrita
dentro del codigo: 26 nombres de personas reales del rodaje de JILOTEPEC, con
sus apodos. Dos problemas a la vez, y los dos serios:

1. PRIVACIDAD. Son personas reales. El motor se publica; ellas no dieron
   consentimiento para aparecer en un repositorio abierto. Nombre y apodo juntos
   son mas identificativos que el nombre solo.

2. FUNCIONAL. Es un hardcode de proyecto de la misma familia que el
   `--project-prefix` con default="ESCALANDO MEXICO/": en el rodaje de otra
   persona esa whitelist no casa con nadie, la deteccion de personajes devuelve
   vacio y el paso termina bien. Trabajo cero con exit 0, otra vez.

La regla de capas del asistente ya decia donde va esto: los DATOS por proyecto
viven en `<disco>/.cinema_assistant/`, nunca en el motor. El motor trae la
LOGICA de deteccion; el reparto es de cada rodaje.

FORMATO
-------
`<disco>/.cinema_assistant/cast.json`. El canonico es el que ya usaba
`bin/attribute_faces_via_transcript.py` — un objeto nombre -> alias:

    {
      "cast": {
        "Fulano": ["fulano", "fulanito"],
        "Mengana": ["mengana"]
      },
      "interviewers": ["Mengana"]
    }

Se acepta TAMBIEN la forma de lista, por si alguien la escribe asi:

    {"cast": [{"nombre": "Fulano", "alias": ["fulano"]}]}

Las dos se leen igual. Que existan dos formatos no es capricho: al centralizar
esto el 2026-07-31 habia ya un lector con el formato de objeto y un cast.json
posible en algun disco. Un formato nuevo incompatible habria dado justo el fallo
que este modulo viene a evitar — un cast.json que funciona con unos scripts y
con otros no, sin decirlo.

Los alias se comparan en minusculas y sin acentos contra el transcript. La clave
(o `nombre`) es lo que se escribe en los artefactos: markers, cronica, cast.

Como se llena: `bin/mine_candidate_names.py` propone candidatos desde los
transcripts; el editor confirma. Nunca se inventa un nombre.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path


def _normalizar(s: str) -> str:
    """Minusculas sin acentos — el transcript no es fiable con las tildes."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def ruta_cast(disk_root) -> Path:
    return Path(disk_root) / ".cinema_assistant" / "cast.json"


def load_cast(disk_root) -> dict[str, set[str]]:
    """Devuelve {nombre: {alias normalizados}}. Vacio si el proyecto no lo definio.

    Deliberadamente NO trae un cast por defecto: no existe un reparto universal,
    y un default heredado de otro rodaje es justo el fallo que se esta evitando.
    """
    p = ruta_cast(disk_root)
    if not p.exists():
        return {}
    try:
        datos = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"cast.json ilegible en {p}: {e}") from e

    bruto = datos.get("cast", {})
    fuera: dict[str, set[str]] = {}

    if isinstance(bruto, dict):          # formato canonico: {"Nombre": [alias]}
        pares = bruto.items()
    elif isinstance(bruto, list):        # formato lista: [{"nombre":…, "alias":…}]
        pares = ((e.get("nombre") or "", e.get("alias", []))
                 for e in bruto if isinstance(e, dict))
    else:
        raise ValueError(f"'cast' en {p} no es objeto ni lista: {type(bruto).__name__}")

    for nombre, alias in pares:
        nombre = (nombre or "").strip()
        if not nombre:
            continue
        s = {_normalizar(a) for a in (alias or []) if a}
        s.add(_normalizar(nombre))
        fuera[nombre] = {a for a in s if a}
    return fuera


def load_interviewers(disk_root) -> set[str]:
    """Quien hace las preguntas: esta detras de camara y NO es el sujeto del clip.

    Vacio si el proyecto no lo declara — y vacio es seguro: simplemente no se
    excluye a nadie del SUJETO.

    Vivia hardcodeado como `INTERVIEWERS = {...}` en dos archivos
    (reformat_descriptions.py y clean_character_identity.py), con nombres reales.
    El saneo de la auditoria 2026-07-31 los sustituyo por seudonimos y el set dejo
    de casar con lo que hay en el manifest: el filtro no excluia a nadie, en
    silencio. Su sitio es el cast.json del proyecto.
    """
    p = ruta_cast(disk_root)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("interviewers", []))
    except (json.JSONDecodeError, OSError):
        return set()


def load_protagonist(disk_root) -> str:
    """Sujeto por defecto de una entrevista sin personaje detectado.

    Cadena vacia si el proyecto no lo declara. Quien llama debe tratar el vacio
    como "sujeto desconocido" y NO inventar un nombre: este valor se ESCRIBE en
    clip_descriptions, asi que un default equivocado contamina el manifest con el
    protagonista de otro rodaje.

    Vivia como `PROJECT_PROTAGONIST = "<nombre real>"` en reformat_descriptions.py.
    """
    p = ruta_cast(disk_root)
    if not p.exists():
        return ""
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("protagonist") or "").strip()
    except (json.JSONDecodeError, OSError):
        return ""


def aviso_sin_cast(disk_root, paso: str) -> str:
    """Mensaje para cuando no hay cast. Es un AVISO, no un error.

    Un proyecto puede no tener cast declarado y aun asi procesarse: simplemente
    no se atribuyen personajes. Lo que no puede pasar es que no se diga.
    """
    return (
        f"\n  AVISO ({paso}): no hay cast declarado para este proyecto.\n"
        f"  Los tramos se enriquecen SIN atribucion de personajes.\n"
        f"  Para declararlo, crea {ruta_cast(disk_root)}:\n"
        '      {"cast": [{"nombre": "Fulano", "alias": ["fulano", "apodo"]}]}\n'
        "  Candidatos desde los transcripts:  python3 bin/mine_candidate_names.py --root <disco>\n"
    )
