# Integración con DaVinci Resolve

Resolve **Free** no expone API externa de scripting. La integración
funcional es la **Consola interna** en modo **Lua**.

**Desde Resolve 21.1 (2026-09)** hay que decirlo con más precisión, porque
Blackmagic movió cosas de sitio:

- El **scripting en Python** pasó a Studio. Su razón, textual en las notas de
  versión: *"The Python API was being used to hack studio features into the
  free version."*
- El **servidor MCP** que Resolve 21.1 estrena también es de Studio, y se
  cablea en `File > Setup AI Assistants` con External scripting en `Local`.
  En Free no existe: no es cuestión de configurarlo.
- La **Consola en modo Lua** es el camino que queda, y sigue siendo el nuestro.
  El README de scripting que instala la propia 21.1 mantiene la Consola y el
  menú de Scripts bajo *Internal Scripting*, y declara que las APIs son un
  *"common superset"* de Free y Studio donde lo de Studio devuelve `False`.

Eso es lo que dice el fabricante. **Lo que dicen terceros** (medido en
21.1.0 build 17 por davinci-resolve-lua-mcp y AutoSubs 3.10, y en la build 14
por resolve-console-bridge) es más restrictivo, y el README no lo menciona.
**Desde el 2026-10-05 está medido aquí, en Free 21.1.0.17** (ver abajo), y
casi todo se confirma:

- Los scripts del menú **Workspace > Scripts** corren en un sandbox en Free:
  no hay `io`, `os.execute/remove/rename`, `require`, `package`, `ffi`,
  `debug` ni `arg`, y `bmd` trae 28 funciones contra 52 en Studio. Sí quedan
  `dofile`, `loadfile`, `loadstring`, `setfenv`, `pcall`, `print` y
  `os.time/date/getenv/clock`. El `print` del menú no se ve, ni con la
  Consola abierta (medido en 21.1.1).
- La **Consola es otro estado**, pero menos de lo que decían: en Free también
  le faltan `io`, `debug`, `require` y `os.execute`. Solo tiene `ffi` de más,
  y ahí `print` se ve.
- **Las builds difieren**: en la .14 el menú no ejecutaba Lua y en la .17 sí.
  Todo resultado se anota con la build completa, no solo "21.1".

Lo que una máquina hace de verdad lo mide el diagnóstico de la Fase 0 del plan
de Resolve Free, `resolve/diagnostico.lua`, que reemplaza a los spikes S9 a S11.
Reporta sin `print` ni archivos: una timeline `DIAG RESUMEN`, un marker por
prueba en `DIAG MARCAS`, un bin `DIEZ50 DIAG` con una subcarpeta por prueba y
una escritura en las preferencias de Fusion. `bin/preparar_diagnostico.py`
arma el kit y los scripts del menú (`Diez50 Diagnostico`), y
`bin/leer_diagnostico.py` junta los canales en una matriz. Solo actúa dentro de
un proyecto llamado `DIEZ50_DIAG`. **Ya corrió en un Free real**: la
21.1.0.17, en una VM, el 2026-10-05. Lo que no está en esa matriz sigue sin
estar comprobado.

## En la Mac de Victor: Studio, y el asistente llega solo a Resolve (medido el 2026-09-28)

La Mac de Victor tiene **DaVinci Resolve Studio 21.1.0.17** (licencia del
2026-09-23). Ahí el camino de la Consola sigue sirviendo, pero ya no hace falta
que nadie pegue la línea. Todo lo de abajo está medido en esa máquina, no es
del README:

- **API de Python externo.** Con `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB`
  (`.../Libraries/Fusion/fusionscript.so`) y `Modules/` en el `PYTHONPATH`,
  `DaVinciResolveScript.scriptapp("Resolve")` devuelve el objeto y lee y
  escribe el proyecto abierto: bins, clips, timelines, `ExportProject`,
  `ImportProject`, `ImportMedia`.
- **El Lua del motor corre DENTRO de Resolve desde fuera.**
  `resolve.Fusion().Execute(lua)` ejecuta el string en un estado donde existen
  `resolve`, `Resolve()`, `io`, `os`, `dofile` y `bmd`. Así que
  `dofile("<MOTOR>/resolve/asistente_<p>.lua")` corre el MISMO aplicador que en
  Free; no hay un aplicador aparte para Studio.
  - `Execute` devuelve `None`. **El canal de vuelta** es `fusion:SetData(clave,
    valor)` desde el Lua y `GetData(clave)` desde Python: el envoltorio hace
    `pcall` del `dofile` y deja ahí "OK" o el error.
  - `print` va a la Consola, no a quien llama. El envoltorio reemplaza `print`
    por uno que además acumula, y al final escribe el log con `io.open` en
    `<disco>/.cinema_assistant/logs/resolve-<clave>-<fecha>.log`.
  - El envoltorio vive en el motor desde el 2026-09-29:
    `bin/aplicar_en_resolve.py --root "$DISK" --clave <k> <script.lua> [NOMBRE=valor_lua ...]`.
    La primera noche vivió en un directorio temporal de la sesión, así que la siguiente
    sesión habría tenido que reescribirlo.
  - Los globals de la Consola (`MERGE_PLAN`, `MERGE_FORZAR`, `MOVER_A_BINS`...)
    se asignan en el mismo string, antes del `dofile`.
- **Lectura de vuelta.** Con el API se comprueba lo que Resolve construyó:
  items por pista, qué `MediaPoolItem` usó cada uno (`GetMediaId`), colores de
  marker, markers de duración, y que los bins y timelines del editor sigan
  idénticos a una foto tomada antes de empezar. Es lo que la puerta G2 no podía
  mirar en Free.
- **El servidor MCP de Blackmagic** (`Contents/Applications/ResolveMCP`, stdio)
  está registrado en Claude Code desde el 2026-09-28 a nivel de usuario
  (`claude mcp add --scope user davinci-resolve -- ".../ResolveMCP"`). Trae 14
  herramientas; las útiles son `run_script` (Python 3.14 en sandbox con
  `resolve` y `project` ya inyectados), `run_script_unsafe` (con sistema de
  archivos) y `search_scripting_api`. Sus herramientas cargan al abrir una
  sesión nueva, no en la que lo instaló. Es el mismo API que arriba, sin tocar
  variables de entorno.

**Lo que no cambia**: lo que se construye así tiene que poder rehacerse en
Free con la línea de la Consola. Por eso el aplicador sigue siendo el Lua.

**Primera corrida completa (Asistente, 2026-09-29)**:

- `merge_pool.lua` por `Execute`: 96 s. `verify_merge.py` dio 35 audios ligados con un error
  máximo de 17 ms; los límites de AutoSyncAudio están en `errores-comunes-a-corregir.md`.
- `asistente_asistente.lua` por `Execute`: 13.4 s. Construyó A-ROLL, B-ROLL y AUDIOS
  EXTERNOS, con 63 de 63 clips.
- La lectura de vuelta no encontró fallas: las timelines del editor quedaron intactas, el
  orden de pistas es correcto y no hay markers de duración.
- **Lo que esa lectura no miraba** (corregido el 2026-10-01): DÓNDE caía cada clip. En las
  timelines por hora real de esa copia, los clips sin sync iban 6 h corridos, la primera
  hora quedaba antes del arranque y cada corte del WAV perdía un frame. Ver en
  `errores-comunes-a-corregir.md` "La cronología mezclaba dos relojes" y "Studio 21.1 cambió
  dos cosas de AppendToTimeline". Las timelines de esa copia quedaron así; las buenas viven
  en `ProyectoAsistente`.

