#!/usr/bin/env python3
"""Re-transcribe audios externos con prompt contextual.

Doctrina (Zezzions iter9.3, 2026-05-27): Whisper transcribe DRAMÁTICAMENTE
mejor con `--prompt` que dé contexto del dominio. Errores típicos sin contexto:
  - "Vici" en lugar de "Avicii"
  - "boro" en lugar de "porro"
  - "tornar a esa pelota" → frase sin sentido
  - Nombres propios mal escritos
  - Mexicanismos perdidos

Strategy:
  1. Construir contexto por audio basado en el video sincronizado:
     - Cast (nombres del entrevistado/entrevistador)
     - Categoría / tema del clip
     - Vocabulario del proyecto (jerga musical, mexicanismos, lugares)
  2. Re-transcribir con whisper-cli --prompt
  3. Guardar como transcripts/{clip_id}.json (sobrescribe el viejo)
  4. Snapshot del viejo en transcripts/legacy_pre_context_{clip_id}.json

Doctrina: `~/memoria-asistente-edicion/lecciones/patrones-exitosos.md`

Uso:
    bin/retranscribe_with_context.py --root <disk>
    bin/retranscribe_with_context.py --root <disk> --audio-ids 158
    bin/retranscribe_with_context.py --root <disk> --only-interviews-sync
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import manifest  # noqa: E402


# Vocabulario base del proyecto Zezzions (música electrónica + entrevista)
# Se concatena con cast específico del video.
DEFAULT_PROJECT_VOCABULARY = (
    "Entrevista en evento de música electrónica Sessions. "
    "Vocabulario común: música electrónica, IDM, EDM, productor, DJ, "
    "Avicii, Daft Punk, Aphex Twin, Tomorrowland, Sessions, evento. "
    "Mexicanismos: güey, wey, chido, neta, cabrón, porro, banda, ahorita, chingón."
)


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def get_context_for_video(conn: sqlite3.Connection, video_id: int,
                           extra_vocab: str = "") -> str:
    """Construye prompt contextual basado en cast + categoría + curated full_text.

    Filtros importantes:
      - NO usar clip_descriptions.description (puede tener debug info como
        "distinct=409 clean_words=936" que confunde a Whisper)
      - SÍ usar clip_curated_segments.full_text si formato canónico
        (PERSONAJES | PLANO | ACCIÓN. Idea: 'FRASE') porque es contenido limpio
    """
    # 1. Personajes
    chars_row = conn.execute(
        "SELECT characters FROM clip_characters WHERE clip_id=?", (video_id,)
    ).fetchone()
    chars = (chars_row[0] if chars_row else "") or ""

    # 2. Solo categoría (no description, que puede tener metadata sucia)
    cat_row = conn.execute(
        "SELECT category FROM clip_descriptions WHERE clip_id=?", (video_id,)
    ).fetchone()
    category = cat_row[0] if cat_row else ""

    # 3. full_text del curated_segments (solo si curated_by='claude' = formato canónico)
    full_text_row = conn.execute("""
        SELECT full_text FROM clip_curated_segments
        WHERE clip_id=? AND curated_by='claude' AND full_text != ''
        ORDER BY is_representative DESC LIMIT 1
    """, (video_id,)).fetchone()
    full_text = full_text_row[0] if full_text_row else ""

    parts = []
    if category:
        parts.append(f"Tipo: {category}.")
    if chars:
        clean_chars = chars.replace("*", "").replace("(~", "(").strip()
        parts.append(f"Personajes: {clean_chars}.")
    if full_text:
        # Extraer solo personajes y acción del formato canónico
        # "{PERSONAJES} | {PLANO} | {ACCIÓN}. {LUGAR}. Idea: '{FRASE}'"
        # Tomamos la sección de acción/lugar como contexto
        segments = full_text.split("|", 2)
        if len(segments) >= 3:
            action_part = segments[2].strip()
            # Truncar
            if len(action_part) > 200:
                action_part = action_part[:200].rsplit(" ", 1)[0]
            parts.append(f"Tema: {action_part}")

    project_vocab = extra_vocab or DEFAULT_PROJECT_VOCABULARY
    parts.append(project_vocab)

    full_prompt = " ".join(parts)
    # Whisper.cpp prompt límite: n_text_ctx/2 ≈ 224 tokens ≈ 800 chars
    if len(full_prompt) > 800:
        full_prompt = full_prompt[:800]
    return full_prompt


def get_audios_for_retranscription(conn: sqlite3.Connection,
                                     only_interviews_sync: bool = False,
                                     audio_ids: list[int] = None) -> list[tuple]:
    """Audios candidatos: los sincronizados con entrevistas (lavaliers).
    Returns list of (audio_id, video_id, audio_path, audio_filename).
    """
    if audio_ids:
        # Forzar específicos
        rows = []
        for aid in audio_ids:
            r = conn.execute("""
                SELECT ca.id, sp.video_clip_id, ca.path, ca.filename
                FROM audio_sync_pairs sp
                JOIN clips ca ON ca.id=sp.audio_clip_id
                WHERE ca.id=?
                LIMIT 1
            """, (aid,)).fetchone()
            if r:
                rows.append(r)
        return rows

    q = """
        SELECT DISTINCT ca.id, sp.video_clip_id, ca.path, ca.filename
        FROM audio_sync_pairs sp
        JOIN clips ca ON ca.id=sp.audio_clip_id
        JOIN clips v ON v.id=sp.video_clip_id
    """
    if only_interviews_sync:
        q += """
        WHERE EXISTS (
            SELECT 1 FROM clip_descriptions d
            WHERE d.clip_id=v.id AND d.category LIKE 'entrevista%'
        )
        """
    return conn.execute(q).fetchall()


def run_whisper_with_prompt(audio_path: Path, model_path: Path, prompt: str,
                              language: str = "es") -> dict | None:
    """Corre whisper-cli con --prompt y devuelve el JSON parseado.
    Returns dict con {text, words} en el formato estándar del motor.
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Whisper.cpp escribe a stdout con --output-json + --print-progress 0
        out_json = td / "out.json"
        # Convertir a WAV 16kHz mono si necesario (whisper-cli requiere ese formato)
        wav_16k = td / "in_16k.wav"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(audio_path),
                "-ac", "1", "-ar", "16000",
                str(wav_16k)
            ], check=True, timeout=300)
        except Exception as e:
            logging.error(f"ffmpeg fallo: {e}")
            return None

        cmd = [
            "whisper-cli",
            "-m", str(model_path),
            "-l", language,
            "--prompt", prompt,
            "--output-json",
            "-of", str(out_json.with_suffix("")),  # whisper-cli añade .json
            "--print-progress", "0",
            str(wav_16k),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
        except subprocess.TimeoutExpired:
            logging.error(f"whisper-cli timeout sobre {audio_path}")
            return None
        if result.returncode != 0:
            logging.error(f"whisper-cli error: {result.stderr[-500:]}")
            return None
        # Leer JSON
        json_path = out_json.with_suffix(".json")
        if not json_path.exists():
            # Buscar cualquier *.json en el dir
            candidates = list(td.glob("*.json"))
            if candidates:
                json_path = candidates[0]
            else:
                logging.error("whisper-cli no produjo JSON output")
                return None
        try:
            d = json.load(json_path.open())
        except Exception as e:
            logging.error(f"parse JSON fallo: {e}")
            return None

        # Whisper.cpp JSON: { transcription: [{timestamps: {from,to}, text, ...}], ...}
        # Convertir al formato standard del motor: {text: str, words: [[w, t], ...]}
        text_parts = []
        words = []
        segments = d.get("transcription", []) or d.get("segments", []) or []
        for seg in segments:
            seg_text = seg.get("text", "").strip()
            if not seg_text:
                continue
            text_parts.append(seg_text)
            ts = seg.get("timestamps", {}) or seg.get("offsets", {}) or {}
            t_from_ms = ts.get("from") or ts.get("offset_from") or 0
            # whisper.cpp timestamps están en ms; convertir a sec
            try:
                t_from = float(t_from_ms) / 1000.0 if t_from_ms else 0.0
            except (TypeError, ValueError):
                t_from = 0.0
            # Aproximar timestamps de palabras dividiendo el segmento
            seg_words = seg_text.split()
            n = len(seg_words)
            if n == 0:
                continue
            t_to_ms = ts.get("to") or ts.get("offset_to") or 0
            try:
                t_to = float(t_to_ms) / 1000.0 if t_to_ms else t_from + 1.0
            except (TypeError, ValueError):
                t_to = t_from + 1.0
            step = (t_to - t_from) / max(n, 1)
            for i, w in enumerate(seg_words):
                words.append([w, round(t_from + i * step, 2)])
        full_text = " ".join(text_parts).strip()
        return {"text": full_text, "words": words}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--audio-ids", help="CSV de clip_ids de audio para forzar")
    ap.add_argument("--only-interviews-sync", action="store_true", default=True,
                    help="Solo audios sincronizados con entrevistas (default)")
    ap.add_argument("--model", default=str(Path.home() / "cinema-assistant/models/ggml-large-v3-turbo.bin"))
    ap.add_argument("--language", default="es")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-transcribir aunque haya legacy_pre_context_*.json (ya re-transcrito)")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    tr_dir = root / ".cinema_assistant" / "transcripts"
    log_file = root / ".cinema_assistant" / "logs" / f"retranscribe_ctx_{int(time.time())}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )

    model_path = Path(args.model).expanduser()
    if not model_path.exists():
        sys.exit(f"Modelo Whisper no existe: {model_path}")

    conn = manifest.conectar(str(db))
    ids = None
    if args.audio_ids:
        ids = [int(x.strip()) for x in args.audio_ids.split(",") if x.strip()]
    audios = get_audios_for_retranscription(conn, args.only_interviews_sync, ids)
    logging.info(f"Audios candidatos: {len(audios)}")

    n_done = 0
    n_skipped = 0
    for aid, vid, apath, afn in audios:
        legacy = tr_dir / f"legacy_pre_context_{aid}.json"
        plain = tr_dir / f"{aid}.json"
        if legacy.exists() and not args.force:
            logging.info(f"  ⊘ aid={aid} {afn}: ya re-transcrito (legacy existe)")
            n_skipped += 1
            continue

        # Construir contexto
        prompt = get_context_for_video(conn, vid)
        logging.info(f"\n=== aid={aid} {afn} (sync con vid={vid}) ===")
        logging.info(f"  prompt ({len(prompt)} chars): {prompt[:200]}…")

        if args.dry_run:
            logging.info(f"  [DRY] skip actual transcription")
            continue

        # Snapshot del viejo
        if plain.exists() and not legacy.exists():
            shutil.copy2(plain, legacy)
            logging.info(f"  ✓ snapshot legacy_pre_context_{aid}.json")

        # Re-transcribir
        t0 = time.time()
        new = run_whisper_with_prompt(Path(apath), model_path, prompt, args.language)
        elapsed = time.time() - t0
        if not new or not new.get("text"):
            logging.warning(f"  ⊘ re-transcripción fallo (en {elapsed:.0f}s)")
            continue

        # Guardar
        with plain.open("w") as f:
            json.dump(new, f, ensure_ascii=False)
        n_done += 1
        logging.info(f"  ✓ aid={aid}: {len(new['words'])} words, "
                     f"text[:120]: {new['text'][:120]!r}  (en {elapsed:.0f}s)")

    logging.info(f"\n✅ Re-transcripciones: {n_done}, skipped: {n_skipped}")
    conn.close()


if __name__ == "__main__":
    main()
