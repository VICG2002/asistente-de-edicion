# Lo que NO hacer

Lista negra. Nunca.

## Principio rector (transversal)

- **IA ejecuta, no decide.** Las decisiones creativas —qué clip se queda, qué se corta, qué orden cuenta la historia, el corte final— las toma el humano; el asistente indexa, transcribe, sincroniza, analiza y **propone**. Desarrollo en `~/memoria-asistente-compartido/principio-rector.md`.

## Fuentes (video, audio)

- **NO borrar** archivos del disco del proyecto.
- **NO mover ni renombrar** fuentes.
- **NO modificar metadatos** de las fuentes (exiftool de escritura, etc.).
- **NO tocar** el audio original embebido del clip — **A1 intocable**. El
  audio externo va a A2 con su offset.

## Resolve

- **NO usar features Studio-only** en lo que se distribuye: Magic Mask, Neural
  Engine, API externa de scripting (`scriptapp("Resolve")` devuelve None en
  Free), y desde 21.1 también el **servidor MCP** y el **scripting en Python**.
  El motor y los horneados tienen que seguir sirviendo en una Mac con Free.
- **NO proponer el MCP nativo de Resolve** como si estuviera al alcance en una
  instalación Free. Es de Studio, licencia perpetua de 295 USD, y en Free no
  aparece ni configurándolo. Proponerlo sin decir eso le hace perder el viaje
  al editor.
  **Excepción: la Mac de Victor.** Tiene Studio 21.1.0.17 desde el 2026-09-23,
  y desde el 2026-09-28 el MCP está registrado en Claude Code
  (`claude mcp add --scope user davinci-resolve -- ".../Contents/Applications/ResolveMCP"`).
  Ahí el camino directo es válido, con dos condiciones: lo que se construya así
  tiene que poder rehacerse en Free, y no se usa ninguna función Studio-only
  sin decirlo. Ver `metodologia/resolve-integracion.md`.
- **NO proponer FCPXML/AAF/EDL** round-trips — no funcionan en este setup
  (intentado, falló). La integración es **Lua Console**.
- **NO borrar timelines que NO empiecen con `ESC — `** sin confirmar.
  Pueden ser del usuario.
- **NO editar manualmente** la metadata del clip en el Media Pool a
  espaldas del usuario.

## Sync

- **NO sincronizar GoPro** con audio externo. Trae su propio audio embebido,
  no es dual-system.
- **NO confiar en TC sync** entre cámaras consumer y recorders consumer
  (relojes no sincronizados → coincidencia, no match real).
- **NO aceptar** un pair con confianza alta si la grabación es < 60 s
  (waveform produce falsos positivos con audio corto).
- **NO mezclar** convenciones de offset entre módulos. Convención única:
  `offset = audio_start - video_start`.

## Cámaras

- **NO asumir** una sola cámara principal en un proyecto multi-shoot.
- **NO clasificar** solo por filename (verificar con metadata + códec +
  resolución).
- **NO descartar ProRes** como "transcode" sin verificar (puede ser cámara
  cinema). Test: ¿hay un gemelo h264 con el mismo basename? Si no, es
  cámara nativa.

## Transcripción

- **NO transcribir pistas `_Tr`** duplicadas — solo mixes `_LR` y nombrados.
- **NO confiar** en transcripts cortos o repetitivos — Whisper alucina en
  silencio.
- **NO usar** transcripts con < 60 palabras distintas como evidencia de habla
  real.
- **NO asumir que >= 60 palabras distintas implica diálogo real.** Música
  alta encima del diálogo (audio de cámara en eventos) hace que Whisper
  produzca cientos de "palabras" alucinadas que pasan el filtro de densidad.
  Antes de marcar como entrevista o derivar preguntas, **correr
  `lib/transcript_quality.is_hallucinated()`** y descartar los degradados.
  Caso fundador documentado en `errores-comunes-a-corregir.md §5`.

## Indexación / Manifest

- **NO indexar tablas** keyed por filename (colisión silenciosa).
- **NO ignorar** carpetas duplicadas (`*repetido*`) sin filtrarlas en TODO
  el pipeline — y nunca borrarlas.
- **NO editar** `manifest.sqlite` a mano. Cambios vía scripts del motor.
- **NO descartar archivos por SHA-256 idéntico** entre carpetas L/R, Dr/Izq,
  A/B, o similares. El SHA dice "los bytes son los mismos hoy" — NO dice
  "el contenido editorial es el mismo". Indexar siempre por ruta absoluta,
  tratar cada ruta como candidato independiente para sync. Si huele a copia
  rota desde el grabador, avisarle al usuario con evidencia y dejar que él
  decida. Caso de Zezzions documentado en `errores-comunes-a-corregir.md §14`.

## Allowlist de permisos (`.claude/settings.json`)

- **NO allowlistear** intérpretes (`Bash(python3 *)`, `Bash(lua *)`,
  `Bash(node *)`) ni shells — equivalen a arbitrary code execution.