**Diagnóstico de la Fase 0 en Studio 21.1.0.17 (2026-10-01)**. Es la columna **Studio**. La de Free
se midió el 2026-10-05 y está justo abajo: nada de lo de Studio la sustituye.

- **Qué se corrió.** El kit `Diez50 Diagnostico` (motor en la rama `resolve-free-fase0`, sello
  `20261001-1122`), en un proyecto `DIEZ50_DIAG` de la biblioteca "Local Database", no en VICG:
  el menú dos veces, el submenú, Edit, el stub con error y la Consola.
  La matriz está en `~/Diez50-diag/matriz/21.1.0.17-DaVinci_Resolve_Studio-20261001-1130.md`.
- **Resultado.**
  - 93 OK de 182 en los cuatro contextos, sin ninguna fila que varíe, y r1 igual a r2.
  - Los siete canales funcionan.
  - El zip de `recoger_kit.sh`, leído con `--desde`, da la misma matriz: el camino de otra Mac
    queda validado.
- **En Studio el menú no tiene sandbox.** `io`, `os.execute/remove/rename`, `require`, `package`,
  `ffi`, `debug`, `arg` y `print` existen en el menú, el submenú, Edit y la Consola. Eso no dice
  nada de Free.
- **Avisos.** El `print` del menú se ve en la Consola. Un script del menú con error de sintaxis no
  abre ninguna ventana: el error queda en rojo en la Consola y solo lo ve quien la abre.
- **Carga.**
  - `dofile` funciona desde `~`, desde `/Volumes` (una `.dmg`) y con acentos.
  - Un horneado sintético de 1995 clips carga en 20 ms.
  - `loadfile` + `setfenv` aísla.
- **API.**
  - `AutoSyncAudio` sobre el par sintético devuelve true. No se midió el offset.
  - 500 appends tardan 43 ms.
  - `SetPrefs`/`SavePrefs`/`GetPrefs` y `SaveProject` funcionan.
  - Nombres de timeline y de marker de 250 caracteres pasan enteros.
  - FCPXML 1.10 sale como **paquete**, una carpeta con `Info.fcpxml` dentro.
  - `EXPORT_TEXT_CSV` y `EXPORT_TEXT_TAB` devuelven false.
  - Ocho métodos devuelven nil o false sobre los objetos del diagnóstico. Puede ser falta de
    condiciones; no se investigó. Son `GetAudioRenderCodecs`, `GetTranscription`,
    `AutoAlignClips`, `NormalizeAudioLevel`, `AddTransition`, `SetOutputBlanking`,
    `FlattenMulticam` y `PerformMulticamSmartSwitch`.
- **Nombres que Resolve rechaza.** `/ \ : * ? " < > |` no entran en el nombre de una timeline ni de
  un bin: `CreateEmptyTimeline` y `AddSubFolder` devuelven nil y `SetName` false. Acepta
  `·`, `—`, `ñ` y lo demás. La prueba `LANG-11` lo mide en cada build. Le pega a tres cosas:
  - El resumen del kit (`93/181`) nunca nacía. Se arregló en `53573f2`; ver
    `errores-comunes-a-corregir.md`.
  - La timeline de reporte del plan (`Diez50 · … · n/m`) tampoco nacería. Va `n de m`.
  - Una timeline por subcarpeta lleva el nombre de una carpeta del disco, y una carpeta con `/` en el
    Finder llega como `:`. En `construir` (6-oct) todo nombre pasa por `nombreSeguro`; en los scripts
    por proyecto, `freshTimeline` sigue sin limpiarlo.

**Diagnóstico de la Fase 0 en Free 21.1.0.17 (2026-10-05)**. Es la columna **Free**. Se midió en una VM de
macOS 27.0.1 (UTM con Apple Virtualization: 8 GB y 6 núcleos del M3 Pro), con Resolve Free 21.1.0 build 17
instalado desde el zip del 10-sep. La columna Studio es la del 1-oct.

- **Qué se corrió.** El kit con sello `20261005-1705`, en un proyecto `DIEZ50_DIAG` de "Local Database":
  - el menú dos veces, el submenú, Edit, el stub con error, la Consola y el `.drt` a mano;
  - el kit se preparó con `TZ=US/Pacific`, la zona de la VM, para que `OS-14` comparara contra la hora
    de allá;
  - en una instalación nueva de la 21.1, "Local Database" vive en `Resolve Project Library` y no en
    `Resolve Disk Database`. `recoger_kit.sh` la encuentra igual, porque lee `dblist.conf`.

  La matriz está en `~/Diez50-diag/matriz/21.1.0.17-DaVinci_Resolve-20261005-1759.md`.
- **Resultado.**
  - 90 OK de 182 (Studio, 93).
  - Cambian 23 filas, y **ninguna es del API de Resolve**: las 64 pruebas API dan lo mismo que en Studio
    (AutoSyncAudio, markers, metadata, multicam, ajustes).
- **El sandbox existe, y no solo en el menú.** En Free no hay `io`, `require`, `package`, `debug`, `arg`
  ni `os.execute/exit/remove/rename/setlocale`, ni en el menú, el submenú o Edit, **ni en la Consola**.
  - En la Consola lo único distinto es que existe `ffi` y que hay más globals (4560 contra 113 del menú).
  - `bmd` trae 28 funciones, contra 52 en Studio.
- **Lo que sí queda en Free:**
  - `print`, que se ve en la Consola;
  - `os.time/date/getenv/clock`;
  - `dofile`, `loadfile`, `setfenv` y `pcall`, desde `~`, desde una `.dmg` en `/Volumes` y con acentos;
    un horneado de 1995 clips carga;
  - `fusion:SetPrefs/SavePrefs` y `SaveProject`.
- **`Timeline:Export` funciona en Free, desde el menú.** DRT, OTIO, FCPXML 1.10, EDL y AAF escriben su
  archivo; CSV y TAB devuelven false, como en Studio. Pasa el sandbox porque el archivo lo escribe
  Resolve, no Lua.
- **Los cuatro candidatos a canal de regreso funcionan:**
  - `Project.db` con `SaveProject`;
  - `Timeline:Export` del `.drt`;
  - `Fusion.prefs`;
  - el export manual.

  El canal `io` no: 0 recibos.
- **A ojo.** El menú y el submenú aparecen, y el `print` de la Consola se ve. Un script del menú con
  error no muestra nada, y en la Consola tampoco queda rastro: en la 21.1.1 se corrió con la Consola ya
  abierta, y siguió vacía. En Studio el error se ve en rojo.
- **Lo que no salió igual dos veces.** En la segunda corrida del menú, `LARGO-01` colocó 499 de 500
  `AppendToTimeline` (63 s). Las otras cuatro corridas colocaron 500, en 16 a 72 s, lentas por la VM.
  Una pieza se perdió sin error, así que la verificación tiene que contar lo que quedó, no lo que se pidió.
- **Consecuencia para hoy.** Los scripts que escriben con `io` truenan en Free también desde la Consola,
  no solo desde el menú: `volcarLayouts` y `guardarOrigenBins` de `asistente_lib`, `merge_pool`,
  `leer_sync_merge`, `auditar_settings` y `lavas_al_corte`. La línea de la Consola sigue sirviendo para
  lo que no usa `io`.

