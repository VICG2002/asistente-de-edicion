#!/usr/bin/env python3
"""Orquestador del pipeline. Declarativo, generico y verificable.

Sustituye a `run_full_pipeline.sh`, que llevaba roto desde el 2026-07-31: la
auditoria de ese dia hizo `--project-prefix` obligatorio en cinco scripts y el
orquestador nunca se actualizo, asi que **abortaba en su linea 110, antes de
indexar un solo audio**. Nadie lo vio porque ninguna prueba ejecutaba los `.sh`.

Ese no era el unico problema del shell, y todos son de la misma familia — cosas
que en bash salen caras y aqui son gratis:

  1. INVISIBLE AL LINTER. `lint_pipeline.py` solo leia `.py`, y dentro del .sh
     habia cinco hardcodes geograficos (`LIKE 'ESCALANDO MEXICO/%'`,
     `substr(rel_path, 18, ...)`, `for SECTOR in GUADALAJARA ...`).
  2. IMPOSIBLE DE PROBAR. No habia forma de ejercitar el orden de los pasos sin
     un disco de 2 TB montado. Ahora hay `--plan-only` y `--dry-run`.
  3. GUARDAS QUE MENTIAN. El `assert_grew` de bash comparaba contra un piso
     ABSOLUTO (1700 tramos), asi que en un disco con trabajo previo daba OK
     aunque la corrida no escribiera una sola fila. Aqui se mide el DELTA real.
  4. UN SOLO INTERPRETE. `python3` pelado no tiene cv2 ni resemblyzer, asi que
     ningun paso de identidad podia correr. Ahora cada paso declara que
     capacidad necesita y `lib.entorno` resuelve con cual se le llama.
  5. FALLOS TAPADOS CON `|| WARN`. Ocho pasos seguian adelante tras fallar,
     incluido el sync principal. Ahora cada paso declara `al_fallar`.

Uso:
    python3 bin/run_pipeline.py --root /Volumes/MI_DISCO
    python3 bin/run_pipeline.py --root <disco> --plan-only
    python3 bin/run_pipeline.py --root <disco> --desde sync-v2
    python3 bin/run_pipeline.py --root <disco> --solo transcribir-audios
    python3 bin/run_pipeline.py --root <disco> --listar

`CINEMA_EXEC_STUB=/ruta/a/un/stub` antepone ese ejecutable a cada paso en vez de
correrlo. Es lo que usa `tests/test_orquestador.py` para ejercitar el
orquestador entero —orden, abort contra warn, pasos saltados— en menos de un
segundo, sin disco, sin ffmpeg y sin Whisper.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
# La suite viaja solo con el repo del motor: build-plugin.sh no empaqueta
# tests/ y bootstrap.sh no la instala. Una Mac que instalo el plugin no la
# tiene, y eso no es un fallo del proyecto.
SUITE = ENGINE / "tests"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from lib import entorno, proyecto  # noqa: E402
from lib.guards import EXIT_NO_WORK  # noqa: E402
from lib import manifest  # noqa: E402

EXIT_PRECONDICION = 2
EXIT_PASO_FALLO = 4


# --------------------------------------------------------------------------
# Contexto y pasos
# --------------------------------------------------------------------------

@dataclass
class Contexto:
    root: Path
    forma: proyecto.FormaDeProyecto
    db: Path
    whisper_model: Path
    workers: int = 4
    capacidades: dict = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return self.forma.prefix

    def tiene_audio_externo(self) -> bool:
        """Hay audio de sistema dual en este proyecto?

        Un rodaje SOLO CAMARA es legitimo y frecuente —un comercial, una pieza
        institucional, cualquier cosa rodada con el microfono de la camara— y
        ahi todo el bloque de sync sobra. Sin esta comprobacion el pipeline
        moria en el paso 4: `index_audios.py` sale 1 cuando no hay carpeta de
        audio, y ese paso estaba en `abort` porque el orquestador de shell daba
        por hecho que siempre habia lavaliers.
        """
        return self.sql(
            "SELECT COUNT(*) FROM clips WHERE file_kind='audio' "
            "AND index_status='ok'") > 0

    def sql(self, query: str, params: tuple = ()) -> int:
        """Un escalar del manifest. 0 si la tabla aun no existe."""
        try:
            conn = manifest.conectar(str(self.db))
            try:
                return conn.execute(query, params).fetchone()[0] or 0
            finally:
                conn.close()
        except sqlite3.Error:
            return 0


@dataclass
class Delta:
    """Guarda de crecimiento REAL, no de piso absoluto.

    `lib/guards.py:283-285` ya documentaba por que el de bash estaba mal: en un
    disco con trabajo previo, comparar contra 1700 da OK aunque la corrida no
    haya escrito nada. Aqui se cuenta antes y despues.
    """
    tabla: str
    min_delta: int = 1
    where: str = ""
    # Un paso que BORRA Y REESCRIBE su tabla da delta 0 en cada re-corrida
    # aunque haya trabajado. Para esos, la medida honesta es el total final.
    #
    # Esto NO reabre la puerta al piso absoluto del .sh viejo: aquel comparaba
    # contra 1700 fijo en un paso ACUMULATIVO, y por eso tapaba que la corrida
    # no escribiera una fila. Aqui la tabla se vacia primero, asi que el total
    # final ES lo que produjo esta corrida. La diferencia esta en la naturaleza
    # del paso, no en la comodidad de la medicion.
    reemplaza: bool = False

    def contar(self, ctx: Contexto) -> int:
        q = f"SELECT COUNT(*) FROM {self.tabla}"
        if self.where:
            q += f" WHERE {self.where}"
        return ctx.sql(q)


@dataclass
class Paso:
    id: str
    script: str
    descripcion: str
    args: Callable[[Contexto], list[str]] = lambda ctx: []
    requiere: list[str] = field(default_factory=lambda: ["nucleo"])
    al_fallar: str = "abort"          # abort | warn | skip
    por_sector: bool = False
    guarda: Delta | None = None
    paralelo: int = 1                 # >1 solo si el paso es seguro concurrente
    # Precondicion sobre EL PROYECTO, distinta de `requiere`, que habla de la
    # MAQUINA. Un paso que no aplica se salta con motivo; no es un fallo.
    aplica_si: Callable[[Contexto], bool] | None = None
    motivo_no_aplica: str = ""


def _sectores_o_uno(ctx: Contexto, paso: Paso) -> list[str | None]:
    return list(ctx.forma.sectores) if paso.por_sector else [None]


# Espera minima ante un lock para considerar un paso apto para ir en paralelo.
# El default de Python son 5 s: suficiente para una consulta, corto para una
# transaccion de indexado sobre un disco externo.
BUSY_TIMEOUT_MINIMO_MS = 30_000

# El `(?:[^()]|\([^()]*\))*` cruza un nivel de parentesis anidados. Con un
# `[^)]*` simple —que fue la primera version— el patron se paraba en el `)` de
# `str(db)` y `sqlite3.connect(str(db), timeout=60.0)` no hacia match nunca:
# los 45 scripts que SI declaraban timeout se habrian dado por desprotegidos.
RE_TIMEOUT_EXPLICITO = re.compile(
    r"sqlite3\.connect\((?:[^()]|\([^()]*\))*timeout\s*=\s*([\d.]+)")


def _aguanta_concurrencia(paso: Paso) -> bool:
    """Si al script se le pueden lanzar varios sectores a la vez sin que se
    pisen escribiendo en el manifest.

    Se comprueba LEYENDO el script, no con una lista a mano: una lista
    envejeceria igual que envejecio el orquestador de shell.

    Vale con cualquiera de las dos vias, porque las dos dan el mismo
    busy_timeout: pasar por `manifest.conectar` (30 s) o llamar a
    `sqlite3.connect(..., timeout=N)` con N alto. Lo que NO basta es el default
    de Python —5 s—, que aguanta una consulta pero no una transaccion de
    indexado sobre un disco externo lento.
    """
    ruta = HERE / paso.script
    try:
        txt = ruta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "manifest.conectar" in txt or "from lib.manifest import conectar" in txt:
        return True
    explicitos = [float(m) for m in RE_TIMEOUT_EXPLICITO.findall(txt)]
    return bool(explicitos) and min(explicitos) * 1000 >= BUSY_TIMEOUT_MINIMO_MS


def _motivo_no_aplica(paso: Paso, ctx: Contexto, sector: str | None) -> str:
    if paso.id == "sync-audio" and sector:
        return (f"'{sector}' no tiene carpeta de audio externo asociada — usa "
                f"el audio de camara, o falta declararlo en 'mapa_audio'")
    return "el paso no produjo argumentos para este alcance"


# --------------------------------------------------------------------------
# LOS PASOS
# --------------------------------------------------------------------------
# El orden es el del .sh viejo. Lo que cambia es que aqui nada esta escrito a
# mano: los sectores salen de `ctx.forma`, el prefijo se inyecta siempre, y las
# guardas miden delta.

def _args_sync_audio(ctx: Contexto, sector: str) -> list[str]:
    carpetas = ctx.forma.mapa_audio.get(sector, [])
    if not carpetas:
        return []          # sector sin audio externo: no es error, se salta
    args = ["--root", str(ctx.root),
            "--video-folder", f"{ctx.prefix}{sector}"]
    for c in carpetas:
        args += ["--audio-folder", c]
    return args


def _args_transcribir_audios(ctx: Contexto) -> list[str]:
    """El filtro de carpeta sale de la forma del proyecto, no del default.

    transcribe_audios.py selecciona por `rel_path LIKE '%audios%'`, que es el
    layout de ESCALANDO MEXICO. En un proyecto plano con `AUDIO/` o `Audio/`
    —Asistente 2026-09-28, y Morsa antes— no casa nada: el script avisaba con
    "TRABAJO CERO", el paso quedaba en sin-trabajo y el pipeline seguia al sync
    SIN un solo transcript de lavalier.
    """
    args = ["--root", str(ctx.root), "--model", str(ctx.whisper_model)]
    carpetas = sorted({c for cs in ctx.forma.mapa_audio.values() for c in cs})
    if len(carpetas) == 1:
        args += ["--audio-like", f"{ctx.prefix}{carpetas[0]}/%"]
    return args


PASOS: list[Paso] = [
    # ---- pre-flight ------------------------------------------------------
    Paso("lint", "lint_pipeline.py",
         "hardcodes geograficos, DELETE globales y llamadas sin prefijo",
         args=lambda ctx: []),
    Paso("pruebas", "__unittest__",
         "suite del motor (estatica y sintetica, sin disco)",
         args=lambda ctx: [],
         # Sin esto, `unittest discover` sobre un directorio que no existe sale
         # con 1 y, como el paso es abort, el pipeline moria en el paso 2 en
         # TODA Mac instalada con el plugin (2026-09-23).
         aplica_si=lambda ctx: SUITE.is_dir(),
         motivo_no_aplica="esta instalacion no trae tests/ (el plugin no la "
                          "empaqueta); la suite corre en el repo del motor"),
    Paso("baseline", "verify_coverage.py",
         "cobertura antes de empezar",
         args=lambda ctx: ["--root", str(ctx.root), "--report-only",
                           "--project-prefix", ctx.prefix],
         al_fallar="warn"),

    # ---- indexado de audio ----------------------------------------------
    Paso("indexar-audios", "index_audios.py",
         "audios externos que falten en el manifest",
         args=lambda ctx: [str(ctx.root)], al_fallar="warn"),
    Paso("pistas-de-campo", "index_audio_tracks.py",
         "poly-WAV de grabadora: bext + iXML y separacion de canales",
         args=lambda ctx: ["--root", str(ctx.root), "--split",
                           "--solo-polifonicos"],
         al_fallar="warn",
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),

    # ---- sync por sector -------------------------------------------------
    Paso("sync-audio", "sync_audio.py",
         "sync por timecode BWF, sector a sector",
         args=_args_sync_audio, por_sector=True, al_fallar="warn",
         guarda=Delta("audio_sync_pairs"), paralelo=3),

    # ---- transcripcion ---------------------------------------------------
    # NO va por sector aunque el .sh lo hiciera: Whisper ya satura la CPU por si
    # solo, asi que trocearlo en cuatro corridas no gana nada y complica el
    # cache. El script recorre todo el material en una pasada, idempotente.
    Paso("transcribir-clips", "transcribe_clips.py",
         "A1 de las camaras principales",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--model", str(ctx.whisper_model),
                           "--cameras", "main"]),
    Paso("transcribir-audios", "transcribe_audios.py",
         "audios externos (idempotente, salta los ya hechos)",
         args=_args_transcribir_audios,
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),

    # Capa 2 de la doctrina de transcripcion: corrige los nombres propios que
    # Whisper inventa, con el vocabulario y las correcciones del proyecto. NO
    # estaba en el pipeline pese a ser un paso del playbook desde mayo.
    Paso("vocabulario", "correct_transcripts_vocab.py",
         "corrige nombres y terminos del proyecto en los transcripts",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),

    # ---- analisis --------------------------------------------------------
    # `curate_segments.py` lee cinco tablas que ningun paso creaba. En los
    # proyectos anteriores existian porque se habian corrido a mano (playbook
    # §6, §7 y "dependencias de curate_segments"); en uno NUEVO el pipeline
    # abortaba en `curar-tramos` con `no such table: clip_descriptions`
    # (Asistente, 2026-09-28). Todos son idempotentes.
    Paso("analizar-clips", "analyze_clips.py",
         "exposicion, movimiento y audio por clip (LENTO: decodifica todo)",
         args=lambda ctx: ["--root", str(ctx.root), "--workers", str(ctx.workers)]),
    Paso("categorias", "derive_video_categories.py",
         "entrevista o b-roll por clip",
         args=lambda ctx: ["--root", str(ctx.root), "--project-prefix", ctx.prefix]),
    Paso("valor-de-plano", "derive_shot_value.py",
         "valor de plano por clip",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("angulo", "derive_camera_angle.py",
         "angulo de camara por clip",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("analyze-segments", "analyze_segments.py",
         "tramos usables multi-senal",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix,
                           "--workers", str(ctx.workers)],
         guarda=Delta("clip_segments")),
    Paso("tramos-de-contenido", "derive_content_segments.py",
         "tramos de contenido",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("curar-tramos", "curate_segments.py",
         "tramos curados",
         args=lambda ctx: ["--root", str(ctx.root)],
         guarda=Delta("clip_curated_segments")),
    Paso("frames", "extract_segment_frames.py",
         "frames representativos de cada tramo",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("objetos", "analyze_segment_objects.py",
         "objetos por frame",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),
    Paso("describir-llm", "describe_segments_local_llm.py",
         "descripcion por LLM local (LENTO; preserva lo ya descrito)",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--skip-already-described"],
         al_fallar="warn"),

    # ---- sync v2 ---------------------------------------------------------
    Paso("sync-schema", "init_sync_schema.py",
         "tablas y indices de sync",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("lavalier-pairs", "build_lavalier_pairs.py",
         "parejas de lavalier por timestamp y contenido",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn",
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),
    Paso("sync-v2", "sync_pipeline_full.py",
         "voice-first + transcript-segments",
         args=lambda ctx: ["--root", str(ctx.root), "--skip-existing"],
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo",
         # ANTES ERA `|| WARN`. El sync es el corazon del producto: si falla,
         # todo lo que viene despues se hornea sobre pares incompletos y el
         # editor lo descubre en Resolve. Falla ruidosamente.
         al_fallar="abort"),
    Paso("verify-sync", "verify_sync_physical.py",
         "verificacion fisica de los pares",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn",
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),

    # ---- preguntas y beats ----------------------------------------------
    Paso("master-transcripts", "build_master_transcripts.py",
         "A1 + lavaliers por entrevista",
         args=lambda ctx: ["--root", str(ctx.root), "--only-interviews"],
         al_fallar="warn",
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),
    Paso("preguntas", "derive_question_segments.py",
         "Purple Q markers",
         args=lambda ctx: ["--root", str(ctx.root)],
         guarda=Delta("question_segments")),
    Paso("espejo-preguntas", "mirror_questions_to_audio.py",
         "las preguntas tambien en la pista de audio",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn",
         aplica_si=lambda ctx: ctx.tiene_audio_externo(),
         motivo_no_aplica="rodaje solo camara: no hay audio externo"),
    # Va DESPUES de corregir el vocabulario: agrupa por el texto, asi que con
    # los nombres sin corregir partiria en dos grupos lo que es uno solo.
    Paso("tomas", "derive_takes.py",
         "agrupa las tomas que dicen el mismo texto y marca las cortadas",
         args=lambda ctx: ["--root", str(ctx.root)],
         al_fallar="warn", guarda=Delta("take_groups", reemplaza=True)),
    Paso("pausas", "detect_pauses.py",
         "silencios reales (sin esto los beats caen al metodo estimado)",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),
    Paso("beats", "derive_interview_beats.py",
         "pregunta/respuesta separadas, pausas, keywords",
         args=lambda ctx: ["--root", str(ctx.root)]),
    Paso("verify-beats", "verify_beats.py",
         "los beats caen donde deben",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),

    # ---- identidad -------------------------------------------------------
    # ORDEN INVERTIDO respecto al .sh (2026-08-05). Antes era atribuir y luego
    # limpiar, y `clean_character_identity.py:100` hace `DELETE FROM
    # clip_characters`: borraba lo que el paso anterior acababa de escribir.
    # Limpiando primero, la atribucion es lo ultimo que toca la tabla.
    # (La columna `fuente` que hace el DELETE selectivo es la Fase 4.5.)
    Paso("limpiar-identidad", "clean_character_identity.py",
         "deja solo identidades confirmadas por texto",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),
    Paso("detectar-caras", "detect_faces.py",
         "caras del material (insumo de la atribucion)",
         args=lambda ctx: ["--root", str(ctx.root)],
         # NUNCA estuvo cableado: `face_detections` quedaba vacia y las tres
         # senales de attribute_faces_via_transcript no tenian sobre que votar.
         requiere=["caras"], al_fallar="warn"),
    Paso("atribuir-caras", "attribute_faces_via_transcript.py",
         "quien es quien, via transcript",
         args=lambda ctx: ["--root", str(ctx.root)], al_fallar="warn"),
    Paso("enriquecer-tramos", "enrich_curated_segments.py",
         "personajes y sector en cada tramo",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix],
         al_fallar="warn"),

    # ---- roll y horneado -------------------------------------------------
    Paso("roll", "derive_roll.py",
         "A-roll / B-roll",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix]),
    Paso("payload", "build_metadata_payload.py",
         "metadata que consume el bake",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix]),
    Paso("bake", "export_lua_data.py",
         "horneado Lua al disco del proyecto",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix,
                           "--out", str(ctx.root / ".cinema_assistant" /
                                        "resolve" / f"{_slug(ctx)}_data.lua")]),

    # ---- cierre ----------------------------------------------------------
    Paso("verify-final", "verify_coverage.py",
         "cobertura final por sector",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix],
         al_fallar="warn"),

    # La QUINTA capa de verificacion (metodologia/pruebas-del-asistente.md):
    # las cuatro anteriores -- lint, tests, guards, verify_* -- comprueban el
    # CODIGO y los DATOS. Esta comprueba AL ASISTENTE: si curo por comprension
    # con correlato fisico, si nunca dejo trabajo cero sin reconocer, si el
    # bake esta al dia. Al_fallar=warn a proposito: el acta reporta hallazgos,
    # no bloquea el pipeline -- el editor decide que hacer con ellos (mismo
    # principio que doctrina_novedades.py).
    Paso("acta", "verify_asistente.py",
         "acta de conducta: las cuatro puertas + invariantes transversales",
         args=lambda ctx: ["--root", str(ctx.root),
                           "--project-prefix", ctx.prefix,
                           "--todas", "--acta"],
         al_fallar="warn"),
]


def _slug(ctx: Contexto) -> str:
    cfg = proyecto.leer_config(ctx.root)
    if cfg.get("slug"):
        return str(cfg["slug"])
    base = ctx.prefix.strip("/") or ctx.root.name
    return "".join(c.lower() if c.isalnum() else "_" for c in base).strip("_")


# --------------------------------------------------------------------------
# Ejecucion
# --------------------------------------------------------------------------

@dataclass
class Resultado:
    paso: str
    estado: str            # ok | warn | abort | saltado | sin-trabajo
    motivo: str = ""
    delta: int | None = None
    segundos: float = 0.0


def construir_argv(paso: Paso, ctx: Contexto, sector: str | None) -> list[str] | None:
    """El argv exacto de un paso. None si el paso no aplica a ese sector."""
    if paso.script == "__unittest__":
        return [sys.executable, "-m", "unittest", "discover",
                "-s", str(SUITE), "-t", str(SUITE)]

    interprete, faltan = entorno.interprete_para_todas(
        paso.requiere, ctx.capacidades)
    if interprete is None:
        return None

    # La firma se cuenta, no se adivina con try/except TypeError: ese patron se
    # tragaria un TypeError legitimo de DENTRO de la lambda y el paso quedaria
    # "sin aplicar" en silencio, que es la clase de fallo que este orquestador
    # existe para eliminar.
    n_params = len(inspect.signature(paso.args).parameters)
    args = paso.args(ctx, sector) if n_params >= 2 else paso.args(ctx)
    if args is None or (paso.por_sector and not args):
        return None
    return [interprete, str(HERE / paso.script)] + list(args)


def ejecutar(argv: list[str], log) -> int:
    """Corre un paso. En --dry-run, CINEMA_EXEC_STUB lo sustituye."""
    stub = os.environ.get("CINEMA_EXEC_STUB")
    if stub:
        argv = [stub] + argv
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        log(f"    no se pudo ejecutar: {e}")
        return 127
    for linea in (r.stdout or "").splitlines():
        log(f"    {linea}")
    for linea in (r.stderr or "").splitlines():
        log(f"    {linea}")
    return r.returncode


def correr(ctx: Contexto, pasos: list[Paso], log,
           plan_only: bool = False) -> list[Resultado]:
    resultados: list[Resultado] = []

    for paso in pasos:
        # Precondicion sobre el proyecto. Un rodaje solo camara no tiene nada
        # que sincronizar: esos pasos se saltan con motivo, no fallan.
        if paso.aplica_si is not None and not plan_only and not paso.aplica_si(ctx):
            motivo = paso.motivo_no_aplica or "no aplica a este proyecto"
            resultados.append(Resultado(paso.id, "saltado", motivo))
            log(f"[SALTADO] {paso.id} — {motivo}")
            continue

        interprete, faltan = entorno.interprete_para_todas(
            paso.requiere, ctx.capacidades)
        if interprete is None and paso.script != "__unittest__":
            # NO se cae y NO se silencia: se anota con el comando para arreglarlo.
            resultados.append(Resultado(
                paso.id, "saltado",
                f"falta la capacidad {', '.join(faltan)} en cualquier interprete"))
            log(f"[SALTADO] {paso.id} — falta: {', '.join(faltan)}")
            continue

        antes = paso.guarda.contar(ctx) if (paso.guarda and not plan_only) else None
        t0 = time.time()
        peor = 0
        corrio_algo = False
        trabajos: list[tuple[str, list[str]]] = []

        for sector in _sectores_o_uno(ctx, paso):
            argv = construir_argv(paso, ctx, sector)
            if argv is None:
                # Un paso que no aplica tiene que DECIRLO. Si se salta callando,
                # el plan parece completo y nadie nota que un sector se quedo
                # sin sync — que es como ESCALANDO MEXICO acabo con seis
                # sectores sin markers.
                if plan_only:
                    etq = f"{paso.id}" + (f" [{sector}]" if sector else "")
                    log(f"PASO {etq:<34} NO APLICA "
                        f"({_motivo_no_aplica(paso, ctx, sector)})")
                continue
            corrio_algo = True
            etiqueta = f"{paso.id}" + (f" [{sector}]" if sector else "")
            if plan_only:
                log(f"PASO {etiqueta:<34} requiere={','.join(paso.requiere):<12} "
                    f"al-fallar={paso.al_fallar:<6} argv={argv}")
                continue
            trabajos.append((etiqueta, argv))

        if not plan_only and trabajos:
            hilos = min(paso.paralelo, len(trabajos))
            if hilos > 1 and not _aguanta_concurrencia(paso):
                # No se paraleliza a ciegas: sin busy_timeout en el script
                # destino, dos sectores a la vez dan "database is locked" y se
                # pierde el trabajo de uno. Mejor lento que a medias.
                log(f"  ({paso.id}: {paso.paralelo} hilos pedidos, pero "
                    f"{paso.script} no abre con manifest.conectar — va en serie)")
                hilos = 1

            if hilos > 1:
                log(f"\n=== {paso.id} — {paso.descripcion}  [{hilos} en paralelo]")
                with ThreadPoolExecutor(max_workers=hilos) as pool:
                    futuros = [pool.submit(ejecutar, argv, log)
                               for _etq, argv in trabajos]
                    for fut in as_completed(futuros):
                        peor = max(peor, fut.result())
            else:
                for etq, argv in trabajos:
                    log(f"\n=== {etq} — {paso.descripcion}")
                    peor = max(peor, ejecutar(argv, log))

        if plan_only:
            resultados.append(Resultado(paso.id, "ok"))
            continue

        if not corrio_algo:
            resultados.append(Resultado(
                paso.id, "saltado", "no aplica a ningun sector de este proyecto"))
            continue

        seg = time.time() - t0
        delta = None
        if paso.guarda:
            despues = paso.guarda.contar(ctx)
            delta = despues if paso.guarda.reemplaza else despues - (antes or 0)

        if peor == 0:
            estado, motivo = "ok", ""
            if delta is not None and delta < paso.guarda.min_delta:
                # El paso dijo que si y no escribio nada. En bash esto pasaba
                # inadvertido porque el piso era absoluto.
                estado = "sin-trabajo"
                motivo = (f"{paso.guarda.tabla} no crecio "
                          f"(delta={delta}, esperado >= {paso.guarda.min_delta})")
        elif peor == EXIT_NO_WORK:
            estado, motivo = "sin-trabajo", "el script reporto seleccion vacia"
        else:
            estado = "abort" if paso.al_fallar == "abort" else "warn"
            motivo = f"exit={peor}"

        resultados.append(Resultado(paso.id, estado, motivo, delta, seg))
        log(f"  -> {estado}" + (f" ({motivo})" if motivo else "") +
            (f"  delta={delta}" if delta is not None else "") +
            f"  {seg:.1f}s")

        if estado == "abort":
            log(f"\nABORTA en '{paso.id}': {motivo}")
            break

    return resultados


def _escribir_sidecar(log_path: Path, ctx: Contexto, resultados: list[Resultado]) -> None:
    """Espejo maquinable del log humano (Pieza 4 de la quinta capa de
    verificacion). bin/verify_asistente.py lo lee para reconstruir que paso
    corrio, con que delta y con que motivo, sin parsear texto. Las 504
    corridas historicas en logs/ no lo tienen -- el acta cae a parsear el
    .log cuando el .json hermano no existe.
    """
    payload = {
        "root": str(ctx.root),
        "prefix": ctx.forma.prefix,
        "sectores": list(ctx.forma.sectores),
        "pasos": [
            {"id": r.paso, "estado": r.estado, "motivo": r.motivo,
             "delta": r.delta, "segundos": round(r.segundos, 3)}
            for r in resultados
        ],
    }
    try:
        log_path.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass   # el log humano ya se escribio; el sidecar es una comodidad, no una garantia


def resumen(resultados: list[Resultado], ctx: Contexto, log) -> int:
    log("\n" + "=" * 72)
    log("RESUMEN")
    log("=" * 72)
    for r in resultados:
        linea = f"  {r.estado:<12} {r.paso:<24}"
        if r.delta is not None:
            linea += f" delta={r.delta:<7}"
        if r.segundos:
            linea += f" {r.segundos:6.1f}s"
        if r.motivo:
            linea += f"  {r.motivo}"
        log(linea)

    # LA SECCION QUE NO EXISTIA. Por no tenerla, la identidad por cara lleva
    # meses sin ejecutarse y nadie lo noto: el pipeline decia OK igual.
    no_corrio = [r for r in resultados if r.estado in ("saltado", "sin-trabajo")]
    if no_corrio:
        log("\n" + "=" * 72)
        log("NO CORRIO Y POR QUE")
        log("=" * 72)
        for r in no_corrio:
            log(f"  {r.paso}: {r.motivo}")
        if any("capacidad" in r.motivo for r in no_corrio):
            log(f"\n  Para habilitar las capacidades que faltan:\n    "
                f"{entorno.COMO_INSTALAR}")

    if ctx.forma.avisos:
        log("\nAVISOS SOBRE LA FORMA DEL PROYECTO")
        for a in ctx.forma.avisos:
            log(f"  - {a}")

    fallos = [r for r in resultados if r.estado in ("abort", "warn")]
    log("")
    if any(r.estado == "abort" for r in resultados):
        log("PIPELINE INCOMPLETO — un paso critico fallo.")
        return EXIT_PASO_FALLO
    if fallos:
        log(f"Pipeline terminado con {len(fallos)} aviso(s).")
    else:
        log("Pipeline terminado.")
    bake = ctx.root / ".cinema_assistant" / "resolve" / f"{_slug(ctx)}_data.lua"
    if bake.exists():
        log(f"\nAplica en Resolve: Workspace > Console > Lua")
        log(f'  dofile("{ENGINE}/resolve/asistente_{_slug(ctx)}.lua")')
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # --root NO es required: `--listar` tiene que funcionar sin disco montado,
    # que es cuando mas falta hace consultar los pasos y sus ids.
    ap.add_argument("--root", help="raiz del disco del proyecto")
    ap.add_argument("--project-prefix", default=None,
                    help="'' para proyecto plano. Si no se pasa, se lee de "
                         "project_config.json; si tampoco esta, se pregunta.")
    ap.add_argument("--plan-only", action="store_true",
                    help="imprime el argv de cada paso y no ejecuta nada")
    ap.add_argument("--desde", help="empezar en este paso")
    ap.add_argument("--hasta", help="terminar en este paso")
    ap.add_argument("--solo", help="correr solo este paso")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--listar", action="store_true", help="lista los pasos y sale")
    args = ap.parse_args()

    if args.listar:
        for p in PASOS:
            marcas = []
            if p.requiere != ["nucleo"]:
                marcas.append("requiere " + ",".join(p.requiere))
            if p.por_sector:
                marcas.append("por sector")
            if p.al_fallar != "abort":
                marcas.append(p.al_fallar)
            extra = f"  [{'; '.join(marcas)}]" if marcas else ""
            print(f"  {p.id:<22} {p.descripcion}{extra}")
        return 0

    if not args.root:
        print("Falta --root (solo --listar funciona sin disco).", file=sys.stderr)
        return EXIT_PRECONDICION

    root = Path(args.root.rstrip("/")).expanduser()
    if not root.is_dir():
        print(f"Disco no encontrado: {root}", file=sys.stderr)
        return EXIT_PRECONDICION
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        print(f"Manifest no existe: {db}\nCorre primero index_project.py.",
              file=sys.stderr)
        return EXIT_PRECONDICION

    try:
        forma = proyecto.detectar(root, prefix_flag=args.project_prefix)
    except proyecto.FormaAmbigua as e:
        print(str(e), file=sys.stderr)
        return EXIT_PRECONDICION

    ctx = Contexto(
        root=root, forma=forma, db=db,
        whisper_model=ENGINE / "models" / "ggml-large-v3-turbo.bin",
        workers=args.workers,
        capacidades=entorno.mapa_de_capacidades(),
    )

    pasos = list(PASOS)
    if args.solo:
        pasos = [p for p in pasos if p.id == args.solo]
        if not pasos:
            print(f"No hay un paso '{args.solo}'. Ver --listar.", file=sys.stderr)
            return EXIT_PRECONDICION
    else:
        if args.desde:
            ids = [p.id for p in pasos]
            if args.desde not in ids:
                print(f"No hay un paso '{args.desde}'. Ver --listar.", file=sys.stderr)
                return EXIT_PRECONDICION
            pasos = pasos[ids.index(args.desde):]
        if args.hasta:
            ids = [p.id for p in pasos]
            if args.hasta not in ids:
                print(f"No hay un paso '{args.hasta}'. Ver --listar.", file=sys.stderr)
                return EXIT_PRECONDICION
            pasos = pasos[:ids.index(args.hasta) + 1]

    log_dir = ENGINE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pipeline-{time.strftime('%Y%m%d-%H%M%S')}.log"
    fh = None if args.plan_only else log_path.open("w", encoding="utf-8")

    def log(msg: str = "") -> None:
        print(msg, flush=True)
        if fh:
            fh.write(msg + "\n")
            fh.flush()

    try:
        if not args.plan_only:
            log("=" * 72)
            log("ORQUESTADOR DEL ASISTENTE DE EDICION")
            log("=" * 72)
            log(f"  disco     {root}")
            log(f"  prefijo   {forma.prefix!r}  (origen: {forma.origen_prefix})")
            log(f"  sectores  {len(forma.sectores)}: {', '.join(forma.sectores[:6])}"
                + ("..." if len(forma.sectores) > 6 else ""))
            log(f"  log       {log_path}")
            for cap, quien in ctx.capacidades.items():
                if not quien:
                    log(f"  capacidad '{cap}': NO disponible")

        resultados = correr(ctx, pasos, log, plan_only=args.plan_only)
        if args.plan_only:
            return 0
        _escribir_sidecar(log_path, ctx, resultados)
        return resumen(resultados, ctx, log)
    finally:
        if fh:
            fh.close()


if __name__ == "__main__":
    sys.exit(main())
