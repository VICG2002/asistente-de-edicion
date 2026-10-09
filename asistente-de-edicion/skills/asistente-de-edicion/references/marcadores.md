# Marcadores y metadata (v11)

## Filosofía

- **Metadata primero** — todos los datos descriptivos del clip van al
  panel **Metadata Editor** de Resolve (`⌘+1`). Editables, persistentes,
  filtrables por Keywords.
- **Markers solo para lo accionable** — el editor brinca a un marker;
  si el marker no le da info útil para decidir/cortar, es ruido.
- **Cada dato a su campo dedicado** — no duplicar shot/scene/keywords
  dentro de Comments. Comments es solo para notas técnicas (clipping,
  sombras, peak audio).

## Metadata aplicada al MediaPoolItem

El payload viene pre-armado por `bin/build_metadata_payload.py` desde
todas las tablas del manifest. El Lua solo lo lee y lo aplica.

| Campo Resolve  | Contenido                                                      |
|----------------|----------------------------------------------------------------|
| **Description**| Resumen de los tramos curados (o `SUJETO \| PLANO \| ACCIÓN`) |
| **Shot**       | Código de plano dominante: PM, PA, GPG, PP, PPP, PE, PG, PD    |
| **Scene**      | Categoría: entrevista, accion-dialogo, charla-equipo, b-roll…  |
| **Keywords**   | Filtrable: categoría, plano, **ángulo**, personajes, sector    |
| **Comments**   | SOLO notas técnicas (`clip_analysis.analysis_notes`)           |

Filtrar por keyword "ESCALADOR_A" o "PM" o "Cenital" trae todos los clips
instantáneamente desde el Metadata Editor.

## Markers

**Todos son marcadores DE PUNTO.** Instrucción del usuario (FCC 2026-07-11,
literal): *"los duration markers nunca han funcionado como deberían"*. La
duración del tramo se escribe en la **nota**, que sí se lee bien en el panel.

| Color    | Significa                                  | Fuente                        | Requiere transcript verificable? |
|----------|--------------------------------------------|-------------------------------|---|
| **Purple** | **Pregunta** de entrevista                | `interview_beats` kind=pregunta | **SÍ** |
| **Blue**   | **Respuesta** — donde empieza a responder | `interview_beats` kind=respuesta | **SÍ** |
| **Sand**   | Pausa real dentro de una respuesta        | `interview_beats` kind=pausa    | **SÍ** |
| **Mint**   | Palabra clave del proyecto                | `interview_beats` kind=keyword  | **SÍ** |
| **Lemon**  | Candidato a momento emocional             | `interview_beats` kind=emocion  | **SÍ** |
| **Green**  | Tramo curado con descripción real         | `clip_curated_segments`       | **SÍ** |
| **Cyan**   | Bitácora del sync de audio externo        | `audio_sync_pairs`            | No |
| **Rojo**   | Posible descarte                          | `clip_analysis.status='cull'` | No |
| **Amarillo** | Revisar manualmente                     | `clip_analysis.status='review'` | No |

### Pieza con guion — comercial, cápsula, spot (v0.4.0, IMODAE 2026-08-07)

Aquí no hay preguntas: hay cápsulas y tomas repetidas del mismo parlamento. Los
colores son **nuevos a propósito**, para no pisar los de arriba — reeducar el
ojo del editor sale caro, y en un mismo proyecto podrían convivir.

