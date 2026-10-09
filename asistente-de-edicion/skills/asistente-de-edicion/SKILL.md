---
name: asistente-de-edicion
description: Asistente de edición documental para DaVinci Resolve Free (macOS, español). Invocar al iniciar un proyecto de edición, continuar uno en curso, o al trabajar con material de video, audio externo (lavalieres), transcripts, sync, multicám, subtítulos, reels, cápsulas, corte de silencios, behind the scenes, curaduría de transcript, cronología del rodaje o timelines en Resolve. Frases típicas — "hazme el sync", "indexa el proyecto", "transcribe el material", "arma las timelines", "hazme la timeline de B-roll", "ponlo en orden cronológico", "hazme los reels", "quítale los silencios", "separa el behind the scenes", "hazme los subtítulos de este export", "enséñame ese tramo", "mira el export antes de entregarlo", "asistente de edición".
---

# Asistente de edición (Diez50)

Asistente de edición documental para DaVinci Resolve **Free**, macOS, en
español. Todo el stack es **open source y 100 % local** — nada de APIs de
paga ni datos del proyecto a internet.

## Arquitectura de capas

1. **Motor** (`~/cinema-assistant/`): scripts Python + Lua reusables.
2. **Doctrina local** (`~/memoria-asistente-edicion/`): metodología +
   lecciones. La siembra el bootstrap desde `references/` de este skill;
   después es TUYA — lo aprendido en cada proyecto se escribe ahí.
   `_del-plugin/` es la copia de referencia del plugin: se sobrescribe en cada
   instalación y sirve para ver qué trae el plugin que tú no tienes.
3. **Datos por proyecto** (`<disco>/.cinema_assistant/`): manifest.sqlite
   (ground truth), transcripts, reports **y los horneados Lua**
   (`.cinema_assistant/resolve/<proyecto>_data.lua`). Nunca en el motor — el
   motor solo guarda el script `asistente_<proyecto>.lua`.

## Lo primero al invocarme

0. **Si `~/cinema-assistant/bin/` no existe** → correr primero el skill
   `instalar-motor-edicion` (bootstrap del motor + modelo Whisper).
1. Leer `~/memoria-asistente-edicion/metodologia/pasos-a-seguir.md` — el
   playbook end-to-end (copia de arranque en `references/pasos-a-seguir.md`).
2. Detectar el estado del proyecto activo: ¿existe
   `<disco>/.cinema_assistant/manifest.sqlite`? ¿Cuántos clips, transcripts,
   `audio_sync_pairs`? ¿Última corrida en `logs/`?
3. Correr `python3 ~/cinema-assistant/bin/verify_coverage.py --root "$DISK"
   --report-only` — si algún sector está atrás, ese es el siguiente trabajo.
   **En proyecto plano pasar `--project-prefix ''`**: con el default heredado
   el verificador mira cero material y da el proyecto por bueno (fue lo que
   dejó a Fantástico Cómics sin curaduría sin que nadie se enterara).
4. Correr `python3 ~/cinema-assistant/bin/doctrina_novedades.py` — si el
   plugin trae lecciones que la doctrina local no tiene, mencionarlas. Es un
   aviso: el editor decide qué incorporar.
5. **Puerta G0 del acta de conducta** — antes de tocar material:
   `python3 ~/cinema-assistant/bin/verify_asistente.py --root "$DISK"
   --project-prefix "$PREFIX" --puerta G0`. Es la quinta capa de
   verificación (ver "Las cuatro puertas" abajo); toma la línea base del
   proyecto y confirma que el prefijo y las capacidades de la máquina están
   resueltos antes de escribir una sola fila.
6. Arrancar desde donde quedó; si es proyecto nuevo, paso 1 del playbook.

## Reglas duras (resumen — detalle en references/lo-que-no-hacer.md)

- **NO borrar, mover ni renombrar fuentes** (video, audio). Jamás.
- **A1 = audio de cámara intocable**; el externo va en pistas propias,
  lavalieres SIEMPRE como últimas pistas.
- **Transcripts NUNCA llegan crudos a artefactos visibles** (markers,
  crónica, cast): Capa 2c de curaduría por comprensión — el audit
  estadístico no ve el garble plausible en nombres propios.
  Herramienta: `bin/surface_transcript_suspects.py`.
- **Sin duration markers**: solo marcadores de punto — Purple por pregunta
  (Name = entrevistado, nota = pregunta curada), Red/Yellow de estado,
  Cyan de sync.
- Sync por **contenido** (transcript) > reloj > waveform. El reloj
  IDENTIFICA, el audio CLAVA, el contenido CONFIRMA la escena, el
  conflicto ESCALA al humano.
- Resolve Free: integración SOLO vía Consola Lua (`dofile(...)`). Nada
  Studio-only.
- Indexar por **ruta absoluta**, no por filename.
- Offset: `audio_start − video_start` (negativo = audio empezó antes).

## Operación

- **Pipeline con verificación entre etapas**: si el proyecto tiene
  orquestador (`bin/run_full_pipeline.sh`, layout multi-sector), es el
  único entry-point. En proyectos planos: los pasos del playbook EN ORDEN,
  verificando cobertura entre etapas — nunca pasos sueltos sin documentar
  por qué.
