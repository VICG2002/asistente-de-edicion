#!/bin/bash
# Bootstrap del motor del asistente de edición (Diez50).
# Idempotente: instala lo que falta, respalda antes de actualizar,
# NUNCA toca datos por proyecto ni la doctrina propia del usuario.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
ENGINE_SRC="$PLUGIN_ROOT/engine"
REFS_SRC="$PLUGIN_ROOT/skills/asistente-de-edicion/references"
DEST="$HOME/cinema-assistant"
DOCTRINA="$HOME/memoria-asistente-edicion"
DEL_PLUGIN="$DOCTRINA/_del-plugin"
MODEL="$DEST/models/ggml-large-v3-turbo.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
# SHA-256 verificado contra la cabecera x-linked-etag de HuggingFace (2026-07-27).
# No es "lo que me tocó bajar": es lo que publica el origen.
MODEL_SHA256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
BACKUPS_A_CONSERVAR=2

# Subdirectorios del motor. config/ es obligatorio: bin/index_project.py,
# lib/manifest.py, lib/probe.py y lib/classify.py hacen `from config import
# defaults`. Sin él, el indexado revienta con ModuleNotFoundError.
# tools/build-plugin.sh verifica que esta lista y la suya coincidan.
ENGINE_SUBDIRS=(bin lib config)

echo "== Bootstrap asistente de edición (Diez50) =="
[ -d "$ENGINE_SRC" ] || { echo "ERROR: no encuentro el motor en $ENGINE_SRC"; exit 1; }

# ---------------------------------------------------------------------------
# 1. Dependencias
# ---------------------------------------------------------------------------
# OJO: el binario que instala un paquete NO siempre se llama como el paquete.
# `whisper-cpp` instala `whisper-cli`. Antes esto se resolvía recortando el
# nombre en el primer guión, así que buscaba `whisper` — y en una Mac con el
# `whisper` de Python (el de OpenAI, otro programa) la comprobación pasaba, se
# saltaba la instalación, y el motor fallaba después. Mapa explícito:
DEPS=(
  "ffmpeg:ffmpeg"
  "mediainfo:mediainfo"
  "exiftool:exiftool"
  "whisper-cpp:whisper-cli"
  "lua:lua"
)

if ! command -v brew >/dev/null; then
  echo "ERROR: falta Homebrew (https://brew.sh). Instálalo y re-corre."; exit 1
fi
for dep in "${DEPS[@]}"; do
  pkg="${dep%%:*}"; bin="${dep##*:}"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "-- brew install $pkg  (provee '$bin')"; brew install "$pkg"
  fi
done

# Verificación final: si el binario que el motor USA de verdad no aparece, hay
# que fallar aquí y no dentro de tres horas de transcripción.
faltantes=()
for dep in "${DEPS[@]}"; do
  bin="${dep##*:}"
  command -v "$bin" >/dev/null 2>&1 || faltantes+=("$bin")
