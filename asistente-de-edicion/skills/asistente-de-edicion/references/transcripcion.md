# Transcripción

## Herramienta

`whisper.cpp` vía Homebrew:
```bash
brew install whisper-cpp
```

Da `whisper-cli`, `whisper-vad-speech-segments`, `whisper-bench`, etc.
En Apple Silicon usa Metal automáticamente.

## Modelos disponibles localmente (2026-05-27)

El sistema tiene **dos modelos descargados** que distintos consumidores eligen según necesidad:

| Modelo | Tamaño | Uso recomendado | Ubicación |
|---|---|---|---|
| `ggml-small.bin` | 487 MB | Transcripciones rápidas, backfill masivo, contenido de YouTube auto-captioneable | `~/.cache/whisper-models/ggml-small.bin` |
| `ggml-large-v3-turbo.bin` | 1.5 GB | Entrevistas documentales, material con jerga regional, ground truth | `~/cinema-assistant/models/ggml-large-v3-turbo.bin` (cuando esté descargado) |

**Recomendación para edición documental (este asistente):** `large-v3-turbo` — el sweet spot velocidad/calidad para español. Detección de jerga, acentos regionales, nombres propios.

**Recomendación para backfill rápido (memoria-creativa pipeline YouTube):** `small` — suficiente para descripción de contenido + extracción de temas.

```bash
mkdir -p ~/cinema-assistant/models
curl -L --fail \
  -o ~/cinema-assistant/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

# Small (ya descargado el 2026-05-27 para memoria-creativa):
mkdir -p ~/.cache/whisper-models
curl -L --fail \
  -o ~/.cache/whisper-models/ggml-small.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
```

Modelos más chicos (`small`, `medium`) son más rápidos pero pierden
nombres propios, jerga y acentos regionales. Para entrevistas en
español de Latinoamérica, `large-v3-turbo` vale la pena. Para indexar
contenido público (YouTube auto-uploads de archivo formativo), `small`
basta para que la memoria capture temas y diálogos clave.

## Cross-asistente — pipeline memoria-creativa

A partir del 2026-05-27, **la memoria-creativa también usa Whisper** para
transcribir videos del canal de YouTube del autor (Victor) cuando YouTube
no provee auto-captions. Flujo establecido:

```bash
# 1. Descargar audio (yt-dlp ya instalado vía pip user + homebrew)
yt-dlp -x --audio-format wav --output "%(upload_date)s_%(id)s_%(title)s.%(ext)s" \
    "https://www.youtube.com/watch?v=<ID>"

# 2. Transcribir con small (rápido) — para inventario
whisper-cli -m ~/.cache/whisper-models/ggml-small.bin \
    -l es --output-srt --output-txt --output-file <out> <wav>

# 3. (Opcional) Re-transcribir críticos con large-v3-turbo — para análisis
whisper-cli -m ~/cinema-assistant/models/ggml-large-v3-turbo.bin \
    -l es --output-srt --output-txt --output-file <out>_large <wav>
```

Resultados se guardan en `~/memoria-creativa/sesiones/youtube-transcripciones/`
(no en `<disco>/.cinema_assistant/` que es para proyectos de edición activa).

## Wrapper

`lib/transcribe.py::transcribe(media_path, model_path, lang="es")` →
`{text, words: [(word, t_sec), ...]}` o `None`.

Internamente:
1. `ffmpeg` decodifica el primer stream de audio a WAV 16 kHz mono
   PCM 16-bit (lo que come whisper-cli).
2. `whisper-cli -m <model> -f <wav> -l es -ml 1 -sow -oj -of <out>`
   → JSON con **un segmento por palabra** (gracias a `-ml 1 -sow`),
   cada uno con `offsets.from`/`to` en milisegundos.
3. Parsea cada segmento como `(palabra, t_sec)`.

Parámetros importantes:
- `-ml 1` — max segment length 1 char → fuerza un segmento por palabra.
- `-sow` — split on word boundary.
- `-oj` — output JSON.
- `-np` — no prints (silencia el stdout para logs limpios).

## Velocidad

~14× tiempo real en M-series con Metal y `large-v3-turbo`:
- 6 h de audio → ~25 min de cómputo.
- Carga del modelo: ~2-3 s por invocación de whisper-cli. Con cientos
  de archivos cortos, eso suma; vale la pena agrupar.

## Caching

`<disco>/.cinema_assistant/transcripts/<clip_id>.json`.

Para clips sin habla, guardar un transcript vacío `{text:"", words:[]}`
en lugar de borrar — así no se re-transcriben en re-corridas.

## Alucinaciones de Whisper

Whisper inventa texto cuando el audio no tiene habla real. Lo más común
en español:
- `"¡Suscríbete al canal!"` repetido — datos de YouTube en su training.
- `"Gracias. Gracias. Gracias..."` repetido — final típico de un video.
- `"No, no, no, no..."`.

**Filtro:** contar palabras distintas (normalizadas: lowercase + sin
acentos + sin puntuación):
```python
distinct = len({normalize(w) for w, _ in words} - {""})
if distinct >= 60:
    # contenido real
```

60 es un buen umbral en proyectos documentales — una entrevista corta
de 1 min ya supera eso fácil; un clip sin habla suele quedar < 30.

## Wrappers de batch

- `bin/transcribe_clips.py --location <sub> --cameras main` — clips de
  video, solo cámara principal (donde hay diálogo).
- `bin/transcribe_audios.py` — audio externo bajo `AUDIOS/`. Salta
  archivos `_Tr[0-9]*` (pistas duplicadas de mixes multi-track).

Ambos cachean por `clip_id`.

## VAD (Voice Activity Detection)

Para clasificación pre-transcripción, `whisper-vad-speech-segments` es
mucho más rápido que la transcripción completa. Útil para construir el
catálogo de audio (detectar si un archivo tiene habla o no).