- **Tareas pesadas** (transcribir horas, contact sheets, LLM batch) van en
  background con reporte de progreso (`Job X: n/target, ETA`). **NUNCA dos
  Whisper en paralelo** — comparten la GPU Metal y el largo se arrastra
  (lección 43).
- **NO allowlistear** en permisos: intérpretes (`python3 *`, `lua *`),
  `sqlite3 *`, `ffmpeg *`, `rm/cp/mv` — equivalen a ejecución arbitraria.
- Convenciones Resolve: bin del asistente `Timelines/asistente de edicion`;
  timelines con prefijo `<SIGLAS> — ` (em-dash) por proyecto; prefijos
  `NNN_` para forzar orden en bins.

## Novedades v0.12.2 (2026-10-07 — lavas en estéreo, y lo que la API no hace)

- **Las pistas `LAVA` van en estéreo**, con el WAV mono en los dos lados: un mono
  en una pista estéreo se oye de un solo lado. El mapeo se pone por item con
  `item:SetSourceAudioChannelMapping`; `SetAudioMapping` en el clip del bin no
  cambia los items que ya están en timelines. `lavas_al_corte.lua` todavía crea
  las pistas `LAVA` en mono: queda pendiente.
- **Lo que la API de Resolve no hace**: no renombra un bin (se crea otro y se le
  pasan clips y bins), no cambia el tipo de una pista que ya existe, y con un
  archivo importado dos veces `item:GetMediaPoolItem()` puede devolver la otra
  copia. Un duplicado se quita solo si las dos medidas dicen "sin uso".
- Solo cambia la doctrina: el motor es el de la v0.12.1. Detalle en
  `references/resolve-integracion.md`.

## Novedades v0.12.1 (2026-10-07 — la revisión de `construir`)

- **El orden cronológico es total** y ya no puede tumbar `construir` con skews
  grandes. La cobertura cuenta por clip, no por ruta.
- **El orden de las pistas LAVA lo da el `lavalier_tx` del `project_config.json`**,
  y el B-ROLL nombra sus pistas: la regla dura se comprueba también ahí.
- **Una timeline esperada sin material se dice en el reporte**, y
  `verificar_regreso.py` la da amarilla. Un clip encontrado por nombre (media
  movida) cuenta como presente, con un aviso. Lo que falta se explica cruzando
  nombres, no solo contando.
- Hay un solo recorrido del Media Pool por pedido, y con `dia` ya no se importan
  los WAV de otros días.

## Novedades v0.12.0 (2026-10-07 — el aplicador construye)

- **`construir`, la acción del aplicador del menú para proyectos documentales**
  (Fase 1 del plan de Resolve Free). Arma A-ROLL, B-ROLL por hora real y AUDIOS
  EXTERNOS desde el horneado, sin `io`, `os` ni `print`. Nunca borra una timeline:
  la aparta como `· anterior <sello>`. Vive en `engine/resolve/construir.lua`.
- **El pedido sale de `bin/pedido.py construir --root "$DISK" --proyecto <nombre
  exacto> [--dia AAAA-MM-DD]`.** El prefijo lo toma del `timeline_prefix` del
  `project_config.json`. El editor corre *Workspace › Scripts › Diez50 Aplicar*, y
  `bin/verificar_regreso.py --recibos <...>` comprueba desde el `.drt` que estén
  todos los clips y WAV del día.
- **`export_lua_data.py` escribe `<slug>_indice.json`** junto al horneado. Sin él no
  hay pedido: re-hornear.
- **El stub del menú pasa `motor`**: hay que volver a escribirlo con
  `bin/pedido.py stub`.
- **Probado** en Studio 21.1.0.17, donde da lo mismo que el script por proyecto, y
  en Free 21.1.1.10, en verde. Los proyectos comerciales siguen con su
  `asistente_<proyecto>.lua` hasta el siguiente incremento. Detalle en
  `references/resolve-integracion.md`.

## Novedades v0.11.1 (2026-10-05 — la revisión del PR del motor)

- **`LIB.isoEpoch` ya no pierde una hora en verano.** Con zona (`Z`, `±hh:mm`) el
  epoch es aritmética del calendario; antes, en zonas con horario de verano, las
  timelines por hora real quedaban una hora corridas. En México no se veía.
- **Un `project_config.json` que no se deja leer no se reescribe.** Antes, una coma
  de más hacía que la siguiente corrida lo dejara solo con `project_prefix`. Ahora
  el pipeline avisa y no lo toca.
- **El buzón lleva candado**, y el error que exporta el aplicador es uno por
  proyecto: `errores/error_<proyecto>.drt`. Detalle en
  `references/errores-comunes-a-corregir.md`.

## Novedades v0.11.0 (2026-10-05 — Free medido, y la ida y vuelta por el .drt)

Free 21.1 ya está medido (21.1.0.17 y 21.1.1.10, en una VM): hay sandbox en el
menú **y en la Consola** (sin `io`, `require`, `debug`), el API de Resolve es el
de Studio y `Timeline:Export` escribe su archivo. Lo que escribe con `io`
(`volcarLayouts`, `guardarOrigenBins`, `merge_pool`, `auditar_settings`,
`lavas_al_corte`) truena en Free también desde la Consola. Detalle y matriz en
`references/resolve-integracion.md`.

