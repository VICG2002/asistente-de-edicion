# Reels y corte de silencios — la pieza con guion

Doctrina nacida de IMODAE "El estilo se vive" (6-ago-2026), el primer
**comercial** que pasa por el asistente. Un rodaje con guion rompe varios
supuestos que venian de los seis documentales anteriores.

## Que cambia en una pieza con guion

| | Documental | Pieza con guion |
|---|---|---|
| A-roll | la entrevista | quien dice el texto |
| Unidad de trabajo | el clip | el **grupo de tomas** (mismo texto) |
| La pausa | es contenido: se marca | es espera de prompter: se quita |
| Estructura | cronologia del dia | N capsulas rodadas el mismo dia |

Las tres filas de la derecha son las tres cosas que hubo que construir.

## 1. `project_kind: "comercial"`

Se **declara**, nunca se infiere. Un rodaje con guion se parece demasiado a la
ficcion desde el manifest, y adivinar mal reparte el A-roll entero por un umbral
que no aplica. La regla es directa:

> **A-roll = clip con fila en `clip_takes`.** Dice el texto guionizado. Todo lo
> demas es B-roll: recurso, detalle, dron, ambiente — tenga o no gente hablando
> de fondo, porque la charla de rodaje no sostiene la pieza.

En `lib/dialogue_density.decidir_comercial()`. No hay umbral que calibrar:
`derive_takes.py` ya sabe quien dice el texto, porque agrupa por el texto.

Si `derive_roll` ve 5 o mas clips agrupados como tomas y el proyecto NO es
comercial, lo dice: es la señal de que alguien olvido declararlo.

## 2. Reels / capsulas — `bin/derive_reels.py`

Un rodaje de campaña graba cinco o seis piezas el mismo dia y vuelve como una
cronologia plana. La asignacion va en dos pasadas:

**Por texto (fuerte).** La unidad es el **take group**, no el clip: todas las
tomas del mismo texto pertenecen a la misma capsula por construccion. El texto
canonico del grupo se puntua contra el vocabulario del tema de cada capsula
(`titulo` + `tema` + `escenario` + `palabras_clave` del brief).

Un termino vale sus palabras — que aparezca "mexican chic" dice muchisimo mas
que que aparezca "chic" — y los terminos se consumen de mas largo a mas corto
sin solaparse. Sin eso, "dos mujeres" y "mujeres" contarian dos veces la misma
evidencia y el score dejaria de significar nada.

**Por hora (debil).** El material sin dialogo no tiene texto contra el que
puntuar, pero si tiene hora: hereda la capsula que se estaba rodando en ese
momento. Si cae en dos ventanas solapadas no se asigna — dos capsulas
rodandose a la vez es justo el caso en que la hora deja de ser evidencia.

### El guion del brief NO es el texto rodado

