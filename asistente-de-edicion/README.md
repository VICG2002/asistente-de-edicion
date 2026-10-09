# Asistente de edición (Diez50)

Plugin de Claude Code/Cowork que empaqueta el asistente de edición
documental del colectivo Diez50: indexado del material, transcripción
local (Whisper), sync multi-señal (transcript + reloj + waveform),
multicám verificado, curaduría de transcript y construcción de timelines
en **DaVinci Resolve Free** vía Consola Lua.

**Filosofía**: 100 % local y gratuito (sin APIs de paga, sin subir
material), garantías verificables (verifiers que fallan, no promesas), y
*la IA ejecuta, el editor decide*.

## Qué incluye

| Pieza | Qué es |
|---|---|
| Skill `asistente-de-edicion` | Punto de entrada: reglas duras, protocolo de arranque, doctrina como referencias |
| Skill `instalar-motor-edicion` | Bootstrap en una Mac nueva (motor + dependencias + modelo Whisper + doctrina inicial) |
| `engine/` | El motor: 153 archivos (110 en `bin/`, 43 en `lib/`) + `config/` + base Lua para Resolve |
| `references/` | Metodología completa (playbook, sync, curaduría…) + lecciones técnicas de proyectos reales |

## Instalación (Mac nueva)

1. En Claude Code (en la terminal o en la app de escritorio), agrega el
   marketplace de Diez50 e instala el plugin:

   ```
   /plugin marketplace add VICG2002/asistente-de-edicion
   /plugin install asistente-de-edicion@diez50
   ```

   Reinicia Claude Code para que lo cargue. Si tu app instala plugins desde
   un archivo, el `.plugin` de cada versión está en los Releases del repo.
2. Di: **"instala el motor de edición"** — corre el bootstrap (necesita
   Homebrew; descarga el modelo Whisper de ~1.6 GB una sola vez).
3. Conecta el disco del proyecto y di: **"asistente de edición: arranca
   con este proyecto"**.

## Capas (qué vive dónde)

- `~/cinema-assistant/` — motor (lo instala/actualiza este plugin).
- `~/memoria-asistente-edicion/` — doctrina **local y editable**: el
  bootstrap la siembra una vez y después es tuya; lo que el asistente
  aprende contigo se escribe ahí, nunca en el plugin.
- `<disco>/.cinema_assistant/` — datos por proyecto (manifest, transcripts,
  reports). Viajan con el disco del proyecto.

## Requisitos

macOS (Apple Silicon recomendado), Homebrew, DaVinci Resolve Free o Studio,
~2 GB de disco para el modelo Whisper.

**Resolve 21.1 (septiembre de 2026)** pasó el Python y el MCP a Studio, y según
encierra los scripts de Free en un sandbox. Medido aquí: Studio 21.1.0 build
17 el 1 de octubre, y Free 21.1.0.17 y 21.1.1.10 el 5 de octubre. En Free no hay
`io`, `require` ni `debug`, ni en el menú ni en la Consola; el API de Resolve es
el mismo que en Studio, y `Timeline:Export` escribe su archivo. El diagnóstico que
lo mide viene en el motor (`bin/preparar_diagnostico.py`).

## Qué NO hace

- No exige funciones Studio-only de Resolve: la integración es Lua, la misma
  en Free y en Studio. En Studio, `bin/aplicar_en_resolve.py` corre ese mismo
  Lua sin pegar la línea en la Consola; es opcional.
- No arma las timelines desde el menú de Resolve sin Claude. Ese plugin
  (`resolve/aplicar.lua` y `resolve/construir.lua`, en Workspace > Scripts)
  viaja en el motor, pero está en pruebas: todavía no es para uso general.
- No borra, mueve ni renombra fuentes. Jamás.
- No manda material ni transcripts a internet. La única salida de red del motor
  es `localhost:11434` (Ollama, si lo usas para describir tramos).

Con un asterisco, para ser exactos: el modelo de Whisper se descarga una vez
durante la instalación, verificado por SHA-256. Y las funciones **opcionales**
de detección de caras y pose (`detect_faces.py`, `analyze_segment_pose.py`)
descargan sus modelos la primera vez que se usan, sin checksum, porque los
gestionan `insightface` y `ultralytics`. No sale nada tuyo; entra código de
terceros. El resto del pipeline no toca la red.

## Sobre la doctrina incluida

Las `references/` son metodología real, destilada de rodajes documentales
reales. Los nombres de las personas que participaron están **anonimizados**: el
empaquetado los sustituye por seudónimos estables y el build falla si alguno se
escapa. Los ejemplos se leen igual; las personas no quedan expuestas.

## Licencia

MIT — ver [LICENSE](../LICENSE). Los modelos de terceros que el motor descarga
(Whisper, y opcionalmente insightface/ultralytics) tienen sus propias licencias
y no se distribuyen con este plugin.

---
v0.12.3 — Victor Correa, Diez50. Motor y doctrina nacidos en los proyectos
ESCALANDO MEXICO, Zezzions, Más Allá del Balón, Film Club Café, Fantástico
Cómics y The Avalanches.