**1. El aplicador del menú, en prototipo.** `resolve/aplicar.lua` lee un buzón
de solo datos (`~/Library/Application Support/Diez50/buzon.lua`), toma el pedido
del proyecto abierto y lo cumple desde *Workspace › Scripts › Diez50 Aplicar*,
sin Consola. Nunca borra: lo anterior se renombra a `… · anterior <sello>`. Deja
una timeline de reporte `Diez50 · <sello> · <estado> · <n> de <m>`. Hoy conoce
`prueba_regreso` y `exportar`; `construir` llega en la Fase 1, así que los
proyectos se siguen aplicando con la línea de la Consola.

**2. El buzón se escribe solo con `bin/pedido.py`** (`lib/pedido.py`): valida
el pedido, lo escribe de forma atómica sin pisar los de otros proyectos y genera
el stub del menú con rutas absolutas. Nunca a mano.

**3. El canal de regreso es el `.drt`.** El aplicador exporta cada timeline y
su reporte con `Timeline:Export`, y `bin/verificar_regreso.py` los lee y da
verde, amarillo, rojo o pendiente, sin mirar Resolve. Corrió en verde en Free
21.1.1.10 el 5 de octubre.

## Novedades v0.10.0 (2026-10-05 — Resolve 21.1: Studio por el API, Free por medir)

Resolve 21.1 (septiembre de 2026) pasó el Python y el MCP a Studio, y según
terceros encerró los scripts del menú de Free en un sandbox: sin `io`, `os`,
`require`, `debug` ni `print` visible. Esta versión trae lo que se midió desde
entonces, y lo que no está medido en Free se dice así. Detalle en
`references/resolve-integracion.md`.

**1. En Studio el Lua llega solo a Resolve.** `bin/aplicar_en_resolve.py` corre
el MISMO aplicador por `resolve.Fusion().Execute`, sin pegar nada en la Consola,
y el veredicto vuelve por `fusion:SetData`. Solo Studio: Free no abre el API
externo. Lo que se construye así tiene que poder rehacerse en Free con la línea
de la Consola.

**2. El diagnóstico de la Fase 0.** `bin/preparar_diagnostico.py` arma un kit
(`Diez50 Diagnostico`, en el menú de scripts) que mide qué deja hacer un script
en ESA máquina: `resolve/diagnostico.lua` corre en un proyecto `DIEZ50_DIAG` y
`bin/leer_diagnostico.py` saca la matriz. Con `--kit-zip` viaja a otra Mac. En
Studio 21.1.0.17 (1 de octubre) el menú no tiene sandbox. **Free sigue sin
medir**, y nada de lo que funciona en Studio cuenta como probado en Free.

**3. Resolve rechaza `/ \ : * ? " < >` y la barra vertical en nombres de
timeline y de bin, sin dar error.** `CreateEmptyTimeline` y `AddSubFolder`
devuelven nil y `SetName` false. Todo nombre que lleve datos (una carpeta del
disco, un mensaje) se limpia antes.

**4. Studio 21.1 cambió `AppendToTimeline` sin avisar.** El `endFrame` es
exclusivo, y anexar sobre un rango ocupado sobrescribe. El motor lo calibra con
la primera pieza. Esa y otras diez lecciones del primer proyecto de dos días de
rodaje (TX con relojes distintos, cámara lenta, globals que viven entre
corridas) están en `references/errores-comunes-a-corregir.md`.

**5. `resolve/lavas_al_corte.lua`: los lavalieres debajo del corte del
editor.** Duplica su timeline y, por cada audio de cámara que el editor usó,
coloca el tramo de su lavalier en pistas nuevas al final. La del editor no se
toca.

**6. `restaurar_bins.lua` y `reel_subtitulado.lua` son módulos.** Cargarlos no
hace nada: `correr(ctx)` hace el trabajo, con la ruta de la librería y los datos
en `ctx`, en UNA línea de Consola (`references/roll.md`,
`references/reels-y-cortes.md`). En el reel, `hay_srt` lo pone quien arma la
línea después de mirar el disco: el script ya no lee archivos.

**7. El lint revisa el Lua.** `bin/lint_pipeline.py` prohíbe en `resolve/*.lua`
lo que el menú de Free no tiene (`io`, `os`, `require`, `package`, `debug`,
`ffi`) y los globals de Consola. La deuda conocida está congelada, cada una con
lo que la sustituye; lo nuevo tumba el lint.

## Novedades v0.9.0 (2026-08-26 — mirar el material, y mirar lo que sale)

Salió de evaluar dos herramientas de edición con agentes
(`bradautomates/claude-video` y `browser-use/video-use`) contra lo que este
motor ya hacía. Casi todo lo que se solapaba ya estaba aquí y mejor; entraron
cuatro cosas y se rechazaron por escrito las demás. Detalle completo en
`references/ver-el-material.md`.

**1. Los frames de un tramo se eligen donde la imagen CAMBIA.** `lib/frames.py`
trae tres motores en `bin/extract_segment_frames.py`, y el default no cambia
nada: `porcentaje` sigue siendo lo de siempre. `escena` detecta cortes
(`select=gt(scene,0.20)`) sobre el rango completo y recorta después —capar la
detección con `-frames:v` conserva los primeros cortes y tira la cola del
tramo—; si salen menos de 8 escenas el tramo es estático, cae a uniforme y **lo
declara**.

