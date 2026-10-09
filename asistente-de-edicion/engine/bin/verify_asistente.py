#!/usr/bin/env python3
"""El acta de conducta -- la quinta capa de verificación del asistente.

POR QUÉ EXISTE. `tests/README.md` documenta cuatro capas: `lint_pipeline.py`
(código), `tests/` (código + datos sintéticos), `lib/guards.py` (durante la
corrida) y `bin/verify_*.py` (datos reales). Las cuatro verifican EL CÓDIGO Y
LOS DATOS. Ninguna verifica AL ASISTENTE: si leyó la doctrina antes de tocar
material, si corrió los pasos en orden, si pasó los seis verificadores del
§12b antes de decir "listo para Resolve", si curó por comprensión o solo
corrió el script, si reportó números honestos, si no rehízo trabajo ya hecho.

Doctrina completa (las cuatro puertas, cada checkpoint con su caso fundador):
    metodologia/pruebas-del-asistente.md

PRINCIPIO DE DISEÑO: ninguna declaración sin correlato físico. Un checkpoint
nunca se conforma con que el asistente "diga" que hizo algo -- exige el
artefacto que lo demuestra (un reporte generado, filas con curated_by='claude',
un journal sin solapes, una nota escrita en acta-notas.md). La diferencia entre
una garantía y una promesa (preferencias-del-usuario.md, "Lo que valora del
asistente").

Tres veredictos, nunca un booleano pelado: PASA / FALLA / NO-APLICA (con
motivo). Reusa los verify_*.py existentes como subprocesos -- este script es
el AGREGADOR, no un verificador nuevo que duplica lógica.

`acta-notas.md`, formato (lo escribe el asistente a mano, es la declaración
con correlato físico que exigen B1/B4/B5):

    - <id_de_paso_o_checkpoint>: texto libre explicando por qué es legítimo.

Uso:
    python3 bin/verify_asistente.py --root <disco> --project-prefix '' --listar
    python3 bin/verify_asistente.py --root <disco> --project-prefix '' --puerta G0
    python3 bin/verify_asistente.py --root <disco> --project-prefix '' --todas --acta
    python3 bin/verify_asistente.py --root <disco> --project-prefix '' --todas --json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from lib import manifest, proyecto  # noqa: E402
from lib.guards import table_count  # noqa: E402
from bin.doctor import CONSECUENCIA  # noqa: E402

PASA, FALLA, NO_APLICA = "PASA", "FALLA", "NO-APLICA"
EXIT_HALLAZGOS = 1


# --------------------------------------------------------------------------
# Estructuras
# --------------------------------------------------------------------------

@dataclass
class Veredicto:
    estado: str
    detalle: str = ""


@dataclass
class Checkpoint:
    id: str
    puerta: str            # G0 | G1 | G2 | G3 | I (transversal)
    descripcion: str
    caso_fundador: str
    fn: Callable[["Ctx"], Veredicto]
    obligatorio: bool = True


@dataclass
class Ctx:
    root: Path
    prefix: str
    db: Path
    reports_dir: Path
    _conn: sqlite3.Connection | None = field(default=None, repr=False)

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = manifest.conectar(str(self.db))
        return self._conn

    def sql(self, q: str, params: tuple = ()):
        row = self.conn().execute(q, params).fetchone()
        return row[0] if row else None

    def cerrar(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _slug(ctx: Ctx) -> str:
    """Mismo cálculo que bin/run_pipeline.py:_slug -- reimplementado aquí
    (no importado) porque aquél toma un run_pipeline.Contexto distinto."""
    cfg = proyecto.leer_config(ctx.root)
    if cfg.get("slug"):
        return str(cfg["slug"])
    base = ctx.prefix.strip("/") or ctx.root.name
    return "".join(c.lower() if c.isalnum() else "_" for c in base).strip("_")


# --------------------------------------------------------------------------
# Helpers de evidencia compartidos
# --------------------------------------------------------------------------

def _ts_de_nombre(p: Path) -> float | None:
    """`pipeline-20260824-105435.log` -> epoch. None si no matchea."""
    m = re.match(r"pipeline-(\d{8})-(\d{6})", p.stem)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def _parsear_resumen_legacy(txt: str) -> list[dict]:
    """Reconstruye la lista de pasos desde el bloque RESUMEN de un .log de
    texto plano, para las corridas anteriores al sidecar .json (Pieza 4).
    Formato exacto de bin/run_pipeline.py:resumen():
        '  {estado:<12} {paso:<24}[ delta={n}][ {seg}s][  {motivo}]'
    """
    lineas = txt.splitlines()
    try:
        i = lineas.index("RESUMEN") + 2   # salta el "="*72 de cierre
    except ValueError:
        return []
    pat = re.compile(r"^  (\S+)\s+(\S+)(?:\s+delta=(-?\d+))?(?:\s+([\d.]+)s)?(?:\s{2,}(.*))?$")
    pasos = []
    for ln in lineas[i:]:
        if not ln.strip():
            break
        m = pat.match(ln)
        if not m:
            continue
        estado, pid, delta, seg, motivo = m.groups()
        pasos.append({
            "id": pid, "estado": estado,
            "delta": int(delta) if delta is not None else None,
            "segundos": float(seg) if seg is not None else 0.0,
            "motivo": motivo or "",
        })
    return pasos


def _corridas_de_pipeline(ctx: Ctx) -> list[tuple[Path, dict]]:
    """Corridas de run_pipeline.py que mencionan este --root, más antigua
    primero. Usa el sidecar .json (Pieza 4) cuando existe; cae a parsear el
    .log de texto para las 504 corridas históricas que no lo tienen."""
    logs_dir = ENGINE / "logs"
    out: list[tuple[Path, dict]] = []
    for log in sorted(logs_dir.glob("pipeline-*.log")):
        sidecar = log.with_suffix(".json")
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("root") == str(ctx.root):
                out.append((log, data))
            continue
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"disco     {ctx.root}" not in txt:
            continue
        out.append((log, {"root": str(ctx.root), "pasos": _parsear_resumen_legacy(txt)}))
    return out


def _leer_notas(ctx: Ctx) -> dict[str, str]:
    """acta-notas.md: '- <id>: <texto libre>' -> {id: texto}. El correlato
    físico que exigen B1/B4/B5 -- una nota que el asistente escribió a mano,
    no una afirmación de paso."""
    p = ctx.reports_dir / "acta-notas.md"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*-\s*([\w.-]+)\s*:\s*(.+)$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _mtime_max(root: Path) -> float | None:
    if not root.is_dir():
        return None
    mtimes = [f.stat().st_mtime for f in root.glob("*") if f.is_file()]
    return max(mtimes) if mtimes else None


def _run(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _ultima_linea_util(texto: str) -> str:
    lineas = [l for l in texto.splitlines() if l.strip()]
    return lineas[-1].strip() if lineas else ""


# --------------------------------------------------------------------------
# G0 -- Arranque
# --------------------------------------------------------------------------

TABLAS_BASELINE = ["clip_analysis", "clip_segments", "clip_curated_segments",
                   "audio_sync_pairs", "question_segments", "clip_metadata_payload"]


def check_a1_baseline(ctx: Ctx) -> Veredicto:
    baseline_path = ctx.reports_dir / "acta-baseline.json"
    logs = [log for log, _ in _corridas_de_pipeline(ctx)]
    if not baseline_path.exists():
        ctx.reports_dir.mkdir(parents=True, exist_ok=True)
        conteos = {t: table_count(ctx.conn(), t) for t in TABLAS_BASELINE}
        baseline_path.write_text(json.dumps(
            {"tomado_ts": time.time(), "conteos": conteos},
            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if logs:
            return Veredicto(FALLA, f"línea base tomada AHORA pero ya hay "
                             f"{len(logs)} corrida(s) de run_pipeline.py previas -- "
                             f"se tomó tarde (§13 'cobertura del pipeline')")
        return Veredicto(PASA, "línea base tomada ahora, sin corridas previas del pipeline")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    tomado_ts = baseline.get("tomado_ts", 0)
    anteriores = [p for p in logs if (_ts_de_nombre(p) or 0) < tomado_ts]
    if anteriores:
        return Veredicto(FALLA, f"{len(anteriores)} corrida(s) de run_pipeline.py son "
                         f"anteriores a la línea base: {[p.name for p in anteriores[:3]]}")
    return Veredicto(PASA, f"línea base de {time.ctime(tomado_ts)}, "
                     f"{len(logs)} corrida(s) posteriores")


def check_a2_prefix(ctx: Ctx) -> Veredicto:
    if ctx.prefix == "":
        return Veredicto(PASA, "proyecto plano, --project-prefix ''")
    n = ctx.sql("SELECT COUNT(*) FROM clips WHERE rel_path LIKE ? || '%'", (ctx.prefix,))
    if n and n > 0:
        return Veredicto(PASA, f"{n} clips bajo el prefijo {ctx.prefix!r}")
    return Veredicto(FALLA, f"--project-prefix {ctx.prefix!r} no casa con ningún clip "
                     f"(lección 47 -- Fantástico Cómics quedó a medias por esto)")


def check_a3_capacidades(ctx: Ctx) -> Veredicto:
    from lib import entorno
    mapa = entorno.mapa_de_capacidades()
    faltan = {k: v for k, v in mapa.items() if not v}
    if not faltan:
        return Veredicto(PASA, "todas las capacidades disponibles en algún intérprete")
    sin_consecuencia = [k for k in faltan if k not in CONSECUENCIA]
    if sin_consecuencia:
        return Veredicto(FALLA, f"capacidad(es) sin consecuencia documentada en "
                         f"bin/doctor.py: {sin_consecuencia}")
    detalle = "; ".join(f"{k}: {CONSECUENCIA[k]}" for k in faltan)
    return Veredicto(PASA, f"faltan {len(faltan)} capacidad(es), consecuencia conocida -- {detalle}")


def check_a4_novedades(ctx: Ctx) -> Veredicto:
    script = HERE / "doctrina_novedades.py"
    try:
        proc = _run([sys.executable, str(script), "--strict"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Veredicto(FALLA, f"no se pudo correr doctrina_novedades.py: {e}")
    if proc.returncode not in (0, 1):
        return Veredicto(FALLA, f"doctrina_novedades.py exit={proc.returncode} inesperado")
    if proc.returncode == 1:
        return Veredicto(PASA, f"hay novedades pendientes (aviso, no compuerta -- el editor "
                         f"decide qué incorporar): {_ultima_linea_util(proc.stdout)}")
    return Veredicto(PASA, "doctrina al día, sin novedades del plugin")


# --------------------------------------------------------------------------
# G1 -- Entre etapas
# --------------------------------------------------------------------------

def check_b1_trabajo_cero(ctx: Ctx) -> Veredicto:
    corridas = _corridas_de_pipeline(ctx)
    if not corridas:
        return Veredicto(NO_APLICA, "sin corridas de run_pipeline.py registradas para "
                         "este proyecto")
    notas = _leer_notas(ctx)
    sin_reconocer = []
    for log, data in corridas:
        for p in data.get("pasos", []):
            if p.get("estado") == "sin-trabajo" and p.get("id") not in notas:
                sin_reconocer.append(f"{p['id']} ({log.name})")
    if sin_reconocer:
        return Veredicto(FALLA, f"{len(sin_reconocer)} paso(s) con TRABAJO CERO sin "
                         f"reconocer en acta-notas.md (lecciones 40/47/50/51): "
                         + "; ".join(sin_reconocer[:5]))
    return Veredicto(PASA, f"{len(corridas)} corrida(s) revisadas, sin trabajo-cero "
                     f"sin reconocer")


# Dependencias de la rama visual que curate_segments.py NO exige con
# guards.exigir_tablas() -- muere con "no such table" en vez de avisar.
# Documentado en la memoria 'rama-visual-orden-de-dependencias' y en
# metodologia/pasos-a-seguir.md §10/11.
DEP_RAMA_VISUAL = {
    "clip_curated_segments": ["clip_shot_values", "clip_angles", "content_segments"],
}


def check_b2_dependencias(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    if table_count(conn, "clip_segments") == 0:
        return Veredicto(NO_APLICA, "sin clip_segments -- la rama visual aún no arrancó")
    problemas = []
    for tabla, requeridas in DEP_RAMA_VISUAL.items():
        if table_count(conn, tabla) > 0:
            continue   # ya corrió con éxito, sus prerrequisitos existieron
        faltantes = [t for t in requeridas if table_count(conn, t) == 0]
        if faltantes:
            problemas.append(f"{tabla} vacía; faltan {faltantes} (§10/11, "
                             f"'rama-visual-orden-de-dependencias')")
    if problemas:
        return Veredicto(FALLA, "; ".join(problemas))
    return Veredicto(PASA, "dependencias de la rama visual satisfechas")


def check_b3_whisper_lock(ctx: Ctx) -> Veredicto:
    jpath = ENGINE / "logs" / "whisper-journal.jsonl"
    if not jpath.exists():
        return Veredicto(NO_APLICA, "sin journal de Whisper -- nada transcrito aún con "
                         "el lock activo")
    entradas = []
    for ln in jpath.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entradas.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    abiertos: dict[tuple, float] = {}
    intervalos: list[tuple[float, float, str, int]] = []
    for e in entradas:
        clave = (e.get("pid"), e.get("script"))
        if e.get("evento") == "inicio":
            abiertos[clave] = e.get("ts", 0.0)
        elif e.get("evento") == "fin":
            ini = abiertos.pop(clave, e.get("ts", 0.0) - e.get("duracion_seg", 0.0))
            intervalos.append((ini, e.get("ts", 0.0), e.get("script", "?"), e.get("pid", 0)))
    solapes = []
    for i in range(len(intervalos)):
        for j in range(i + 1, len(intervalos)):
            a, b = intervalos[i], intervalos[j]
            if a[3] != b[3] and a[0] < b[1] and b[0] < a[1]:
                solapes.append(f"{a[2]}(pid={a[3]}) solapó con {b[2]}(pid={b[3]})")
    if solapes:
        return Veredicto(FALLA, "lección 43 violada: " + "; ".join(solapes[:3]))
    return Veredicto(PASA, f"{len(intervalos)} transcripción(es) en el journal, "
                     f"ninguna solapada")


def check_b4_retrabajo(ctx: Ctx) -> Veredicto:
    corridas = _corridas_de_pipeline(ctx)
    if len(corridas) < 2:
        return Veredicto(NO_APLICA, "menos de 2 corridas -- no hay con qué comparar")
    notas = _leer_notas(ctx)
    por_paso: dict[str, list[dict]] = {}
    for _log, data in corridas:
        for p in data.get("pasos", []):
            por_paso.setdefault(p["id"], []).append(p)
    repetidos = []
    for pid, pasos in por_paso.items():
        ceros = [p for p in pasos if p.get("delta") == 0]
        if len(ceros) >= 2 and pid not in notas:
            repetidos.append(f"{pid} x{len(ceros)}")
    if repetidos:
        return Veredicto(FALLA, "re-trabajo mudo, directiva raíz 2 (1 procesar bien > "
                         "10 procesar de más): " + ", ".join(repetidos))
    return Veredicto(PASA, f"{len(corridas)} corrida(s) comparadas, sin re-trabajo mudo")


def check_b5_pasos_sueltos(ctx: Ctx) -> Veredicto:
    """MEJOR ESFUERZO, no exhaustivo: no todos los scripts dejan log propio
    bajo <root>/.cinema_assistant/logs/. Evidencia disponible: si el log de
    un script cae FUERA de la ventana temporal de cualquier corrida orquestada
    y sin nota, es indicio de un paso corrido a mano (ESCALANDO MEXICO: seis
    sectores sin sync ni markers por exactamente esto)."""
    logs_proyecto = sorted((ctx.root / ".cinema_assistant" / "logs").glob("*.log"))
    if not logs_proyecto:
        return Veredicto(NO_APLICA, "el proyecto no tiene logs propios por script todavía")
    ventanas = []
    for log, data in _corridas_de_pipeline(ctx):
        ts = _ts_de_nombre(log)
        if ts is None:
            continue
        duracion = sum(p.get("segundos", 0.0) for p in data.get("pasos", []))
        ventanas.append((ts - 30, ts + duracion + 120))
    notas = _leer_notas(ctx)
    sueltos = []
    for lp in logs_proyecto:
        ts = lp.stat().st_mtime
        if any(a <= ts <= b for a, b in ventanas):
            continue
        clave = lp.stem.rsplit("_", 1)[0] if "_" in lp.stem else lp.stem
        if clave in notas or lp.stem in notas:
            continue
        sueltos.append(lp.name)
    if sueltos:
        return Veredicto(FALLA, f"{len(sueltos)} log(s) de script fuera de cualquier "
                         f"corrida orquestada y sin nota: " + ", ".join(sueltos[:5]))
    return Veredicto(PASA, f"{len(logs_proyecto)} log(s) de proyecto, todos dentro de "
                     f"corridas orquestadas o reconocidos en acta-notas.md")


# --------------------------------------------------------------------------
# G2 -- Antes de que el editor vea nada
# --------------------------------------------------------------------------

def _args_multicam(ctx: Ctx) -> list[str] | None:
    lua = ENGINE / "resolve" / f"{_slug(ctx)}_multicam.lua"
    if not lua.exists():
        return None
    return ["--root", str(ctx.root), "--lua", str(lua)]


VERIFICADORES_12B: list[tuple[str, Callable[[Ctx], list[str] | None]]] = [
    ("verify_interviews.py", lambda ctx: ["--root", str(ctx.root), "--fail-on-found"]),
    ("verify_coverage.py", lambda ctx: ["--root", str(ctx.root),
                                        "--project-prefix", ctx.prefix]),
    ("lint_pipeline.py", lambda ctx: []),
    ("verify_lav_offsets.py", lambda ctx: ["--root", str(ctx.root)]),
    ("verify_multicam_placement.py", _args_multicam),
    ("surface_transcript_suspects.py", lambda ctx: ["--root", str(ctx.root)]),
]


def check_c1_verificadores_12b(ctx: Ctx) -> Veredicto:
    resultados = []
    for script, argf in VERIFICADORES_12B:
        argv = argf(ctx)
        if argv is None:
            resultados.append((script, NO_APLICA, "sin *_multicam.lua -- proyecto "
                              "sin multicámara"))
            continue
        try:
            proc = _run([sys.executable, str(HERE / script)] + argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            resultados.append((script, FALLA, str(e)))
            continue
        if proc.returncode == 0:
            resultados.append((script, PASA, f"exit=0"))
        else:
            resultados.append((script, FALLA,
                              f"exit={proc.returncode}: {_ultima_linea_util(proc.stderr or proc.stdout)}"))
    fallidos = [r for r in resultados if r[1] == FALLA]
    resumen = "; ".join(f"{s}={st}" for s, st, _ in resultados)
    if fallidos:
        return Veredicto(FALLA, "; ".join(f"{s}: {m}" for s, _, m in fallidos)
                         + f"  [{resumen}]")
    return Veredicto(PASA, resumen)


def check_c2_curaduria_comprension(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    try:
        sin_curar = conn.execute(
            "SELECT COUNT(*) FROM question_segments "
            "WHERE COALESCE(TRIM(question_short),'') = ''").fetchone()[0]
    except sqlite3.OperationalError:
        return Veredicto(NO_APLICA, "manifest sin columna question_short (versión vieja "
                         "del schema)")
    reportes = sorted((ctx.reports_dir).glob("curaduria-transcript-*.md"))
    if not reportes:
        if sin_curar > 0:
            return Veredicto(FALLA, f"{sin_curar} preguntas sin question_short y ningún "
                             f"reporte de curaduría generado (lección 46, garble plausible)")
        n_preguntas = table_count(conn, "question_segments")
        if n_preguntas == 0:
            return Veredicto(NO_APLICA, "sin preguntas de entrevista en este proyecto")
        return Veredicto(PASA, f"{n_preguntas} pregunta(s), todas con question_short, "
                         f"sin reporte formal (no hizo falta)")
    ultimo = reportes[-1]
    ts_transcripts = _mtime_max(ctx.root / ".cinema_assistant" / "transcripts")
    if ts_transcripts and ultimo.stat().st_mtime < ts_transcripts:
        return Veredicto(FALLA, f"{ultimo.name} es ANTERIOR al último transcript -- la "
                         f"curaduría no vio el material más reciente")
    if sin_curar > 0:
        return Veredicto(FALLA, f"{sin_curar} preguntas siguen sin question_short pese a "
                         f"tener reporte de curaduría ({ultimo.name})")
    n_claude = table_count(conn, "clip_curated_segments", "curated_by='claude'")
    return Veredicto(PASA, f"{ultimo.name} posterior a los transcripts, 0 preguntas sin "
                     f"curar, {n_claude} tramo(s) curado(s) por Claude (§11b)")


def check_c3_invariantes_sql(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    problemas = []
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM clip_curated_segments cs "
            "JOIN clips c ON c.id=cs.clip_id "
            "WHERE cs.end_sec > c.duration_sec + 0.5").fetchone()[0]
        if n:
            problemas.append(f"{n} tramo(s) exceden la duración del clip")
    except sqlite3.OperationalError:
        pass
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM audio_sync_pairs sp "
            "JOIN clips a ON a.id=sp.audio_clip_id "
            "WHERE ABS(sp.offset_sec) > a.duration_sec").fetchone()[0]
        if n:
            problemas.append(f"{n} offset(s) de sync mayores que la duración del audio")
    except sqlite3.OperationalError:
        pass
    muestra = conn.execute(
        "SELECT path FROM clips WHERE file_kind='video' "
        "ORDER BY RANDOM() LIMIT 5").fetchall()
    faltantes = [p for (p,) in muestra if not Path(p).exists()]
    if faltantes:
        problemas.append(f"{len(faltantes)}/{len(muestra)} paths de la muestra NO "
                         f"existen en disco")
    if problemas:
        return Veredicto(FALLA, "; ".join(problemas))
    return Veredicto(PASA, "tramos, offsets y muestra de paths -- todo dentro de rango (§13)")


def check_c4_bake_actualizado(ctx: Ctx) -> Veredicto:
    bake_dir = ctx.root / ".cinema_assistant" / "resolve"
    bakes = sorted(bake_dir.glob("*_data.lua")) if bake_dir.is_dir() else []
    if not bakes:
        return Veredicto(NO_APLICA, "sin horneado todavía -- normal antes del §11")
    bake = max(bakes, key=lambda p: p.stat().st_mtime)
    # SQLite no guarda mtime por fila: se usa el mtime del propio manifest.sqlite
    # como proxy de "hubo escritura reciente". Proxy grueso (no distingue QUÉ
    # tabla cambió) pero honesto sobre su límite.
    ultima_escritura = ctx.db.stat().st_mtime
    if ultima_escritura > bake.stat().st_mtime + 5:
        return Veredicto(FALLA, f"{bake.name} es anterior al último cambio del manifest "
                         f"-- §11b paso 4, re-hornear con export_lua_data.py")
    return Veredicto(PASA, f"{bake.name} es posterior al último cambio del manifest")


def check_c5_lua_limpio(ctx: Ctx) -> Veredicto:
    slug = _slug(ctx)
    bake_dir = ctx.root / ".cinema_assistant" / "resolve"
    bakes = sorted(bake_dir.glob("*_data.lua")) if bake_dir.is_dir() else []
    if not bakes:
        return Veredicto(NO_APLICA, "sin horneado todavía")
    bake = max(bakes, key=lambda p: p.stat().st_mtime)
    asistente_lua = ENGINE / "resolve" / f"asistente_{slug}.lua"
    if not asistente_lua.exists():
        return Veredicto(FALLA, f"hay bake en {bake_dir.name}/ pero no existe "
                         f"resolve/{asistente_lua.name} en el motor "
                         f"(bin/nuevo_asistente_proyecto.py)")
    luac = shutil.which("luac")
    if not luac:
        return Veredicto(FALLA, "luac no está instalado -- no se puede validar sintaxis Lua")
    for target in (bake, asistente_lua):
        proc = _run([luac, "-p", str(target)], timeout=30)
        if proc.returncode != 0:
            return Veredicto(FALLA, f"luac -p falló sobre {target.name}: "
                             f"{proc.stderr.strip()[-300:]}")
    mock = ENGINE / "resolve" / "mock_resolve_smoke.lua"
    lua_bin = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    if not lua_bin or not mock.exists():
        return Veredicto(FALLA, "luac -p limpio, pero falta lua nativo o "
                         "mock_resolve_smoke.lua -- el smoke con mock es obligatorio "
                         "(luac -p no caza globals nil)")
    proc2 = _run([lua_bin, str(mock), str(asistente_lua)], timeout=60)
    if proc2.returncode != 0 or "FALLO" in proc2.stdout:
        return Veredicto(FALLA, f"smoke con mock falló: "
                         f"{(proc2.stdout + proc2.stderr).strip()[-300:]}")
    return Veredicto(PASA, "luac -p limpio en data+asistente, smoke con mock OK")


def check_c6_multicam_placement(ctx: Ctx) -> Veredicto:
    slug = _slug(ctx)
    lua_mc = ENGINE / "resolve" / f"{slug}_multicam.lua"
    if not lua_mc.exists():
        return Veredicto(NO_APLICA, "sin *_multicam.lua -- proyecto sin multicámara")
    asistente_lua = ENGINE / "resolve" / f"asistente_{slug}.lua"
    texto = (asistente_lua.read_text(encoding="utf-8", errors="replace")
            if asistente_lua.exists() else "")
    tiene_assert = "MC_PLACED" in texto and "MC_FAILED" in texto
    try:
        proc = _run([sys.executable, str(HERE / "verify_multicam_placement.py"),
                    "--root", str(ctx.root), "--lua", str(lua_mc)])
    except (OSError, subprocess.TimeoutExpired) as e:
        return Veredicto(FALLA, f"verify_multicam_placement.py no corrió: {e}")
    if proc.returncode != 0:
        return Veredicto(FALLA, f"verify_multicam_placement.py exit={proc.returncode} "
                         f"-- un par se perdería en silencio (FCC 2026-07-11)")
    if not tiene_assert:
        return Veredicto(FALLA, f"{asistente_lua.name} no tiene el assert de cierre "
                         f"MC_PLACED/MC_FAILED -- sin defensa en profundidad si el "
                         f"verifier no corre")
    return Veredicto(PASA, "simulación fuera de Resolve OK + assert de cierre presente")


# --------------------------------------------------------------------------
# G3 -- Cierre
# --------------------------------------------------------------------------

def check_d1_auto_review(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    cobertura = {
        "videos": table_count(conn, "clips", "file_kind='video' AND index_status='ok'"),
        "analizados": table_count(conn, "clip_analysis",
                                  "analysis_status IN ('keep','review','cull')"),
        "con_curated": table_count(conn, "clip_curated_segments"),
        "sync_pairs": table_count(conn, "audio_sync_pairs"),
        "preguntas": table_count(conn, "question_segments"),
    }
    reportes = sorted(ctx.reports_dir.glob("*.md")) if ctx.reports_dir.is_dir() else []
    if not reportes:
        return Veredicto(FALLA, f"cobertura {cobertura} pero cero reportes en reports/ -- "
                         f"sin evidencia escrita de auto-review (cierre obligatorio "
                         f"2026-05-25)")
    return Veredicto(PASA, f"cobertura {cobertura}; {len(reportes)} reporte(s) en reports/")


def check_d2_auditoria_exhaustiva(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    try:
        rows = conn.execute("""
            SELECT c.id, c.filename FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id = c.id
            WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= 30
              AND (d.category IS NULL OR d.category = '')
              AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)
        """).fetchall()
    except sqlite3.OperationalError:
        return Veredicto(NO_APLICA, "tablas de categorización aún no existen")
    if rows:
        muestra = ", ".join(fn for _cid, fn in rows[:3])
        return Veredicto(FALLA, f"{len(rows)} clip(s) >=30s con audio, sin sync y sin "
                         f"categoría (audit ad-hoc §12c, Zezzions 2026-05-26): {muestra}")
    return Veredicto(PASA, "sin clips huérfanos del audit ad-hoc §12c")


def check_d3_doctrina_actualizada(ctx: Ctx) -> Veredicto:
    baseline_path = ctx.reports_dir / "acta-baseline.json"
    if not baseline_path.exists():
        return Veredicto(NO_APLICA, "sin línea base todavía (correr la puerta G0 primero)")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    tomado_ts = baseline.get("tomado_ts", 0)
    doctrina = Path.home() / "memoria-asistente-edicion"
    if not doctrina.is_dir():
        return Veredicto(NO_APLICA, "sin doctrina local instalada en esta máquina")
    tocados = [p for p in doctrina.rglob("*.md")
              if ".git" not in p.parts and p.stat().st_mtime > tomado_ts]
    if not tocados:
        return Veredicto(FALLA, "ninguna página de la doctrina se tocó desde que arrancó "
                         "la sesión -- directiva raíz 0: lo aprendido no se escribió de "
                         "vuelta (puede ser legítimo si no hubo lección nueva)")
    return Veredicto(PASA, f"{len(tocados)} página(s) de doctrina actualizada(s) esta "
                     f"sesión: " + ", ".join(p.name for p in tocados[:5]))


def check_d4_boveda(ctx: Ctx) -> Veredicto:
    vault = Path.home() / "memoria-creativa"
    if not vault.is_dir():
        return Veredicto(NO_APLICA, "sin bóveda de memoria creativa en esta máquina "
                         "(§13b es opcional)")
    pendientes = vault / "_cambios" / "pendientes"
    if not pendientes.is_dir():
        return Veredicto(FALLA, "hay bóveda pero exportar_a_memoria.py nunca corrió "
                         "para ningún proyecto (§13b)")
    slug = _slug(ctx)
    candidatos = list(pendientes.glob(f"*{slug}*"))
    if not candidatos:
        return Veredicto(FALLA, f"hay bóveda pero ninguna propuesta pendiente para "
                         f"'{slug}' en _cambios/pendientes/ (§13b)")
    return Veredicto(PASA, f"{len(candidatos)} propuesta(s) para '{slug}' en la bóveda")


# --------------------------------------------------------------------------
# Invariantes transversales
# --------------------------------------------------------------------------

LIMITE_MUESTRA_I1 = 5000


def check_i1_fuentes_intactas(ctx: Ctx) -> Veredicto:
    conn = ctx.conn()
    total = table_count(conn, "clips", "file_kind IN ('video','audio') "
                        "AND index_status='ok'")
    if total > LIMITE_MUESTRA_I1:
        rows = conn.execute(
            "SELECT path FROM clips WHERE file_kind IN ('video','audio') "
            "AND index_status='ok' ORDER BY RANDOM() LIMIT ?",
            (LIMITE_MUESTRA_I1,)).fetchall()
        nota_muestra = f" (muestra de {len(rows)}/{total})"
    else:
        rows = conn.execute(
            "SELECT path FROM clips WHERE file_kind IN ('video','audio') "
            "AND index_status='ok'").fetchall()
        nota_muestra = ""
    faltantes = [p for (p,) in rows if not Path(p).exists()]
    if faltantes:
        return Veredicto(FALLA, f"{len(faltantes)}/{len(rows)} ruta(s) indexada(s) ya NO "
                         f"existen en disco{nota_muestra} (regla dura nº1 -- material "
                         f"irreemplazable): " + "; ".join(faltantes[:3]))
    return Veredicto(PASA, f"{len(rows)} ruta(s) verificadas{nota_muestra}, todas "
                     f"presentes en disco")


def check_i2_orden_pistas(ctx: Ctx) -> Veredicto:
    script = HERE / "verify_track_order.py"
    if not script.exists():
        return Veredicto(NO_APLICA, "verify_track_order.py no existe en esta versión "
                         "del motor")
    # El *_layout.json solo existe DESPUES de que asistente_<p>.lua construyo
    # las timelines dentro de Resolve -- verify_track_order.py sale 1 pidiendo
    # correrlo ahi primero. Eso es NO-APLICA (aun no se llega a ese paso), no
    # una FALLA de la regla de orden.
    layouts = sorted((ctx.root / ".cinema_assistant" / "resolve").glob("*_layout.json"))
    if not layouts:
        return Veredicto(NO_APLICA, "sin *_layout.json todavía -- se genera al construir "
                         "las timelines desde la Consola de Resolve, después del bake")
    try:
        proc = _run([sys.executable, str(script), "--root", str(ctx.root)])
    except (OSError, subprocess.TimeoutExpired) as e:
        return Veredicto(FALLA, f"verify_track_order.py no corrió: {e}")
    if proc.returncode != 0:
        return Veredicto(FALLA, f"verify_track_order.py exit={proc.returncode}: "
                         f"{_ultima_linea_util(proc.stderr or proc.stdout)}")
    return Veredicto(PASA, "orden de pistas verificado (A1 cámara, externos después)")


# `[^)]*` se para en el PRIMER ')' -- con un argumento que anida parens
# (string.format(...), tostring(...)) trunca la llamada antes de llegar al
# argumento de duracion real y produce falsos positivos. Cruza un nivel de
# anidamiento, mismo patron que RE_TIMEOUT_EXPLICITO en run_pipeline.py.
RE_ADDMARKER = re.compile(r"AddMarker\(((?:[^()]|\([^()]*\))*)\)")


def _split_args_nivel_superior(s: str) -> list[str]:
    """Separa por comas SOLO fuera de parentesis/corchetes anidados -- un
    split(',') ingenuo corta tambien las comas de dentro de
    string.format(...) y desalinea la posicion del argumento."""
    args: list[str] = []
    buf: list[str] = []
    profundidad = 0
    for ch in s:
        if ch in "([":
            profundidad += 1
            buf.append(ch)
        elif ch in ")]":
            profundidad -= 1
            buf.append(ch)
        elif ch == "," and profundidad == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    args.append("".join(buf))
    return [a.strip() for a in args]


def check_i3_sin_duration_markers(ctx: Ctx) -> Veredicto:
    lib_lua = ENGINE / "resolve" / "asistente_lib.lua"
    if not lib_lua.exists():
        return Veredicto(FALLA, "asistente_lib.lua no existe -- no se puede verificar "
                         "la política de markers")
    texto = lib_lua.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"LIB\.DURACION_MARKER\s*=\s*(\d+)", texto)
    if not m or m.group(1) != "1":
        return Veredicto(FALLA, f"LIB.DURACION_MARKER = {m.group(1) if m else '?'} "
                         f"(debe ser 1 -- política FCC 2026-07-11, sin duration markers)")
    sospechosas = []
    for f in sorted((ENGINE / "resolve").glob("*.lua")):
        txt = f.read_text(encoding="utf-8", errors="replace")
        for call in RE_ADDMARKER.findall(txt):
            args = _split_args_nivel_superior(call)
            if len(args) < 5:
                continue    # firma corta (p.ej. tl:AddMarker sin customData) -- no es lo que se audita
            quinto = args[4]
            if "DURACION_MARKER" not in quinto and quinto != "1":
                sospechosas.append(f"{f.name}: 5to arg={quinto!r} en "
                                   f"AddMarker({call[:50]}...)")
    if sospechosas:
        return Veredicto(FALLA, f"{len(sospechosas)} llamada(s) a AddMarker sin duración=1 "
                         f"ni LIB.DURACION_MARKER: " + "; ".join(sospechosas[:3]))
    return Veredicto(PASA, "LIB.DURACION_MARKER=1 y todas las llamadas AddMarker usan "
                     "duración 1")


def check_i4_convencion_offset(ctx: Ctx) -> Veredicto:
    script = HERE / "verify_sync_physical.py"
    if not script.exists() or table_count(ctx.conn(), "audio_sync_pairs") == 0:
        return Veredicto(NO_APLICA, "sin audio_sync_pairs -- proyecto sin sync externo "
                         "o aún no llega ahí")
    try:
        proc = _run([sys.executable, str(script), "--root", str(ctx.root)])
    except (OSError, subprocess.TimeoutExpired) as e:
        return Veredicto(FALLA, f"verify_sync_physical.py no corrió: {e}")
    if proc.returncode != 0:
        return Veredicto(FALLA, f"verify_sync_physical.py exit={proc.returncode} -- "
                         f"posible signo de offset invertido (lección 6)")
    return Veredicto(PASA, "verificación física de offsets OK "
                     "(convención audio_start - video_start)")


# --------------------------------------------------------------------------
# Registro
# --------------------------------------------------------------------------

CHECKPOINTS: list[Checkpoint] = [
    Checkpoint("A1", "G0", "línea base tomada antes del primer paso de escritura",
              "§13 'cobertura del pipeline'", check_a1_baseline),
    Checkpoint("A2", "G0", "--project-prefix declarado y casa con material real",
              "Lección 47 -- Fantástico Cómics quedó a medias", check_a2_prefix),
    Checkpoint("A3", "G0", "capacidades de la máquina conocidas y su consecuencia listada",
              "Identidad por cara sin correr meses por python3 pelado sin cv2",
              check_a3_capacidades),
    Checkpoint("A4", "G0", "novedades de doctrina revisadas",
              "El plugin llegó a distribuir errores-comunes con 44 líneas menos",
              check_a4_novedades, obligatorio=False),

    Checkpoint("B1", "G1", "ninguna etapa con trabajo cero sin motivo reconocido",
              "Lecciones 40, 47, 50, 51 -- '0 procesados' con exit 0",
              check_b1_trabajo_cero),
    Checkpoint("B2", "G1", "dependencias de tabla satisfechas antes de cada paso",
              "curate_segments muere con 'no such table' sin avisar",
              check_b2_dependencias),
    Checkpoint("B3", "G1", "nunca dos Whisper a la vez",
              "Lección 43 -- comparten la GPU Metal y el largo se arrastra",
              check_b3_whisper_lock),
    Checkpoint("B4", "G1", "sin re-trabajo mudo",
              "Directiva raíz 2 -- optimización constante", check_b4_retrabajo),
    Checkpoint("B5", "G1", "pasos sueltos fuera del orquestador, justificados",
              "ESCALANDO MEXICO -- seis sectores sin sync ni markers por pasos a mano",
              check_b5_pasos_sueltos, obligatorio=False),

    Checkpoint("C1", "G2", "los seis verificadores del §12b en verde o NO-APLICA "
              "estructural", "Zezzions perdió las entrevistas 2794 y 2786",
              check_c1_verificadores_12b),
    Checkpoint("C2", "G2", "curaduría por comprensión con correlato físico",
              "Lección 46 -- garble plausible que el audit estadístico no ve",
              check_c2_curaduria_comprension),
    Checkpoint("C3", "G2", "invariantes SQL del §13 en cero",
              "§13 checklist de cierre", check_c3_invariantes_sql),
    Checkpoint("C4", "G2", "el bake es más nuevo que el último dato que lo alimenta",
              "§11b paso 4 -- curar y olvidar re-hornear", check_c4_bake_actualizado),
    Checkpoint("C5", "G2", "el Lua carga limpio (luac -p + smoke con mock)",
              "'luac -p no caza globals nil'", check_c5_lua_limpio),
    Checkpoint("C6", "G2", "que la API devolviera algo no significa que lo hiciera",
              "FCC 2026-07-11 -- un par triple-verificado perdido en silencio",
              check_c6_multicam_placement),

    Checkpoint("D1", "G3", "auto-review del §13 con números honestos",
              "Cierre obligatorio 2026-05-25", check_d1_auto_review),
    Checkpoint("D2", "G3", "auditoría exhaustiva del §12c resuelta ítem por ítem",
              "Zezzions 2026-05-26, instrucción literal del usuario",
              check_d2_auditoria_exhaustiva),
    Checkpoint("D3", "G3", "lo aprendido escrito de vuelta en la doctrina",
              "Directiva raíz 0 -- el próximo proyecto no reinventa",
              check_d3_doctrina_actualizada, obligatorio=False),
    Checkpoint("D4", "G3", "puente a la bóveda de memoria creativa (opcional)",
              "§13b -- cinco proyectos entre junio y julio de 2026, nada llegó a la bóveda",
              check_d4_boveda, obligatorio=False),

    Checkpoint("I1", "I", "ninguna fuente borrada, movida ni renombrada",
              "Regla dura nº1 -- el material rodado es irreemplazable",
              check_i1_fuentes_intactas),
    Checkpoint("I2", "I", "audios externos siempre después de los de cámara",
              "verify_track_order, regla dura v0.2.0", check_i2_orden_pistas),
    Checkpoint("I3", "I", "sin duration markers en lo que se hornea",
              "Política del usuario, FCC 2026-07-11", check_i3_sin_duration_markers),
    Checkpoint("I4", "I", "convención de offset única (audio_start - video_start)",
              "Lección 6", check_i4_convencion_offset),
]

_PUERTAS_VALIDAS = ["G0", "G1", "G2", "G3", "I"]


# --------------------------------------------------------------------------
# Salida
# --------------------------------------------------------------------------

def _a_json(resultados: list[tuple[Checkpoint, Veredicto]]) -> dict:
    return {
        "checkpoints": [
            {"id": c.id, "puerta": c.puerta, "descripcion": c.descripcion,
             "caso_fundador": c.caso_fundador, "obligatorio": c.obligatorio,
             "estado": v.estado, "detalle": v.detalle}
            for c, v in resultados
        ],
    }


def _imprimir(resultados: list[tuple[Checkpoint, Veredicto]]) -> None:
    puerta_actual = None
    for c, v in resultados:
        if c.puerta != puerta_actual:
            puerta_actual = c.puerta
            print(f"\n-- puerta {puerta_actual} " + "-" * (60 - len(puerta_actual)))
        marca = {"PASA": "OK ", "FALLA": "!! ", "NO-APLICA": "-- "}[v.estado]
        obl = "" if c.obligatorio else " (informativo)"
        print(f"  {marca}{c.id:<4} {v.estado:<10}{obl}  {c.descripcion}")
        if v.detalle:
            print(f"        {v.detalle}")


def _escribir_acta(ctx: Ctx, resultados: list[tuple[Checkpoint, Veredicto]]) -> None:
    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    fecha = time.strftime("%Y-%m-%d")
    base = ctx.reports_dir / f"acta-{fecha}"
    payload = _a_json(resultados)
    payload["root"] = str(ctx.root)
    payload["prefix"] = ctx.prefix
    payload["generado_ts"] = time.time()
    (base.with_suffix(".json")).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    L = [f"# Acta de conducta -- {fecha}", "",
        f"Proyecto: `{ctx.root}` (prefix {ctx.prefix!r})", "",
        "Doctrina: `metodologia/pruebas-del-asistente.md`. Ninguna declaración sin "
        "correlato físico -- cada PASA/FALLA cita la evidencia que lo decidió, no "
        "la palabra del asistente.", ""]
    puerta_actual = None
    for c, v in resultados:
        if c.puerta != puerta_actual:
            puerta_actual = c.puerta
            L.append(f"## Puerta {puerta_actual}")
            L.append("")
        obl = "" if c.obligatorio else " _(informativo)_"
        L.append(f"- **{c.id}** [{v.estado}]{obl} -- {c.descripcion}")
        L.append(f"  - Caso fundador: {c.caso_fundador}")
        if v.detalle:
            L.append(f"  - Evidencia: {v.detalle}")
    fallidos_ob = [c for c, v in resultados if v.estado == FALLA and c.obligatorio]
    L.append("")
    if fallidos_ob:
        L.append(f"**{len(fallidos_ob)} checkpoint(s) obligatorio(s) en FALLA: "
                 + ", ".join(c.id for c in fallidos_ob) + ".**")
    else:
        L.append("Sin checkpoints obligatorios en FALLA.")
    (base.with_suffix(".md")).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\nActa escrita: {base.with_suffix('.md')}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_root(root_arg: str) -> Path:
    import glob
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", help="raíz del disco del proyecto")
    ap.add_argument("--project-prefix", default=None,
                    help="'' para proyecto plano. Si no se pasa, se lee de "
                         "project_config.json.")
    ap.add_argument("--puerta", choices=_PUERTAS_VALIDAS, default=None,
                    help="correr solo esta puerta")
    ap.add_argument("--todas", action="store_true", help="correr las cuatro puertas "
                    "más los invariantes (default si no se pasa --puerta)")
    ap.add_argument("--acta", action="store_true", help="escribe "
                    "reports/acta-<fecha>.md + .json")
    ap.add_argument("--json", action="store_true", help="imprime el resultado como "
                    "JSON a stdout")
    ap.add_argument("--listar", action="store_true", help="lista los checkpoints y sale")
    args = ap.parse_args()

    if args.listar:
        for c in CHECKPOINTS:
            print(f"  {c.id:<4} [{c.puerta:<3}] {c.descripcion}")
        return 0

    if not args.root:
        print("Falta --root (solo --listar funciona sin disco).", file=sys.stderr)
        return 2

    root = resolve_root(args.root.rstrip("/")).expanduser()
    if not root.is_dir():
        print(f"Disco no encontrado: {root}", file=sys.stderr)
        return 2
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        print(f"Manifest no existe: {db}\nCorre primero index_project.py.",
              file=sys.stderr)
        return 2

    try:
        forma = proyecto.detectar(root, prefix_flag=args.project_prefix, persistir=False)
    except proyecto.FormaAmbigua as e:
        print(str(e), file=sys.stderr)
        return 2

    ctx = Ctx(root=root, prefix=forma.prefix, db=db,
              reports_dir=root / ".cinema_assistant" / "reports")

    puertas = [args.puerta] if args.puerta else _PUERTAS_VALIDAS
    seleccion = [c for c in CHECKPOINTS if c.puerta in puertas]

    resultados: list[tuple[Checkpoint, Veredicto]] = []
    for c in seleccion:
        try:
            v = c.fn(ctx)
        except Exception as e:  # un checkpoint roto no debe tumbar el acta entera
            v = Veredicto(FALLA, f"el checkpoint reventó: {type(e).__name__}: {e}")
        resultados.append((c, v))

    ctx.cerrar()

    _imprimir(resultados)
    if args.json:
        print(json.dumps(_a_json(resultados), ensure_ascii=False, indent=2))
    if args.acta:
        _escribir_acta(ctx, resultados)

    fallidos_ob = [c for c, v in resultados if v.estado == FALLA and c.obligatorio]
    print(f"\n{len(fallidos_ob)} checkpoint(s) obligatorio(s) en FALLA de "
         f"{len(seleccion)} evaluado(s).")
    return EXIT_HALLAZGOS if fallidos_ob else 0


if __name__ == "__main__":
    sys.exit(main())
