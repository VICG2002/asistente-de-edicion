---
name: instalar-motor-edicion
description: Instala o actualiza el motor local del asistente de edición de Diez50 en esta Mac (scripts Python a ~/cinema-assistant, dependencias Homebrew/pip, modelo Whisper y doctrina inicial). Invocar cuando el usuario diga "instala el asistente de edición", "instala el motor de edición", "configura el asistente de edición en esta máquina", o cuando el skill asistente-de-edicion detecte que ~/cinema-assistant no existe.
---

# Instalar el motor de edición

Bootstrap del asistente de edición en una Mac nueva. Todo local y gratuito.

## Pasos

1. Ejecutar el bootstrap (idempotente — se puede re-correr para actualizar):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/instalar-motor-edicion/scripts/bootstrap.sh"
```

   El script:
   - Verifica/instala dependencias: `ffmpeg`, `mediainfo`, `exiftool`,
     `whisper-cpp`, `lua` (vía Homebrew) + `numpy`, `Pillow`, `zstandard` (pip).
   - Copia el motor del plugin a `~/cinema-assistant/` (bin, lib, resolve).
     Si ya existe, respalda lo local en `~/cinema-assistant/_backup-<fecha>/`
     antes de sobreescribir bin/lib.
   - Descarga el modelo Whisper `ggml-large-v3-turbo.bin` (~1.6 GB, una vez)
     a `~/cinema-assistant/models/`.
   - Siembra la doctrina editable en `~/memoria-asistente-edicion/`
     (metodología + lecciones) SOLO si no existe — nunca pisa la local.

2. Verificar la instalación:

```bash
python3 ~/cinema-assistant/bin/lint_pipeline.py && \
  ls -la ~/cinema-assistant/models/ggml-large-v3-turbo.bin && \
  lua -v
```

3. Reportar al usuario qué se instaló, qué ya estaba, y el siguiente paso:
   conectar el disco del proyecto e invocar el skill `asistente-de-edicion`.

## Notas

- Requiere macOS con Homebrew. Si no hay Homebrew, indicarlo al usuario
  (instalación: https://brew.sh) — no instalarlo sin su ok.
- Fases avanzadas (caras InsightFace, diarización, PANNs) usan un venv
  aparte (`~/cinema-assistant/.venv/`); se instalan bajo demanda cuando un
  proyecto las necesita, no en el bootstrap.
- La descarga del modelo tarda según la conexión; correr en background y
  avisar progreso.
