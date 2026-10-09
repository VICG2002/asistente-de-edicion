"""Refinamiento sub-segundo de offsets de sync.

Caso fundador (Zezzions 2026-05-26): el usuario reportó "el audio del
interlocutor es correcto pero el sync no coincide exactamente". Los métodos
de sync existentes tienen resoluciones limitadas:
  - transcript/phrase-match/question-anchor: ~200ms jitter (Whisper word
    timestamps)
  - envelope: ~100ms (envelope sample rate típico 10 Hz)
  - chromaprint: ~125ms (8 hashes/segundo)

Para lip-sync perfecto necesitamos < 40ms de precisión (1 frame a 24fps).

Este módulo toma un offset existente y lo refina cross-correlacionando
el audio INTERNO de la cámara (A1, embebido en el video) con el lavalier
externo (A2) en una ventana fina alrededor del offset actual, con
resolución de 10ms (~100 Hz).

Estrategia:
  1. Extraer ~30s de A1 y A2 alrededor del centro temporal del video.
  2. Re-samplear a 4kHz mono (suficiente para correlación de voz).
  3. Cross-correlate con FFT con scipy.signal.correlate.
  4. Buscar peak dentro de ±2s alrededor del offset actual.
  5. Retornar new_offset = current_offset + delta, donde delta es la
     diferencia entre el peak encontrado y el centro de búsqueda.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


# Sample rate de trabajo para la correlación fina. 4 kHz es suficiente
# para voz (banda 100-1500 Hz) y mantiene FFT manejable.
WORK_SR = 4000
# Resolución temporal mínima del refinamiento = 1/WORK_SR = 0.25 ms.
# Ventana de búsqueda alrededor del offset actual.
SEARCH_WINDOW_SEC = 2.0
# Duración del tramo de audio a analizar (más largo = mejor SNR contra
# música, pero más lento).
ANALYSIS_DUR_SEC = 30.0


@dataclass
class RefineResult:
    delta_sec: float          # corrección aplicada (new = old + delta)
    new_offset: float
    correlation_peak: float   # valor del peak normalizado [0..1]
    confidence: float         # heurística [0..1]
    notes: str = ""


def _load_pcm(path: Path, start_sec: float, duration: float,
              sr: int = WORK_SR, voice_bandpass: bool = True) -> np.ndarray | None:
    """Extrae un tramo PCM mono usando ffmpeg.

    Si voice_bandpass=True, aplica filtro 300-3400 Hz (banda de voz humana)
    via ffmpeg highpass+lowpass — elimina rumble bajo y agudos extremos donde
    suele estar más música/ambiente.
    """
    af_chain = []
    if voice_bandpass:
        af_chain.append("highpass=f=300")
        af_chain.append("lowpass=f=3400")
    af_arg = ["-af", ",".join(af_chain)] if af_chain else []
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{start_sec:.3f}", "-i", str(path),
        "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", str(sr),
        *af_arg,
        "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as e:
        logging.warning(f"sync_refiner: ffmpeg falló para {path.name}: {e}")
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout
    if len(raw) < 100:
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _envelope(pcm: np.ndarray, sr: int = WORK_SR,
              hop_ms: int = 10) -> np.ndarray:
    """Calcula log-RMS envelope por ventanas de hop_ms.

    Mucho más robusto que PCM raw para correlate con ambiente musical:
    captura la DINÁMICA del habla (onset/offset de sílabas) que es
    consistente entre A1 (cámara con música de fondo) y A2 (lavalier
    cercano con voz dominante).
    """
    hop = max(1, int(sr * hop_ms / 1000))
    n_frames = len(pcm) // hop
    if n_frames == 0:
        return np.zeros(0, dtype=np.float32)
    out = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        chunk = pcm[i * hop:(i + 1) * hop]
        out[i] = np.sqrt(np.mean(chunk * chunk) + 1e-9)
    # log para comprimir dinámica
    out = np.log1p(out * 100)
    # zero-mean
    out = out - out.mean()
    return out


def refine_offset(video_path: Path, audio_path: Path, current_offset: float,
                  video_dur: float, audio_dur: float,
                  window: float = SEARCH_WINDOW_SEC,
                  analysis_dur: float = ANALYSIS_DUR_SEC,
                  center_frac: float = 0.40,
                  voice_bandpass: bool = True) -> Optional[RefineResult]:
    """Refina el offset entre A1 (audio embebido video) y A2 (audio externo).

    current_offset: offset_sec ya conocido, con la convencion del motor
        `offset = audio_start - video_start`. De ahi, y esto es lo unico que
        importa al buscar el tramo correspondiente:

            audio_t = video_t - offset

        Ejemplo: offset = -5.0 (la camara arranco 5 s despues que la grabadora)
        => el frame 0 del video cae en el segundo 5 del audio.

        OJO (2026-08-13): hasta hoy esta linea decia `audio_t = video_t + offset`
        y el codigo de abajo la implementaba, o sea centraba la busqueda a
        2x offset del sitio correcto. Medido contra tests/media_prueba (offset
        real -5.00): desde una estimacion de -4.70 devolvia -4.000 con
        prominence 0.995 — un segundo de error con la maxima confianza, y
        pasando los tres filtros de refine_all_pairs. Lo cubre
        tests/test_sync_convencion.py.
    window: ±window segundos de búsqueda alrededor del offset actual.
    center_frac: posición relativa (0-1) del tramo de análisis dentro del
        video. Default 0.40 (sesgo a la mitad inicial). Llamar con varios
        valores permite VOTO MULTI-VENTANA (MAB 2026-06-12) — más robusto
        cuando el A1 es scratch ruidoso.

    voice_bandpass: filtro 300-3400 Hz antes de correlacionar. True (default)
        para entrevistas, que es para lo que se escribio esto. **False en
        musica**: el filtro borra el bajo y el kick, que son la señal con mas
        energia y mejor definida temporalmente de todo el material. Medido por
        la bifurcacion de Adrian (The Shelter 2026-08-03): un clip daba
        prominence 0.03 con filtro y 0.25 sin el, y el sync sin filtro era el
        correcto a oido del editor.

        AVISO, y es el importante (FilmClubCafe 2026-08-10): quitar el filtro NO
        arregla el refinamiento en concierto. Esta funcion correlaciona envelope
        log-RMS, o sea ENERGIA, y dos camaras en puntos distintos de una sala no
        comparten envelope de energia —otra mezcla, otra reverb, otro publico
        encima— aunque si compartan los mismos onsets. Corrido en full-spectrum,
        17 de 97 pares se "corrigieron" igual de erratico, uno con
        prominence=5.74 (el valor deberia estar en [0,1]). En concierto, el
        refinamiento sub-segundo por esta via no sirve en NINGUNO de los dos
        modos: quedarse con el offset de la etapa anterior.

    Returns None si no se puede refinar (audio inutilizable, peak débil).
    """
    # Elegir un tramo del VIDEO donde haya voz (centro temporal funciona bien
    # para entrevistas — el inicio puede ser instalación de micro).
    video_center = video_dur * center_frac
    v_start = max(0.0, video_center - analysis_dur / 2)
    v_end = min(video_dur, v_start + analysis_dur)
    actual_dur = v_end - v_start
    if actual_dur < 5.0:
        return None

    # Tramo correspondiente en el audio: audio_t = video_t - offset.
    a_center = v_start - current_offset
    a_start = a_center - window

    if a_start < 0.0:
        # El tramo pedido empieza antes del principio del audio.
        #
        # Recortarlo a 0 —lo que se hacia hasta el 2026-08-13— rompe el supuesto
        # de `center_idx` de abajo, que da por hecho que el contenido del video
        # esta EXACTAMENTE a `window` segundos del inicio del audio extraido. El
        # sesgo resultante es igual a lo recortado y CONSTANTE: no depende del
        # offset, asi que no se distingue de una medida buena. Medido: -1.6 s.
        #
        # Rendirse tampoco sirve: en la verificacion A1<->A1 de multicam el
        # offset es casi cero, asi que `a_start` sale negativo por poco y CASI
        # NINGUN par se podria medir. Medido en Morsa: los pares verificados
        # caian de 25 a 10.
        #
        # La salida es correr la ventana de analisis mas adelante EN EL VIDEO
        # hasta que su reflejo en el audio empiece en 0. No hay sesgo —los dos
        # tramos se desplazan juntos— y solo se pierde la medida cuando de
        # verdad no cabe.
        v_start += -a_start
        v_end = min(video_dur, v_start + analysis_dur)
        actual_dur = v_end - v_start
        if actual_dur < 5.0:
            return None
        a_center = v_start - current_offset
        a_start = max(0.0, a_center - window)

    a_end = min(audio_dur, a_start + actual_dur + 2 * window)
    if a_end - a_start < actual_dur + window:
        # No hay suficiente audio para buscar — el offset original probablemente
        # ya está cerca del borde. Sin búsqueda fina.
        return None

    v_pcm = _load_pcm(video_path, v_start, actual_dur, voice_bandpass=voice_bandpass)
    a_pcm = _load_pcm(audio_path, a_start, a_end - a_start, voice_bandpass=voice_bandpass)
    if v_pcm is None or a_pcm is None:
        return None

    # NUEVO 2026-05-26: trabajar sobre ENVELOPE log-RMS (10ms hop) en lugar
    # de PCM raw. Más robusto a música ambient — captura la dinámica del
    # habla (onset/offset de sílabas) que es consistente entre A1 (cámara
    # con música de fondo) y A2 (lavalier cercano con voz dominante).
    HOP_MS = 10
    v_env = _envelope(v_pcm, sr=WORK_SR, hop_ms=HOP_MS)
    a_env = _envelope(a_pcm, sr=WORK_SR, hop_ms=HOP_MS)
    if v_env.size < 100 or a_env.size < 100:
        return None
    ENV_SR = 1000 // HOP_MS  # 100 Hz

    try:
        from scipy.signal import correlate
    except ImportError:
        correlate = lambda x, y, mode="full", method="auto": np.correlate(x, y, mode=mode)

    corr = correlate(a_env, v_env, mode="full", method="fft")

    n_v = len(v_env)
    # Ventana de búsqueda en samples del envelope (window seg × ENV_SR)
    win_samples = int(window * ENV_SR)
    center_idx = (n_v - 1) + win_samples
    lo_idx = max(0, center_idx - win_samples)
    hi_idx = min(len(corr) - 1, center_idx + win_samples)
    window_corr = corr[lo_idx:hi_idx + 1]
    if window_corr.size == 0:
        return None
    peak_local = int(np.argmax(window_corr))
    peak_global = lo_idx + peak_local
    delta_samples = peak_global - center_idx
    delta_sec = delta_samples / ENV_SR

    # Peak normalizado: pico / norma (heurística de calidad).
    peak_val = float(window_corr[peak_local])
    # Normalización por L2-norm de ambos envelopes (correlación normalizada)
    v_norm = float(np.linalg.norm(v_env))
    a_norm = float(np.linalg.norm(a_env))
    peak_norm = peak_val / (v_norm * a_norm + 1e-9)

    # Heurística de confianza: prominence del peak vs second-best
    secondary_mask = np.ones_like(window_corr, dtype=bool)
    half = max(int(0.2 * ENV_SR), 5)  # 200ms a cada lado del peak
    secondary_mask[max(0, peak_local - half):peak_local + half + 1] = False
    if secondary_mask.any():
        runner_up = float(np.max(window_corr[secondary_mask]))
        prominence = (peak_val - runner_up) / (peak_val + 1e-9)
    else:
        prominence = 1.0
    confidence = float(np.clip(0.5 + prominence * 0.5, 0.0, 1.0))

    # `delta_sec` es cuanto se aparta el pico del centro asumido, y el centro se
    # asumio en `v_start - current_offset`. Despejando:
    #     p_medido = current_offset - offset_real + window
    #     delta    = p_medido - window = current_offset - offset_real
    # => offset_real = current_offset - delta.  (Iba con `+` hasta 2026-08-13.)
    new_offset = current_offset - delta_sec
    return RefineResult(
        delta_sec=delta_sec,
        new_offset=new_offset,
        correlation_peak=peak_norm,
        confidence=confidence,
        notes=f"prominence={prominence:.3f}, peak_norm={peak_norm:.3f}",
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("Uso: python sync_refiner.py <video> <audio> <current_offset>")
        sys.exit(1)
    vp = Path(sys.argv[1])
    ap = Path(sys.argv[2])
    co = float(sys.argv[3])

    # Sacar duración con ffprobe
    def get_dur(path):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=nw=1:nk=1",
                            str(path)], capture_output=True, text=True)
        return float(r.stdout.strip())
    vd = get_dur(vp)
    ad = get_dur(ap)
    print(f"Video: {vp.name} ({vd:.1f}s)")
    print(f"Audio: {ap.parent.name}/{ap.name} ({ad:.1f}s)")
    print(f"Offset actual: {co:+.3f}s")

    r = refine_offset(vp, ap, co, vd, ad)
    if r is None:
        print("No se pudo refinar (audio insuficiente o cross-correlation débil)")
        sys.exit(1)
    print(f"Δ = {r.delta_sec*1000:+.0f} ms")
    print(f"Nuevo offset = {r.new_offset:+.3f}s")
    print(f"Peak normalizado: {r.correlation_peak:.3f}")
    print(f"Confianza: {r.confidence:.2f}")
    print(f"Notas: {r.notes}")
