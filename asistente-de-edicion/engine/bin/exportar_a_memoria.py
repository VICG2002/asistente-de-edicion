#!/usr/bin/env python3
"""Devuelve a la memoria creativa lo que el asistente de edicion aprendio de un proyecto.

Es el paso 13b del playbook (`~/memoria-asistente-edicion/metodologia/pasos-a-seguir.md`),
simetrico al paso 0a: si el 0a lee la memoria antes de empezar, el 13b la alimenta al cerrar.

Hasta el 2026-07-28 este paso no existia. El asistente proceso cinco proyectos entre junio y
julio de 2026 y nada llego a la boveda: el playbook abria consultando la memoria y cerraba sin
volver a ella. La auditoria de esa fecha lo detecto.

NO escribe en la memoria final. Escribe una PROPUESTA en `_cambios/pendientes/`, que es la
regla 2 de la boveda ("el sistema propone, tu apruebas"). Para aplicarla:

    ~/memoria-creativa/ingesta/aprobar.sh <id>

Uso:
    python3 bin/exportar_a_memoria.py "/Volumes/MI_DISCO/Diez50/The Avalanches"
    python3 bin/exportar_a_memoria.py <ruta> --dry-run     # muestra sin escribir
    python3 bin/exportar_a_memoria.py <ruta> --force       # reescribe aunque ya exista

Lee el manifest en SOLO LECTURA (URI immutable). Nunca modifica el proyecto.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path.home() / "memoria-creativa"
PENDIENTES = VAULT / "_cambios" / "pendientes"
APLICADOS = VAULT / "_cambios" / "aplicados"


# --------------------------------------------------------------------------- utilidades


def slugify(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-+", "-", s)


def abrir_solo_lectura(db: Path) -> sqlite3.Connection:
    """Abre el manifest sin poder tocarlo ni recuperar journals colgados."""
    return sqlite3.connect(f"file:{db}?immutable=1", uri=True)


def tablas(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


def contar(con: sqlite3.Connection, tabla: str, existentes: set[str]) -> int | None:
    """None si la tabla no existe en este proyecto (los manifests varian entre rodajes)."""
    if tabla not in existentes:
        return None
    try:
        return con.execute(f"select count(*) from {tabla}").fetchone()[0]
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- lectura


def leer_manifest(db: Path) -> dict:
    con = abrir_solo_lectura(db)
    try:
        t = tablas(con)
        datos: dict = {"tablas": sorted(t)}

        # material por extension
        material = []
        if "clips" in t:
            for ext, n, horas in con.execute(
                "select lower(ext), count(*), round(coalesce(sum(duration_sec),0)/3600.0, 2) "
                "from clips group by lower(ext) order by count(*) desc"
            ):
                material.append({"ext": ext, "archivos": n, "horas": horas})
            datos["camaras"] = [
                r[0]
                for r in con.execute(
                    "select distinct camera_model from clips "
                    "where camera_model is not null and camera_model != ''"
                )
            ]
            rango = con.execute(
                "select min(creation_time), max(creation_time) from clips "
                "where creation_time is not null and creation_time != ''"
            ).fetchone()
            datos["rango_grabacion"] = {"desde": rango[0], "hasta": rango[1]} if rango else None
        datos["material"] = material

        for clave, tabla in [
            ("clips_indexados", "clips"),
            ("transcripciones", "transcript_quality"),
            ("pares_sync", "audio_sync_pairs"),
            ("pares_lavalier", "lavalier_pairs"),
            ("preguntas_curadas", "question_segments"),
            ("tramos_curados", "clip_curated_segments"),
            ("identidades_cara", "face_identities"),
            ("identidades_voz", "voice_catalog"),
            ("eventos_sonido", "sound_events"),
        ]:
            datos[clave] = contar(con, tabla, t)

        # preguntas curadas: son la capa mas util para la memoria
        datos["muestra_preguntas"] = []
        if "question_segments" in t:
            cols = {r[1] for r in con.execute("pragma table_info(question_segments)")}
            campo = "question_short" if "question_short" in cols else "question_text"
            if campo in cols:
                datos["muestra_preguntas"] = [
                    r[0]
                    for r in con.execute(
                        f"select distinct {campo} from question_segments "
                        f"where {campo} is not null and length({campo}) > 12 limit 12"
                    )
                ]
        return datos
    finally:
        con.close()


def leer_extras(ca: Path) -> dict:
    """cast.json, project_config.json y el inventario de informes legibles."""
    extras: dict = {"cast": None, "config": None, "informes": []}
    for nombre, clave in [("cast.json", "cast"), ("project_config.json", "config")]:
        f = ca / nombre
        if f.is_file():
            try:
                extras[clave] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"AVISO: no se pudo leer {nombre}: {e}", file=sys.stderr)
    reports = ca / "reports"
    if reports.is_dir():
        for f in sorted(reports.glob("*.md")):
            if f.name.startswith("._"):  # basura de AppleDouble en discos exFAT
                continue
            extras["informes"].append({"archivo": f.name, "bytes": f.stat().st_size})
    return extras


def nombres_del_cast(cast) -> list[str]:
    """cast.json tiene dos formas segun el proyecto: lista plana o dict por persona."""
    if not cast:
        return []
    if isinstance(cast, dict):
        if isinstance(cast.get("cast"), list):
            return [x for x in cast["cast"] if isinstance(x, str)]
        return [k for k in cast if not k.startswith("_")]
    if isinstance(cast, list):
        return [x for x in cast if isinstance(x, str)]
    return []


# --------------------------------------------------------------------------- idempotencia


def huella(db: Path, datos: dict) -> str:
    """Identifica el estado del manifest. Si no cambio, no se vuelve a proponer."""
    base = json.dumps(
        {
            "size": db.stat().st_size,
            "clips": datos.get("clips_indexados"),
            "sync": datos.get("pares_sync"),
            "preguntas": datos.get("preguntas_curadas"),
            "transcripciones": datos.get("transcripciones"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def destino_en_boveda(slug: str, nombre: str, carpeta: Path | None = None) -> str:
    """Resuelve el destino contra _registro.json, que es la fuente de verdad de las rutas.

    Tres pasadas, de mas fiable a menos:
      1. Por carpeta de material fuente. Es la unica clave que no depende de como se
         escriba el nombre ('FilmClubCafé' en disco contra slug 'film-club-cafe').
      2. Por slug exacto.
      3. Por nombre normalizado.

    Sin esto el destino se calculaba por slug y no coincidia con las carpetas reales
    (slug 'mas-alla-del-balon' contra carpeta 'Balon'), asi que aprobar.sh rechazaba
    siempre la propuesta por destino inexistente.
    """
    registro = VAULT / "_registro.json"
    try:
        proyectos = json.loads(registro.read_text(encoding="utf-8")).get("proyectos", [])
    except (OSError, json.JSONDecodeError):
        return f"Diez50/Diez50-Coberturas/{slug}/"

    if carpeta:
        objetivo = str(carpeta).rstrip("/")
        for p in proyectos:
            if (p.get("carpeta_material_fuente") or "").rstrip("/") == objetivo and p.get("ruta_memoria"):
                return p["ruta_memoria"]
    for p in proyectos:
        if p.get("slug") == slug and p.get("ruta_memoria"):
            return p["ruta_memoria"]
    objetivo = slugify(nombre)
    for p in proyectos:
        if slugify(p.get("nombre", "")) == objetivo and p.get("ruta_memoria"):
            return p["ruta_memoria"]
    return f"Diez50/Diez50-Coberturas/{slug}/"


def ya_propuesto(slug: str, hh: str) -> Path | None:
    for carpeta in (PENDIENTES, APLICADOS):
        if not carpeta.is_dir():
            continue
        for f in carpeta.glob(f"*_cobertura_{slug}.json"):
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("huella_manifest") == hh:
                    return f
            except (OSError, json.JSONDecodeError):
                continue
    return None


# --------------------------------------------------------------------------- salida


def linea_material(datos: dict) -> str:
    partes = []
    for m in datos.get("material", []):
        if m["ext"] in ("xml",):  # sidecars de Sony, no son material
            continue
        txt = f"{m['archivos']} {m['ext'].upper()}"
        if m["horas"]:
            txt += f" ({m['horas']} h)"
        partes.append(txt)
    return ", ".join(partes) if partes else "sin desglose"


def redactar_md(nombre: str, slug: str, proyecto: Path, datos: dict, extras: dict, hh: str) -> str:
    hoy = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    cast = nombres_del_cast(extras["cast"])

    L = [
        f'# Propuesta: ficha de cobertura "{nombre}"',
        "",
        f"**Fecha**: {hoy} · **Origen**: asistente de edicion (paso 13b, automatico)",
        "",
        "## Que se propone",
        "",
        f"Crear o actualizar la ficha de **{nombre}** en la boveda. Material fuente en",
        f"`{proyecto}`.",
        "",
        "## Datos duros (leidos del manifest)",
        "",
        f"- **Material**: {linea_material(datos)}",
    ]
    if datos.get("camaras"):
        L.append(f"- **Camaras**: {', '.join(datos['camaras'])}")
    if datos.get("rango_grabacion") and datos["rango_grabacion"].get("desde"):
        r = datos["rango_grabacion"]
        L.append(f"- **Rango de grabacion**: {r['desde']} a {r['hasta']}")

    for etiqueta, clave in [
        ("Registros indexados", "clips_indexados"),
        ("Transcripciones", "transcripciones"),
        ("Pares de sincronia", "pares_sync"),
        ("Preguntas curadas", "preguntas_curadas"),
        ("Tramos curados", "tramos_curados"),
        ("Identidades por voz", "identidades_voz"),
        ("Identidades por cara", "identidades_cara"),
    ]:
        if datos.get(clave):
            L.append(f"- **{etiqueta}**: {datos[clave]}")

    if cast:
        L += ["", "## Personas detectadas", "",
              "Auto-identificadas por el asistente. Verificar grafias antes de subtitular.", "",
              ", ".join(cast[:30]) + ("..." if len(cast) > 30 else "")]

    if datos.get("muestra_preguntas"):
        L += ["", "## Muestra de preguntas curadas", ""]
        L += [f"- {q}" for q in datos["muestra_preguntas"]]

    if extras["informes"]:
        L += ["", "## Informes legibles en disco", "",
              "Son la capa mas util para redactar la ficha: cronicas de rodaje, cierres y",
              "propuestas de cast con evidencia literal.", ""]
        L += [f"- `.cinema_assistant/reports/{i['archivo']}` ({i['bytes'] // 1024} KB)"
              for i in extras["informes"]]

    L += [
        "",
        "## Por que via pendientes",
        "",
        "Regla 2 de la boveda: el sistema propone, Victor aprueba. El asistente no escribe",
        "en la memoria final. Para aplicar esta propuesta:",
        "",
        "```",
        f"~/memoria-creativa/ingesta/aprobar.sh {slug}",
        "```",
        "",
        f"Huella del manifest: `{hh}` (si no cambia, no se vuelve a proponer).",
        "",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------- principal


def main() -> int:
    global VAULT, PENDIENTES, APLICADOS

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proyecto", help="carpeta del proyecto (la que contiene .cinema_assistant)")
    ap.add_argument("--nombre", help="nombre legible (por defecto, el de la carpeta)")
    ap.add_argument("--vault", default=str(VAULT), help="raiz de la boveda")
    ap.add_argument("--dry-run", action="store_true", help="muestra la propuesta sin escribir")
    ap.add_argument("--force", action="store_true", help="propone aunque ya exista con la misma huella")
    args = ap.parse_args()

    VAULT = Path(args.vault).expanduser()
    PENDIENTES = VAULT / "_cambios" / "pendientes"
    APLICADOS = VAULT / "_cambios" / "aplicados"

    proyecto = Path(args.proyecto).expanduser()
    if not proyecto.is_dir():
        print(f"ERROR: no existe la carpeta {proyecto}", file=sys.stderr)
        return 2

    db = proyecto / ".cinema_assistant" / "manifest.sqlite"
    if not db.is_file():
        print(f"ERROR: no hay manifest en {db}\n"
              f"       Corre antes el pipeline del asistente sobre este proyecto.", file=sys.stderr)
        return 2

    nombre = args.nombre or proyecto.name
    slug = slugify(nombre)

    datos = leer_manifest(db)
    extras = leer_extras(proyecto / ".cinema_assistant")
    hh = huella(db, datos)

    previo = ya_propuesto(slug, hh)
    if previo and not args.force:
        print(f"Sin novedades: el manifest de '{nombre}' no ha cambiado desde {previo.name}.")
        print("Nada que proponer. Usa --force para reescribir de todos modos.")
        return 0

    cuerpo_md = redactar_md(nombre, slug, proyecto, datos, extras, hh)
    ahora = datetime.now(timezone.utc).astimezone()
    ts = ahora.strftime("%Y%m%d-%H%M%S")
    propuesta = {
        "id": f"{ts}_cobertura_{slug}",
        "tipo": "alta_proyecto",
        "creado_en": ahora.isoformat(),
        "origen": "cinema-assistant:exportar_a_memoria",
        "fuente_archivos": [str(db)],
        "destino": destino_en_boveda(slug, nombre, proyecto),
        "razon": (
            f"Cobertura '{nombre}' procesada por el asistente de edicion "
            f"({datos.get('clips_indexados') or 0} registros indexados, "
            f"{datos.get('pares_sync') or 0} pares de sincronia, "
            f"{datos.get('preguntas_curadas') or 0} preguntas curadas)."
        ),
        "confianza": 0.9,
        "huella_manifest": hh,
        "accion_propuesta": {
            "operacion": "crear_o_actualizar_ficha",
            "ficha": {
                "nombre": nombre,
                "slug": slug,
                "carpeta_material_fuente": str(proyecto),
                "manifest": str(db),
                "datos": datos,
                "cast": nombres_del_cast(extras["cast"]),
                "informes": extras["informes"],
            },
        },
        "aplicado_en": None,
    }

    if args.dry_run:
        print(cuerpo_md)
        print("\n--- propuesta JSON (no escrita) ---")
        print(json.dumps(propuesta, indent=2, ensure_ascii=False)[:1200] + "...")
        return 0

    if not PENDIENTES.is_dir():
        # NO es un fallo del pipeline de edicion: es que esta instalacion no usa
        # boveda. El paso 13b es opcional y personal — `~/memoria-creativa/` es
        # la boveda del autor del plugin, no algo que traiga la herramienta. Con
        # return 2 el orquestador lo trataba como error y el primer proyecto que
        # alguien cerrara terminaba en rojo por algo que no le incumbe.
        print(
            f"Paso 13b omitido: no hay boveda en {VAULT}.\n"
            f"  Si usas otra ruta:   --vault <ruta>\n"
            f"  Si no usas boveda:   este paso es opcional, no afecta la edicion.\n"
            f"  Para ver que se habria propuesto, sin escribir nada:  --dry-run",
            file=sys.stderr)
        return 0

    f_json = PENDIENTES / f"{ts}_cobertura_{slug}.json"
    f_md = PENDIENTES / f"{ahora.strftime('%Y-%m-%d')}-ficha-{slug}.md"
    f_json.write_text(json.dumps(propuesta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    f_md.write_text(cuerpo_md, encoding="utf-8")

    print(f"Propuesta escrita para '{nombre}':")
    print(f"  {f_json}")
    print(f"  {f_md}")
    print(f"\nAplicar con:  ~/memoria-creativa/ingesta/aprobar.sh {slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
