#!/usr/bin/env python3
"""Export manifest analysis + sync + segments to a Lua table the in-Resolve script reads.

Usage:
    python3 bin/export_lua_data.py --root /Volumes/MI_DISCO --out <path.lua>

Output structure:
  return {
    clips  = { ["/abs/path.mov"] = {
                  name=, folder=, status=, notes=, dur=, created=,
                  camera=, interview=, segments={{s,e},...},
                  category=, description=, content_segments={{s,e},...}} },
    byname = { ["NAME.mov"] = "/abs/path.mov" },  -- fallback index; last writer wins on duplicate names
    sync   = { ["/abs/video.mov"] = {audio=, audiopath=, offset=, conf=} },
  }

Clips are keyed by FULL PATH (not filename) because GoPro/DSLR reuse filenames
across cards and locations — a name-keyed table silently dropped colliding clips.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras  # noqa: E402
from lib.guards import assert_selected, exigir_prefix, exigir_tablas  # noqa: E402
from lib import manifest  # noqa: E402
from lib import proyecto  # noqa: E402


def ruta_del_indice(out: Path) -> Path:
    """<slug>_indice.json junto a <slug>_data.lua (el nombre que busca
    lib/pedido.py); otro nombre de salida da <stem>_indice.json."""
    nombre = out.name[:-len("_data.lua")] if out.name.endswith("_data.lua") else out.stem
    return out.with_name(f"{nombre}_indice.json")


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    matches = glob.glob(root_arg + "*")
    if len(matches) == 1:
        return Path(matches[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def lua_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return f'"{s}"'


def par_validado_por_onda(method: str, notas: str) -> bool:
    """Un par medido por onda (refinado o confirmado) y no descartado."""
    m, n = method or "", notas or ""
    if "descartado" in n or "debil" in n:
        return False
    return ("longwin ok" in n or m.endswith("-longwin")
            or "iter2 refined" in n or "iter2 ok" in n)


def orden_de_pares(fila) -> tuple:
    """Clave para ordenar (vpath, wav, offset, conf, apath, method, notes):
    primero los medidos por onda, luego por confianza descendente."""
    return (not par_validado_por_onda(fila[5], fila[6]), -(fila[3] or 0))


def alinear_wavs_por_contenido(pos_por_video: dict, max_desv: float = 0.100, nombre=str):
    """Correccion de arranque por WAV para que los que grabaron a la vez casen.

    `pos_por_video` = {video: {wav: arranque_que_ese_wav_implica_para_el_video}}.
    Dos WAV que comparten videos se contradicen en una constante: la diferencia
    de sus relojes. Por componente conexa, el WAV con mas videos queda fijo y los
    demas se mueven hacia el por BFS, con la MEDIANA de la contradiccion. Una
    arista cuya contradiccion no es constante (desviacion > `max_desv`) no se
    usa. Devuelve ({wav: correccion_s}, [avisos]).
    """
    import statistics
    aristas: dict[tuple, list[float]] = {}
    cuenta: dict[int, int] = {}
    for pos in pos_por_video.values():
        for a in pos:
            cuenta[a] = cuenta.get(a, 0) + 1
            for b in pos:
                if a != b:
                    aristas.setdefault((a, b), []).append(pos[a] - pos[b])
    vecinos: dict[int, list[int]] = {}
    avisos = []
    for (a, b), ds in aristas.items():
        desv = statistics.pstdev(ds) if len(ds) > 1 else 0.0
        if desv > max_desv:
            if a < b:
                avisos.append(f"{nombre(a)} <-> {nombre(b)}: {len(ds)} video(s) sin constante "
                              f"(desv {desv * 1000:.0f} ms), no se alinean por ahi")
            continue
        vecinos.setdefault(a, []).append(b)
    ajuste: dict[int, float] = {}
    for ref in sorted(cuenta, key=lambda w: (-cuenta[w], w)):
        if ref in ajuste:
            continue
        ajuste[ref] = 0.0
        cola = [ref]
        while cola:
            a = cola.pop(0)
            for b in vecinos.get(a, []):
                if b in ajuste:
                    continue
                ajuste[b] = ajuste[a] + statistics.median(aristas[(a, b)])
                avisos.append(f"{nombre(b)} se mueve {ajuste[b] * 1000:+.0f} ms para casar con {nombre(a)} "
                              f"({len(aristas[(a, b)])} video(s))")
                cola.append(b)
    return {w: d for w, d in ajuste.items() if d != 0.0}, avisos


def dia_de_rodaje(instante) -> str:
    """Fecha LOCAL (la de la Mac que hornea) de un instante: el dia de rodaje.

    Acepta epoch o ISO 8601; lo que no se entiende da "" (sin dia). Con dos dias
    en un proyecto, las timelines por hora real se arman de un dia a la vez
    (`DIA = "2026-09-29"` en el aplicador) y este es el dato con que se filtra:
    juntas, el hueco de la noche quedaba en medio (Asistente, 2026-09-29).
    """
    if instante in (None, "") or (isinstance(instante, (int, float)) and instante <= 0):
        return ""
    try:
        if isinstance(instante, (int, float)):
            dt = datetime.fromtimestamp(float(instante))
        else:
            dt = datetime.fromisoformat(str(instante).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return ""


# Interview heuristic — thresholds chosen 2026-05-22 from the JILOTEPEC motion data.
INTERVIEW_MIN_DUR = 60.0     # interviews are long takes
INTERVIEW_MAX_MOTION = 8.0   # interviews are shot static (tripod / low motion)
SYNC_MIN_CONF = 0.30         # transcript-based sync — relaxed (2026-05-23) para
                              # incluir pares cortos verificados por contenido.


# `classify_camera` vivia aqui y era la copia "de referencia" que las otras
# cuatro decian seguir sin hacerlo. Ahora sale del registro declarativo:
# `config/camera_profiles.json` + `<disco>/.cinema_assistant/camera_profiles.json`.
# Para agregar una camara se edita el JSON, no ocho scripts.


def is_interview(rel_path, camera, dur, motion, category=None) -> bool:
    """True si: (a) bin path nombrado 'entrevista'/'visita nata', (b) la heuristica
    de toma fija lo declara entrevista (motion < 8 + main + >= 60s) — solo
    fires donde hay motion analysis, (c) clip_descriptions.category lo marca
    como 'entrevista' o 'entrevista-audio' (señal definitiva añadida 2026-05-25
    para soportar proyectos sin ‘entrevista’ en la ruta — Zezzions).

    EXCLUYE explicitamente las categorias terminadas en '-degradada' (transcripts
    con alucinaciones de Whisper — caso fundador Zezzions 2026-05-25,
    audio de cámara con música)."""
    if category:
        if category.endswith("-degradada"):
            return False  # transcript contaminado, no fiable como entrevista
        if category in ("entrevista", "entrevista-audio"):
            return True
    low = (rel_path or "").lower()
    if "entrevista" in low or "visita nata" in low:
        return True
    return bool(camera == "main" and dur and dur >= INTERVIEW_MIN_DUR
                and motion is not None and motion < INTERVIEW_MAX_MOTION)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. rel_path prefix del proyecto. Pasar '' "
                         "para exportar TODOS los videos del manifest (proyecto "
                         "plano). No hay default a proposito: con uno heredado "
                         "este script hornea una timeline vacia sin avisar.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("export_lua_data"))

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")
    conn = manifest.conectar(str(db))
    cfg = proyecto.leer_config(root)

    # Prerrequisitos del query principal. El resto de las tablas que consulta
    # este script son enriquecimiento y ya se manejan como opcionales; estas dos
    # estan en el LEFT JOIN de la consulta base, y en SQLite un LEFT JOIN a una
    # tabla inexistente falla igual que un FROM.
    exigir_tablas(conn, {
        "clip_analysis": "python3 bin/analyze_clips.py --root <disco>",
        "clip_descriptions": "python3 bin/derive_video_categories.py --root <disco> --project-prefix ''",
    }, "export_lua_data")

    prefix_like = (args.project_prefix + "%") if args.project_prefix else "%"

    # Per-clip: analysis + creation_time — scope por --project-prefix
    clip_rows = conn.execute("""
        SELECT c.id, c.filename, c.path, c.parent_folder, c.rel_path, c.duration_sec,
               c.creation_time, c.camera_model,
               a.analysis_status, a.analysis_notes, a.motion_score_mean,
               c.camera_make, c.ext
        FROM clips c
        LEFT JOIN clip_analysis a ON a.clip_id = c.id
        WHERE c.file_kind = 'video' AND c.index_status = 'ok'
          AND c.rel_path LIKE ?
        ORDER BY c.parent_folder, c.filename
    """, (prefix_like,)).fetchall()

    # Guard: hornear cero clips produce un .lua sintacticamente valido y
    # completamente vacio. En Resolve eso se ve como "el script corrio y no paso
    # nada", que es el peor diagnostico posible.
    assert_selected(clip_rows, "clips a hornear", filters={
        "--root": str(root), "--project-prefix": args.project_prefix,
    }, hint="Proyecto plano: --project-prefix ''")

    reg = cameras.load_profiles(root)

    # External audios — para tener descripciones + segments en su propia
    # timeline "ESC — AUDIOS EXTERNOS". Incluye AUDIOS/* y ESCALANDO MEXICO/*
    # (los audios viven dispersos en ambas raíces).
    audio_rows = conn.execute("""
        SELECT c.id, c.filename, c.path, c.parent_folder, c.rel_path,
               c.duration_sec, c.creation_time, c.mtime
        FROM clips c
        WHERE c.file_kind = 'audio' AND c.index_status = 'ok'
          AND c.filename NOT GLOB '*_[Tt][Rr][0-9]*'
        ORDER BY c.parent_folder, c.filename
    """).fetchall()

    # POSICIÓN REAL DE CADA AUDIO EN EL DÍA — para la timeline de cronología.
    #
    # El Lua no puede deducirla solo: los WAV del Rode no traen BEXT ni iXML, así
    # que `creation_time` quedó NULL en el manifest y `DATA.audios[].created`
    # sale vacío. Lo único que hay es el `mtime` (instante en que el TX cerró el
    # archivo), y de ahí se saca el arranque restando la duración.
    #
    # Precisión: el filesystem guarda el mtime a segundo entero, así que esto
    # vale ±1 s. Sirve para COLOCAR (una timeline de tres horas) y NO para
    # SINCRONIZAR (que necesita ±1 frame). El sync sale de audio_sync_pairs, que
    # está medido por correlación.
    sys.path.insert(0, str(HERE))
    from derive_chrono_sync import build_chains       # noqa: E402
    _wavs = {r[0]: {"rel": r[4], "start": (r[7] or 0) - (r[5] or 0),
                    "fn": r[1], "dur": r[5] or 0}
             for r in audio_rows if r[7] and r[5]}
    # La tolerancia de cadena es la DECLARADA, la misma que usa verify_lav_offsets.
    # Con el 5.0 fijo, drc/00035 y drc/00036 (1.7 s de hueco: alguien paro y
    # reanudo el TX) se unian en una cadena y el segundo quedaba 1.7 s antes.
    _tol_cadena = float(cfg.get("cadena_tolerancia_s") or 5.0)
    _chain_of = build_chains(_wavs, _tol_cadena) if _wavs else {}
    # posición dentro de la cadena = suma de las duraciones de los anteriores
    _chain_pos: dict[int, float] = {}
    _por_cadena: dict[str, list[int]] = {}
    for aid, ch in _chain_of.items():
        _por_cadena.setdefault(ch, []).append(aid)
    _chain_start: dict[str, float] = {}
    for ch, ids in _por_cadena.items():
        ids.sort(key=lambda a: _wavs[a]["start"])
        _chain_start[ch] = _wavs[ids[0]]["start"]
        acc = 0.0
        for aid in ids:
            _chain_pos[aid] = acc
            acc += _wavs[aid]["dur"]

    # HORA DE CADA WAV: de la CADENA, no del archivo (2026-08-13).
    #
    # Los WAV del Rode no traen BEXT ni iXML, asi que la unica hora es el mtime,
    # guardado a SEGUNDO ENTERO. Tomar `mtime - dur` archivo por archivo le mete
    # a cada uno un error independiente de hasta 1 s, y los archivos de una
    # cadena son contiguos MUESTRA A MUESTRA — el Rode corta por hora sin perder
    # nada. Medido en Morsa: entre archivos consecutivos aparecian huecos de 0 a
    # 2 s, y el skew aparente de una misma camara variaba 1.7 s segun contra que
    # archivo se hubiera medido (Ayan -14.69 vs -16.41; Vic -50.81 vs -52.68).
    #
    # En una timeline por tiempo real eso se ve: la pista del lavalier da un
    # salto en cada frontera de archivo y los clips de alrededor quedan
    # desalineados. Es el "ligero desfase" que reporto el editor.
    #
    # Ahora la cadena se ancla UNA vez —en el mtime de su primer archivo— y el
    # resto se coloca por suma de duraciones. El error de +-1 s queda en el
    # ancla, comun a toda la cadena, en vez de repartido y distinto en cada
    # archivo. Y la cadena vuelve a ser continua, que es lo que hace falta para
    # meter el lavalier integro.
    _epoch_cadena: dict[int, float] = {}
    for aid, ch in _chain_of.items():
        if ch in _chain_start:
            _epoch_cadena[aid] = _chain_start[ch] + _chain_pos.get(aid, 0.0)

    # ALINEAR LAS CADENAS ENTRE SI POR CONTENIDO (2026-08-13).
    #
    # Cada cadena se ancla en el mtime de su primer archivo, y ese mtime esta
    # redondeado a SEGUNDO ENTERO. Con dos TX, cada cadena arrastra su propio
    # error de +-1 s, asi que las dos quedan desalineadas ENTRE SI aunque cada
    # una sea internamente contigua.
    #
    # Eso se ve en la timeline como camaras desfasadas entre ellas, no como
    # audio mal puesto: una camara que sincronizo contra Izq y otra contra
    # Derecha caen separadas por el error entre anclas. Medido en Morsa:
    # +163.3 ms con 8.5 ms de desviacion sobre 267 videos y dos camaras — una
    # constante fisica, no ruido. El mtime decia 2.000 s exactos entre anclas;
    # la diferencia real es 1.837 s. Casi 4 frames a 23.976.
    #
    # Es la "delta fisica D" que la doctrina de Morsa daba por PENDIENTE DE
    # MEDIR: no hace falta medirla contra el audio, ya esta en los pares. Los
    # videos que sincronizaron contra AMBAS cadenas dicen exactamente cuanto se
    # contradicen, y la mediana de esa contradiccion ES la correccion.
    #
    # Se toma como referencia la cadena con mas pares medidos y las demas se
    # mueven hacia ella. Solo se aplica con evidencia suficiente: si hay pocos
    # videos en comun o las medidas no concuerdan, se deja como esta y se dice.
    _MIN_COMUNES = 5
    _MAX_DESV = 0.100          # 100 ms: por encima, no es una constante
    if len(_por_cadena) > 1:
        import statistics as _stats

        _pos_por_video: dict[int, dict[str, float]] = {}
        try:
            for _vid, _aid, _off in conn.execute(
                    "SELECT video_clip_id, audio_clip_id, offset_sec "
                    "FROM audio_sync_pairs"):
                _ch = _chain_of.get(_aid)
                if _ch is None or _aid not in _epoch_cadena:
                    continue
                _pos_por_video.setdefault(_vid, {})[_ch] = (
                    _epoch_cadena[_aid] - (_off or 0.0))
        except sqlite3.OperationalError:
            _pos_por_video = {}

        _ref = max(_por_cadena, key=lambda ch: sum(
            1 for d in _pos_por_video.values() if ch in d))
        _ajuste: dict[str, float] = {_ref: 0.0}
        for _ch in _por_cadena:
            if _ch == _ref:
                continue
            _ds = [d[_ref] - d[_ch] for d in _pos_por_video.values()
                   if _ref in d and _ch in d]
            if len(_ds) < _MIN_COMUNES:
                print(f"  · cadenas: '{_ch}' comparte solo {len(_ds)} video(s) "
                      f"con '{_ref}' — sin evidencia para alinearlas, se dejan")
                continue
            _desv = _stats.pstdev(_ds) if len(_ds) > 1 else 0.0
            if _desv > _MAX_DESV:
                print(f"  · cadenas: '{_ch}' vs '{_ref}' no dan una constante "
                      f"(desv {_desv * 1000:.0f} ms sobre {len(_ds)} videos) — "
                      f"no se alinean")
                continue
            _d = _stats.median(_ds)
            _ajuste[_ch] = _d
            print(f"  · cadenas: '{_ch}' se mueve {_d * 1000:+.0f} ms para casar "
                  f"con '{_ref}' (desv {_desv * 1000:.0f} ms, {len(_ds)} videos)")
        for _aid, _ch in _chain_of.items():
            if _aid in _epoch_cadena and _ch in _ajuste:
                _epoch_cadena[_aid] += _ajuste[_ch]

    # Y CADA WAV CON LOS QUE GRABARON A LA VEZ, por contenido (2026-10-01).
    # La alineacion de arriba compara cada cadena con UNA de referencia y pide 5
    # videos en comun: con cadenas de un archivo cada una casi nunca se cumple, y
    # los dos TX quedaban corridos por la diferencia de sus relojes (3.0 s el
    # 29-sep en Asistente, 2.2 s el 28). Aqui manda el grafo: dos WAV que
    # comparten un video con pares validados por onda quedan donde el contenido
    # dice. Es lo que pide "la timeline de los audios": los lavalieres como
    # timecode, con todo lo demas encima.
    if _epoch_cadena:
        _pos_wav: dict[int, dict[int, float]] = {}
        try:
            for _vid, _aid, _off, _met, _notas in conn.execute(
                    "SELECT video_clip_id, audio_clip_id, offset_sec, method, "
                    "IFNULL(notes,'') FROM audio_sync_pairs"):
                if _aid in _epoch_cadena and par_validado_por_onda(_met, _notas):
                    _pos_wav.setdefault(_vid, {})[_aid] = (
                        _epoch_cadena[_aid] - (_off or 0.0))
        except sqlite3.OperationalError:
            _pos_wav = {}
        _aj_wav, _avisos_wav = alinear_wavs_por_contenido(
            _pos_wav, nombre=lambda w: _wavs.get(w, {}).get("rel", str(w)))
        for _aid, _d in _aj_wav.items():
            _epoch_cadena[_aid] += _d
        for _l in _avisos_wav:
            print("  · wav: " + _l)

    # Segments keyed by clip_id (visual analysis: motion/quality)
    segs_by_clip: dict[int, list] = {}
    try:
        for clip_id, s, e in conn.execute(
            "SELECT clip_id, start_sec, end_sec FROM clip_segments ORDER BY clip_id, seg_index"
        ):
            segs_by_clip.setdefault(clip_id, []).append((s, e))
    except sqlite3.OperationalError:
        pass  # clip_segments table not created yet

    # Content segments keyed by clip_id (derived from transcripts via lexical density)
    content_by_clip: dict[int, list] = {}
    try:
        for clip_id, s, e in conn.execute(
            "SELECT clip_id, start_sec, end_sec FROM content_segments ORDER BY clip_id, seg_index"
        ):
            content_by_clip.setdefault(clip_id, []).append((s, e))
    except sqlite3.OperationalError:
        pass

    # Descriptions / categories keyed by clip_id
    desc_by_clip: dict[int, tuple[str, str]] = {}
    try:
        for clip_id, cat, desc in conn.execute(
            "SELECT clip_id, category, description FROM clip_descriptions"
        ):
            desc_by_clip[clip_id] = (cat or "", desc or "")
    except sqlite3.OperationalError:
        pass

    # Characters + context per clip
    chars_by_clip: dict[int, tuple[str, str]] = {}
    try:
        for clip_id, chars, ctx in conn.execute(
            "SELECT clip_id, characters, context FROM clip_characters"
        ):
            chars_by_clip[clip_id] = (chars or "", ctx or "")
    except sqlite3.OperationalError:
        pass

    # Shot value + duration multiplier
    shot_by_clip: dict[int, tuple[str, float]] = {}
    try:
        for clip_id, sv, mult in conn.execute(
            "SELECT clip_id, shot_value, multiplier FROM clip_shot_values"
        ):
            shot_by_clip[clip_id] = (sv or "", float(mult or 0.85))
    except sqlite3.OperationalError:
        pass

    # Angle por clip (heurístico o manual)
    angle_by_clip: dict[int, str] = {}
    try:
        for clip_id, angle in conn.execute(
            "SELECT clip_id, angle FROM clip_angles"
        ):
            angle_by_clip[clip_id] = angle or "Normal"
    except sqlite3.OperationalError:
        pass

    # Metadata payload (la fuente DE VERDAD para los 5 campos de Resolve v11)
    payload_by_clip: dict[int, dict] = {}
    try:
        for clip_id, desc, shot, scene, kw, comments in conn.execute(
            "SELECT clip_id, description, shot, scene, keywords, comments "
            "FROM clip_metadata_payload"
        ):
            payload_by_clip[clip_id] = {
                "description": desc or "", "shot": shot or "",
                "scene": scene or "", "keywords": kw or "",
                "comments": comments or "",
            }
    except sqlite3.OperationalError:
        pass

    # REGLA (doctrina 2026-05-27): markers SOLO en clips con transcript verificable.
    # Si transcript_quality.is_hallucinated=1, el contenido del Whisper es basura
    # (música ambient sobre diálogo) — descripciones LLM y question detection
    # producen markers falsos. Excluir estos clips.
    hallucinated_clips: set[int] = set()
    try:
        for (cid,) in conn.execute(
            "SELECT clip_id FROM transcript_quality WHERE is_hallucinated=1"
        ):
            hallucinated_clips.add(cid)
    except sqlite3.OperationalError:
        pass  # tabla no existe (proyecto sin audit) — no filtramos

    # EXCEPCIÓN (iter9 2026-05-27): si existe master_transcript para el clip,
    # las question_segments fueron derivadas del MASTER (combina A1+lavaliers).
    # El lavalier típicamente tiene audio limpio del entrevistador o entrevistado,
    # así que las preguntas SÍ son corroborables aunque el A1 del video esté
    # alucinado por música ambient. Sólo filtrar curated_segments
    # (esos sí fueron escritos manualmente sobre el A1 viejo).
    clips_with_master: set[int] = set()
    if 'tr_dir' in dir() or True:
        # tr_dir se construye más arriba en el script
        pass
    # Detectar archivos master_{cid}.json en el dir de transcripts
    tr_path_root = (root / ".cinema_assistant" / "transcripts") if 'root' in locals() else None
    # Calcular tr_dir robustly desde db_path
    _tr_dir = (db_path.parent / "transcripts") if 'db_path' in locals() else None
    if _tr_dir is None:
        import os as _os
        _tr_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "transcripts"
    if _tr_dir.exists():
        for p in _tr_dir.glob("master_*.json"):
            try:
                cid = int(p.stem.replace("master_", ""))
                clips_with_master.add(cid)
            except ValueError:
                pass
    if clips_with_master:
        print(f"  ✓ master transcripts disponibles para {len(clips_with_master)} clips "
              f"(question_segments de esos clips NO se filtran)")
    if hallucinated_clips:
        print(f"  ⊘ markers excluidos para {len(hallucinated_clips)} clips "
              f"con transcript alucinado")

    # Curated segments por clip (v11) — fuente para markers Green con descripción real
    # FILTRO: omitir clips con is_hallucinated=1 (markers no corroborables)
    # EXCEPCION (CLIENTE_1 dia 3, 2026-08-23): un tramo con
    # curated_by='claude' NO se filtra por transcript alucinado.
    #
    # El filtro existe porque el full_text 'auto' se construye PEGANDO el
    # transcript ("Dialogo: ..."), asi que un A1 alucinado se propagaria tal cual
    # a un marcador visible. Un tramo curado a mano se escribio MIRANDO EL FRAME,
    # no leyendo el A1: su descripcion vale justo cuando el transcript no vale, y
    # de hecho suele decirlo ("transcript alucinado, sin dialogo real").
    # Es el mismo razonamiento que la excepcion de master_transcript de arriba.
    #
    # Caso fundador: 7 de 67 tramos curados a mano se perdian en silencio, entre
    # ellos "ultimo plano del dia: el collar en primer termino" y "arranca la
    # sesion de exteriores". El recurso sin dialogo es justo donde la descripcion
    # mas falta hace para encontrar el plano, y era justo el que se caia.
    curated_by_clip: dict[int, list[dict]] = {}
    curated_skipped = 0
    curated_rescatados = 0
    try:
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(clip_curated_segments)")}
        _cb = "curated_by" if "curated_by" in _cols else "NULL"
        for cid, s, e, shot, angle, chars, full_text, is_repr, total_note, curated_by in conn.execute(
            "SELECT clip_id, start_sec, end_sec, shot_value, angle, characters, "
            f"full_text, is_representative, total_dur_note, {_cb} "
            "FROM clip_curated_segments ORDER BY clip_id, seg_index"
        ):
            if cid in hallucinated_clips:
                if (curated_by or "") != "claude":
                    curated_skipped += 1
                    continue
                curated_rescatados += 1
            curated_by_clip.setdefault(cid, []).append({
                "s": s, "e": e, "shot": shot or "", "angle": angle or "",
                "chars": chars or "", "full_text": full_text or "",
                "is_repr": int(is_repr or 0),
                "total_note": total_note or "",
            })
    except sqlite3.OperationalError:
        pass
    if curated_skipped:
        print(f"  ⊘ curated_segments skipped: {curated_skipped} (transcript alucinado)")
    if curated_rescatados:
        print(f"  ✓ curated_segments preservados: {curated_rescatados} "
              f"(transcript alucinado pero curated_by='claude')")

    # Questions per interview (video y audio)
    # FILTRO: omitir clips con is_hallucinated=1 SI no tienen master_transcript.
    # Si hay master (combina A1+lavaliers), las preguntas vienen del lavalier
    # limpio y SÍ son corroborables.
    questions_by_clip: dict[int, list[dict]] = {}
    questions_skipped = 0
    try:
        # question_short = versión curada clara/sintética (petición del
        # usuario FCC 2026-07-11: el marker lleva LA PREGUNTA, no el garble
        # literal de Whisper). Fallback al texto literal si no existe.
        for clip_id, s, e, qtext, summary in conn.execute(
            "SELECT clip_id, start_sec, end_sec, "
            "COALESCE(NULLIF(TRIM(question_short),''), question_text), "
            "response_summary "
            "FROM question_segments ORDER BY clip_id, seg_index"
        ):
            if clip_id in hallucinated_clips and clip_id not in clips_with_master:
                questions_skipped += 1
                continue
            questions_by_clip.setdefault(clip_id, []).append({
                "s": s, "e": e, "q": qtext or "", "r": summary or ""
            })
    except sqlite3.OperationalError:
        # compat: manifests viejos sin question_short (o sin la tabla)
        try:
            for clip_id, s, e, qtext, summary in conn.execute(
                "SELECT clip_id, start_sec, end_sec, question_text, response_summary "
                "FROM question_segments ORDER BY clip_id, seg_index"
            ):
                if clip_id in hallucinated_clips and clip_id not in clips_with_master:
                    questions_skipped += 1
                    continue
                questions_by_clip.setdefault(clip_id, []).append({
                    "s": s, "e": e, "q": qtext or "", "r": summary or ""
                })
        except sqlite3.OperationalError:
            pass
    if questions_skipped:
        print(f"  ⊘ question_segments skipped: {questions_skipped} (transcript alucinado, sin master)")
    # Candado de curaduría (FCC 2026-07-11): los markers llevan la pregunta
    # CURADA — bakear con preguntas sin question_short manda garble de
    # Whisper a la cara del editor.
    try:
        sin_curar, = conn.execute(
            "SELECT COUNT(*) FROM question_segments "
            "WHERE COALESCE(TRIM(question_short),'') = ''").fetchone()
        if sin_curar:
            print(f"  ⚠⚠ {sin_curar} preguntas SIN CURAR (question_short vacío) "
                  f"— correr surface_transcript_suspects.py y curar ANTES de "
                  f"entregar este bake.")
    except sqlite3.OperationalError:
        pass

    # A-roll / B-roll (v0.2.0) — el Lua arma una timeline por rol, colorea el
    # clip y escribe el Keyword. `reason` viaja para que el editor pueda ver en
    # la nota POR QUE cayo ahi sin abrir el informe.
    roll_by_clip: dict[int, tuple] = {}
    try:
        for cid, roll, score, reason in conn.execute(
            "SELECT clip_id, roll, score, reason FROM clip_roll"
        ):
            roll_by_clip[cid] = (roll or "", score or 0.0, reason or "")
    except sqlite3.OperationalError:
        pass  # proyecto sin derive_roll todavia

    # Reels / capsulas (v0.4.0) — a que pieza de la campaña pertenece cada clip.
    # El Lua pone un marcador Sky al empezar cada bloque en la timeline de
    # A-ROLL. `metodo` viaja para poder distinguir de un vistazo lo decidido por
    # el texto de lo heredado por la hora del rodaje.
    reel_by_clip: dict[int, tuple] = {}
    try:
        for cid, n, titulo, metodo in conn.execute(
            "SELECT clip_id, reel_n, reel_titulo, metodo FROM clip_reels"
        ):
            reel_by_clip[cid] = (n or 0, titulo or "", metodo or "")
    except sqlite3.OperationalError:
        pass  # proyecto sin derive_reels todavia

    # Tramos de OTRA capsula dentro del clip. Solo los que NO son la capsula
    # del propio clip: repetir la principal seria ruido. El Lua les pone un
    # marcador dentro del clip para que se vean sin abrir el informe.
    reel_segs_by_clip: dict[int, list] = {}
    try:
        for cid, s, e, n, tit in conn.execute(
            "SELECT clip_id, start_sec, end_sec, reel_n, reel_titulo "
            "FROM clip_reel_segments ORDER BY clip_id, idx"
        ):
            principal = (reel_by_clip.get(cid) or (0, "", ""))[0]
            if n and n != principal:
                reel_segs_by_clip.setdefault(cid, []).append(
                    (float(s), float(e), int(n), tit or ""))
    except sqlite3.OperationalError:
        pass  # manifest anterior a la segmentacion

    # Corte de silencios (v0.4.0) — los tramos que se conservan de cada clip.
    # Sin esto (o con CORTAR_SILENCIOS apagado en la Consola) el clip baja
    # entero: es opt-in y reversible por diseño.
    cortes_by_clip: dict[int, list] = {}
    try:
        for cid, a, b in conn.execute(
            "SELECT clip_id, start_sec, end_sec FROM clip_keep_ranges "
            "ORDER BY clip_id, idx"
        ):
            cortes_by_clip.setdefault(cid, []).append((float(a), float(b)))
    except sqlite3.OperationalError:
        pass  # proyecto sin derive_silence_cuts todavia

    # Tomas (v0.4.0) — grupo, numero y estado, para el marcador de cada toma.
    takes_by_clip: dict[int, dict] = {}
    try:
        # SOLO grupos con 2 o mas tomas (2026-08-18). `derive_takes` abre un
        # grupo por cada clip con dialogo, se repita o no; hornear todos hacia
        # que el evento de 30 min y la charla de set llegaran a Resolve con
        # `toma` puesta, y de ahi salian dos errores visibles: el color por
        # grupo de tomas les daba color de reel, y el corte de silencios
        # "solo en tomas" los consideraba tomas. Un grupo de UNA toma no es un
        # grupo de tomas. Misma regla que derive_roll y derive_video_categories,
        # para que las tres no puedan contradecirse.
        for cid, gid, num, completa, marcador, cita in conn.execute(
            "SELECT clip_id, group_id, take_num, completa, marcador, cita "
            "FROM clip_takes WHERE group_id IN ("
            "  SELECT group_id FROM clip_takes GROUP BY group_id HAVING COUNT(*) > 1)"
        ):
            takes_by_clip[cid] = {
                "g": gid or 0, "n": num or 0, "completa": 1 if completa else 0,
                "marcador": marcador or "", "cita": cita or "",
            }
        tomas_de_grupo = dict(conn.execute(
            "SELECT id, n_tomas FROM take_groups"))
        for v in takes_by_clip.values():
            v["de"] = tomas_de_grupo.get(v["g"], 0)
    except sqlite3.OperationalError:
        pass  # proyecto sin derive_takes todavia

    # Beats de entrevista (v0.2.0) — pregunta/respuesta SEPARADAS, mas pausas,
    # palabras clave y candidatos a momento emocional.
    # Mismo filtro que `questions`: sin transcript verificable no hay beats.
    beats_by_clip: dict[int, list[dict]] = {}
    beats_skipped = 0
    try:
        for cid, kind, s, e, speaker, txt, conf, src, qi in conn.execute(
            "SELECT clip_id, kind, start_sec, end_sec, speaker, text, "
            "confidence, source, q_index "
            "FROM interview_beats ORDER BY clip_id, beat_index"
        ):
            if cid in hallucinated_clips and cid not in clips_with_master:
                beats_skipped += 1
                continue
            beats_by_clip.setdefault(cid, []).append({
                "kind": kind or "", "s": s, "e": e, "speaker": speaker or "",
                "text": txt or "", "conf": conf or 0.0, "src": src or "",
                "qi": qi if qi is not None else -1,
            })
    except sqlite3.OperationalError:
        pass  # proyecto sin derive_interview_beats todavia
    if beats_skipped:
        print(f"  ⊘ interview_beats skipped: {beats_skipped} (transcript alucinado, sin master)")

    # Sync pairs — include the external audio's full path so the Lua script can import it.
    # Tolerante como los otros 15 queries opcionales de este archivo: era el UNICO
    # sin try/except, asi que hornear antes de correr el sync reventaba con
    # "no such table: audio_sync_pairs" (auditoria 2026-07-31). Hornear sin sync es
    # legitimo: sirve para ver el material en Resolve antes de sincronizar.
    try:
        # El primer par de cada video es el que usa el Lua para la HORA del clip
        # (horaDe). Va primero el medido por onda, y luego la confianza: el
        # 9469 se colocaba por su par con drc, derivado por reloj y descartado
        # por la onda (el TX no estaba en quien hablaba), y quedaba 1 s corrido
        # contra el lavalier bueno (Asistente, 2026-10-01).
        _filas = conn.execute("""
            SELECT cv.path, ca.filename, p.offset_sec, p.confidence, ca.path,
                   p.method, IFNULL(p.notes, '')
            FROM audio_sync_pairs p
            JOIN clips cv ON cv.id = p.video_clip_id
            JOIN clips ca ON ca.id = p.audio_clip_id
        """).fetchall()
        _filas.sort(key=orden_de_pares)
        sync_rows = [r[:5] for r in _filas]
    except sqlite3.OperationalError:
        sync_rows = []
        print("  ⊘ sin audio_sync_pairs: se hornea sin sync "
              "(corre bin/sync_transcript.py para incluirlo)")

    # Fase 4 (2026-05-26): para cada audio, obtener el speaker dominante
    # (mayor duration_sec entre los speakers del audio) y, si su catalog_id
    # tiene canonical_name, propagar al sync entry como `speaker`.
    speaker_by_audio: dict[str, str] = {}  # audio_path → "Nombre Real"
    try:
        rows = conn.execute("""
            SELECT c.path,
                   COALESCE(vc.canonical_name, asp.speaker_label) AS name
            FROM audio_speakers asp
            JOIN clips c ON c.id = asp.clip_id
            LEFT JOIN voice_catalog vc ON vc.id = asp.catalog_id
            WHERE asp.id IN (
                SELECT id FROM audio_speakers asp2
                WHERE asp2.clip_id = asp.clip_id
                ORDER BY asp2.duration_sec DESC LIMIT 1
            )
        """).fetchall()
        for apath, name in rows:
            if name and not name.startswith("SPEAKER_"):
                speaker_by_audio[apath] = name
    except sqlite3.OperationalError:
        pass  # tabla audio_speakers no existe (proyecto pre-Fase 1)

    # Fase 4: sound_events de hito (risa/aplauso/whistle) por clip
    highlight_events_by_clip: dict[int, list[dict]] = {}
    try:
        for row in conn.execute("""
            SELECT clip_id, start_sec, end_sec, event_group, confidence
            FROM sound_events
            WHERE event_group IN ('laughter','applause','whistle','bang')
              AND confidence >= 0.30
            ORDER BY clip_id, start_sec
        """):
            cid, s, e, g, c = row
            highlight_events_by_clip.setdefault(cid, []).append({
                "s": s, "e": e, "group": g, "conf": c
            })
    except sqlite3.OperationalError:
        pass  # tabla sound_events no existe

    conn.close()

    lines = ["-- Auto-generated by export_lua_data.py (v11)", "return {", "  clips = {"]
    n_clips = 0
    n_segs = 0
    n_content = 0
    n_curated = 0
    n_payload = 0
    n_interview = 0
    n_questions = 0
    n_beats = 0
    n_roll = __import__('collections').Counter()
    n_reel = __import__('collections').Counter()
    n_con_cortes = 0
    n_tramos = 0
    n_reel_segs = 0
    n_shot = 0
    byname: dict[str, str] = {}  # filename -> path; fallback index, last writer wins
    # El gemelo JSON del horneado (2026-10-06): que clips y que WAV trae, con su
    # rol y su dia. Lo lee lib/pedido.py para decir que tiene que volver de
    # Resolve en el .drt; Python no parsea el .lua.
    indice: dict = {"formato": 1, "clips": {}, "audios": {}}
    for (clip_id, fn, path, folder, rel_path, dur, created, cam_model,
         status, notes, motion, cam_make, ext) in clip_rows:
        segs = segs_by_clip.get(clip_id, [])
        n_segs += len(segs)

        content_segs = content_by_clip.get(clip_id, [])
        n_content += len(content_segs)

        cat, desc = desc_by_clip.get(clip_id, ("", ""))

        chars, ctx = chars_by_clip.get(clip_id, ("", ""))
        sv, mult = shot_by_clip.get(clip_id, ("", 0.85))
        if sv:
            n_shot += 1
        angle = angle_by_clip.get(clip_id, "Normal")

        # v11: metadata payload pre-armado (la fuente para applyMetadata)
        pl = payload_by_clip.get(clip_id, {})
        if pl.get("description"):
            n_payload += 1

        # Curated segments (v11) — fuente para markers Green con descripción real
        curated = curated_by_clip.get(clip_id, [])
        n_curated += len(curated)
        curated_lua_items = []
        for cs in curated:
            curated_lua_items.append(
                "{s=" + f"{cs['s']:.2f}" + ",e=" + f"{cs['e']:.2f}"
                + ",shot=" + lua_str(cs['shot'])
                + ",angle=" + lua_str(cs['angle'])
                + ",chars=" + lua_str(cs['chars'])
                + ",text=" + lua_str(cs['full_text'])
                + ",is_repr=" + str(cs['is_repr'])
                + ",note=" + lua_str(cs['total_note']) + "}"
            )
        curated_lua = ", ".join(curated_lua_items)

        # Question segments (Lua array of {s, e, q, r})
        qs = questions_by_clip.get(clip_id, [])
        n_questions += len(qs)
        q_lua_items = [
            "{s=" + f"{q['s']:.2f}" + ",e=" + f"{q['e']:.2f}"
            + ",q=" + lua_str(q['q']) + ",r=" + lua_str(q['r']) + "}"
            for q in qs
        ]
        q_lua = ", ".join(q_lua_items)

        # Beats (v0.2.0). Nota: `questions` se sigue emitiendo tal cual para
        # que un data.lua nuevo funcione con un asistente_*.lua viejo.
        bts = beats_by_clip.get(clip_id, [])
        n_beats += len(bts)
        beats_lua = ", ".join(
            "{kind=" + lua_str(b["kind"])
            + ",s=" + f"{b['s']:.2f}" + ",e=" + f"{b['e']:.2f}"
            + ",text=" + lua_str(b["text"])
            + ",speaker=" + lua_str(b["speaker"])
            + f",conf={b['conf']:.2f}"
            + ",src=" + lua_str(b["src"])
            + f",qi={b['qi']}}}"
            for b in bts
        )

        roll_v, roll_s, roll_r = roll_by_clip.get(clip_id, ("", 0.0, ""))
        if roll_v:
            n_roll[roll_v] += 1

        reel_n, reel_tit, reel_met = reel_by_clip.get(clip_id, (0, "", ""))
        if reel_n:
            n_reel[reel_n] += 1
        rsegs = reel_segs_by_clip.get(clip_id, [])
        n_reel_segs += len(rsegs)
        reel_segs_lua = ", ".join(
            "{s=" + f"{s:.2f}" + ",e=" + f"{e:.2f}" + ",n=" + str(n)
            + ",titulo=" + lua_str(t) + "}" for s, e, n, t in rsegs)
        cortes_lua = ", ".join(f"{{{a:.2f},{b:.2f}}}"
                               for a, b in cortes_by_clip.get(clip_id, []))
        if cortes_lua:
            n_con_cortes += 1
            n_tramos += len(cortes_by_clip[clip_id])
        tk = takes_by_clip.get(clip_id) or {}
        toma_lua = (
            "{g=" + str(tk.get("g", 0)) + ",n=" + str(tk.get("n", 0))
            + ",de=" + str(tk.get("de", 0))
            + ",completa=" + ("true" if tk.get("completa") else "false")
            + ",marcador=" + lua_str(tk.get("marcador", ""))
            + ",cita=" + lua_str(tk.get("cita", "")) + "}"
        ) if tk else "nil"

        camera = cameras.role_of(fn, cam_model, cam_make, ext, registry=reg)
        interview = is_interview(rel_path, camera, dur, motion, category=cat)
        if interview:
            n_interview += 1

        # Fase 4: highlight_events (laughter/applause/whistle/bang) — markers
        # Yellow especiales en Resolve para localizar momentos de hito.
        hl_items = highlight_events_by_clip.get(clip_id, [])
        hl_lua_items = [
            "{s=" + f"{h['s']:.2f}" + ",e=" + f"{h['e']:.2f}"
            + ",group=" + lua_str(h['group'])
            + f",conf={h['conf']:.2f}}}"
            for h in hl_items
        ]
        hl_lua = ", ".join(hl_lua_items)

        indice["clips"][path] = {
            "nombre": fn, "roll": roll_v, "dia": dia_de_rodaje(created),
            "folder": proyecto.grupo_de_camara(folder, rel_path, cfg)}
        # v11 emite SOLO los campos que usa el Lua: metadata payload + curated +
        # questions + sync support + highlights. Compactamos el resto.
        lines.append(
            # `folder` es el GRUPO DE CAMARA para el Lua (skews, pista fija, una
            # timeline por camara): por prefijo si el proyecto lo declara.
            f"    [{lua_str(path)}] = {{name={lua_str(fn)}, "
            f"folder={lua_str(proyecto.grupo_de_camara(folder, rel_path, cfg))}, "
            f"status={lua_str(status)}, notes={lua_str(notes)}, "
            f"dur={dur or 0:.2f}, created={lua_str(created)}, "
            f"dia={lua_str(dia_de_rodaje(created))}, "
            f"camera={lua_str(camera)}, interview={'true' if interview else 'false'}, "
            f"category={lua_str(cat)}, "
            f"who={lua_str(chars_by_clip.get(clip_id, ('', ''))[0])}, "
            f"meta_description={lua_str(pl.get('description',''))}, "
            f"meta_shot={lua_str(pl.get('shot',''))}, "
            f"meta_scene={lua_str(pl.get('scene',''))}, "
            f"meta_keywords={lua_str(pl.get('keywords',''))}, "
            f"meta_comments={lua_str(pl.get('comments',''))}, "
            f"curated_segments={{{curated_lua}}}, "
            f"questions={{{q_lua}}}, "
            f"beats={{{beats_lua}}}, "
            f"roll={lua_str(roll_v)}, "
            f"roll_score={roll_s:.2f}, "
            f"roll_reason={lua_str(roll_r)}, "
            f"reel={reel_n}, "
            f"reel_titulo={lua_str(reel_tit)}, "
            f"reel_metodo={lua_str(reel_met)}, "
            f"reel_segmentos={{{reel_segs_lua}}}, "
            f"toma={toma_lua}, "
            f"cortes={{{cortes_lua}}}, "
            f"highlights={{{hl_lua}}}}},"
        )
        byname[fn] = path
        n_clips += 1
    lines.append("  },")

    lines.append("  byname = {")
    for fn, path in byname.items():
        lines.append(f"    [{lua_str(fn)}] = {lua_str(path)},")
    lines.append("  },")

    # DATA.sync[vpath] = lista de pares ordenados por confianza descendente.
    # Cambio 2026-05-25 (Zezzions dual-lavalier): antes era un solo dict por
    # video; ahora es lista para soportar 2 TX (TX1 en A2, TX2 en A3).
    # Para proyectos single-lavalier la lista tiene un solo elemento (compat).
    lines.append("  sync = {")
    pairs_by_video: dict[str, list[str]] = {}
    seen_audios_per_video: dict[str, set] = {}
    for vpath, aname, offset, conf, apath in sync_rows:
        if (conf or 0) < SYNC_MIN_CONF:
            continue
        seen = seen_audios_per_video.setdefault(vpath, set())
        if apath in seen:
            continue
        seen.add(apath)
        # Fase 4: agregar nombre del speaker dominante si existe
        speaker_name = speaker_by_audio.get(apath, "")
        pairs_by_video.setdefault(vpath, []).append(
            "{audio=" + lua_str(aname)
            + ", audiopath=" + lua_str(apath)
            # Tres decimales: con dos, el redondeo ya mete hasta 5 ms antes de
            # que el Lua cuantice a fotograma.
            + f", offset={offset or 0:.3f}, conf={conf or 0:.3f}"
            + ", speaker=" + lua_str(speaker_name) + "}"
        )
    for vpath, plist in pairs_by_video.items():
        lines.append(
            f"    [{lua_str(vpath)}] = {{ " + ", ".join(plist) + " },"
        )
    lines.append("  },")

    # AUDIOS EXTERNOS — su propia tabla, mismo esquema + questions para entrevistas
    lines.append("  audios = {")
    n_audios = 0
    n_audio_segs = 0
    n_audio_q = 0
    for (clip_id, fn, path, folder, rel_path, dur, created, mtime) in audio_rows:
        content_segs = content_by_clip.get(clip_id, [])
        n_audio_segs += len(content_segs)
        content_lua = ", ".join(f"{{{s:.2f},{e:.2f}}}" for s, e in content_segs)
        cat, desc = desc_by_clip.get(clip_id, ("", ""))
        pl = payload_by_clip.get(clip_id, {})

        # Preguntas en audios entrevista
        qs = questions_by_clip.get(clip_id, [])
        n_audio_q += len(qs)
        q_lua_items = [
            "{s=" + f"{q['s']:.2f}" + ",e=" + f"{q['e']:.2f}"
            + ",q=" + lua_str(q['q']) + ",r=" + lua_str(q['r']) + "}"
            for q in qs
        ]
        q_lua = ", ".join(q_lua_items)

        indice["audios"][path] = {
            "nombre": fn, "tx": proyecto.tx_de_lavalier(rel_path, cfg),
            "dia": dia_de_rodaje(_epoch_cadena.get(clip_id, (mtime or 0) - (dur or 0)))}
        lines.append(
            f"    [{lua_str(path)}] = {{name={lua_str(fn)}, folder={lua_str(folder)}, "
            f"tx={lua_str(proyecto.tx_de_lavalier(rel_path, cfg))}, "
            f"dia={lua_str(dia_de_rodaje(_epoch_cadena.get(clip_id, (mtime or 0) - (dur or 0))))}, "
            f"dur={dur or 0:.2f}, created={lua_str(created)}, "
            f"category={lua_str(cat)}, "
            f"meta_description={lua_str(pl.get('description', desc))}, "
            f"meta_shot={lua_str(pl.get('shot',''))}, "
            f"meta_scene={lua_str(pl.get('scene',''))}, "
            f"meta_keywords={lua_str(pl.get('keywords',''))}, "
            f"meta_comments={lua_str(pl.get('comments',''))}, "
            f"content_segments={{{content_lua}}}, "
            f"chain={lua_str(_chain_of.get(clip_id, ''))}, "
            f"chain_pos={_chain_pos.get(clip_id, 0.0):.3f}, "
            f"epoch_start={_epoch_cadena.get(clip_id, (mtime or 0) - (dur or 0)):.3f}, "
            f"epoch_mtime={((mtime or 0) - (dur or 0)):.3f}, "
            f"questions={{{q_lua}}}}},"
        )
        n_audios += 1
    lines.append("  },")

    # VENTANAS DE SHOW al horneado (2026-08-13).
    #
    # Las mide `bin/derive_show_windows.py` y vivian solo en project_config.json,
    # donde el Lua no las ve. En una timeline por TIEMPO REAL hacen falta para
    # navegar: el material previo al show y los huecos empujan la primera
    # cancion muy adentro —en Morsa, del minuto 19 de la empacada al 65 de la
    # cronologica— y sin una marca no hay forma de encontrarla salvo scrubbear.
    # Que el editor no las encuentre se parece demasiado a que no esten.
    _cfg_p = root / ".cinema_assistant" / "project_config.json"
    try:
        _cfg = json.loads(_cfg_p.read_text(encoding="utf-8")) if _cfg_p.exists() else {}
    except (OSError, json.JSONDecodeError):
        _cfg = {}
    _ventanas = _cfg.get("show_windows") or []
    lines.append("  show_windows = {")
    for _w in _ventanas:
        _ini, _fin = _w.get("inicio_epoch"), _w.get("fin_epoch")
        if _ini is None or _fin is None:
            continue
        lines.append(
            f"    {{inicio={float(_ini):.3f}, fin={float(_fin):.3f}, "
            # `local` es palabra reservada en Lua: como clave hay que escribirla
            # entre corchetes o el archivo no carga.
            f"[\"local\"]={lua_str(str(_w.get('inicio_local', '')))}, "
            f"dur_min={float(_w.get('dur_min', 0)):.1f}, "
            f"nivel_db={float(_w.get('nivel_db', 0)):.1f}}},")
    lines.append("  },")
    lines.append("}")

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Despues del .lua: un indice nunca puede ser mas viejo que su horneado.
    ruta_indice = ruta_del_indice(out)
    ruta_indice.write_text(json.dumps(indice, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    # Conteos reales de sync (bug histórico: len(seen) sólo medía el último
    # video del loop, no el total). Ahora se cuentan videos con sync y pares
    # totales escritos al data file.
    n_videos_with_sync = len(pairs_by_video)
    n_sync_pairs = sum(len(v) for v in pairs_by_video.values())
    print(f"Wrote {out}  (v11)")
    print(f"  indice: {ruta_indice.name}")
    print(f"  clips: {n_clips}  |  curated: {n_curated}  |  payloads: {n_payload}  "
          f"|  questions: {n_questions}  |  beats: {n_beats}  "
          f"|  A-roll: {n_roll['A']} B-roll: {n_roll['B']} BTS: {n_roll['BTS']}  "
          f"|  sync pairs: {n_sync_pairs} "
          f"({n_videos_with_sync} videos)")
    print(f"  audios: {n_audios}  |  audio content segs: {n_audio_segs}  "
          f"|  audio questions: {n_audio_q}")
    if n_reel or n_con_cortes:
        reparto = " ".join(f"R{n}:{c}" for n, c in sorted(n_reel.items()))
        print(f"  reels: {sum(n_reel.values())} clips ({reparto})  |  "
              f"cortes: {n_con_cortes} clips -> {n_tramos} tramos"
              + (f"  |  otra cápsula dentro: {n_reel_segs} tramo(s)"
                 if n_reel_segs else ""))


if __name__ == "__main__":
    main()
