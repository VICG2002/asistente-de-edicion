# Patrones de refactorización exitosos

Cosas que se probaron, funcionaron, y se quedaron en el flujo. Usar como
default en proyectos nuevos.

## Arquitectura

- **Dos capas** — motor reusable (`~/cinema-assistant/`) + datos por proyecto
  (`<disco>/.cinema_assistant/`). Permite mover proyectos entre discos sin
  tocar el motor; el motor evoluciona sin afectar datos viejos.
- **Manifest SQLite como ground truth**. Todo lo que el motor produce sale
  de queries al manifest. Cero fuentes intermedias divergentes.

## Datos / Indexación

- **Indexar por ruta absoluta**, no por nombre. La ruta es única en el
  manifest; los nombres se repiten en formatos consumer.
- **`byname` como fallback** (nombre → ruta) para robustez ante mismatch.
- **Cache de transcripts por `clip_id`** en JSON. Re-corridas instantáneas
  en lo cacheado; nuevas pasadas solo procesan lo nuevo.
- **Sidecar XML de Sony para `camera_model`** (Más Allá del Balón
  2026-06-11): los MP4 XAVC de Sony NO exponen `model` en los format tags
  de ffprobe — `camera_model` quedaba NULL y los clips con prefijo de
  filename no contemplado en `MAIN_CAM` (ej. `!2026...` de una segunda
  FX30) quedaban fuera de transcripción/sync. Fix genérico en
  `lib/probe.py::enrich_from_sony_sidecar()`: si existe `<clip>M01.XML`
  junto al MP4, leer `<Device manufacturer=.. modelName=..>`. Funciona
  para cualquier cuerpo Sony (FX30, FX3, a7S...) sin hardcodes por
  proyecto. Bonus del sidecar: `captureFps` vs `formatFps` distingue
  S&Q (audio inválido) de HFR normal 120p (audio scratch válido).

### Garantías verificables del pipeline (no promesas)

Aprendido la 2da vez que perdí material editorial sin darme cuenta
(Zezzions 2026-05-26, ENTREVISTADO_1 + ENTREVISTADO_2). El usuario reiteró:
**"Cómo vas a garantizar que algo así no vuelva a pasar?"** La respuesta
correcta es construir **scripts que fallan si el proyecto está mal**, no
prometer "voy a tener más cuidado".

Verifiers existentes en `~/cinema-assistant/bin/`:

- `verify_coverage.py` — cobertura por sector × tipo de procesamiento.
- `verify_interviews.py` — entrevistas potenciales no detectadas (caso
  Zezzions). Patrón: duración ≥60s + audio + ≥2 patrones de pregunta sin
  categoría 'entrevista' ni sync_pair.
- `lint_pipeline.py` — hardcodes geográficos y DELETE globales peligrosos.

Política: los 3 deben pasar antes de declarar un proyecto terminado.
`run_full_pipeline.sh` debería invocarlos todos al final (pendiente
integrar `verify_interviews` ahí).

### Pendientes futuros del motor de sync

- **Event-sync** (cross-correlate ventanas pequeñas 5s) — útil para clips
  donde música domina pero hay momentos puntuales reconocibles (silbatos,
  golpes, riffs). Por ahora chromaprint cubre casos similares.
- **Stand-alone audio**: cuando un personaje fue entrevistado SOLO en
  lavalier sin cámara (caso ENTREVISTADO_1 en Zezzions 2026-05-26), marcar el
  audio con `clip_characters.context='stand-alone-audio'`. El Lua debe
  destacarlo en `ZEZ — AUDIOS EXTERNOS` con su contenido editorial intacto.

### Arsenal de métodos de sync (orden de precisión, Zezzions 2026-05-26)

El motor tiene 5 métodos de sync. Aplicar en cascada — cada método cubre
casos donde el anterior falla. Política recomendada al procesar un
proyecto nuevo:

1. **`sync_transcript`** (`bin/sync_transcript.py`) — n-gramas largos del
   transcript completo. ALTA precisión pero requiere ambos transcripts
   limpios y densos.
2. **`sync_by_questions`** (`bin/sync_by_questions.py`) — preguntas de
   entrevista detectadas (`question_segments`) como anchors. Funciona
   cuando el A1 de cámara tiene preguntas audibles pero el resto está
   tapado por música.
