"""Transcript maestro: combina A1 (cámara) + lavaliers externos en una sola
secuencia temporal, ordenada en la escala del video.

Doctrina (Zezzions iter9, 2026-05-27): las preguntas del entrevistador se
escuchan mejor en el A1 de cámara (cámara cerca del entrevistador o
ambiente). Las respuestas del entrevistado vienen claras de su lavalier.
Si solo uso uno de los dos, pierdo la mitad de la conversación.

Convención de timestamps:
    Todo se lleva a la escala del VIDEO (video_t=0 al inicio del clip).
    - A1 word at t_v → t_v (ya está en esa escala)
    - audio word at t_a → t_v_equiv = t_a + offset
        (offset = audio_start - video_start; con offset<0, audio_t MAYOR que t_v_equiv)

    Verificación:
        offset = -96.6 (audio empezó 96.6s antes del video)
        audio_t = 96.6 → t_v_equiv = 96.6 + (-96.6) = 0 ✓
        audio_t = 100 → t_v_equiv = 3.4 ✓

Estructura del master:
    [
      {"t": 0.30, "w": "me", "src": "video"},
      {"t": 0.45, "w": "llamo", "src": "video"},
      {"t": 1.06, "w": "mariana", "src": "audio_dr_150"},
      ...
    ]

Las palabras del audio externo se INTERCALAN con las del A1 ordenadas por t.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


def load_words_with_source(tr_dir: Path, clip_id: int, source_tag: str
                           ) -> list[tuple[float, str, str]]:
    """Carga words y agrega source tag. Returns list of (t, word, source)."""
    p = tr_dir / f"{clip_id}.json"
    if not p.exists():
        return []
    try:
        d = json.load(p.open())
    except Exception:
        return []
    out = []
    for w in d.get("words", []) or []:
        if isinstance(w, list) and len(w) >= 2:
            tx = str(w[0]).strip()
            try:
                t = float(w[1])
            except (TypeError, ValueError):
                continue
            if tx:
                out.append((t, tx, source_tag))
    return out


def build_master_transcript(conn: sqlite3.Connection, tr_dir: Path,
                            video_id: int, prefer_lavalier_window: float = 0.5
                            ) -> Optional[dict]:
    """Construye master transcript para un video combinando A1 + lavaliers sincronizados.

    Args:
        conn: sqlite3 connection.
        tr_dir: directorio de transcripts (cada {clip_id}.json).
        video_id: clip_id del video.
        prefer_lavalier_window: si dos palabras IGUALES (case-insensitive) del A1 y
            de un lavalier están dentro de esta ventana de tiempo (seg), preferir
            la del lavalier (mejor calidad). Default 0.5s.

    Returns:
        dict con:
          {
            "video_id": int,
            "sources": ["video_a1", "audio_<id>", ...],
            "words": [[word, t, src], ...],   # ordenadas por t
            "text": str  # texto concatenado para preview
          }
        O None si no hay datos.
    """
    # 1. A1 del video
    a1_words = load_words_with_source(tr_dir, video_id, "video_a1")

    # 2. Lavaliers sincronizados con este video
    sync_pairs = conn.execute("""
        SELECT sp.audio_clip_id, sp.offset_sec, ca.filename, ca.rel_path
        FROM audio_sync_pairs sp
        JOIN clips ca ON ca.id=sp.audio_clip_id
        WHERE sp.video_clip_id=?
    """, (video_id,)).fetchall()

    audio_words_per_source: dict[str, list[tuple[float, str, str]]] = {}
    for aid, offset, afn, arel in sync_pairs:
        src_tag = f"audio_{aid}"  # ej "audio_146"
        aw = load_words_with_source(tr_dir, aid, src_tag)
        if not aw:
            continue
        # Convertir cada t_a a t_v_equiv = t_a + offset
        aw_in_video_scale = [
            (t + offset, w, src) for (t, w, src) in aw
        ]
        audio_words_per_source[src_tag] = aw_in_video_scale

    if not a1_words and not audio_words_per_source:
        return None

    # 3. Combinar todas las palabras
    all_words = list(a1_words)
    for src, aw in audio_words_per_source.items():
        all_words.extend(aw)

    # 4. Detectar duplicados redundantes: si A1 y lavalier dicen la misma palabra
    #    en aprox el mismo instante, preferir el del lavalier (mejor SNR).
    #    Marcamos las del A1 como "skip" para no perderlas pero saber que hay
    #    mejor versión.
    # Sort por tiempo
    all_words.sort(key=lambda x: x[0])

    # Detectar duplicados case-insensitive en ventana
    n = len(all_words)
    skip_marks = [False] * n
    norm = lambda w: w.lower().strip(".,?¿!¡;:'\"-")
    for i in range(n):
        if skip_marks[i]:
            continue
        t_i, w_i, s_i = all_words[i]
        wn_i = norm(w_i)
        if not wn_i:
            continue
        # Si esta palabra es del A1, buscar version igual en lavalier en ventana
        if s_i == "video_a1":
            for j in range(i+1, min(n, i+20)):
                t_j, w_j, s_j = all_words[j]
                if t_j - t_i > prefer_lavalier_window:
                    break
                if s_j != "video_a1" and norm(w_j) == wn_i:
                    skip_marks[i] = True  # A1 cede al lavalier
                    break

    # 5. Filtrar duplicados
    final_words = [
        (round(t, 3), w, src) for i, (t, w, src) in enumerate(all_words)
        if not skip_marks[i]
    ]

    # 6. Filter: solo palabras en rango razonable
    # (descartar palabras de audio externo cuyo t_v_equiv esté FUERA del video
    # — esas son zonas donde el audio sigue pero el video ya acabó)
    v_dur = conn.execute(
        "SELECT duration_sec FROM clips WHERE id=?", (video_id,)
    ).fetchone()
    if v_dur and v_dur[0]:
        vd = float(v_dur[0])
        final_words = [(t, w, src) for (t, w, src) in final_words
                       if -1.0 <= t <= vd + 1.0]

    sources_used = ["video_a1"] + sorted(audio_words_per_source.keys())
    text = " ".join(w for (_, w, _) in final_words)

    # CRÍTICO (iter9.1): preservar texto + words POR SOURCE para detección
    # independiente de preguntas. Concatenar todas las fuentes en un solo
    # texto MEZCLA preguntas con respuestas, generando basura.
    by_source: dict[str, dict] = {}
    a1_in_video_scale = [(t, w, src) for (t, w, src) in a1_words]
    for src_name, words_list in [
        ("video_a1", a1_in_video_scale),
        *list(audio_words_per_source.items())
    ]:
        text_src = " ".join(w for (_, w, _) in words_list)
        by_source[src_name] = {
            "n_words": len(words_list),
            "text": text_src[:50000],
            "words": [[w, t] for (t, w, _) in words_list],  # formato standard [w, t]
        }

    return {
        "video_id": video_id,
        "sources": sources_used,
        "n_words_total": len(final_words),
        "n_sync_audios": len(audio_words_per_source),
        "words": [[w, t, src] for (t, w, src) in final_words],
        "text": text[:50000],
        # Nuevo: text + words por fuente individual (iter9.1)
        "by_source": by_source,
    }


def save_master_transcript(tr_dir: Path, video_id: int, master: dict) -> Path:
    """Guarda master transcript a transcripts/master_{video_id}.json"""
    out_path = tr_dir / f"master_{video_id}.json"
    with out_path.open("w") as f:
        json.dump(master, f, ensure_ascii=False, indent=None)
    return out_path


def load_master_or_fallback(tr_dir: Path, video_id: int) -> Optional[dict]:
    """Carga master_{video_id}.json si existe, sino {video_id}.json normal."""
    master_path = tr_dir / f"master_{video_id}.json"
    if master_path.exists():
        try:
            return json.load(master_path.open())
        except Exception:
            pass
    plain_path = tr_dir / f"{video_id}.json"
    if plain_path.exists():
        try:
            d = json.load(plain_path.open())
            # Normalizar al formato master
            return {
                "video_id": video_id,
                "sources": ["video_a1"],
                "words": [[w[0], w[1], "video_a1"] for w in d.get("words", []) or []
                          if isinstance(w, list) and len(w) >= 2],
                "text": d.get("text", "")
            }
        except Exception:
            pass
    return None