**Medido en IMODAE**: el brief traia hooks cortos ("¿Que buscas primero cuando
armas un look?") y en el set se escribieron textos de prompter mucho mas largos
que no comparten una sola frase con el brief. Un matcheo por texto literal
habria dado **cero en las seis capsulas**. Por eso la señal es el vocabulario
del TEMA, no el guion.

Corolario practico: antes de correr `derive_reels`, **leer el brief y llenar
`palabras_clave`**. Con tres palabras por capsula el matcher no tiene con que
trabajar.

### El motor propone, el editor firma

`reports/reels-<fecha>.md` lleva, por grupo: la capsula propuesta, los puntos,
los terminos que hicieron match y el segundo candidato. Se corrige en un
renglon:

```json
"reel_overrides": {"grupo:96": 4, "clip:1234": 2, "grupo:101": null}
```

`null` fuerza *sin capsula*: saca del reparto la charla de rodaje que el
vocabulario emparejo por casualidad.

**No asignar es un resultado valido y frecuente.** En IMODAE, 13 de 19 grupos
eran charla de set y se quedaron fuera, que es lo correcto.

### Un clip puede cubrir dos capsulas — `clip_reel_segments`

`clip_reels` da UNA capsula por clip. Eso es correcto para decidir a que bloque
pertenece la toma, pero **un clip puede hablar de dos capsulas**, y durante un
tiempo la segunda desaparecia del reparto sin que nada lo dijera.

Caso fundador (IMODAE, 13-ago): `C1194` son 69 s que arrancan con el look de
viaje —capsula 3— y a los 39 s entran los maxi accesorios y la coleccion de
joyeria —capsula 1—. El vocabulario de viaje gano por puntos, el grupo entero
fue a la 3, y **se reporto que la capsula 1 no se habia rodado** cuando estaba
dentro de otro clip. Lo destapo el editor al pedir los subtitulos de un reel.

`segmentar_por_capsula()` recorre el clip en ventanas de 30 palabras (paso 15) y
puntua cada una. Los tramos van a `clip_reel_segments`, el bake los lleva como
`reel_segmentos` y el Lua pone un marcador **Fuchsia** dentro del clip, en el
punto donde entra la otra capsula.

**Los tiempos salen de los word-timings de Whisper, que DERIVAN** (§4). Sirven
para saber por donde entra el otro tema; **no** para cortar ahi. Nada de esto
alimenta un corte.

#### La regla que separa un cambio de tema de una coincidencia

Una capsula distinta de la dominante solo abre segmento si **supera al tema
dominante en esa ventana por un factor de 2**. Dicho de otro modo: el tema
anterior tiene que haber dejado de mencionarse.

Los dos casos que fijaron el numero, los dos reales:

| Clip | Ventana | Secundaria | Dominante | Veredicto |
|---|---|---|---|---|
| C1194 | 39-46 s | capsula 1: 2 ptos | capsula 3: **0** | cambio de tema REAL |
| C1170 | 35-53 s | capsula 6: 4 ptos | capsula 4: **3** | coincidencia de frase |

En C1170 la frase "es hacerla parte de tu vida" casa con el cierre del hero
video, pero el clip sigue hablando de la capsula 4 ahi mismo. Notese que el
falso positivo puntua MAS ALTO que el verdadero: **el valor absoluto no
discrimina, la ausencia del tema dominante si.**

Y una ventana necesita `min_puntos` de verdad (2). Se probo bajarlo a 1 con la
idea de que una señal debil pero sostenida bastara —para que pasara un fixture
de prueba inventado— y sobre el material real aparecieron dos falsos positivos
inmediatos: "accesorios" dicho de pasada dentro del discurso de otra capsula, y
"cafeteria" en una charla de rodaje.

> **Leccion de metodo, no de dominio:** el fixture de prueba se escribio
> parafraseando el clip en vez de copiando el transcript. Al comprimirlo, las
> palabras clave caian en otras ventanas, la prueba fallaba, y la tentacion fue
> aflojar el umbral hasta que pasara — rompiendo el material real dos veces. **Un
> caso real se prueba con el texto real.**

#### El guard: tres estados, no dos

Antes de decir que una capsula no se rodo, `derive_reels` la clasifica en:

1. **asignada** — tiene clips propios;
2. **solo en segmentos** — no tiene clips propios, pero SI hay material dentro de
   un clip asignado a otra;
3. **sin rastro** — no aparece en ningun transcript.

Solo el tercero significa "no hay material". El informe lo dice con esas
palabras y la consola avisa en el segundo caso: `NO decir que no se rodo`.

## 3. Corte de silencios — el paso opt-in

`bin/detect_pauses.py --seleccion takes` mide, `bin/derive_silence_cuts.py`
planifica, `bin/verify_cortes.py` garantiza, el Lua coloca.

**Es opt-in por diseño.** Peticion literal del usuario: *"ojo que esto no va a
aplicar siempre pero debes de poder hacerlo"*. En una entrevista la pausa es
contenido —es donde el entrevistado piensa, se emociona o cambia de idea— y el
asistente la MARCA (Sand) precisamente para que el editor la vea. Aqui es al
reves porque el material es distinto. Nunca se enciende solo.

Y solo en la timeline de **A-ROLL**: el B-roll no tiene habla que comprimir, y
cortarlo seria destrozar planos de recurso por medir silencio en un ambiente.

### El umbral se calibra por clip, no se fija

Un numero fijo de dB no vale para todo: entre el A1 de una camara grabando
caliente y una grabadora de campo conservadora hay 20 dB, y el mismo `-35 dB`
que en una caza todas las pausas en la otra no ve ninguna. **En IMODAE, `-35 dB`
encontraba 16 silencios en 47 clips; el umbral calibrado encontro 44.**

La regla, medida sobre cuatro tomas largas de dos locutores:

> **umbral = RMS del clip − 10 dB**, acotado a [−45, −18].

Se mide con `ffmpeg -af astats`. **No usar el "Noise floor" que astats tambien
imprime**: es el minimo instantaneo (en IMODAE daba −41 dB) y el ambiente real
durante una pausa esta muy por encima; calibrar con el deja el umbral tan bajo
que no detecta nada.

### El aire es la seguridad

De cada silencio no se quita todo: se dejan `--aire` segundos a cada lado, del
lado del habla. `silencedetect` marca donde el nivel cruza el umbral, y el
ataque de una consonante cruza tarde. Sin colchon, el corte se come la 'p' de la
palabra siguiente y suena a error de edicion, no a corte.

Default conservador elegido por el editor: **silencios ≥ 1.5 s, 0.3 s de aire**.
Es la perilla que se mueve si el resultado queda flojo o picado.

### `-vn` en el ffmpeg

Sin el, ffmpeg decodifica tambien el video para medir algo que solo mira el
audio. Con lavaliers no se noto nunca —son WAV— pero sobre el A1 de camara en 4K
la diferencia es de dos ordenes de magnitud.

## 4. Los tiempos de palabra de Whisper NO sirven para cortar

Esto es lo mas caro que enseño IMODAE, y va contra la intuicion.

La tentacion es evidente: hay word-timings cacheados, asi que "no cortar donde
hay una palabra" parece la garantia natural. **Esta medido que no funciona.**
Los word-timings aciertan en unos clips y derivan varios segundos en otros:

| Clip | Transcript dice hueco en | El audio tiene silencio en |
|---|---|---|
| C1178 | 29.1–31.0 (ahi hay voz) | 37.0–40.5 (no lo ve) |
| C1170 | 0.3–2.1 y 7.6–9.2 | 50.4–51.9 |
| C1199 | 22.4–28.2 y 46.4–51.6 | los dos, mas tres | 
| C1194 | 5.8–9.8, 24.4–29.4, 62.6–65.3 | los tres, mas dos |

Una garantia apoyada en esos tiempos habria vetado los dos unicos cortes buenos
de C1178 —matando la funcion— y habria dado verde falso en otros.

> **La autoridad es el audio.** El transcript sirve para agrupar tomas, no para
> decidir donde hay habla.

## 5. La garantia: `bin/verify_cortes.py`

Dos medidas distintas sobre la misma fuente. El corte se decidio con
`silencedetect` (deteccion por ventana movil); el verificador vuelve al audio y
mide el **RMS de cada tramo que se va a quitar**, ya con el aire descontado. Si
en un tramo hay voz, el RMS lo delata.

Comprueba ademas la estructura: ningun tramo se sale del clip, ninguno se
solapa, van en orden, y lo conservado mas lo quitado cubre el clip entero — un
segundo que no este en ninguno de los dos se perderia sin que nadie lo diga.

**Probado que falla.** Forzando `--aire -1.5` (los cortes invaden el habla), el
verificador caza los 38 cortes malos y sale con 1. Con el plan conservador, los
44 cortes pasan y el tramo mas ruidoso que se quita esta a 16 dB por debajo del
techo. Un verificador que nunca ha fallado no es una garantia.

Entra en la tanda obligatoria del §12b cuando el proyecto lleva cortes.

## 6. Marcadores de la pieza con guion

Colores NUEVOS, elegidos para no pisar la tabla de entrevistas de
[`marcadores.md`](marcadores.md) — reeducar el ojo del editor sale caro.

| Color | Significa | Fuente |
|---|---|---|
| **Sky** | `REEL n — <titulo>`, empieza el bloque de esa capsula | `clip_reels` |
| **Lavender** | `G96 T3/13 — completa`, empieza una toma | `clip_takes` |
| **Rose** | La toma trae un fallo cantado; la nota es la cita literal | `clip_takes.marcador` |
| **Sand** | Aqui se quito un silencio (opt-in `MARCAR_CORTES`) | `clip_keep_ranges` |

Todos de punto, como manda la regla dura. Responden a las tres preguntas que se
hace el editor al recorrer la timeline: ¿de que capsula es esto?, ¿que toma es?,
¿esta se cayo?

## 7. Behind the scenes: el tercer destino

**B-roll no es "todo lo que no es A-roll".** El B-roll ilustra la pieza y entra
en el montaje. El material de la cámara que anda por el set registrando el
rodaje es otra cosa —otra pieza, o ninguna— y mezclarlo en B-roll obliga al
editor a saltárselo clip a clip cada vez que busca un recurso.

Se declara por cámara, porque cuál es la cámara de BTS no se puede deducir del
manifest: la misma cámara es principal en otro rodaje. Lo sabe quien estuvo ahí.

```json
"bts_camaras": ["DJI_001"],
"bts_camaras_nota": "qué cámara es y por qué su material es BTS"
```

`derive_roll.py` le pone `roll='BTS'`, el Lua le hace su timeline
`<PFX> — BEHIND THE SCENES` y su bin, y el color de clip es **Violet** (ni el
naranja del A-roll ni el teal del B-roll: tiene que cantarse a la primera).

Precedencia: `roll_overrides` (por clip) > `bts_camaras` (por cámara) > la regla
del tipo de proyecto.

**Al abrir un tercer destino hay que ampliar la garantía de cobertura en el
mismo cambio.** El Lua comprobaba que entre A-ROLL y B-ROLL estuvieran TODOS los
clips; una garantía que sólo mira dos de tres destinos deja de serlo y no avisa
de nada. Ahora comprueba las tres y lo dice con números al cerrar.

**No confundir la cámara con su formato.** En IMODAE la carpeta `DJI_001` se dio
por dron sin comprobarlo; el tag `encoder` de los MP4 decía **DJI OsmoPocket 3**,
una cámara de bolsillo con gimbal. La corrección la hizo el editor. El dato
estaba en el manifest desde el primer día: `raw_metadata_json` → `format.tags.encoder`.

## 8. Subtítulos de un export

`bin/build_subtitles.py --video <export> --config <project_config.json>`.

**Sin `--offset-tc` los tiempos salen desde 0**, que es lo que hace falta para
subir el reel o para importarlo en su propia timeline. `--offset-tc` desplaza el
SRT al TC de la timeline maestra: sirve para pegarlo ahí, y **no** para entregar
el reel — con el offset puesto, el SRT empieza en 01:02:25 y ningún reproductor
muestra nada.

Cuatro errores que llegaron a un SRT entregable en IMODAE y ya no pueden volver:

1. **El vocabulario no se leía.** El script miraba sólo `vocabulary_notes`; el
   proyecto tenía sus correcciones en `vocabulary_fixes` (que es lo que usa
   `correct_transcripts_vocab.py`). Salió "Jimena" donde el proyecto canoniza
   "Ximena", anunciando "0 reglas de corrección" como si fuera normal. Ahora lee
   `vocabulary_fixes`, `vocabulary_hints` y `vocabulary_notes`.
2. **Se perdían palabras.** El envoltorio cerraba con `lines[:2]`: ante tres
   líneas se quedaba con dos y **tiraba el final de la frase sin decir nada**.
   Es el peor fallo posible en un subtítulo, porque se entrega y nadie lo nota.
3. **Medir caracteres no basta para saber si algo cabe en dos líneas.** Un texto
   de 78 no entra en 2×42 si las palabras sólo permiten cortar en 34+43. El
   mismo error estaba en tres sitios —el envoltorio, el troceado y la fusión— y
   arreglar dos dejaba pasar el tercero. Ahora se pregunta `cabe_en_dos_lineas()`.
4. **Cortes en palabra colgante.** "…las texturas y la" / "forma de combinarlos".
   El ojo se queda esperando el sustantivo. Hay una lista de artículos,
   preposiciones y conjunciones que no pueden cerrar línea ni cue.

**El CPS alto no siempre es un defecto.** Si el locutor habla a 19 caracteres por
segundo, los subtítulos van a 19: es una propiedad del habla, no del subtítulo.
Bajarlo exige **condensar el texto**, que es una decisión editorial y se firma a
mano. El script lo reporta y no lo toca.

### La timeline del reel: `resolve/reel_subtitulado.lua`

En la Consola, en **una sola línea**:

```
dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/reel_subtitulado.lua").correr({resolve = resolve, lib = os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_lib.lua", video = "/ruta/al/Reel 001 v3.mov", pfx = "IMODAE — ", hay_srt = true})
```

Desde el 2026-10-05 es un **módulo**, como `restaurar_bins.lua`: ya no lee
`REEL_*` ni mira el disco. **`hay_srt` lo pone quien arma la línea**, después de
comprobar que el `.srt` está junto al export: en el menú de Resolve Free no hay
`io` y el script no puede verlo. Sin `hay_srt`, abre la pista igual y el reporte
dice "sin comprobar". Un video que no existe ya no hace falta comprobarlo:
`ImportMedia` no lo importa y el script lo reporta como error.

Importa el export, arma su timeline **con la resolución del propio archivo** (un
reel vertical en un proyecto 16:9 hereda 1920×1080 y sale con bandas), y abre la
pista ST. El nombre de la timeline conserva la versión del archivo: con tres
exports del mismo reel en la carpeta, saber cuál está montado vale más que un
título bonito.

**El SRT lo pone el editor, en tres clics** (File → Import → Subtitle…). No es
pereza: **no existe API de subtítulos en Resolve**, ni Free ni Studio.
`AppendToTimeline` conoce mediaType 1 (video) y 2 (audio) y nada más.

#### Cuando el import "no pasa nada"

Resolve no da error si el import falla. Dos causas, las dos silenciosas:

1. **El SRT tiene saltos LF.** `open(f, "w")` en macOS escribe LF; el estándar
   de facto de SubRip es **CRLF**. Se escribe con
   `open(out, "w", encoding="utf-8", newline="\r\n")`, sin BOM.
2. **No hay pista ST en la timeline.** Sin pista, el import se ejecuta y no
   coloca nada. Clic derecho en los encabezados → Add Subtitle Track.

En IMODAE (2026-08-07) se arreglaron **las dos a la vez** y el import funcionó,
así que **no está aislado cuál era la culpable**. Da igual a efectos prácticos:
CRLF lo acepta todo lo demás, así que no hay nada que ganar volviendo a LF.

Y ojo con cómo se comprueba la pista: `pcall(function() tl:AddTrack("subtitle") end)`
devuelve `true` **también cuando AddTrack devolvió false**. Hay que contar las
pistas después con `GetTrackCount("subtitle")`. Es la misma regla de arriba —
"¿devolvió?" no es "¿funcionó?"— y se rompió en el mismo archivo donde se acababa
de escribir.

Y probar `mediaType=3` "por si acaso, que el coste es cero" **no sale gratis**:
medido el 2026-08-07, la llamada *devuelve un item* y lo coloca **en V1** — el
SRT entra como si fuera un clip de video, encima del reel, y el script se queda
tan ancho anunciando "subtítulos colocados en ST1". `DeleteClips` no siempre
está para deshacerlo.

> Regla que generaliza: **que una llamada de la API devuelva algo no significa
> que hiciera lo que se le pidió.** La comprobación no es "¿devolvió?" sino
> "¿está donde tenía que estar?". Es la misma lección de las pistas de audio que
> se perdían en silencio, con otro disfraz.

## 9. En la Consola de Resolve

```
CORTAR_SILENCIOS = true
MARCAR_CORTES = true
dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_<proyecto>.lua")
```

Con los flags apagados (el default) la A-ROLL sale integra. **Nada de esto es
destructivo**: los tramos son coordenadas de timeline, las fuentes no se tocan y
volver a correr el script sin el flag devuelve las tomas enteras.

Con cortes, un clip produce VARIOS items en la timeline. El emparejamiento
item→clip por ruta deja de valer (dos items comparten ruta) y pasa a ser **por
posicion**: `GetItemListInTrack` devuelve los items en el orden en que se
hicieron los append. Solo el primer trozo lleva los markers del clip; repetirlos
en cada fragmento llenaria la timeline de ruido.

## Orden de trabajo

```
index_project → transcribe_clips → correct_transcripts_vocab
  → derive_takes            (agrupa las tomas por texto)
  → derive_reels            (las reparte entre capsulas)  ← leer el informe
  → derive_roll             (A/B con la regla comercial)
  → detect_pauses --seleccion takes
  → derive_silence_cuts
  → verify_cortes           ← la garantia
  → export_lua_data + export_multicam_lua
  → Consola de Resolve
```

`export_multicam_lua` hace falta aunque no haya multicam: es el vehiculo de los
`camera_skews_manual` hasta el Lua. Un rodaje a varias camaras con solo audio de
camara los necesita igual.

---

# Actualización v0.4.1 — el comercial con entrevistas (CLIENTE_1 día 2, 2026-08-18)

Segundo rodaje del mismo cliente y primer proyecto que mezcla **piezas
guionizadas con intentos** y **un evento largo** el mismo día. Todo lo de abajo
está medido sobre ese material: 108 clips, 68 min, dos cámaras, sin audio
externo.

## 1. A-roll también es la entrevista — `aroll_incluye_entrevistas`

La regla de IMODAE («A-roll = quien dice el texto guionizado, todo lo demás es
B-roll») manda a B-roll cualquier cosa que no esté agrupada como toma. En este
rodaje eso eran **cinco clips de 3 a 9 minutos**, 30.7 min de conversación
grabada, que es el material principal del encargo.

Segunda vía a A-roll, **opt-in y declarada**:

```json
{ "project_kind": "comercial", "aroll_incluye_entrevistas": true }
```

`decidir_comercial()` la aplica sobre `CATEGORIAS_ENTREVISTA`. Apagada (el
default) ningún proyecto anterior cambia. Y `derive_roll` **lo dice siempre**,
encendida o apagada, con el número de clips categorizados como entrevista: si el
flag falta en un proyecto que sí tiene entrevistas, se ven irse a B-ROLL.

## 2. Un grupo de UNA sola toma no es una toma

`derive_takes` abre un grupo por cada clip con diálogo, se repita o no. «Tener
fila en `clip_takes`» incluía por tanto la charla de rodaje: un *«ya está
aprobado»* de 11 s, un *«dile a mi asistente que son uno arriba y uno abajo»*. Con
la regla vieja, 24 clips de charla entraban a A-ROLL.

> **La señal de que algo es una toma es que el texto SE REPITIÓ.** Grupo de dos
> o más. Un grupo de uno es un clip suelto con diálogo, que es justo lo que
> puede ser una entrevista.

La regla vive en **tres sitios y tiene que decir lo mismo en los tres**:
`derive_roll` (quién es A-roll), `derive_video_categories` (señal 0) y
`export_lua_data` (qué clips llevan `toma` al horneado). El tercero se olvidó al
principio, y el síntoma fue doble y visible: el color por grupo de tomas pintaba
el evento como si fuera un reel, y el corte «solo en tomas» consideraba tomas los
8 clips del evento. **Una regla derivada que vive en varios scripts se comprueba
en el horneado, no en el script.**

## 3. La toma guionizada NO es una entrevista — señal 0

La densidad léxica no las distingue: un texto de prompter de 60 s es tan denso
como una respuesta de entrevista. Medido: `derive_video_categories` marcó como
`entrevista` **20 clips, de los cuales 15 eran intentos de cinco reels**.

Señal 0, antes que ninguna otra: clip en grupo de 2+ tomas → categoría
`toma-guionizada`. Sale de un hecho medido (el texto se repitió), no de un
umbral. Con ella, `entrevista` quedó en los 5 clips que de verdad lo son.

Y `category_overrides` en el project_config para lo que ninguna señal alcanza:
tres fragmentos del mismo evento (9 s, 11 s y 35 s) se quedaban bajo el umbral de
60 palabras distintas por **cortos, no por pobres**.

## 4. El corte de silencios puede no tener NADA que cortar

Petición del editor: cortar las pausas de las entrevistas. Resultado medido:
**cero silencios**, y no por calibración conservadora — en `C1252` y `C1256` no
hay un solo silencio de 1 s por debajo de **−45 dB**. Es un evento en sala llena:
el ambiente no baja nunca.

> El corte de silencios asume un set controlado. En directo, con público, el
> piso de sala tapa las pausas y **no hay nada que quitar**. Decirlo con el
> número es la respuesta; subir el umbral hasta que aparezcan cortes no corta
> silencio, corta habla — y el verificador lo rechaza.

Donde sí había pausas era en las tomas (52 cortes, 151 s), y ahí fueron. La
lección de método: **medir las dos hipótesis antes de contestar**, porque el
material que el editor cree que tiene pausas puede no tenerlas.

Un tropiezo propio que vale documentar: la primera medición manual dio 0
silencios *a cualquier umbral, incluso 0 dB*, lo cual es imposible. La causa era
`ffmpeg -v error`: **`silencedetect` escribe a nivel `info`**, así que el flag
ocultaba justo lo que se estaba midiendo. El motor lo hace bien (`-v info`); el
error fue de la comprobación a mano. Un resultado imposible es un fallo de
instrumento hasta que se demuestre lo contrario.

## 5. El corte va en TIMELINE APARTE, no muta la A-ROLL

Petición literal: *«que hagas el intento de recortar los silencios, pero en una
timeline a parte»*. En IMODAE, `CORTAR_SILENCIOS` mutaba la A-ROLL y comparar
entero contra cortado exigía re-correr el script con el flag al revés.

Ahora conviven: `<PFX> — A-ROLL` íntegra y `<PFX> — A-ROLL SIN PAUSAS`, espejo
con los mismos clips, el mismo orden y los mismos colores.

Y **qué se corta se decide por CLIP, no por timeline**: `CORTES_EN` vale
`"tomas"`, `"entrevistas"` o `"todo"`. Un booleano por timeline no alcanza
cuando en la misma A-ROLL conviven tomas con espera de prompter y un evento
donde no hay pausa que quitar.

El espejo **no cuenta en la garantía de cobertura**: sus clips ya están en
A-ROLL. Si contara, el verificador diría que hay más clips colocados que clips en
el proyecto y dejaría de significar nada.

## 6. Color por grupo de tomas en A-ROLL

Petición del editor: *«colores dependiendo de la toma que es y sus intentos,
para poder distinguir los diferentes Reels que hay»*. Con `colorDeRoll` el
A-roll entero sale naranja y catorce intentos seguidos son una mancha.

`LIB.colorDeGrupo` reparte **doce** de los 16 colores de Resolve. Se reservan
cuatro que ya significan otra cosa y reusarlos haría que una toma se leyera como
recurso al pasar de largo: Orange = A-roll sin grupo, Teal = B-roll,
Violet = behind the scenes, Chocolate = descarte.

**El color sale del ID del grupo, no de su posición.** Así re-hornear no
recolorea la timeline entera, y sobre todo: no cambia cuando llegue el orden
definitivo del cliente, porque el número de reel es provisional y el grupo de
tomas es el dato medido.

## 7. El número de toma se ordena con el reloj CORREGIDO

`derive_takes` ordenaba por `creation_time` crudo. Con dos cámaras cuyos relojes
no coinciden eso numera mal: la Sony va 11 h 50 min por delante de la Osmo, así
que sus tres tomas salían como T4, T5 y T6 **detrás** de las tres de la Osmo,
cuando son los mismos tres intentos vistos desde la otra cámara. El marcador
decía «T4/6» de algo que es el intento 1. El mismo fallo estaba latente en
IMODAE, donde la A74 iba +12 h.

Corolario que sigue abierto: el numerador cuenta **clips, no intentos**. Un reel
cubierto por dos cámaras dice «6 tomas» de 3 intentos. Se avisa en el informe.

## 8. El skew se MIDE cuando dos cámaras cubren la misma toma

En el día 1 el desfase de la cámara se dedujo a ojo y quedó anotado como no
comprobado. Aquí se midió, y la primera hipótesis a ojo (**+10 h exactas**)
estaba **mal por casi dos horas**.

El método, cuando existe: buscar en los transcripts la toma que **las dos
cámaras cubrieron** y emparejar intento por intento.

| Osmo | Sony | delta |
|---|---|---|
| DJI_0001 18:10:45Z | C1234 06:01:43Z (+1d) | 11:50:58 |
| DJI_0002 18:13:11Z | C1235 06:04:09Z (+1d) | 11:50:58 |
| DJI_0003 18:15:41Z | C1236 06:06:39Z (+1d) | 11:50:58 |

Tres parejas, el mismo número al segundo. Y una **prueba independiente del
emparejamiento**: los intervalos entre intentos consecutivos coinciden al segundo
en las dos cámaras (146 s y 150 s), así que los dos relojes corren al mismo
ritmo y las parejas son correctas.

Tercera corroboración, gratis: con el skew aplicado, `export_multicam_lua`
verifica los tres pares por contenido A1↔A1 con residuo de −1.52, −1.37 y
−0.92 s. Si el skew estuviera mal por más de unos segundos no habría traslape
que medir y no habría par.

## 9. Un falso positivo por texto contamina la herencia por hora

`derive_reels` reparte el material sin diálogo por la hora: hereda la cápsula que
se rodaba en ese momento. Medido aquí: **C1250**, 327 s del evento donde se lee
en voz alta un texto sobre Pedro Friedeberg, casó por vocabulario con la cápsula
de los grabados. Eso **estiró la ventana de esa cápsula de 11:06–11:11 a
11:06–13:06 —dos horas—** y con la ventana estirada otros **23 clips** heredaron
la cápsula por hora: todo el recurso de la mañana, la charla de set y el propio
evento.

> Un falso positivo por texto no se queda en su clip: **mueve el borde de la
> ventana y arrastra todo lo que cae dentro.** Las ventanas hay que mirarlas —
> una cápsula de 40 s cuya ventana dura dos horas es una alarma, no un dato.

Y la premisa de la herencia por hora —que las cápsulas se reparten el día— **no
se cumple** cuando en medio hay un evento que no es ninguna cápsula. Con
`--sin-ventanas`, cada cápsula se queda exactamente con su grupo de tomas: 26
clips, cero por hora, y el recurso queda «sin cápsula», que es la verdad.

---

# Actualización v0.7.0 — el subtítulo se corta donde se corta el sonido (Morsa, 2026-08-19)

Victor lo dijo mirando sus propios reels: *«una referencia que puedes tomar son
los cortes, a menos que sea un voice over los cortes te dan la pauta ideal»*.
Esta sección es esa frase medida, y lo que hace falta para aplicarla.

## 1. La timeline de Resolve se puede leer sin abrir Resolve

Hasta ahora el motor sólo sabía **escribir** en Resolve. El dato más valioso que
produce el editor —dónde decidió cortar— estaba fuera de alcance.

La base de disco guarda cada proyecto en un `Project.db` que es **SQLite
corriente**. `lib/timeline_resolve.py` lo abre en sólo lectura (copia a temporal,
`mode=ro`) y devuelve fps, resolución, pistas, items y cortes.

    ~/Library/Preferences/Blackmagic Design/DaVinci Resolve/activedb.conf
        -> "disk*:VICG"
    .../dblist.conf
        -> "VICG:$HOME/Movies/Resolve Project Library:*:::DISK"
    <ruta>/Resolve Projects/Users/guest/Projects/<Proyecto>/Project.db

Esquema medido sobre Resolve 21.0.2 (`DbPrjVer 17`):

| tabla | lo que hace falta |
|---|---|
| `Sm2Timeline` | `Name`, `Sequence`, `ModTimeInSecs` |
| `Sm2Sequence` | `FrameRate` (double **LE**), `Resolution` (dos int64 **BE**), `MediaExtents` (dos doubles LE: inicio_s, duración_s) |
| `Sm2SequenceContainer` + `..._Sm2TiTrack` | la unión secuencia→pista |
| `Sm2TiItem` | `Name`, `Start`, `Duration`, `In`, `MediaFilePath` |

**Tres trampas, ya pagadas:**

- Existe una tabla `Sm2Sequence_Sm2TiTrack` con toda la pinta de ser la unión.
  Está **vacía** (0 filas en Morsa, con 243 pistas reales). La buena es la del
  contenedor. Consultar la equivocada no da error: devuelve cero filas, que se
  lee como «esta timeline no tiene pistas».
- El frame rate es **little-endian** y la resolución **big-endian**, en
  registros vecinos de la misma tabla. Leerlos igual da 23.976 y una resolución
  de 4222124650659840.
- `Start` y `Duration` pueden traer **fracción de fotograma**:
  `92956|00a0246afd9de83f`, donde lo de después de la barra es un double LE en
  hexadecimal. `int()` revienta y `CAST(... AS INTEGER)` de SQL tira la fracción
  en silencio: siete items de Morsa desaparecían. Comprobación bonita de que el
  parseo es correcto: en un item cortado, la fracción del `Start` (0.769286) y
  la del `Duration` (0.230714) **suman exactamente 1.0**.

Lo que **no** está ahí son los settings de proyecto: viven en
`SM_Config.SetupBA`, un struct binario de 2704 bytes sin nombres de campo. Para
eso está `resolve/auditar_settings.lua`, que los pide por la API.

## 2. La duración de la timeline no es la duración del corte

`Cut 1.2 sonido ayan` mide **78:10** de línea de tiempo y sólo **24:56 son
contenido**, en 7 bloques separados por huecos de hasta 21:32. Lo que se entregó
—`Morsa ultimate cut.mov`— es el **primer bloque, 16:35.7**. Todo lo demás es
material aparcado más allá del primer hueco.

> **Antes de medir nada sobre una timeline de trabajo, párte­la en bloques y di
> con cuál trabajas.** Aquí la diferencia entre la timeline y el corte es de 61
> minutos. `Timeline.bloques()` lo hace.

## 3. Sólo manda el corte que también corta el sonido

En los 995.7 s del bloque entregado hay **366 cortes de imagen**. De ellos,
**77 cortan también el audio de diálogo** (cámara o lavalier *declarados* en el
`project_config`, con tolerancia de 2 fotogramas). Los otros **289 son de solo
imagen**: B-roll encima de un audio que sigue corriendo.

| | n | separación mediana | cae dentro de una pausa medida |
|---|---:|---:|---:|
| corte **sincrónico** (imagen + sonido) | 77 | 7.44 s | **31 %** |
| corte de **solo imagen** | 289 | 2.04 s | 15 % |
| punto al azar (línea base) | — | — | 9 % |

La pausa se midió con `silencedetect` a −34 dB sobre el propio máster: 90 pausas
de 0.30 s o más.

> **Un corte solo manda sobre el subtítulo si también corta el sonido.** El
> sincrónico acierta una pausa de habla 3.4 veces más que el azar y llega con la
> cadencia de un cue (7.44 s). El de solo imagen está al nivel del azar y llega
> cada dos segundos: eso es el voice over, y ahí el corte no sabe nada del habla.

La clasificación se deriva de lo **declarado** (`cameras[*].folder`,
`filename_prefix`, claves de `audio_folders`). Lo que no encaja sale
`desconocido` y **no cuenta como diálogo**: meter por defecto la música ahí
convertiría un corte de canción en un borde de subtítulo. En Morsa quedan 98
items sin declarar y el motor lo dice en cada corrida.

## 4. Prohibición e imán, no tabique

El corte acierta la pausa el 31 % de las veces, no el 100 %. Forzar un borde en
cada corte partiría frases por la mitad el otro 69 %. Son **dos reglas
distintas**:

- **Prohibición**: ningún cue cruza un corte sincrónico. Si partirlo dejara un
  trozo por debajo de 1 s, o mucho más denso que la frase de la que sale, **no
  se parte y se dice** con su timecode.
- **Imán**: un borde que ya caía a menos de 10 fotogramas de un corte se clava a
  su fotograma exacto. El final sale **2 fotogramas antes** del corte: un
  subtítulo que sobrevive al plano aunque sea un fotograma se lee como error de
  edición.

Nunca se crea un borde donde el habla no lo pedía.

## 5. La prueba: Victor ya lo hacía a mano

En la timeline `Reels verticales`, rango del Reel 3:

- **9 de los 10 cortes sincrónicos tienen un borde de subtítulo EXACTAMENTE
  encima** (0 fotogramas).
- De los **31 cortes de solo imagen, sólo 10** lo tienen.
- Al revés: **el 33 % de los bordes de subtítulo cae en el fotograma exacto de
  un corte de imagen, contra un 2 % de azar** (16×).

Cortando y pegando a mano, el editor puso un borde en casi todos los cortes que
llevaban sonido y en apenas un tercio de los que no. La regla estaba aplicada
antes de estar escrita.

Y los reels se cortan mucho más rápido que el corte largo: **plano de 2.31 s de
mediana contra 4.21 s**, con cues de 3.95 s. Un cue dura menos de dos planos.
Razón de más para no partir en los cortes de solo imagen: ahí llegan cada 1.7 s.

## 6. El texto no se reparte por regla de tres

Al partir una frase en un corte hay que decidir qué palabras van a cada lado.
Repartir por proporción del tiempo **miente**: una frase de 70 s con una pausa
de 50 s dentro no lleva sus caracteres repartidos uniformemente.

Se usa la propia segmentación de Whisper (`piezas`, que ya traen tiempos por
trozo): si el corte cae cerca de un límite entre piezas se parte ahí y el
reparto es exacto; si cae dentro de una pieza se parte **esa y sólo esa** por
proporción, que a escala de un segmento de Whisper sí es fiable.

Tres cosas que salieron mal por no hacerlo así, todas medidas el 2026-08-19:

- Buscar el punto de corte con `rfind` en una ventana del 25 % del texto se
  queda con el **último** límite de la ventana, no con el más cercano: puso 45
  caracteres donde tocaban 25 y el cue salió a 25.4 CPS. Buscar «cerca» no es
  buscar «el último de por aquí».
- Los offsets de cada trozo **se buscan en el texto**, no se acumulan sumando
  longitudes: los trozos salen con `.strip(",")` y el acumulado se desfasa trozo
  a trozo. Con seis trozos ya dejaba 43 caracteres en 1.33 s.
- Al partir en un límite de pieza, las piezas del borde hay que **recortarlas al
  corte**. Sin recortar, el texto de una pieza se queda con el tiempo de la
  otra: 43 caracteres en 1.35 s donde la pieza original tenía 1.94.

En los tres casos el síntoma fue el mismo —un cue imposible de leer— y la causa,
un reparto de tiempo que no se correspondía con el del texto.

## 7. El CPS alto no siempre es culpa del motor

Medido sobre el máster de Morsa: **47 de los 171 segmentos que devuelve Whisper
ya vienen por encima de 17 CPS**, con mediana 15.0 y p90 19.4. La gente habla
así. Bajar de 17 sólo se consigue **condensando el texto**, y este motor no
condensa por doctrina: no inventa palabras ni se las quita a nadie.

> El CPS cuenta como incumplimiento **sólo cuando había sitio por delante y no
> se usó**. El resto se reporta con el número, sin poner el verificador en rojo
> permanente por cómo habla un señor de 73 años.

Corolario para el imán y para el corte: **ninguno puede empeorar la densidad**.
Clavar el inicio de un cue a un corte 60 ms más adelante le quita 60 ms de
lectura, y en un cue apretado eso lo empuja de 31.6 a 33.1 CPS. Ganar un
fotograma de precisión a cambio de que no se pueda leer es un mal cambio.

## 8. Lo que se gana, con el número

Mismo transcript, mismo material, con y sin los cortes de la timeline:

| | sin cortes | con cortes |
|---|---:|---:|
| cues | 109 | 113 |
| cruzan un corte sincrónico | 31 | **23** |
| bordes pegados a un corte (±2 f) | 6/218 (3 %) | **37/226 (16 %)** |
| cues por encima de 17 CPS | 31 (máx 21.9) | **27 (máx 21.6)** |

Los 23 que siguen cruzando son los que **no se podían partir** sin romper otra
regla, y el script los cuenta y sugiere el `--max-cruces` con el que
verificarlos. Declararlos no es esconderlos.

## 9. El SRT, el TTML y el quemado no dicen lo mismo

Al cortar el subtítulo con la imagen quedan fragmentos que **comparten texto**.
En el rango del Reel 3 hay **41 items** de subtítulo en la timeline: 20 textos
distintos y 21 items que repiten el del anterior.

`Reel 3.srt` tiene **20 cues**. 41 − 21 = 20: **al exportar SRT, Resolve funde
los items consecutivos de texto idéntico y la fragmentación desaparece.** El
TTML de la misma pieza parte la presentación de un entrevistado en **tres
bloques con texto distinto** donde el SRT los junta en uno solo, y el fotograma
quemado coincide con el TTML, no con el SRT.

> **Tres artefactos, tres segmentaciones.** Si el reel se publica quemado en un
> sitio y con el SRT en otro, no dicen lo mismo en el mismo instante. Hay que
> decidir cuál es el original antes de entregar.

## 10. Cómo se corre

    python3 bin/build_subtitles.py --video <export.mov> --config <project_config.json> \
        --timeline "<nombre de la timeline>"

`--proyecto` sale del `project_name` del config. `--offset-tc` sigue queriendo
decir «TC de la timeline donde arranca el export», y ahora sirve para dos cosas:
desplazar el SRT y **situar los cortes**. Sin él se asume que el export empieza
en el TC de inicio de la timeline, que es lo que pasa en 15 de los 18 renders de
Morsa.

`--desde <TC>` escribe sólo los cues posteriores a ese timecode, renumerados
desde 1 pero **conservando su timecode original**. Es exactamente lo que Victor
había hecho a mano en `Exports/Reel 3 — solo lo que falta.srt` para reimportar
en Resolve nada más que lo nuevo.

Y después:

    python3 bin/verify_subtitulos.py --srt <export.srt> --video <export.mov> \
        --config <project_config.json> --timeline "<nombre>"

Comprueba contra el **audio** y contra los **cortes**, no contra el transcript
que lo generó. Dos medidas distintas sobre la misma fuente, como
`verify_cortes.py`.

## 11. El ancho del cuadro decide cuántos caracteres caben por línea

Los 42 caracteres/línea son una convención de LECTURA (EBU), no de ancho: salen
de cuánto se lee cómodo de una pasada. En 16:9 nunca ata el ancho —con la
fuente de subtítulo de Resolve caben ~100 por línea—, así que 42 es siempre el
límite real. **En 9:16 sí ata, y por debajo de 42.**

Medido restando, frame a frame, el export limpio del export con subtítulos
quemados (cuatro reels de CLIENTE_1, 2160×3840, 2026-08-25):

    alto de línea                       116 px  = 3.0 % del alto de cuadro
    ancho medio de carácter               56 px  = 0.48 × el alto de línea
    línea más ancha con el límite de 42   1960 px = 91 % del ancho de cuadro
    margen que quedaba                     98 px  = 4.5 % por lado

Con 42 el texto llegaba al borde. `lib/subtitulos.chars_por_linea(ancho, alto)`
devuelve el mínimo entre el límite de lectura y el que impone el ancho del
cuadro (margen de seguridad 10 % por lado, deliberadamente más conservador que
el peor caso medido de 4.5 %):

    16:9  (cualquier resolución)  ->  42   (el ancho nunca ata)
    9:16  (cualquier resolución)  ->  31   (ata la geometría, no los píxeles)

`build_subtitles.py` lo aplica solo: lee el ancho/alto del export con ffprobe e
imprime `Cuadro: 2160x3840 (vertical) - 31 caracteres por línea` antes de
transcribir. `--max-chars` lo fuerza a mano; `project_config.json ->
"subtitulos": {"max_chars_linea": N}` lo fija por proyecto. `configurar(n)` en
`lib/subtitulos.py` es el único punto de entrada correcto para cambiarlo en
caliente: `MAX_CHARS_LINE` se importa por VALOR en `build_subtitles.py`, así que
reasignar sólo el nombre del módulo no le llega a las funciones puras
(`wrap_lines`, `cabe_en_dos_lineas`) que lo leen como global propia.

**Reflujar un SRT ya curado a un límite más estrecho no es sólo re-envolver
texto.** Un cue que cabía en 2 líneas de 42 puede no caber en 2 de 31, y ahí la
única salida es partirlo en más cues — lo que exige repartir también el
TIEMPO. La frontera correcta no es proporción de caracteres: es el tiempo real
de la palabra donde se corta, medido con Whisper `-ml 1 -sow` (ver
`lecciones/errores-comunes-a-corregir.md`, "`-ml 1` sin `-sow`..."). En los
cuatro reels de esta cobertura, 17 de 48 cues necesitaron partirse; los otros
31 sólo cambiaron de envoltura.

> **Aviso para el editor**: si el SRT se corrige DESPUÉS de haber quemado un
> export (`*.mov` con subtítulos ya renderizados en Resolve), el archivo
> quemado no se actualiza solo. Hay que reimportar el SRT nuevo y re-exportar.