```bash
python3 bin/extract_segment_frames.py --root "$DISK" --motor escena --force
python3 bin/extract_segment_frames.py --root "$DISK" --motor escena \
    --cues beats,silencios --force
```

**2. Los casi idénticos se tiran, y el dedup sólo puede equivocarse hacia
conservar.** Miniatura 16x16 en gris, diferencia media por píxel, umbral 2.0
sobre 0-255; ffmpeg hace el decode en una pasada y el resto es stdlib. Medido:
**9 frames de un plano sostenido colapsan a 1**. Dos detalles cargados de razón:
se compara contra el último CONSERVADO (contra el anterior, la deriva lenta se
escapa entera) y es **fail-open** — si los bytes no cuadran uno a uno, no se
deduplica nada. Un dedup que adivina tiraría frames buenos en silencio.

**3. Los cues: los tiempos que el habla señala.** La selección visual se pierde
justo los momentos que alguien está señalando, porque señalar apenas cambia la
imagen. `--cues beats,silencios` fuerza un frame en `interview_beats` (la
pregunta y dónde empieza a responder) y en el centro de cada `clip_silences`.
Se descuentan del tope ANTES de elegir: un cue no compite con un frame de
escena, gana siempre.

**4. Un desfase ya no se discute con una tabla: se VE.** `bin/vista_tramo.py`
dibuja un PNG con el filmstrip, una cinta de forma de onda por fuente de audio,
las palabras del transcript en su tiempo y los silencios sombreados, todo en la
misma regla. Con `--audio ambos`, las dos cintas ya vienen convertidas a tiempo
de vídeo: si el sync está bien los picos coinciden en vertical.

```bash
python3 bin/vista_tramo.py --root "$DISK" --clip-id 1234 --desde 12 --hasta 24 \
    --audio ambos
```

El pico de normalización es COMÚN a las dos cintas: normalizar cada una contra
su propio máximo hace que un lavalier limpio y un A1 de sala se dibujen igual de
altos y parezcan la misma señal. Y **es una herramienta de punto de decisión, no
de barrido** — cada vista cuesta una extracción y un decode; para el barrido
están los verificadores. Esto cierra el `visual_qa.py` que `analisis-visual.md`
declaraba pendiente desde la v13.

**5. El entregable se mira, no sólo se mide.** `bin/verify_export.py` genera una
vista de ±1.5 s en cada frontera de corte, más arranque, medios y cierre, y
escribe `reports/export-<fecha>.md` con qué comprobar: salto de imagen, pico de
onda en la frontera (el clic que se coló), subtítulo tapado, continuidad de
color. Mide y avisa, no reprueba — si un corte salta es juicio editorial.

Con `--proyecto` y `--timeline` las fronteras son los cortes que el editor
DECIDIÓ; sin ellos, los que se VEN por detección de escena, y el reporte dice
cuál de las dos cosas está mirando. Ojo con `--bloque`: la duración de la
timeline no es la duración del corte.

> **Tope de tres pasadas.** Corregir, volver a mirar y corregir otra vez
> converge o no converge. Si a la tercera sigue habiendo algo, se le DICE al
> editor con la lista de lo que sigue mal y por qué no se corrigió. El script se
> niega a una cuarta. Y generar las vistas no es mirarlas: eso es del asistente.

**6. Un fallo silencioso menos.** `bin/analyze_segment_objects.py` hace
`int(fp.stem)` para poblar `segment_objects.frame_pct` y se saltaba **en
silencio** cualquier frame con otro nombre: un tramo entero podía quedarse sin
objetos sin que el resumen final lo dijera. Ahora se cuenta y se avisa.

**7. Lo que NO entró, y queda escrito.** ElevenLabs Scribe y Whisper por
Groq/OpenAI: material confidencial saliendo del disco, en un documental con
personas reales delante de cámara. Y el anti-patrón ajeno de que *"Whisper local
es lento"* es cierto en CPU y falso aquí —Metal, ~14x tiempo real,
`large-v3-turbo`—: antes de adoptar el anti-patrón de alguien, comprobar que su
supuesto se cumple en esta máquina. Tampoco entran el render ni el EDL propio:
el asistente no monta, el corte lo firma el editor.

## Novedades v0.7.0 (Morsa 2026-08-19 — el corte manda sobre el subtítulo)

**1. La timeline de Resolve se puede LEER, sin abrir Resolve.**
`lib/timeline_resolve.py` abre el `Project.db` de la base de disco (SQLite) en
sólo lectura y devuelve fps, resolución, pistas, items y cortes al fotograma.
Hasta ahora el motor sólo sabía escribir en Resolve; el dato más valioso que
produce el editor —dónde decidió cortar— estaba fuera de alcance.

```python
tl = leer_timeline("<proyecto>", "<timeline>", config=project_config)
tl.bloques()            # una timeline de trabajo NO es continua
tl.cortes_sync()        # los que cortan imagen Y diálogo
tl.cortes_solo_imagen() # B-roll sobre audio continuo: el voice over
```