**Free 21.1.1 build 10 (2026-10-05): lo mismo que la 21.1.0.17.** Salió el 1-oct y se midió esa misma
noche, en la misma VM, después de actualizar con el propio actualizador de Resolve (*Check for Updates*).
Ese actualizador baja el instalador de `sw.blackmagicdesign.com` sin el formulario de la página de soporte.

- **Qué se corrió.** El kit con sello `20261005-1836`, en un proyecto nuevo `DIEZ50_DIAG_2111`: el menú
  tres veces (la tercera con la Consola abierta), el submenú, Edit, el stub con error (también con la
  Consola abierta), la Consola y el `.drt` a mano. La matriz está en
  `~/Diez50-diag/matriz/21.1.1.10-DaVinci_Resolve-20261005-2013.md`. Se lee con
  `leer_diagnostico.py --proyecto DIEZ50_DIAG_2111`, porque el zip trae los dos proyectos.
- **Resultado.** 91 OK de 182. **Ni una fila del sandbox ni del API cambió** respecto de la 21.1.0.17: el
  punto de versión no movió el host. Lo único distinto es `LARGO-01`, que aquí colocó 500 de 500 en las
  seis corridas (16 a 18 s). El append perdido de la 21.1.0.17 no se repitió.
- **Medido con la Consola abierta:** el `print` del menú no aparece en ella, y el error de un script del
  menú tampoco. En Free, un script del menú solo se hace visible por lo que construye: timelines,
  markers, bins.
- **Diferencias entre corridas que no son del host:**
  - la primera corrida de un proyecto nuevo no encuentra corrida anterior (`LOAD-11`);
  - `PREFS-01` queda "sin confirmar" en las corridas viejas, porque `Fusion.prefs` solo guarda el último
    valor de cada contexto.

**Cómo se midió Free en la VM, para repetirlo tras cada update.** La VM es `Diez50-Free`, en UTM, con la
carpeta compartida `~/Diez50-VM/compartida`. Lo que costó tiempo el 2026-10-05:

- **La imagen de macOS 27 pesa 26.6 GB**, no 18: a 2 MB/s son más de tres horas. El disco de la VM es
  ASIF y UTM lo muestra como "4 MB" aunque por dentro mida 80 GB.
- **A la VM solo le llegan códigos de tecla.** El texto que escribe la automatización llega como "aaaa", y
  el portapapeles no se comparte con el anfitrión. Lo que sí funciona:
  - un agente que corre en la Terminal de la VM, `agente.command` (doble clic en el Finder), que ejecuta
    los pedidos que se dejan en `compartida/agente/` y devuelve ahí la salida;
  - `pbcopy` dentro de la VM para pegar con Cmd+V en Resolve;
  - en los menús, las flechas del teclado, porque el cursor no abre submenús.
- **macOS le pide permiso a la Terminal** para Escritorio y Descargas, y el comando se queda esperando la
  respuesta. Instalar lo descargado se hace desde el Finder.
- **La VM se detiene con la pantalla del anfitrión bloqueada**, y la propia VM se bloquea sola. Hace falta
  `caffeinate` en las dos.
- **La VM arranca en la zona US/Pacific.** El kit se prepara con `TZ=US/Pacific` para que `OS-14` compare
  bien.
- **Una VM suspendida vuelve a la pantalla de bloqueo**, y la contraseña la escribe Victor. Para que quede
  apagada de verdad, se apaga desde el menú Apple de la VM: así no queda el `vmstate` de 1.2 GB.
- **UTM solo recibe clics con su ventana al frente.** Con la ventana en segundo plano el cursor de la VM se
  mueve pero el clic no entra. Si el Resolve del anfitrión está abierto, recupera el foco y tapa la
  ventana: se oculta con System Events (`set visible of process "Resolve" to false`), sin cerrarlo, y al
  terminar se vuelve a mostrar.

**La ida y vuelta con el canal elegido, en Free 21.1.1.10 (2026-10-05): verde.** Es lo último del
criterio de salida de la Fase 0. En la VM, en un proyecto nuevo `DIEZ50_IDA`:

- **Qué se corrió.** `bin/pedido.py prueba` escribió el pedido con la media del kit. `Diez50 Aplicar`
  (Workspace › Scripts) construyó `Diez50 · prueba de regreso`, con V1 y V2 de cámara, A1 y A2 de
  cámara y A3 de lavalier, y la exportó con `Timeline:Export` a `<recibos>/prueba.drt` junto con su
  reporte. `bin/verificar_regreso.py` leyó los dos `.drt` en el anfitrión y comprobó el nombre, el
  sello en el frame 0, cada pista con su nombre y su clip, y el orden. Salió verde, sin que nadie mirara
  Resolve.
- **Lo que quedó medido en Free:**
  - Resolve acepta el `·` en nombres de timeline. El reporte nació como `Diez50 · 5bb7 · OK · 2 de 2`.
  - Un segundo clic con el mismo pedido no construye nada: `Project.db` siguió con dos timelines y los
    recibos no cambiaron.
  - Un pedido nuevo encontró la timeline anterior y leyó su sello en el marker del frame 0 (`GetMarkers`
    y `customData`). La renombró a `… · anterior 5bb7` sin borrarla.
  - `ImportMedia` no duplica: el aplicador busca primero el clip por su `File Path`.
  - Con un buzón sin pedido para el proyecto abierto salió `Diez50 · ERROR · E02 · sin pedido para
    DIEZ50_IDA`, y su `.drt` llegó a la carpeta `errores/`. **Una timeline vacía también se
    exporta**, así que el motor se entera de un error sin que nadie abra Resolve. Desde la
    revisión del PR #1 el archivo es uno por proyecto, `errores/error_<proyecto>.drt`, y
    `verificar_regreso.py` solo lo cuenta si es posterior al pedido: con uno solo para todos y
    120 s de tolerancia, un E02 de un clic anterior o de otro proyecto ponía rojo al pedido nuevo.
- **Para repetirla tras un update** (en la VM, con el agente corriendo):

  ```bash
  python3 bin/pedido.py --buzon ~/Diez50-VM/compartida/ida/buzon.lua prueba --proyecto DIEZ50_IDA \
    --kit /Users/<usuario-vm>/Diez50-diag --recibos /Users/<usuario-vm>/Diez50-ida/recibos --otra-maquina
  ```

  El agente instala el buzón, el stub, `aplicar.lua` y `pedido.json` con `compartida/ida/instalar.sh`.
  Se corre `Diez50 Aplicar`, `ida/recoger.sh` trae los recibos, y se lanza
  `bin/verificar_regreso.py --recibos ~/Diez50-VM/compartida/ida/recibos/<sello>`.
- **No se midió en la 21.1.0.17.** La VM ya estaba en la 21.1.1, y el diagnóstico dice que las dos builds
  tienen el mismo host.

**`construir`: el aplicador arma las timelines de un proyecto documental (Fase 1, 2026-10-06).** Lo
que hacían los `asistente_<proyecto>.lua` documentales vive en `resolve/construir.lua`, con la lógica
de `asistente_asistente.lua`, la más reciente. Es un módulo que `aplicar.lua` carga de `ctx.motor`, la
carpeta del motor que le pasa el stub (en el menú de Free no hay `debug` para ubicarse):

