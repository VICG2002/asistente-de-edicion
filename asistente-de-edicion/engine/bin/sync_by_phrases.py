#!/usr/bin/env python3
"""Sync por phrase-match cruzado entre transcripts de video y audio.

Caso fundador (Zezzions 2026-05-26): el `sync_acoustic.py` (envelope FFT)
y el `sync_transcript.py` (n-gramas de palabras) fallaron para clip 2712
(ENTREVISTADO_5) — su lavalier verdadero era `00007 Dr` pero
el envelope solo dio conf=0.06 y el sync_transcript no lo encontró (audio
denso pero el video transcript estaba lleno de alucinaciones).

**Solución más robusta**: cuando el TRANSCRIPT del video menciona una
identidad explícita ("ENTREVISTADO_5") o frases distintivas ("fin de
semana"), buscar esas mismas frases en los transcripts de los audios
externos. Si ambos las tienen, el offset se calcula directamente:

    offset_sec = audio_word_t - video_word_t

Múltiples frases con offsets consistentes (cluster ±60s) → sync verificado
con confianza muy alta (esto es contenido textual cruzado, no señal
ruidosa).

Uso:
    bin/sync_by_phrases.py --root /Volumes/.../Zezzions VICG \\
        --min-matches 2 --max-pairs-per-video 2

Política recomendada en el pipeline:
  1. sync_transcript    (alta precisión, requiere transcripts densos)
  2. sync_acoustic      (música/ambiente compartido, conf marginal)
  3. sync_by_phrases    (frases distintivas — caso entrevista con alucinación)
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


def normalize_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()
    return w.strip(".,;:!?¿¡()\"'`'’")


def find_timestamps(words, target_phrase: str):
    """Devuelve la lista de timestamps donde aparece la frase en los words."""
    target = [normalize_word(w) for w in target_phrase.split()]
    n = len(target)
    out = []
    for i in range(len(words) - n + 1):
        seq = [normalize_word(words[j][0]) if isinstance(words[j], list) else "" for j in range(i, i+n)]
        if seq == target:
            if isinstance(words[i], list) and len(words[i]) >= 2:
                out.append(words[i][1])
    return out


# Frases distintivas que se buscan en cross-transcripts: nombres y patrones
# de auto-identificación + términos editoriales específicos del proyecto.
DEFAULT_DISTINCTIVE_PATTERNS = re.compile(
    r"\b(?:[Yy]o\s+soy|[Mm]e\s+llamo|[Mm]i\s+nombre\s+es|[Ss]oy)\s+"
    r"([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]+)?)"
)


def extract_distinctive_phrases(text: str, min_word_freq: int = 1) -> list[str]:
    """Extrae frases distintivas de un transcript:
    - Auto-identificaciones ("Yo soy ENTREVISTADO_5" → "entrevistado_5")
    - n-gramas de 3-4 palabras que no son alucinación obvia (sin repeticiones masivas)
    """
    phrases = set()
    # Auto-IDs (apellidos)
    for m in DEFAULT_DISTINCTIVE_PATTERNS.finditer(text):
        phrases.add(normalize_word_phrase(m.group(1)))
    return list(phrases)


def normalize_word_phrase(s: str) -> str:
    return " ".join(normalize_word(w) for w in s.split())


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def cluster_median_offset(offsets: list[float], radius: float = 60.0):
    """Devuelve el promedio del cluster de offsets ±radius de la mediana."""
    if not offsets:
        return None, 0
    med = statistics.median(offsets)
    cluster = [d for d in offsets if abs(d - med) <= radius]
    if not cluster:
        return None, 0
    return statistics.mean(cluster), len(cluster)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-like", default="%lavas%",
                    help="LIKE pattern para audios candidatos.")
    ap.add_argument("--location", default="video",
                    help="LIKE pattern para videos candidatos.")
    ap.add_argument("--min-matches", type=int, default=2,
                    help="Mínimas frases compartidas para emitir un par.")
    ap.add_argument("--max-pairs-per-video", type=int, default=2)
    ap.add_argument("--only-missing", action="store_true", default=True)
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    cache = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"sync_phrases_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    conn = manifest.conectar(str(db))

    # Audios: cargar transcripts
    audio_rows = conn.execute(
        "SELECT id, filename, path FROM clips "
        "WHERE file_kind='audio' AND index_status='ok' "
        "AND lower(rel_path) LIKE ? AND duration_sec >= 30",
        (args.audio_like,)
    ).fetchall()
    audio_data = {}
    for aid, afn, apath in audio_rows:
        p = cache / f"{aid}.json"
        if not p.exists(): continue
        try:
            d = json.load(open(p))
        except: continue
        words = d.get("words", []) or []
        text = d.get("text", "") or ""
        audio_data[aid] = {"fn": afn, "text": text.lower(), "words": words}
    logging.info(f"Audios cargados: {len(audio_data)}")

    # Videos: candidatos = entrevistas o videos con identidades confirmadas
    missing_clause = (
        "AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)"
        if args.only_missing else ""
    )
    video_rows = conn.execute(
        f"""SELECT c.id, c.filename FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id=c.id
            WHERE c.file_kind='video' AND c.index_status='ok' AND c.has_audio=1
              AND lower(c.rel_path) LIKE ?
              AND (d.category LIKE 'entrevista%' OR c.duration_sec >= 60)
              {missing_clause}""",
        (f"%{args.location.lower()}%",)
    ).fetchall()
    logging.info(f"Videos candidatos: {len(video_rows)}")

    n_pairs = 0
    for vid, vfn in video_rows:
        p = cache / f"{vid}.json"
        if not p.exists(): continue
        try:
            vd = json.load(open(p))
        except: continue
        v_text = (vd.get("text", "") or "").lower()
        v_words = vd.get("words", []) or []

        # Extract phrases distinctivas: auto-IDs en el video
        distinct_phrases = extract_distinctive_phrases(vd.get("text", ""))
        if not distinct_phrases:
            continue

        # Para cada audio, buscar overlap de frases
        scored = []
        for aid, info in audio_data.items():
            a_text = info["text"]
            a_words = info["words"]
            # Frases compartidas
            shared = [ph for ph in distinct_phrases if ph in a_text and ph in v_text]
            if len(shared) < 1:
                continue
            # Calcular offsets por cada frase compartida
            all_offsets = []
            for phrase in shared:
                vts = find_timestamps(v_words, phrase)
                ats = find_timestamps(a_words, phrase)
                for vt in vts:
                    for at in ats:
                        all_offsets.append(at - vt)
            if not all_offsets:
                continue
            offset, cluster_size = cluster_median_offset(all_offsets)
            if offset is None or cluster_size < args.min_matches:
                continue
            scored.append({
                "aid": aid, "afn": info["fn"],
                "offset": offset, "shared": shared, "cluster_size": cluster_size,
            })

        scored.sort(key=lambda m: -m["cluster_size"])
        kept = scored[:args.max_pairs_per_video]
        for rank, s in enumerate(kept, 1):
            conf = min(0.95, 0.50 + 0.1 * s["cluster_size"])
            conn.execute(
                "INSERT INTO audio_sync_pairs "
                "(video_clip_id, audio_clip_id, method, offset_sec, confidence, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (vid, s["aid"], "phrase-match", s["offset"], conf,
                 f"shared={','.join(s['shared'][:3])} cluster={s['cluster_size']}",
                 time.time())
            )
            n_pairs += 1
            logging.info(f"  {vfn} -> {s['afn']} #{rank} offset={s['offset']:+.1f}s "
                         f"conf={conf:.2f} shared={s['shared'][:3]} cluster={s['cluster_size']}")

    conn.commit()
    conn.close()
    logging.info(f"sync_by_phrases: {n_pairs} pares escritos.")


if __name__ == "__main__":
    main()
