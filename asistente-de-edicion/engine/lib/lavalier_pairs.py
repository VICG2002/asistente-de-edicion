"""Detección de pares de lavaliers hermanos (Dr ↔ Izq) que comparten contenido.

Caso fundador (Zezzions 2026-05-26, idea del usuario): si un lavalier está
bien sincronizado con un video, su hermano (mismo número, otro canal)
puede DERIVAR su sync automáticamente cuando ambos capturan el mismo
contenido sonoro.

Detección de pares aplicables:
  1. Mismo nombre base (`00006_Wireless PRO.WAV` en Dr y Izq).
  2. Transcript 4-grama overlap ≥ 50% (contenido compartido).
  3. Cross-correlate envelope log-RMS → prominence ≥ 0.40 (peak claro).
  4. Voice embedding similarity speaker dominante ≥ 0.85 (mismo speaker).

Si los 4 criterios pasan: `applicable=True` con `delta_sec` = lag entre
los dos lavaliers (cuánto se desplaza Izq respecto a Dr).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


# Umbrales empíricos calibrados con Zezzions 2026-05-26 (música ambient).
# Originalmente 0.50/0.40/0.85 era demasiado estricto — los lavaliers
# legítimos (00003 Dr/Izq de ENTREVISTADO_3) daban 0.33/0.31/0.73, fallaban los 3.
# Combinación: score = 0.4*overlap + 0.3*prom + 0.3*vsim ≥ 0.40 → aplicable.
MIN_4GRAM_OVERLAP = 0.20      # antes 0.50
MIN_ENVELOPE_PROMINENCE = 0.20  # antes 0.40
MIN_VOICE_SIM = 0.65          # antes 0.85
MIN_COMBINED_SCORE = 0.40


def _norm_words(text: str) -> list[str]:
    import re, unicodedata
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return text.split()


def fourgram_overlap(text_a: str, text_b: str) -> float:
    """Fracción de 4-gramas de text_a que también aparecen en text_b."""
    wa = _norm_words(text_a)
    wb = _norm_words(text_b)
    if len(wa) < 4 or len(wb) < 4:
        return 0.0
    gr_a = set(" ".join(wa[i:i+4]) for i in range(len(wa) - 3))
    gr_b = set(" ".join(wb[i:i+4]) for i in range(len(wb) - 3))
    if not gr_a:
        return 0.0
    return len(gr_a & gr_b) / len(gr_a)


def envelope_lag(path_a: Path, path_b: Path,
                 analysis_dur: float = 60.0):
    """Cross-correlate envelope log-RMS banda voz entre dos grabaciones hermanas.

    Returns (delta_sec, prominence) o None.

    CONVENCION, con la misma forma que la del motor para video↔audio:

        delta = b_start - a_start        (negativo => b empezo antes que a)
        t_b = t_a - delta

    Es antisimetrica: `envelope_lag(x, y)` == `-envelope_lag(y, x)`.

    OJO (2026-08-13). El docstring anterior decia dos cosas incompatibles a la
    vez ("Izq empezo delta antes que Dr" y "audio_t_dr = audio_t_izq + delta"),
    igual que pasaba en sync_refiner. Pero medido contra tests/media_prueba —dos
    grabaciones de la misma señal con 5.00 s de desfase conocido— el problema
    resulto no ser el signo: devolvia 2.0 con prominence 0.008. La correlacion
    iba sobre las envolventes CRUDAS, y una envolvente RMS es siempre positiva,
    asi que el termino de continua domina y la correlacion de solape triangular
    empuja el pico hacia el centro. El signo era correcto; la magnitud no.
    """
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from lib.sync_refiner import _load_pcm, _envelope, WORK_SR
    from scipy.signal import correlate

    pcm_a = _load_pcm(path_a, 0.0, analysis_dur, voice_bandpass=True)
    pcm_b = _load_pcm(path_b, 0.0, analysis_dur, voice_bandpass=True)
    if pcm_a is None or pcm_b is None:
        return None
    env_a = _envelope(pcm_a, sr=WORK_SR, hop_ms=10)
    env_b = _envelope(pcm_b, sr=WORK_SR, hop_ms=10)
    if env_a.size < 100 or env_b.size < 100:
        return None
    ENV_SR = 100

    # Restar la media ANTES de correlacionar. Sin esto el pico se va al centro:
    # la envolvente RMS no tiene valores negativos, asi que su continua pesa mas
    # que la estructura del habla, y lo que se acaba midiendo es cuanto se
    # solapan las dos ventanas, no donde coinciden.
    a = env_a.astype(np.float64)
    b = env_b.astype(np.float64)
    a -= a.mean()
    b -= b.mean()

    # AGUJA Y PAJAR, en vez de correlacion 'full'. Con 'full' cada lag solapa una
    # cantidad distinta de muestras y hay que normalizar por eso; los lags de los
    # extremos, con poquisimo solape, dan coeficientes altisimos por puro ruido y
    # se llevan el pico. Exigiendo que la aguja quepa ENTERA dentro del pajar, el
    # solape es constante y el problema desaparece. Es lo que ya hacia bien
    # `lib/waveform_sync.correlate`, de donde sale este esquema.
    #
    # La aguja es la mitad central de A: deja margen para deslizar a ambos lados
    # aunque las dos ventanas midan lo mismo, que es el caso normal (a las dos se
    # les pide `analysis_dur`).
    n_b = len(b)
    m = min(len(a), n_b // 2)
    if m < 16 or n_b < 32:
        return None
    ini = (len(a) - m) // 2
    aguja = a[ini:ini + m]

    na = float(np.sqrt(np.dot(aguja, aguja)))
    if na <= 0.0:
        return None

    size = 1 << int(np.ceil(np.log2(n_b + m)))
    corr = np.fft.irfft(
        np.fft.rfft(b, size) * np.conj(np.fft.rfft(aguja, size)), size)
    max_lag = n_b - m
    corr = corr[:max_lag + 1]

    # Energia de la ventana deslizante del pajar: hace el score independiente
    # del nivel de grabacion, que entre dos lavaliers distintos nunca es igual.
    cs = np.concatenate(([0.0], np.cumsum(b * b)))
    win_energy = cs[m:m + max_lag + 1] - cs[0:max_lag + 1]
    ncc = corr / (na * np.sqrt(np.maximum(win_energy, 1e-12)))

    k = int(np.argmax(ncc))
    # La aguja empieza en `ini` dentro de A y aparece en `k` dentro de B:
    #     a_start + ini/SR == b_start + k/SR
    # => delta = b_start - a_start = (ini - k)/SR
    delta_sec = (ini - k) / ENV_SR

    # Prominence sobre la correlacion ya normalizada, excluyendo 200 ms a cada
    # lado del pico para no compararlo consigo mismo.
    peak_val = float(ncc[k])
    half = max(int(0.2 * ENV_SR), 5)
    mask = np.ones_like(ncc, dtype=bool)
    mask[max(0, k - half):k + half + 1] = False
    if mask.any() and peak_val > 0:
        runner_up = float(np.max(ncc[mask]))
        prom = (peak_val - runner_up) / peak_val
    else:
        prom = 0.0
    return float(delta_sec), float(np.clip(prom, 0.0, 1.0))


def voice_sim_dominant_speakers(conn: sqlite3.Connection,
                                audio_a_id: int, audio_b_id: int) -> float:
    """Cosine similarity entre los voice embeddings de los speakers
    dominantes de ambos audios."""
    def get_dominant_emb(aid):
        row = conn.execute(
            "SELECT embedding FROM audio_speakers "
            "WHERE clip_id=? AND embedding IS NOT NULL "
            "ORDER BY duration_sec DESC LIMIT 1", (aid,)
        ).fetchone()
        if not row or not row[0]:
            return None
        arr = np.frombuffer(row[0], dtype=np.float32)
        n = np.linalg.norm(arr)
        return arr / n if n > 1e-9 else None
    a = get_dominant_emb(audio_a_id)
    b = get_dominant_emb(audio_b_id)
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


def detect_pairs(conn: sqlite3.Connection,
                 tr_dir: Path,
                 log: Optional[logging.Logger] = None) -> list[dict]:
    """Detecta TODOS los pares Dr↔Izq con mismo nombre base.

    Returns list of dicts con campos:
      audio_a_id, audio_b_id, name_base,
      overlap_4gram, delta_sec, prominence, voice_sim, applicable
    """
    log = log or logging.getLogger(__name__)
    # Agrupar audios por basename
    rows = conn.execute(
        "SELECT id, filename, rel_path, path FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' AND duration_sec >= 30"
    ).fetchall()
    by_basename: dict[str, list[tuple]] = {}
    for cid, fn, rel, path in rows:
        by_basename.setdefault(fn, []).append((cid, rel, path))

    pairs: list[dict] = []
    for basename, audios in by_basename.items():
        if len(audios) != 2:
            continue
        # Determinar Dr y Izq
        audio_a, audio_b = audios
        # Cargar transcripts
        def load_text(cid):
            tp = tr_dir / f"{cid}.json"
            if not tp.exists():
                return ""
            try:
                with tp.open() as f:
                    j = json.load(f)
                return j.get("text", "") or ""
            except Exception:
                return ""
        text_a = load_text(audio_a[0])
        text_b = load_text(audio_b[0])
        overlap = fourgram_overlap(text_a[:4000], text_b[:4000])

        # Envelope lag (sólo si overlap razonable)
        delta = None
        prom = 0.0
        if overlap >= 0.3:
            res = envelope_lag(Path(audio_a[2]), Path(audio_b[2]))
            if res:
                delta, prom = res

        # Voice sim (speakers dominantes)
        vsim = voice_sim_dominant_speakers(conn, audio_a[0], audio_b[0])

        # Score combinado (más permisivo que AND estricto)
        combined = (
            0.4 * overlap +
            0.3 * (prom if delta is not None else 0.0) +
            0.3 * vsim
        )
        applicable = (
            delta is not None
            and combined >= MIN_COMBINED_SCORE
            # Cada componente individual debe pasar al menos el mínimo bajo
            and overlap >= MIN_4GRAM_OVERLAP
            and prom >= MIN_ENVELOPE_PROMINENCE
            and vsim >= MIN_VOICE_SIM
        )
        result = {
            "audio_a_id": audio_a[0],
            "audio_b_id": audio_b[0],
            "name_base": basename,
            "overlap_4gram": overlap,
            "delta_sec": delta if delta is not None else 0.0,
            "prominence": prom,
            "voice_sim": vsim,
            "applicable": applicable,
        }
        pairs.append(result)
        log.info(f"  {basename}: overlap={overlap:.2f}, delta={result['delta_sec']:+.2f}s, "
                 f"prom={prom:.2f}, vsim={vsim:.2f}, applicable={applicable}")
    return pairs


def ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lavalier_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_a_id INTEGER NOT NULL,
            audio_b_id INTEGER NOT NULL,
            name_base TEXT,
            overlap_4gram REAL,
            delta_sec REAL,
            prominence REAL,
            voice_sim REAL,
            applicable INTEGER,
            created_at REAL,
            FOREIGN KEY (audio_a_id) REFERENCES clips(id),
            FOREIGN KEY (audio_b_id) REFERENCES clips(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lp_a ON lavalier_pairs(audio_a_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lp_b ON lavalier_pairs(audio_b_id)")
    conn.commit()


def derive_sibling_sync(conn: sqlite3.Connection,
                        log: Optional[logging.Logger] = None) -> int:
    """Para cada lavalier_pairs.applicable=1, si uno de los audios tiene
    sync_pair pero el hermano no, derivar el sync del hermano.

    Returns número de pairs derivados.
    """
    import time
    log = log or logging.getLogger(__name__)
    pairs = conn.execute(
        "SELECT audio_a_id, audio_b_id, delta_sec, voice_sim FROM lavalier_pairs "
        "WHERE applicable=1"
    ).fetchall()
    now = time.time()
    n_derived = 0
    for aid_a, aid_b, delta, vsim in pairs:
        # Convención: cross-correlate(env_b, env_a) con peak en delta_samples
        # significa que env_b está delayed delta_samples respecto a env_a.
        # En tiempo: audio_b_start = audio_a_start + delta_sec.
        # Para un video V syncado con audio_a (offset_a):
        #   offset_a = audio_a_start - V_start
        #   offset_b = audio_b_start - V_start = offset_a + delta_sec
        for src_id, dst_id, sign in [(aid_a, aid_b, +1), (aid_b, aid_a, -1)]:
            # Buscar videos donde src tiene sync, dst no
            src_pairs = conn.execute("""
                SELECT id, video_clip_id, offset_sec, confidence, method, identity_score
                FROM audio_sync_pairs WHERE audio_clip_id=?
            """, (src_id,)).fetchall()
            for sp_id, vid, src_off, src_conf, src_method, src_idscore in src_pairs:
                # ¿Ya tiene dst syncado con este video?
                existing = conn.execute(
                    "SELECT id FROM audio_sync_pairs WHERE video_clip_id=? AND audio_clip_id=?",
                    (vid, dst_id)
                ).fetchone()
                if existing:
                    continue
                derived_off = src_off + sign * delta
                # Validar plausible: ¿el video y dst se solapan?
                dst_dur = conn.execute(
                    "SELECT duration_sec FROM clips WHERE id=?", (dst_id,)
                ).fetchone()[0]
                v_dur = conn.execute(
                    "SELECT duration_sec FROM clips WHERE id=?", (vid,)
                ).fetchone()[0]
                # audio_t at video_t=0 = -derived_off. Si negativo y |derived_off| > dst_dur, no overlap.
                # if derived_off < -v_dur or derived_off > dst_dur: skip
                if abs(derived_off) > max(v_dur, dst_dur) + 30:
                    continue
                # Escribir
                notes = (f"derived from audio={src_id} (offset={src_off:+.2f}) "
                         f"+ delta_sec={sign*delta:+.2f} (sibling lavalier, vsim={vsim:.2f})")
                conn.execute("""
                    INSERT INTO audio_sync_pairs
                    (video_clip_id, audio_clip_id, method, offset_sec, confidence,
                     notes, created_at, identity_score)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (vid, dst_id, "lavalier-derived-locked", derived_off,
                      max(0.65, src_conf * 0.9), notes, now,
                      src_idscore or 0.85))
                log.info(f"  derived sync: video={vid} audio_dst={dst_id} "
                         f"off={derived_off:+.2f} (de src={src_id} off={src_off:+.2f})")
                n_derived += 1
    conn.commit()
    return n_derived