- **El pedido.** `bin/pedido.py construir --root "$DISK" --proyecto <nombre exacto> [--dia AAAA-MM-DD]`.
  El prefijo sale del `timeline_prefix` de `project_config.json`. Arma A-ROLL, B-ROLL por hora real,
  AUDIOS EXTERNOS con la línea de lavas, una por subcarpeta si hay varias, y las exporta al `.drt`.
- **Lo que cambia respecto del script por proyecto:**
  - nunca borra una timeline: la que ya existe se aparta como `… · anterior <sello>`;
  - no lee globals de Consola ni usa `io`, `os` o `print`. Lo que el script imprimía es una anotación
    del reporte, agrupada por tipo, con cuántos y unos ejemplos;
  - cada timeline lleva el sello en el frame 0, puesto al final porque en una timeline vacía Resolve
    no tiene dónde colgar un marker; las marcas de show van después, desde el frame 1;
  - la hora sale de un ISO con zona (`LIB.isoEpochConZona`). Uno sin zona no se adivina con la hora de
    la Mac: el clip queda sin hora y se dice;
  - los clips del horneado que faltan en el Media Pool no se importan (los importa el editor) y se
    nombran. Si no hay ningún video del horneado en el pool, no se construye ni se importa nada.
- **El índice y la cobertura fuera de Resolve.** `export_lua_data.py` escribe `<slug>_indice.json` junto
  al horneado: qué clips y qué WAV trae, con su rol y su día. La copia del pedido en los recibos lleva esa
  lista, y `verificar_regreso.py` la cruza con las rutas (`MediaFilePath`) del `.drt`. Lo que falta y el
  reporte explica ("faltan en el pool", "sin hora") da amarillo; lo que falta sin explicación, rojo.
- **Paridad en Studio 21.1.0.17 (6-oct).** En proyectos de prueba nuevos de la base VICG, con los mismos
  videos importados, corrió el script por proyecto en uno y `construir` en el otro. Se compararon los
  `.drt` pista por pista:
  - **Asistente, día 28: idéntico.** Tres timelines, 319 items (ruta, inicio, duración y entrada) y 144
    markers de item. La única diferencia es el marker del sello.
  - **Morsa: A-ROLL idéntica**, con los 5 ángulos de multicam en su pista fija.
  - **El B-ROLL de Morsa difiere, y es el heredado el que está mal.** Mismos clips en las mismas pistas,
    pero 254 van 6 horas exactas tarde: `asistente_morsa.lua:295` tiene su propio `isoEpoch` con
    `os.time`, que lee el `Z` como hora local. Es el bug que la librería corrigió el 1-oct y que
    `construir` ya no tiene.
  - AUDIOS EXTERNOS de Morsa difiere por diseño: la suya es la generación anterior.
  - Un segundo clic dijo "pedido ya aplicado". Un pedido nuevo apartó las tres timelines como
    `· anterior 4029` y construyó otras.
- **Colores medidos en esos `.drt`:** Lemon es 16384, Sand 32768 y Cream 131072. Ya están en
  `lib/timeline_resolve.py`.
- **Aceptación en Free 21.1.1.10 (7-oct): verde, dos veces.** En la VM `Diez50-Free`, proyecto
  `DIEZ50-CONSTRUIR`, con el paquete de `bin/preparar_ida_construir.py` (cinco clips de dos cámaras, dos
  WAV y su horneado):
  - **El primer pedido** construyó A-ROLL, B-ROLL y AUDIOS EXTERNOS desde el menú:
    - la compañera de multicam quedó en V2/A2 y el lavalier en A3;
    - `construir` importó los dos WAV que faltaban;
    - `verificar_regreso.py` dio verde desde el `.drt`, con 5 de 5 clips y 2 de 2 WAV.
  - **El segundo clic** no construyó nada, y los recibos no cambiaron.
  - **Un pedido nuevo** apartó las tres timelines como `· anterior abbe` y construyó otras: verde.
- **La revisión del código (7-oct) cambió cuatro cosas que se ven desde fuera:**
  - el orden de las pistas LAVA lo da el `lavalier_tx` de `project_config.json`
    (Asistente: izq y drc); sin él, por nombre. Antes estaba fijo en izq y drc para todos;
  - el B-ROLL nombra sus pistas (`CAM <cámara>`, `LAVA <tx>`), así que la regla dura
    también se comprueba ahí;
  - una timeline esperada sin material sale en el reporte como `timeline sin material
    (...)` y el verificador la da amarilla;
  - el verificador cuenta como presente un clip que está en la timeline con otra ruta,
    encontrado por nombre (la media se movió), y lo avisa. Lo que falta lo explica
    cruzando nombres con el reporte, no solo contando.
- **Para repetirla tras un update de Resolve:**

  ```bash
  python3 bin/preparar_ida_construir.py --home-otra /Users/<usuario-vm> --destino ~/Diez50-VM/compartida/construir
  ```

  - En la VM, el agente corre `construir/instalar.sh <sello>`.
  - Se crea el proyecto `DIEZ50-CONSTRUIR`, se importan los videos de `~/Diez50-construir/media`
    y se corre `Diez50 Aplicar`.
  - `construir/recoger.sh <sello>` trae los recibos, y aquí se corre `verificar_regreso.py`.
- **Lo que UTM no pasa a la VM, y cómo se rodea** (7-oct):
  - **Texto de corrido** (sale "aaaa"). Se teclea letra por letra. El guion bajo no sale con el teclado
    español de la VM, por eso el proyecto lleva guion.
  - **Arrastrar** del Finder al Media Pool tampoco llega: se importa con clic derecho › Import Media,
    archivo por archivo o con Shift+clic.
  - **La barra de menús** de la VM se esconde y no aparece con un salto del puntero. Aparece con
    movimiento real: botón apretado en una zona vacía y arrastre hasta arriba. Si el menú se cierra
    solo, se abre otro y se llega con las flechas, que sí entran.
  - **No hay que pedirle `osascript` a System Events dentro de la VM**: abre un diálogo de permiso que
    bloquea al agente. Se deniega.
  - **Para apagar**: `utmctl stop --request Diez50-Free`, que es el botón de encendido, y "Apagar" en
    el diálogo de la VM. No queda `vmstate`.

**Si el editor está trabajando en el proyecto**, no se aplica encima:

- Resolve abre un solo proyecto a la vez, así que aplicar en otro proyecto cambia la ventana
  del editor.
- Un diálogo modal abierto (una estabilización, un Relink Media, Project Settings) BLOQUEA el
  API: la llamada espera minutos sin error. Antes de aplicar se mira con `app_list_windows` si
  hay diálogos abiertos.

Lo que funcionó:

1. El editor guarda y cierra sus diálogos.
2. El asistente exporta el `.drp` y lo importa como `<proyecto> — COPIA asistente`. La
   palabra COPIA hace que el candado de `merge_pool` pase sin forzarlo.
3. Aplica en la copia, la guarda y vuelve a cargar el proyecto del editor.

## Cómo correr el script

1. Abrir Resolve. Cargar el proyecto.
2. Workspace → Console.
3. Verificar que esté en modo **Lua** (no Py2/Py3).
4. Pegar la línea en el **input strip al fondo** de la Consola y Return:
   ```
   dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_jilotepec.lua")
   ```

### Trampa: el input strip

Es una franja delgada al borde inferior de la ventana de la Consola.
Es fácil pegar al área de OUTPUT por error.

