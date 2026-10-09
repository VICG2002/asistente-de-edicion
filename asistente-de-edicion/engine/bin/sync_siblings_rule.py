#!/usr/bin/env python3
"""REGLA OBLIGATORIA (Zezzions 2026-05-26, idea del usuario):
Cuando un video tiene sync con UN lavalier, debe intentar derivar sync
del lavalier hermano (mismo Wireless PRO RX, mismo basename, otro canal).

Salvaguardia: solo derivar si los dos audios comparten contenido
(envelope cross-correlation prominence ≥ 0.20 + voice_sim si hay
embeddings). Si los hermanos son de personas distintas (00007 Dr ENTREVISTADO_5
vs 00007 Izq ENTREVISTADO_13/ENTREVISTADO_10), NO derivar.

Uso:
    bin/sync_siblings_rule.py --root <disk>
    bin/sync_siblings_rule.py --root <disk> --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.voice_match import _emb_from_blob, _cosine
from lib.lavalier_pairs import fourgram_overlap
from lib import manifest  # noqa: E402


def transcript_overlap(tr_dir: Path, audio_a_id: int, audio_b_id: int) -> float:
    """4-grama overlap entre transcripts de dos audios."""
    try:
        with (tr_dir / f"{audio_a_id}.json").open() as f:
            t_a = json.load(f).get("text", "") or ""
        with (tr_dir / f"{audio_b_id}.json").open() as f:
            t_b = json.load(f).get("text", "") or ""
    except Exception:
        return 0.0
    return fourgram_overlap(t_a[:6000], t_b[:6000])


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


# Convencion de carpetas por defecto: la de Zezzions, que es donde nacio esta
# regla. Se conserva como fallback para no romper proyectos viejos.
CADENAS_POR_DEFECTO = [("/Dr/", "/Izq/")]


def cadenas_lavalier(root: Path) -> list[tuple[str, str]]:
    """Pares de carpetas que son los dos canales de un dual-lavalier.

    Se DECLARAN en <disco>/.cinema_assistant/project_config.json:

        "cadenas_lavalier": [["Audio/izquierdo/", "Audio/derecho/"],
                             ["TX1/", "TX2/"]]

    POR QUE SE DECLARA Y NO SE ADIVINA (2026-08-28). Esto estaba hardcodeado a
    `/Dr/` y `/Izq/`. Cualquier otra convencion —`Audio/01/`+`Audio/02/`,
    `Audio/izquierdo/`+`Audio/derecho/`, `TX1/`+`TX2/`— devolvia None y TODO
    salia "sin-hermano" EN SILENCIO: no por falta de par real, sino por el
    heuristico incompleto. Lo destapo la bifurcacion de Adrian (The Shelter,
    2026-08-02), que lo arreglo agregando su par a la lista.

    Aqui se arreglo al reves, porque el motor ya tiene la regla contraria
    escrita: lo que es del rodaje se declara y el motor no lo infiere (ver
    lib/timeline_resolve.py, lib/dialogue_density.py, lib/project_vocabulary.py,
    y la leccion 53 sobre sacar los datos del codigo). Una funcion que crece por
    proyecto es una funcion que hay que acordarse de tocar; una que lee la
    declaracion del proyecto no se olvida, porque el proyecto que no la declara
    no encuentra hermanos y eso se ve.
    """
    cfg_path = root / ".cinema_assistant" / "project_config.json"
    pares: list[tuple[str, str]] = []
    if cfg_path.exists():
        try:
            d = json.load(cfg_path.open())
        except (json.JSONDecodeError, OSError) as e:
            print(f"AVISO: no pude leer {cfg_path}: {e}", file=sys.stderr)
            d = {}
        for par in d.get("cadenas_lavalier", []) or []:
            if isinstance(par, (list, tuple)) and len(par) == 2 \
                    and all(isinstance(x, str) and x for x in par):
                pares.append((par[0], par[1]))
            else:
                print(f"AVISO: cadenas_lavalier ignora entrada mal formada: {par!r}",
                      file=sys.stderr)
    return pares + CADENAS_POR_DEFECTO


def sibling_audio_id(conn: sqlite3.Connection, audio_id: int,
                     cadenas: list[tuple[str, str]] | None = None) -> int | None:
    """Para audio_id, retorna el id del hermano.

    Primero el hermano MEDIDO: la fila aplicable de `lavalier_pairs`, que
    `build_lavalier_pairs.py` empareja por hora y por contenido. Despues, el
    de siempre: mismo basename en el canal opuesto.

    El basename solo vale cuando UN receptor graba los dos TX y numera igual
    los dos canales (MAB, Zezzions). Con TX que graban cada uno por su cuenta
    (Morsa, Asistente) el mismo numero no es la misma hora: en Asistente
    (2026-09-28) el hermano de `drc/00034` (15:34) salia `izq/00034` (19:17).
    La salvaguardia de 4-gramas lo rechazo bien, pero el hermano real,
    `izq/00031`, se quedaba sin sync.
    """
    try:
        medido = conn.execute(
            "SELECT CASE WHEN audio_a_id=? THEN audio_b_id ELSE audio_a_id END "
            "FROM lavalier_pairs WHERE applicable=1 AND (audio_a_id=? OR audio_b_id=?) "
            "LIMIT 1", (audio_id, audio_id, audio_id)).fetchone()
    except sqlite3.OperationalError:      # proyecto sin la tabla todavia
        medido = None
    if medido:
        return medido[0]
    row = conn.execute(
        "SELECT filename, rel_path FROM clips WHERE id=?", (audio_id,)
    ).fetchone()
    if not row:
        return None
    fname, rel = row
    other_pattern = None
    for a, b in (cadenas if cadenas is not None else CADENAS_POR_DEFECTO):
        if a in rel:
            other_pattern = f"%{b}%"
            break
        if b in rel:
            other_pattern = f"%{a}%"
            break
    if other_pattern is None:
        return None
    sib = conn.execute(
        "SELECT id FROM clips WHERE filename=? AND file_kind='audio' "
        "AND rel_path LIKE ? AND index_status='ok'",
        (fname, other_pattern)
    ).fetchone()
    return sib[0] if sib else None


def envelope_delta(path_a: Path, path_b: Path, analysis_dur: float = 60.0):
    """Cross-correlate envelope log-RMS de path_a y path_b.

    Returns (delta_sec, prominence) donde delta es el shift de b respecto a a.
    audio_b_start = audio_a_start + delta_sec.

    DELEGA (2026-08-13). Hasta hoy esto era una copia literal de
    `lib.lavalier_pairs.envelope_lag`, con sus dos defectos:

      1. Devolvia `a_start - b_start` mientras el docstring de aqui arriba y el
         consumidor de la linea 231 (`derived_off = src_off + delta_sec`)
         esperaban `b_start - a_start`. Cada offset derivado salia con un error
         de 2x delta. Invisible con dos lavas del mismo RX (delta ~ 0), real con
         dos TX de relojes independientes.
      2. Correlacionaba envolventes RMS crudas, que no tienen valores negativos,
         asi que la continua dominaba y el pico se iba al centro.

    Dos copias del mismo calculo es como se mantuvo el error: se arreglaba una y
    la otra seguia. Ahora hay una sola, con prueba en
    `tests/test_sync_convencion.py`.
    """
    from lib.lavalier_pairs import envelope_lag

    return envelope_lag(path_a, path_b, analysis_dur=analysis_dur)


def voice_sim_dominant(conn: sqlite3.Connection, a: int, b: int) -> float:
    def dom_emb(cid):
        # Sin `audio_speakers` (la capacidad `voz` no ha corrido) no hay dato:
        # mismo camino que un audio sin embedding, 0.0 = "no disponible".
        try:
            r = conn.execute(
                "SELECT embedding FROM audio_speakers WHERE clip_id=? AND embedding IS NOT NULL "
                "ORDER BY duration_sec DESC LIMIT 1", (cid,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not r or not r[0]:
            return None
        return _emb_from_blob(r[0])
    ea = dom_emb(a)
    eb = dom_emb(b)
    if ea is None or eb is None:
        return 0.0
    return _cosine(ea, eb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-envelope-prominence", type=float, default=0.20)
    ap.add_argument("--min-voice-sim-if-available", type=float, default=0.65)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    CADENAS = cadenas_lavalier(root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_siblings_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Obtener todos los pares (video, audio) actuales
    rows = conn.execute("""
        SELECT sp.id, sp.video_clip_id, sp.audio_clip_id, sp.offset_sec,
               sp.confidence, sp.method, sp.identity_score,
               v.filename, ca.filename, ca.rel_path
        FROM audio_sync_pairs sp
        JOIN clips v ON v.id=sp.video_clip_id
        JOIN clips ca ON ca.id=sp.audio_clip_id
        ORDER BY sp.video_clip_id
    """).fetchall()
    logging.info(f"Pairs existentes: {len(rows)}")

    now = time.time()
    n_derived = 0
    n_skip_no_sibling = 0
    n_skip_already = 0
    n_skip_low_prom = 0
    n_skip_distinct_voice = 0
    delta_cache: dict[tuple[int, int], tuple[float, float]] = {}

    for sp_id, vid, src_aid, src_off, src_conf, src_method, src_idscore, vfn, afn, arel in rows:
        sib_id = sibling_audio_id(conn, src_aid, CADENAS)
        if sib_id is None:
            n_skip_no_sibling += 1
            continue
        # ¿Ya está syncado el hermano?
        existing = conn.execute(
            "SELECT id, method FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
            (vid, sib_id)
        ).fetchone()
        if existing:
            n_skip_already += 1
            continue

        # Verificar similitud de contenido entre src y sibling
        key = tuple(sorted([src_aid, sib_id]))
        if key not in delta_cache:
            src_path = conn.execute("SELECT path FROM clips WHERE id=?", (src_aid,)).fetchone()[0]
            sib_path = conn.execute("SELECT path FROM clips WHERE id=?", (sib_id,)).fetchone()[0]
            r = envelope_delta(Path(src_path), Path(sib_path))
            if r is None:
                delta_cache[key] = (0.0, 0.0)
            else:
                # delta es de src→sib (audio_sib_start = audio_src_start + delta_sec)
                if key[0] != src_aid:
                    # Si invertimos el orden, invertir el signo
                    delta_cache[key] = (-r[0], r[1])
                else:
                    delta_cache[key] = r
        # Recuperar con orden correcto
        if key[0] == src_aid:
            delta_sec, prom = delta_cache[key]
        else:
            d, p = delta_cache[key]
            delta_sec, prom = -d, p

        # Salvaguardia FUERTE: overlap de transcript ≥ 0.10
        # (sin esto, ambient musical hace que pasen falsos positivos)
        tr_dir = root / ".cinema_assistant" / "transcripts"
        overlap = transcript_overlap(tr_dir, src_aid, sib_id)
        if overlap < 0.10:
            logging.info(f"  ⊘ {vfn}: src={afn} sib={sib_id} overlap_4gram={overlap:.2f} "
                         f"— contenido distinto (los lavaliers capturaron eventos distintos), NO derivar")
            n_skip_distinct_voice += 1
            continue

        # Salvaguardia 1: envelope prominence (debe ser razonable junto al overlap)
        if prom < args.min_envelope_prominence and overlap < 0.25:
            logging.info(f"  ⊘ {vfn}: prom={prom:.2f} + overlap={overlap:.2f} ambos bajos — NO derivar")
            n_skip_low_prom += 1
            continue

        vsim = voice_sim_dominant(conn, src_aid, sib_id)
        if vsim > 0 and vsim < 0.50 and overlap < 0.20:
            logging.info(f"  ⊘ {vfn}: voice_sim={vsim:.2f} + overlap={overlap:.2f} bajos — NO derivar")
            n_skip_distinct_voice += 1
            continue

        # Derivar: offset_sib = offset_src + delta
        derived_off = src_off + delta_sec
        # Verificar plausibilidad (no fuera de rango)
        sib_dur = conn.execute("SELECT duration_sec FROM clips WHERE id=?", (sib_id,)).fetchone()[0]
        v_dur = conn.execute("SELECT duration_sec FROM clips WHERE id=?", (vid,)).fetchone()[0]
        if abs(derived_off) > max(v_dur, sib_dur) + 30:
            logging.info(f"  ⊘ {vfn}: derived_off={derived_off:+.2f} fuera de rango plausible")
            n_skip_low_prom += 1
            continue

        notes = (f"sibling-rule: src={src_aid} off={src_off:+.2f} + delta={delta_sec:+.2f} "
                 f"(prom={prom:.2f}, vsim={vsim:.2f})")
        new_conf = max(0.65, src_conf * 0.9)
        if args.dry_run:
            logging.info(f"  [DRY] {vfn}: {afn}({src_off:+.2f}) → sib={sib_id} off={derived_off:+.2f}")
        else:
            conn.execute("""
                INSERT INTO audio_sync_pairs
                (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                 notes, created_at, identity_score)
                VALUES (?,?,?,?,?,?,?,?)
            """, (vid, sib_id, "sibling-rule-locked", derived_off, new_conf,
                  notes, now, src_idscore or 0.80))
            n_derived += 1
            logging.info(f"  ✓ {vfn}: derived sib={sib_id} off={derived_off:+.2f} "
                         f"(de src={src_aid} off={src_off:+.2f}, delta={delta_sec:+.2f}, prom={prom:.2f})")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nDerivados: {n_derived}, ya-existentes: {n_skip_already}, "
                 f"sin-hermano: {n_skip_no_sibling}, low-prom: {n_skip_low_prom}, "
                 f"hablantes-distintos: {n_skip_distinct_voice}")


if __name__ == "__main__":
    main()
