"""Guardas de trabajo-cero para los scripts de bin/.

Clase de fallo que atajan (lecciones 40, 47, 50, 51 de
~/memoria-asistente-edicion/lecciones/errores-comunes-a-corregir.md):
un script procesa 0 elementos por un default de filtro heredado de otro
proyecto, imprime "Listo. 0 transcritos" y sale con exit 0. Un script que no
hace nada y dice que todo salio bien es peor que uno que truena.

Regla (leccion 40): "cuando una fase reporta 0 items procesados en un
proyecto con material, es un BUG de seleccion hasta demostrar lo contrario
— no 'no habia nada que hacer'".

Generaliza assert_grew() de bin/run_full_pipeline.sh:49, pero
  - en Python, llamable desde dentro de cualquier script suelto;
  - sin minimos absolutos hardcodeados (compara DELTA, no piso);
  - sin nada de ESCALANDO MEXICO;
  - con exit code propio, distinguible.

Uso canonico:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.guards import assert_selected, assert_grew, report_done, table_count

    rows = conn.execute(SQL, (args.audio_like.lower(),)).fetchall()
    assert_selected(rows, "audios a transcribir", filters={
        "--root": str(root), "--audio-like": args.audio_like,
    }, hint="En proyecto plano (carpeta=cadena) probar --audio-like 'audio/%'")

    before = table_count(conn, "transcripts")
    ...trabajo...
    assert_grew(conn, "transcripts", before, what="transcripts nuevos")
    report_done("transcribe_audios", con_texto=done, sin_habla=empty, cache=cached)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

EXIT_NO_WORK = 3   # 1 = precondicion (root/manifest), 2 = uso, 3 = trabajo cero


def _allow_empty() -> bool:
    """Valvula de escape explicita: ALLOW_EMPTY=1 python3 bin/foo.py ...

    Existe para el caso legitimo (re-correr un paso idempotente ya completo).
    Es deliberadamente incomodo: obliga a decir en voz alta 'si, espero cero'.
    """
    return os.environ.get("ALLOW_EMPTY", "").strip().lower() not in ("", "0", "false", "no")


def _die(lines: list[str]) -> None:
    if _allow_empty():
        lines.append("ALLOW_EMPTY=1 -> continuo de todos modos.")
        print("\n".join(lines), file=sys.stderr, flush=True)
        return
    lines.append("Si de verdad esperabas 0, re-corre con ALLOW_EMPTY=1.")
    print("\n".join(lines), file=sys.stderr, flush=True)
    sys.exit(EXIT_NO_WORK)


def assert_selected(items, what: str, *, filters: dict | None = None,
                    hint: str = "", min_count: int = 1) -> int:
    """Aborta si la SELECCION vino vacia. Llamar justo despues del fetchall().

    `items` puede ser una lista o un int. `filters` es el diccionario de los
    flags EFECTIVOS de esta corrida — se imprimen para que el diagnostico sea
    inmediato y no haya que leer el --help.
    """
    n = len(items) if hasattr(items, "__len__") else int(items)
    if n >= min_count:
        return n
    lines = [
        "",
        "=" * 68,
        f"TRABAJO CERO: 0 {what} seleccionados (se esperaban >= {min_count}).",
        "=" * 68,
        "Esto es un BUG DE SELECCION hasta demostrar lo contrario, no",
        "'no habia nada que hacer' (leccion 40, FCC 2026-07-09).",
    ]
    if filters:
        lines.append("Filtros efectivos en esta corrida:")
        width = max(len(k) for k in filters)
        for k, v in filters.items():
            lines.append(f"    {k:<{width}} = {v!r}")
    if hint:
        lines.append(f"Sugerencia: {hint}")
    _die(lines)
    return n


# --------------------------------------------------------------------------
# --project-prefix: obligatorio, sin default
# --------------------------------------------------------------------------
# Cinco scripts (verify_coverage, export_lua_data, build_metadata_payload,
# analyze_segments, derive_video_categories) tenian default="ESCALANDO MEXICO/".
# En cualquier otro proyecto el LIKE no casaba con nada y salian con exit 0
# diciendo que todo estaba bien: asi quedo FANTASTICO COMICS a medias. El default
# se quito. Como no hay forma de adivinar la forma del manifest, se pregunta —
# pero se pregunta UNA vez y con la respuesta a mano, no con un "falta un flag".


def exigir_tablas(conn: sqlite3.Connection, requeridas: dict[str, str],
                  script: str) -> None:
    """Aborta con un mensaje util si faltan tablas de pasos anteriores.

    `requeridas` es {tabla: "comando que la crea"}.

    Nacio de la auditoria 2026-07-31: recien indexado un proyecto, el manifest
    solo tiene `clips` y `scan_runs`. Correr export_lua_data ahi —que es lo que
    hace cualquiera que quiera ver algo en Resolve cuanto antes— reventaba con

        sqlite3.OperationalError: no such table: clip_analysis

    Un traceback de SQLite no le dice a nadie que lo que falta es correr
    analyze_segments primero. El pipeline TIENE un orden; hay que decirlo cuando
    se rompe, no dar por hecho que se conoce.
    """
    existentes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    faltan = {t: cmd for t, cmd in requeridas.items() if t not in existentes}
    if not faltan:
        return
    lineas = [
        "",
        "=" * 68,
        f"FALTAN PASOS PREVIOS: {script} necesita datos que aun no existen.",
        "=" * 68,
        "Tabla ausente          Se crea corriendo",
        "-" * 68,
    ]
    for t, cmd in sorted(faltan.items()):
        lineas.append(f"  {t:<20} {cmd}")
    lineas += [
        "",
        "El playbook define el orden completo:",
        "  metodologia/pasos-a-seguir.md  (o references/pasos-a-seguir.md)",
    ]
    print("\n".join(lineas), file=sys.stderr, flush=True)
    sys.exit(1)


def dep_faltante(modulo: str, para: str, instalar: str) -> str:
    """Mensaje canonico cuando falta una dependencia OPCIONAL pesada.

    El motor corre con la stdlib mas numpy y Pillow, que es lo que instala el
    bootstrap. Un punado de scripts (caras, pose, embeddings de voz) necesitan
    paquetes de cientos de MB que no se instalan por defecto a proposito. Sin
    esto, quien siga la doctrina hasta ese paso se encuentra un ModuleNotFoundError
    pelado y no tiene forma de saber que era opcional ni que instalar.

    Devuelve el texto para sys.exit(). Los imports se hacen tolerantes para que
    `--help` siga funcionando sin las dependencias.
    """
    return (
        f"\nFalta una dependencia opcional: {modulo}\n"
        f"\n{para} necesita paquetes que el bootstrap NO instala por defecto\n"
        f"(pesan cientos de MB y el resto del pipeline no los usa):\n"
        f"\n    python3 -m pip install --user {instalar}\n"
        f"\nTodo lo demas del asistente funciona sin esto."
    )


def assert_prefix_casa(conn: sqlite3.Connection, pfx: str, script: str) -> int:
    """El --project-prefix tiene que casar con material real del manifest.

    Distinta de assert_selected: aqui no se juzga el trabajo del paso (que puede
    ser cero legitimamente — un proyecto sin entrevistas no tiene entrevistas),
    sino si el FILTRO apunta a alguna parte. Un prefix que no casa con un solo
    clip es siempre un error de invocacion, nunca un dato.

    Con pfx == '' no hay nada que comprobar: selecciona todo por definicion.
    """
    if not pfx:
        return -1
    n = int(conn.execute(
        "SELECT COUNT(*) FROM clips WHERE rel_path LIKE ? || '%'", (pfx,)
    ).fetchone()[0])
    if n > 0:
        return n
    muestra = [r[0] for r in conn.execute(
        "SELECT DISTINCT rel_path FROM clips WHERE file_kind='video' LIMIT 5"
    )]
    _die([
        "",
        "=" * 68,
        f"PREFIX QUE NO CASA: --project-prefix {pfx!r} no selecciona ni un clip.",
        "=" * 68,
        f"({script}) El manifest tiene material, pero ninguno bajo ese prefijo.",
        "Asi se ven las rutas que SI hay:",
        *(f"    {p}" for p in muestra),
        "Si el material cuelga directo del disco, el prefix correcto es ''.",
    ])
    return 0


def exigir_prefix(script: str) -> str:
    """Mensaje canonico cuando falta --project-prefix. Devuelve el texto para sys.exit()."""
    return (
        f"\n{'=' * 68}\n"
        f"{script}: falta --project-prefix (es obligatorio, no tiene default).\n"
        f"{'=' * 68}\n"
        "Como saber cual te toca:\n"
        "\n"
        "  Proyecto PLANO (las carpetas de primer nivel del disco son los\n"
        "  sectores/jornadas — el caso normal):\n"
        "      --project-prefix ''\n"
        "\n"
        "  Proyecto dentro de una CARPETA RAIZ (todo el material cuelga de\n"
        "  una carpeta con el nombre del proyecto):\n"
        "      --project-prefix 'MI PROYECTO/'\n"
        "\n"
        "Para ver la forma de tu manifest:\n"
        "  sqlite3 <disco>/.cinema_assistant/manifest.sqlite \\\n"
        "    \"SELECT DISTINCT rel_path FROM clips LIMIT 5;\"\n"
        "\n"
        "No hay default a proposito: cuando lo hubo, este script daba proyectos\n"
        "enteros por buenos sin haber mirado un solo clip."
    )


# --------------------------------------------------------------------------
# Material que ffmpeg no puede decodificar (BRAW, R3D, ARRIRAW)
# --------------------------------------------------------------------------
# Resolve SI los conforma nativo, asi que se indexan y se llevan a timeline con
# normalidad. Pero los pasos que necesitan DECODIFICAR (analyze_segments,
# transcribe_clips) tienen que saltarlos. La regla es la misma de siempre: un
# salto silencioso es un bug disfrazado — se salta DICIENDOLO.

SQL_DECODABLE = "COALESCE(decodable, 1) = 1"


def reportar_no_decodables(conn: sqlite3.Connection, paso: str,
                           where_extra: str = "", params=()) -> int:
    """Imprime que clips se saltaron por no ser decodificables, y por que.

    Llamar en los pasos que corren ffmpeg sobre el video, JUSTO despues de
    seleccionar. Devuelve cuantos se saltaron.
    """
    sql = ("SELECT filename, COALESCE(skip_reason,'') FROM clips "
           "WHERE file_kind='video' AND index_status='ok' "
           "AND COALESCE(decodable, 1) = 0")
    if where_extra:
        sql += f" AND {where_extra}"
    try:
        filas = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return 0   # manifest viejo sin la columna
    if not filas:
        return 0
    motivos = {}
    for fn, motivo in filas:
        motivos.setdefault(motivo or "sin motivo registrado", []).append(fn)
    print(f"\n  ⊘ {paso}: {len(filas)} clip(s) SALTADOS (ffmpeg no los decodifica)")
    for motivo, nombres in motivos.items():
        muestra = ", ".join(nombres[:3])
        resto = f" y {len(nombres)-3} mas" if len(nombres) > 3 else ""
        print(f"     {len(nombres):>4}  {motivo}")
        print(f"           ej: {muestra}{resto}")
    print("     Esos clips SI llegan a la timeline: Resolve los conforma nativo.\n")
    return len(filas)


def table_count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    """COUNT(*) tolerante: si la tabla no existe todavia devuelve 0.

    (Leccion 41: en proyecto fresco varias tablas aun no existen.)
    """
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def assert_grew(conn: sqlite3.Connection, table: str, before: int, *,
                what: str = "", min_delta: int = 1, where: str = "") -> int:
    """Aborta si la tabla no CRECIO. Delta real, sin pisos por proyecto.

    Reemplaza a assert_grew() de run_full_pipeline.sh, que compara contra un
    minimo absoluto (1700, el conteo de ESCALANDO MEXICO) y por eso da OK
    aunque la corrida de hoy no haya escrito una sola fila.
    """
    after = table_count(conn, table, where)
    delta = after - before
    label = what or f"filas nuevas en {table}"
    if delta >= min_delta:
        print(f"  OK: {label} = +{delta} ({before} -> {after})")
        return delta
    _die([
        "",
        "=" * 68,
        f"TRABAJO CERO: {label} = +{delta} (antes {before}, despues {after}).",
        "=" * 68,
        f"El paso corrio pero no escribio nada en '{table}'"
        + (f" (WHERE {where})" if where else "") + ".",
        "Revisar: filtros de seleccion, dependencias de pasos previos,",
        "o si el paso ya estaba completo de una corrida anterior.",
    ])
    return delta


def assert_invariant(conn: sqlite3.Connection, sql: str, expected: int,
                     what: str) -> int:
    """Candado de integridad: la query DEBE devolver `expected`.

    Para la clase de fallo de la leccion 48 ("trabajo a medias"):
        assert_invariant(conn,
            "SELECT COUNT(*) FROM question_segments WHERE question_short IS NULL",
            0, "espejos sin question_short (leccion 48)")
    """
    try:
        val = int(conn.execute(sql).fetchone()[0])
    except sqlite3.OperationalError as e:
        print(f"  (invariante '{what}' no evaluable aun: {e})", file=sys.stderr)
        return -1
    if val == expected:
        print(f"  OK: {what} = {val}")
        return val
    print(f"\nINVARIANTE ROTA: {what} = {val}, se esperaba {expected}.\n"
          f"  query: {sql}", file=sys.stderr, flush=True)
    sys.exit(EXIT_NO_WORK)


def report_done(script: str, **counters) -> None:
    """Linea final canonica, greppable por el orquestador y por las pruebas.

    Formato: RESULTADO script=transcribe_audios con_texto=12 sin_habla=3 cache=0
    """
    body = " ".join(f"{k}={v}" for k, v in counters.items())
    print(f"RESULTADO script={script} {body}", flush=True)

# --------------------------------------------------------------------------
# Lock exclusivo de Whisper -- leccion 43: NUNCA dos en paralelo
# --------------------------------------------------------------------------
# Comparten la GPU Metal y el mas largo se arrastra sin limite. Antes esto era
# doctrina en prosa ("nunca correr dos Whisper a la vez") -- la clase de regla
# que un asistente puede olvidar en una sesion larga con jobs en background.
# Aqui se vuelve estructuralmente imposible: el segundo proceso que intenta
# transcribir mientras el primero tiene el lock ABORTA al instante, no espera
# en silencio ni compite por la GPU.
#
# Cada toma y suelta del lock queda en logs/whisper-journal.jsonl -- es lo que
# bin/verify_asistente.py lee para el checkpoint B3: certificar, con evidencia,
# que en toda la sesion nunca hubo dos transcripciones solapadas.

EXIT_LOCK_OCUPADO = 5  # distinto de EXIT_NO_WORK: aqui SI habia trabajo, lo bloquea otro proceso


def _engine_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _lock_path() -> Path:
    return _engine_root() / "logs" / ".whisper.lock"


def _journal_path() -> Path:
    return _engine_root() / "logs" / "whisper-journal.jsonl"


def _append_journal(entrada: dict) -> None:
    p = _journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entrada, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def lock_exclusivo(script: str):
    """Envolver el bucle de transcripcion con esto. Aborta si otro proceso ya
    lo tiene -- no espera, no reintenta: esperar en silencio es exactamente el
    "el largo se arrastra" que la leccion 43 documenta.

    Uso canonico (transcribe_clips.py, transcribe_audios.py):

        with guards.lock_exclusivo("transcribe_audios"):
            for i, (cid, path, fn) in enumerate(rows, 1):
                ...transcribir...
    """
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("\n" + "=" * 68, file=sys.stderr)
            print("LOCK OCUPADO: otra transcripcion Whisper ya esta corriendo.",
                  file=sys.stderr)
            print("=" * 68, file=sys.stderr)
            print("Leccion 43: NUNCA dos Whisper en paralelo -- comparten la GPU",
                  file=sys.stderr)
            print("Metal y el mas largo se arrastra sin limite. Espera a que",
                  file=sys.stderr)
            print(f"termine, o revisa {lock_path} si crees que quedo huerfano",
                  file=sys.stderr)
            print("(proceso muerto sin liberar el lock).", file=sys.stderr)
            sys.exit(EXIT_LOCK_OCUPADO)
        inicio = time.time()
        pid = os.getpid()
        _append_journal({"script": script, "pid": pid, "evento": "inicio",
                         "ts": inicio})
        try:
            yield
        finally:
            fin = time.time()
            _append_journal({"script": script, "pid": pid, "evento": "fin",
                             "ts": fin, "duracion_seg": round(fin - inicio, 1)})
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()