**Técnica que funciona** (vía computer-use):
1. `write_clipboard` con la línea a ejecutar.
2. `double_click` en el strip de input (enfoca).
3. `cmd+V` (pegar).
4. `Return`.

Typing directo a veces no registra; paste sí.

## Datos: `escalando_data.lua`

Auto-generado por `bin/export_lua_data.py` desde el manifest. **No
editar a mano.**

Estructura:
```lua
return {
  clips  = {
    ["/abs/path.mov"] = {
      name="MVI_2520.MOV", folder="...", status=nil, notes=nil,
      dur=311.0, created="2017-03-23T12:49:44",
      camera="main", interview=true,
      segments={{2.0,35.4}, {41.2,78.8}},
      description="ESCALADOR_A habla del descubrimiento de Lujuria...",
    },
    ...
  },
  byname = { ["NAME.mov"] = "/abs/path.mov" },  -- fallback
  sync   = {
    ["/abs/video.mov"] = {
      audio="ZOOM0006_LR.WAV",
      audiopath="/abs/audios/ZOOM0006_LR.WAV",
      offset=-203.04, conf=0.68,
    },
  },
}
```

**Indexado por RUTA**, no por nombre. La ruta es única en el manifest;
los nombres se repiten (GoPro/DSLR reciclan).

`byname` es solo un índice de fallback cuando la ruta no resuelve.

## Subcarpeta de timelines

El script crea/usa `Timelines/asistente de edicion` en el Media Pool y
mete ahí todos los timelines `ESC — *`. Mantiene el resto del Media
Pool sin invadir.

## Sweep al inicio

Antes de reconstruir, el script borra **todos los timelines `ESC — *`**.
**No toca otros timelines** — los lista al inicio para que se vean en
el output (si hay viejos del asistente con otro naming, ahí salen).

## API útil de Resolve (en Lua)

```lua
local pm = resolve:GetProjectManager()
local proj = pm:GetCurrentProject()
local mp = proj:GetMediaPool()

-- timelines del proyecto
proj:GetTimelineCount()
proj:GetTimelineByIndex(i)
mp:DeleteTimelines({tl1, tl2})

-- Media Pool / bins
mp:GetRootFolder()
folder:GetSubFolderList()
folder:GetClipList()
folder:GetName()
mp:AddSubFolder(parent, "asistente de edicion")
mp:SetCurrentFolder(folder)

-- crear / construir timelines
mp:CreateEmptyTimeline("ESC — JILOTEPEC")
mp:AppendToTimeline({mediaPoolItem1, mediaPoolItem2, ...})

-- mapear timeline items a sus archivos (path-keyed)
local mpi = item:GetMediaPoolItem()
local fp = mpi and mpi:GetClipProperty("File Path")

-- markers
item:AddMarker(frame, color, name, note, duration, customData)

-- importar WAVs externos
mp:ImportMedia({path1, path2})

-- tracks de audio
tl:AddTrack("audio", "stereo")
```

Lo que la API no hace, o hace a medias (ProyectoAsistente, 2026-10-07, Studio 21.1):

- **No renombra un bin.** Se crea otro con `AddSubFolder` y se le pasan clips y bins con
  `mp:MoveClips` y `mp:MoveFolders`; el viejo se borra con `mp:DeleteFolders` cuando está
  vacío por dentro.
- **No cambia el tipo de una pista que ya existe** (mono, estéreo). Solo `AddTrack` con
  subtipo. En la interfaz: clic derecho en la cabecera de la pista > Change Track Type to.
- **Con un archivo importado dos veces, `item:GetMediaPoolItem()` puede devolver la otra
  copia**, y la columna `Usage` del clip dice otra cosa. Un duplicado se quita del Media Pool
  solo si las dos medidas dicen "sin uso"; si no coinciden, se quedan los dos.

Y de Fusion y el render (visualizador de la voz, 2026-10-08, Studio 21.1.0.17):

- **Lo que se escribe por script en la composición de un clip no llega al render.** Queda en el
  `.comp` exportado, pero Deliver sigue usando la versión anterior. Se arregla con
  `item:ExportFusionComp(path, 1)` y `item:ImportFusionComp(path)`.
- **`tl:InsertFusionCompositionIntoTimeline()` es una edición de inserción en V1, en el cursor:**
  parte y recorre lo que sigue. `SetCurrentTimecode` más allá del final se queda en el último
  cuadro, así que la inserción cayó dentro del último clip. Para poner un clip en un cuadro exacto, sin
  empujar nada: `mp:AppendToTimeline({{mediaPoolItem, startFrame, endFrame, trackIndex,
  recordFrame, mediaType = 1}})`.
- **`MarkIn`/`MarkOut` de `proj:SetRenderSettings` marcan In y Out en la timeline.** También
  cambian la ubicación de salida de Deliver. Después: `tl:ClearMarkInOut("all")` y la `TargetDir`
  de vuelta a la carpeta del editor.
- **`Trails` con preroll traba el render de Deliver.** Se queda en 0 % con el CPU casi ocioso, no
  para con Stop, y Resolve solo se cierra a la fuerza. Aislado con renders de 2 cuadros: con el
  resto de la cadena (máscaras, `Resize`, `CustomTool`, `Blur`, `Soft Glow`) no pasa.
- **`comp:AddTool(id, -32768, -32768)` conecta la herramienta nueva a la que está seleccionada**
  con un `Merge`. Se le dan coordenadas.

### Fusion movido por audio: el Fairlight Animator

Es un modificador nativo (`FairlightAnimator`) que anima cualquier número con el audio. Lo medido:

- **Lee el audio del archivo del clip que lleva la composición, no la pista.** Un clip puesto solo
  con su video da los mismos valores que con su audio. `ClipName` lo pone Resolve y no se escribe.
  Una composición de generador, sin audio, deja la lista `Analysis` vacía. Para que escuche una voz,
  la voz tiene que ser el audio del clip: un "portador", un video negro con la toma como audio,
  puesto solo con su video, con el mismo inicio, fin y offset que la voz.
- **`levelAnalysis` (la única opción) es el pico lineal de cada cuadro.** Contra el WAV medido fuera:
  correlación 0.9945, desfase 0 cuadros.
- **Al elegir el análisis aparecen `param1` y `param2`:** el pasa-altos (30-1000 Hz) y el pasa-bajos
  (500-20000 Hz), Butterworth de orden 2. Con pendientes tan suaves, las bandas vecinas se parecen (0.9 de
  correlación). Lo que se separa bien es grave contra agudo.
- **`Scale` y `Offset` dan `valor·Scale + Offset`.** Un `Offset` negativo funciona como expansor: una
  máscara con altura negativa no se dibuja.
- **`TimeOffset = -1` lee el cuadro anterior.** Con eso se arma la caída de un pico sin `Trails`.
- **El render, con 48 Animators y audio, va a unos 15 cuadros por segundo** a 1828x1332.

El caso completo (bandas, ganancias, el `.comp` y el script) está en
`Diez50/Asistente/VOZ CLAUDE/guion-voz-claude.md`, sección "Visualizador de la voz".

## Convenciones del script

- Color por ubicación (Orange, Blue, Green, Teal, Purple, Pink, Yellow,
  Navy).