Estos van en la **REGLA de la timeline**, no dentro del clip (petición del
usuario, 2026-08-07: *"me quiero enterar fácilmente dónde terminan los intentos
de cada reel"*). Un marcador de item vive dentro del clip y hay que pasar por
encima para verlo; uno de timeline se lee recorriendo la regla, que es como se
busca un bloque.

| Color    | Dónde | Significa                                        | Fuente                  |
|----------|-------|--------------------------------------------------|-------------------------|
| **Sky**      | regla | `REEL n — <título>`: **empieza** el bloque; la nota lleva nº de tomas y minutos | `clip_reels` |
| **Cocoa**    | regla | `FIN REEL n`: **acaban los intentos** de ese bloque | `clip_reels` |
| **Lavender** | regla | `G96 T3/13 — completa`: empieza una toma        | `clip_takes`            |
| **Rose**     | regla | La toma trae un fallo cantado; la nota es la cita literal | `clip_takes.marcador` |
| **Sand**     | clip  | Aquí se quitó un silencio (opt-in `MARCAR_CORTES`) | `clip_keep_ranges`    |

Responden a las cuatro preguntas del editor al recorrer la timeline: ¿de qué
cápsula es esto?, ¿qué toma es?, ¿ésta se cayó?, **¿dónde acaba este bloque?**

El de cierre va en el **último frame del último clip del bloque**, no en el
primero del siguiente: así sigue estando dentro del material que cierra, y no se
mueve si después se reordena lo de al lado. Sin él habría que deducir el final
mirando dónde empieza el siguiente bloque — y en el último no hay siguiente.

**Dos trampas de la API**, las dos silenciosas:

- `TimelineItem:GetStart()` es **absoluto** (incluye el arranque de la timeline,
  01:00:00:00 en un proyecto normal) y `Timeline:AddMarker()` los quiere
  **relativos**. Sin restar `GetStartFrame()`, todos los marcadores caen una
  hora más allá del material y no se ve ninguno.
- Dos marcadores en el mismo frame: Resolve rechaza el segundo y devuelve
  `false`. Se usa `LIB.nuevoAsignador` también en la regla, no sólo en los clips.

Sand se reutiliza a propósito: en entrevista significa "aquí hay una pausa" y
aquí significa lo mismo — la diferencia es que en un comercial esa pausa se
quita en vez de marcarse. Detalle en
[`reels-y-cortes.md`](reels-y-cortes.md).

### Separación pregunta / respuesta (v0.2.0)

Antes un solo marker Purple cubría **pregunta y respuesta**: iba del inicio de
una pregunta al inicio de la siguiente, con la respuesta comprimida en la nota.
El editor no podía saltar a donde empieza a responder — que es donde corta.

`bin/derive_interview_beats.py` separa las dos, con tres métodos en orden de
fuerza. **El método usado se escribe en la nota del marker**, para que el editor
sepa cuánto confiar en ese frame:

| Método | Cómo | Confianza | Cuándo aplica |
|---|---|---|---|
| `master_src` | cambio de fuente en `master_*.json` (`words = [[palabra, t, fuente]]`) | 0.90 | hay dos TX: entrevistador y entrevistado con lavalier propio. Es **físico**, no heurístico |
| `silencio` | fin del primer silencio real tras la pregunta (`clip_silences`) | 0.60 | hay `detect_pauses.py` corrido |
| `estimado` | duración de la pregunta por su número de palabras (2.8 pal/s) | 0.30 | último recurso; la nota dice "inicio APROXIMADO" |

Si salen muchas `estimado`, faltan pasos: correr `build_master_transcripts.py`
y `detect_pauses.py` antes de derivar los beats.

### Pausas: sólo dentro de una respuesta

Una pausa mientras se **formula** la pregunta no es punto de corte, es la
entrevistadora pensando. `beats_de_clip()` sólo emite `pausa` si el silencio cae
completo dentro del tramo de respuesta. Hay prueba que lo fija
(`test_pausa_dentro_de_la_pregunta_NO_cuenta`).

### Momentos emocionales = candidatos, nunca afirmaciones

Salen de señales que ya teníamos, sin dependencias nuevas y sin LLM:
risa / aplauso / llanto (`sound_events`, PANNs) y pausa larga (≥3.5 s) dentro
de una respuesta. El marker **dice "candidato"** y lleva su confianza. El
editor confirma viendo el clip.

### Colisión de frames

Resolve **rechaza** un segundo marker en el mismo frame del mismo item:
`AddMarker` devuelve `false` y el marker se pierde **en silencio**. Con
pregunta + respuesta + pausas + keywords las colisiones son seguras.
`LIB.nuevoAsignador()` (en `resolve/asistente_lib.lua`) lleva la cuenta de los
frames usados y corre el marker al siguiente libre. `mock_resolve_full.lua`
reproduce esa regla y **falla la suite** si algún marker se pierde.

### `customData` — re-hornear sin destruir

Cada marker del asistente lleva `customData = "beat:<clip>:<i>"`. Con eso,
`DeleteMarkerByCustomData` quita **sólo lo que puso el asistente** y respeta
los markers que el editor agregó a mano. Antes había que borrar la timeline
entera y reconstruirla.

### Un solo lugar

La lógica de markers vive en `resolve/asistente_lib.lua`, que cargan los
`asistente_<proyecto>.lua`. Antes cada proyecto tenía su copia de `decorate()`
y fueron divergiendo: FCC quitó los duration markers y MAB los conservó, así que
"arreglar los markers" significaba editar seis archivos.

### Regla más estricta del usuario (iter9.6 2026-05-27)

**Instrucción literal del usuario** (cierre iter9.6):
> *"De momento la instrucción, únicamente dejar los que se pueden validar
> directamente con el transcript, y de momento eso son las entrevistas
> o escenas donde hablan los personajes."*

**Operacionalización**:
- Green Tramo Y Purple Q: SOLO en clips donde el transcript tiene
  contenido validable (entrevistas, charlas-equipo, discursos, escenas
  con diálogo de personajes).
- Excluir: B-roll sin habla, paisajes, timelapses, GoPro POV silencioso,
  cualquier clip cuyo transcript sea vacío o alucinado.
- Concretamente, exportar Green/Purple solo si:
  - `category IN ('entrevista', 'entrevista-audio', 'entrevista-solo-video',
    'dialogo-audio', 'charla-equipo', 'discurso', 'accion-dialogo')` Y
  - `transcript_quality.is_hallucinated = 0` (o no auditado pero con
    `clean_words_count >= 30`)
  - ⚠ `entrevista-solo-video` (entrevista sin lavalier, solo audio de
    cámara A1) DEBE estar en esa lista. Olvidarla dejó a 2573 (ENTREVISTADO_5
    organizador) sin un solo marker — ver `derive_question_segments.py`
    WHERE clause y errores-comunes §49.

**Razón**: el editor confía en los markers para saltar a momentos
relevantes. Un marker Green en B-roll sin habla con descripción
formulaica LLM ("Sin personas en cuadro. Plano abierto.") no aporta
valor — es ruido. El editor ya VE el clip al pasar; el marker debe
agregar información NO obvia.

### Regla original: markers que dependen del transcript deben verificar `is_hallucinated`

**Filtro aplicado en `bin/export_lua_data.py`** (Zezzions iter8 2026-05-27):
- Si `transcript_quality.is_hallucinated=1` para un clip, NO se exportan
  sus `clip_curated_segments` ni `question_segments` al Lua.
- Otros markers (Red, Cyan, Yellow PANNs) sí siguen porque no usan el
  transcript del video.

**Por qué**: Whisper sobre música ambient produce texto que parece denso
("Suscríbete al canal" ×100). Si el LLM o curaduría manual escribió
`full_text` basado en esa basura, el marker engaña al editor.

**Caso fundador**: Zezzions tenía 56 curated_segments en clips alucinados
(20% del total). Antes del filtro, el editor veía markers con
descripciones inventadas. Después del filtro: 226 markers — todos
corroborables con el contenido real del clip.

### Contenido del marker Green (tramos curados)

Label: `T<n> [<plano>/<ángulo>]` — ej: `T1 [PM/Normal]`, `T2 [PA/POV]`.

Note (multilínea):
```
[Personajes] | [Plano + Ángulo] | [Acción + lugar + objetos + idea diálogo]
Personajes: ESCALADOR_A, ESCALADORA_B
[total_dur_note si es muestra de clip largo]
(<start>s - <end>s)
```

### Contenido del marker Purple (preguntas)

Label: `Q<n>: <texto pregunta truncado>`.

Note:
```
¿<pregunta literal>?

Respuesta: <primeras 1-2 oraciones>

(<start>s - <end>s)
```

Funcionan tanto en clips de entrevista video como en los audios externos
clasificados como `entrevista-audio` (~180 audios en JILOTEPEC, 2155
preguntas detectadas).

### Detección de preguntas SIN signos `¿?` (iter10 2026-05-28)

**Problema fundador (Zezzions 2573):** una entrevista solo-video (ENTREVISTADO_5,
sin lavalier) tenía un transcript A1 limpio pero **0 Purple Q**. Dos causas:

1. El detector base solo extraía texto entre `¿` y `?`. Whisper **omite el
   `¿` de apertura** en muchas preguntas del entrevistador captadas en el
   audio de cámara. Resultado: 0 preguntas.
2. La categoría `entrevista-solo-video` no estaba en el WHERE (§49).

**Solución — el acento es el ancla.** En español el interrogativo lleva
TILDE (`cómo`, `qué`, `cuándo`, `dónde`, `quién`, `cuál`, `cuánto`) y el
relativo NO (`como te decía`, `que quiero resaltar`). Whisper respeta esa
distinción aunque borre los signos. `lib/question_filter.py` ahora detecta,
además de `¿...?`, **oraciones cuyo interrogativo acentuado va al frente**.

**Tres guardas de precisión** (sin ellas el detector mete monólogo del
entrevistado como falsas preguntas):

| Guarda | Qué corrige |
|--------|-------------|
| **Solo interrogativo ACENTUADO** en las primeras ~6 palabras | "Como dice, pues todos somos…" (relativo sin tilde) ya NO es pregunta |
| **`has_verb` por palabra completa**, no substring | "es" dentro de "este"/"eventos" ya NO cuenta como verbo |
| **Tope ~22 palabras** para preguntas loose | "cómo va cambiando, cómo se abren muros, cómo el piso…" (monólogo run-on) ya NO es pregunta |

Las `¿...?` explícitas NO llevan tope de longitud ni guarda de acento (el
signo ya confirma que es pregunta).

**Principio rector — precisión > recall en markers.** Es mucho peor un
marker basura ("Como dice pues todos somos…") que una pregunta real
perdida. El editor confía en cada Purple. Ante la duda, NO emitir.

**Verbos de entrevista:** lista curada en `INTERVIEW_VERBS`. Cuando un
proyecto pierda una pregunta real por un verbo no listado (p.ej. 2596
perdió "cómo te **encargas** de promover los eventos"), **agregar el verbo**
a la lista — no relajar el filtro entero.

**Validación obligatoria al tocar el filtro:** re-correr
`derive_question_segments.py` y comparar conteos por clip ANTES/DESPUÉS.
Cada delta negativo se inspecciona manualmente: confirmar que lo eliminado
era ruido (filler/monólogo/duplicado) y NO una pregunta real. En Zezzions el
total bajó 94→80 markers eliminando ruido, sin perder ninguna pregunta real
(las reales de los lavaliers ya están cubiertas por el master del video).

## Estándar de planos (español obligatorio)

| Código | Nombre completo                | Multiplicador para marker |
|--------|--------------------------------|---------------------------|
| **GPG**| Gran Plano General             | 0.40                      |
| **PG** | Plano General                  | 0.50                      |
| **PE** | Plano Entero                   | 0.70                      |
| **PA** | Plano Americano (3/4)          | 0.75                      |
| **PM** | Plano Medio                    | 0.85                      |
| **PP** | Primer Plano                   | 0.90                      |
| **PPP**| Primerísimo Primer Plano       | 1.00                      |
| **PD** | Plano Detalle                  | 1.00                      |

## Estándar de ángulos (nuevo en v11)

| Código        | Posición de la cámara                          |
|---------------|-----------------------------------------------|
| **Normal**    | Paralelo al suelo, altura de los ojos          |
| **Picado**    | Desde arriba apuntando hacia abajo             |
| **Contrapicado** | Desde abajo apuntando hacia arriba          |
| **Cenital**   | 90° vertical desde arriba (drone)              |
| **Nadir**     | 90° vertical desde abajo apuntando al cielo    |
| **Holandés**  | Inclinado 5-45° lateralmente                   |
| **Escorzo**   | Over-the-shoulder a 45°                        |
| **POV**       | Subjetiva — sustituye los ojos del personaje   |

Heurística automática (`bin/derive_camera_angle.py`):

- drone (DJI_*) → **Cenital**
- gopro → **POV**
- resto → **Normal**

Los ángulos especiales (Picado, Contrapicado, Holandés, Escorzo, Nadir)
se asignan **manualmente** vía JSON override durante la sesión de
descripciones por tramo. La heurística sola es insuficiente.

## Pipeline completo (v11)

```bash
# 1. Análisis técnico (motion/quality/audio)
bin/analyze_segments.py --root <disco>

# 2. Transcripts → content_segments + question_segments + characters
bin/derive_content_segments.py --root <disco>
bin/derive_question_segments.py --root <disco>   # ahora soporta audios
bin/derive_characters.py --root <disco>          # legacy, fallback

# 3. Face recognition (sustituye derive_characters)
bin/detect_faces.py --root <disco>
bin/build_face_catalog.py --root <disco>         # mosaicos para identificar
bin/build_face_catalog.py --root <disco> --assign 0=ESCALADOR_A 1=ESCALADORA_B …
bin/identify_faces.py --root <disco>             # poblar clip_characters

# 4. Heurísticas básicas
bin/derive_shot_value.py --root <disco> --overrides jilo_shot_overrides.json
bin/derive_camera_angle.py --root <disco> --overrides jilo_angle_overrides.json

# 5. Curaduría automática (candidatos)
bin/curate_segments.py --root <disco>

# 6. Sesión interactiva (Claude rellena las 8 preguntas por tramo)
# Output: /tmp/<proyecto>_curated/*.json
bin/bake_curated.py --root <disco> --dir /tmp/<proyecto>_curated/

# 7. Payload de metadata
bin/build_metadata_payload.py --root <disco>

# 8. Bake a Lua
bin/export_lua_data.py --root <disco> --out resolve/escalando_data.lua

# 9. En Resolve (Lua Console):
# dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_jilotepec.lua")
```

## Trampa: AddMarker rechaza markers en el mismo frame

Resolve no acepta dos markers en el mismo frame. El script Lua desplaza:
descarte en frame 0, revisar en 1, tramos curados/preguntas empiezan en
≥ 3 (con offset por índice si coinciden). Si dos segmentos coinciden en
frame inicial, uno se pierde silenciosamente — mejorar agrupando antes
del bake si pasa con frecuencia.
