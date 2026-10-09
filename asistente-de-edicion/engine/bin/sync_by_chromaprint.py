#!/usr/bin/env python3
"""Sync por chromaprint (audio fingerprinting).

Caso fundador: complemento al `sync_acoustic.py` (envelope FFT) cuando este
falla por baja correlación de envelope. Chromaprint es estándar de la
industria (Last.fm, AcoustID) y más robusto a SNR/EQ.

Uso típico (último recurso después de transcript + questions + acoustic):
    bin/sync_by_chromaprint.py --root /Volumes/.../Zezzions VICG \\
        --min-confidence 0.30 --max-pairs-per-video 2

Precondición: `brew install chromaprint` para tener el binario `fpcalc`.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.chromaprint_sync import extract_fingerprint, correlate_fingerprints
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%lavas%")
    ap.add_argument("--location", default="video")
    ap.add_argument("--min-confidence", type=float, default=0.30)
    ap.add_argument("--max-pairs-per-video", type=int, default=2)
    ap.add_argument("--only-missing", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--max-duration", type=int, default=7200,
                    help="segundos de audio a huellar. El default de la libreria "
                         "eran 1800, o sea que de un WAV de 60 min solo se miraba "
                         "la mitad y nada de la segunda hora podia casar.")
    ap.add_argument("--rehacer-derivados", action="store_true",
                    help="apunta a los videos cuyos pares son TODOS derivados por "
                         "reloj. Son los que arrastran la cuantizacion a segundo "
                         "entero del creation_time (+-0.5 s).")
    ap.add_argument("--refinar", action="store_true",
                    help="segunda etapa: pulir el ancla gruesa de la huella con "
                         "correlacion de envolvente en ventana estrecha.")
    ap.add_argument("--min-dur", type=float, default=60.0,
                    help="duracion minima del video. El default de 60 s venia de "
                         "cuando esto era rescate de entrevistas; para cubrir el "
                         "B-roll hay que bajarlo, porque son clips cortos y son "
                         "justo los que se quedan colocados por reloj.")
    ap.add_argument("--max-desvio", type=float, default=2.0,
                    help="segundos de desvio maximo respecto al skew conocido de "
                         "la camara. La huella encuentra picos creibles donde no "
                         "los hay: en Morsa, 12 de 57 medidas salieron hasta 122 s "
                         "fuera. Lo que las delata es que implican un reloj de "
                         "camara absurdo.")
    ap.add_argument("--refine-window", type=float, default=3.0)
    ap.add_argument("--refine-min-prom", type=float, default=0.15)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_chromaprint_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    audio_rows = conn.execute(
        "SELECT id, filename, path FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND duration_sec >= 30",
        (args.audio_like,)
    ).fetchall()
    logging.info(f"Audios candidatos: {len(audio_rows)}")

    audio_fps = {}
    audio_dur = {}
    audio_path = {}
    for aid, afn, apath in audio_rows:
        fp = extract_fingerprint(Path(apath), max_duration=args.max_duration)
        if fp:
            audio_fps[aid] = (afn, fp)
            audio_dur[aid] = fp.duration
            audio_path[aid] = apath
    logging.info(f"Audios con fingerprint: {len(audio_fps)}")

    # A QUE VIDEOS APUNTAMOS.
    #
    # `--rehacer-derivados` existe porque el problema no son los videos SIN par:
    # son los que tienen par DERIVADO por reloj. El creation_time se guarda a
    # segundo entero, asi que un clip colocado por reloj no puede estar mejor
    # que +-0.5 s (medido: 311 ms de desviacion en una camara, 346 en la otra —
    # que es exactamente la firma del redondeo, sigma = 1/raiz(12) = 289 ms).
    # Solo la medicion por contenido baja de ahi.
    if args.rehacer_derivados:
        filtro = """AND c.id IN (
                      SELECT video_clip_id FROM audio_sync_pairs
                      GROUP BY video_clip_id
                      HAVING SUM(CASE WHEN method LIKE '%longwin%'
                                        OR method LIKE '%transcript%'
                                        OR method LIKE '%chromaprint%'
                                        OR method LIKE '%resolve-merge%'
                                      THEN 1 ELSE 0 END) = 0)"""
    elif args.only_missing:
        filtro = "AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)"
    else:
        filtro = ""
    video_rows = conn.execute(
        f"""SELECT c.id, c.filename, c.path, c.duration_sec, c.parent_folder,
                   c.creation_time FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id=c.id
            WHERE c.file_kind='video' AND c.index_status='ok' AND c.has_audio=1
              AND lower(c.rel_path) LIKE ?
              AND (d.category LIKE 'entrevista%' OR c.duration_sec >= ?)
              {filtro}
            GROUP BY c.id""",
        (f"%{args.location.lower()}%", args.min_dur)
    ).fetchall()
    logging.info(f"Videos candidatos: {len(video_rows)}")

    existing_pairs = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT video_clip_id, audio_clip_id FROM audio_sync_pairs"
        )
    )

    if args.refinar:
        from lib.sync_refiner import refine_offset

    # GUARDA DE COHERENCIA CON EL RELOJ DE LA CAMARA.
    #
    # La huella acustica encuentra un pico creible donde no lo hay: en Morsa,
    # 12 de 57 medidas salieron disparatadas, hasta 122 s. La mediana era
    # correcta —la calibracion es buena— pero el error individual no.
    #
    # Lo que las delata es que implican un reloj de camara absurdo. Cada camara
    # tiene un skew propio y ESTABLE (creation_time vs hora real); una medida
    # que lo contradiga por segundos no es un hallazgo, es un falso match. Aqui
    # se calcula ese skew con las medidas que YA existen y se rechaza lo que no
    # concuerde. Misma disciplina que import_iban_offsets: mejor sin sync y
    # dicho, que con sync y mentido.
    import statistics as _stats

    def _epoch(s):
        import datetime as _dt
        try:
            return _dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return None

    _wav_ini = {}
    for _aid, _mt, _du in conn.execute(
            "SELECT id, mtime, duration_sec FROM clips WHERE file_kind='audio'"):
        if _mt and _du:
            _wav_ini[_aid] = _mt - _du

    _skew_cam: dict[str, float] = {}
    _muestras: dict[str, list] = {}
    for _pf, _aid, _off, _ct in conn.execute(
            """SELECT v.parent_folder, p.audio_clip_id, p.offset_sec, v.creation_time
               FROM audio_sync_pairs p JOIN clips v ON v.id = p.video_clip_id
               WHERE p.method LIKE '%longwin%' OR p.method LIKE '%transcript%'"""):
        _t = _epoch(_ct)
        if _t is None or _aid not in _wav_ini:
            continue
        _muestras.setdefault(_pf, []).append(_wav_ini[_aid] - _off - _t)
    for _pf, _v in _muestras.items():
        if len(_v) >= 3:
            _skew_cam[_pf] = _stats.median(_v)
    if _skew_cam:
        logging.info("Skew de referencia por camara (de las medidas que ya habia):")
        for _pf, _s in sorted(_skew_cam.items()):
            logging.info(f"  {_pf}: {_s:+.2f}s  (n={len(_muestras[_pf])})")
    else:
        logging.info("Sin skew de referencia: la guarda de coherencia no puede "
                     "aplicarse y NO se escribira nada. Consigue primero unas "
                     "cuantas medidas por contenido.")

    def _coherente(vid_pf, aid, off, ct):
        """¿La medida implica un reloj de camara creible?"""
        ref = _skew_cam.get(vid_pf)
        if ref is None:
            return False, "sin skew de referencia para esta camara"
        t = _epoch(ct)
        if t is None or aid not in _wav_ini:
            return False, "sin reloj utilizable"
        implicado = _wav_ini[aid] - off - t
        d = abs(implicado - ref)
        if d > args.max_desvio:
            return False, f"implica skew {implicado:+.1f}s vs {ref:+.1f}s (fuera por {d:.1f}s)"
        return True, ""

    n_pairs = n_refinados = n_solo_ancla = n_sin_ancla = n_incoherente = 0
    for vid, vfn, vpath, vdur, vfolder, vct in video_rows:
        vfp = extract_fingerprint(Path(vpath), max_duration=args.max_duration)
        if vfp is None:
            n_sin_ancla += 1
            continue
        scored = []
        for aid, (afn, afp) in audio_fps.items():
            if (vid, aid) in existing_pairs and not args.rehacer_derivados:
                continue
            res = correlate_fingerprints(vfp, afp)
            if res is None:
                continue
            offset, conf = res
            if conf >= args.min_confidence:
                scored.append({"aid": aid, "afn": afn, "offset": offset, "conf": conf})
        if not scored:
            n_sin_ancla += 1
            continue
        scored.sort(key=lambda m: -m["conf"])
        kept = scored[:args.max_pairs_per_video]

        for rank, s in enumerate(kept, 1):
            ok, motivo = _coherente(vfolder, s["aid"], s["offset"], vct)
            if not ok:
                n_incoherente += 1
                logging.info(f"  ⊘ {vfn} -> {s['afn']}: {motivo} — descartado")
                continue
            metodo, off, conf = "chromaprint", s["offset"], s["conf"]
            nota = f"rank={rank} ancla=huella"

            # SEGUNDA ETAPA. La huella aguanta la musica pero su resolucion es
            # de decimas; la envolvente es fina pero necesita partir de cerca —
            # sola, sobre un concierto, se pierde. Encadenadas se cubren: la
            # huella acerca y la envolvente clava.
            if args.refinar:
                r = refine_offset(Path(vpath), Path(audio_path[s["aid"]]),
                                  s["offset"], vdur or 0, audio_dur.get(s["aid"], 0),
                                  window=args.refine_window)
                prom = 0.0
                if r is not None:
                    for tok in r.notes.split(","):
                        if "prominence=" in tok:
                            try:
                                prom = float(tok.split("=")[1])
                            except (ValueError, IndexError):
                                pass
                # Un pico pegado al borde de la ventana no es un pico: es que la
                # respuesta esta fuera y se devolvio lo mejor de dentro.
                en_el_borde = r is not None and abs(r.delta_sec) > args.refine_window * 0.9
                if r is not None and prom >= args.refine_min_prom and not en_el_borde:
                    off, conf = r.new_offset, max(conf, r.confidence)
                    metodo = "chromaprint-refinado"
                    nota += f" fino={r.delta_sec * 1000:+.0f}ms prom={prom:.2f}"
                    n_refinados += 1
                else:
                    motivo = ("sin refinar" if r is None
                              else "borde de ventana" if en_el_borde
                              else f"prom {prom:.2f}")
                    nota += f" ({motivo}: se queda el ancla)"
                    n_solo_ancla += 1

            if args.dry_run:
                logging.info(f"  [DRY] {vfn} -> {s['afn']} #{rank} "
                             f"{off:+.2f}s conf={conf:.2f} {metodo} · {nota}")
                n_pairs += 1
                continue

            # Si ya habia un par derivado para este (video, audio), se SUSTITUYE:
            # una medida siempre gana a una derivacion por reloj.
            cur = conn.execute(
                "SELECT id FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
                (vid, s["aid"])).fetchone()
            if cur:
                conn.execute(
                    "UPDATE audio_sync_pairs SET method=?, offset_sec=?, "
                    "confidence=?, notes=?, created_at=? WHERE id=?",
                    (metodo, off, conf, nota, time.time(), cur[0]))
            else:
                conn.execute(
                    "INSERT INTO audio_sync_pairs (video_clip_id, audio_clip_id, "
                    "method, offset_sec, confidence, notes, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (vid, s["aid"], metodo, off, conf, nota, time.time()))
            n_pairs += 1
            logging.info(f"  {vfn} -> {s['afn']} #{rank} {off:+.2f}s "
                         f"conf={conf:.2f} {metodo}")

    if not args.dry_run:
        conn.commit()
    conn.close()
    logging.info(f"\nsync_by_chromaprint: {n_pairs} pares"
                 + (" (DRY-RUN, no se escribio nada)" if args.dry_run else " escritos"))
    if args.refinar:
        logging.info(f"  refinados a frame: {n_refinados} | solo ancla gruesa: "
                     f"{n_solo_ancla} | sin ancla: {n_sin_ancla} | "
                     f"incoherentes con el reloj: {n_incoherente}")


if __name__ == "__main__":
    main()