- Marker colors:
  - **Verde** — tramo útil (duration marker).
  - **Rojo** — descarte sugerido.
  - **Amarillo** — revisar.
  - **Cyan** — audio sync en A2 con offset y conf en la nota.
  - **Pink** — descripción del clip.
- A1 = audio de cámara intacto. A2 = audio externo.
- Clips ordenados cronológicamente por `creation_time`.

## Aplicar cambios — flujo completo

```bash
# 1. Hornear datos — AL DISCO DEL PROYECTO, no al motor.
#    Los *_data.lua llevan rutas, nombres reales y transcripciones: son datos
#    por proyecto. El motor solo guarda el script asistente_<proyecto>.lua.
BAKE="$DISK/.cinema_assistant/resolve"
mkdir -p "$BAKE"
python3 ~/cinema-assistant/bin/export_lua_data.py \
  --root "$DISK" \
  --out "$BAKE/<proyecto>_data.lua" --project-prefix ""

# 2. Syntax check (opcional pero recomendado)
luac -p "$BAKE/<proyecto>_data.lua"
luac -p ~/cinema-assistant/resolve/asistente_<proyecto>.lua
```

En Resolve:
- Workspace → Console (si no está abierta).
- Pegar `dofile("...asistente_jilotepec.lua")` en el strip → Return.
- Esperar (el output va apareciendo en la Consola — clips, timelines,
  markers).
- ⌘+S para guardar el proyecto.

## Layout de pistas con TRES cámaras (Morsa, 2026-08-03)

Todos los proyectos anteriores del colectivo tenían dos cámaras, y
`placeCompanions` colocaba la compañera en V2 usando V3 solo como red de
seguridad "si V2 está ocupado". Con tres, eso deja de servir: el mismo ángulo
cae en una pista distinta según el clip y no se puede cortar rápido, que es
justo para lo que existe la multicám.

Desde v0.3.0 la pista es **fija por cámara**: el grupo base va a V1/A1 y los
demás toman V2/A2, V3/A3… en orden estable (base primero, el resto alfabético).
El nombre de la pista es el de la cámara (`CAM Vic`, `CAM Iban`), no
"compañera 1/2".

```
V1 / A1   cámara base          (Ayan)
V2 / A2   compañera 1          (Iban)
V3 / A3   compañera 2          (Vic)
A4, A5    lavalieres           (Izq, Derecha)
```

Sigue cumpliendo la regla dura —audio de cámara antes que el externo— y
`verify_track_order.py` la comprueba igual.

## Alinear por reloj dentro de las ventanas de show

La regla histórica era colocar **solo** los pares confirmados por contenido
(envelope A1↔A1), por el caso MAB "luchador cortado por el ciclista": dos
cámaras rodando escenas distintas a la misma hora, y alinear por reloj pone una
copia sobre la escena equivocada.

En un concierto esa regla cuesta demasiado: en Morsa dejaba 16 pares de 136. Y
el riesgo que la motivó no existe mientras suena la banda, porque las tres
cámaras apuntan al escenario.

La solución no es relajar la regla a mano sino **medir dónde aplica**:

```bash
python3 bin/derive_show_windows.py --root "$DISK"      # escribe show_windows
python3 bin/export_multicam_lua.py --root "$DISK" ... --trust-clock-in-windows
```

`derive_show_windows.py` saca el perfil de sonoridad del día y corta con Otsu
aplicado **dos veces** — una sola pasada separa silencio de todo lo demás y mete
la voz dentro del "show"; la segunda separa voz de música. En Morsa: umbral
−11.6 dB, 5 ventanas, 73 min, y el arranque del concierto (12:58:05) coincide a
diez segundos con el anuncio del presentador que se oye en el transcript.

Cada par sale con `basis`: `contenido`, `reloj-show` o vacío (no se coloca). Los
`reloj-show` llevan **marcador Yellow** en la timeline, para poder distinguir de
un vistazo lo confirmado de lo alineado por reloj. Resultado en Morsa: de 16 a
112 ángulos colocados, con 24 pares descartados por caer fuera de las ventanas.

## Linkear: qué lo resuelve y qué falta medir

Petición permanente del usuario (2026-08-03): *"los distintos ángulos y el audio
linkéalos a partir de ahora siempre, luego resulta enredoso querer pasar algo
pero que no se mueva con todo lo demás."*

Son dos mecanismos distintos y conviene no confundirlos:

- **El audio con su clip** lo resuelve el **merge del Media Pool**
  (`MediaPool:AutoSyncAudio`): el WAV queda dentro del clip, en la timeline es
  un solo item y moverlo mueve todo. Por eso el merge pasa a ser el **default
  del pipeline**, no un opcional. Va siempre sobre proyecto duplicado.
- **Un ángulo con otro** es un link de *timeline*, y necesita
  `Timeline:SetClipsLinked`, que **no está en la lista de API medida** de
  Resolve Free. Se mide con los spikes **S7/S8** de `resolve/spikes_v2.lua`.
  `LIB.ligarGrupo` comprueba el método antes de usarlo y, si no existe, lo dice
  y sigue — no finge un link que Resolve no aplicó.

Cuando S7/S8 se corran, **escribir aquí el veredicto** para no volver a
preguntárselo.

**Veredicto en Studio 21.1.0.17 (2026-10-01), sin los spikes**: `Timeline:SetClipsLinked`
existe y liga. `lavas_al_corte.lua` ligó 39 grupos (video + audio de cámara + lavalier), y
la lectura de vuelta por `GetLinkedItems()` encontró cada lavalier ligado a su audio de
cámara. En Free sigue sin medir.

## La timeline de los audios: los lavalieres como timecode (Asistente, 2026-10-01)

Petición de Victor, el 1-oct: *"ahí quiero que metas los lavas, sin cortar, y sobre eso
coloques el B-roll y el A-roll"*. Berni lo había dicho en el rodaje del 28 con otras palabras:
*"los lavalieres funcionan como timecode"*. Es `ASISTENTE — AUDIOS EXTERNOS <día>`, hecha por
`LIB.construirLineaDeLavas`:

- **Lavalieres.** Cada WAV entero, sin cortes, en la pista de su TX (`LAVA izq`,
  `LAVA drc`), al final. Las horas vienen alineadas por contenido desde el horneado.
- **Video.** Encima, una pista por rol (`A-ROLL`, `B-ROLL`) y otra del mismo rol donde dos
  clips se enciman (`B-ROLL 2`). Cada clip va en su hora: la de su par validado por onda o,
  sin par, la del reloj de su cámara con su skew.
- **Audio de cámara**, solo de los clips SIN lavalier (`CAM sin lava`). El de uno con
  lavalier ya está debajo, entero, y el clip mergeado traería el lava otra vez: medido en
  Studio 21.1, Resolve riega el canal ligado en la pista siguiente.
- Todo se planea antes de tocar Resolve, con intervalos, porque en 21.1 un append pisa lo
  que hay.

Lo que tenía antes y la hacía inservible: una pista por ARCHIVO de WAV (7 el 28-sep), el
lavalier repetido en las pistas de cámara, y los dos TX corridos por sus relojes. Medido de
vuelta con `verificar_linea_lavas.py`: WAV enteros, 62 pares a 30 ms o menos contra su
lavalier, y ningún lava regado en una pista de cámara.

## Lavalieres debajo del corte del editor — `lavas_al_corte.lua` (Asistente, 2026-10-01)

Petición de Victor con su corte ya empezado: *"ponle el audio de los lavas a lo que llevo
del corte"*.