3. **`sync_by_phrases`** (`bin/sync_by_phrases.py`) — auto-IDs ("Yo soy
   X") + nombres distintivos cruzados entre transcript de video y audio.
   Funciona cuando el LAVALIER dice el nombre del entrevistado pero la
   cámara ya no captura ese momento.
4. **`sync_acoustic`** (`bin/sync_acoustic.py`) — envelope FFT de
   loudness. Música/ambiente compartido. Conf típicamente baja (0.2-0.6).
5. **`sync_by_chromaprint`** (`bin/sync_by_chromaprint.py`) — chromaprint
   acoustic fingerprint. Más robusto a SNR/EQ que envelope. Requiere
   `brew install chromaprint`.

Para casos marginales: **`sync_review`** (`bin/sync_review.py`) — CLI
interactivo que lista pares con conf < threshold con comando `ffplay`
para validación manual.

- **Sync por contenido (transcript-based)** > sync por reloj (TC) > sync
  por loudness (waveform). El por transcript se verifica con las palabras
  exactas dichas — no puede emparejar mal por accidente.
- **Pero el sync por transcript falla cuando la música tapa el diálogo**:
  Whisper no extrae palabras-ancla del A1 cuando hay música alta encima.
  Para esos casos hay un **sync acústico complementario** (`bin/sync_acoustic.py`)
  que cross-correlaciona el ENVELOPE de loudness de música/ambiente entre
  el A1 y el lavalier — la música es la misma señal con SNR distinto.
  Caso fundador Zezzions 2026-05-26: pasó de 5 videos sincronizados a 23
  cuando el usuario sugirió justamente esto. Política: correr sync_transcript
  primero (alta precisión), después sync_acoustic con `--only-missing` sobre
  los videos sin par. El sync_acoustic guarda `method='waveform-envelope'`
  en `audio_sync_pairs.method` para que el editor distinga la fuente.
- **Filtro por densidad léxica** (`distinct_words >= 60`) descarta clips
  sin habla y alucinaciones.
- **Filtro por duración** (`audio >= 60 s`) descarta fragmentos que generan
  falsos positivos en waveform.
- **Sanity check pre-sync**: si dos carpetas (Dr/Izq, TX1/TX2, A/B) tienen
  archivos con SHA idéntico, NO sincronizar — la info del 2do canal no
  llegó al disco. Avisar al usuario para que recupere del grabador antes
  de continuar. Caso Zezzions en `errores-comunes-a-corregir.md §17`.

## Dual-lavalier sync (entrevistas con 2 interlocutores)

Cuando el Rode Wireless PRO (u otro recorder dual-mic) capturó dos personas
en un mismo video, la salida esperada en Resolve es:

- A1 = audio de cámara intocable (scratch).
- A2 = TX1 (lapel persona 1) sincronizado con offset propio.
- A3 = TX2 (lapel persona 2) sincronizado con offset propio.

Esto requiere que en disco existan **dos archivos físicamente distintos
por sesión** (uno por TX). Estructura recomendada:

```
Lavas/TX1/00001.WAV
Lavas/TX2/00001.WAV
```

Pipeline:
- `index_audios` recorre las dos subcarpetas.
- `sync_transcript --audio-like '%tx1%'` produce pares (video, TX1).
- `sync_transcript --audio-like '%tx2%'` produce pares (video, TX2).
- `mirror_questions_to_audio` espeja preguntas a ambos.
- El Lua coloca TX1 en track 2 y TX2 en track 3 si ambos sincronizan al
  mismo video.

**Pendiente del motor**: `placeSyncAudio` en el Lua actualmente asume un
solo audio externo por video (track 2). Cuando aparezca el primer proyecto
con dos archivos reales por sesión, extender para colocar el 2do en track 3.

## Visual a escala

- **Hojas de contacto con Pillow**: 1 cuadro por clip, mosaicos 5×5
  etiquetados, índice JSON. Permite analizar 200+ clips en ~12 lecturas
  visuales en vez de 200 lecturas individuales.

## Resolve

- **Lua Console** como única integración con Resolve Free. Pegar
  `dofile(...)` en el input strip al fondo de la ventana de la Consola.
- **Bin `Timelines/asistente de edicion`** para aislar el trabajo del
  asistente sin invadir el Media Pool del usuario.
- **Sweep de timelines `ESC — *`** al inicio de cada corrida — el script
  reconstruye los del asistente; los del usuario no se tocan.
- **Indexar `clips` y `sync` por ruta** en el bake Lua (no por nombre).
  `byname` como fallback. Resuelve colisiones GoPro/DSLR.

## Workflow

- **Background jobs** para tareas largas (transcripción, indexación, análisis
  de segmentos). Notificación al terminar — no pollear.
- **Reportes honestos**: mostrar `n=7 verificados` en vez de `n=45 dudosos`.
  La calidad gana al volumen.
- **Documentar convenciones explícitamente** (signo de offset, naming,
  colores de markers) — cualquier ambigüedad cuesta horas y bugs.
- **Heurística + transcript** para entrevistas: la heurística sirve cuando
  no hay transcript todavía; el transcript la corrige cuando aparece.

## Categorización de clips — más allá de "interview vs B-roll"

Descubierto en el piloto JILOTEPEC (hoja 2): la dicotomía "entrevista
con diálogo denso" vs "B-roll mudo" pierde matices reales del material.
Categorías adicionales que aparecen:

- **`accion-dialogo`** — escalada / acción con beta-calling del equipo
  ("¡Venga!", "¡Trapo no!", "Bájalo, ESCALADORA_H"). Transcript ~30-50
  palabras distintas (debajo del umbral de entrevista pero con
  contenido). Audio del crag importante para la doc.
- **`charla-equipo` / `making-of`** — conversaciones nocturnas en
  campamento (DP/operador hablando de filtros, lesiones, planeación).
  Transcript muy denso (100+ palabras distintas, hasta 1900 palabras
  totales). No es entrevista formal pero es material muy valioso para
  el making-of.
- **`b-roll-gear`** — toma del grupo descansando con gear, alguien
  comentando el topo de la ruta. Transcript moderado pero referido a
  logística, no a contenido narrativo.

Categorías adicionales descubiertas en hojas 6-12:

- **`timelapse`** — duración ≥ 5 min, motion ≤ 2, trípode estático. Bloques
  de 3-5 clips sucesivos del mismo ángulo. Establishing/paisaje/cambio de
  luz. Patrón visual: pocas paletas de color, cambios graduales.
- **`pov-static`** — GoPro (`GOPR*.MP4`) larga (≥ 5 min) con motion bajo
  (3-10). Cámara fija en casco o trípode subjetivo. Sin transcripts útiles.
- **`export`** — masters / cortes finales del proyecto. Filename indica
  ('MASTER *', 'Corte final *', 'CC *', 'CORTE *'). Duración ~9 min en este
  caso. NO son fuentes — son entregables previos para referencia.
- **`discard`** — clips de prueba ('prueba *', 'FOCUS.mov', clips < 5 s).
  No usar en edición.

Categoría sugerida por clip (refinada):

| Señal                                  | Probable categoría             |
|----------------------------------------|--------------------------------|
| Filename contiene MASTER/Corte/CC      | `export`                       |
| Filename contiene 'prueba' o dur < 5s  | `discard`                      |
| Dur ≥ 5min + motion ≤ 2 + main         | `timelapse`                    |
| GoPro + dur ≥ 5min + motion ≤ 10       | `pov-static`                   |
| Distinct ≥ 100 + main                  | `entrevista` o `charla-equipo` |
| Distinct 30–100                        | `accion-dialogo`               |
| Distinct < 30 + larga + main           | `b-roll` o `accion`            |
| Distinct < 30 + corta                  | `b-roll` o `discard`           |

**Distribución empírica JILOTEPEC** (281 clips, hojas 1-12):

| Categoría        | n   | %    |
|------------------|-----|------|
| b-roll           | 169 | 60 % |
| accion-dialogo   |  35 | 12 % |
| timelapse        |  15 |  5 % |
| charla-equipo    |  13 |  5 % |
| discard          |  13 |  5 % |
| entrevista       |  11 |  4 % |
| pov-static       |  10 |  4 % |
| export           |   8 |  3 % |
| otros            |   7 |  2 % |

El editor decide; el asistente no descarta nada por categoría — solo
la usa para el marcador y filtros de búsqueda.

## Hitos narrativos (auto-detección)

Marcar como `★ HITO` clips que cumplen 2+ de:

- Distinct ≥ 200 (entrevista densa)
- Filename del bloque de la segunda sesión de un día clave
- Transcript menciona palabras del título del proyecto / vías clave
  (ej. en JILOTEPEC: "lujuria", "encadenar", "encadene", "primer",
  "14a", "5-15", "ESCALADOR_A")
- Cluster temporal: 3+ clips dentro de ≤ 30 min con distinct ≥ 80

En JILOTEPEC: clips 329-331 (entrevista post-encadene ESCALADOR_A, ~19 min,
1000+ palabras totales) y 380, 382 (entrevista al pie de Lujuria).

## Marcadores enriquecidos (Frame 0 + Q/R + shot value)

Descubierto en el feedback post-piloto JILOTEPEC: el marker de
descripción solo con el texto del clip no es suficiente. El editor
necesita 3 cosas más para usarlo:

1. **Personajes** mencionados en la toma — detectados del transcript
   con whitelist por proyecto (`bin/derive_characters.py`). Cada
   nombre con conteo de menciones. Ej: `ESCALADOR_A (3), ESCALADORA_B (1)`.
2. **Contexto** — sector + horario inferido del folder + transcript.
   Ej: `Lujuria (14a) · encadene · atardecer`.
3. **Valor del plano** (ELS / LS / MLS / MS / MCU / CU / ECU / INS),
   asignado por heurística + overrides manuales (`bin/derive_shot_value.py`).
   No es solo metadata: **ajusta la duración del marker verde**
   (un LS ocupa solo 50 % del segmento porque rara vez lo usas más).

Para **entrevistas**, además: un marker Purple por pregunta detectada
en el transcript (`bin/derive_question_segments.py`), con la pregunta
literal + resumen de la respuesta en la nota. El editor brinca de
pregunta en pregunta sin leer todo el clip.

En JILOTEPEC: 47 preguntas, 9 entrevistas, ~6-9 preguntas reales por
clip de ESCALADOR_A (después de filtrar muletillas `¿no?` `¿vale?` etc.).

## Ruta futura: migración a DaVinci Resolve Studio (si algún día)

*Reescrita el 2026-09-11. La versión anterior era del 2026-05-24 y describía
un puente de terceros que Blackmagic volvió innecesario.*

Desde **Resolve 21.1**, Blackmagic trae **servidor MCP propio**, y es de
**Studio**. Ya no hace falta evaluar puentes de la comunidad: el fabricante
expone la integración él mismo, y nombra a Claude, Claude Code y ChatGPT Codex
como clientes.

**Cómo se conecta** (cuando haya Studio):

- `Preferences > System > General`, poner **External scripting** en `Local`.
- `File > Setup AI Assistants`, que es donde se cablea el cliente.
- Licencia: Studio, perpetua, 295 USD de una sola vez.

**Lo que 21.1 se llevó de Free en el mismo movimiento.** Blackmagic movió el
scripting de **Python** a Studio, y lo dice sin rodeos en sus notas de versión:

> The Python API was being used to hack studio features into the free version.
> DaVinci Resolve relies on studio license sales to pay for the engineering team.

Esto no es lo mismo que cerrar el scripting entero. El README de scripting que
la propia 21.1 instala en el disco sigue declarando que las APIs *"cover a
common superset of functions for both the Free and Studio versions"*, y que una
llamada de Studio desde Free devuelve `False` en vez de no existir. También
sigue listando la Consola y el menú de Scripts bajo **Internal Scripting**.

**Qué está medido y qué no.** Lo de arriba es lo que dicen Blackmagic y la
prensa. Lo que esta máquina hace de verdad lo contestan los spikes **S9, S10 y
S11** de `resolve/spikes_v2.lua`, que existen para eso. Hasta que corran, nada
de esto se da por bueno en un plan.

**Las 20 APIs nuevas de 21.1, y cuáles importan aquí.** Cuatro tocan justo lo
que el motor resuelve hoy por fuera, y por eso son las que deciden si Studio
vale la pena:

| API nueva | Qué haría en este pipeline |
|---|---|
| `MediaPoolItem.GetTranscription` | Transcripción con hablante y tiempo, que hoy hace Whisper por fuera |
| `Timeline.AutoAlignClips` | Alineación en timeline, al lado del sync multi-señal propio |
| `MediaPool.CreateMulticamClip` y `TimelineItem.FlattenMulticam` | Multicám por API, que hoy se arma clip por clip |
| `Timeline.NormalizeAudioLevel` | Normalización, hoy fuera del alcance del asistente |

Las otras dieciséis son presets de teclado y de proyecto, presets de render,
códecs y formatos de render de audio, SmartSwitch de multicám, transiciones,
fades y velocidad por item, blanking de salida, mapeo de audio de origen,
clonado de medios y validación de DCTL.

**Qué seguiría siendo nuestro aunque se compre Studio.** El MCP automatiza la
entrada y la salida hacia Resolve. No sabe nada de esto, y es lo que justifica
que el motor exista:

- Curaduría narrativa por 8 preguntas estandarizadas.
- Crónica cronológica como artefacto del proyecto.
- Face detection con InsightFace + clustering interactivo.
- Sync por transcript, frente al timecode que el MCP asume.
- Detección de preguntas en entrevistas con regex semántico.
- Esquema estandarizado `SUJETO | PLANO | ACCIÓN`.
- Espejado de markers de video a audios sincronizados.

**Si se migra**: el Lua de la Consola se reemplaza por llamadas MCP desde el
cliente, y `~/cinema-assistant/` sigue siendo la fuente de inteligencia
editorial. El motor no se tira: cambia de puerta de salida.

## Existencia de masters previos

Si en el catálogo aparecen archivos `MASTER *`, `Corte final *`, `CC *`,
`Master *.mp4/.mov`: el proyecto YA tuvo un corte previo. Esos clips:

- NO son fuentes, son entregables.
- Sirven como **referencia editorial** — qué se priorizó, qué tomas
  llegaron al corte, qué pacing se usó.
- Categoría `export`. Bin separado en Resolve si se importan.

## Filtros de calidad — degradación gradual, no binaria

Patrón validado en Zezzions (2026-05-26) después de que el filtro binario
descartó 8+ entrevistas reales. Cuando una señal de calidad (alucinaciones,
exposición, motion, etc.) puede ser **local** dentro de un clip/transcript:

1. Operar a nivel de **ventana o segmento**, no de archivo entero.
2. Devolver un **dict con rangos** afectados (índices, timestamps), no solo
   bool.
3. Marcar como inútil SOLO si la parte buena no tiene sustancia editorial.
4. Propagar el rango limpio a downstream para que filtre internamente
   (ej. `derive_question_segments` con `clean_until_sec`).

El usuario decide al editar. El asistente nunca debe descartar material
editorial real por agresividad de filtro.

## Proyectos planos (sin sectores)

Descubierto en Zezzions (2026-05-25): no todos los proyectos tienen estructura
multi-sector como ESCALANDO MEXICO. Zezzions es un bloque único en
`/Volumes/MI_DISCO/Diez50/Zezzions VICG/` con tres subfolders simples
(`Video 001 VICG`, `Video 002 VICG`, `Audio 001 VICG/Lavas/...`). Para
soportar este patrón sin romper ESC:

- Tratar la subcarpeta del proyecto como `$DISK` (no el volumen entero) —
  el manifest queda dentro del proyecto, no contamina el disco.
- Scripts del motor parametrizados con `--project-prefix` (default
  `"ESCALANDO MEXICO/"` mantiene compat; pasar `""` activa modo plano):
  `analyze_segments.py`, `export_lua_data.py`, `build_metadata_payload.py`,
  `transcribe_audios.py` (con `--audio-like`).
- `transcribe_clips.py` + `sync_transcript.py`: extender `MAIN_CAM` para
  reconocer Sony FX30 (`VICG` filename prefix, `camera_model='ILME-FX30'`).
- Lua separado por proyecto: `asistente_zezzions.lua` clona la lógica de
  `asistente_jilotepec.lua` pero quita el loop por ubicación, usa prefijo
  `ZEZ — ` y agrupa por subfolder de primer nivel si hay más de uno.

Pendiente: generalizar `run_full_pipeline.sh` para detectar
automáticamente si el proyecto es multi-sector (TSV con sectores) o plano
(sin TSV o TSV vacío) y aplicar los flags correctos.

## Garantías para pipelines multi-sector (lint + verify + orquestador)

Caso fundador ESCALANDO MEXICO (2026-05-25): pipeline de 12 scripts × 5
sectores, ejecutado a mano paso por paso. Resultado: solo JILOTEPEC quedó
completo; los otros 4 sectores quedaron sin Purple Q, sin tramos curados
con descripción, y sin sync de audio externo. El usuario percibió "no
hiciste lo que te pedí: que todo lo de JILOTEPEC se aplicara al resto".

**Las 3 garantías mínimas** que evitan que esto se repita:

1. **Linter pre-flight** (`bin/lint_pipeline.py`) — escanea todos los
   `bin/*.py` buscando:
   - Hardcodes geográficos literales (`'JILOTEPEC'`, `'GUADALAJARA'`, etc.)
     cerca de SQL `LIKE` o `rel_path` o `video-folder`.
   - `DELETE FROM` / `DROP TABLE` global sobre tablas multi-sector cuando
     el script acepta flag de scope (`--sector`, `--location`, etc.).
   - Override explícito permitido con comentario `# lint:ok delete-global`
     cuando el comportamiento es intencional.
   El orquestador lo corre antes de empezar y aborta con exit 3 si falla.

2. **Verificador post-flight** (`bin/verify_coverage.py`) — reporta una
   tabla **sector × tipo de procesamiento** con PASS/FAIL por celda:
   `clip_segments`, `clip_curated_segments`, `*_with_llm`,
   `question_segments_video`, `audio_sync_pairs`, `clip_metadata_payload`.
   Umbrales individuales por tipo (clip_segments ≥ 50%, curated ≥ 10%, …)
   o `--min-coverage 0.5` global. Filtra denominador a `file_kind='video'`
   para evitar el bug #24. Exit 1 si algún sector falla.

3. **Orquestador único** (`bin/run_full_pipeline.sh`) — ejecuta los pasos
   en orden con asserts entre etapas. Lee un mapping declarativo
   `bin/sector_video_audio_map.tsv` (un sector por línea) para que ningún
   sector se quede fuera por olvido. Banner explícito recordando que es el
   único entry-point soportado.

La **regla operativa** se escribe en 3 lugares (SKILL.md +
`pasos-a-seguir.md` + banner del orquestador): "Para procesar material del
proyecto se usa **únicamente** `run_full_pipeline.sh`. Si necesitas correr
un script individual, primero documenta por qué."

## Auto-clasificación conservadora por rel_path + filtro de alucinaciones

ESCALANDO MEXICO (2026-05-26): no había script para asignar
`category='entrevista'` automáticamente. JILOTEPEC tenía categorías
metidas vía `bake_descriptions.py` (`desc_*.json` ya borrados); los otros
4 sectores tenían `category` vacío. Sin esa categoría,
`derive_question_segments` no generaba Purple Q.

**Solución** (`bin/derive_video_categories.py`) — auto-asignación basada en
señal 1 del playbook (carpeta con "entrevista" en rel_path), conservadora:

```python
# Conservador: solo clips cuyo rel_path contiene 'entrevista' (case-insensitive)
# Y NO sobreescribir categoría existente
candidates = conn.execute("""
    SELECT c.id, c.rel_path, IFNULL(d.category,'') as existing_cat
    FROM clips c LEFT JOIN clip_descriptions d ON d.clip_id = c.id
    WHERE c.file_kind='video' AND c.index_status='ok'
      AND lower(c.rel_path) LIKE '%entrevista%'
""")
```

**Crítico**: aplicar el filtro de alucinaciones de
`lib.transcript_quality.is_hallucinated()` **antes** de asignar
`category='entrevista'`. Si Whisper alucinó (música alta sobre diálogo del
camera scratch), marcar como `'entrevista-degradada'` para que
`derive_question_segments` la excluya. Sin este filtro, 16 entrevistas con
transcripts inflados generaron 45 preguntas falsas Purple Q.

## Configuración por proyecto, motor genérico (pendiente refactor)

Doctrina añadida 2026-05-26. Todo lo aprendido en un proyecto debe quedar
en el motor + doctrina. Hardcodes a sectores/cast/colores específicos
encontrados durante Zezzions y que **necesitan ser parametrizables**:

| Script / archivo | Hardcode | Origen | Refactor pendiente |
|---|---|---|---|
| `bin/attribute_faces_via_transcript.py` | `CAST = {...}` con ESCALADOR_A, ESCALADORA_B, etc. | Jilotepec | leer `--cast-json` o `<disk>/.cinema_assistant/cast.json` |
| `bin/describe_segments_local_llm.py` | `CAST_ALIASES = {...}` igual | Jilotepec | igual |
| `bin/clean_character_identity.py` | mismo CAST | Jilotepec | igual |
| `bin/derive_characters.py` | whitelist Jilotepec | Jilotepec | igual |
| `bin/reformat_descriptions.py` | "ESCALADOR_A" como protagonista | Jilotepec | leer `interviewee` del config |
| `resolve/asistente_jilotepec.lua` | `LOC_COLOR{JILOTEPEC=Orange,...}` | Jilotepec | leer del data.lua |
| `bin/export_lua_data.py` | `INTERVIEW_MAX_MOTION=8.0` | Jilotepec | parametrizable |
| `bin/sync_audio.py` | path "AUDIOS/AUDIOS JILOTEPEC..." en docstring | Jilotepec | doc genérico |

**Esquema propuesto `<disk>/.cinema_assistant/project_config.json`:**

```json
{
  "project_prefix": "ESCALANDO MEXICO/" | "" (proyecto plano),
  "timeline_prefix": "ESC — " | "ZEZ — " | ...,
  "interviewee": "ESCALADOR_A" | "(múltiple)",
  "cast": {
    "ENTREVISTADO_8": ["entrevistado_8", "entrevistado_8"],
    "ENTREVISTADO_4": ["entrevistado_4"]
  },
  "loc_color": {"JILOTEPEC": "Orange", ...},
  "thresholds": {
    "interview_max_motion": 8.0,
    "interview_min_dur": 60.0,
    "sync_transcript_min_conf": 0.5,
    "sync_acoustic_min_conf": 0.30
  }
}
```

Plan de migración (incremental):
1. Cuando aparezca el próximo proyecto: crear `project_config.json` antes
   de empezar.
2. Cada vez que se toque un script con hardcode, parametrizarlo a leer del
   config.
3. Documentar los proyectos pasados en `cast.json` en su disco
   (`/Volumes/MI_DISCO*/.cinema_assistant/cast.json` para Jilotepec).

## Curaduría manual de Claude para tramos clave

Paso 11b del playbook (`metodologia/pasos-a-seguir.md`). Aprendido de
Jilotepec donde 10 tramos clave fueron descritos a mano por Claude con
formato `Personajes | Plano + Ángulo | Acción + Lugar. Idea: 'frase clave'`.
Las descripciones manuales son notablemente mejores que las del LLM local
para tramos narrativos. Aplicado a Zezzions 2026-05-26 en 5 tramos
(entrevistas de ENTREVISTADO_6, ENTREVISTADO_11, ENTREVISTADO_4, ENTREVISTADO_8 + ENTREVISTADO_7).

Formato canónico (extraído de Jilotepec):
```
{PERSONAJES_COMA} | {PLANO} + {ÁNGULO} | {ACCIÓN-RESUMEN}. {LUGAR-CONTEXTO}. Idea: '{FRASE CLAVE}'
```

## Convergencia multi-método como señal de alta confianza

Aprendido en Zezzions 2026-05-26 al sincronizar el clip 2645 (ENTREVISTADO_13 y
ENTREVISTADO_10 entrevista dual-lavalier). Las confianzas individuales de envelope
y chromaprint eran bajas:

| método | audio 149 (Dr) | audio 160 (Izq) |
|---|---|---|
| envelope | off=-19.4 conf=0.24 | off=-19.3 conf=0.19 |
| chromaprint | off=-19.9 conf=0.12 | off=-19.9 conf=0.09 |

Individualmente ninguna pasaba el umbral 0.30. Pero **las 4 medidas
convergen en offset ≈ -19.5s con desviación < 0.6s**. Esa convergencia
multi-método entre métodos independientes (envelope = energía RMS;
chromaprint = espectro perceptual) es señal de alta confianza incluso
si cada uno por separado sería rechazado.

Patrón generalizable: cuando dos métodos independientes coinciden en el
offset (±2s) con conf > 0.10 cada uno, **subir el score combinado a
~0.85** y aceptar el par. Aplicable también a transcript-based +
acoustic, o questions + phrases. Ver `bin/sync_review.py` para CLI que
hace este cross-check semi-automático.

## Auditoría exhaustiva final — instrucción del usuario (Zezzions)

Paso 12c del playbook: *"En cada proceso de asistencia de edición es
vital que revises a TODO el material para que los duration markers
tengan la información más exacta posible con relación a lo que pasa en
el video"* (usuario, 2026-05-26).

Operacionalmente: después de correr verifiers, hacer audit ad-hoc de
TODO clip ≥30s + audio + transcript no-alucinado que no esté ya
categorizado/sincronizado. Inspeccionar uno por uno. En Zezzions este
audit encontró 3 huecos que los verifiers automáticos no detectaron
(2645 ENTREVISTADO_13 y ENTREVISTADO_10, 2573 ENTREVISTADO_5 organizador, 2732 cita de Galeano). Los
verifiers son red de seguridad — la pasada humana es auditoría real.

## Fusión multi-señal con clase de método (Fase 1, 2026-05-26)

`lib/sync_fusion.py` reemplaza el umbral único de los 5 scripts antiguos
con una decisión por convergencia. Pero NO todos los métodos son iguales:

| Clase | Métodos | Política con 1 voto |
|---|---|---|
| **Fuertes / semánticos** | transcript, phrase-match, questions, lip-sync | Aceptar si conf ≥ 0.50 (el contenido literal es muy improbable de coincidir por accidente) |
| **Débiles / acústicos** | envelope, chromaprint, diarization | **Rechazar** salvo conf ≥ 0.95 (ambiente musical produce muchos falsos positivos genuinos) |

Lección de Zezzions: cuando el script original `sync_acoustic.py` daba pares
con conf=0.30-0.50 en envelope solo, generaba muchos falsos positivos (videos
de B-roll syncados con audios random porque "tienen energía RMS parecida").
La fusión exige que envelope conviva con al menos otro método para
aceptar — eso elimina toda esa clase de falsos positivos.

**Doctrina**: cualquier nuevo método de sync se clasifica como fuerte o
débil ANTES de integrar. Defecto: si imita el contenido literal (texto,
caras visibles, lip sync) es fuerte; si imita la huella física genérica
(espectro, RMS, fingerprint) es débil.

## Resemblyzer no discrimina voces en ambiente musical (limitación)

Aprendido en Zezzions 2026-05-26 al testear `lib/voice_embeddings.py`
(GE2E embeddings de Resemblyzer) contra 5 audios del evento:

| comparación | sim esperada | sim obtenida |
|---|---|---|
| 00007 Dr (ENTREVISTADO_5) ↔ 00011 Dr (ENTREVISTADO_1) | < 0.50 (voces distintas) | **0.887** (¡falso "misma persona"!) |
| 00007 Izq (ENTREVISTADO_13 y ENTREVISTADO_10) ↔ 00005 Dr (ENTREVISTADO_13 y ENTREVISTADO_10) | > 0.70 | 0.953 ✓ |
| 00007 Dr (ENTREVISTADO_5) ↔ 00008 Izq (ENTREVISTADO_2) | < 0.50 | **0.817** falso pos |

Hipótesis: el VAD interno de Resemblyzer (webrtcvad) deja entrar mucha
música ambient porque tiene componentes vocales (los DJs cantan encima).
El embedding GE2E aprende un "espacio acústico del evento" más que la
identidad de voz individual.

**Consecuencias**:
- Voice embeddings **NO sirven solos** en este contexto de evento musical.
- Para identidad cross-audio, necesitamos algo más robusto: pyannote.audio
  v4 (entrenado en datos más diversos) o filtros pre-VAD agresivos.
- Como CLASIFICADOR (¿esta voz es ENTREVISTADO_5? — sí/no), no funciona.
- Como SEÑAL DE CONVERGENCIA débil junto a transcript + lip-sync, podría
  todavía ayudar si conf > 0.90 (descartar candidatos claramente distintos).

**Pendiente Fase 1**: probar con pyannote-audio v4 que tiene VAD propio
más selectivo (`pyannote/voice-activity-detection`).

## Diarización local (webrtcvad + Resemblyzer por chunks) sí funciona

Aprendido en Zezzions 2026-05-26: aunque Resemblyzer puro (embedding del
audio entero) NO discrimina voces en ambiente musical, **embebedar por
chunks de 1.5s y clusterar** sí separa speakers correctamente:

| Audio | Esperado | Detectado | Resultado |
|---|---|---|---|
| 00007 Izq (ENTREVISTADO_13 y ENTREVISTADO_10 dialogan) | 2 speakers ~50/50 | 2 speakers (53/44%) | ✅ correcto |
| 00011 Dr (ENTREVISTADO_1 entrevistado solo) | 1 dominante + entrevistador | 3 speakers (69/28/2%) | ✅ correcto (dominante claro) |

Patrón: `lib/diarization_local.py`:
1. webrtcvad aggressiveness=3 (excluye música/ruido).
2. Ventana 1.5s con `preprocess_wav` de Resemblyzer (VAD interno + RMS).
3. Embedding GE2E por chunk.
4. AgglomerativeClustering(metric="cosine", linkage="average").
5. Auto-K vía silhouette score (2..max_speakers).
6. Mergear ventanas contiguas con mismo cluster.

**Por qué funciona donde Resemblyzer puro falla**:
- VAD agresivo elimina segmentos donde domina la música.
- Chunks cortos (1.5s) capturan voz individual, no "ambiente del evento".
- Clustering aglomerativo es robusto a outliers.

Aplicación inmediata:
- **Detectar dual-lavalier** automáticamente: si dos audios tienen
  `dominant_ratio > 0.85` en speakers DISTINTOS → dual-lavalier con dos
  interlocutores; si dominantes son MISMA voz (sim > 0.85 con embeddings
  de los chunks dominantes), un solo entrevistado con dos micros.
- **Clasificar audio**: `single-speaker` vs `interview-dual` vs
  `interview-multi` vs `ambient-or-mixed` — útil para categorizar
  automáticamente sin transcript.
- **Anchor de sync**: si un video tiene una persona dominante en cuadro
  (Fase 2 con face detection) y el audio tiene un speaker dominante,
  asociarlos.

## Identity fusion cara↔voz por sincronía (Fase 2, validado en Zezzions)

`lib/identity_fusion.py` vincula detecciones de cara (clip de video) con
speakers (audio sincronizado) usando el sync offset:

  audio_t = face.frame_t_sec + sync.offset_sec

Por cada cara, busca qué speaker estaba activo en ese momento del audio.
El link queda en `face_voice_links(face_detection_id, speaker_id)`.

**Resultado en Zezzions 2026-05-26**:

| Métrica | Valor |
|---|---|
| Detecciones de cara (InsightFace) | 1141 caras / 217 clips |
| Clusters DBSCAN | 53 (Persona_00..52) |
| Speakers diarizados | 49 |
| Segmentos granulares de habla | 1801 |
| Voice catalog clusters (sim ≥0.72) | 10 |
| **face_voice_links creados** | **165** |
| Propagaciones cara→voz automáticas | 4 (cuando cluster_cooccurrence ≥3) |

**Caso de validación: clip 2645 (ENTREVISTADO_13 y ENTREVISTADO_10, entrevista dual)**:
- 2 caras dominantes en cuadro: Persona_12 (10 detecciones) + Persona_20 (9 det)
- Ambos audios sync (00005 Dr + 00007 Izq) vinculados a esas mismas caras.
- El algoritmo correctamente identifica que es una entrevista de DOS personas.

### Pre-requisito CRÍTICO: tabla `audio_speaker_segments`

Bug aprendido: la primera versión de `audio_speakers` guardaba solo el
chunk más largo del speaker (start/end del longest segment). Por eso
`identity_fusion` encontraba solo 14 links (las caras debían caer dentro
de ese chunk ÚNICO). Fix: tabla auxiliar `audio_speaker_segments` con
fila por cada segmento → 165 links (11.7× más).

→ **Patrón generalizable**: cuando un objeto tiene "periodos de actividad",
  guardar GRANULARMENTE cada periodo, NO la envolvente ni el más largo.
  Resúmenes (envolvente, duración total) van en la tabla padre; consultas
  temporales puntuales necesitan la tabla granular.

### Auto-discovery del cast del proyecto

`attribute_faces_via_transcript.py` v2 elimina el `CAST` hardcoded
(Jilotepec era el único contemplado). Ahora descubre el cast del proyecto
en cascada:

1. **`<root>/.cinema_assistant/cast.json`** explícito → override total.
2. **`clip_characters.characters`** ya curado por Claude/usuario.
3. **Mining de transcripts** con patrones de auto-ID (`Yo soy X`,
   `Mi nombre es X`, `Soy hermano de X`, etc.).
4. **Fallback Jilotepec** si todo lo anterior está vacío.

Validación en Zezzions (sin cast.json): descubrió 13 personas reales
después de filtrado anti-stopwords + deduplicación (ENTREVISTADO_4 vs ENTREVISTADO_4
Fede colapsadas a la versión larga). Personas detectadas:
ENTREVISTADO_3, ENTREVISTADO_4, ENTREVISTADO_2, ENTREVISTADO_8, ENTREVISTADO_9,
ENTREVISTADO_10, ENTREVISTADO_11, ENTREVISTADO_1, ENTREVISTADO_12, ENTREVISTADO_13, ENTREVISTADO_5, ENTREVISTADO_6
Guillermo, ENTREVISTADO_7.

### Patrón "MERGE en mismo canonical_name" (Fase 2)

Cuando DBSCAN separa la misma persona en clusters distintos (luz/ángulo
diferentes) pero el audio confirma que son la misma identidad, las
filas de `face_identities` del cluster duplicado se redirigen al
identity_id ganador y se borra la fila duplicada de `face_catalog`. En
Zezzions: cluster 4 y cluster 15 ambos asignados a "ENTREVISTADO_6"
→ merge automático, 21 detecciones consolidadas en una sola identity.

### Sobrescritura solo de nombres provisionales

`attribute_faces_via_transcript` v2 sobrescribe `face_catalog
.canonical_name` SOLO si el nombre actual matchea `^Persona_\d+$`
(provisional generado por DBSCAN). Para nombres ya curados por usuario,
preserva e imprime advertencia. Esto permite correr el script en bucle
sin destruir asignaciones manuales.

### Fallback "interview sin questions" + multi-persona dual-lavalier

Bug detectado en Zezzions: el clip 2645 (ENTREVISTADO_13 y ENTREVISTADO_10, entrevista dual)
no recibía atribuciones porque no tenía `question_segments` (era una
entrevista marcada DESPUÉS de derive_question_segments). Fix con dos
mejoras:

1. **Fallback "interview_no_questions"**: si el clip es entrevista,
   tiene caras detectadas, y tiene `clip_characters` curado, atribuir
   TODAS las caras a la(s) persona(s) listadas con peso 0.5/N
   (incertidumbre temporal porque no sabemos cuál cara es cuál).

2. **detect_interviewee_from_characters retorna lista** (no string).
   Si `clip_characters` dice "ENTREVISTADO_13 y ENTREVISTADO_10", ambos reciben atribución
   con peso 0.4 cada uno. El clustering y el merge posterior asignan
   identidad por mayoría.

Resultado en Zezzions tras estos cambios + threshold 0.55 → 0.40:
- 3 identidades confirmadas → **6 identidades reales** (ENTREVISTADO_1,
  ENTREVISTADO_13, ENTREVISTADO_6, ENTREVISTADO_2, ENTREVISTADO_4, ENTREVISTADO_3).
- 448 detecciones asignadas a personas reales (de 1141 totales).

### Preservar curaduría manual en clip_characters

Aprendido al perder el "ENTREVISTADO_5* (hermano de Joaquín)" del clip 2573:
el script `attribute_faces_via_transcript` sobreescribe sin distinguir
manual vs auto. Fix: detectar formato `(~N)` (marcador propio del
script). Si la fila NO contiene `(~`, es manual y se preserva.

Esto vale para CUALQUIER script auto: si quieres mezclar con curaduría
humana, usa un marcador distintivo en tus outputs y verifica ausencia
del marcador para identificar curado a mano.

### Sync por eventos sonoros — efectivo solo si hay eventos discretos

`bin/sync_by_events.py` con PANNs detecta eventos del AudioSet y los usa
como anchors para offset. Funciona, pero requiere eventos genuinamente
DISCRETOS — aplauso, bang, whistle, risa. Continuos (speech, music) NO
sirven porque dan falsos positivos masivos.

**Resultado en Zezzions 2026-05-26**:
- 10337 eventos indexados (5298 audios + 5039 videos).
- De los 4 grupos anchor: 15 applause + 1 bang + 0 whistle + 54 laughter.
- **Solo 1 video** con ≥2 anchor events de los grupos discretos default.
- 0 sync pairs creados por este método (no había suficiente material).

**Lección**: PANNs es una buena infraestructura pero el sync por eventos
brilla en proyectos con eventos discretos abundantes (concierto con
aplausos entre canciones, escalada con grupos cantando, eventos deportivos
con bangs/silbatos). Para entrevistas con música ambient continua, el
método es complemento marginal de sync_combined.

**Recalibración del score** (después de observar falsos positivos):
- min_matches ≥ 3 obligatorio (antes 2).
- Confianza = 0.3 × distinctiveness + 0.5 × coverage + min(0.15, n/50).
- distinctiveness = matches en cluster / total candidates.
- coverage = matches / min(events_video, events_audio).

Sin esta recalibración, dos events random del mismo grupo daban conf=0.95
trivialmente. La fórmula exige que el cluster ganador domine claramente.

### Uso de la infraestructura `sound_events` más allá de sync

Aunque sync por eventos no aportó en Zezzions, la tabla `sound_events`
sigue siendo útil para:

1. **Auto-categorizar audios**: si `event_group=music` ocupa >70% del
   tiempo, audio es ambient musical; si `speech` >70%, es entrevista.
2. **Localizar momentos de risa/aplauso** en B-roll → markers especiales
   en Resolve ("hito narrativo" automático).
3. **Detectar "música live" vs "música editada"** por concentración de
   eventos `singing` cerca de `applause` (cantante + reacción público).
4. **Filtrar transcripts alucinados**: si Whisper transcribió texto
   denso pero PANNs dice >80% del audio es `music`, probable alucinación.

### Refinamiento sub-segundo del offset post-sync (Fase 4+)

`lib/sync_refiner.py` + `bin/refine_sync_pairs.py` — resuelve el caso
reportado por el usuario en Zezzions 2026-05-26: *"el audio del
interlocutor es correcto pero el sync no coincide exactamente"*.

**Diagnóstico**: los métodos de sync existentes tienen jitter:
- question-anchor / phrase-match: ~200ms (timestamps Whisper).
- envelope: ~100ms (envelope sample rate).
- chromaprint: ~125ms (8 hashes/s).

Para lip-sync perfecto necesitamos < 40ms (1 frame a 24fps).

**Algoritmo del refiner**:
1. Toma el offset existente como punto de partida.
2. Extrae 30s de A1 (audio cámara) y A2 (lavalier) alrededor del centro
   temporal del video (zona típica de voz limpia).
3. **Banda de voz** (300-3400 Hz, ffmpeg highpass+lowpass) — elimina
   rumble bajo y agudos donde domina la música.
4. **Envelope log-RMS** con hop 10ms — captura DINÁMICA del habla
   (onset/offset de sílabas) en lugar de PCM raw que se confunde con
   música.
5. Cross-correlate FFT, buscar peak en ±2s alrededor del offset actual.
6. Resolución resultante: **10ms**.

**Filtros de aceptación**:
- `prominence >= 0.30`: peak claramente dominante sobre el segundo mejor.
- `|delta| >= 30ms`: skip cambios despreciables.
- `|delta| <= 2500ms`: skip cambios sospechosamente grandes.

**Resultado en Zezzions**: 6 refinements aplicados de 39 pairs.
- ENTREVISTADO_2 (2786): +3.4s → **+5.23s** (Δ +1.83s, prom 0.70) — caso
  reportado por usuario, ahora corregido.
- ENTREVISTADO_8+ENTREVISTADO_7 (2796 segundo audio): -1.72s → -0.05s (Δ +1.67s, prom 2.82).
- ENTREVISTADO_4 (2671): -10.1s → -9.88s (Δ +0.22s, prom 0.53).
- 3 pairs más con prominence >0.50.

**Hipótesis del bias sistemático**: muchos refinements convergen en
Δ≈+1.83s. Esto sugiere que `question-anchor` mide el inicio de la
pregunta (entrevistador) en video y busca match en audio externo
(lavalier del entrevistado), pero los dos hablan en momentos distintos
→ el match cae con ~1.8s de bias. Worth investigar a futuro: ajustar
question-anchor para que use SOLO timestamps de palabras DEL ENTREVISTADO.

**Casos sin refinar** (prominence baja por música fuerte):
- ENTREVISTADO_1 (2794): peak sugería Δ -400ms pero prom 0.12. Mantener
  original. El usuario debe validar visualmente y reportar.
- ENTREVISTADO_5 (2712): peak sugería Δ -1510ms pero prom 0.21.
  Mantener.

### Pipeline voice-first rebuild (Zezzions 2026-05-26, sync rescue)

Cuando el sync legacy quedó tan deteriorado que las entrevistas mostraban
delay visible (ENTREVISTADO_1, ENTREVISTADO_2, ENTREVISTADO_5), reconstruimos
el sync desde cero. **Lo que funcionó:**

#### 1. `lib/transcribe_strict.py` — re-transcripción quirúrgica con VAD
Reduce alucinaciones de Whisper sobre música ambient:
- ffmpeg pre-filter `highpass=300, lowpass=3400` (banda voz).
- whisper-cli con `--vad` + Silero VAD model (`ggml-silero-v5.1.2.bin`).
- `--no-fallback` + `--no-speech-thold 0.7` + `--logprob-thold -0.5`.
- Resultado en Zezzions: de 59 transcripts alucinados → 4 re-transcritos
  recuperaron contenido útil (ENTREVISTADO_1 2794: de 710 words con
  "No, ya tenemos artística" ×12 → 309 words con preguntas reales).

#### 2. Truncamiento por `clean_until_sec` antes de alinear
`transcript_quality.analyze_transcript()` detecta la zona limpia inicial
y donde empieza la alucinación. Pasar SOLO esas palabras a
`transcript_sync.align()` elimina anchors corruptos.

Para ENTREVISTADO_3 (2571): el clean_until truncó video a 226 words y
audio (00003 Dr) a 476 words → 113 anchors n-grama-3 → offset -96.34
(coincide con el legacy -96.66, diferencia 320ms).

#### 3. Búsqueda de audio por 4-grama match cuando voice_match falla
Si no hay `face_voice_links` para el cluster de cara, fallback más útil
que extraer voz del A1: calcular **set de 4-gramas del primer 2000 chars
del transcript del video**, intersectar con cada audio. El audio con
más 4-gramas en común es el match correcto.

```python
v_4grams = set(' '.join(words[i:i+4]) for i in range(len(words)-3))
# Repetir para cada audio, count intersección.
```

Validación Zezzions: ENTREVISTADO_3 2571 ↔ 00003 Dr dio **157 4-gramas comunes**
vs 75 con Izq vs ≤1 con todos los demás. Match inequívoco.

#### 4. Pipeline correcto: identidad PRIMERO, offset DESPUÉS
- **Pilar 1**: voice_match contra audios candidatos (cuando face_voice_links existe)
- **Pilar 2**: si voice_match falla → 4-grama match + transcript_sync.align
- **Pilar 3**: refinement sub-segundo SOLO si prominence ≥ 0.20

#### 5. Convención de signo del offset (re-confirmada)
`offset = audio_start - video_start`, equivalente a
`audio_t = video_t - offset`. Bug detectado en primera versión de
`compute_sync_offset.py`: el cálculo de cross-correlation invertía el
signo. Fix: `offset = v_start - (delta_samples / ENV_SR)` (no al revés).

Verificación: con la corrección, 2598 ENTREVISTADO_9 y 2645 ENTREVISTADO_13/ENTREVISTADO_10 dieron
offsets ≈ idénticos al legacy. Casos donde el legacy estaba MAL
(2794 Jorge +14.8 → +3.42; 2786 Diego refinado-mal +5.23 → +2.34)
reciben ahora ofsets correctos.

#### 6. `method='*-locked'` para pares rescatados manualmente
Los pares creados por transcript-rescue se marcan con sufijo `-locked`
en method. `refine_sync_pairs.py` ignora pares con `method='manual'` y
debe ignorar también `-locked` para no destruir el rescate.

### Lo que NO funcionó (anti-patrones del sync rebuild)

1. **Voice embedding del A1 del video como expected_emb fallback**:
   con música ambient fuerte, el embedding captura más la firma del
   evento que la voz humana. Match a cualquier audio del mismo evento
   con sim 0.7-0.9, sin discriminación útil.

2. **Cross-correlate envelope full-range sin offset seed**: para 2712
   ENTREVISTADO_5 (audio truncado), encontró un peak en posición
   incorrecta → offset +81.22 vs el correcto -73.8 (signo invertido,
   magnitud distinta). **Mitigación**: si existe un sync legacy con
   conf≥0.5, usar su offset como seed y buscar en ventana ±10s solamente.

3. **Refinement con prominence baja (<0.20)**: ENTREVISTADO_3 Izq con prom=0.31
   produjo Δ-1.9s que empeoró el offset (de -96.44 a -98.34 vs el Dr
   en -96.34). **Mitigación**: subir umbral a 0.40 cuando hay música
   detectada en `sound_events`.

4. **Borrar todos los sync pairs sin preservar `face_voice_links`**:
   identity_fusion solo crea links si HAY sync. Al borrar el sync,
   se perdieron todos los links → expected_voice_embedding_for_video
   fallaba para clips cuyo voice_catalog no tenía nombre real. **Mitigación**:
   antes de borrar audio_sync_pairs, snapshot también face_voice_links
   y re-poblar con el nuevo sync.

### Derivación de sync por hermano (idea del usuario, Zezzions iter2 2026-05-26)

Cuando un lavalier está bien sincronizado con un video, su hermano
(mismo número, otro canal Dr/Izq) puede DERIVAR su sync automáticamente
si ambos capturan el mismo contenido sonoro. Implementado en
`lib/lavalier_pairs.py`.

**Detección de aplicabilidad** (score combinado, no AND estricto):
```
score = 0.4 × overlap_4grama_transcript +
        0.3 × envelope_cross_correlation_prominence +
        0.3 × voice_embedding_similarity
applicable: score ≥ 0.40 Y cada componente ≥ umbral mínimo (0.20/0.20/0.65)
```

**Derivación**: si audio_A está syncado con video V (offset_A), y A↔B son
hermanos aplicables con delta_sec:
```
offset_B = offset_A + (sign × delta_sec)
```

Método se marca como `lavalier-derived-locked` (blindado contra
refinement futuro automático).

Validación Zezzions: 00003 Dr ↔ Izq detectado como aplicable (overlap 33%,
prom 31%, vsim 73%, score combinado 0.44). Derivó automáticamente sync de
2572 ENTREVISTADO_3 parte 2 ↔ 00003 Dr (off=-531.75) partiendo del sync existente
con 00003 Izq (off=-531.83). Diferencia de 0.08s = el delta entre los dos
lavaliers del mismo Wireless PRO RX.

### Derivación cronológica por mismo audio (ENTREVISTADO_12 parte 2)

Variante de la idea anterior: si dos VIDEOS consecutivos comparten el
MISMO audio (porque el audio sigue grabando entre dos tomas de cámara),
el offset del segundo puede derivarse del primero:

```
gap_wall = creation_time_2 − creation_time_1   # del manifest
offset_2 = offset_1 − gap_wall
```

Validación Zezzions: 2596 ENTREVISTADO_12 parte 1 con offset_2596=-115.07
correctamente. 2597 parte 2 tenía offset_2597=-503.57 (con delay 504ms).
Cálculo: gap_wall = 388s → offset_esperado = -115.07 − 388 = -503.07.
Diferencia de 0.504s explica EXACTAMENTE el delay que el usuario reportó
visualmente. Corregido a -503.07, marcado `chrono-derived-locked`.

**Cuándo aplica**:
- Dos videos con offsets a un mismo audio.
- creation_time confiable en ambos clips.
- Resultado tiene Δ vs offset previo < 5s (sino sospechoso).

### `method LIKE '%-locked'` como sufijo blindado

Convención: cualquier método con sufijo `-locked` no debe ser tocado por
`refine_sync_pairs.py` ni por re-builds automáticos. Casos hasta ahora:
- `transcript-rescue-locked` — rescate manual transcript-based.
- `chrono-derived-locked` — derivado de cronología.
- `lavalier-derived-locked` — derivado de lavalier hermano.

Fix en `refine_sync_pairs.py`:
```sql
WHERE sp.method != 'manual' AND (sp.method NOT LIKE '%-locked')
```

### Endurecimiento de filtros en `compute_sync_offset.py` (iter2)

Detectados falsos positivos B-roll (2585, 2722, etc.) con sync espectral
musical sin voz humana. Fix:
1. Si `n_persons_in_frame = 0`, exigir `identity_score ≥ 0.80` (vs 0.65).
2. Si spread top1−top3 en combined_score < 0.05, es señal ambient
   genérica — marcar `needs_review`, no escribir.

### `propagate_names` con co-ocurrencia consistente (iter2)

Bug detectado: el voice_catalog 39 (con 5 caras distintas co-ocurriendo)
recibió canonical_name="ENTREVISTADO_1" porque era el con más votos pero NO
era dominante claro. Fix en `lib/identity_fusion.py`:
1. Voice destino debe co-ocurrir con UNA cara que domine ≥70%.
2. NO sobrescribir nombre ya asignado.
3. Cara no debe estar mapeada a >2 voice_catalogs distintos (cara genérica).

Resultado en Zezzions: de 3 propagaciones (algunas incorrectas) a 2
estrictas correctas: ENTREVISTADO_4 → voice 41, ENTREVISTADO_3 → voice 40.

### Ground truth desde .drt — el atajo definitivo (Zezzions iter3 2026-05-26)

Cuando el usuario arma manualmente una timeline de sync en Resolve y la
exporta como `.drt`, ese archivo es **ground truth**. Mucho más confiable
que cualquier sync automático.

**`bin/import_drt_ground_truth.py`** parsea el `.drt` (es ZIP+XML) y
extrae:
- `Sm2TiVideoClip`: `Name`, `Start`, `Duration`, `MediaFilePath`.
- `Sm2TiAudioClip`: igual + `In` (sub-rango del WAV mostrado).

**Cálculo del offset desde el .drt**:
```
offset = (audio.Start - video.Start)/fps - audio.In/fps
```
- `audio.Start - video.Start`: cuánto desplazó el editor el audio en la
  timeline respecto al video (puede ser 0 = alineado, o positivo si el
  audio empezó "después" del video como Diego +2.75, Jorge +3.37,
  ENTREVISTADO_5 +80.83).
- `audio.In`: punto del WAV mostrado al inicio del clip. Para sync
  típico el audio empezó ANTES del video, entonces `In` es grande
  (skipea esos primeros segundos del WAV) y `offset = -audio.In/fps`.

**Filtros para asociar audio↔video correctamente**:
- `audio.Start ∈ [video.Start − 5s, video.Start + 2min]` (descarta
  audios del clip siguiente en la timeline).
- `audio.End ≤ video.End + 5s` (audio no se extiende a otro video).

**Resultado en Zezzions**: 18 sync pairs extraídos automáticamente del
`.drt` con `method='manual-from-drt'` y `confidence=1.0`. Reemplaza el
sync automático que el usuario había validado como "descontrolado".

**Convención de blindaje**: `method='manual-from-drt'` se trata como
`*-locked` — no se refina ni sobreescribe.

### Bias sistemático del envelope log-RMS (Zezzions iter3)

Comparando 11 pairs de ground truth vs mi sync automático, mi pipeline
producía offsets ~0.5-1.9s MÁS NEGATIVOS que el editor manual. Hipótesis
del bias:
1. Filtro de banda voz (highpass 300 + lowpass 3400 Hz) introduce
   latencia de fase.
2. VAD de Whisper recorta zonas iniciales/finales.
3. Envelope log-RMS hop 10ms acumula desplazamiento por filtros.

**Trabajo futuro**: medir latencia del filter chain ffmpeg con un signo
de impulso (delta function) y compensar en `refine_sync_pairs.py`.

### Convención de offset confirmada empíricamente

`audio_t = video_t + offset_sec` donde `offset = audio_start - video_start`.
- offset < 0: audio empezó antes que video → video_t=0 cae en audio_t=|offset|.
- offset > 0: audio empezó después → primeros offset segundos del video sin
  audio externo.

`identity_fusion` y `placeSyncAudio` (Lua) usan la misma convención.

### Identidad de hermanos lavalier por TIMESTAMP, no por nombre (Zezzions iter4 2026-05-26)

**Bug raíz**: asumir que `Dr/00007.WAV` e `Izq/00007.WAV` son hermanos del
mismo Wireless PRO RX porque comparten número de archivo. **Falso**.
Cuando uno de los dos TX se apaga (batería baja, modo standby), los
contadores se desincronizan independientemente. En Zezzions el TX-Izq
se apagó dos veces (entre Izq/00005 y Izq/00006), haciendo que los
números siguientes estuvieran desfasados:

| Mi asunción | Realidad por timestamp+duración+transcript |
|---|---|
| Dr/00004 ↔ Izq/00004 | Dr/00004 ↔ **Izq/00006** (mismos minutos, overlap 0.23) |
| Dr/00005 ↔ Izq/00005 | Dr/00005 ↔ **Izq/00007** (ENTREVISTADO_13/ENTREVISTADO_10, overlap 0.54) |
| Dr/00006 ↔ Izq/00006 | Dr/00006 **solo** (TX-Izq apagado) |
| Dr/00007 ↔ Izq/00007 | Dr/00007 **solo** (ENTREVISTADO_5 DJ, TX-Izq apagado) |
| Dr/00010 ↔ Izq/00010 | Dr/00010 ↔ **Izq/00008** |
| Dr/00012 ↔ Izq/00012 | Dr/00012 ↔ **Izq/00009** |

**Pipeline correcto para construir `lavalier_pairs`**:
1. `stat -f %m` o `os.stat(path).st_mtime` de cada WAV.
2. Cross-match Dr × Izq exigiendo `|Δmtime| ≤ 120s`.
3. Verificar contenido con 4-grama overlap del transcript (`≥ 0.20`).
4. Calcular delta empírico con **transcript ngram-alignment**
   (más robusto que envelope cross-correlate en ambient musical):
   - Extraer todos los n-gramas (n=4) únicos de ambos transcripts.
   - Para cada n-grama común, `delta_i = t_b - t_a`.
   - Si ≥100 anchors, `delta = median(delta_i)`. Aplicable.
   - Si <100 anchors o overlap <0.20, NO derivar hermano.

**Resultado Zezzions iter4** (transcript ngram-alignment):

| Hermanos | n anchors | delta | overlap |
|---|---|---|---|
| Dr/00001 ↔ Izq/00001 | 1492 | -0.08s | 0.42 |
| Dr/00003 ↔ Izq/00003 | 881 | +0.06s | 0.46 |
| Dr/00004 ↔ Izq/00006 | 205 | +0.06s | 0.23 |
| Dr/00005 ↔ Izq/00007 | 319 | 0.00s | 0.54 |
| Dr/00012 ↔ Izq/00009 | 282 | -0.08s | 0.55 |

Delta consistentemente < 100ms — confirma que Wireless PRO RX
inicia los dos TX casi simultáneamente. Signo varía (a veces Izq
inicia primero, a veces Dr) — no es determinístico por firmware.

### Validación temporal del ground truth importado (Zezzions iter4)

**Bug raíz**: `import_drt_ground_truth.py` resolvía paths del `.drt`
por basename + carpeta parent. Cuando el editor reorganizó archivos
físicos entre exports del `.drt` (en este caso: copiar de T7 a T9
con renumeración), el match por basename apuntaba a un archivo distinto.

Caso fundador: 2645 (ENTREVISTADO_13/ENTREVISTADO_10, video grabado 21:12) ↔ Dr/00007 según
`.drt`. Pero Dr/00007 en T9 fue grabado a las **23:27** (ENTREVISTADO_5 DJ).
Imposible que sean el mismo evento sonoro — están a 2h de diferencia.

**Validación obligatoria al importar GT**:
```python
# Para cada pair del .drt:
audio_mtime = os.stat(audio_path).st_mtime
video_creation = clip.creation_time  # del manifest
# El audio debe contener al video:
audio_end = audio_mtime  # (assumiendo mtime = momento de cierre del WAV)
audio_start = audio_mtime - audio_dur_sec
if not (audio_start <= video_creation <= audio_end + 60):
    REJECT_PAIR(reason=f"Audio {audio_mtime} no contiene al video {video_creation}")
```

En Zezzions: aplicada esta verificación, 2645↔Dr/00007 habría sido
rechazado automáticamente y caído a `sync_candidates.status='needs_review'`
en lugar de aceptarse como ground truth.

### Verificador físico de sync con detección de multi-take (iter7)

`bin/verify_sync_physical.py` — verificador automático que clasifica cada
sync pair por categoría empírica. NO modifica BD, solo diagnóstico.

**Métricas calculadas por pair**:
1. **Offset físico por transcript ngram-align** en segmentos de 60s del video.
   Si la mediana de offsets entre segmentos es consistente (MAD < 0.10s),
   confianza alta.
2. **Multi-take score**: % de 4-gramas del video que aparecen >1 vez en
   el audio. >10% = multi-take detectado.
3. **Bias vs BD**: diferencia entre offset físico mediana y offset BD.

**Categorías**:
| Estado | Criterio | Acción |
|---|---|---|
| 🔧 REFINABLE | MAD<0.15, anchors≥100, bias>150ms | Refinement seguro |
| 🎬 MULTI-TAKE | multi_score > 10% | NO refinable — editor manual |
| ⚠ BIAS DUDOSO | bias>150ms pero MAD>0.15 | Refinement con cautela |
| ⚠ DRIFT-LEVE | MAD entre 0.3 y 0.5s | Sync impreciso, dudoso |
| ✓ OK | bias<50ms | Sin acción |

**Resultado Zezzions iter7** (audit de 23 pairs):
- 3 REFINABLE: 2571 Izq, 2572 Izq, 2712 → refinados +Δ
- 1 MULTI-TAKE: 2671 ENTREVISTADO_4 → preservado .drt
- 5 BIAS DUDOSOS: 2566 Dr/Izq, 2571 Dr, 2645 Dr/Izq → refinados con autorización
- 1 DRIFT-LEVE: 2598 → preservado
- 5 no-data, 4 minor, 3 OK

### Detección de multi-take en el audio

Caso fundador: ENTREVISTADO_4 2671 ↔ Dr/00006 (Zezzions iter7, 2026-05-26).
El audio Dr/00006 contiene **3+ tomas distintas** de ENTREVISTADO_4 diciendo
su presentación ("me llamo ENTREVISTADO_4" en t=3.06, t=11.66, t=...). El
editor montó fragmentos de tomas distintas en el video.

**Heurística de detección** (multi-take_score):
```python
v_ngrams = set of 4-gramas del transcript del video
a_count = Counter of 4-gramas del audio (con conteo)
multi_count = sum(1 for ng in v_ngrams if a_count[ng] > 1)
multi_score = multi_count / |v_ngrams ∩ a_keys|
```

Umbral 10%: si más del 10% de los 4-gramas comunes están duplicados
en el audio, multi-take confirmado.

**Implicación**: NO existe un único offset que sincronice todo el clip.
Cada segmento del video requeriría un offset distinto correspondiente
a la toma del audio que el editor usó.

**Acción**: preservar el offset original del .drt manual del editor
(es el mejor compromiso visual). Marcar como `multi-take-detected`
en notes. NO aplicar refinement automático.

**Solución para el usuario**: en Resolve, cortar el audio en segmentos
correspondientes a cada toma y ajustar cada uno manualmente.

### Transcript maestro A1+lavaliers para detectar mejor preguntas (iter9, 2026-05-27)

**Insight del usuario** (Zezzions iter9): *"Las preguntas del entrevistador
se escuchan mejor en el audio de la cámara. Combina los dos audios externos
+ A1 para identificar mejor preguntas y respuestas."*

**Razón física**:
- A1 de cámara: cerca del entrevistador → preguntas claras (cámara apunta
  al entrevistado, micrófono del operador escucha al entrevistador atrás).
- Lavalier del entrevistado: SU voz nítida, voz del entrevistador débil.
- Lavalier del entrevistador (si existe): inverso.

Si solo uso UNO de los tres, pierdo la mitad de la conversación. La detección
de preguntas en español (regex `¿...?` + palabras `qué/cómo/cuándo`)
necesita ambos lados de la conversación.

**Resultado Zezzions iter9** (14 entrevistas con master):

| Clip | Preguntas antes (A1 solo) | Preguntas con master | Δ |
|---|---|---|---|
| 2645 ENTREVISTADO_13/ENTREVISTADO_10 | 0 | **25** | +25 |
| 2712 ENTREVISTADO_5 | 2 | **22** | +20 |
| 2794 ENTREVISTADO_1 (alucinado A1) | 0 | **19** | +19 |
| 2786 ENTREVISTADO_2 (alucinado A1) | 0 | **17** | +17 |
| 2566 ENTREVISTADO_6+ENTREVISTADO_11 | 7 | (audio) | similar |
| 2598 ENTREVISTADO_9 | 1 | **8** | +7 |
| 2671 ENTREVISTADO_4 | 8 | 8 | ≈ |
| Total video | 41 | **132** | **+91 (+222%)** |

### Subagentes: cuándo delegar y cuándo no (iter9.8, 2026-05-27)

El usuario propuso usar subagentes para delegar tareas. La validación
empírica (auditoría multi-señal de entrevistas perdidas) reveló la
división correcta:

#### Subagentes (Agent tool, fan-out) — para JUICIO y PERSPECTIVA

✓ Aplicar cuando cada "lente" requiere INTERPRETAR contenido:
- Auditoría con perspectivas diversas (un agente por tipo de señal,
  cada uno ciego a los otros — captura lo que un filtro único pierde)
- Verificación adversarial (N escépticos intentando refutar un sync)
- Curaduría de tramos (un agente por entrevista lee y escribe full_text)
- Investigación cross-archivo (Explore escanea manifest en paralelo)

#### Scripts deterministas — para FILTRADO mecánico

✗ NO usar subagentes cuando el trabajo es queries SQL + reglas fijas.
Eso es un script Python: más barato, reproducible, versionable.

**Error cometido (iter9.8)**: lancé 4 subagentes para una auditoría
cuyos "4 lentes" eran TODOS queries SQL. Gasté ~118k tokens en lo que
`bin/audit_lost_interviews.py` (determinista) hace gratis. Peor: los 4
agentes descubrieron que **el disco no estaba montado** — algo que
`ls /Volumes/` decía en 1 segundo.

#### Las dos reglas duras

1. **Verificar precondiciones baratas ANTES de delegar trabajo caro.**
   ¿Existe el disco? ¿Hay datos? Un `ls` o un `SELECT COUNT(*)` va
   primero, siempre.

2. **Filtrado → script; juicio → subagente/Claude.** El patrón validado:
   ```
   bin/audit_lost_interviews.py  (determinista)
     → produce candidatos rankeados por nº de lentes que los señalan
   Claude/subagente
     → juicio SOLO sobre candidatos ambiguos (leer transcript:
        ¿entrevista o performance musical o alucinación?)
   ```

#### Resultado en Zezzions (validación del patrón)

`audit_lost_interviews.py` señaló 10 candidatos con 4 lentes (caras,
audio, transcript, posición). El juicio de Claude (barato, leyendo
transcripts) clasificó:
- 1 entrevista real perdida (2644, ya rescatada antes)
- 1 entrevista-solo-video confirmada (2573, sin lavalier)
- 3 performances musicales en vivo (no entrevistas)
- 3 alucinaciones puras / B-roll
- 2 charlas ambiguas

El script hizo el 90% del trabajo (filtrado). Claude hizo el 10% que
importa (juicio). **Esa es la proporción correcta.**

### Meta-doctrina: principios de construcción del asistente (iter9.7, 2026-05-27)

**Cinco lecciones críticas** aprendidas del rescate de la entrevista 2644
(11:29 min con 3 personas que se había perdido silenciosamente):

#### 1. Las cascadas `if X: continue` son pérdida silenciosa de material

Estructura tóxica del pipeline anterior:

```
transcript alucinado → categoría 'entrevista-degradada'
  → skip en compute_sync_offset
  → skip en derive_question_segments
  → skip en attribute_faces_via_transcript
  → skip en export_lua_data
  → INVISIBLE para el editor
```

Cada `if X: continue` debe ir acompañado de:

```python
if condition_fails:
    if other_signals_suggest_relevance:
        report_as_rescue_candidate(...)  # NO descartar silenciosamente
    continue
```

#### 2. Jerarquía de confiabilidad de señales del asistente

Cuando dos señales discrepan, confiar en la más cercana al bit raw:

| Nivel | Tipo | Ejemplos | Confiabilidad |
|---|---|---|---|
| 1 | Metadata del archivo | `duration_sec`, `creation_time`, `file_size` | Absoluta |
| 2 | Señales físicas crudas | `audio_rms_db`, `motion_score`, file SHA | Alta |
| 3 | Detecciones por modelo entrenado | `face_detections`, `sound_events` | Alta |
| 4 | Outputs de modelos generativos | Whisper transcript | Media |
| 5 | Categorías derivadas | `category`, `is_hallucinated` | Variable |
| 6 | LLM-generated text | `full_text`, `description` | Baja |

**Caso 2644**: 36 face_detections (nivel 3) + dur 689s (nivel 1) + rms activo
(nivel 2) **vs** `is_hallucinated=1` (nivel 5). Las señales nivel 1-3 ganan.

#### 3. Rescate antes que skip — preferir recall sobre precision

Para asistencia de edición, **descartar es pérdida permanente**, **falso
positivo es rechazo de 5 segundos por el usuario**. La asimetría dicta
la política:

- Mejor 10 candidatos rescate + 8 rechazados por el usuario
- Que 8 entrevistas correctamente filtradas + 2 perdidas silenciosamente

**Operacionalmente**: cualquier clip excluido por una señal débil debe
emerger en algún reporte de verificación para revisión humana.

#### 4. Bug detectado → query que escanea casos similares

Patrón observado: cuando rescato un caso específico (2644), la lección
NO es "el clip 2644 ahora está bien". La lección es:

> **¿Qué query SQL identifica TODOS los clips con este patrón de fallo?**

Ejemplo:

```sql
-- Casos como 2644: transcript alucinado pero señal visual fuerte
SELECT c.id, c.filename FROM clips c
JOIN transcript_quality tq ON tq.clip_id=c.id
WHERE tq.is_hallucinated=1
  AND c.duration_sec >= 120
  AND (SELECT COUNT(*) FROM face_detections WHERE clip_id=c.id) >= 20
  AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs);
```

Esta query queda persistente en `bin/verify_interviews.py`. **Cada bug
debe convertirse en un guardian permanente**.

#### 5. Los componentes del asistente DEBEN combinarse explícitamente

El motor ya tenía todo lo necesario para rescatar 2644 **desde hace 6
iteraciones**:
- face_detections (Fase 2, iter1)
- waveform_sync_full (iter9.5)
- retranscribe_with_context (iter9.3)
- master_transcripts (iter9.1)
- project_vocabulary (iter9.4)

**Pero nunca se combinaron porque cada componente operaba aislado**.

El pipeline correcto NO es "ejecuta cada paso en orden". Es:

> **Cuando un paso falla, prueba alternativas usando los outputs de otros pasos.**

Ejemplo operacional para entrevistas:

```
detección entrevista FALLA por A1 alucinado
  → ¿hay face_detections?
    → ¿hay audios externos sin sync de duración compatible?
      → ¿waveform A1↔candidato encuentra peak?
        → re-transcribir candidato con prompt contextual
          → si transcript candidato es limpio: ENTREVISTA RESCATADA
```

Esta lógica de "fallback en cascada" debe estar en TODOS los pasos del
pipeline, no solo en detección de entrevistas.

### Sync por waveform A1↔lavalier como método PRIMARIO (iter9.5, 2026-05-27)

**Insight del usuario** (Zezzions iter9.5): *"antes de incluso hacer sync
con transcript deberías considerar hacerlo con el waveform en DaVinci"*.

**Razón física**: el sync por waveform mide energía RMS (loudness) en
banda voz — es independiente del contenido lingüístico. Funciona aun
cuando:
- Transcripts están alucinados o son inconsistentes entre fuentes
- Hay multi-take (ENTREVISTADO_4 2671) que confunde transcript matching
- Pocas palabras anchor comunes (clips muy cortos, B-roll)
- Whisper falla por música ambient

**`lib/waveform_sync_full.py`** — sync por envelope log-RMS FFT
cross-correlate en **rango COMPLETO** del audio (no ventana ±2s). Usar
cuando:
- Sync inicial sin ground truth
- El transcript sync da resultados sospechosos
- Verificación de pairs ya escritos

**Algoritmo**:
```python
# 1. Cargar A1 del video (60s del medio del clip, banda voz 300-3400 Hz)
# 2. Cargar audio externo COMPLETO (banda voz)
# 3. Envelope log-RMS @ 100 Hz (hop 10ms)
# 4. Cross-correlate FFT full range
# 5. Encontrar peak global con prominence
# 6. offset = v_start_in_video - lag_sec
```

**Resultado Zezzions iter9.5** (waveform sobre 23 pairs):

| Caso | BD (transcript) | Waveform | Δ |
|---|---|---|---|
| ENTREVISTADO_3 2571 ↔ Dr/00003 | -96.605 | **-96.661** | -56ms ✓ |
| 2566 ↔ Dr/00001 | -334.810 | **-334.658** | +152ms ✓ |
| 2570 ↔ Dr/00002 | -411.245 | -411.270 | -26ms (no cambio) ✓ |
| 2572 ↔ Dr/00003 | -531.657 | **-531.929** | -273ms ✓ |
| 2596 ↔ Izq/00004 | -114.948 | **-115.070** | -122ms ✓ |
| 2597 ↔ Izq/00004 | -502.961 | **-503.068** | -107ms ✓ |
| ENTREVISTADO_4 2671 ↔ Dr/00006 | -9.843 | **-10.109** | -266ms ✓ |
| ENTREVISTADO_13/ENTREVISTADO_10 2645 ↔ Dr/00005 | -19.912 | **-19.379** | +533ms ⚠ |

14 de 17 viables se actualizaron con waveform. 6 skipped por baja
prominence (clips muy cortos como 2574 de 47s donde el envelope no
encuentra peak claro). 0 actualizados con delta > 5s (criterio safety).

### Criterios de aceptación para waveform sync

```python
if prominence >= 0.30 AND peak_norm >= 0.10:
    if abs(waveform_offset - bd_offset) < 0.030:  # < 30ms
        # BD ya está bien, no cambiar
    elif abs(waveform_offset - bd_offset) < 5.0:  # < 5s
        # Actualizar con waveform (más confiable físicamente)
    else:
        # Sospechoso, no tocar — escalar a revisión humana
else:
    # Confianza baja, mantener BD
```

### Sync entre hermanos lavalier: transcript > waveform

Empíricamente (iter9.5), el waveform entre Dr↔Izq del mismo Wireless PRO
RX FALLA con prominencia ~0.02-0.22 en algunos casos (ENTREVISTADO_13/ENTREVISTADO_10 dio
-25s con prom 0.02 — basura). El sync por **transcript ngram-align**
es MÁS robusto entre hermanos porque las palabras compartidas son señal
clara aún con audio sucio.

→ **Política**: para hermanos lavalier, usar `lavalier_pairs.delta_sec`
calculado por transcript. Para video↔lavalier, usar waveform.

### Re-transcribir con prompt contextual y corregir vocabulario (iter9.3-9.4)

**Insight** (Zezzions iter9.3, 2026-05-27): Whisper transcribe DRAMÁTICAMENTE
mejor cuando se le da contexto del dominio en `--prompt`. Errores típicos
sin contexto:
  - "Vici" → debería ser "Avicii"
  - "boro" → "porro"
  - "Tecno" → "techno"
  - "ENTREVISTADO_3_MAL_ESCRITO" → "ENTREVISTADO_3"
  - Frases sin sentido como "Para tornar a esa pelota"

**Tres capas de mejora**:

#### 1. Re-transcripción con prompt contextual (`bin/retranscribe_with_context.py`)

```python
# Por cada audio sincronizado con un video de entrevista:
prompt = (
    f"Tipo: {category}. "
    f"Personajes: {chars_clean}. "
    f"Tema: {full_text_action_part}. "
    "Entrevista en evento de música electrónica Sessions. "
    "Vocabulario común: música electrónica, IDM, EDM, productor, DJ, "
    "Avicii, Daft Punk, Aphex Twin, Tomorrowland, Sessions, evento. "
    "Mexicanismos: güey, wey, chido, neta, cabrón, porro, banda, ahorita, chingón."
)
# Whisper.cpp prompt límite: n_text_ctx/2 ≈ 224 tokens ≈ 800 chars
```

**CRÍTICO**: NO usar `clip_descriptions.description` en el prompt — puede
contener debug info como "distinct=409 clean_words=936 garb=7%" que
confunde a Whisper. Usar `clip_curated_segments.full_text` (formato
canónico curado por Claude) que solo contiene contenido editorial real.

#### 2. Corrección por vocabulario del proyecto (`bin/correct_transcripts_vocab.py`)

Post-process sobre los transcripts (originales O re-transcritos).
Estrategia segura: NO sobre-corregir.

```python
# Layer 1: EXPLICIT_CORRECTIONS_LOWER (errores documentados)
EXPLICIT_CORRECTIONS_LOWER = {
    "vici": "Avicii",
    "tecno": "techno",
    "session": "Sessions",  # singular sin 's' = error frecuente
    "leon": "León",
    "garageband": "GarageBand",
    "ableton": "Ableton",
}

# Layer 2: Fuzzy match SOLO contra nombres propios del cast
# (ENTREVISTADO_3, ENTREVISTADO_13, ENTREVISTADO_9, etc.) — NO contra palabras comunes
# Y SOLO si la palabra original está capitalizada Y no es inicio de oración
# Y la similaridad ≥ 0.85
# Y la diferencia de longitud ≤ 2 caracteres
```

**Anti-sobrecorrección** (lección importante):
- ❌ "mandas" → "ENTREVISTADO_4" (verbo válido sobre-corregido)
- ❌ "güeyes" → "güey" (plural válido)
- ❌ "chingona" → "chingón" (femenino válido)

→ Fix: fuzzy match SOLO sobre `is_proper_noun_capitalized(w) AND NOT
sentence_start`. Las palabras comunes en minúsculas pueden pasar siempre.

#### 3. Vocabulario del proyecto (`lib/project_vocabulary.py`)

```python
def build_project_vocabulary(conn, disk_root) -> set[str]:
    vocab = set()
    vocab.update(DEFAULT_DOMAIN_TERMS)        # música electrónica + mexicanismos
    vocab.update(load_cast_from_manifest(conn))  # face_catalog + voice_catalog
    vocab.update(load_cast_json(disk_root))    # <disk>/.cinema_assistant/cast.json
    return {v for v in vocab if len(v) >= 3}
```

Pendiente del motor: parametrizar `DEFAULT_DOMAIN_TERMS` por proyecto
(cuando se editan documentales de cocina, deporte, etc. el vocabulario
debe cambiar). Por ahora hardcoded a "música electrónica".

### Limitaciones honestas del transcript contextual

Re-transcribir con prompt MEJORA significativamente pero NO es panacea:

1. **Audio inherentemente sucio**: si el micrófono captó música muy alta
   sobre voz baja, Whisper no puede recuperar las palabras. El prompt
   ayuda solo cuando hay suficiente señal.
2. **Frases ininteligibles físicamente**: "Para tornar a esa pelota" puede
   ser una transcripción que NO existe en el audio real — Whisper la
   alucinó por VAD. El prompt no la elimina porque el LLM de Whisper
   completa lo que escucha.
3. **Nombres propios sin contexto**: si en el prompt no aparece "Aphex
   Twin" Whisper lo transcribirá como "Apex Twin" o "Mafex Twin".
   Mantener el vocabulario del proyecto ACTUALIZADO con cada nombre
   importante.

### Filtro estricto de preguntas (`lib/master_question_detect.is_real_question_q`)

Heurística post-detección que descarta nonsense (Zezzions iter9.4):

```python
def is_real_question_q(q: str) -> bool:
    # 1. Rechazar fillers ("Qué tal", "Verdad si te suena", "Por qué" solo)
    # 2. Mínimo 3 palabras
    # 3. Rechazar duplicación adyacente ("te te", "qué qué")
    # 4. Si ≤4 palabras: DEBE empezar con interrogativo claro
    # 5. Si >4 palabras: interrogativo acentuado (qué/cómo) en cualquier posición
    #    O empezar con interrogativo
    # 6. ≤25% short_garbage (palabras de 1-2 letras no comunes)
    # 7. DEBE contener verbo de entrevista (es/son/empezaste/gusta/quieres/etc)
    # 8. Rechazar patrones de letras raras (ww, jj, kh, ph, ck, etc)
```

Test coverage: 18 casos (8 reales + 10 basura) → 100% acertado.

### CRÍTICO: detectar preguntas POR SOURCE, NO concatenar palabras (iter9.2)

**Bug fundador** (Zezzions iter9.0): el master transcript inicial concatenaba
todas las palabras de A1+lavaliers en un solo `text` ordenadas por
tiempo. Cuando se aplicaba `split_questions` sobre ese texto, las palabras
del **entrevistador y del entrevistado se intercalaban**, generando
preguntas absurdas como:

> "Cómo soy mi ENTREVISTADO_13 niño y aquí estaba perdido, mi niño perdido, ENTREVISTADO_10 se y James. pues conocieron"

(Mezcla "¿Cómo se conocieron?" del entrevistador + "Soy ENTREVISTADO_13 y aquí
estaba mi niño perdido, ENTREVISTADO_10" del entrevistado.)

**Fix iter9.2** — procesar cada source por separado:

```python
# master.json incluye 'by_source': {
#   'video_a1':   {'text': '...', 'words': [[w,t], ...]},
#   'audio_146':  {'text': '...', 'words': [[w,t], ...]},
#   'audio_155':  {'text': '...', 'words': [[w,t], ...]},
# }
# Cada source es un transcript independiente con palabras en escala video.

for src, payload in master['by_source'].items():
    qs = detect_questions_in_source(payload['text'], payload['words'])
    # Buscar respuesta en OTRO source en ventana post-pregunta
    ...
```

**Resultado en Zezzions iter9.2**:
| Clip | Antes (A1 solo) | Master concat | Master per-source |
|---|---|---|---|
| 2645 ENTREVISTADO_13/ENTREVISTADO_10 | 0 | 25 (basura) | **8 reales** |
| 2712 ENTREVISTADO_5 | 2 | 22 (mucha basura) | **17** |
| 2794 ENTREVISTADO_1 | 0 | 19 | **15** |
| 2598 ENTREVISTADO_9 | 1 | 61 (basura) | **14** |
| 2571 ENTREVISTADO_3 | 2 | 23 | **9** |
| Total | 41 | 482 (mucho basura) | **199** |

199 son preguntas reales y deduplicadas; 482 era inflado con mezclas.

### Filtros para preguntas reales del master

1. **Filtro temporal**: descartar preguntas con `start_sec < 0 OR > clip_dur`.
   Las palabras de un lavalier pueden caer fuera del rango del video.
2. **Fillers conversacionales**: rechazar "Qué tal", "Bueno", "Sí", etc.
   No son preguntas reales.
3. **Búsqueda de respuesta cross-source**: si la respuesta en el mismo
   source es flaca (<60 chars), buscar palabras en OTRO source en
   ventana [t_q, t_q + 60s].
4. **Deduplicación inteligente**:
   - Texto idéntico normalizado → SIEMPRE dedupar (sin ventana)
   - Similar dentro de ventana dinámica:
     - Q corta (≤3 sig words): win 15s, threshold 70%
     - Q media (4-5 sig words): win 90s, threshold 60%
     - Q larga (≥6 sig words): win 240s, threshold 55%

### Algoritmo del master transcript (`lib/master_transcript.py`)

Para un `video_id`:

1. Cargar A1 del video: `transcripts/{video_id}.json` → tag `"video_a1"`.
2. Para cada `audio_sync_pair` del video, cargar transcript del audio:
   `transcripts/{audio_id}.json` → tag `"audio_{aid}"`.
3. **Llevar a escala del video**: para cada palabra de audio en `t_a`:
   ```
   t_v_equiv = t_a + offset_sec
   ```
   (offset = audio_start - video_start, negativo si audio empezó antes).
4. Combinar todas las palabras en una lista, ordenar por `t_v_equiv`.
5. **Resolver duplicados**: si A1 y un lavalier dicen la misma palabra
   (case-insensitive) dentro de ±0.5s, preferir el lavalier (mejor SNR).
6. Filtrar palabras fuera del rango `[-1s, video_dur + 1s]`.
7. Guardar en `transcripts/master_{video_id}.json`.

**Formato del master**:
```json
{
  "video_id": 357,
  "sources": ["video_a1", "audio_150"],
  "n_words_total": 1797,
  "n_sync_audios": 1,
  "words": [["me", 0.01, "video_a1"], ["llamo", 0.30, "video_a1"], ...],
  "text": "..."
}
```

### Integración en derive_question_segments

`bin/derive_question_segments.py` modificado:
```python
master_tp = tr_dir / f"master_{clip_id}.json"
if master_tp.exists() and kind == "video":
    tp = master_tp
    n_master_used += 1
elif plain_tp.exists():
    tp = plain_tp
```

El `process_interview` soporta formato standard `[word, t]` Y master
`[word, t, src]` (ignora `src`).

### Filtro de exportación adaptado

`bin/export_lua_data.py` (iter9 update):
- `clips_with_master = { cid : existe master_{cid}.json }`
- Si `clip_id in hallucinated_clips AND clip_id in clips_with_master`:
  question_segments SÍ se exportan (vienen del lavalier limpio).
- `curated_segments` siguen filtrados por `is_hallucinated` (fueron escritos
  manualmente sobre el A1 viejo, no se rescatan automáticamente).

Resultado: clips como ENTREVISTADO_1 2794 y ENTREVISTADO_2 2786 (transcripts del
video alucinados) ahora muestran Purple Q markers correctos derivados del
master transcript.

### Cuándo aplica el master transcript

✓ Aplica cuando:
- El video tiene ≥1 audio externo sincronizado.
- Los offsets están en `audio_sync_pairs` con confidence alta.
- Por lo menos uno de los transcripts (A1 o lavalier) tiene contenido útil.

✗ NO aplica:
- B-roll sin habla.
- Videos sin audio externo sincronizado (solo A1, master = mismo que plain).
- Multi-take en el audio (el master mezcla tomas, sync no lineal).

### Doctrina general para asistentes futuros

**Patrón "combinar fuentes redundantes para mejor señal"**:
Cuando el mismo evento tiene múltiples captures (A1 de cámara + lavalier),
**combinar** las fuentes en escala de tiempo común da mejor resultado que
elegir una sola.

Aplica a:
- Sync detection (combinar transcript + envelope + chromaprint)
- Pregunta detection (combinar A1 + lavaliers)
- Identity matching (combinar face + voice)
- Visual analysis (combinar exposure + motion + faces)

### Markers SOLO en clips con transcript verificable (iter8, 2026-05-27)

**Doctrina**: los duration markers (Green Tramo + Purple Q) son útiles
para el editor SOLO si el contenido del marker corresponde a lo que
realmente pasa en el video. Si Whisper alucinó el transcript ("Suscríbete
al canal" ×100, "no no no" ×28), entonces:

- `clip_curated_segments.full_text` puede estar basado en basura
- `question_segments.question_text` puede ser inventado
- El editor ve markers que NO existen en el material → pérdida de confianza

**Filtro al exportar al Lua** (`bin/export_lua_data.py`):
```python
hallucinated_clips = set of clip_id WHERE transcript_quality.is_hallucinated=1
# Excluir curated_segments y question_segments de esos clips
```

**Política**:
- Si `is_hallucinated=1` → NO mostrar markers Green/Purple para ese clip
- El marker Red ("Posible descarte") sigue saliendo (basado en `clip_analysis`,
  no transcript)
- El marker Cyan de sync sigue saliendo (sync verificado por otros métodos)
- El marker Yellow de highlights sound_events sigue saliendo (PANNs, no transcript)

**Resultado Zezzions iter8**:
| Concepto | Antes | Después filtro | Excluidos |
|---|---|---|---|
| Curated segments en Lua | 282 | 226 | 56 (alucinados) |
| Question segments video | 41 | 41 | 0 (ya todos en limpios) |
| Question segments audio | 104 | 104 | 0 |

### Formato canónico de Green markers (Tramo)

Curated `full_text` debe seguir el patrón:
```
{PERSONAJES_COMA} | {PLANO} + {ÁNGULO} | {ACCIÓN-RESUMEN}. {LUGAR-CONTEXTO}. Idea: '{FRASE CLAVE}'
```

Ejemplos validados (Zezzions, todos `curated_by='claude'`):
- `ENTREVISTADO_6, equipo | PM + Normal | Bromean sobre el formato de la entrevista... Idea: 'así lo hemos hecho ahorita en nuestro Insta'`
- `ENTREVISTADO_3 (artista) | PM + Normal | ENTREVISTADO_3 reflexiona sobre la etapa temprana de su proceso creativo... Idea: 'es una etapa temprana de estas ideas'`

El Lua genera el label como `T1 [PM/Normal]` y la nota como el `full_text` completo + `Personajes: ...` + `(start - end s)`.

### Formato canónico de Purple Q markers (Pregunta)

`question_segments` con:
- `question_text`: texto literal de la pregunta (sin signos de interrogación)
- `response_summary`: resumen de la respuesta del entrevistado (~200 chars)

El Lua genera:
- Label: `Q1: ¿Cómo empezaste a hacer música?` (primeros 60 chars)
- Nota: `¿Cómo empezaste a hacer música?\n\nRespuesta: {response_summary}\n\n(start - end s)`

### Filtro de preguntas unificado: `lib/question_filter.py` (iter10)

**Patrón estructural:** el filtro de preguntas vivía DUPLICADO — versión
"débil" en `derive_question_segments.py` (`_is_real_question`: ≥4 palabras
O interrogativo) y versión "estricta" en `master_question_detect.py`
(`is_real_question_q`). Dos fuentes de verdad → divergían y el camino plano
(solo-video, audio entrevista) usaba el filtro malo. **Fix: una sola fuente
de verdad** en `lib/question_filter.py`, importada por ambos. Sin ciclos
(question_filter no importa a nadie del pipeline).

**El acento como señal lingüística (clave reusable):** Whisper transcribe
español con tildes correctas. El interrogativo SIEMPRE lleva tilde
(`cómo/qué/cuándo/dónde/quién/cuál/cuánto`); el relativo/conjuntivo NO
(`como/que/cuando/donde`). Esa distinción permite detectar preguntas que
Whisper escribió **sin los signos `¿?`** (frecuente en el audio de cámara
del entrevistador) sin tragarse el monólogo del entrevistado. Es gratis y
robusto — no requiere modelo extra.

**Receta de detección "loose" (sin `¿?`) con alta precisión:**
1. Partir el transcript en oraciones (con offsets de carácter para mapear a
   timestamps).
2. Candidata solo si un interrogativo **acentuado** aparece en las primeras
   ~6 palabras (front-loaded = pregunta del entrevistador; embebido tardío =
   relativo del entrevistado).
3. Descartar oraciones > ~22 palabras (run-on = monólogo descriptivo).
4. Pasar TODO por `is_real_question_q` (verbo de entrevista por palabra
   completa, rechazo de primera persona, dedup de trigramas, etc.).
5. Las `¿...?` explícitas se saltan las guardas 2-3 (el signo ya confirma).

**Resultado Zezzions:** 2573 (solo-video) 0→2 markers reales; 2596 recuperó
su pregunta; clip 2566 dejó de emitir 7 falsos positivos de monólogo. Total
94→80 markers, **sin perder ninguna pregunta real** (verificado clip por
clip). La caída de conteo = ruido eliminado, no señal.

**Verbos de entrevista = lista curada, no regex.** `INTERVIEW_VERBS` se
amplía cuando un proyecto revela un verbo real perdido (encargas, presentas,
dedicas…). Más preciso que intentar morfología `-as/-es` (choca con plurales
de sustantivos: "eventos", "lugares").

### Refinamiento físico contra ground truth manual del editor (iter6)

**Insight clave** (Zezzions iter6 2026-05-26): el ground truth del `.drt`
del editor NO es ground truth físico — es el armado **visual** del editor
con tolerancia perceptual humana (~200-700ms en clips difíciles).

Después de importar el `.drt` con fps correcto (iter5), el usuario reportó
que ALGUNOS clips (ENTREVISTADO_3 2571/2572, ENTREVISTADO_4 2671, ENTREVISTADO_5 2712) seguían
fuera de sync. El refinement contra **transcript ngram-alignment**
reveló bias del editor:

| Clip | .drt (manual) | Físico (transcript) | Bias editor |
|---|---|---|---|
| 2566 Dr/00001 | -334.42 | **-334.80** | -380ms |
| 2571 Dr/00003 | -96.31 | **-96.55** | -245ms |
| 2572 Izq/00003 | -531.60 | **-531.95** | -353ms |
| 2671 Dr/00006 | -9.84 | **-10.47** | -627ms |
| 2712 Dr/00007 | +80.91 | **+81.21** | +296ms |
| 2598 Izq/00005 | -87.09 | **-86.90** | +187ms |

Los clips con `|Δ| < 50ms` (`2796`, `2786`, `2645 Dr`) NO fueron
refinados — ya están perceptualmente perfectos según el editor.

### Pipeline definitivo de offset físico

```python
def fine_offset_physical(words_video, words_audio, expected, window=3.0):
    """Cross-correlación de n-gramas de transcript entre video y audio.
    Independiente de fps. Robusto a alucinaciones por filtro de window."""
    # 1. Construir n-gramas (n=3) de ambos transcripts con timestamps
    # 2. Para cada n-grama común, calcular shift = t_audio - t_video
    # 3. Filtrar shifts cuyo |shift - expected| > window (descarta outliers)
    # 4. Mediana de shifts filtrados → offset = -shift
    # 5. Aceptar solo si n_anchors ≥ 20 Y MAD < 1.0s
```

**Criterios para aplicar refinement**:
- `n_anchors >= 20`: suficiente material para mediana robusta.
- `MAD < 1.0s`: consistencia (low jitter de Whisper timestamps).
- `|Δ_vs_drt| > 30ms`: descartar cambios despreciables.
- `|Δ_vs_drt| < 5s`: descartar refinements catastróficos (probable mismatch
  de contenido, no jitter).

En Zezzions iter6, 14 de 23 pairs se refinaron con esos criterios.
Los 9 restantes no tenían transcript suficiente (B-roll, audios cortos)
o el `Δ` era muy pequeño.

### Convención `manual-from-drt` ya NO es intocable

Iteraciones anteriores marcaban `manual-from-drt` como blindado contra
refinement. Iter6 cambia eso: **el refinement por transcript es MÁS
preciso que el armado visual del editor**, así que se aplica encima del
`.drt`. El `notes` registra la cadena: `[iter5: fps fix] [iter6
transcript-refined: -96.305→-96.550 Δ-0.245s n=604]`.

`refine_all_pairs.py` (envelope log-RMS) sigue bloqueado para evitar
falsos positivos en música ambient, pero `bin/refine_pairs_by_transcript.py`
(transcript ngram, futuro script) sí puede tocar `manual-from-drt`.

### Detección automática del fps de la timeline al importar .drt (iter5)

**Bug raíz** (Zezzions iter5 2026-05-26): el `.drt` de Resolve almacena
`Start/Duration/In` como **frames de la timeline**. Mi importador asumía
`fps=24`, pero la timeline era 23.976 (NTSC). Drift proporcional al
offset → delays visibles solo en clips con offset > 100s.

**Validación empírica del fps**:
- Manifest dice video X dura `dur_real` (ej. 399.4s).
- .drt dice `dur_fr = videoclip.Duration` (ej. 9575).
- `fps_real = dur_fr / dur_real` → 23.976 (no 24).

**Detección automática** (`detect_timeline_fps()`):
- El primer `<Sm2TiVideoClip>` tiene `<MediaFrameRate>` con un hex de 32
  caracteres. Los primeros 16 son un **double IEEE 754 little-endian**.
- Decodificar: `struct.unpack('<d', bytes.fromhex(hex_str[:16]))[0]`.
- Validar rango sensato (20 < fps < 60).
- Fallback al default si falla.

Hex comunes:
| Hex (primeros 16) | fps |
|---|---|
| 872211b5dcf93740 | **23.976023976023978** (NTSC, 24000/1001) |
| 0000000000003840 | 24.0 |
| e8e1d5fe6c0c3940 | 25.0 (PAL) |
| 0000000000803e40 | 30.0 |
| 64ddebd1ae3d3a40 | 29.97 (30000/1001) |

### Convención `manual-from-drt-sibling-locked`

Sufijo `-locked` ya existente, pero el prefijo `manual-from-drt-sibling-`
indica que el pair fue **derivado** del ground truth aplicando regla del
hermano (no escrito directamente en el `.drt`). Esto facilita auditoría:

```sql
-- Pares originales del .drt (ground truth puro)
SELECT * FROM audio_sync_pairs WHERE method='manual-from-drt';
-- Pares derivados de hermanos
SELECT * FROM audio_sync_pairs WHERE method='manual-from-drt-sibling-locked';
```

Ambos son intocables por refine, pero el segundo tiene confidence 1.0
asumida (no verificada por edición humana). Si el usuario reporta delay
en uno de estos, el bug está en el cálculo del delta entre hermanos.

## Memoria del proyecto

- **Playbook externo al código** (`~/memoria-asistente-edicion/`) en markdown
  navegable. El código cambia; los principios persisten en formato leíble.
- **Auto-memory mínima** apuntando al playbook. **No duplicar contenido**
  entre auto-memory y playbook — si conflictan, el playbook gana (curado
  por el usuario).
- **Estructura clara**: `metodologia/` (cómo se hace) + `lecciones/`
  (preferencias, errores, patrones, NO-hacer).

## Sync cronológico por skew de reloj (grabación continua) — MAB 2026-06-11

Generalización del §8d de Zezzions, ahora como script del motor:
**`bin/derive_chrono_sync.py`**. Aplica cuando el recorder graba CONTINUO
(archivos consecutivos sin gap, ej. Rode Wireless PRO onboard con cortes
por hora) y las cámaras tienen reloj con desfase constante.

Receta:
1. `sync_transcript` produce anchors de alta confianza (pocos bastan).
2. Por **grupo de cámara** (parent_folder — genérico), medir
   `skew = offset_observado − (wav_start − video_epoch)`. Con reloj sano
   el skew es CONSTANTE: en MAB, VICG +14.38s ±0.34 y Ayan −549.8s ±0.5
   (¡reloj 9 min mal puesto pero establemente mal — funciona igual!).
3. Outliers de skew (>10s vs mediana del grupo) = pares MAL emparejados
   por el alineador → corregir contra el WAV cuya ventana contiene al
   video (method `chrono-corrected-locked`).
4. Videos sin sync: derivar `offset = skew + (wav_start − video_epoch)`
   para el WAV de mayor overlap (method `chrono-derived-locked`).

Resultado MAB: de 22 pares por transcript → 174 derivados + 2 corregidos
(196 de 254 videos con A2; los restantes caen fuera del horario del
recorder). Validación: cadenas chrono-consistentes a ±1.1s en 12/14
anchors consecutivos ANTES de derivar.

**Anti-patrón descubierto (vox pop)**: el host repite las mismas
preguntas/intro decenas de veces al día → el alineador de transcript
puede matchear una COPIA repetida horas después con confianza inflada
(conf 19.49 con 10k anclas = bandera roja, no señal fuerte). El reloj de
pared es el árbitro: si el skew del par se sale de la norma del grupo de
cámara, el par está mal aunque el texto coincida. Corolario: confidencias
> 1.0 en sync_transcript indican fórmula desbordada por contenido
repetitivo — tratar como sospechoso, no como garantía.

Bonus: el skew también detecta cobertura a 2 cámaras — pares de clips de
distinto grupo cuyo tiempo real corregido coincide (±5s) son la misma
escena (en MAB: 8284/2989 a 3s real uno del otro, disfrazados por los
9 min de error del reloj de Ayan).

## Multicámara en V2 por sync compartido (MAB 2026-06-12)

Cuando dos cámaras cubren la misma escena y ambas tienen sync al MISMO
audio externo, su alineación mutua es derivable sin análisis adicional:
`delta = offset_A − offset_B` (validar overlap > 5s). Motor:
`bin/export_multicam_lua.py` (genérico: grupos = parent_folder) emite
`<proyecto>_multicam.lua`; el asistente Lua apila la segunda cámara en
**V2 (V3 si dos compañeros se traslapan)** sobre el clip base en la
timeline de ENTREVISTAS, `mediaType=1` (solo video — el audio bueno ya
está en A2, el A1 del compañero estorbaría). Pedido del usuario en MAB:
"la segunda cámara debería ser otro track de video arriba del V1".
En MAB v2: 68 pares (candidatos por RELOJ corregido — cubre tramos sin audio externo) con verificación directa A1↔A1 (`refine_offset` video↔video); 11/12 pares de entrevista verificados. CLAVE: si una cámara lleva el RX del lavalier al A1 (Ayan en MAB), su A1 es señal limpia y el match A1↔A1 es fortísimo. Indexar pares en AMBAS direcciones en el Lua (la entrevista puede ser el clip b).

**Orden importa**: exportar multicam DESPUÉS del refinement de offsets
(los deltas heredan la precisión del sync). Refinar primero, derivar
después.

## Refinement explícito de pares chrono (`--include-locked-like`)

Los `chrono-derived-locked` se blindan contra refinement CIEGO, pero una
pasada explícita (`refine_all_pairs.py --include-locked-like 'chrono-%'
--min-prominence 0.30+`) es obligatoria antes de entregar: el reloj da
±1s de jitter, el envelope en banda de voz lo baja a <100ms donde hay
voz compartida. En MAB: 31 pares corregidos en 2 pasadas (ventana ±2.5s
y luego ±5s para los Δ grandes); los pares por transcript traían el
jitter documentado de Whisper (±1.5-4s, signos mixtos). Sin esta pasada
el usuario VE el delay en Resolve.

## Sync de precisión: ventana larga + etapa fina + triangulación (MAB iter3)

El estándar de sync de entrega sube de nivel (los métodos de ventana corta
quedan como SEED, no como verdad):

1. **Ventana larga** (hasta 150s del traslape): la música de plaza es
   periódica y produce picos en el beat equivocado que hasta "votan" igual
   en dos ventanas de 45s; el patrón de habla largo es aperiódico y domina.
2. **Etapa fina**: PCM banda-voz alrededor del pico grueso → precisión de
   milisegundos (el envelope hop-10ms deja ±50-100ms residual).
3. **Triangulación**: directa (A1↔A1) vs cadena (off_a−off_b vía el MISMO
   WAV). Solo coincidencia ±0.25s = verificado; desacuerdo = re-medir las
   patas ("reparar la cadena") o NO colocar.

Motor: `bin/verify_multicam_deltas.py` (+ `--repair-chain` corrige el
manifest) y `bin/refine_pairs_longwin.py` (pasada global video↔WAV).
Resultados MAB: 15/21 pares V2 a ±0.02s; 74 offsets de lava corregidos
(+161ms promedio, máx 1.36s — INCLUSO en la cámara cuyo A1 es el lav feed,
donde "no debería dar problema": el error era del método corto, no de la
señal). Validación física: en la cámara lavfeed la corrección longwin debe
dejar prominence ≥0.5 — si no, el pair es sospechoso de origen.

## Sync multi-TX por cadenas de continuidad (FCC 2026-07-09)

Caso: dos Rode Wireless PRO TX grabando continuo en paralelo (izq+der),
cortes por hora, archivos intercalados en la misma carpeta (`00023..25` y
`00037..39` en `Jueves/AUDIO/`). El RX estuvo mal conectado a la cámara —
NINGÚN A1 lleva el lav limpio; los WAV onboard son el único audio bueno.

Extensiones del motor (todas genéricas, lint OK):

1. **`derive_chrono_sync.py --per-chain`** — agrupa los WAV en CADENAS de
   continuidad temporal (mismo dir, inicio ≈ fin anterior ±5 s; greedy por
   cercanía de fin → soporta cadenas intercaladas). Skew por (grupo de
   cámara × cadena) porque **cada TX tiene reloj propio** (delta entre los
   dos TX de FCC: ~1.7-1.9 s, estable entre días). Deriva un par por
   video × cadena → regla 8b (ambos lavas) cumplida por construcción.
2. **`--group-by dirname`** — en layouts anidados (`Jueves/VIDEO/A/`),
   `parent_folder` mezcla las dos cámaras; el grupo correcto es el dirname
   del rel_path. Mismo flag en `export_multicam_lua.py`.
3. **`--bridge-chains`** — transitividad de relojes:
   `skew(g,c) = skew(g,c0) + skew(g2,c) − skew(g2,c0)`. Completa combos
   (cámara × cadena) sin anchors suficientes; se descarta si contradice
   anchors locales débiles. Validación FCC: predicción +0.74 s vs anchor
   local +0.86 s. Pares puenteados llevan confidence 0.80.
4. Con pocos anchors por combo, `--min-anchors 2` es aceptable SI los
   combos se validan cruzadamente por transitividad (los skews de 2
   anchors coincidieron a ±0.3 s con las predicciones).

Resultado FCC: 22 anchors transcript → 395 chrono-derived → 205/324
videos con AMBOS lavs; los 112 sin sync están fuera de las ventanas de
grabación de los TX (física, no falla).

## Rescate multicám: --rescue-unverified + --repair-chain (FCC 2026-07-09)

`verify_multicam_deltas.py` solo re-verificaba pares `verified=true` — una
corrida previa sin `--repair-chain` degradaba pares a false y los volvía
irrecuperables. Flag nuevo `--rescue-unverified`: procesa también los
false (ventana larga + triangulación por WAV compartido + reparación de
patas). FCC: 16 → **55/96 verificados**, y la reparación corrigió offsets
de lav en el manifest con doble ruta convergente.

Nota de calibración: la triangulación por cadena hereda el jitter de los
offsets chrono (±0.3-0.6 s) — con `--chain-tolerance 0.25` los pares
buenos fallan si la cadena no se repara primero. `--repair-chain` es el
default sensato en proyectos chrono-derived.

## Aplicador físico de sync: verify_sync_physical --apply-refinable (FCC 2026-07-09)

La doctrina (sync-sin-ground-truth §Fase 6) decía "🔧 REFINABLE: aplicar
refinement automático" pero no existía aplicador. Ahora
`verify_sync_physical.py --apply-refinable` escribe la mediana física de
segmentos (transcript-based — robusta a música) a los pairs REFINABLE,
respetando `method='manual'`. FCC: 12 pares afinados 0.2-1.4 s.

## Consistencia dual-lav como ecuación exacta (FCC 2026-07-10)

Reporte del usuario: "el lava de la chica está bien pero el del señor no —
se alcanza a colar el audio entre ambos lavas" (el bleed entre lavs hace
audible cualquier desfase >100 ms como eco). Pidió "auto sync con DaVinci";
la respuesta correcta fue el mismo principio (waveform) pero del lado del
motor, sin romper el layout de pistas.

Física: con dos TX grabando continuo, (1) los archivos de cada cadena son
contiguos muestra a muestra, y (2) el delta de reloj entre cadenas es
CONSTANTE. Medido en FCC: D por waveform WAV↔WAV = ±8 ms en 3 horas; D por
mediana de pares medidos = std 10-23 ms. Dos rutas independientes a ±6 ms.

    off_chain(TX_A) − off_chain(TX_B) = D_día   (off_chain = off − prefijo)

**`bin/enforce_dual_lav_consistency.py`** impone la ecuación: ancla = pata
medida físicamente (longwin/transcript/manual), la pata chrono se re-deriva
(`lavalier-derived-locked`, conf 0.90). FCC: 75 corregidos (0.1-1.8 s), 130
ya consistentes, 0 conflictos ambas-medidas.

Orden recomendado del flujo de sync multi-TX (actualiza al playbook §8):
  sync_transcript → derive_chrono_sync --per-chain → refine_pairs_longwin
  → enforce_dual_lav_consistency → verify_multicam --repair-chain
  --rescue-unverified → verify_sync_physical --apply-refinable → bake.

Lección de secuencia: refine_pairs_longwin DEBE correr antes del primer
bake que ve el usuario — el bake de FCC salió con offsets chrono crudos
(±0.3-1 s) y el usuario detectó el delay a ojo en las entrevistas 3-8.

**Tres refuerzos desde la copia de Adrián (The Shelter / Central de abastos, ago 2026).**

1. **Un match de UN SOLO canal es válido.** La corroboración cruzada entre lavaliers es
   *bonus*, no requisito. Exigir que ambos concuerden tira sync legítimo, porque los dos TX
   van en **personas distintas**, que pueden estar en lugares distintos captando cosas
   distintas. Instrucción literal del editor de ese proyecto: *"ahí teníamos el lava en un
   wey en backstage, pero estaban tocando en el escenario otros weyes. Sí está bien alineado
   éste, pero te lo digo para que lo tomes en cuenta. Puede que ambos lavaliers no
   concuerden, pero uno sí y el otro no."* Al quitar ese requisito hay que **reemplazar** el
   discriminante, no solo relajarlo: el sustituto es el reloj real (`mtime − duración`), que
   es corroboración física e independiente del contenido acústico. Tolerancia práctica
   medida allí: ±120 s, porque el `mtime` es fin-de-archivo y tiene ruido.

2. **El delta entre cadenas se mide antes, no después.** Correlacionar los envelopes de las
   dos cadenas **entre sí** (no contra el video) dio `delta(izq − der) = +0.068 s` con
   **MAD = 0.005 s sobre 71 clips** — 5 ms de dispersión por caminos independientes, que
   confirma desde otro material los ±8 ms medidos aquí en FCC. Sirve para dos cosas:
   verificar que `off_izq − off_der` de CADA clip reproduce el delta (allí: 0 pares fuera de
   tolerancia, el sync se auto-verifica), y **derivar con precisión de milisegundos** la
   cadena que no ancló. Rescató 40 pares que habrían caído a chrono. Son 4 correlaciones:
   medirlo primero convierte "una cadena falló" de problema en trámite.

3. **La medición del editor sobre el material real MANDA sobre la del algoritmo.** El delta
   de aquel proyecto NO lo dio la correlación de envelope (dio +0.070 y erraba 0.125 s); lo
   dio el editor midiendo el eco en Resolve: *"al recorrer el clip de audio en el track Audio
   3 +3 se corrige"* → D real = −0.055 s. Él puede oír un eco de 3 frames; la correlación
   sobre envelopes de 10 ms no siempre lo resuelve.

## Puente físico-físico multicám — la teoría del usuario (FCC 2026-07-11)

Teoría de Victor, literal: *"basado en la fecha de creación de los archivos,
calculando la diferencia de tiempo entre ellos comparado con el sync que ya
tenemos, podrías identificar todos los momentos en que estuvieron en
simultáneo las dos cámaras y los lavas"*. Es la arquitectura del sistema
(chrono-derivación + candidatos multicám por reloj corregido) llevada un
paso más lejos, y ese paso rescató 5 ángulos:

**Regla** (`verify_multicam_deltas.py --physical-bridge`): si AMBOS videos
de un par candidato tienen pata FÍSICA (longwin/transcript/dual-lav) al
mismo WAV, el puente `off_a − off_b` da el delta multicám a ±ms — sin
depender del envelope A1↔A1 (que la música engaña). La "misma escena" se
prueba por CONTENIDO: overlap de 4-gramas de transcript ≥ 5% (los
rescatados de FCC: 32-100%). Directa fuerte (prom ≥ 0.5) que contradiga el
puente por > 0.3 s = conflicto → NO colocar, reportar al humano.

**Jerarquía de señales que quedó**: reloj IDENTIFICA (simultaneidad,
candidatos — infalible para eso, pero granularidad 1 s del creation_time),
audio CLAVA (longwin/transcript ±ms), contenido CONFIRMA escena (4-gramas),
conflicto ESCALA al humano.

FCC: 53 → 58 pares verificados; recuperados los dos ángulos de la
entrevista 11 (0725+8630/8631) que ninguna otra ruta alcanzaba.

## Sync de evento musical con UN TX continuo (The Avalanches 2026-07-20)

Escenario: concierto/evento con música alta; lav único rotado entre
entrevistados; transcript del WAV 100% alucinado → sync por transcript
IMPOSIBLE. Lo que funcionó, en orden:

1. **Cadena del TX por mtime**: start = mtime − dur por archivo; validar
   continuidad (gaps ≤ 0.4s). Da la línea de tiempo maestra.
2. **Sonda global por clip** (`waveform_sync_full` adaptado, patrón = clip
   COMPLETO, no 60s del medio): los locks fuertes (prom ≥ 0.30) de varios
   clips coincidiendo en el MISMO skew a través de WAVs distintos = lock de
   cámara real (imposible por azar, aun con prom individual baja).
3. **Ventana local ±60s** alrededor de reloj+skew para re-medir el resto
   (la ventana < loop de la canción elimina alias de música repetida).
4. **A1 saturado (limitador, max ≈ 0 dB)**: el envelope RMS falla; la
   variante **onset novelty** (diff+ del log-RMS full-band) sí correlaciona
   (CAMAROGRAFO_2 C6852 prom 0.056→0.56). Probar voz/full/onset y quedarse con la
   mejor.
5. **Puente cámara↔cámara**: clip sin lock directo se ancla contra un clip
   MEDIDO de otra cámara que traslapa (corr A1↔A1, 3 variantes deben
   converger). Validación y rescate a la vez (C6853 vía C0122: la medición
   onset local estaba 0.7s corrida; el puente la corrigió).
6. **Gate anti-outlier**: medición aceptada solo si |skew − mediana de su
   cámara| ≤ 1.0s (un clip de 9s dio peak falso a 10s — rechazado).
7. Derivados = chrono + modelo LINEAL de skew por cámara (drift medido:
   CAMAROGRAFO_1 −0.18 s/h). Pares multicám de entrevistas = verified solo si ambos
   lados medidos; el contenido del transcript CONFIRMA la identidad del par.

Números AVA: 112 clips, 107 con sync (49 medidos, 58 derivados, 5 clips
≤2.5s sin par); skews Fx30 −7.49s, CAMAROGRAFO_1 −326.6s, CAMAROGRAFO_2 −60.94s.

**Dos añadidos de la copia de Adrián (The Shelter 2026-08-03).**

- **El filtro pasa-banda de voz (300-3400 Hz) es exactamente lo contrario de lo que conviene
  en material musical**: borra el bajo y el kick, que son la señal con más energía y mejor
  definida temporalmente de todo el material. Caso fundador: un clip daba prominence 0.03
  con filtro de voz y **0.25 sin filtro**, con ambos canales convergiendo en 0.07 s; el
  editor confirmó a oído que el sync era correcto. Encaja con el punto 4 de arriba (probar
  voz/full/onset): la novedad es que en concierto **full-spectrum debería ser el default**,
  no una de las tres variantes a probar.

- **Corolario de rendimiento, para barrer un proyecto entero**: cachear el envelope de cada
  audio UNA vez y correlacionar todos los videos contra esa caché. Cargar 1 h de WAV por
  cada par convierte un barrido de minutos en uno de horas. Ver "Correlación NORMALIZADA con
  envelopes cacheados", más abajo.

## Subtitular un export (BROD, The Avalanches 2026-07-21)

Herramienta: `bin/build_subtitles.py --video export.mov [--config project_config.json]
[--offset-tc <TC del IN del render>]`. Automatiza lo que la primera vez se hizo
a mano. Las tres trampas que Whisper NO resuelve solo:

1. **Arranque estirado**: si el video abre con música, Whisper estira el primer
   subtítulo desde 00:00 hasta el habla (25 s en BROD) y alucina "¡Suscríbete
   al canal!" encima. Detección: CPS < 3 en el primer cue. Corrección: probar
   ventanas de 4 s hasta hallar el onset real (±0.04 s medido en test).
2. **Alucinación de cola** sobre música sin voz ("Gracias por ver el video") →
   se recorta con `lib/transcript_quality.HALLUCINATION_PHRASES`.
3. **Garbles de nombres propios** ("Sinside F.U." = Since I Left You) → el
   script lee `vocabulary_notes` del project_config y los sustituye. Por eso
   vale la pena que la curaduría escriba los garbles ahí en formato
   "garble1/garble2 = Correcto".

Reglas de subtitulaje que el script aplica y Whisper ignora: frases COMPLETAS
(Whisper corta por nº de caracteres y parte "…por la pureza del / sampleo."),
≤42 car/línea, ≤2 líneas, CPS ≤17, 1–6 s, gap-out 0.4 s (no colgar el texto
durante una pausa) y candado anti-solape.

### Colocarlo al frame en Resolve
La API libre NO crea subtítulos por script y el drag&drop los suelta donde cae
el mouse, NO en su timecode (por eso la 1ª vez quedaron 7 frames corridos tras
pelear con el nudge). Método exacto: importar el SRT → arrastrar a la pista ST1
→ seleccionar todos (Timeline → Select Clips → Forward on Track) → ⌘X → playhead
al TC que imprime el script → ⌘V. Pega alineado al playhead.
**Trampa de foco**: tras teclear un TC en el visor, el campo se queda con el
foco y se traga las teclas — incluido el `.` del nudge. Escape o clic en la
timeline antes de cualquier atajo.

## Música en vivo: el de-loop de transcripts (Morsa, 2026-08-02)

Concierto de una banda tributo con vox pop entre el público. Whisper colapsó
sobre la música: bucles de invención pura — "La ciudad de México" ×N,
"¡Suscríbete al canal!" ×N, "¡Eso, cabrame!" ×N. Medido por n-gramas repetidos:
la hora 13:31–14:31 quedó en **0 % y 2 % de contenido limpio en las dos cadenas
de lavalier a la vez**. Nivel del audio en esa ventana: −9.8 dB de media con
picos a 0 dB, contra −26 dB en la ventana de entrevistas. No era un fallo de
transcripción: era el show sonando encima del micrófono.

**Por qué `lib/transcript_quality` no bastó.** Ese módulo está pensado para la
alucinación que arranca en un punto y sigue hasta el final: devuelve
`clean_until_idx` = PRIMER índice tóxico, y downstream usa solo `0..idx`. Cuando
lo limpio y lo tóxico van INTERCALADOS —la música va y viene— cortar en la
primera ráfaga tira tramos buenos. Medido en Izq/00013: cortar ahí deja 108 s de
1584 s útiles; de-loopear deja los cinco tramos.

**La técnica que funcionó.** Marcar toda palabra atrapada en un n-grama de 8
palabras que se repite ≥4 veces, y borrar solo esas. El habla real casi nunca
repite ocho palabras seguidas cuatro veces; un bucle de decoder siempre. Más una
lista de muletillas que Whisper emite sobre silencio ("Gracias por ver el
video") para vaciar los clips que son solo eso. Resultado en Morsa: 400
transcripts revisados, 216 modificados, **204 vaciados por ser 100 % invención**,
48 % de las palabras eliminadas. Quedaron 190 clips con habla real.

**VAD sí funciona, pero no para un pase completo.** `whisper-cli` soporta
`--vad --vad-model` con silero (el modelo ya está en `~/cinema-assistant/models/`).
Probado contra la hora destruida: elimina el 100 % del bucle. Probado contra la
entrevista limpia: **reproduce el habla real palabra por palabra** — por eso
sirve de referencia para validar el de-loop. El problema es el costo: corre en
CPU a ~1× tiempo real contra los ~13× del pase normal. Las 5 h 10 m de lavalier
serían ~5 h de proceso. **Usarlo como rescate dirigido, no como pase completo**;
para el pase, de-loopear.

**Consecuencia editorial que hay que decir en voz alta:** una hora sin habla en
los lavalieres NO es una hora perdida. En un concierto es el show, y el video de
esa ventana es cobertura. El transcript crudo mentía diciendo que había texto;
el limpio dice la verdad, que es que ahí no hay habla.

## Dependencias de orden que el playbook no declaraba (Morsa, 2026-08-02)

Cuatro pasos abortan si se corren en el orden "natural" del playbook. Ninguna es
un bug: son prerrequisitos reales que conviene tener escritos.

1. **`derive_chrono_sync` va DESPUÉS de `sync_transcript`**, no antes. Deriva el
   skew de cada cámara *a partir de* los pares de transcript de alta confianza y
   sale con error si no existe la tabla `audio_sync_pairs`.
2. **`sync_siblings_rule` necesita `init_sync_schema`** (columna
   `audio_sync_pairs.identity_score`).
3. **`verify_lav_offsets` necesita `clip_descriptions`**, o sea
   `derive_video_categories --project-prefix ''` corrido antes.
4. **`curate_segments` necesita tres tablas previas**: `clip_shot_values`
   (`derive_shot_value`), `clip_camera_angle` (`derive_camera_angle`) y
   `content_segments` (`derive_content_segments`). Sin ellas revienta con
   "no such table" y `clip_curated_segments` queda vacía — y el bake sale con
   `curated: 0` sin avisar de nada raro.

Y una más: **`enrich_curated_segments` espera `cast.json` con las claves de
primer nivel `cast` / `interviewers` / `protagonist`** (ver `lib/cast.py`). Un
cast.json documental pero con otra forma hace que el enriquecimiento corra sin
atribuir a nadie, avisando solo con una línea fácil de pasar por alto.

## Leer una timeline de Resolve sin abrir Resolve (Morsa, 2026-08-19)

La base de disco de Resolve guarda cada proyecto en un `Project.db` que es
**SQLite corriente**. `lib/timeline_resolve.py` lo abre en sólo lectura y
devuelve fps, resolución, pistas, items y cortes con precisión de fotograma.
Sirve para lo que el motor no podía hacer: leer las decisiones del editor.

La ruta se resuelve sola, sin escribirla a mano:

    ~/Library/Preferences/Blackmagic Design/DaVinci Resolve/activedb.conf
        -> "disk*:VICG"                       (base activa)
    .../dblist.conf
        -> "VICG:$HOME/Movies/Resolve Project Library:*:::DISK"
    <ruta>/Resolve Projects/Users/guest/Projects/<Proyecto>/Project.db

Sólo bases de **DISCO**: la línea de `dblist.conf` termina en `DISK`. Si el
proyecto vive en PostgreSQL, este camino no existe y el lector lo dice.

**Tres cosas que cuestan una tarde si nadie las escribe:**

1. Hay una tabla `Sm2Sequence_Sm2TiTrack` con toda la pinta de ser la unión
   secuencia→pista. Está **vacía**: 0 filas en Morsa, con 243 pistas reales. La
   buena es `Sm2SequenceContainer_Sm2TiTrack`. Consultar la equivocada **no da
   error**: devuelve cero filas, que se lee como «esta timeline no tiene
   pistas» — el mismo patrón de la lección 40.
2. En `Sm2Sequence`, el `FrameRate` es un double **little-endian** y la
   `Resolution` dos int64 **big-endian**. En el mismo registro. Leerlos igual da
   23.976 y una resolución de 4222124650659840.
3. `Start` y `Duration` de `Sm2TiItem` son TEXTO y pueden traer fracción de
   fotograma: `92956|00a0246afd9de83f`, donde lo de después de la barra es un
   double LE en hexadecimal (el audio se coloca con precisión de muestra, no de
   fotograma). `int()` revienta y `CAST(... AS INTEGER)` de SQL tira la fracción
   **en silencio**: siete items de Morsa desaparecían del recuento.

   Comprobación bonita de que el parseo es correcto: en un item cortado, la
   fracción del `Start` (0.769286) y la del `Duration` (0.230714) **suman
   exactamente 1.0**. El corte cae en fotograma entero aunque ninguno de los dos
   números lo sea.

**Se copia el .db a un temporal y se abre la copia.** No se usa
`immutable=1` sobre el original: esa bandera le promete a SQLite que el archivo
no cambia, y si Resolve escribe a mitad de la lectura el resultado es corrupción
silenciosa. La copia cuesta milisegundos y garantiza que no se toca el archivo
del editor.

**Resolve sólo vuelca a disco al GUARDAR.** Con el proyecto abierto y cambios
sin guardar, lo que se lee va por detrás de lo que el editor ve. `Timeline.avisos()`
compara el `ModTimeInSecs` de la timeline con el `mtime` del archivo y lo dice.

Lo que **no** está ahí: los settings de proyecto. Viven en `SM_Config.SetupBA`,
un struct binario de 2704 bytes sin nombres de campo. Para eso está
`resolve/auditar_settings.lua`, que los pide por la API.

## El imán de subtítulos: el editor ya lo hacía a mano (Morsa, 2026-08-19)

Antes de programar una regla, conviene mirar si el editor ya la aplica. En la
timeline `Reels verticales`, en el rango del Reel 3, subtitulado a mano:

- **9 de los 10 cortes sincrónicos tienen un borde de subtítulo EXACTAMENTE
  encima** (0 fotogramas de diferencia).
- De los **31 cortes de solo imagen, sólo 10** lo tienen.
- Al revés: **el 33 % de los bordes de subtítulo cae en el fotograma exacto de
  un corte, contra un 2 % de azar** (16×).

Eso convirtió una intuición —*«los cortes te dan la pauta ideal, a menos que sea
un voice over»*— en dos reglas medibles: prohibir que un cue cruce un corte
sincrónico, e **imantar** al fotograma del corte los bordes que ya caían a menos
de 10 fotogramas. Ver `metodologia/reels-y-cortes.md`, actualización v0.7.0.

El resultado sobre el mismo transcript del máster de Morsa:

| | sin cortes | con cortes |
|---|---:|---:|
| cues que cruzan un corte sincrónico | 31 | **23** |
| bordes pegados a un corte (±2 f) | 6/218 (3 %) | **37/228 (16 %)** |
| cues por encima de 17 CPS | 30 (máx 21.9) | **26 (máx 21.6)** |

Los 23 que siguen cruzando son los que no se podían partir sin romper otra
regla. El script los cuenta y sugiere el `--max-cruces` con el que verificarlos:
**declararlos no es esconderlos**.

## Medir el reloj de una cámara sin audio externo: n-gramas únicos (CLIENTE_1 día 3, 2026-08-23)

El día 3 repitió el problema del día 2 —la Sony con el reloj ~12 h adelantado— pero sin ningún WAV
con el que sincronizar. El método que lo resolvió, y que sirve para cualquier proyecto de dos cámaras
sin audio externo:

1. Barrer **n-gramas de 4 palabras** entre los transcripts de las dos cámaras.
2. Quedarse **sólo con los n-gramas únicos en toda la segunda cámara**. Sin este filtro el texto
   guionizado repetido empareja cualquier intento con cualquier otro.
3. Por cada coincidencia: `skew = (creation_time_A + t_palabra_A) − (creation_time_B + t_palabra_B)`.
4. Aceptar sólo si **dos anclas independientes y separadas en el tiempo** convergen.

Salieron dos: 17 n-gramas en un par y 3 en otro, separados 17 minutos, con medianas a **1.21 s**
(42648.70 y 42649.91). Valor adoptado: **42649 s**.

**Las cuatro comprobaciones que lo cerraron**, y ninguna sobra:

- **La indicación improvisada.** El ancla buena no fue el texto guionizado sino la corrección de
  dirección que se coló en los dos micrófonos («…tenemos que hablar siempre hacia ella / siempre para
  acá»). Un texto de prompter se repite en cada intento; una frase improvisada no. **Cuando hay guion,
  busca el ancla en lo que NO estaba escrito.**
- **El ritmo de los relojes.** El intervalo entre las dos anclas medido con un reloj (1033.72 s) y con
  el otro (1032.76 s) difiere 0.96 s en 17 minutos. Si el emparejamiento fuera falso, no tendrían por
  qué coincidir.
- **La deriva contra el día anterior.** El día 2 midió 42658 s para la misma cámara; dos días después,
  42649. Son 9.2 s, ~4.6 s/día: deriva normal de un cuarzo. Un salto grande habría delatado un error.
- **El waveform, por una vía distinta.** `export_multicam_lua.py` encontró después el mismo par
  (C1303 + DJI_0026) midiendo **A1↔A1 por envolvente**, con residuo de **0.26 s** tras aplicar el
  skew. Si el número estuviera mal por más de unos segundos no habría traslape que medir.

**Heredar el skew del día anterior habría fallado por 9 s.** Es poco para ordenar una cronología y
mucho para decir que se midió. La hipótesis sirve para saber dónde buscar, no para escribirse.

### Los falsos positivos que hay que descartar a mano

- **El mismo texto en otra toma.** C1301 y C1302 casaban con el mismo clip de la Osmo dando skews de
  42734 y 42932: son otros intentos de la misma cápsula. En un rodaje con guion, el texto repetido
  **no identifica el momento** — es la trampa central del método.
- **Las alucinaciones de Whisper.** Dos clips casaron por «Gracias por ver el video» y dieron 38739 y
  46700. Basura que empareja con basura; se ve porque los valores caen lejísimos de los buenos.

---

# Patrones traídos de la copia de Adrián (2026-08-28)

De su bifurcación del motor sobre The Shelter, FILM CLUB CAFÉ (la sesión del 10 de agosto
del mismo proyecto de las lecciones FCC de julio) y Central de abastos. Los tres que siguen
no tenían equivalente aquí. Las herramientas que los implementan son suyas y
**no están en este motor**; lo que se guarda es el método.

## Correlación NORMALIZADA con envelopes cacheados (Adrián, Central de abastos 2026-08-27)

Resolvió el sync de un mercado —ambiente denso, dos FX30, dos lavaliers continuos de
~2h45m—. Sustituye a `refine_offset` cuando el material es ruidoso, y es **más rápido**, no
más lento.

**1. Cachear el envelope del audio UNA vez por archivo.**

```python
pcm = load_pcm(wav)                              # 4 kHz mono, full-spectrum
env = np.log1p(np.sqrt((x*x).mean(1)) * 100)     # log-RMS 100 Hz, vectorizado
np.save(cache / f"a{clip_id}.npy", env)
```

8 WAV de 1 h → 8 s en total. Después, cada clip se correlaciona contra el envelope ya en
RAM: **336 mediciones en 22 s**. Cargar 1 h de WAV por cada par convierte un barrido de
minutos en uno de horas.

**2. Correlación NORMALIZADA (Pearson deslizante), no `correlate` cruda.**

```python
num = fftconvolve(sig, p[::-1], mode="valid")    # p = patron zero-mean / unit-norm
cs, cs2 = cumsum(sig), cumsum(sig**2)            # sumas deslizantes
den = sqrt(win_sq - win_sum**2 / m)
ncc = num / den                                  # valores en [-1, 1]
```

La diferencia importa: **la correlación cruda premia las zonas de más energía del WAV**, así
que en material con música o ambiente fuerte el pico se va donde suena más, no donde
coincide. La NCC da un coeficiente comparable en `[-1,1]` y una **prominencia honesta**
(pico contra el mejor competidor fuera de ±3 s) sobre el archivo entero.

**3. Dos etapas: global para el modelo, ventana para la precisión.**

- Etapa 1: búsqueda global sobre el WAV completo; los picos fuertes (peak ≥ 0.50,
  prom ≥ 0.15) alimentan el modelo de relojes.
- Etapa 2: búsqueda en ±6 s alrededor de la predicción del modelo, con la prominencia
  medida contra el vecindario ±25 s. Con el modelo ajustado la predicción entra a ±1 s, así
  que el pico de la ventana es casi con certeza el verdadero.

**Resultado**: prominencia ≥ 0.15 → **100 % de las mediciones dentro de 1 s del modelo de
reloj** (dos métodos independientes confirmándose). Residuo tras aplicar: **−0.08 frames** a
23.976.

## Modelo de relojes de dos factores (Adrián, Central de abastos 2026-08-27)

Con N cámaras y M archivos de audio, el desfase de cada medición se descompone en dos
constantes:

```
drift(clip, wav) = sesgo_camara − delta_wav
```

- `sesgo_camara`: cuánto va corrido el reloj de ESA cámara (medido: A = 0, V = +52.33 s).
- `delta_wav`: cuánto va corrido el `mtime` de ESE archivo (±1.4 s de redondeo del sistema
  de archivos).

Se ajusta con **medianas alternantes** —robusto a falsos positivos—, fijando una cámara en 0
por identificabilidad:

```python
for _ in range(30):
    for aid in audios:  d[aid] = median(b[cam] - drift for votos de ese audio)
    for cam in camaras: b[cam] = median(drift + d[audio] for votos de esa camara)
    b["V"] -= b["A"]; b["A"] = 0.0
```

Sirve para dos cosas a la vez: **predecir** dónde buscar (la etapa 2 del patrón anterior) y
**discriminar** falsos positivos por consenso. Residual p90 = 0.77 s, que es exactamente el
truncado a 1 s de `creation_time`: el modelo llegó al límite de lo que el metadato permite.

Encaja con el modelo lineal de skew por cámara que ya usamos desde AVA, pero resuelve algo
que aquél no: separa el error del **archivo de audio** del error de la **cámara**, en vez de
meter los dos en el mismo skew.

## Leer un dual-lav: fundir las dos cadenas en hora real (Adrián, Central de abastos 2026-08-27)

Para valorar de qué habla un material con dos lavaliers continuos, leer los dos transcripts
por separado es inmanejable y redundante: cuando los micros van juntos capturan la misma
conversación dos veces.

1. **Agrupar palabras en frases por silencios** (`gap` de 1.2-1.5 s). El transcript viene
   como `words = [[palabra, t], ...]`; sin agrupar no hay unidades legibles.
2. **Filtrar bucles de alucinación**: descartar frases repetidas consecutivas y los hints
   conocidos ("Gracias por ver el video" ×1533 en un archivo de ese proyecto — 7929 palabras
   con solo 153 distintas). La señal barata es `distintas / totales`, que aquí ya usamos
   como `garbage_ratio`.
3. **Poner cada frase en HORA REAL** con `audio_time_base.t0 + t`. Así el contenido queda
   cruzado con los bloques de rodaje y con qué cámara estaba grabando.
4. **Fundir cadenas**: si dos intervenciones de cadenas distintas caen a < 8 s y comparten
   > 55 % de tokens normalizados, es la misma conversación oída dos veces — se queda la
   versión más larga, marcada `AMBOS`. Donde difieren, se conservan las dos: los micros iban
   en personas distintas.

Resultado: 42 667 palabras crudas → **847 intervenciones legibles**. Suficiente para leer el
rodaje entero de corrido y sacar los hilos.

**El hallazgo que no esperaba, y que es el que más vale**: las conversaciones de
**producción** quedan grabadas en los lavaliers. En ese proyecto el equipo discute al aire
que caminando solo graban la nuca del entrevistado, y más tarde dicta la gramática del
montaje ("el clip padre es este güey disparando y luego ves la foto"). **Eso es material, no
ruido — buscarlo siempre.**