- **NO allowlistear** `Bash(sqlite3 *)` — arbitrary SQL incluye `.shell` que
  ejecuta comandos del sistema.
- **NO allowlistear** `Bash(ffmpeg *)` — escribe archivos.
- **NO allowlistear** `Bash(rm *)`, `Bash(cp *)`, `Bash(mv *)`, `Bash(brew
  install *)` — destructivos/mutativos.

## Stack tecnológico (directiva raíz: gratis)

- **NO usar servicios de paga** (Anthropic Claude API, OpenAI, Google
  Cloud Vision, AWS Rekognition, etc.). Si una solución sólo es viable
  con un servicio de paga, NO es viable para este proyecto — busca
  alternativa local o pasa.
- **NO mandar datos del proyecto a internet**. Material confidencial.
- **NO instalar dependencias que requieran build pesado** sin verificar
  alternativas pre-built primero (`dlib-bin` antes que `dlib`,
  `opencv-python-headless` antes que `opencv-python` con GUI).
- **NO dar por protegido un archivo solo porque este en `.gitignore`.** El
  `.gitignore` impide que entre a partir de ahora; **no saca lo que ya entro**.
  `_local/cast-jilotepec.json` —el reparto real de JILOTEPEC— llevaba desde el
  2026-07-31 en el `.gitignore` de la doctrina, con el comentario "si algun dia
  este repo recibe un remoto, no debe viajar". El 2026-08-26 el repo recibio
  remoto y el archivo seguia en dos commits del historial: `snapshot 2026-07-31`
  lo metio y `Leccion 53` lo saco, pero los blobs no se van solos. Antes de dar
  remoto a un repo que ha tenido datos personales, **auditar el historial
  completo** (`git rev-list --objects --all`), no solo el arbol. Se purga con
  `git filter-branch --index-filter 'git rm -r --cached --ignore-unmatch <path>'
  --prune-empty -- --all`, respaldo antes y `--force` al pushear.
- **NO mandar audio ni video a una API de transcripción de terceros**
  —ElevenLabs Scribe, Whisper por Groq, Whisper por OpenAI— por muy
  recomendada que venga en la documentación de otro proyecto. Dos
  herramientas de edición con agentes que se evaluaron el 2026-08-26
  (`bradautomates/claude-video` y `browser-use/video-use`) dan por sentado
  ese camino: la primera sube el audio extraído cuando no hay subtítulos
  nativos, la segunda exige una clave de ElevenLabs para arrancar. En un
  documental con personas reales delante de cámara eso no es una preferencia
  técnica: es material confidencial saliendo del disco. La transcripción de
  este motor es `whisper.cpp` local y se queda local.
- **NO "corregir" la doctrina con el anti-patrón ajeno de que "Whisper local
  es lento".** `video-use` lo lista literalmente como anti-patrón —*running
  Whisper locally on CPU*— y tiene razón en CPU. Aquí no corre en CPU: corre
  en Metal, a unas 14 veces tiempo real (6 h de audio en ~25 min de cómputo),
  con `large-v3-turbo`, que es lo que sostiene los nombres propios, la jerga y
  el acento latinoamericano. El consejo es correcto para otra máquina y falso
  para ésta. Antes de adoptar el anti-patrón de alguien, comprobar que su
  supuesto se cumple aquí.

## Eficiencia (directiva raíz: optimización constante)

- **NO procesar a escala antes del smoke test**. Pipeline sobre 1000+
  items requiere validar primero con 3-10 representativos.
- **NO descargar el modelo más grande "por si acaso"**. Empezar con el
  menor que pueda cumplir; subir solo si insuficiente.
- **NO re-procesar lo ya procesado** sin razón. Usa caches y skip-if-exists.
- **NO extraer 5 frames cuando 1 alcanza**. Usar pcts adaptivos
  según duración del tramo. Desde 2026-08-26 hay algo mejor que adivinar por
  posición: `--motor escena` reparte el presupuesto donde la imagen CAMBIA y
  tira los casi idénticos (`lib/frames.py`). Medido sobre un plano sostenido,
  9 frames colapsan a 1. Ver `metodologia/ver-el-material.md`.
- **NO mirar dos veces la misma imagen.** Dos vistas a menos de una ventana
  de distancia dan la misma imagen y cuestan el doble. Vale para los frames de
  un tramo y para las vistas de un entregable.
- **NO bake escalando_data.lua entero** si solo cambió 1 sección — pero
  por ahora es OK porque es rápido (<5s).

## Workflow

- **NO planificar eternamente** — el usuario quiere momentum. Decidir y
  ejecutar.
- **NO sobre-incluir** por bulk — mejor 7 syncs verificados que 45 dudosos.
- **NO empezar** un proyecto sin verificar pre-requisitos (Whisper, numpy,
  Pillow, modelo descargado).
- **NO terminar** una corrida sin guardar el proyecto en Resolve (`⌘+S`).
- **NO pedir confirmación** repetidamente para cosas que el usuario ya
  autorizó (el "continúa" / "arranca" cuenta hasta nuevo aviso).