Tres trampas medidas: `Sm2Sequence_Sm2TiTrack` existe y está VACÍA (la buena es
la del contenedor); el frame rate es little-endian y la resolución big-endian en
el mismo registro; y `Start`/`Duration` pueden traer fracción de fotograma
(`92956|00a0246afd9de83f`), que `CAST(... AS INTEGER)` tira en silencio.

**2. Un corte sólo manda sobre el subtítulo si también corta el sonido.**
Medido sobre 995.7 s de corte: 366 cortes de imagen, sólo **77 sincrónicos**.
El sincrónico cae en una pausa de habla el 31 % de las veces contra un 9 % de
azar y llega cada 7.44 s; el de solo imagen está al nivel del azar y llega cada
2.04 s. Ahí el audio sigue corriendo por debajo: **eso es el voice over**.

Son dos reglas: **prohibición** (ningún cue cruza un corte sincrónico; si
partirlo dejara un trozo ilegible, no se parte y SE DICE) e **imán** (un borde
que ya caía a menos de 10 fotogramas se clava al fotograma del corte). Nunca se
crea un borde donde el habla no lo pedía.

```bash
python3 bin/build_subtitles.py --video "$EXPORT" --config "$CFG" --timeline "<nombre>"
python3 bin/build_subtitles.py --video "$EXPORT" --config "$CFG" --desde 00:00:23:03
python3 bin/verify_subtitulos.py --srt "$SRT" --video "$EXPORT" --config "$CFG" \
    --timeline "<nombre>" --max-cruces N
```

**3. La duración de la timeline no es la duración del corte.** En Morsa la
timeline medía 78:10 y sólo 24:56 eran contenido, en 7 bloques: lo entregado era
el PRIMER bloque, 16:35.7. Confundirlos es un error de 61 minutos.
`Timeline.bloques()` lo resuelve.

**4. El sonido del entregable se mide.** `bin/verify_audio.py` saca LUFS
integrado, LRA, true peak y **muestras a 0 dBFS** de cada export a
`reports/audio-<fecha>.md`. **No reprueba contra ningún objetivo** — decisión de
Victor: el motor mide, el número lo firma el editor. La primera corrida encontró
7 de 14 exports con clipping duro, uno con 1,031,972 muestras a fondo de escala,
y 15.4 LU de dispersión entre entregables.

**5. Dos reglas mal ordenadas y una fusión sin candado.** El tope de duración
va ANTES del ajuste de CPS (al revés, el recorte a 6 s volvía a subir el CPS: 18
cues por encima del tope en un SRT entregado), y la fusión de cues ahora
comprueba `MAX_DUR` (de ahí salía un cue de 9.91 s con el tope en 6.0).

**6. El CPS alto no siempre es culpa del motor.** 47 de 171 segmentos de Whisper
ya venían por encima de 17 CPS. Bajar de ahí exige CONDENSAR, y el motor no
condensa. El CPS cuenta como incumplimiento sólo cuando había sitio por delante
y no se usó; el resto se reporta con el número.

**7. Los settings de proyecto sólo se leen por la API.** Viven en
`SM_Config.SetupBA`, un struct binario sin nombres de campo. Para eso está
`resolve/auditar_settings.lua` (lectura pura). Y `reel_subtitulado.lua` ahora
fija también el **frame rate** de la timeline del reel, no sólo la resolución.

## Novedades v0.5.0 (Morsa 2026-08-13 — la cronología del día)

**1. El signo del offset iba invertido, y no había pruebas que lo vieran.**
`lib/sync_refiner` centraba la búsqueda en `v_start + offset` cuando la
convención obliga a `v_start − offset`. Con lavalieres continuos el efecto no
era "2× offset": `a_start` se recortaba a 0 y el refinador correlacionaba contra
los PRIMEROS segundos del WAV, así que escribía **ruido acotado por la ventana**.
33 pares desplazados hasta 4.19 s en dos proyectos. Revertidos con
`bin/revertir_refinamientos_iter2.py` — el delta va en la nota, así que deshacer
es aritmética exacta.

> **La lección cara: las compensaciones.** `export_multicam_lua.py` pasaba
> `−delta` y negaba el resultado, o sea que la doble negación CANCELABA el bug.
> Al arreglar el refinador, ese sitio empeoró. **Al corregir un signo, buscar
> quién lo estaba compensando antes de dar el arreglo por bueno.** Con los dos
> lados bien, Morsa pasó de 25 a 54 pares verificados por contenido.

**2. B-ROLL es cronología, A-ROLL es selección.** La timeline de B-roll se
armaba empacada, clip tras clip, sin los huecos reales del día — y un lavalier
continuo no puede cuadrar contra eso: se desfasa por la SUMA de los huecos
eliminados. Era estructural, no de medición. Ahora cada cosa va en su hora y el
WAV entra **íntegro**, cortado solo en los bordes de la cámara base. Los huecos
se conservan a propósito. → `LIB.construirPorTiempoReal`, `roll.md`.

**3. V1 = la cámara del lavalier, y se DECLARA.** Se intentó deducir y no se
puede: medido, las cámaras empatan en cobertura de pares y el nivel del A1
engaña —la del lav puede ser la más baja, porque capta una voz con silencios
mientras la otra capta la sala entera—. Va en `project_config.json`:
`{"camara_lavalier": "VICG 001"}`. Sin declarar, cae al metraje y **lo dice**.

