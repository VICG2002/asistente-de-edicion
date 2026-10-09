# A-roll / B-roll (v0.2.0)

## Reglas del usuario

- **Documental**: las entrevistas son **A-roll automáticamente**. Sin discutir,
  sin mirar densidad.
- **Ficción**: las tomas con **mayor densidad de diálogo** son A-roll.
- **Comercial** (v0.4.0): **A-roll = quien dice el texto guionizado**, o sea
  todo clip con fila en `clip_takes`. Sin umbral que calibrar: `derive_takes.py`
  ya lo sabe porque agrupa las tomas *por* el texto. Todo lo demás es recurso.
- Todo lo demás, **B-roll**.
- **Behind the scenes** (v0.4.0): la cámara que registra el rodaje va a su
  propia timeline, no al B-roll. Se declara por cámara:
  `"bts_camaras": ["DJI_001"]`. Manda sobre la regla del tipo de proyecto, y
  `roll_overrides` manda sobre ella.
- `export` y `discard` no son ni A ni B: van a `PFX — DESCARTES`.

## Tipo de proyecto

`<disco>/.cinema_assistant/project_config.json`:

```json
{
  "project_kind": "documental",
  "roll_overrides": { "1234": "A", "1250": "B", "1299": "descarte" }
}
```

Si `project_kind` falta, se **infiere** (¿hay categorías de entrevista? →
documental) **y se avisa en pantalla**. Nunca se decide en silencio: si la
inferencia es incorrecta, la clasificación entera lo es.

`"comercial"` **nunca se infiere: se declara**. Un rodaje con guion se parece
demasiado a la ficción desde el manifest, y adivinar mal reparte el A-roll
entero por un umbral que no aplica. Si `derive_roll` ve 5 o más clips agrupados
como tomas y el proyecto no es comercial, lo dice — es la señal de que alguien
olvidó declararlo. Detalle en [`reels-y-cortes.md`](reels-y-cortes.md).

## La métrica: `dialogue_density` ∈ [0,1]

Todas las señales ya estaban en el manifest. No se calcula nada nuevo ni se
llama a ningún modelo.

```
speech_coverage = Σ(content_segments.end - start) / duration_sec
lexical_rate    = distinct_words / (duration_sec / 60)
intent_bonus    = 1 si tiene par de sync o preguntas detectadas, si no 0

dialogue_density = clamp01( 0.50 * speech_coverage
                          + 0.35 * min(lexical_rate / 60, 1.0)
                          + 0.15 * intent_bonus )
```

`60` palabras distintas/min es el mismo umbral que usa
`derive_video_categories.py --min-distinct`.

`intent_bonus` no mide contenido, mide **intención**: si alguien se molestó en
ponerle un lavalier a ese clip, ese clip importaba.

### CANDADO — transcript alucinado ⇒ densidad 0

Si `transcript_quality.is_hallucinated = 1`, la densidad es **0** y el clip cae
a B-roll con el motivo *"transcript no confiable (alucinado)"*.

Sin esto, Whisper sobre música ambient produce texto que **parece** denso
("Suscríbete al canal" ×100) y manda paisajes a A-roll. Es la lección fundadora
de Zezzions con otra cara. Hay prueba que lo fija
(`test_transcript_alucinado_anula_la_densidad`).

## Ficción: umbral adaptativo, no fijo

El corte es el **percentil 60 de la distribución del proyecto**, con piso en
`0.30`. El umbral efectivo se reporta en pantalla y en el informe.

**Por qué no un umbral fijo**: la densidad absoluta depende del guion. Una
escena de acción con tres líneas es A-roll *en su película*. Un umbral fijo
clasificaría mal proyectos enteros.

**El piso existe** para que un proyecto entero de densidad casi cero no mande
todo a A-roll sólo porque su percentil 60 es 0.03.

### La decisión se toma por ESCENA, no por toma

