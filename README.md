# Asistente de edición — Diez50

Un plugin de [Claude Code](https://claude.com/claude-code) para la asistencia de
edición documental en **DaVinci Resolve**. Es el que usamos en
[Diez50](https://www.youtube.com/@Diez50), un colectivo de cine de la Ciudad de
México, para cada video que sacamos: del disco del rodaje a las timelines listas
para empezar a editar.

**La IA ejecuta, el editor decide.** El asistente hace el trabajo mecánico; qué se
queda en el corte lo decides tú.

## Qué hace

- **Ordena el material:** un inventario de cada clip y cada audio del rodaje.
- **Transcribe todo en tu Mac**, con Whisper: las cámaras y los micrófonos de
  solapa.
- **Sincroniza los lavalieres con las cámaras** de tres maneras: por las palabras,
  por la hora de grabación y por la forma de la onda. Si no coinciden, no adivina:
  te pregunta y lo marca.
- **Arma las timelines en Resolve:** entrevistas (A-roll), material de apoyo
  (B-roll), audios externos y la cronología completa del rodaje. También reels,
  subtítulos y corte de silencios.
- **Revisa su propio trabajo:** antes de entregarte nada, corre verificaciones que
  fallan en voz alta, y después le pregunta a Resolve qué quedó.

Habla en español y está pensado para documental: entrevistas, coberturas y
rodajes con varias cámaras.

## Qué necesitas

- Una **Mac** (Apple Silicon recomendado). El motor solo corre en macOS.
- **Claude Code**, en la terminal o en la app de escritorio, con el **plan Pro de
  Claude** o uno superior. Claude Code no viene en el plan gratuito.
- [**Homebrew**](https://brew.sh).
- **DaVinci Resolve**, Free o Studio. Probado en la 21.1.
- Unos 2 GB de disco para el modelo de Whisper.

## Instalación

1. En Claude Code, agrega el marketplace de Diez50 e instala el plugin:

   ```
   /plugin marketplace add VICG2002/asistente-de-edicion
   /plugin install asistente-de-edicion@diez50
   ```

   Reinicia Claude Code para que lo cargue. Si tu app instala plugins desde un
   archivo, el `.plugin` de cada versión está en
   [Releases](https://github.com/VICG2002/asistente-de-edicion/releases).

2. Dile: **"instala el motor de edición"**. Instala el motor en tu Mac, sus
   dependencias de Homebrew y el modelo de Whisper (1.6 GB, se descarga una sola
   vez).

3. Conecta el disco del proyecto y dile: **"asistente de edición: arranca con este
   proyecto"**. También puedes usar los comandos:

   | Comando | Qué hace |
   |---|---|
   | `/asistente-de-edicion:edicion` | Arranca el asistente sobre un proyecto |
   | `/asistente-de-edicion:edicion-estado` | Dice en qué punto quedó, sin escribir nada |
   | `/asistente-de-edicion:edicion-verificar` | Corre las verificaciones |
   | `/asistente-de-edicion:edicion-resolve` | Te da la línea lista para la Consola de Resolve |

## Cómo se conecta con Resolve

- **En Resolve Free:** la versión gratuita no deja que otro programa la controle.
  El asistente prepara todo y te da una línea para pegar en la Consola
  (Workspace > Console); esa línea arma las timelines.
- **En Resolve Studio:** puede hacer lo mismo por el API, sin que pegues nada.
  Es opcional.

## Lo que todavía no está

**El plugin para el menú de Resolve**, que armaría las timelines sin Claude, desde
Workspace > Scripts. Ya funciona en pruebas y sus archivos viajan en el motor
(`resolve/aplicar.lua` y `resolve/construir.lua`), pero todavía no es para uso
general.

## Privacidad

Todo corre en tu Mac. No sube material ni transcripciones a ningún lado, y no usa
APIs de paga. Lo único que entra de internet:

- el modelo de Whisper, una vez, durante la instalación, verificado por SHA-256;
- si usas las funciones opcionales de caras y pose, sus modelos, que descargan
  `insightface` y `ultralytics` la primera vez.

Si usas Ollama para describir tramos, el motor le habla en `localhost`.

La doctrina que trae el plugin salió de rodajes reales de Diez50. Los nombres de
las personas que aparecen en ellos están cambiados por seudónimos.

## Contacto

- Correo: diezcincuentastudios@gmail.com
- Instagram: [@diez50_](https://www.instagram.com/diez50_/)
- YouTube: [@Diez50](https://www.youtube.com/@Diez50)
- Si algo falla, abre un
  [issue](https://github.com/VICG2002/asistente-de-edicion/issues).

## Licencia

MIT: ver [LICENSE](LICENSE). Los modelos de terceros que el motor descarga
(Whisper y, si los usas, los de insightface y ultralytics) tienen sus propias
licencias y no vienen en este repositorio.
