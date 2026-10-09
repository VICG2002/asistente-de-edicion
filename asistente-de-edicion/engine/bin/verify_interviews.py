#!/usr/bin/env python3
"""Verificador: detectar entrevistas potenciales no marcadas como tal.

Caso fundador (Zezzions 2026-05-26): perdí 2 entrevistas (ENTREVISTADO_1 clip
2794, ENTREVISTADO_2 clip 2786) porque sus transcripts de cámara eran
"basura masiva" (Whisper alucinó "No, ya tenemos artística" ×100 sobre
música) PERO el inicio del transcript contenía las preguntas REALES del
entrevistador (~5 preguntas, ~70-100 palabras limpias).

El detector `analyze_transcript()` marcó esos transcripts como degradados
por el ratio de basura, y mi categorización requería ≥60 palabras
distintivas limpias. Como las preguntas son cortas, no pasaban el umbral.
Resultado: no se marcaron como 'entrevista' → no se sincronizaron →
quedaron como B-roll desconocido hasta que el usuario los detectó
visualmente en Resolve.

**Garantía verificable**: este script audita el manifest y reporta:
  1. Videos con duración >= MIN_DUR (default 60s)
  2. + audio activo (audio_rms_db > -50 o ausente — no asumir silencio)
  3. + transcript que contiene ≥ MIN_QUESTIONS patrones de pregunta
     ("¿Cómo te enteraste?", "Yo soy X", "Cuéntanos cómo...", etc.)
  4. SIN categoría 'entrevista' en clip_descriptions
  5. SIN sync_pair en audio_sync_pairs

Estos son "entrevistas potenciales no detectadas" — el editor debe
revisarlas. Exit code 1 si encuentra ≥ 1 — falla el pipeline si quieres
forzar revisión humana antes de cerrar el proyecto.

Uso:
    bin/verify_interviews.py --root /Volumes/.../Zezzions VICG
    bin/verify_interviews.py --root ... --fail-on-found  # exit 1 si hay alguna
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


# Patrones distintivos de PREGUNTA o AUTO-ID de entrevista, en español.
# v2 (2026-05-26 Zezzions): ampliado tras descubrir que clip 2645 (entrevista
# ENTREVISTADO_13 y ENTREVISTADO_10) solo matcheaba 1 patrón con la lista v1 — se perdió porque
# default min_questions=2. Agregados: "¿Cómo se conocieron?", "¿De dónde",
# "Hola, soy", "Soy hermano de", "¿Por qué", "¿Para qué", auto-IDs amplios.
QUESTION_PATTERNS = [
    r"¿[Cc]ómo te enteraste",
    r"¿[Cc]ómo conociste",
    r"¿[Cc]ómo empez(aste|aron|amos)",
    r"¿[Cc]ómo se conoci",
    r"¿[Cc]ómo (les|le) (va|salió|fue)",
    r"¿[Cc]uál es (tu|el)",
    r"¿[Cc]uéntanos",
    r"¿[Cc]uándo empez",
    r"¿[Qq]uién (es|eres|fue|son)",
    r"¿[Qq]ué es lo que",
    r"¿[Qq]ué te (gusta|inspira|dedicas|parece)",
    r"¿[Qq]ué (prefier|opina)",
    r"¿[Pp]or qué",
    r"¿[Pp]ara qué (sirve|haces)",
    r"¿[Pp]odrías (decir|contarnos|repetir|describir)",
    r"¿[Tt]e late",
    r"¿[Cc]ómo te ves en \d+ años",
    r"¿[Dd]ónde (quieres|estás|vives)",
    r"¿[Dd]e dónde (vienes|eres|son)",
    r"¿[Cc]uáles son (tus|sus)",
    r"di tu nombre",
    # Auto-ID (presentación del entrevistado o entrevistador):
    r"Mi nombre (es|artístico) \w+",
    r"[Yy]o soy \w+",
    r"[Mm]e llamo \w+",
    r"Hola[,.] soy \w+",
    r"[Ss]oy hermano de \w+",
    r"[Ss]oy uno de los",
    r"[Mm]i proyecto (es|se llama)",
    # Específicos de eventos musicales (Zezzions context)
    r"¿[Cc]ómo manejas",
    r"con la música",
]

# Marcadores anti-alucinación (Whisper sobre música o video largo sin habla):
HALLUCINATION_HINTS = [
    "Suscríbete al canal", "Gracias por ver el video",
    "subtítulos por la comunidad", "♪♪♪", "Amara.org",
]


def count_question_patterns(text: str) -> int:
    """Cuenta cuántos patrones distintos de pregunta-entrevista aparecen."""
    if not text:
        return 0
    return sum(1 for pat in QUESTION_PATTERNS if re.search(pat, text))


def looks_hallucinated(text: str) -> bool:
    """Heurística: si una sola frase ocupa >50% del texto, o domina un hint
    de alucinación, marcar como basura — no es entrevista útil."""
    if not text:
        return True
    words = text.split()
    if len(words) < 20:
        return False  # texto corto puede ser legítimo
    # Hint match: si >30% del texto es un hint de alucinación
    low = text.lower()
    for h in HALLUCINATION_HINTS:
        if low.count(h.lower()) >= 3:
            return True
    # Diversidad léxica: si la palabra más común ocupa > 30% del texto, sospechoso
    from collections import Counter
    top1, n1 = Counter(words).most_common(1)[0]
    return (n1 / len(words)) > 0.3


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
    ap.add_argument("--min-dur", type=int, default=60,
                    help="Duración mínima del clip de video (segundos).")
    ap.add_argument("--min-questions", type=int, default=2,
                    help="Mínimo de patrones de pregunta para considerar entrevista posible.")
    ap.add_argument("--fail-on-found", action="store_true",
                    help="Exit 1 si encuentra entrevistas no detectadas (para CI/pipeline).")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db_path = root / ".cinema_assistant" / "manifest.sqlite"
    cache = root / ".cinema_assistant" / "transcripts"
    if not db_path.exists():
        sys.exit(f"No manifest at {db_path}")

    conn = manifest.conectar(str(db_path))

    # Candidatos v2: videos largos, con audio, donde
    #   (a) NO tienen categoría entrevista Y NO tienen sync_pair — perdidos.
    #   (b) SÍ están marcados entrevista PERO sin sync_pair — para revisión.
    # Caso (b) agregado tras Zezzions 2026-05-26 (clip 2573 ENTREVISTADO_5 organizador
    # marcado como entrevista pero sin audio externo grabado — no es bug,
    # pero debe REPORTARSE para que el editor confirme manualmente).
    # Incluir n_face_detections para regla de rescate de entrevistas con
    # A1 alucinado pero caras visibles (Zezzions iter9.7 — clip 2644 perdido).
    # face_detections es de Fase 2 (opcional) — en un proyecto fresco puede
    # no existir aún (FCC 2026-07-09); n_faces=0 deja la regla de rescate
    # inactiva sin tirar el verifier.
    # `audio_sync_pairs` la crean los scripts de sync. Un rodaje SOLO CAMARA
    # —legitimo, no incompleto— nunca los corre, asi que la tabla no existe y
    # este verificador MORIA con "no such table" (medido el 2026-08-18 en el dia
    # 2 de esa cobertura). Es de los OBLIGATORIOS del paso 12b: si truena,
    # la garantia no corre, y quien mire solo la ultima linea no se entera.
    # Crearla vacia deja el NOT IN operando sobre cero filas, que es justo lo
    # que significa "ningun clip tiene sync".
    manifest.ensure_audio_sync_pairs(conn)

    has_faces = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='face_detections'").fetchone() is not None
    n_faces_expr = ("COALESCE((SELECT COUNT(*) FROM face_detections fd "
                    "WHERE fd.clip_id=c.id), 0)" if has_faces else "0")
    rows = conn.execute(
        f"""SELECT c.id, c.filename, ROUND(c.duration_sec) AS dur,
                   d.category, ca.audio_rms_db,
                   {n_faces_expr} AS n_faces
            FROM clips c
            LEFT JOIN clip_descriptions d ON d.clip_id=c.id
            LEFT JOIN clip_analysis ca ON ca.clip_id=c.id
            WHERE c.file_kind='video' AND c.index_status='ok' AND c.has_audio=1
              AND c.duration_sec >= ?
              AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)""",
        (args.min_dur,)
    ).fetchall()

    candidates = []
    for cid, fn, dur, cat, rms, n_faces in rows:
        tp = cache / f"{cid}.json"
        if not tp.exists():
            continue
        try:
            d = json.load(open(tp))
        except Exception:
            continue
        text = d.get("text", "") or ""
        # REGLA DE RESCATE iter9.7 (clip 2644 perdido):
        # Si el A1 está alucinado pero hay caras detectadas Y duración largo,
        # MUY PROBABLEMENTE es entrevista con A1 corrupto. NO descartar —
        # reportar para buscar audio externo limpio como fuente alternativa.
        is_hallucinated = (not text) or looks_hallucinated(text)
        if is_hallucinated:
            if n_faces >= 20 and dur >= 120:
                # Buscar audios externos no sincronizados como candidatos
                un_synced = conn.execute("""
                    SELECT id, filename FROM clips
                    WHERE file_kind='audio' AND index_status='ok'
                      AND duration_sec >= ?
                      AND id NOT IN (SELECT audio_clip_id FROM audio_sync_pairs)
                    ORDER BY ABS(duration_sec - ?) ASC
                    LIMIT 5
                """, (dur * 0.5, dur)).fetchall()
                cand_audios = ", ".join(f"aid={a[0]}({a[1]})" for a in un_synced[:3])
                candidates.append({
                    "cid": cid, "fn": fn, "dur": dur, "n_q": 0,
                    "cat": cat or "", "n_faces": n_faces,
                    "rms": rms, "ctx": "(transcript A1 alucinado)",
                    "reason": (f"A1 alucinado + {n_faces} caras + dur {dur}s — "
                               f"probable entrevista con A1 corrupto. "
                               f"Audios candidatos sin sync: {cand_audios or 'NONE'}. "
                               f"Probar bin/apply_waveform_sync con esos."),
                })
            continue
        n_q = count_question_patterns(text)
        # Audio activo: rms > -45dB sugiere voz humana. Si rms es None, no
        # asumir nada (no penalizar).
        audio_active = rms is None or (rms > -45)
        # Lógica multi-señal:
        #   - Si ya es entrevista marcada sin sync → reportar SIEMPRE (caso b).
        #   - Si no es entrevista marcada Y matchea >= min_questions → reportar.
        #   - Si no es entrevista marcada Y n_q=1 Y audio_active Y dur>=120s
        #     → reportar (señal débil pero confluencia sugiere entrevista).
        is_already_interview = bool(cat and cat.startswith("entrevista"))
        # entrevista-solo-video = caso confirmado: el editor sabe que no se
        # grabó audio externo (sin lavalier o lavalier no disponible).
        # NO re-reportar — ya está documentado.
        is_solo_video = bool(cat and cat == "entrevista-solo-video")
        reason = None
        if is_solo_video:
            continue  # confirmado por editor — saltar
        if is_already_interview:
            reason = "entrevista marcada SIN sync"
        elif n_q >= args.min_questions:
            reason = f"{n_q} preguntas detectadas, sin categoría"
        elif n_q == 1 and audio_active and dur >= 120:
            reason = "1 pregunta + audio activo + >=120s (señal débil)"
        if reason:
            ctx = ""
            for pat in QUESTION_PATTERNS:
                m = re.search(pat, text)
                if m:
                    pos = m.start()
                    ctx = text[max(0, pos-20):pos+100]
                    break
            if not ctx:
                ctx = text[:120]
            candidates.append({
                "id": cid, "fn": fn, "dur": dur, "cat": cat or "(s/cat)",
                "rms_db": rms, "n_q": n_q, "ctx": ctx, "reason": reason,
            })

    # RODAJE SOLO CAMARA: el caso (b) deja de ser una alarma (2026-08-18).
    #
    # El caso (b) —"entrevista marcada SIN sync"— existe para que el editor
    # confirme que no se perdio un lavalier. Si el proyecto NO TIENE UN SOLO
    # archivo de audio externo, ese estado es el unico posible y la alarma se
    # dispara en todas las entrevistas del rodaje: en el dia 2 de Daniel
    # Espinosa, 5 de 5. Un verificador que grita lobo en cada proyecto solo
    # camara se deja de leer, y entonces tampoco avisa del caso (a), que es el
    # que de verdad pierde material.
    #
    # El caso (a) —entrevista NO detectada— NO se toca: sigue siendo fallo.
    # "Hay audio externo" NO es "hay algun archivo de audio". En el dia 2 de
    # esa cobertura el manifest tiene 5 .AAC — pero son los hermanos de los
    # clips de camara lenta de la Osmo, de 0.8 a 7.7 s. Ninguno puede sostener
    # una entrevista ni sincronizar nada: la doctrina ya exige >= 60 s antes de
    # correlacionar (errores-comunes #4, "waveform con audio corto = falsos
    # positivos"). Se usa el mismo umbral para no tener dos definiciones.
    hay_audio_externo = conn.execute(
        "SELECT COUNT(*) FROM clips WHERE file_kind='audio' "
        "AND IFNULL(duration_sec,0) >= 60").fetchone()[0]
    if not hay_audio_externo:
        solo_b = [c for c in candidates if "SIN sync" in (c.get("reason") or "")]
        if solo_b:
            print(f"ℹ  {len(solo_b)} entrevista(s) marcadas sin sync — el proyecto no "
                  "tiene NINGUN audio externo de 60 s o mas,")
            print("   asi que 'sin sync' es el unico estado posible: son "
                  "entrevistas-solo-video. No es un hallazgo.")
            for c in solo_b:
                print(f"     · {c['fn']} ({c['dur']:.0f}s)")
            print()
        candidates = [c for c in candidates if c not in solo_b]

    if not candidates:
        print("✅ verify_interviews: 0 entrevistas potenciales no detectadas")
        return 0

    print(f"⚠ ENTREVISTAS POTENCIALES NO DETECTADAS: {len(candidates)}")
    print(f"   (videos >= {args.min_dur}s + audio + >= {args.min_questions} patrones de pregunta + sin categoría + sin sync)")
    print()
    for c in candidates:
        print(f"  ✗ clip_id={c['id']:>4} {c['fn']:<32} dur={c['dur']:>4.0f}s "
              f"cat={c['cat']:<25} rms_db={c['rms_db'] if c['rms_db'] is not None else '?':<6} "
              f"patrones={c['n_q']}  [{c['reason']}]")
        print(f"      contexto: ...{c['ctx'].strip()[:140]!r}...")
    print()
    print("Acciones sugeridas:")
    print(" - Curar manualmente: marcar como 'entrevista' en clip_descriptions.")
    print(" - Buscar TX con sync_by_phrases o sync_by_questions.")
    print(" - Si no hay TX: marcar como entrevista-solo-video.")
    print(" - Ver doctrina §5 (Whisper alucina con música) y §22 (mención≠cuadro).")

    if args.fail_on_found:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