En ficción los clips son tomas de una escena. Se usa la **mediana** de densidad
de las tomas de esa escena y se propaga a todas.

Sin esto, el inserto mudo de una escena de diálogo cae en B-roll y **rompe la
continuidad del A-roll**.

Convenciones de nombre/carpeta que se reconocen:

| Patrón | Ejemplo | Escena | Toma |
|---|---|---|---|
| `ESC<n>_T<n>` | `ESC12_T03.mov` | 12 | 03 |
| `SC<n>_TK<n>` | `SC07_TK02.mp4` | 07 | 02 |
| `<n><letra>-<n>` | `12A-3.MOV` | 12A | 3 |
| `E<n>T<n>` | `E5T11.mov` | 5 | 11 |
| carpeta por escena | `Escena 8 - cocina/8B-12.mov` | 8B | 12 |

Si menos de la mitad de los clips tiene escena reconocible, el script **avisa**:
la decisión está cayendo a nivel clip y eso rompe continuidad.

### Empates a favor de A-roll

Dentro de `±0.05` del umbral, gana A-roll. Es más barato revisar un clip de más
en A-roll que perder una toma de diálogo en B-roll.

## Salida — auditable y corregible

Tabla `clip_roll(clip_id, roll, score, scene, take, reason, source, decided_at)`.
El `reason` está **en español y es legible**:

> `entrevista (categoria 'entrevista') — regla documental · habla 80% del clip,
> 60 palabras distintas/min, con lavalier o preguntas detectadas`

Informe en `<disco>/.cinema_assistant/reports/informe_roll.md` con la
distribución, el umbral efectivo y el A-roll completo con su motivo.

**Revisar el informe ANTES de dejar que el script toque bins.**

Para corregir un clip: `roll_overrides` en `project_config.json`. Los overrides
ganan sobre cualquier regla y quedan marcados con `source='override'`.

## Las cuatro materializaciones en Resolve

| Dónde | Qué | Destructivo? |
|---|---|---|
| **Timelines** | `PFX — A-ROLL` y `PFX — B-ROLL`, cronológicas, con sus markers | No |
| **Keyword** | `A-ROLL` / `B-ROLL` en el Metadata Editor, primero en la lista | No |
| **Color de clip** | A-roll `Orange`, B-roll `Teal` (antes todo era `Apricot`) | No |
| **Bins** | subcarpetas `A-ROLL` / `B-ROLL` | **SÍ — opt-in** |

### Los bins son la única operación destructiva

La API de Resolve **sólo tiene `MoveClips`**: no hay alias ni copia, un clip
vive en un solo bin. Mandarlos a A-ROLL/B-ROLL los **saca de tu organización
manual**.

Por eso:

1. Es **opt-in**. En la Consola, antes del `dofile`:
   ```lua
   MOVER_A_BINS = true
   ```
2. **Antes** de mover, se graba el bin de origen de cada clip en
   `<disco>/.cinema_assistant/resolve/<proj>_bins_origen.lua`.
3. Si ese registro **no se puede escribir, no se mueve nada**. Sin vuelta atrás
   no vale el riesgo. Hay prueba que lo fija
   (`test_no_mueve_nada_si_no_puede_escribir_el_registro`).
4. Para deshacer, en la Consola y en **una sola línea**:
   ```lua
   dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/restaurar_bins.lua").correr({resolve = resolve, lib = os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_lib.lua", registro = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proj>_bins_origen.lua"})
   ```
   Desde el 2026-10-05 `restaurar_bins.lua` es un **módulo**: cargarlo no hace
   nada y `correr(ctx)` hace el trabajo. La ruta de la librería y la del
   registro van en `ctx` porque en el menú de Resolve 21.1 Free no hay `debug`
   para que el script se ubique solo, ni globals de Consola que leer. El
   asistente imprime esta línea, ya armada, al mover los clips.
   Restaurar tiene que ser **Lua**, no Python: mover clips entre bins sólo se
   puede con la API de Resolve.