if __name__ == "__main__":
    import argparse, glob
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--derive", action="store_true",
                    help="Derivar sync de hermanos tras detectar pares")
    args = ap.parse_args()

    p = Path(args.root)
    if not p.exists():
        m = glob.glob(args.root + "*")
        if len(m) == 1: p = Path(m[0])
    db = p / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = p / ".cinema_assistant" / "transcripts"

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = manifest.conectar(str(db))
    ensure_schema(conn)

    print("=== Detectando pares de lavaliers ===")
    pairs = detect_pairs(conn, tr_dir)
    conn.execute("DELETE FROM lavalier_pairs")
    import time
    now = time.time()
    for p_r in pairs:
        conn.execute("""
            INSERT INTO lavalier_pairs
            (audio_a_id, audio_b_id, name_base, overlap_4gram, delta_sec,
             prominence, voice_sim, applicable, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (p_r["audio_a_id"], p_r["audio_b_id"], p_r["name_base"],
              p_r["overlap_4gram"], p_r["delta_sec"], p_r["prominence"],
              p_r["voice_sim"], int(p_r["applicable"]), now))
    conn.commit()
    n_applic = sum(1 for p_r in pairs if p_r["applicable"])
    print(f"\nPares detectados: {len(pairs)}, aplicables: {n_applic}")

    if args.derive:
        print("\n=== Derivando sync de hermanos ===")
        n = derive_sibling_sync(conn)
        print(f"\nSyncs derivados: {n}")
    conn.close()