**4. La hora de un WAV sale de su CADENA, no de su mtime.** Los Rode no traen
BEXT ni iXML; el mtime va a segundo entero. Tomarlo archivo por archivo mete
±1 s independiente en cada uno, cuando la cadena es contigua muestra a muestra:
aparecían huecos de hasta 2 s y el skew aparente de una cámara variaba 1.7 s
según contra qué archivo se midiera. Ahora la cadena se ancla una vez y el resto
va por suma de duraciones.

**4b. Y las cadenas se alinean ENTRE SÍ por contenido (v0.5.1).** Con dos TX
cada cadena arrastra su propio error de ancla, así que quedan desalineadas entre
ellas aunque cada una sea internamente perfecta. **El síntoma no es audio mal
puesto: son cámaras desfasadas entre sí**, porque una sincronizó contra una
cadena y otra contra la otra. En Morsa, Ayan contra Izq y Vic contra Derecha.

No hace falta volver al audio a medirlo: los videos que sincronizaron contra
AMBAS cadenas dicen cuánto se contradicen. Medido allí, **+163.3 ms con 8.5 ms
de desviación sobre 267 videos** — una constante, no ruido. Es la "delta física
D" que la doctrina daba por pendiente de medir. La cadena con más pares hace de
referencia y las demás se mueven hacia ella, solo con evidencia suficiente
(≥5 videos en común y desviación <100 ms); si no, se deja y se dice por qué.
Resultado: la contradicción baja de 163 ms a 7 ms.

**5. `scipy` es capacidad declarada.** Cuatro módulos lo importan sin fallback y
el doctor no lo miraba.

> **Trampa del material:** los nombres de archivo de audio **se repiten entre
> cadenas** (dos `00014_Wireless PRO.WAV`, Izq y Derecha, a una hora exacta de
> distancia). Agrupar por `filename` en vez de por `rel_path` fusiona archivos
> distintos y produce lo que parece un bug de sync y no lo es.

## Novedades v0.4.0 (IMODAE 2026-08-07 — la pieza con guion)

Primer **comercial**: seis cápsulas rodadas el mismo día, con prompter y sin
audio externo. Todo lo de abajo es **opt-in** — ningún proyecto anterior cambia
de comportamiento. Detalle completo en `metodologia/reels-y-cortes.md`.

**1. `project_kind: "comercial"`.** A-roll = **quien dice el texto guionizado**
(tiene fila en `clip_takes`), no la densidad de diálogo. Se **declara**, nunca se
infiere: un rodaje con guion se parece demasiado a la ficción desde el manifest.

**2. Reels / cápsulas — `bin/derive_reels.py`.** Reparte el material entre las
piezas del brief. La unidad es el **grupo de tomas**, no el clip. Puntúa por
vocabulario del TEMA, no por el guion literal: el texto que se rueda casi nunca
es el del brief. Lo que no tiene diálogo hereda la cápsula por la hora, y si cae
en dos ventanas solapadas no se adivina. Escribe informe con la evidencia; se
corrige con `reel_overrides`.

**3. Corte de silencios — `detect_pauses` → `derive_silence_cuts` →
`verify_cortes`.** Quita las esperas de prompter para poder comparar tomas.
**Opt-in y sólo en A-ROLL**: en una entrevista la pausa es contenido y se marca,
no se quita. El umbral se calibra por clip (RMS − 10 dB); uno fijo encontraba 16
pausas donde el calibrado encuentra 44. **La autoridad es el audio, no el
transcript**: los word-timings de Whisper aciertan en unos clips y derivan
segundos en otros.

**4. Behind the scenes como tercer destino.** La cámara que registra el rodaje
no es B-roll: el B-roll ilustra la pieza y entra en el montaje. Se declara por
cámara (`bts_camaras`). Al abrir el tercer destino se amplió la garantía de
cobertura — una que sólo mira dos de tres deja de serlo.

**5. Marcadores en la REGLA de la timeline.** Sky abre bloque de cápsula, Cocoa
lo **cierra** (dónde acaban los intentos), Lavender/Rose por toma. Van en la
regla y no en el clip porque así se leen de un vistazo.

**6. Subtítulos de un export.** `bin/build_subtitles.py` sin `--offset-tc` da
tiempos desde 0 (lo que hace falta para subir el reel), y
`resolve/reel_subtitulado.lua` arma su timeline con la resolución del propio
archivo. El SRT lo importa el editor: **no existe API de subtítulos en Resolve**.

> **Regla que atraviesa todo lo anterior:** que una llamada de la API devuelva
> algo NO significa que hiciera lo que se le pidió. `AppendToTimeline` con
> `mediaType=3` devuelve item y lo coloca en V1; `pcall(AddTrack)` devuelve
> `true` aunque `AddTrack` devolviera `false`. La comprobación no es
> "¿devolvió?" sino "¿está donde tenía que estar?".

## Novedades v0.3.0 (Morsa 2026-08-03 — tres cámaras y un concierto)

Primer proyecto con TRES cámaras y primero en un evento con música. Rompió
supuestos que venían de los cinco anteriores.

**1. Solo A-ROLL y B-ROLL, con garantía de cobertura.**
Se retiraron `TODO EL MATERIAL` y `ENTREVISTAS`. Entre A-roll y B-roll tienen
que estar TODOS los clips, y el script lo comprueba al cerrar con números. Es
configurable por proyecto (`TIMELINES`).

