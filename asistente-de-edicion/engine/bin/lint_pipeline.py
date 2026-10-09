#!/usr/bin/env python3
"""Linter pre-flight para el pipeline del asistente de edicion.

Examina todos los scripts en bin/ (.py Y .sh), y los Lua de resolve/, buscando
errores conocidos que pueden dejar el pipeline parcialmente aplicado al material:

  1. Hardcodes geograficos (LIKE '%JILOTEPEC%', etc.) que filtran a un solo
     sector y rompen "aplicar a todo el material".
  2. DELETE/DROP globales en tablas multi-sector (audio_sync_pairs,
     clip_segments) sin filtro por scope — pueden destruir trabajo de otros
     sectores al re-correr el script por uno.
  3. Solo en .sh: disco resuelto por el propio script con un glob literal de
     /Volumes/ en vez de recibirlo por argumento.
  4. Solo en .sh: invocacion de un script que EXIGE --project-prefix sin
     pasarselo. Eso no es un aviso: el script sale 1 y con `set -e` mata al
     orquestador entero.
  5. Solo en resolve/*.lua: lo que el menu de Resolve 21.1 Free no tiene (io,
     os, require, package, debug, ffi) y los globals de Consola. Es el
     principio 2 del plan del plugin de Resolve Free (2026-10-05).

Si encuentra algo: imprime los hallazgos y sale con exit code 1.
El orquestador lo corre antes de empezar.

POR QUE LEE .sh (2026-08-05): hasta hoy `main()` hacia glob("*.py") y nada mas.
Los dos scripts de shell del pipeline eran invisibles justo para el linter que
corre primero — y dentro habia cinco hardcodes geograficos del tipo exacto que
este archivo existe para cazar, mas la llamada sin --project-prefix de
run_full_pipeline.sh:110 que mataba el orquestador antes de indexar un audio.
Las reglas 1 y 2 ya eran textuales y funcionan sobre shell sin cambios; las
reglas 3 y 4 son nuevas y solo aplican a .sh.

Uso:
    python3 bin/lint_pipeline.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent

# Sectores y carpetas-raiz conocidos en este proyecto. Cualquier referencia
# literal en un script del pipeline es sospechosa porque restringe el alcance.
GEOGRAPHIC_TOKENS = [
    "JILOTEPEC", "GUADALAJARA", "GUADALCAZAR", "GUANAJUATO", "MONTERREY",
    "CHONTACATLAN", "MINERAL DEL CHICO",
    # Agregados 2026-07-27: nombres de proyecto, no solo de sector. Faltaba
    # el propio "ESCALANDO MEXICO", asi que los 5 scripts con
    # --project-prefix='ESCALANDO MEXICO/' eran invisibles para el linter.
    "ESCALANDO MEXICO", "ZEZZIONS",
]

# Tablas que viven a nivel proyecto (multi-sector). Un DELETE/DROP global aqui
# destruye trabajo de los otros sectores al re-correr el script por uno.
MULTI_SECTOR_TABLES = {
    "audio_sync_pairs",
    "clip_segments",
    "clip_curated_segments",
    "question_segments",
    "clip_metadata_payload",
    "clip_characters",
    "clip_shot_values",
    "clip_angles",
    "content_segments",
    "face_attributions",
    "segment_objects",
    "segment_poses",
}

# Scripts excluidos: el linter mismo y scripts utilitarios (no del pipeline).
# lint_pipeline.py se excluye a si mismo (contiene los tokens por definicion).
# transcript_test.py es un script de diagnostico manual con un LIKE fijo.
#
# verify_coverage.py SALIO de esta lista el 2026-07-31. Estaba exento, y era
# justo el script cuyo default="ESCALANDO MEXICO/" daba proyectos enteros por
# buenos sin mirar un clip. Doble ceguera sobre el mismo archivo: el linter no lo
# leia por estar aqui, y la regla de defaults tampoco lo habria visto por el bug
# de la barra final. Al quitarle el default ya pasa limpio: no hay razon para la
# exencion. Si algun dia vuelve a fallar el lint, se arregla el script — no se
# vuelve a meter en esta lista.
EXCLUDE = {"lint_pipeline.py", "transcript_test.py"}


SCOPE_FLAGS = ("--sector", "--location", "--video-folder", "--audio-folder",
               "--include", "--exclude", "--seg-ids", "--clip-id")


def script_has_scope_flag(text: str) -> bool:
    """True si el script acepta algun flag que restringe su alcance.
    Si SI tiene flag de scope y aun asi hace DELETE/DROP global, es un bug
    real (al re-correr por sector se destruyen los demas).
    Si NO tiene flag de scope, el DELETE global es aceptable porque se
    asume que el script procesa TODO el material en una sola corrida."""
    return any(f in text for f in SCOPE_FLAGS)


def find_issues_in_file(path: Path, saltar: set[int] | None = None) -> list[str]:
    issues = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    has_scope = script_has_scope_flag(text)
    saltar = saltar or set()

    for ln, line in enumerate(lines, 1):
        if ln in saltar:
            continue
        # Permitir override con comentario explicito
        if "lint:ok delete-global" in line:
            continue
        # Hardcode geografico en SQL LIKE
        for token in GEOGRAPHIC_TOKENS:
            # Match 'JILOTEPEC' inside a string literal or LIKE clause,
            # NOT en comentarios ni docstrings.
            stripped = line.split("#")[0]
            if (token in stripped
                    and ("LIKE" in stripped.upper()
                         or "rel_path" in stripped.lower()
                         or "video-folder" in stripped.lower())):
                # Permitido si el token viene de una variable (no literal).
                # Heuristica: requerir comilla simple/doble cerca del token.
                # OJO (2026-07-27): antes esto usaba \S*, que NO cruza el
                # espacio de "ESCALANDO MEXICO" ni "MINERAL DEL CHICO", asi
                # que esos tokens nunca hacian match aunque estuvieran en la
                # lista. [^'\"]* si los cruza.
                if re.search(rf"['\"][^'\"]*{re.escape(token)}", stripped):
                    issues.append(
                        f"  L{ln}: hardcode geografico '{token}' en query/path: {stripped.strip()[:120]}"
                    )

        # Default geografico en argparse (caso FCC 2026-07-09: TRES scripts
        # con default="jilo" dejaban 0 items fuera de Jilotepec y el linter
        # no lo veia porque no hay LIKE en la misma linea).
        m_def = re.search(
            r"add_argument\(.*default\s*=\s*['\"]([^'\"]+)['\"]", line)
        if m_def:
            # OJO (2026-07-31): antes se comparaba el valor crudo, asi que
            # default="ESCALANDO MEXICO/" NO hacia match — la barra final rompia
            # tanto startswith() como el `in`. Los cinco --project-prefix con ese
            # default se escaparon de la regla escrita para cazarlos, y de ahi
            # salio lo de FANTASTICO COMICS. Mismo error que el \S* de la regla
            # de arriba: la comparacion mas estricta de lo que debia.
            # Se normaliza quitando separadores y espacios de los extremos.
            val = m_def.group(1).upper().strip("/\\ ")
            if any(tok.startswith(val) or val in tok
                   for tok in GEOGRAPHIC_TOKENS if len(val) >= 4):
                issues.append(
                    f"  L{ln}: default geografico '{m_def.group(1)}' en "
                    f"argparse — restringe el alcance en otros proyectos: "
                    f"{line.strip()[:120]}"
                )

        # Apertura del manifest sin pasar por manifest.conectar(). Se permite
        # `sqlite3.connect` con timeout explicito y la URI inmutable de solo
        # lectura; lo que se caza es volver al default de 5 s y, sobre todo,
        # tener el PRAGMA de WAL repartido por el motor otra vez.
        if ("sqlite3.connect(" in stripped
                and path.name not in ("manifest.py",)
                and "immutable=1" not in stripped
                and "timeout=" not in stripped):
            issues.append(
                f"  L{ln}: sqlite3.connect sin pasar por manifest.conectar() "
                f"(busy_timeout queda en el default de 5 s): {stripped.strip()[:100]}"
            )

        if 'PRAGMA journal_mode' in stripped and path.name != "manifest.py":
            issues.append(
                f"  L{ln}: PRAGMA journal_mode suelto — lo pone "
                f"manifest.conectar(), y WAL persiste en el archivo: "
                f"{stripped.strip()[:100]}"
            )

        # DELETE/DROP global sobre tabla multi-sector, sin clausula WHERE
        m = re.search(r"(DELETE\s+FROM|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+(\w+)",
                      line, re.IGNORECASE)
        if m:
            verb = m.group(1).upper().replace("  ", " ")
            tbl = m.group(2)
            if tbl in MULTI_SECTOR_TABLES:
                # Ver si la misma linea (o esta + la siguiente con string continuation)
                # incluye un WHERE. Aproximacion: examinar +1 linea de continuacion.
                ctx = line + " " + (lines[ln] if ln < len(lines) else "")
                if "WHERE" not in ctx.upper() and has_scope:
                    issues.append(
                        f"  L{ln}: {verb} global sobre tabla multi-sector '{tbl}' "
                        f"(sin WHERE) en script con flag de scope — destruye trabajo "
                        f"de otros sectores al re-correr: {line.strip()[:120]}"
                    )

    return issues


def scripts_que_exigen_prefix(bin_dir: Path) -> set[str]:
    """Los .py que llaman a guards.exigir_prefix() — es decir, los que salen 1
    si no reciben --project-prefix.

    Se detecta leyendo el codigo, NO con una lista fija: si manana un sexto
    script empieza a exigirlo, el linter lo sabe sin que nadie lo actualice.
    Una lista a mano habria envejecido igual que envejecio el orquestador."""
    exigen = set()
    for py in bin_dir.glob("*.py"):
        if py.name in EXCLUDE:
            continue
        txt = py.read_text(encoding="utf-8", errors="replace")
        # Solo la llamada real, no el import ni una mencion en un comentario.
        if re.search(r"exigir_prefix\s*\(", txt.replace("def exigir_prefix(", "")):
            exigen.add(py.name)
    return exigen


def _lineas_logicas(lines: list[str]) -> list[tuple[int, str]]:
    """Une las continuaciones con backslash en una sola linea logica.

    Devuelve (numero_de_linea_donde_empieza, texto_unido). Sin esto, una
    invocacion partida en cuatro lineas —que es como estan escritas casi todas
    las del orquestador— se leeria como cuatro fragmentos y el --project-prefix
    de la ultima no contaria para la primera."""
    out: list[tuple[int, str]] = []
    buf, inicio = "", 0
    for ln, raw in enumerate(lines, 1):
        if not buf:
            inicio = ln
        buf += raw.rstrip("\n")
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        out.append((inicio, buf))
        buf = ""
    if buf:
        out.append((inicio, buf))
    return out


# Un nombre de volumen literal: /Volumes/ seguido de algo que no sea una
# variable ni el cierre inmediato de la cadena. `/Volumes/$DISCO` y
# `/Volumes/"` no son hardcodes; `/Volumes/Escala-Mex*` si lo es.
RE_VOLUMEN_LITERAL = re.compile(r"/Volumes/(?![\"'$}\s/])")


def lineas_en_docstring(path: Path) -> set[int]:
    """Lineas que caen dentro de un docstring de .py.

    Misma razon que el filtro de heredocs: un docstring es prosa, no codigo. El
    de `run_pipeline.py` explica que el orquestador viejo tenia hardcodes de
    ESCALANDO MEXICO — describir el problema no es cometerlo, y marcarlo obliga
    a escribir la documentacion en clave para contentar al linter.

    Se usa `ast`, no una heuristica de comillas: los docstrings con comillas
    dentro y las cadenas multilinea que NO son docstrings requieren parseo real.
    Si el archivo no compila, se devuelve vacio y el linter mira todo — es mejor
    revisar de mas que callar por un fallo de parseo.
    """
    try:
        arbol = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return set()

    dentro: set[int] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        cuerpo = getattr(nodo, "body", None)
        if not cuerpo:
            continue
        primero = cuerpo[0]
        if (isinstance(primero, ast.Expr)
                and isinstance(primero.value, ast.Constant)
                and isinstance(primero.value.value, str)):
            fin = primero.end_lineno or primero.lineno
            dentro.update(range(primero.lineno, fin + 1))
    return dentro


RE_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def lineas_en_heredoc(lines: list[str]) -> set[int]:
    """Numeros de linea que caen DENTRO de un heredoc (`cat <<'EOF' ... EOF`).

    Un heredoc es texto que el script imprime, no codigo que ejecuta. El banner
    de run_full_pipeline.sh explica al usuario que el pipeline 'se rompe en
    sectores como paso con ESCALANDO MEXICO' — mencionar el proyecto ahi es
    prosa, no un hardcode. Sin este filtro el linter marca la advertencia que
    existe precisamente para evitar el problema que el linter busca."""
    dentro: set[int] = set()
    delim = None
    for ln, raw in enumerate(lines, 1):
        if delim is None:
            m = RE_HEREDOC.search(raw.split("#")[0])
            if m:
                delim = m.group(1)
            continue
        if raw.strip() == delim:
            delim = None
        else:
            dentro.add(ln)
    return dentro


def find_issues_in_shell(path: Path, exigen_prefix: set[str]) -> list[str]:
    """Reglas que solo aplican a scripts de shell."""
    issues = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    heredoc = lineas_en_heredoc(lines)

    for ln, logica in _lineas_logicas(lines):
        if ln in heredoc:
            continue
        code = logica.split("#")[0]
        if not code.strip():
            continue

        # (3) El script se resuelve el disco solo, con un glob literal.
        # rebake_after_llm.sh:13 hace `DISK=$(ls -d /Volumes/Escala-Mex*/...)`
        # ignorando el $DISK que el orquestador le paso por contexto: con dos
        # discos montados hornea el equivocado y no lo dice.
        if RE_VOLUMEN_LITERAL.search(code):
            issues.append(
                f"  L{ln}: disco hardcodeado en shell — el root debe llegar por "
                f"argumento, no resolverse aqui: {code.strip()[:120]}"
            )

        # (3b) Token geografico en shell fuera de un contexto SQL/ruta. La regla
        # 1 solo mira lineas con LIKE / rel_path / video-folder, asi que
        # `for SECTOR in GUADALAJARA GUADALCAZAR ...` se le escapaba entera —
        # y es el hardcode mas caro de todos: fija la lista de sectores del
        # proyecto en el codigo del motor. En un .sh del motor no hay ninguna
        # razon legitima para nombrar un sector.
        ya_cazado = ("LIKE" in code.upper() or "rel_path" in code.lower()
                     or "video-folder" in code.lower())
        if not ya_cazado:
            for token in GEOGRAPHIC_TOKENS:
                if token in code:
                    issues.append(
                        f"  L{ln}: token geografico '{token}' en shell — la lista "
                        f"de sectores es dato del proyecto, no codigo del motor: "
                        f"{code.strip()[:120]}"
                    )

        # (4) Invocacion de un script que exige --project-prefix, sin pasarselo.
        if "--project-prefix" not in code:
            for nombre in sorted(exigen_prefix):
                if nombre in code and re.search(r"\bpython3?\b|\$PY\b|\$VENV\b", code):
                    issues.append(
                        f"  L{ln}: se invoca {nombre} sin --project-prefix, que es "
                        f"obligatorio — sale 1 y con `set -e` mata la corrida: "
                        f"{code.strip()[:120]}"
                    )

    return issues


# --- Regla 5: el Lua del camino Free (resolve/*.lua) ------------------------
# 2026-10-05, plan del plugin de Resolve Free (§3, principio 2). En el menu de
# Resolve 21.1 Free —segun terceros en la build 17; la Fase 0 lo mide— no
# existen io, os.execute/remove/rename, require, package, ffi, debug ni arg, y
# un script que los toca truena: a veces al cargarse, a veces al final, con todo
# construido. El plan va mas lejos que el sandbox: en el camino Free Lua no usa
# io, os, require, debug ni ffi aunque alguno exista (os.time existe, y aun asi
# las fechas las resuelve Python). Tampoco lee globals de Consola: las banderas
# viajan en el pedido o en `ctx`, con tipo, no en un global que nadie valida y
# que un dofile anterior pudo dejar puesto.
#
# Se busca FUERA de comentarios y de cadenas: un comentario que explica por que
# no se usa io.open no es un uso.
LUA_PROHIBIDOS = [
    (re.compile(r"(?<![\w.:])io\s*[.:\[]"), "io no existe en el menu de Free"),
    (re.compile(r"(?<![\w.:])os\s*[.:\[]"),
     "os: el camino Free no lo usa (fechas y rutas las resuelve Python)"),
    (re.compile(r"(?<![\w.:])require\b"), "require no existe en el menu de Free"),
    (re.compile(r"(?<![\w.:])package\s*[.:\[]"), "package no existe en el menu de Free"),
    (re.compile(r"(?<![\w.:])debug\s*[.:\[]"), "debug no existe en el menu de Free"),
    (re.compile(r"(?<![\w.:])ffi\b"),
     "ffi no existe en el menu de Free, y salir del sandbox con el choca con la EULA 8(a)(iv)"),
]

# Los globals que hoy se teclean en la Consola antes del dofile. La lista sale
# del inventario del 2026-10-05 (nombres en mayusculas que los scripts leen sin
# declararlos local) mas los que restaurar_bins.lua y reel_subtitulado.lua
# dejaron ese dia al volverse modulos. Una bandera NUEVA de Consola se agrega
# aqui: mejor aun, no se crea y va en ctx.
LUA_GLOBALS_CONSOLA = (
    "MOVER_A_BINS", "CRONOLOGIA_BROLL", "CORTAR_SILENCIOS", "MARCAR_CORTES",
    "CORTES_EN", "MODO_MERGE", "MERGE_PLAN", "MERGE_FORZAR", "SIN_SKEW",
    "TIMELINES", "DIA", "REEL_VIDEO", "REEL_SRT", "REEL_NOMBRE", "REEL_PFX",
    "RESTAURAR_REGISTRO", "ASISTENTE_LIB", "LAVAS_DATA", "LAVAS_TIMELINE",
    "LAVAS_SOLO_PLAN", "LAVAS_COPIA", "LAVAS_BIN", "LAVAS_ORDEN_TX", "DESTINO",
    "SALIDA",
)
RE_GLOBAL_CONSOLA = re.compile(
    r"(?<![\w.:])(" + "|".join(LUA_GLOBALS_CONSOLA) + r")\b")

# Archivos que no se revisan, cada uno con su razon. No es deuda que se pague
# linea por linea: se van enteros cuando llegue lo que los sustituye.
#   - asistente_<proyecto>.lua (todos menos asistente_lib.lua): los scripts por
#     proyecto. Los sustituye UN aplicador, aplicar.lua, en la Fase 1; hasta
#     entonces son copias de la misma plantilla con sus globals de Consola.
LUA_CONGELADOS = {
    "spikes_v2.lua": "spike S1-S11: lo sustituye resolve/diagnostico.lua",
    "spike_merge.lua": "spike del merge: no corre en el camino del editor",
    "spike_merge2.lua": "spike del merge: no corre en el camino del editor",
    "mock_resolve_full.lua": "emula el host de Resolve: corre fuera de el",
    "mock_resolve_smoke.lua": "emula el host de Resolve: corre fuera de el",
    "prueba_cronologia.lua": "prueba de laboratorio de la cronologia",
    "diagnostico.lua": "mide io, os y debug a proposito, cada uno dentro de pcall",
}


def lua_congelado(nombre: str) -> str | None:
    """La razon por la que un .lua no se revisa, o None si se revisa."""
    if nombre in LUA_CONGELADOS:
        return LUA_CONGELADOS[nombre]
    if nombre.startswith("asistente_") and nombre != "asistente_lib.lua":
        return "script por proyecto: lo sustituye aplicar.lua (Fase 1)"
    return None


def lua_sin_comentarios(texto: str) -> list[str]:
    """Las lineas del archivo con comentarios y cadenas en blanco.

    Conserva el numero de lineas (un comentario largo de 5 lineas deja 5 lineas
    vacias) para que el L<n> del hallazgo apunte a donde esta el codigo. Las
    cadenas conservan sus comillas: `print("io.open")` queda `print(" ")`.
    """
    out: list[str] = []
    i, n = 0, len(texto)

    def largo(pos: int) -> int | None:
        """Si en pos abre un [[ o [==[, el numero de '='; si no, None."""
        m = re.match(r"\[(=*)\[", texto[pos:pos + 64])
        return len(m.group(1)) if m else None

    def blanco(trozo: str) -> str:
        return re.sub(r"[^\n]", " ", trozo)

    while i < n:
        c = texto[i]
        if texto.startswith("--", i):
            nivel = largo(i + 2)
            if nivel is not None:
                cierre = "]" + "=" * nivel + "]"
                fin = texto.find(cierre, i)
                fin = n if fin < 0 else fin + len(cierre)
            else:
                fin = texto.find("\n", i)
                fin = n if fin < 0 else fin
            out.append(blanco(texto[i:fin]))
            i = fin
        elif c == "[" and largo(i) is not None:
            nivel = largo(i)
            cierre = "]" + "=" * nivel + "]"
            fin = texto.find(cierre, i)
            fin = n if fin < 0 else fin + len(cierre)
            out.append('"' + blanco(texto[i + 1:fin - 1]) + '"')
            i = fin
        elif c in "\"'":
            j = i + 1
            while j < n and texto[j] != c and texto[j] != "\n":
                j += 2 if texto[j] == "\\" else 1
            if j < n and texto[j] == c:
                out.append(c + blanco(texto[i + 1:j]) + c)
                i = j + 1
            else:
                # Sin cerrar en su linea (codigo roto): el salto se conserva,
                # o todos los L<n> de abajo quedarian corridos.
                out.append(c + blanco(texto[i + 1:j]))
                i = j
        else:
            out.append(c)
            i += 1
    return "".join(out).split("\n")


def find_issues_in_lua(path: Path) -> list[str]:
    """Regla 5 sobre un .lua de resolve/: lo que el menu de Free no tiene."""
    original = path.read_text(encoding="utf-8", errors="replace").split("\n")
    issues = []
    for ln, code in enumerate(lua_sin_comentarios("\n".join(original)), start=1):
        visto = original[ln - 1].strip()[:120] if ln <= len(original) else ""
        for rx, por_que in LUA_PROHIBIDOS:
            if rx.search(code):
                issues.append(f"  L{ln}: {por_que}: {visto}")
        for m in RE_GLOBAL_CONSOLA.finditer(code):
            issues.append(f"  L{ln}: global de Consola {m.group(1)}: va en ctx o en "
                          f"el pedido: {visto}")
    return issues


# Deuda congelada 2026-07-27. Al ampliar GEOGRAPHIC_TOKENS con "ESCALANDO
# MEXICO" y arreglar la regex (\S* no cruzaba el espacio del token), el linter
# destapo 13 hardcodes que llevaba anos sin ver. No tumban el pipeline —
# tumbarlo de golpe dejaria a Victor sin poder correr nada — pero quedan a la
# vista y cualquier hardcode NUEVO si falla.
#
# Los 3 generate_* son legacy (FCPXML/DRP, sustituidos por el bake Lua).
# Los 2 activos son deuda real: parsean el sector con un regex atado al
# nombre del proyecto, asi que en un proyecto plano devuelven None.
#
# Borrar cada linea al arreglar el script. La meta es dejar el set vacio.
#
# CAMBIO 2026-07-30 (v0.2.0): la deuda se indexa por CONTENIDO, no por numero
# de linea. Con numeros de linea, cualquier edicion arriba de un hardcode
# congelado corria la linea y el lint tumbaba el pipeline por un problema que
# ya estaba declarado — paso al migrar los scripts al registro de camaras.
# La huella es el mensaje del linter sin el prefijo "L<n>:", que ya incluye el
# texto ofensor, asi que sigue siendo preciso.
#
# LOS 17 DEL SHELL NO SE CONGELAN (decision del 2026-08-05). Al empezar a leer
# .sh el linter destapo 17 hallazgos en run_full_pipeline.sh y rebake_after_llm.sh.
# La tentacion era congelarlos, como se hizo en julio con los 12 de los .py, para
# no dejar el lint en rojo. Aqui no aplica el mismo criterio, por dos razones:
#
#   1. Aquellos 12 eran deuda: scripts que funcionaban con una limitacion
#      conocida. Estos 17 son el bug — tres de ellos son las llamadas sin
#      --project-prefix que matan el orquestador en la linea 110, antes de
#      indexar un solo audio. Congelar el aviso de un pipeline muerto es
#      esconder el cadaver.
#   2. No hay nada que "tumbar". El orquestador ya no arranca. Con el lint en
#      rojo muere en el paso 0 diciendo exactamente que esta mal, en vez de
#      morir 17 lineas despues con un mensaje sobre un flag.
#
# La suite de pruebas NO invoca al linter (verificado), asi que las 197 siguen
# verdes. La Fase 1 del plan borra los 17 al reescribir el orquestador; cuando
# eso pase, este bloque de comentario se va con ellos.
#
DEUDA_CONGELADA = {
    # PAGADA 2026-07-31: build_metadata_payload.py / detect_sector ya no tiene
    # default. Junto con ella se quitaron los cinco --project-prefix con
    # default="ESCALANDO MEXICO/" (verify_coverage, export_lua_data,
    # build_metadata_payload, analyze_segments, derive_video_categories), que el
    # linter NO veia por el bug de la barra final — ver la regla de arriba.
    # PAGADA 2026-08-05 (Fase 1.4): las dos entradas activas
    # —describe_segments_local_llm.py:231 y enrich_curated_segments.py:92—
    # parseaban el sector con `re.match(r"ESCALANDO MEXICO/([^/]+)")`, asi que
    # en cualquier otro proyecto devolvian None y el tramo quedaba sin sector.
    # Ahora usan lib.proyecto.sector_de() con el prefijo del proyecto.
    #
    # BORRADAS 2026-08-05: las seis de generate_drp / generate_drp_v7 /
    # generate_report. Los cuatro generate_* (mas lib/drp_template.py y
    # lib/fcpxml.py, que solo ellos usaban) se retiraron: eran legacy sustituido
    # por el bake Lua, no los invocaba nadie, y sumaban 1899 lineas de codigo
    # muerto en un producto que se va a publicar. Estan en git.
    #
    # EL SET QUEDA VACIO. Cualquier hardcode que aparezca a partir de aqui es
    # NUEVO y tumba el lint.
    #
    # Ojo al `set()` de abajo y no `{}`: al borrar la ultima entrada, un literal
    # `{}` deja de ser un set y pasa a ser un DICT. Aqui daba igual de momento
    # —`in` funciona en los dos— pero la primera entrada que alguien anadiera
    # tendria que ser `clave: valor` en vez de una tupla, y el fallo seria
    # silencioso. Lo cazo tests/test_lint_shell.py.
}
DEUDA_CONGELADA: set[tuple[str, str]] = set()

# Deuda congelada de la regla 5 (2026-10-05). Eran 29 usos (28 desde la
# revision del PR #1, que pago uno de isoEpoch) que el camino
# Free no aguanta en los .lua que SI se quedan, y cada grupo dice que lo
# sustituye segun la §2 del plan del plugin de Resolve Free. Aqui si se congela,
# a diferencia de los 17 del shell: hoy estos scripts funcionan en la Consola y
# en Studio, y tumbar el lint dejaria a Victor sin pipeline por una limitacion
# conocida de Free, no por un bug. Va aparte de DEUDA_CONGELADA para que aquella
# siga vacia. Borrar cada linea al pagarla.
DEUDA_LUA: set[tuple[str, str]] = {
    # asistente_lib.lua: volcarLayouts y guardarOrigenBins escriben con io, y
    # isoEpoch lee con os.time la hora SIN zona (la con zona ya es aritmetica
    # pura desde la revision del PR #1: dos usos pagados). Plan §2: el canal de
    # regreso sustituye a los dos primeros y la hora se hornea desde Python
    # (Fase 1).
    ("asistente_lib.lua", 'io no existe en el menu de Free: local f = io.open(ruta, "w")'),
    ("asistente_lib.lua", "os: el camino Free no lo usa (fechas y rutas las resuelve Python): return os.time({year = y, month = mo, day = d, hour = h, min = mi, sec = sec}) + frac"),
    ("asistente_lib.lua", 'io no existe en el menu de Free: local f, err = io.open(ruta, "w")'),
    # auditar_settings.lua: escribe su reporte con io, en un destino que lee de
    # DESTINO o de os.getenv. Plan §2: el canal de regreso.
    ("auditar_settings.lua", "global de Consola DESTINO: va en ctx o en el pedido: local destino = DESTINO"),
    ("auditar_settings.lua", 'os: el camino Free no lo usa (fechas y rutas las resuelve Python): destino = os.getenv("HOME") .. "/cinema-assistant/logs"'),
    ("auditar_settings.lua", 'io no existe en el menu de Free: local f = io.open(ruta, "w")'),
    # lavas_al_corte.lua: nacio el 2026-10-01 con globals LAVAS_* y escribe su
    # informe con io. Pasa a ser una accion del pedido en la Fase 1.
    ("lavas_al_corte.lua", "global de Consola LAVAS_DATA: va en ctx o en el pedido: local ARG = {data = LAVAS_DATA, timeline = LAVAS_TIMELINE, solo_plan = LAVAS_SOLO_PLAN,"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_TIMELINE: va en ctx o en el pedido: local ARG = {data = LAVAS_DATA, timeline = LAVAS_TIMELINE, solo_plan = LAVAS_SOLO_PLAN,"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_SOLO_PLAN: va en ctx o en el pedido: local ARG = {data = LAVAS_DATA, timeline = LAVAS_TIMELINE, solo_plan = LAVAS_SOLO_PLAN,"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_COPIA: va en ctx o en el pedido: copia = LAVAS_COPIA, bin = LAVAS_BIN, orden_tx = LAVAS_ORDEN_TX}"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_BIN: va en ctx o en el pedido: copia = LAVAS_COPIA, bin = LAVAS_BIN, orden_tx = LAVAS_ORDEN_TX}"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_ORDEN_TX: va en ctx o en el pedido: copia = LAVAS_COPIA, bin = LAVAS_BIN, orden_tx = LAVAS_ORDEN_TX}"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_DATA: va en ctx o en el pedido: LAVAS_DATA, LAVAS_TIMELINE, LAVAS_SOLO_PLAN = nil, nil, nil"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_TIMELINE: va en ctx o en el pedido: LAVAS_DATA, LAVAS_TIMELINE, LAVAS_SOLO_PLAN = nil, nil, nil"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_SOLO_PLAN: va en ctx o en el pedido: LAVAS_DATA, LAVAS_TIMELINE, LAVAS_SOLO_PLAN = nil, nil, nil"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_COPIA: va en ctx o en el pedido: LAVAS_COPIA, LAVAS_BIN, LAVAS_ORDEN_TX = nil, nil, nil"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_BIN: va en ctx o en el pedido: LAVAS_COPIA, LAVAS_BIN, LAVAS_ORDEN_TX = nil, nil, nil"),
    ("lavas_al_corte.lua", "global de Consola LAVAS_ORDEN_TX: va en ctx o en el pedido: LAVAS_COPIA, LAVAS_BIN, LAVAS_ORDEN_TX = nil, nil, nil"),
    ("lavas_al_corte.lua", 'io no existe en el menu de Free: local f = io.open(rutaInf, "w")'),
    # leer_sync_merge.lua: escribe su salida con io donde dice SALIDA. Plan §2:
    # el canal de regreso.
    ("leer_sync_merge.lua", 'global de Consola SALIDA: va en ctx o en el pedido: local SALIDA_PATH = SALIDA or ""'),
    ("leer_sync_merge.lua", 'io no existe en el menu de Free: local f, err = io.open(SALIDA_PATH, "w")'),
    # merge_pool.lua: lee MERGE_PLAN y MERGE_FORZAR y escribe su informe con io
    # DESPUES de los merges, que no se deshacen. Plan §2: fuera de Free v1, o
    # reordenado para que el informe no dependa de io.
    ("merge_pool.lua", 'global de Consola MERGE_PLAN: va en ctx o en el pedido: if not MERGE_PLAN or MERGE_PLAN == "" then'),
    ("merge_pool.lua", "global de Consola MERGE_PLAN: va en ctx o en el pedido: local okPlan, PLAN = pcall(dofile, MERGE_PLAN)"),
    ("merge_pool.lua", 'global de Consola MERGE_PLAN: va en ctx o en el pedido: print("Plan: " .. MERGE_PLAN)'),
    ("merge_pool.lua", "global de Consola MERGE_FORZAR: va en ctx o en el pedido: if not esCopia and not MERGE_FORZAR then"),
    ("merge_pool.lua", 'global de Consola MERGE_PLAN: va en ctx o en el pedido: local rutaInf = string.gsub(MERGE_PLAN, "%.lua$", "_resultado.json")'),
    ("merge_pool.lua", 'io no existe en el menu de Free: local f = io.open(rutaInf, "w")'),
}


def _huella(issue: str) -> str:
    """El mensaje sin el prefijo de linea — estable ante ediciones del archivo."""
    return re.sub(r"^\s*L\d+:\s*", "", issue).strip()


def main() -> int:
    # Sin esto, `lint_pipeline.py --help` no imprimia ayuda: corria el lint
    # entero e interpretaba el flag como si nada. Lo destapo el endurecimiento
    # de tests/test_cli_arranca.py del 2026-08-05, que pasa a juzgar el
    # returncode — antes solo miraba stderr y este script no escribe ahi.
    argparse.ArgumentParser(
        description="Linter pre-flight del pipeline: hardcodes geograficos, "
                    "DELETE globales, discos hardcodeados en shell y llamadas "
                    "sin --project-prefix; y en el Lua, lo que el menu de "
                    "Resolve Free no tiene. Revisa bin/*.py, bin/*.sh y "
                    "resolve/*.lua.",
        epilog="Exit 0 si no hay hallazgos nuevos; 1 si los hay.",
    ).parse_args()

    bin_dir = HERE
    fails = 0
    congelados = 0
    exigen_prefix = scripts_que_exigen_prefix(bin_dir)

    archivos = sorted(bin_dir.glob("*.py")) + sorted(bin_dir.glob("*.sh"))
    for f in archivos:
        if f.name in EXCLUDE:
            continue
        if f.suffix == ".sh":
            lineas = f.read_text(encoding="utf-8", errors="replace").splitlines()
            heredoc = lineas_en_heredoc(lineas)
            hallazgos = (find_issues_in_file(f, saltar=heredoc)
                         + find_issues_in_shell(f, exigen_prefix))
            # Las dos familias de reglas recorren el archivo por separado, asi
            # que sin esto la salida salta de la L194 a la L85 y es ilegible.
            hallazgos.sort(key=lambda s: int(re.match(r"\s*L(\d+):", s).group(1)))
        else:
            hallazgos = find_issues_in_file(f, saltar=lineas_en_docstring(f))
        nuevos = []
        for it in hallazgos:
            if (f.name, _huella(it)) in DEUDA_CONGELADA:
                congelados += 1
            else:
                nuevos.append(it)
        if nuevos:
            fails += len(nuevos)
            print(f"\n{f.name}: {len(nuevos)} problema(s)")
            for it in nuevos:
                print(it)

    # Regla 5: el Lua del camino Free. Una instalacion del plugin trae solo los
    # scripts de resolve/ que viajan, asi que se revisa lo que haya.
    congelados_lua = 0
    resolve_dir = HERE.parent / "resolve"
    for f in sorted(resolve_dir.glob("*.lua")) if resolve_dir.is_dir() else []:
        if lua_congelado(f.name):
            continue
        nuevos = []
        for it in find_issues_in_lua(f):
            if (f.name, _huella(it)) in DEUDA_LUA:
                congelados_lua += 1
            else:
                nuevos.append(it)
        if nuevos:
            fails += len(nuevos)
            print(f"\n{f.name}: {len(nuevos)} problema(s)")
            for it in nuevos:
                print(it)

    if fails:
        print(f"\nLINT FAIL: {fails} problema(s) NUEVO(s) detectado(s).")
        print("Revisar antes de correr el pipeline. Si un hallazgo es falso")
        print("positivo, agregar comentario justificando o ampliar EXCLUDE")
        print("(en Lua: LUA_CONGELADOS, con su razon).")
        return 1
    print("Lint OK: ningun hardcode geografico, DELETE global ni Lua fuera del "
          "camino Free NUEVO.")
    if congelados:
        print(f"  ({congelados} hardcode(s) en deuda congelada — ver "
              f"DEUDA_CONGELADA en este archivo)")
    if congelados_lua:
        print(f"  ({congelados_lua} uso(s) de Lua que Free no aguanta, en deuda "
              f"congelada — ver DEUDA_LUA en este archivo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