Si un bin de origen ya no existe, ese clip **se deja donde está** y se reporta.
Crear bins a ciegas para "restaurar" sería inventar una organización que el
editor no pidió.

## Las dos timelines son modelos distintos (2026-08-13)

No se arman igual, y confundirlas es lo que producía el desfase acumulativo:

| | **A-ROLL** | **B-ROLL** |
|---|---|---|
| Modelo | Selección de tomas | Cronología del día |
| Colocación | EMPACADA, clip tras clip | Por TIEMPO REAL, cada cosa en su hora |
| Huecos | No hay | **Se conservan a propósito** |
| Audio externo | Pista aparte por tramo | El WAV **íntegro** es la espina dorsal |

### Por qué B-ROLL tuvo que cambiar

Se armaba con `AppendToTimeline` clip tras clip: quedaba empacada, espalda con
espalda, **sin los huecos reales del día**. Un lavalier que grabó tres horas
seguidas no puede cuadrar contra eso — se desfasa exactamente por la SUMA de los
huecos eliminados, y el error crece hacia el final de la timeline.

Eso es el "desfase acumulativo" que reportó el editor, y **no se arregla
midiendo mejor el sync**: es estructural. El motor ya lo sabía desde otro
ángulo — el bloque CLAMP de `placeCompanions` existe porque "las timelines van
EMPACADAS, sin los huecos reales, así que fuera de la ventana del clip base la
compañera queda DESALINEADA".

El hueco **no es un defecto**: el vídeo no rueda todo el tiempo y el lavalier
sí, y la timeline tiene que enseñar esa forma. (Instrucción del usuario,
2026-08-13: *"no tengo problema que haya fragmentos vacíos… es natural, porque
no tiramos video todo el tiempo, pero audio sí"*.)

### Cómo queda el lavalier

Entra **entero**, sin perder un segundo, en la pista de su cadena y en su hora,
pero **cortado en los bordes de los clips de la cámara base** — la del lavalier.
Los pedazos van consecutivos, así que la cobertura de audio es continua: el
corte es editorial, no un hueco.

Los cortes los marca **solo la cámara base**. Con tres cámaras rodando a la vez,
los bordes de todas darían un picado inútil. No hay API de razor en Resolve: se
consigue apilando appends con `startFrame`/`endFrame`, el mismo mecanismo de
`LIB.appendConCortes`.

De dónde sale la posición de cada clip, en este orden:

1. **Par de sync medido** — `pos = pos_del_WAV + (−offset)`. Es física.
2. **Reloj de su cámara** corregido por skew. Es estimación.
3. **Nada** → el clip NO se coloca, y se dice cuántos. Inventarle una hora
   llena la timeline de material desalineado que parece bueno.

La garantía la comprueba el propio script al cerrar: la suma de los pedazos de
cada WAV tiene que dar su duración, sin solapes. Si no cuadra, avisa en vez de
dar la timeline por buena.

### Cuándo NO se aplica

Se activa sola cuando hay lavalier del que colgar **y** cámara base declarada
(`camara_lavalier` en `project_config.json`). Sin cualquiera de las dos se
empaca como siempre: una cronología sostenida solo por relojes de cámara daría
una falsa sensación de precisión, que es justo lo que se está quitando. Es el
caso de un rodaje solo cámara tipo IMODAE.

Se puede apagar a mano con `CRONOLOGIA_BROLL = false` antes del `dofile`.

## Orden en el pipeline

`bin/run_pipeline.py` — después de categorías, sync y preguntas (de ahí salen
sus señales) y **antes** de `build_metadata_payload.py` (que escribe el keyword)
y del bake.

Suelto:

```bash
python3 ~/cinema-assistant/bin/derive_roll.py --root <disco> --project-prefix ""
```

`--dry-run` reporta sin escribir. `--kind documental|ficcion` fuerza el tipo.