**2. Pista fija por cámara, ordenada por CANTIDAD DE MATERIAL.**
V1 la que más rodó, V2 la siguiente, V3 la que menos. Lo calcula
`export_multicam_lua.py` y viaja en el bake como `track_order`; la cámara base
ya no se elige, se deduce. Antes la compañera caía en V2 usando V3 solo como red
de seguridad, y con tres cámaras el mismo ángulo terminaba en pista distinta
según el clip.

**3. Linkear siempre — y el merge deja de ser opcional.**
El audio se liga a su clip con `AutoSyncAudio` (merge del Media Pool, sobre
proyecto duplicado); los ángulos entre sí con `LIB.ligarGrupo`, que comprueba
que `SetClipsLinked` exista antes de prometerlo. El merge entra ANTES del bake.

**4. Alinear por reloj dentro de las ventanas de show.**
`bin/derive_show_windows.py` mide dónde domina la música (Otsu doble sobre el
perfil de sonoridad). Ahí todas las cámaras apuntan a lo mismo, así que se
acepta el reloj; fuera se sigue exigiendo confirmación por contenido. Cada par
sale con `basis`, y los alineados por reloj llevan marcador Yellow.

**5. `AUDIOS EXTERNOS` es la cronología del día**, posicionada por tiempo real:
cada cadena de audio en su pista y las cámaras arriba en su hora.

**6. Relojes que el motor no puede medir.**
Si el A1 está tapado, una cámara puede quedarse sin skew — y el default 0
ordenaba sus clips con el reloj crudo EN SILENCIO. Ahora avisa. Antes de dar un
reloj por no medible, **comprobar que el rango de búsqueda pueda contener la
respuesta**: un desfase de cámara puede ser de HORAS. Cuando el editor lo ve a
ojo, se declara en `camera_skews_manual` con nota de quién lo midió.

**7. Proyecto nuevo: `bin/nuevo_asistente_proyecto.py`.** No copiar a mano el
script de otro proyecto — así fue como tres de cinco quedaron desfasados.
→ `metodologia/pasos-a-seguir.md` § "Actualización v0.3.0"

## Novedades v0.2.0 (leer antes de tocar markers, sync o cámaras)

Cuatro cosas cambian de raíz. El detalle está en las references indicadas.

**1. Marcadores de entrevista — pregunta y respuesta SEPARADAS.**
Antes un solo marker morado cubría ambas. Ahora Purple = pregunta,
**Blue = dónde empieza a responder** (el punto de corte), Sand = pausa real,
Mint = palabra clave, Lemon = candidato emocional. **Todos de punto**: los
duration markers están fuera por instrucción del usuario (FCC 2026-07-11).
El método usado para separar Q/R va escrito en la nota del marker.
→ `bin/derive_interview_beats.py`, `bin/detect_pauses.py`, `metodologia/marcadores.md`

**2. Merge de audio en el Media Pool.**
`MediaPool:AutoSyncAudio` **funciona en Resolve Free** (medido en 21.0.2, con
0 ms de error). El lavalier se pega dentro del clip, así que cualquier timeline
que el editor arme a mano ya trae el audio bueno. **No se deshace por API** —
el script exige un proyecto duplicado.
→ `bin/export_merge_plan.py`, `resolve/merge_pool.lua`, `metodologia/merge-media-pool.md`

**3. A-roll / B-roll automático.**
Documental: las entrevistas son A-roll. Ficción: umbral adaptativo (percentil 60
del proyecto) y decisión **por escena**, no por toma. Se materializa en
timelines, Keyword, color de clip y — opt-in y reversible — bins.
→ `bin/derive_roll.py`, `metodologia/roll.md`

**4. Registro de cámaras y formatos.**
Ya NO hay ninguna `classify_camera()` en los scripts: todo sale de
`config/camera_profiles.json`. Agregar una cámara es editar un JSON, no ocho
archivos. Entran BRAW/R3D/MXF/DNG (con salto registrado donde ffmpeg no
decodifica) y el poly-WAV de campo con `bext`/`iXML`.
→ `bin/inventario_camaras.py`, `bin/index_audio_tracks.py`,
  `metodologia/camaras.md`, `metodologia/formatos.md`

**Regla dura que atraviesa todo**: los audios externos van SIEMPRE después de
los de cámara (`A1` cámara base · `A2` compañera · `A3` lavalier). La verifica
`bin/verify_track_order.py` y falla el pipeline si se rompe.

## Las cuatro puertas — el acta de conducta

Las cuatro capas de verificación del motor (lint, tests, guards y los
`verify_*`) verifican EL CÓDIGO Y LOS DATOS. El plugin trae dos enteras,
`bin/lint_pipeline.py` y los `bin/verify_*.py`; los tests y sus guards son del
desarrollo del motor y viven en su repo, no en el paquete. Ninguna verifica AL
ASISTENTE. Esa es la
QUINTA capa: `bin/verify_asistente.py`, veintitrés checkpoints agrupados en
cuatro puertas transversales más los invariantes duros, cada uno con su caso
fundador y su forma de comprobación por evidencia física — nunca la palabra
del asistente. Doctrina completa:
`metodologia/pruebas-del-asistente.md`.