- **Duplica su timeline** (`V.1.0` → `V.1.0 + lavas`). La suya no se toca, y la lectura de
  vuelta lo comprueba contra una foto previa.
- **Parte del AUDIO de cámara que el editor usó, no del video.** Un plano que usó sin sonido
  no lleva lavalier, y un J-cut lo lleva con el corte de su audio. Los items apagados o a
  otra velocidad no se tocan, y se dice cuántos fueron.
- **Pistas al final**, `LAVA <tx>`, con el TX declarado en `lavalier_tx`. Donde dos clips del
  corte se enciman unos frames, el lavalier del de A2/A3 baja a `LAVA izq 2`, y el de A1 se
  queda arriba. Es el ajedrezado de diálogos: recortar decidiría por el editor qué frames se
  oyen.
- **Sync**: el segundo t del video cae en el t − offset del WAV, con el offset del
  horneado (tres decimales). El redondeo a frame deja 20 ms como máximo.
- **Liga** cada lavalier con su audio de cámara y su video.
- **WAV con la duración correcta**: con nombres repetidos entre TX, un Relink por nombre
  pudo dejar un item con la ruta de un WAV y la duración de otro (el `00034` de izq).
  Se elige el item cuya duración cuadra con el manifest, y si no hay, se importa.

Resultado en Asistente: 53 tramos en 4 pistas. Medido de vuelta contra el manifest con
`verificar_lavas.py`: 20.3 ms de error máximo y 9.6 ms de media, todos ligados, y `V.1.0`
idéntica. Corre igual en Free, con la línea de la Consola:

```
LAVAS_DATA = "<disco>/.cinema_assistant/resolve/<proy>_data.lua"; LAVAS_TIMELINE = "V.1.0"; LAVAS_SOLO_PLAN = true; dofile("<MOTOR>/resolve/lavas_al_corte.lua")
```

Primero con `LAVAS_SOLO_PLAN = true`, que dice qué haría, y luego sin él.

**Pendiente**: llevar `verificar_lavas.py` y la lectura de vuelta de las timelines
(`verificar_resolve.py`) al motor. Esta vez vivieron en el directorio temporal de la sesión.
El 7-oct se volvió a escribir otro igual en el temporal, y su primera versión dio un falso
"corrido 5 s": contó un tramo al 131 %. El del motor tiene que saltarse lo que va a otra
velocidad, igual que el script.

**Estéreo (Victor, 2026-10-07).** Quiere las pistas `LAVA` en **estéreo**, con el WAV mono
en los dos lados: Clip Attributes en Stereo, con el canal 1 a la izquierda y a la derecha.
Un mono en una pista estéreo se oye de un solo lado.

- Por item: `item:SetSourceAudioChannelMapping('{"track_mapping":{"1":{"channel_idx":[1,1],"mute":false,"type":"stereo"}}}')`.
- `SetAudioMapping` en el clip del bin no cambia los items que ya están en timelines
  (medido); sirve para lo que se ponga después.
- Va también para las voces y cualquier WAV mono que viva en esas pistas.
- **Pendiente:** `lavas_al_corte.lua` todavía crea las pistas `LAVA` en mono. Tiene que
  crearlas estéreo y mapear así cada tramo.

En `V.1.0 + lavas` se pasó a mano el 7-oct: las 4 pistas a estéreo desde la interfaz y 140
items mapeados. Además se apagó el audio de cámara donde hay lava debajo (98 items). El de
los clips sin lava se quedó encendido para que no queden mudos (Victor).

**Error de ese día, corregido el 8-oct: el audio de cámara solo se apaga donde están los DOS
transmisores.** Bastaba un lava debajo para apagarlo, y en casi todo el corte había uno solo
(el otro TX no se emparejó o no grabó a bordo). Así se perdieron las voces de Adrián y Bernie
en la mesa redonda, en el 9469 y en el 9780. Victor lo oyó.

- **Las cámaras VICG graban el receptor de los lavas, un TX por canal:** el canal 1 es el TX
  drc y el canal 2 el izq. Se midió el 8-oct, con 0.96 a 0.98 de parecido de envolvente, en el
  9780 y el 9470 (28-sep) y en el 9814 (29-sep).
- **En la mesa redonda, el canal 1 no coincide con ningún WAV drc a bordo:** la cámara tiene la
  única copia de esas voces.
- **El arreglo:** donde hay un solo TX debajo, el audio de cámara se enciende mapeado al canal
  del TX que falta, en los dos lados (`channel_idx` [1,1] si falta drc y [2,2] si falta izq). Ya
  está en sync y no duplica el lava que sí está.
  - En `V.1.0 + visualizador ar` fueron 92 items.
  - No se tocaron los de las cámaras ASA (no se midió su contenido) ni los que no tienen lava
    debajo, porque esos los apagó Victor.
  - El respaldo previo es `ProyectoAsistente-2026-10-08-antes-de-segundo-canal.drp`.
- **Regla para cualquier paso que apague audio de cámara:** antes de apagar, comprobar que los
  dos TX están debajo. Si falta uno, dejar el canal de cámara que lo trae.
  - El apagado del 7-oct se hizo a mano.
  - `lavas_al_corte.lua` no apaga audio de cámara, solo se salta lo que ya está apagado.
  - Si algún día lo hace, tiene que cumplir esta regla.

---

# Varias asistencias en un mismo proyecto de Resolve (2026-08-18)

Instrucción permanente del editor: *«adapta el asistente para que siempre
permita hacer varias asistencias diferentes en un proyecto»*. No es un caso
raro: abrió el día 2 de un cliente en el mismo proyecto donde ya tenía los reels
del día 1, y es lo natural cuando los dos rodajes son del mismo encargo.

Convivir exige **cuatro** cosas. Cumplir tres y fallar una basta para romperlo.

## 1. Las timelines no se pisan — el borrado mira el PFX

Cada script borra y reconstruye **sólo** las timelines que empiezan por su
prefijo. Esto ya estaba y funcionó: las `IMODAE — *` nunca corrieron peligro.

Lo que **no** estaba: nada impedía que dos proyectos nacieran con el mismo
prefijo. `nuevo_asistente_proyecto.py` ahora lo rechaza y dice cuál lo usa. Dos
proyectos con el mismo PFX se destruyen las timelines el uno al otro en cuanto
comparten proyecto de Resolve.

## 2. Los clips ajenos no se procesan — `LIB.esAjeno`

El script recorría el Media Pool entero y aceptaba cualquier vídeo, con un
comentario que decía que no mezclar rodajes era responsabilidad del editor. Eso
es un aviso escondido en el código, no una garantía.

Un clip que no está en el horneado no tiene `roll`, así que no cae en A-ROLL ni
B-ROLL ni BTS — y la **garantía de cobertura lo denunciaba como clip perdido**.
Medido en el caso real: **178 falsos positivos** tapando los de verdad.

> La definición operativa de «no es de este proyecto» es directa: el horneado
> tiene TODOS los clips indexados del disco, así que **si no está en el
> horneado, no es nuestro**. `dataFor` devuelve tabla vacía; eso es la señal.

Se saltan y **se dicen** con su número y los primeros cinco nombres. Callarlo
sería peor que el bug: el editor necesita saber que el script vio material que
no procesó.

## 3. Los bins no se mezclan — `moverABins` con prefijo