done
if [ ${#faltantes[@]} -gt 0 ]; then
  echo "ERROR: tras instalar, estos binarios siguen sin aparecer: ${faltantes[*]}"
  echo "       El motor los necesita. Revisa la salida de brew y re-corre."
  exit 1
fi

# Dependencias Python en un venv PROPIO, no en el Python del sistema.
#
# Antes esto era `pip install --user`, y si fallaba reintentaba con
# `--break-system-packages`, que hace exactamente lo que su nombre dice: mete
# paquetes en el Python de macOS/Homebrew y puede romper otras herramientas de la
# maquina. Instalar dependencias de un asistente de edicion no es motivo para
# tocar el Python del sistema de nadie.
#
# Ademas el motor ya asumia este venv: bin/build_voice_catalog.py,
# lib/sound_events.py y lib/voice_embeddings.py documentan
# "~/cinema-assistant/.venv/bin/python" — un entorno que el bootstrap nunca
# creaba. Ahora existe.
#
# El NUCLEO del pipeline (indexar, transcribir, sincronizar, hornear) solo usa la
# stdlib mas numpy y Pillow, asi que sigue corriendo con `python3` normal: no hay
# que cambiar ningun comando del playbook. El venv es para lo pesado y opcional.
VENV="$DEST/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "-- creando entorno virtual en $VENV"
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet numpy Pillow zstandard
echo "-- dependencias Python en el venv (el Python del sistema queda intacto)"

# numpy y Pillow tambien hacen falta para el `python3` normal, que es con el que
# corre el nucleo. Se instalan con --user, SIN --break-system-packages: si el
# entorno no lo permite, se avisa en vez de forzar.
if ! python3 -c "import numpy, PIL" >/dev/null 2>&1; then
  python3 -m pip install --user --quiet numpy Pillow zstandard 2>/dev/null || {
    echo "   AVISO: no pude instalar numpy/Pillow para el python3 del sistema."
    echo "          El nucleo del pipeline los necesita. Opciones:"
    echo "            python3 -m pip install --user numpy Pillow zstandard"
    echo "            (o usa $VENV/bin/python para correr los scripts)"
  }
fi

# ---------------------------------------------------------------------------
# 2. Motor
# ---------------------------------------------------------------------------
mkdir -p "$DEST"
if [ -d "$DEST/bin" ]; then
  BK="$DEST/_backup-$(date +%Y%m%d-%H%M%S)"
  echo "-- respaldo del motor actual en $BK"
  mkdir -p "$BK"
  for sub in "${ENGINE_SUBDIRS[@]}"; do
    [ -d "$DEST/$sub" ] && cp -R "$DEST/$sub" "$BK/" 2>/dev/null || true
  done
  # Podar: conservar solo los N más recientes. Antes se acumulaban para siempre,
  # una copia completa del motor por cada actualización.
  # shellcheck disable=SC2012
  ls -1dt "$DEST"/_backup-* 2>/dev/null | tail -n +$((BACKUPS_A_CONSERVAR + 1)) \
    | while read -r viejo; do echo "-- borrando respaldo viejo $(basename "$viejo")"; rm -rf "$viejo"; done
fi

# rsync --delete, no cp -R: si un script se borró en la fuente tiene que
# desaparecer de la instalación, no sobrevivir escondido.
for sub in "${ENGINE_SUBDIRS[@]}"; do
  [ -d "$ENGINE_SRC/$sub" ] || { echo "ERROR: el plugin no trae engine/$sub"; exit 1; }
  mkdir -p "$DEST/$sub"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$ENGINE_SRC/$sub/" "$DEST/$sub/"
done

mkdir -p "$DEST/resolve" "$DEST/models" "$DEST/logs"
# resolve/ tiene DOS clases de archivo y se tratan distinto:
#
#  1. Los del PLUGIN (lista de abajo): se SOBREESCRIBEN siempre. asistente_lib.lua
#     es la lógica compartida de markers y pistas que cargan los scripts de
#     proyecto; si no se actualizara, una Mac con la v0.1.6 se quedaría con la
#     librería vieja para siempre y los markers nuevos no aparecerían — el mismo
#     fallo silencioso que el config/ ausente de la v0.1.3.
#
#  2. Todo lo demás (asistente_<proyecto>.lua, *_data.lua, *_multicam.lua):
#     es del usuario. Aditivo, nunca se pisa.
# Tiene que ser EXACTAMENTE la RESOLVE_ALLOWLIST de tools/build-plugin.sh, que
# lo comprueba al empaquetar. Hasta el 2026-10-05 faltaban reel_subtitulado.lua,
# leer_sync_merge.lua y auditar_settings.lua: se trataban como del usuario y una
# Mac que actualizaba se quedaba con la version vieja.
RESOLVE_DEL_PLUGIN=(
  asistente_lib.lua
  merge_pool.lua
  leer_sync_merge.lua
  restaurar_bins.lua
  spikes_v2.lua
  spike_merge.lua
  spike_merge2.lua
  mock_resolve_smoke.lua
  mock_resolve_full.lua
  reel_subtitulado.lua
  auditar_settings.lua
  diagnostico.lua
  lavas_al_corte.lua
  aplicar.lua
  construir.lua
)
es_del_plugin() {
  local n="$1" x
  for x in "${RESOLVE_DEL_PLUGIN[@]}"; do [ "$x" = "$n" ] && return 0; done
  return 1
}
n_act=0; n_new=0; n_resp=0
for f in "$ENGINE_SRC/resolve/"*; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  if es_del_plugin "$base"; then
    if [ -e "$DEST/resolve/$base" ] && ! cmp -s "$f" "$DEST/resolve/$base"; then
      n_act=$((n_act+1))
    elif [ ! -e "$DEST/resolve/$base" ]; then
      n_new=$((n_new+1))
    fi
    cp "$f" "$DEST/resolve/$base"
  else
    if [ -e "$DEST/resolve/$base" ]; then n_resp=$((n_resp+1)); else cp "$f" "$DEST/resolve/"; fi
  fi
done
echo "-- motor instalado en $DEST"
echo "   resolve/: $n_new nuevo(s), $n_act actualizado(s) del plugin, $n_resp tuyo(s) respetado(s)"

# ---------------------------------------------------------------------------
# 3. Modelo Whisper (1.6 GB, una sola vez) — con verificación
# ---------------------------------------------------------------------------
verificar_modelo() {
  [ -s "$1" ] || return 1
  local real; real="$(shasum -a 256 "$1" | awk '{print $1}')"
  [ "$real" = "$MODEL_SHA256" ]
}

if verificar_modelo "$MODEL"; then
  echo "-- modelo Whisper ya presente y verificado"
else
  if [ -e "$MODEL" ]; then
    echo "-- el modelo presente NO coincide con el checksum esperado; se vuelve a bajar"
    mv "$MODEL" "$MODEL.corrupto-$(date +%s)"
  fi
  echo "-- descargando modelo Whisper turbo (~1.6 GB)..."
  TMP_MODEL="$MODEL.parcial"
  # --continue-at - reanuda una descarga cortada en vez de empezar de cero.
  curl -L --fail --progress-bar --continue-at - -o "$TMP_MODEL" "$MODEL_URL"
  if verificar_modelo "$TMP_MODEL"; then
    mv "$TMP_MODEL" "$MODEL"
    echo "-- modelo descargado y verificado"
  else
    rm -f "$TMP_MODEL"
    echo "ERROR: el modelo descargado no coincide con el checksum esperado."
    echo "       Esperado: $MODEL_SHA256"
    echo "       Se borró la descarga. Vuelve a correr el bootstrap."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 4. Doctrina — dos capas
# ---------------------------------------------------------------------------
#   metodologia/ y lecciones/  -> TUYAS. No se tocan jamás.
#   _del-plugin/               -> copia de referencia, se refresca SIEMPRE.
#
# Antes la doctrina se sembraba una sola vez y nunca más: quien instalaba el
# plugin no volvía a recibir una lección nueva aunque actualizara. Ahora las
# novedades llegan a _del-plugin/ y el usuario decide qué incorporar.
if [ ! -d "$DOCTRINA" ]; then
  mkdir -p "$DOCTRINA/metodologia" "$DOCTRINA/lecciones"
  for f in "$REFS_SRC/"*.md; do
    base="$(basename "$f")"
    case "$base" in
      patrones-exitosos.md|lo-que-no-hacer.md|errores-comunes-a-corregir.md)
        cp "$f" "$DOCTRINA/lecciones/" ;;
      *) cp "$f" "$DOCTRINA/metodologia/" ;;
    esac
  done
  cat > "$DOCTRINA/lecciones/preferencias-del-usuario.md" <<'EOF'