| Puerta | Cuándo | IDs |
|---|---|---|
| **G0** — arranque | antes de tocar material | A1–A4 |
| **G1** — entre etapas | cada vez que una etapa termina | B1–B5 |
| **G2** — antes del editor | antes del bake y de Resolve | C1–C6 |
| **G3** — cierre | al declarar el proyecto terminado | D1–D4 |
| **I** — invariantes | transversales, en cualquier puerta | I1–I4 |

```bash
python3 ~/cinema-assistant/bin/verify_asistente.py --root "$DISK" \
  --project-prefix "$PREFIX" --todas --acta
```

Nunca declarar un proyecto cerrado con checkpoints OBLIGATORIOS en FALLA sin
antes: (1) corregir lo corregible sin pedir permiso, (2) escribir en
`<disco>/.cinema_assistant/reports/acta-notas.md` el motivo de lo que
requiere juicio editorial (el correlato físico que exige la doctrina de
declaración), (3) dejar como NO-APLICA solo lo que de verdad no aplica. El
cierre incluye los RESULTADOS del acta, no solo "ya está". Reportar números
honestos: "7 syncs verificados" vale más que "45 dudosos".

Los seis verificadores obligatorios del §12b (`verify_interviews`,
`verify_coverage`, `lint_pipeline`, `verify_lav_offsets`,
`verify_multicam_placement`, `surface_transcript_suspects`) y la auto-review
del §13 (cobertura honesta, sanity SQL, `luac -p` + smoke con mock) siguen
siendo la garantía de fondo — el acta los invoca a través de los checkpoints
C1, C3, C5 y D1/D2; no los reemplaza, los agrega en un solo veredicto.

## Referencias (leer on-demand)

- `references/pasos-a-seguir.md` — playbook completo, el orden correcto.
- `references/sync.md`, `references/sync-sin-ground-truth.md` — doctrina de sync.
- `references/transcripcion.md` — transcripción + curaduría Capa 2c.
- `references/camaras.md`, `references/catalogo-audio.md`, `references/entrevistas.md`
- `references/marcadores.md`, `references/descripciones-cronica.md`, `references/curaduria.md`
- `references/resolve-integracion.md` — la Consola Lua.
- Subtítulos de un export: `bin/build_subtitles.py --video <export> [--config project_config.json] [--timeline "<nombre>"] [--offset-tc <TC del IN>] [--desde <TC>]`.
  Con `--timeline`, los cortes de la timeline mandan sobre los bordes de los
  cues (v0.7.0). Imprime el TC de pegado; en Resolve se colocan con ⌘X/⌘V al
  playhead (el drag&drop NO respeta el timecode). Después,
  `bin/verify_subtitulos.py`, que comprueba contra el audio y contra los cortes.
  Detalle en `references/reels-y-cortes.md` (actualización v0.7.0).
- Sonido del entregable: `bin/verify_audio.py --root <disco> [--detalle]`. Mide y
  avisa, no reprueba.
- Imagen del entregable: `bin/verify_export.py --root <disco>
  [--proyecto <p> --timeline <t> --config <cfg> --bloque N]`. Vistas de cada
  frontera de corte. Mide y avisa; el tope es de tres pasadas.
- `references/ver-el-material.md` — cómo mirar el material sin gastar de
  más (presupuesto de frames, escena, dedup, cues) y cómo mirar lo que
  sale (`vista_tramo.py`, `verify_export.py` y el tope de tres pasadas).
- `references/patrones-exitosos.md` — defaults que funcionaron.
- `references/lo-que-no-hacer.md` — lista negra.
- `references/errores-comunes-a-corregir.md` — pitfalls ya vividos.

## El asistente aprende

Cada proyecto enseña algo: patrones nuevos, errores nuevos y preferencias
van a `~/memoria-asistente-edicion/` (la copia LOCAL, no a este plugin).
Mantener ese playbook vivo es parte del trabajo. Principio rector del
colectivo: **la IA ejecuta, el editor decide** — el asistente indexa,
sincroniza y propone; el corte es humano.

## Al cerrar un proyecto: devolver a la memoria creativa (paso 13b, OPCIONAL)

**Solo si el editor mantiene una bóveda de memoria creativa** (Obsidian u otra).
No es parte del pipeline de edición: es el puente hacia un sistema de notas
personal, y la mayoría de las instalaciones no lo tienen.

```bash
# con bóveda en la ruta por defecto (~/memoria-creativa)
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta/del/proyecto>"
# con bóveda en otra ruta
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta>" --vault ~/mi-boveda
# ver qué propondría, sin escribir nada
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta>" --dry-run
```

Escribe una propuesta en `<bóveda>/_cambios/pendientes/` (JSON más un `.md`
legible). Es idempotente: no duplica si el manifest no cambió. **No escribe en
la memoria final** — el sistema propone, la persona aprueba.

**Si no hay bóveda, el script lo dice y sale con 0: sáltate el paso.** No lo
reportes como fallo del proyecto ni intentes crear la bóveda — es una decisión
del editor, no del asistente.

Por qué existe: entre junio y julio de 2026 se procesaron cinco proyectos y
nada llegó a la bóveda de su autor, porque este paso no existía. Detalle en el
§13b del playbook.