`MOVER_A_BINS` volcaba los clips en bins llamados `A-ROLL`, `B-ROLL` y
`BEHIND THE SCENES` colgando del bin compartido del asistente. Dos asistencias
volcaban en los **mismos tres bins**, y `restaurar_bins.lua` ya no podía deshacer
una sola. Ahora van prefijados: `DE2 — A-ROLL` junto a `IMODAE — A-ROLL`.

## 4. La garantía: `tests/test_multiproyecto.py`

Cinco comprobaciones sobre **todos** los `asistente_*.lua`: que carguen la
librería si la usan, que tengan la guarda y la etiqueta `::siguiente::`, que
`moverABins` lleve el PFX, que el borrado mire el prefijo, y que ningún prefijo
se repita. Probado que falla: con un script de mentira sin nada de eso, salta en
las cuatro.

Y `nuevo_asistente_proyecto.py` exige `multiproyecto` a la referencia **sea cual
sea el tipo de proyecto**, así que un proyecto nuevo no puede nacer sin ello.

## 5. Varios días de rodaje en UNA asistencia — `DIA` (Asistente, 2026-09-29)

Otro caso de convivir: el segundo día cayó en la misma carpeta del disco y el mismo
manifest, no en un proyecto aparte como CLIENTE_1 (`DE2`, `DE3`). B-ROLL y AUDIOS
EXTERNOS van por hora real, y con los dos días juntos la noche (17 h) quedaba en medio.

- El horneado lleva el `dia` de cada clip y cada WAV: su fecha LOCAL, sacada del instante en
  la Mac que hornea (un clip de las 22:24 es del 28 aunque en UTC ya sea el 29).
- Con `DIA = "2026-09-29"` antes del `dofile`, el aplicador arma solo ese día, las timelines
  llevan el día en el nombre (`ASISTENTE — B-ROLL 29-sep`) y **el borrado mira el PFX Y el
  sufijo**: rehacer un día nunca se lleva las del otro.
- La garantía de cobertura cuenta los clips del día, y los de otro día se dicen con su número.
- Sin `DIA`, el script hace lo de siempre, con los nombres de siempre.

Se aplica un día a la vez. En Studio, con el envoltorio de `Execute`:

```
python3 bin/aplicar_en_resolve.py --root "$DISK" --clave dia28 resolve/asistente_asistente.lua 'DIA="2026-09-28"' MODO_MERGE=true
python3 bin/aplicar_en_resolve.py --root "$DISK" --clave dia29 resolve/asistente_asistente.lua 'DIA="2026-09-29"' MODO_MERGE=true
```

En Free, la misma línea de la Consola con `DIA = "2026-09-29"` antes del `dofile`.

## Lo que destapó escribir la prueba

**Cuatro de los nueve scripts usaban `LIB.*` sin cargar `asistente_lib.lua`.**
`asistente_jilotepec.lua` era de la primera generación y nunca la cargó; los
otros tres se quedaron a medias en alguna regeneración. Ninguno lo habría dicho
antes de la Consola: **`luac -p` no caza un global nil**, es la lección de
siempre y volvió a aplicar.

De paso, `asistente_jilotepec.lua` filtraba el pool con
`string.find(binpath, "ESCALANDO MEXICO")` — un hardcode geográfico que además
fallaba si el material no colgaba de esa carpeta. La guarda genérica lo
reemplaza mirando el horneado, que es el dato correcto.

## Y una lección de entrega, no de código

El comando de la Consola se entregó dentro de un bloque de shell y con `echo`
delante. El editor lo pegó tal cual y le dio error, porque la Consola es **Lua**.

> El `dofile(...)` de Resolve **no es un comando de shell**. Se entrega como una
> línea suelta, sin `echo`, sin comillas envolventes y sin bloque `bash`, y con
> el recordatorio de que la Consola tiene que estar en modo Lua — en Py3 no
> existe `dofile`.

---

## Apilado multicámara en UNA timeline (Adrián, The Shelter 2026-08-04)

Variante del layout de arriba ("Layout de pistas con TRES cámaras"), para cuando el editor
quiere ver las cámaras **una encima de otra y en sync** en una sola timeline, y cortar entre
ellas sin buscar. Lo que aporta sobre lo que ya hacemos:

### La cámara que grabó continuo va HASTA ABAJO

| 2 cámaras | 3 cámaras (con CCTV) |
|---|---|
| V2 cámara B · V1 cámara A | V3 cám B · V2 cám A · **V1 CCTV** |
| A1 cám A · A2 cám B | A1 CCTV · A2 cám A · A3 cám B |
| A3/A4 lavaliers | A4/A5 lavaliers |

La CCTV cubría 141 de 148 minutos; **arriba habría tapado a las buenas todo el tiempo**.
Abajo hace de respaldo: se ve solo donde las demás no cubren. La regla generaliza: en una
pila, el criterio de orden no es la calidad de la cámara sino **cuánto tiempo ocupa**.

### Posicionamiento por tiempo real, con el lavalier como excepción

Cada clip va por **`t0`**, no por `mtime` crudo. Para un clip con sync,
`t0 = inicio_del_lavalier − offset`; dos clips que compartan lavalier quedan a su distancia
real, que es justo lo que permite cortar entre cámaras. Los clips sin sync se ubican por
reloj corregido con el skew de su cámara (±1 s: sirve para ubicar, no para lip-sync).

**El lavalier NO se coloca por tiempo absoluto**: va relativo a su clip con el offset del
par, porque el offset es una medida física del contenido y el `mtime` puede estar corrido
horas (ver la lección "El `mtime` miente, y de tres maneras distintas"). Y como con las
cámaras apiladas dos clips simultáneos pedirían el mismo lavalier en la misma pista, hay que
llevar registro de lo ya cubierto y colocar solo lo que falta.

### Colapsar los huecos

El tiempo muerto entre tomas hace timelines larguísimas (allí: concierto 122 → 75 min;
entrevistas 115 → 11.6 min). Se toma la unión de los tramos ocupados y se elimina lo de en
medio: **dentro de cada bloque las distancias quedan intactas** —la alineación entre cámaras
se conserva— y el primer bloque arranca pegado al inicio. Un clip nunca se parte porque su
intervalo está contenido en un bloque.

**Aviso**: al colapsar, los clips quedan pegados por construcción, así que la probabilidad
de solape de 1 frame es alta en toda la timeline. Ver la lección "La costura entre archivos
partidos no cae en frame entero".

### Qué tiene que comprobar el mock

`resolve/mock_resolve_stacked.lua` es de la copia de Adrián y **no está en este motor**, pero
su lista de comprobaciones sí es doctrina — es la que hay que exigirle a cualquier mock de
apilado:

- cada cámara en su pista según el layout de esa timeline;
- **sync del lavalier**: en el frame donde arranca cada clip, el WAV debe estar en el segundo
  que dice su offset (la verificación que de verdad importa);
- que el lavalier no rellene huecos ni colisione consigo mismo;
- que nada caiga antes del frame de arranque de la timeline (86400 por defecto);
- alineación entre cámaras contra la diferencia de sus `t0` reales;
- y las tres que costaron caro: devolver `{}` al rechazar en vez de un item nuevo siempre,
  dar a los items largo REAL en frames (`ceil(dur × fps)`), y correr al fps del proyecto
  (23.976, no 24 — con 24 las duraciones caen casi en frame entero y el solape no aparece).
