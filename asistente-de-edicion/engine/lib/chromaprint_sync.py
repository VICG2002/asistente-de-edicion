"""Audio sync por chromaprint (acoustic fingerprinting).

Chromaprint (`fpcalc`) genera un fingerprint robusto del audio basado en
chroma features (no en envelope crudo). Es MUCHO más resistente a:
- Diferencias de SNR (lavalier vs A1 de cámara con música encima).
- EQ / filtros aplicados (cámaras con preset distinto al recorder).
- Compresión MP3/AAC vs WAV.

Caso fundador (Zezzions 2026-05-26): el envelope FFT
(`lib/waveform_sync.py`) dio confianzas muy bajas (≤ 0.40) para entrevistas
donde la música ambiente tapaba el diálogo en cámara. Chromaprint es la
herramienta estándar de la industria para audio matching y debería
funcionar mejor en esos casos.

Requisitos: `brew install chromaprint` → expone el binario `fpcalc`.

API:
    from lib.chromaprint_sync import extract_fingerprint, correlate_fingerprints
    fp_clip = extract_fingerprint(Path("video.mp4"))
    fp_rec  = extract_fingerprint(Path("audio.wav"))
    res = correlate_fingerprints(fp_clip, fp_rec)
    if res:
        offset_sec, confidence = res

Notas técnicas sobre el fingerprint de fpcalc:
- Por defecto, fpcalc emite ~7-8 hashes/segundo (frames de ~124ms).
- Cada hash es uint32. La distancia entre hashes se mide con popcount XOR
  (Hamming distance).
- Para sync: hacer correlación deslizante de los dos vectores de hashes,
  midiendo "matches buenos" (hamming dist ≤ 8 de 32 bits = 75% similar).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Tasa de hashes de fpcalc, DEDUCIDA de sus parametros en vez de estimada.
#
# fpcalc remuestrea a 11025 Hz y analiza en ventanas de 4096 muestras con 2/3 de
# solape, asi que avanza 4096/3 muestras por hash:
#
#     11025 / (4096/3) = 8.0750 hash/s
#
# Aqui habia un 7.85 "aproximado" que metia un error de ESCALA del +2.87 %:
# proporcional al desfase, o sea invisible en pruebas cortas y demoledor en las
# largas. Medido el 2026-08-13 contra un par de Morsa con offset conocido de
# -1550.93 s (medido por contenido):
#
#     con 7.85    ->  -1595.80 s   (44.87 s de error)
#     con 8.0750  ->  -1551.34 s   ( 0.41 s de error)
#
# Esto importa mas de lo que parece. La huella acustica es la unica tecnica del
# motor que FUNCIONA SOBRE MUSICA — es para lo que se invento—, y es justo donde
# la correlacion de envolvente se rinde: en Morsa, 265 de 291 pares salen con
# prominencia debil porque el concierto tapa la voz. Con la constante mala,
# chromaprint erraba decenas de segundos y parecia inservible; con la buena da
# un ancla de sub-segundo que el refinador fino ya puede pulir a frame.
FPCALC_HASH_RATE = 11025 / (4096 / 3)


@dataclass
class Fingerprint:
    hashes: list[int]
    duration: float

    def __len__(self):
        return len(self.hashes)


def extract_fingerprint(path: Path, max_duration: int = 1800) -> Optional[Fingerprint]:
    """Genera fingerprint chromaprint de un archivo.

    Args:
        path: ruta al archivo.
        max_duration: segundos a analizar (default 30 min — cap para archivos largos).

    Returns:
        Fingerprint o None si falló.
    """
    cmd = ["fpcalc", "-raw", "-length", str(max_duration), "-json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    fp_raw = data.get("fingerprint")
    if not fp_raw:
        return None
    # fpcalc en modo -raw emite la lista de hashes separada por comas
    if isinstance(fp_raw, str):
        try:
            hashes = [int(x) for x in fp_raw.split(",") if x]
        except ValueError:
            return None
    elif isinstance(fp_raw, list):
        hashes = [int(x) for x in fp_raw]
    else:
        return None
    dur = float(data.get("duration", 0.0))
    return Fingerprint(hashes=hashes, duration=dur)


def _popcount(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1")


def correlate_fingerprints(
    fp_clip: Fingerprint,
    fp_rec: Fingerprint,
    similarity_threshold: int = 8,
    min_anchor_matches: int = 8,
) -> Optional[tuple[float, float]]:
    """Cross-correlate dos fingerprints buscando el offset que maximiza matches.

    Args:
        fp_clip: fingerprint del clip de video.
        fp_rec: fingerprint del audio externo (recording).
        similarity_threshold: bits máximos de diferencia (Hamming) para
            considerar un hash como "match" (default 8 — 75% similar).
        min_anchor_matches: mínimo de matches para considerar un offset válido.

    Returns:
        (offset_sec, confidence) con
            offset_sec = audio_start - video_start (negativo = audio antes)
            confidence = matches_at_best / max_possible (0..1)
        o None si no se encontró offset confiable.
    """
    if not fp_clip or not fp_rec:
        return None
    a = fp_clip.hashes
    b = fp_rec.hashes
    if len(a) < 8 or len(b) < 8:
        return None

    # Estrategia: para cada offset (i, j) donde i pos en a y j pos en b,
    # contar cuántos hashes a[i+k] ~ b[j+k] tienen popcount(xor) <= threshold.
    # Esto es O(n^2) — costoso pero correcto. Para audios largos, sub-sample
    # los hashes de uno de los dos (en `b`) y solo evalúa offsets espaciados.

    # Sub-sample b: usar un anchor cada 4 hashes (~0.5s) para reducir trabajo.
    # Pero también necesitamos resolution fina cerca del max.
    # Strategy: dos pasadas.

    # Pasada 1: anchors gruesos
    anchor_stride = max(1, len(a) // 200)  # ~200 anchors
    anchors_idx = list(range(0, len(a), anchor_stride))

    best_lag = 0
    best_score = -1
    n_hashes = len(a)
    coarse_step = max(1, len(b) // 500)

    for j_start in range(0, len(b) - 8, coarse_step):
        # Compare hashes a[anchors] vs b[j_start + anchors] for sample anchors
        score = 0
        n_compared = 0
        for ai in anchors_idx:
            bi = j_start + ai
            if bi >= len(b) - 1:
                break
            d = _popcount(a[ai] ^ b[bi])
            if d <= similarity_threshold:
                score += 1
            n_compared += 1
        if n_compared >= min_anchor_matches and score > best_score:
            best_score = score
            best_lag = j_start

    if best_score < min_anchor_matches:
        return None

    # Pasada 2: refinar alrededor del mejor lag
    refine_start = max(0, best_lag - coarse_step)
    refine_end = min(len(b) - 8, best_lag + coarse_step)
    for j_start in range(refine_start, refine_end + 1):
        score = 0
        n_compared = 0
        for ai in anchors_idx:
            bi = j_start + ai
            if bi >= len(b) - 1:
                break
            d = _popcount(a[ai] ^ b[bi])
            if d <= similarity_threshold:
                score += 1
            n_compared += 1
        if score > best_score:
            best_score = score
            best_lag = j_start

    # offset = audio_start - video_start
    # Si a starts at video_start=0 y b starts at audio_start, y a[ai] ~ b[best_lag + ai],
    # entonces tiempo en audio = tiempo en video + (best_lag / hash_rate).
    # audio_t = video_t + (best_lag / hash_rate)
    # → audio_start = best_lag / hash_rate
    # → offset_sec = audio_start - video_start = best_lag / hash_rate
    offset_sec = best_lag / FPCALC_HASH_RATE
    # Convención del motor: offset = audio_start - video_start
    # Si a (video) empieza después que b (audio), el lag será negativo.
    # En nuestra implementación, asumimos a alineado al inicio de b a partir
    # de best_lag — eso significa b empezó antes (audio_start < 0 vs video).
    # Pero el offset que importa para Resolve es: a qué t en el WAV
    # corresponde el inicio del video. Eso es +best_lag/hash_rate.
    # Para convertir a "audio_start - video_start" (convención negativa-audio-antes):
    offset_sec = -offset_sec  # audio empezó antes → offset negativo

    confidence = min(1.0, best_score / max(1, len(anchors_idx)))
    return offset_sec, confidence
