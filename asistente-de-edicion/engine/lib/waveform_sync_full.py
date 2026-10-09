"""Sync por waveform en RANGO COMPLETO del audio externo.

Doctrina (Zezzions iter9.5, 2026-05-27): el sync por transcript es robusto
cuando ambos lados tienen diálogo denso y limpio. Pero falla con:
  - Multi-take en el audio (ENTREVISTADO_4 2671)
  - Transcripts alucinados
  - Pocas palabras anchor compartidas
  - Whisper inconsistente entre fuentes

El sync por waveform es FÍSICO — mide energía RMS independiente del
contenido lingüístico. Es la base de DaVinci's "Auto Sync by Waveform".

Estrategia (iter9.5):
  1. Extraer A1 del video (banda voz 300-3400 Hz) → envelope log-RMS @ 100Hz
  2. Extraer audio externo completo (banda voz) → envelope log-RMS @ 100Hz
  3. Cross-correlate FFT full range (no ventana ±2s)
  4. Encontrar peak global con prominence
  5. Validar:
       - prominence >= 0.30: peak claramente dominante
       - peak_norm >= 0.05: no es ruido aleatorio
  6. Devolver offset físico

Convención: offset = audio_start - video_start.
Si offset = -334.5, el audio empezó 334.5s antes que el video.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.sync_refiner import _load_pcm, _envelope, WORK_SR

ENV_SR = 100  # envelope sample rate (hop 10ms)


def waveform_sync_full(video_path: Path, audio_path: Path,
                        video_dur: float, audio_dur: float,
                        video_analysis_dur: float = 60.0,
                        voice_bandpass: bool = True
                       ) -> Optional[dict]:
    """Sync por cross-correlation de envelope log-RMS en banda voz, sin
    ventana — busca peak en todo el rango temporal posible.

    Args:
        video_path, audio_path: rutas a los archivos
        video_dur, audio_dur: duraciones de cada uno
        video_analysis_dur: cuántos segundos del video usar como "pattern"
            (default 60s — más rápido y suele ser suficiente)
        voice_bandpass: aplicar filtro 300-3400 Hz (mejora con música ambient)

    Returns dict con:
        offset_sec: float — offset físico
        prominence: float — qué tan claro es el peak (0-1)
        peak_norm: float — peak / max teórico (medida absoluta de fuerza)
        n_video_samples: int — samples del envelope del video usados
        n_audio_samples: int — samples del envelope del audio
        method: str — "waveform-fft-full"
    O None si no se puede calcular.
    """
    if video_dur < 5 or audio_dur < 5:
        return None
    actual_video_dur = min(video_analysis_dur, video_dur - 1.0)
    if actual_video_dur < 5:
        return None

    # 1. Cargar A1 del video (tomar el medio del clip, suele tener voz limpia)
    v_start = max(0.0, (video_dur - actual_video_dur) / 2)
    pcm_v = _load_pcm(video_path, v_start, actual_video_dur,
                       voice_bandpass=voice_bandpass)
    if pcm_v is None or len(pcm_v) < WORK_SR * 5:
        return None

    # 2. Cargar audio externo COMPLETO
    pcm_a = _load_pcm(audio_path, 0.0, audio_dur,
                       voice_bandpass=voice_bandpass)
    if pcm_a is None or len(pcm_a) < WORK_SR * 5:
        return None

    # 3. Envelope log-RMS
    env_v = _envelope(pcm_v, sr=WORK_SR, hop_ms=10)
    env_a = _envelope(pcm_a, sr=WORK_SR, hop_ms=10)
    if env_v.size < 100 or env_a.size < 100:
        return None

    # 4. Cross-correlate FFT (rápido para señales largas)
    from scipy.signal import correlate, correlation_lags
    # Normalizar envelopes (zero-mean) para que el peak sea más limpio
    env_v_n = env_v - env_v.mean()
    env_a_n = env_a - env_a.mean()
    corr = correlate(env_a_n, env_v_n, mode="full", method="fft")
    lags = correlation_lags(env_a_n.size, env_v_n.size, mode="full")

    # 5. Encontrar peak
    peak_idx = int(np.argmax(corr))
    peak_val = float(corr[peak_idx])
    # Normalizar peak por norma euclidiana (Cauchy-Schwarz bound)
    peak_norm = peak_val / (np.linalg.norm(env_v_n) * np.linalg.norm(env_a_n) + 1e-9)

    # Prominence: peak vs runner-up (excluyendo ±0.5s alrededor del peak)
    half = max(int(0.5 * ENV_SR), 5)
    mask = np.ones_like(corr, dtype=bool)
    lo = max(0, peak_idx - half)
    hi = min(len(corr), peak_idx + half + 1)
    mask[lo:hi] = False
    if mask.any():
        runner = float(np.max(corr[mask]))
        prominence = (peak_val - runner) / (peak_val + 1e-9)
    else:
        prominence = 1.0

    # 6. Convertir peak lag a offset físico
    # peak_lag (en samples del envelope) representa: cuántos samples del
    # envelope HAY QUE DESPLAZAR el video para alinearlo con el audio.
    # En segundos: lag_sec = peak_lag / ENV_SR
    # Pero el video se cargó desde v_start, no desde 0.
    # offset_audio_minus_video = lag_sec - v_start
    #   (porque al cargar el video desde v_start, el envelope_v[0] corresponde
    #    a video_t = v_start. El cross-correlate dice que envelope_v aparece
    #    en audio_t = lag_sec. Por lo tanto:
    #    audio_t_at_v_start_video = lag_sec
    #    audio_t_at_video_start (video_t=0) = lag_sec - v_start
    #    offset = audio_start - video_start = -(audio_t at video_t=0)
    #    offset = -(lag_sec - v_start) = v_start - lag_sec)
    peak_lag = int(lags[peak_idx])
    lag_sec = peak_lag / ENV_SR
    offset_sec = v_start - lag_sec

    return {
        "offset_sec": float(offset_sec),
        "prominence": float(prominence),
        "peak_norm": float(peak_norm),
        "n_video_samples": int(env_v.size),
        "n_audio_samples": int(env_a.size),
        "method": "waveform-fft-full",
        "video_analyzed_from": float(v_start),
        "video_analyzed_dur": float(actual_video_dur),
        "peak_lag_sec": float(lag_sec),
    }


def waveform_sync_siblings(audio_a_path: Path, audio_b_path: Path,
                            dur_a: float, dur_b: float,
                            analysis_dur: float = 120.0,
                            voice_bandpass: bool = True
                           ) -> Optional[dict]:
    """Sync entre dos lavaliers (sin video). Útil para verificar/derivar
    sync de hermanos del mismo Wireless PRO RX.

    Returns el offset que aplica a audio_b respecto a audio_a:
        audio_b_start = audio_a_start + delta_sec
    (con delta_sec ≈ 0 si son hermanos del mismo RX, típicamente <100ms)
    """
    use_dur = min(analysis_dur, dur_a - 1, dur_b - 1)
    if use_dur < 30:
        return None

    # Tomar inicio + medio + final para tener señal
    pcm_a = _load_pcm(audio_a_path, 0.0, use_dur, voice_bandpass=voice_bandpass)
    pcm_b = _load_pcm(audio_b_path, 0.0, use_dur, voice_bandpass=voice_bandpass)
    if pcm_a is None or pcm_b is None:
        return None
    env_a = _envelope(pcm_a, sr=WORK_SR, hop_ms=10)
    env_b = _envelope(pcm_b, sr=WORK_SR, hop_ms=10)
    if env_a.size < 100 or env_b.size < 100:
        return None
    from scipy.signal import correlate
    env_a_n = env_a - env_a.mean()
    env_b_n = env_b - env_b.mean()
    corr = correlate(env_b_n, env_a_n, mode="full", method="fft")
    n_a = env_a_n.size
    peak_idx = int(np.argmax(corr))
    delta_samples = peak_idx - (n_a - 1)
    delta_sec = delta_samples / ENV_SR

    # Prominence
    peak_val = float(corr[peak_idx])
    peak_norm = peak_val / (np.linalg.norm(env_a_n) * np.linalg.norm(env_b_n) + 1e-9)
    half = max(int(0.2 * ENV_SR), 5)
    mask = np.ones_like(corr, dtype=bool)
    lo = max(0, peak_idx - half)
    hi = min(len(corr), peak_idx + half + 1)
    mask[lo:hi] = False
    runner = float(np.max(corr[mask])) if mask.any() else 0.0
    prominence = (peak_val - runner) / (peak_val + 1e-9)

    return {
        "delta_sec": float(delta_sec),
        "prominence": float(prominence),
        "peak_norm": float(peak_norm),
        "method": "waveform-sibling",
    }