# Preferencias del usuario

(Plantilla vacía: aquí el asistente registra CÓMO prefieres trabajar —
idioma, estilo de decisiones, convenciones propias. Se llena con el uso.)
EOF
  echo "-- doctrina sembrada en $DOCTRINA (editable, es tuya)"
else
  echo "-- doctrina local ya existe: no se toca"
fi

mkdir -p "$DEL_PLUGIN"
rsync -a --delete "$REFS_SRC/" "$DEL_PLUGIN/"
cat > "$DEL_PLUGIN/LEEME.md" <<'EOF'
# Copia de referencia del plugin — NO editar

Este directorio se **sobrescribe completo** en cada instalación o
actualización del plugin. Cualquier cambio que hagas aquí se pierde.

Tu doctrina es la de `../metodologia/` y `../lecciones/`: esa el instalador
no la toca nunca.

Esta copia existe para que puedas ver qué trae el plugin que tú todavía no
tienes. Para consultarlo:

    python3 ~/cinema-assistant/bin/doctrina_novedades.py

Es un aviso, no una fusión: tú decides qué incorporar y qué no.
EOF
echo "-- referencia del plugin actualizada en $DEL_PLUGIN"

# ---------------------------------------------------------------------------
# 5. Novedades
# ---------------------------------------------------------------------------
if [ -f "$DEST/bin/doctrina_novedades.py" ]; then
  echo
  python3 "$DEST/bin/doctrina_novedades.py" || true
fi

echo
echo "== Listo. Verifica con: python3 $DEST/bin/lint_pipeline.py =="
