# Playbook — proyecto nuevo

El orden correcto. Cada paso tiene su doc para los detalles.

## 0a. Consultar memoria creativa transversal (siempre)

> **Puerta del acta de conducta:** `[A1]` -- ver `metodologia/pruebas-del-asistente.md`.

Antes de cualquier otra cosa, leer **dos archivos** en este orden:

```bash
# 0. Foco actual (hot cache — continuidad de sesión):
cat ~/memoria-creativa/_foco.md

# 1. Síntesis del autor (siempre — define cómo trabaja Victor):
cat ~/memoria-creativa/Autor/Autor-Biografia/Autor-Sintesis.md

# 2. Registro de proyectos (para identificar el proyecto activo):
cat ~/memoria-creativa/_registro.json
```

*(Rutas actualizadas 2026-07-17 — lección 49: la reorganización de
memoria-creativa movió síntesis y registro; las rutas viejas
`autor/biografia/` y `proyectos/_registro.json` ya no existen.)*

Si el proyecto está en el registro, abrir su ficha y su archivo de desarrollo si existe. Si no está, no crear aquí — proponer vía `~/memoria-creativa/_cambios/pendientes/`, que hoy se hace solo con el exportador del **paso 13b**.

Las **entidades** (personajes, lugares, temas, colaboradores) **no viven en un directorio central**. Viven dentro de la carpeta de cada proyecto, con el prefijo del proyecto:

```
Diez50/.../Diez50-Victor-Proyectos/EDLP/EDLP-Personajes/EDLP-Roman/EDLP-Roman-ficha.md
Diez50/.../Diez50-Victor-Proyectos/Tales/Tales-Amapola-ficha.md
Autor/Autor-Obra/Centro/HDUHSP/HDUHSP-Personajes/HDUHSP-Magnolia/HDUHSP-Magnolia-bio.md
```

Para saber si una entidad ya existe, busca en el índice, no en un directorio:

```bash
python3 ~/memoria-creativa/ingesta/indice-fts.py buscar "<nombre>"
```

*(Corregido 2026-07-28: este paso mandaba a `~/memoria-creativa/entidades/`, un directorio que nunca existió. La auditoría de esa fecha lo detectó junto con ~40 personas identificadas que no tenían dónde aterrizar. **Ojo con las personas reales:** las coberturas registran nombre y rol dentro de la ficha del proyecto; no se crean fichas-perfil de personas reales sin decisión previa de Victor — es una cuestión ética abierta desde mayo de 2026.)*

Doctrina completa: [`~/memoria-creativa/README.md`](../../memoria-creativa/README.md).

## 0. Pre-requisitos del entorno

> **Puerta del acta de conducta:** `[A3]` -- ver `metodologia/pruebas-del-asistente.md`.

Una sola vez por máquina:

```bash
brew install ffmpeg mediainfo exiftool whisper-cpp chromaprint
pip3 install --user numpy Pillow zstandard
```

Descargar el modelo Whisper turbo (1.5 GB) a `~/cinema-assistant/models/`:

```bash
mkdir -p ~/cinema-assistant/models
curl -L --fail -o ~/cinema-assistant/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

## 1. Conectar el disco y resolver el volumen

> **Puerta del acta de conducta:** `[A2]` `[A4]` -- ver `metodologia/pruebas-del-asistente.md`.

```bash
DISK=$(ls -d /Volumes/<NOMBRE_DEL_DISCO>*/ 2>/dev/null | head -1)
```

El **glob** es obligatorio: si el nombre del volumen tiene caracteres
invisibles (en este proyecto el de ESCALANDO MEXICO tenía un U+F029),
`ls /Volumes/<nombre>/` falla y `cd` también.

## 2. Indexar el proyecto

```bash
python3 ~/cinema-assistant/bin/index_project.py --root "$DISK"
```

Recorre el árbol del disco, prueba cada archivo con ffprobe/mediainfo y
crea/actualiza `manifest.sqlite` en `<disco>/.cinema_assistant/`. Cubre
video y audio (path, rel_path, duración, codec, resolución, fps,
has_audio, camera_make/model, creation_time, timecode, sha256_partial).

**Nunca toca las fuentes.** Solo lee + indexa.

## 3. Inventario de cámaras

Ver `camaras.md`. Detectar **por ubicación** qué cámaras se usaron y
qué rol jugó cada una:

- **Principal(es)** — DSLR/cine, A-roll y entrevistas. Audio dual-system
  aplica aquí.
- **Acción** — GoPro, POV, embedded audio.
- **Drone** — DJI, aéreo.

**No asumir una sola principal.** ESCALANDO MEXICO tenía dos (Canon EOS
6D + Blackmagic) y al principio se asumió solo Canon.

## 4. Indexar audio externo

Si existe `AUDIOS/`:

```bash
python3 ~/cinema-assistant/bin/index_audios.py "$DISK"
```

Indexa el árbol entero de audio externo (entrevistas, ambientes, room
tone, efectos). Ver `catalogo-audio.md`.

## 5. **TRANSCRIBIR PRIMERO** (regla del usuario, Zezzions 2026-05-27)

> **Puerta del acta de conducta:** `[B3]` -- ver `metodologia/pruebas-del-asistente.md`.

**Instrucción literal del usuario** (cierre iter9.6):
> *"El transcript debería de ser una de las primeras cosas que hagas en
> todo el proceso de asistencia de edición a lo largo de todos los
> proyectos."*
>
> *"El transcript debe hacerse combinando el audio de la cámara con los
> audios externos. Es muy importante que revises el transcript y lo
> corrijas los errores a partir del contexto que tienes de todo el
> proyecto."*

**El transcript NO es un paso único — es un PROCESO ITERATIVO de capas
que se enriquecen con el contexto del proyecto**:

### Capa 1: transcripts individuales (raw Whisper)

```bash
# A1 de cámaras principales (audio scratch)
python3 ~/cinema-assistant/bin/transcribe_clips.py --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin --cameras main

# Audios externos (lavaliers, recorder)
python3 ~/cinema-assistant/bin/transcribe_audios.py --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin

# Audit de calidad (alucinaciones, clean_until_sec)
python3 ~/cinema-assistant/bin/audit_transcripts.py --root "$DISK"
```

### Capa 2: corrección contextual con vocabulario del proyecto

A medida que el proyecto avanza, descubrimos:
- Cast del proyecto (face_catalog + voice_catalog)
- Términos del dominio (música, deporte, cocina, etc.)
- Mexicanismos y argot

**Aplicar `bin/correct_transcripts_vocab.py` MÚLTIPLES VECES** según
nuevo vocabulario descubierto:

```bash
# Capa 2a (después de detección inicial de cast):
python3 ~/cinema-assistant/bin/correct_transcripts_vocab.py --root "$DISK"

# Capa 2b (después de cast confirmado por el usuario):
# Editar <disk>/.cinema_assistant/cast.json con nombres canónicos
# Re-correr correct_transcripts_vocab.py
```

### Capa 2c: CURADURÍA REAL por comprensión (obligatoria, FCC 2026-07-11)

**Instrucción del usuario**: *"falta un proceso de verdadera curaduría del
transcript para verificar que no haya alucinaciones."*

El audit estadístico (capa 1) caza la alucinación MASIVA ("Suscríbete al
canal" ×N); la corrección por vocabulario (capa 2a/2b) arregla variantes
conocidas. Lo que ninguno caza: el **garble plausible** — texto que parece
válido pero es alucinación local de Whisper, típicamente en NOMBRES
PROPIOS: "Satriacit Drive" (Satyajit Ray), "Regin Bull" (Raging Bull),
"la plodería" (la curaduría), "el P.O.P. / el peón" (nombre del club).
Eso solo lo caza la COMPRENSIÓN. Proceso:

```bash
# 1. Sacar los sospechosos a un solo reporte legible:
python3 ~/cinema-assistant/bin/surface_transcript_suspects.py --root "$DISK"
#    → reports/curaduria-transcript-<fecha>.md con: preguntas sin curar,
#      auto-IDs del cast, tokens capitalizados raros CON CONTEXTO, recap
#      del audit estadístico.
```

2. **Claude LEE el reporte completo** y cura por comprensión:
   - `question_segments.question_short` = cada pregunta clara y sintética,
     des-garblada (los markers Purple la usan — política del usuario).
   - Nombres del cast validados/corregidos → `cast.json` + propuesta al
     usuario de los dudosos.
   - Términos del proyecto detectados → `vocabulary_hints` del
     project_config + re-correr `correct_transcripts_vocab.py`.
3. **Candado**: `export_lua_data.py` avisa "⚠⚠ N preguntas SIN CURAR" si
   hay `question_short` vacíos — no se entrega un bake con ese aviso.
4. Registrar las correcciones no-obvias en el reporte (qué se corrigió y
   por qué) — es evidencia de curaduría, no promesa.

### Capa 3: master transcripts (post-sync)

Una vez que hay sync (paso 8), combinar A1+lavaliers por entrevista:

```bash
python3 ~/cinema-assistant/bin/build_master_transcripts.py --root "$DISK" \
  --only-interviews
```

**IMPORTANTE**: el master NO concatena texto — preserva `by_source` para
detección de preguntas independiente por fuente (sino mezcla voces y
genera basura).

### Capa 4: derivar entidades del proyecto desde el corpus completo

Con todos los transcripts (individuales + masters) limpios:
- `derive_question_segments.py` → Purple Q markers
- `derive_video_categories.py` → categorización refinada (entrevista,
  charla-equipo, accion-dialogo)
- `attribute_faces_via_transcript.py` → identidad por auto-ID
- `derive_characters.py` → cast definitivo del proyecto

### Por qué este orden funciona

El transcript es la señal que retroalimenta TODOS los demás procesos
del asistente:
- Sync por transcript (anchors de n-gramas, robusto)
- Detección de entrevistas (densidad léxica + filtro alucinaciones)
- Master transcripts (A1+lavaliers por source)
- Question detection (Purple Q)
- Curated full_text (Green Tramos)
- Vocabulario del proyecto (cast + términos)
- Identidad de speakers (face_voice_links + clip_characters)
- Verificación de sync físico (anchors únicos)

Sin transcript completo y CORREGIDO con contexto del proyecto, el resto
del pipeline navega a ciegas. ANTES del análisis técnico de imagen,
ANTES del catálogo audio detallado, ANTES de cualquier sync — TRANSCRIBE
y CORRIGE iterativamente.

```bash
# A1 de cámaras principales (audio scratch tiene diálogo):
python3 ~/cinema-assistant/bin/transcribe_clips.py \
  --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin \
  --cameras main

# Audio externo (lavaliers, recorder de campo):
python3 ~/cinema-assistant/bin/transcribe_audios.py \
  --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin

# Audit calidad de transcripts (filtra alucinaciones):
python3 ~/cinema-assistant/bin/audit_transcripts.py --root "$DISK"
```

~14× tiempo real en Apple Silicon. Cache en
`<disco>/.cinema_assistant/transcripts/<clip_id>.json`. Ver
`transcripcion.md`.

## 6. Análisis técnico de clips

> **Puerta del acta de conducta:** `[B2]` -- ver `metodologia/pruebas-del-asistente.md`.

(Lento. Correr en background MIENTRAS los transcripts están listos.)

```bash
python3 ~/cinema-assistant/bin/analyze_clips.py --root "$DISK"
python3 ~/cinema-assistant/bin/analyze_segments.py --root "$DISK"
```

Produce `clip_analysis` (exposure / motion / audio peak / silencio /
clipping por clip) y `clip_segments` (tramos usables multi-señal —
los duration markers verdes).

`analyze_segments.py` procesa **todo el material** bajo `ESCALANDO
MEXICO/` por default. Para iterar sobre un sector específico (sin tocar
el resto), pasarle `--sector NOMBRE` — re-procesa solo ese sector y
preserva lo demás en `clip_segments`. Historial: este script estuvo
hardcodeado a JILOTEPEC y dejó al resto del proyecto sin tramos
curados; el fix vive desde 2026-05-25.

## 6b. Transcripción redundante / legacy

Cámaras principales (audio scratch tiene el diálogo):

```bash
python3 ~/cinema-assistant/bin/transcribe_clips.py \
  --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin \
  --location <substring_ubicacion> \
  --cameras main
```

Audio externo (mixes y nombrados, salta `_Tr` duplicadas):

```bash
python3 ~/cinema-assistant/bin/transcribe_audios.py \
  --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin
```

~14× tiempo real en Apple Silicon. Cache en
`<disco>/.cinema_assistant/transcripts/<clip_id>.json`. Ver
`transcripcion.md`.

## 6c. **Rescate de entrevistas con A1 alucinado** (iter9.7 2026-05-27)

Si un clip tiene transcript del A1 alucinado (música ambient + Whisper)
pero **señal visual fuerte** (>=20 face_detections + dur >=120s),
NO abandonarlo. Probable que el A1 esté contaminado pero los lavaliers
del entrevistado/entrevistador sean limpios.

```bash
# Auto-rescue: identifica clips alucinados con caras + sugiere audios candidatos
python3 ~/cinema-assistant/bin/verify_interviews.py --root "$DISK"

# Para cada candidato reportado, probar waveform A1↔audios externos sin sync:
python3 ~/cinema-assistant/bin/apply_waveform_sync.py --root "$DISK" \
    --pair-ids <ids> --dry-run

# Si match (prominence >=0.30): aplicar sync, re-transcribir lavalier con
# contexto, build master, re-derive questions, re-categorizar como 'entrevista'.
```

**Caso fundador**: Zezzions clip 2644 — entrevista de 11:29 min con 3
personas que se había perdido por A1 alucinado. Dr/00004 e Izq/00006
(hermanos sin sync) sincronizaron con offset +74.4s y prom 0.61. Después
del rescate: 6 preguntas reales detectadas, clip recategorizado.

## 7. Detectar entrevistas

Tres señales (ver `entrevistas.md`), de más débil a más fuerte:
1. Carpeta llamada `entrevista` / `visita nata` / etc.
2. Heurística: cámara principal + ≥ 60 s + motion < 8.
3. **Transcript denso** (≥ 60 palabras distintas, poca repetición) +
   **PASA el filtro de alucinaciones** de `lib/transcript_quality`.
   La densidad sola no basta — música alta encima de diálogo hace que
   Whisper produzca alucinaciones masivas que parecen densas (Zezzions
   2026-05-25). Descartar los `category='entrevista-degradada'` antes
   de derivar preguntas, sync o descripciones LLM.

## 8. Sincronizar — **transcript PRIMERO, waveform como verificación**

> **Puerta del acta de conducta:** `[B1]` `[B4]` `[B5]` -- ver `metodologia/pruebas-del-asistente.md`.

**Orden correcto** (doctrina del usuario, iter9.6 2026-05-27):

```
1. transcript-based sync (anchors únicos de n-gramas)
   → produce offset físico inicial
2. waveform sync (DaVinci o lib/waveform_sync_full.py)
   → CORROBORA el offset físico
3. Si convergen (±200ms): aceptar
4. Si divergen: usar transcript (más confiable para entrevistas con
   habla densa y limpia)
```

```bash
# Paso 1: sync por transcript
python3 ~/cinema-assistant/bin/sync_transcript.py --root "$DISK" \
  --min-confidence 0.5

# Paso 2: corroborar con waveform (sólo entrevistas con UN entrevistado)
python3 ~/cinema-assistant/bin/apply_waveform_sync.py --root "$DISK" \
  --dry-run  # primero ver qué propondría
```

**Reglas críticas**:
- Para entrevistas **DUALES** (≥2 personas en cuadro): waveform A1↔lavalier
  falla porque A1 mezcla voces. Usar transcript + sibling sync entre lavaliers.
- Para entrevistas **UNI** (1 entrevistado): waveform A1↔lavalier es el
  método primario más preciso.
- Para hermanos del mismo Wireless PRO RX: validar con 3 indicadores
  (mtime ≤120s + transcript overlap ≥0.20 + waveform sibling prom ≥0.30).

Verifica por contenido (n-gramas de palabras). Robusto: no puede
emparejar mal por accidente. Ver `sync.md`. Si quedan pocos pares,
asegurate de haber indexado y transcrito **todo** `AUDIOS/`.

### 8b. REGLA: ambos lavaliers SIEMPRE deben tener sync (Zezzions 2026-05-26)

**Instrucción literal del usuario**: *"Siempre debes de hacer sync de los
dos lavas tomando en cuenta el tiempo de la grabación, para evitar que
andemos batallando."*

Cuando un video tiene sync con UN lavalier (ej. 00006 Dr), el sistema
**debe intentar derivar sync del lavalier hermano** (00006 Izq, mismo
Wireless PRO RX) automáticamente:

```bash
$VENV ~/cinema-assistant/bin/sync_siblings_rule.py --root "$DISK"
```

**Salvaguardia obligatoria** (sino propaga falsos positivos):
- `overlap_4grama_transcript ≥ 0.10` entre los dos audios. Si NO comparten
  contenido, NO son hermanos verdaderos (es que el usuario rotó el
  lavalier a otra entrevista).
- Caso especial Zezzions: muchos lavaliers numerados igual (00006 Dr y
  Izq) capturaron contenidos distintos porque los lavaliers se rotaban
  entre entrevistas. La salvaguardia previene asignar audio incorrecto.

### 8c. Refinement sub-segundo final para eliminar delays residuales

```bash
$VENV ~/cinema-assistant/bin/refine_all_pairs.py --root "$DISK" \
  --min-prominence 0.15 --search-window 2.0
```

Procesa TODOS los pares (incluyendo `*-locked` excepto `manual`) buscando
delays residuales en ventana ±2s. Aplica solo si:
- prominence ≥ 0.15 (peak claro en banda voz)
- |Δ| ≥ 30ms (no aplica cambios despreciables)
- |Δ| ≤ 1500ms (cambios mayores son sospechosos, mantener original)

Esto resuelve los "delays" que el usuario reporta cuando el sync identitario
es correcto pero el offset numérico tiene jitter de timestamps Whisper o
envelope.

### 8d. Derivación cronológica para clips secuenciales con mismo audio

Cuando dos clips de video tienen el MISMO audio externo (ej. ENTREVISTADO_12
parte 1 y parte 2 con 00004 Izq), el offset del segundo se deriva del
primero usando creation_time:

```
gap_wall = creation_time_2 − creation_time_1
offset_2 = offset_1 − gap_wall
```

Validación Zezzions: corrigió 2597 ENTREVISTADO_12 parte 2 de -503.57 a -503.07
(delay exacto -504ms eliminado).

## 9. Catalogar el audio externo

(Para diseño de sonido.) Clasificar cada archivo: diálogo / ambiente
/ room tone / SFX / música por nombre + transcript + duración. Ver
`catalogo-audio.md`.

## 10. Descripciones, crónica y marcadores con contenido

(Ver `descripciones-cronica.md` y `marcadores.md`.)

1. Hojas de contacto etiquetadas: `bin/build_contact_sheets.py`.
2. Análisis visual (lectura por hoja).
3. Combinar con el transcript del clip + metadata → descripción de
   una oración.
4. Re-derivar duration markers en entrevistas usando los timestamps de
   palabras (qué dicen en cada tramo).
5. Compilar la crónica cronológica.

## 11. Hornear datos y aplicar en Resolve

> **Puerta del acta de conducta:** `[C3]` `[C4]` `[C5]` -- ver `metodologia/pruebas-del-asistente.md`.

**El horneado va en el disco del proyecto, no en el motor.** Regla de capas
(hallazgo 5.6, corregido 2026-07-27): los `*_data.lua` y `*_multicam.lua`
llevan rutas absolutas, nombres reales de entrevistados y transcripciones
literales — son DATOS por proyecto. El motor guarda CÓDIGO. Lo que sí se queda
en el motor es el script `asistente_<proyecto>.lua`, para que la ruta que
tecleas en la Consola no cambie de un proyecto a otro.

```bash
BAKE="$DISK/.cinema_assistant/resolve"
mkdir -p "$BAKE"
python3 ~/cinema-assistant/bin/export_lua_data.py \
  --root "$DISK" --out "$BAKE/<proyecto>_data.lua" --project-prefix ""
luac -p "$BAKE/<proyecto>_data.lua"
luac -p ~/cinema-assistant/resolve/asistente_<proyecto>.lua
```

En el `asistente_<proyecto>.lua`, `DATA_PATH` y `MULTICAM_PATH` apuntan al
disco. Si heredas un script viejo que apunta al motor, repúntalo con
`python3 ~/cinema-assistant/bin/migrar_horneados.py --aplicar`.
`tests/test_capas.py` falla si un horneado vuelve a aparecer en el motor.

En Resolve:

1. Abrir proyecto → Workspace → Console → Lua.
2. Pegar (double-click en el strip de input, ⌘+V):
   `dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_jilotepec.lua")`
3. Return.

El script borra los timelines viejos `ESC — *`, crea/usa la sub-bin
`Timelines/asistente de edicion`, reconstruye todo (timelines por
ubicación + `ESC — TODO EL MATERIAL` + `ESC — ENTREVISTAS`), pone
markers de duración / status / sync / descripción.

⌘+S al final para guardar.

## 11b. Curaduría manual de Claude para tramos clave

> **Puerta del acta de conducta:** `[C2]` -- ver `metodologia/pruebas-del-asistente.md`.

**Doctrina añadida 2026-05-26 desde Jilotepec.** El LLM local (Qwen2.5VL:3b)
genera descripciones de B-roll razonables pero **fallece en tramos
narrativos importantes** — entrevistas, momentos hito, conversaciones
densas. Para esos tramos el flujo correcto es **curaduría manual por
Claude** (este asistente), escribiendo `full_text` directamente con base
en el transcript + identidades + contexto del clip.

Patrón observado en Jilotepec (`/Volumes/MI_DISCO*/.cinema_assistant/`):
- `curated_by='llm'`: 2336 tramos descritos por Qwen2.5VL — descripciones
  útiles pero formulaicas para B-roll.
- `curated_by='claude'`: ~10 tramos descritos manualmente — descripciones
  ricas que combinan PERSONAJES + ACCIÓN + LUGAR + IDEA.

Formato estándar para `full_text` curado por Claude (extraído de Jilotepec):
```
{PERSONAJES} | {PLANO} + {ÁNGULO} | {ACCIÓN-RESUMEN}. {LUGAR-CONTEXTO}. Idea: '{FRASE CLAVE DEL TRAMO}'
```

Ejemplos reales (Jilotepec):
- `ESCALADOR_A | Plano Americano + Contrapicado | ESCALADOR_A escala Lujuria 14a con beta-calling intenso del grupo. Sector Lujuria, pared de conglomerado. Idea: '¡Bien rana, vamos ESCALADOR_A!'`
- `ESCALADOR_A, equipo | Plano Medio + Normal | Charla íntima de campamento; el grupo acompaña a ESCALADOR_A en un momento personal. Idea: '<frase clave del tramo>'`
- `ESCALADOR_E, ESCALADOR_F | Plano Americano + Contrapicado | Pegue nocturno con linternas frontales; equipo encendido. Idea: '¡Es el día cabrón, hazla tuya!'`

**Cuándo curar manualmente** (criterios):
- Todos los tramos en entrevistas con sync verificado (los Purple Q ya
  tienen pregunta+respuesta, pero el Green del clip completo o de B-roll
  cercano a la entrevista merece descripción rica).
- Hitos narrativos (`★` flag): momentos pivotales del proyecto.
- Tramos con personajes confirmados (`clip_characters` poblado con
  identidad auto-identificada).
- Material que el usuario marque como prioritario.

**Cómo hacerlo**:
1. Para cada tramo elegible, leer:
   - `clip_curated_segments`: rango (s, e), shot_value, angle, is_representative
   - `clips.filename`, `clips.duration_sec`
   - El transcript de ese rango: `transcripts/{clip_id}.json` filtrado a
     palabras con `s <= t <= e`
   - `clip_characters` para el clip
   - `question_segments` si cae en zona de pregunta
2. Escribir `full_text` siguiendo el formato anterior.
3. `UPDATE clip_curated_segments SET full_text=?, curated_by='claude'
   WHERE id=?` (preservar el tramo, sobreescribir solo `full_text`).
4. Re-bake `export_lua_data` para que los Green markers en Resolve muestren
   el texto curado.

**Cuándo NO curar manualmente** (dejar al LLM):
- B-roll de paisaje/establecedor sin personas.
- Clips < 30s sin transcript denso.
- Tramos repetitivos del mismo encuadre.

## 12. Iterar

Revisar el resultado en Resolve. Ajustar:
- Umbral de confianza de sync.
- Categorías de las descripciones.
- Heurística de entrevista.

Re-hornear y re-correr la Consola.

## 12b. Verificadores obligatorios — garantías, no promesas

> **Puerta del acta de conducta:** `[C1]` `[C6]` -- ver `metodologia/pruebas-del-asistente.md`.

**Doctrina añadida 2026-05-26 después de perder 2 entrevistas en Zezzions
(ENTREVISTADO_1 clip 2794, ENTREVISTADO_2 clip 2786).** El usuario exige
**garantías verificables, no promesas**. Los verifiers SON las garantías:
scripts que fallan si encuentran lo que no debería existir.

Antes de declarar el proyecto cerrado, correr OBLIGATORIAMENTE:

```bash
# 1. Entrevistas potenciales no detectadas — exit 1 si encuentra alguna
python3 ~/cinema-assistant/bin/verify_interviews.py \
    --root "$DISK" --fail-on-found

# 2. Cobertura global del pipeline (todos los sectores procesados)
python3 ~/cinema-assistant/bin/verify_coverage.py --root "$DISK"

# 3. Lint del pipeline (hardcodes peligrosos)
python3 ~/cinema-assistant/bin/lint_pipeline.py

# 4. Offsets de lavalier medidos o validados por la ecuación dual-lav
#    (FCC 2026-07-10: el usuario detectó A OJO offsets chrono crudos en
#    entrevistas — exit 1 si un bake saldría en ese estado)
python3 ~/cinema-assistant/bin/verify_lav_offsets.py --root "$DISK"

# 5. Todo par multicám verificado ATERRIZA alineado en la timeline
#    (FCC 2026-07-11: un par triple-verificado se perdió en silencio por
#    colisiones de colocación en timelines empacadas — simula la colocación
#    fuera de Resolve; exit 1 si un ángulo se perdería). Solo aplica si el
#    proyecto tiene <p>_multicam.lua.
python3 ~/cinema-assistant/bin/verify_multicam_placement.py --root "$DISK" \
    --lua ~/cinema-assistant/resolve/<proyecto>_multicam.lua

# 6. Curaduría de transcript completa (Capa 2c, FCC 2026-07-12): exit 1
#    si hay preguntas sin question_short curado. El reporte que genera es
#    ADEMÁS lo que Claude debe haber LEÍDO para cazar garble plausible
#    (nombres propios inventados que el audit estadístico no ve — lección 46).
python3 ~/cinema-assistant/bin/surface_transcript_suspects.py --root "$DISK"
```

Defensa en profundidad del punto 5: el propio `asistente_<p>.lua` lleva un
assert de cierre (contadores `MC_PLACED`/`MC_FAILED`) que imprime
"⚠⚠ MULTICAM INCOMPLETO" en la Consola de Resolve si algún par verificado
no se colocó — el fallo nunca vuelve a ser silencioso, ni siquiera si el
verifier no corrió.

**Regla de orden (FCC 2026-07-10)**: en proyectos con audio continuo
multi-TX, ANTES del primer bake que ve el usuario corre SIEMPRE:
`refine_pairs_longwin.py` (mide físicamente todo par medible) →
`enforce_dual_lav_consistency.py` (impone off_chainA − off_chainB = D) →
`verify_lav_offsets.py` (la garantía). El chrono derivado es para COBERTURA,
no es precisión de entrega.

### Caso fundador `verify_interviews.py`

Audita el manifest buscando clips que cumplen TODOS estos criterios:
- duración ≥ 60s + tiene audio
- transcript contiene ≥ 2 patrones de pregunta de entrevista
- SIN categoría 'entrevista' en `clip_descriptions`
- SIN par en `audio_sync_pairs`

Esos son **entrevistas potenciales perdidas**. Bug raíz: cuando Whisper
alucina masivamente sobre música del evento, el transcript queda
dominado por basura aunque las primeras 5 preguntas del entrevistador
sean reales. `analyze_transcript()` marcaba el transcript como degradado
por ratio de basura, y la categorización requería ≥ 60 palabras
distintivas limpias — eso no aplicaba a clips con 5 preguntas (~70-100
palabras limpias).

Patrón verificado empíricamente: el verifier detecta 2786 (ENTREVISTADO_2)
y 2794 (ENTREVISTADO_1) cuando se desmarcan, prueba que la garantía funciona.

**Actualización 2026-05-26 (Zezzions, post-Lua en Resolve)**: el usuario
encontró visualmente la entrevista 2645 (ENTREVISTADO_13 y ENTREVISTADO_10, dual-lavalier 322s)
en Resolve, sin sync ni categoría. La razón: solo matcheaba 1 patrón de
pregunta con `QUESTION_PATTERNS` v1 — y el default `min-questions=2` la
descartaba. Mejoras aplicadas al verifier (v2):

1. **Patrones ampliados**: agregados `¿[Cc]ómo se conoci`, `¿[Pp]or qué`,
   `¿[Pp]ara qué`, `¿[Dd]e dónde`, `Hola[,.] soy`, `[Ss]oy hermano de`,
   `Mi proyecto`, etc. La lista v1 era específica para Sessions GPI.
2. **Detección de entrevistas-sin-sync** (caso b): ahora reporta también
   clips ya marcados como entrevista que no tienen `audio_sync_pairs`.
   Permite confirmar manualmente que es "entrevista-solo-video" (no
   se grabó audio externo) — no es bug pero exige revisión.
3. **Filtro anti-alucinación** (`looks_hallucinated`): rechaza transcripts
   donde una palabra ocupa >30% del texto o hay >3 hits de hint
   ("Suscríbete al canal", "Gracias por ver el video"). Whisper sobre
   música genera basura masiva.
4. **Multi-señal de baja confianza**: si n_q=1 + `audio_rms_db > -45dB`
   + dur ≥ 120s, reportar como candidato débil. Confluencia de tres
   señales débiles es mejor que dos fuertes.

## 12c. Auditoría exhaustiva del material — instrucción del usuario

**Doctrina añadida 2026-05-26 (Zezzions, frase literal del usuario):**

> "En cada proceso de asistencia de edición es vital que revises a TODO
> el material para que los duration markers tengan la información más
> exacta posible con relación a lo que pasa en el video."

Esto refuerza el paso 13 y obliga a:

- **Revisar TODO el material** (no solo lo marcado como entrevista o lo
  sincronizado). Cada clip con dur ≥ 30s y audio activo merece pasada
  manual del transcript si los verifiers automáticos no lo cubren.
- **Markers de duración con descripción precisa** (`clip_curated_segments
  .full_text`) — no formulaicos. Para entrevistas, identificar a las
  personas en cuadro; para B-roll, describir acción + lugar + idea.
- **No confiar solo en verifiers** — son red de seguridad, no auditor
  total. Después de correrlos, hacer un audit ad-hoc con la fuente:

```bash
# Audit ad-hoc: todos los videos >=30s con audio + transcript no-alucinado
# que NO sean entrevista marcada y NO tengan sync. Inspeccionar manualmente.
python3 - <<'EOF'
import sqlite3, json, re
from pathlib import Path
db = Path('<DISK>/.cinema_assistant/manifest.sqlite')
cache = Path('<DISK>/.cinema_assistant/transcripts')
conn = sqlite3.connect(db)
rows = conn.execute("""SELECT c.id, c.filename, c.duration_sec, d.category, ca.audio_rms_db
    FROM clips c LEFT JOIN clip_descriptions d ON d.clip_id=c.id
    LEFT JOIN clip_analysis ca ON ca.clip_id=c.id
    WHERE c.file_kind='video' AND c.has_audio=1 AND c.duration_sec >= 30
      AND c.id NOT IN (SELECT video_clip_id FROM audio_sync_pairs)""").fetchall()
for cid, fn, dur, cat, rms in rows:
    tp = cache / f"{cid}.json"
    if not tp.exists(): continue
    text = (json.load(open(tp)).get('text','') or '')[:300]
    if 'Suscríbete' in text or len(text) < 30: continue
    print(f"[{cid}] {fn} dur={dur:.0f}s rms={rms} cat={cat}: {text[:100]!r}")
EOF
```

Si el audit retorna ítems, **mirar uno por uno**, marcar como
`entrevista`/`discurso`/`b-roll`/etc., asignar identidades y curar
descripción ANTES de declarar el proyecto cerrado.

## 13. Auto-review obligatoria antes de declarar terminado

> **Puerta del acta de conducta:** `[D1]` `[D2]` `[D3]` `[I1]` `[I2]` `[I3]` `[I4]` -- ver `metodologia/pruebas-del-asistente.md`.

**No se cierra el proyecto sin pasar por esta pasada.** Detalle de qué
revisar y por qué en
[`lecciones/preferencias-del-usuario.md` § "Cierre obligatorio"](../lecciones/preferencias-del-usuario.md).

Checklist mínimo:

```sql
-- 1. Cobertura por etapa
SELECT
  (SELECT COUNT(*) FROM clips WHERE file_kind='video' AND index_status='ok') AS videos,
  (SELECT COUNT(*) FROM clip_analysis WHERE analysis_status IN ('keep','review','cull')) AS analizados,
  (SELECT COUNT(DISTINCT clip_id) FROM clip_segments) AS con_tramo_visual,
  (SELECT COUNT(DISTINCT clip_id) FROM clip_curated_segments) AS con_curated,
  (SELECT COUNT(*) FROM audio_sync_pairs) AS sync_pairs,
  (SELECT COUNT(*) FROM question_segments) AS questions;

-- 2. Sanity: paths del manifest existen en disco
SELECT path FROM clips WHERE file_kind='video' ORDER BY RANDOM() LIMIT 3;
-- (correr `ls "$path"` por cada uno; deben existir)

-- 3. Sanity: tramos no exceden duración del clip
SELECT cs.clip_id, cs.start_sec, cs.end_sec, c.duration_sec
FROM clip_curated_segments cs JOIN clips c ON c.id=cs.clip_id
WHERE cs.end_sec > c.duration_sec + 0.5;
-- (debe ser 0 filas)

-- 4. Sanity: offsets de sync plausibles (|offset| < audio_dur)
SELECT v.filename, sp.offset_sec, a.duration_sec AS audio_dur
FROM audio_sync_pairs sp
JOIN clips v ON v.id=sp.video_clip_id
JOIN clips a ON a.id=sp.audio_clip_id
WHERE ABS(sp.offset_sec) > a.duration_sec;
-- (debe ser 0 filas)
```

Plus:

- `luac -p` sobre `<project>_data.lua` y `asistente_<project>.lua`.
- Carga real con `lua` nativo (mock de `resolve`) y verificar conteos.
- Lista de hallazgos: qué quedó procesado, qué quedó pendiente (con razón),
  qué se corrigió mid-process.

**Corregir todo lo corregible sin pedir permiso**; listar como "pendiente"
solo lo que requiera decisión del usuario.

## 13b. Devolver a la memoria creativa (opcional — solo si hay bóveda)

> **Puerta del acta de conducta:** `[D4]` -- ver `metodologia/pruebas-del-asistente.md`.

Simétrico al **paso 0a**. Si el 0a lee la memoria antes de empezar, el 13b la alimenta al terminar.

**Este paso solo aplica si el editor mantiene una bóveda de memoria creativa.** No es parte del pipeline de edición: es el puente hacia un sistema de notas personal. Quien no tenga bóveda se lo salta y no pasa nada — el script lo detecta, lo dice y sale con 0.

```bash
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta/del/proyecto>"
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta>" --vault ~/mi-boveda
python3 ~/cinema-assistant/bin/exportar_a_memoria.py "<ruta>" --dry-run   # sin escribir
```

Escribe dos archivos hermanos en `<bóveda>/_cambios/pendientes/`: la propuesta en JSON con el esquema que define `_cambios/README.md`, y un `.md` legible con los datos duros del manifest, el cast detectado, una muestra de las preguntas curadas y el inventario de informes.

Es **idempotente**: guarda una huella del manifest, así que correrlo dos veces sobre el mismo material no duplica la propuesta. Si el manifest cambió (más sync, más preguntas), sí propone de nuevo.

**No escribe en la memoria final.** Es la regla 2 de la bóveda: el sistema propone, Victor aprueba. Para aplicar:

```bash
~/memoria-creativa/ingesta/aprobar.sh --listar        # qué hay pendiente
~/memoria-creativa/ingesta/aprobar.sh <slug>          # marcar aplicada
```

Al terminar, deja constancia en los hallazgos de cierre del §13: qué propuesta se generó y si quedó pendiente de aprobación.

### Por qué existe este paso

Entre junio y julio de 2026 el asistente procesó cinco proyectos —Más allá del balón, Film Club Café, Fantástico Cómics, The Avalanches, más Zezzions antes— y **nada llegó a la bóveda**. El playbook abría consultando la memoria y cerraba sin volver a ella; el único canal de vuelta era que Claude escribiera propuestas a mano, y cuatro se quedaron sin aplicar entre ocho días y siete semanas. La auditoría del 2026-07-28 lo identificó como un hueco de diseño, no un descuido.

Hay una segunda vía de respaldo por si alguien se salta este paso: `scan-discos.sh` hace una pasada dirigida que busca los `manifest.sqlite` y los pasa a `ingesta/extractores/cinema-manifest.py`. Pero es red de seguridad, no sustituto: el barrido corre de vez en cuando, este paso corre siempre.

### Qué NO propone el exportador

- **Fichas-perfil de personas reales.** Registra los nombres detectados dentro de la propuesta, para que la ficha del proyecto los recoja. Los perfiles individuales de personas reales requieren decisión previa de Victor.
- **La redacción de la ficha.** El JSON trae las cifras; la materia prima buena son los `reports/*.md` (crónicas de rodaje, cierres, propuestas de cast con evidencia literal). Esos se leen con criterio, no se vuelcan.

---

## Infraestructura V2 — Sync multi-señal + diarización (Fase 1, 2026-05-26)

A petición del usuario tras el bug del delay y los falsos negativos
sucesivos (ENTREVISTADO_1, ENTREVISTADO_2, ENTREVISTADO_13 y ENTREVISTADO_10), comenzamos rediseño
del pipeline de procesamiento audio+imagen. La Fase 1 cubre **sync robusto**.

### Componentes ya implementados

| Pieza | Path | Estado |
|---|---|---|
| Bug delay (offset > 0 en placeSyncAudio) | `resolve/asistente_*.lua` | ✅ arreglado |
| Fusion multi-señal con fuerte/débil | `lib/sync_fusion.py` | ✅ tests OK |
| Sync unificado (envelope + chromaprint + transcript) | `bin/sync_combined.py` | ✅ |
| Voice embeddings (Resemblyzer GE2E) | `lib/voice_embeddings.py` | ✅ funciona, **limitación documentada** en ambiente musical |
| Diarización local sin HF gate | `lib/diarization_local.py` | ✅ webrtcvad + clustering |
| Diarización pyannote v4 | `lib/speaker_diarization.py` | ⚠ requiere HF token + aceptar términos |
| venv dedicado | `~/cinema-assistant/.venv/` | ✅ Python 3.12 + ML deps |

### Cuándo usar cada componente

1. **`bin/sync_combined.py`** — punto de entrada. Reemplaza llamadas
   separadas a `sync_acoustic` + `sync_by_chromaprint`. Corre TODOS los
   métodos y los combina con `lib.sync_fusion`. Política:
   - Métodos fuertes (transcript, phrases, questions): 1 voto basta si
     conf ≥ 0.50.
   - Métodos débiles (envelope, chromaprint, diarization): NO basta 1
     voto — exige convergencia con otra señal.
   - Convergencia ≥2 votos en offset ±2s: conf combinado = min(N×0.25, 0.95).

2. **`lib/voice_embeddings.py`** — identidad de voz cross-audio. **Solo
   útil si los audios tienen voz limpia** (ambient musical confunde).
   En Zezzions, validación empírica mostró sim > 0.77 entre voces
   distintas (ENTREVISTADO_5 vs ENTREVISTADO_1). Mantener para casos donde
   funcione, pero no ciegamente.

3. **`lib/diarization_local.py`** — sin HF gate, usa webrtcvad +
   Resemblyzer clustering. Más lento que pyannote (~5x real-time).
   Para 1-2 speakers funciona; ambient musical limita 3+.

4. **`lib/speaker_diarization.py`** (pyannote) — mejor calidad pero
   requiere `huggingface-cli login` + aceptar términos en
   `huggingface.co/pyannote/speaker-diarization-3.1`. Opcional.

### Activación del venv

Para correr cualquier script de Fase 1 con ML:

```bash
source ~/cinema-assistant/.venv/bin/activate
# o llamar directamente:
~/cinema-assistant/.venv/bin/python <script>.py ...
```

### Pendiente Fase 1

- ~~Refactor de `run_full_pipeline.sh` para llamar `sync_combined.py` en
  vez de los 5 scripts antiguos.~~ **SUPERADO (2026-08-05):** el shell se
  retiró. El orquestador es `bin/run_pipeline.py` y el sync vivo es
  `sync_audio.py` (candidatos por timecode) + `sync_pipeline_full.py`
  (voice-first + transcript-segments). `sync_combined.py` y los cinco antiguos
  quedaron fuera del pipeline.
- `bin/build_voice_catalog.py` — para cada audio, computar embedding +
  diarización + guardar en tabla `audio_speakers` del manifest.
- `bin/verify_lip_sync.py` — cross-correlate boca abierta (vision) vs
  voz sonando (audio); requiere Fase 2 (InsightFace).

### Fase 2 (en marcha) — Identidad por imagen + fusión cara↔voz

**Estado 2026-05-26**:

| Pieza | Path | Estado |
|---|---|---|
| InsightFace detection + ArcFace embedding 512-D | `bin/detect_faces.py` | ✅ corrido en Zezzions (1141 caras / 217 clips) |
| Clustering DBSCAN sobre embeddings | `bin/build_face_catalog.py` | ✅ 53 clusters (Persona_00..52) |
| Indexar speakers + cluster por voz | `bin/build_voice_catalog.py` | ✅ con `audio_speaker_segments` granular |
| Fusión cara↔voz por sincronía | `lib/identity_fusion.py` | ✅ |

**Flujo completo Fase 2**:

```bash
VENV=~/cinema-assistant/.venv/bin/python
# 1. Detectar caras en TODOS los clips de video
$VENV ~/cinema-assistant/bin/detect_faces.py --root "$DISK"

# 2. Clustering DBSCAN → mosaicos en /tmp/jilo_faces/
$VENV ~/cinema-assistant/bin/build_face_catalog.py --root "$DISK"

# 3. Persistir clusters con nombres provisionales Persona_NN
python3 -c "
import json
d = json.load(open('/tmp/jilo_faces/clusters_index.json'))
print(' '.join(f\"{c['cluster']}=Persona_{c['cluster']:02d}\" for c in d if c['cluster']>=0))
" | xargs $VENV ~/cinema-assistant/bin/build_face_catalog.py --root "$DISK" --assign

# 4. Indexar voces (diariza + embed + clusterea por voice_catalog)
$VENV ~/cinema-assistant/bin/build_voice_catalog.py --root "$DISK" --cluster --sim-threshold 0.72

# 5. Vincular cara↔voz por sincronía + propagar nombres
$VENV ~/cinema-assistant/lib/identity_fusion.py --root "$DISK" --propagate

# 6. Asignar nombres reales (interactivo, mirando los mosaicos):
$VENV ~/cinema-assistant/bin/build_face_catalog.py --root "$DISK" --assign 5=ENTREVISTADO_11 8=ENTREVISTADO_13 11=ENTREVISTADO_10 ...
```

**Tablas pobladas**:
- `face_detections` (1 fila por cara detectada)
- `face_catalog` (1 fila por identidad: nombre canónico + centroid + cluster_id)
- `face_identities` (mapea detection → identity)
- `audio_speakers` (1 fila por (audio, speaker_label) con embedding GE2E)
- `audio_speaker_segments` (1 fila por segmento de habla — **granular**)
- `voice_catalog` (1 fila por identidad de voz inferida cross-audio)
- `face_voice_links` (vincula detection ↔ speaker por timestamp en sync)

**Lección Zezzions 2026-05-26**: la primera versión de `audio_speakers`
solo guardaba el chunk más largo del speaker. `identity_fusion` solo
encontraba 14 links (de 200+ posibles). Fix: tabla auxiliar
`audio_speaker_segments` con TODOS los segmentos granulares; ahora
identity_fusion puede preguntar correctamente "¿está activo el speaker
S en t=X?" para cualquier momento del audio.

### Fase 3 (en marcha) — Eventos sincronizados

**Estado 2026-05-26**: PANNs CNN14 entrenado en AudioSet (527 clases),
checkpoint Cnn14_mAP=0.431.pth (~312 MB) — sin Hugging Face gate.

| Pieza | Path | Estado |
|---|---|---|
| Clasificador de eventos sonoros (PANNs CNN14) | `lib/sound_events.py` | ✅ funciona, ~50× real-time en CPU M-series |
| Indexador de eventos por proyecto | `bin/index_sound_events.py` | ✅ |
| Sync por eventos únicos | `bin/sync_by_events.py` | ✅ |
| Tabla `sound_events` | manifest.sqlite | ✅ |

**Setup inicial (una vez)**:
```bash
mkdir -p ~/panns_data
# Labels CSV (necesario para parsing)
curl -sL "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv" \
  -o ~/panns_data/class_labels_indices.csv
# Checkpoint del modelo (~312 MB, una sola vez por máquina)
curl -sL "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1" \
  -o ~/panns_data/Cnn14_mAP=0.431.pth
# Librería (en el venv)
~/cinema-assistant/.venv/bin/pip install panns_inference
```

**Flujo**:
```bash
VENV=~/cinema-assistant/.venv/bin/python
# 1. Indexar eventos sonoros en audios + videos
$VENV ~/cinema-assistant/bin/index_sound_events.py --root "$DISK" --kind both

# 2. Sync por eventos únicos (aplauso, risa, bang, whistle)
$VENV ~/cinema-assistant/bin/sync_by_events.py --root "$DISK" \
  --min-anchor-events 2 --min-confidence 0.30
```

**Grupos agrupados** (`lib/sound_events.GROUP_BY_LABEL`):
- `speech`, `music`, `singing` — continuos, NO sirven como sync anchor.
- **`applause`, `laughter`, `bang`, `whistle`** — discretos, **sí sirven** como anchor.
- `chatter`, `ambient`, `silence` — informativos para clasificación.

**Lección Zezzions 2026-05-26**: en proyectos de evento musical (Sessions GP)
los aplausos son raros — solo 6 detectados en 1 audio ambient de 1h. La
mayoría de los anchors útiles vienen de `laughter` y `singing`. Si el
proyecto tiene poco material con eventos discretos, este método es
complementario (no reemplazo) a `sync_combined.py`.

### Fase 4 (completada base) — DAW (Resolve) más informativo

**Estado 2026-05-26**: el Lua produce markers con información rica derivada
de Fases 1-3.

| Mejora | Estado |
|---|---|
| Bug offset positivo arreglado (placeSyncAudio) | ✅ |
| Markers Cyan con identidad por canal: "A2 — ENTREVISTADO_13" | ✅ |
| Anotación "GAP Xs al inicio/final" cuando offset crea hueco | ✅ |
| Speaker name propagado desde voice_catalog vía export_lua_data | ✅ |
| Markers Yellow para highlights de sound_events (👏 risa, 🎵 silbato) | ✅ |

**Flujo de actualización del Lua**:

```bash
# 1. Asegurar que voice_catalog tiene nombres canónicos (cara→voz propagado)
$VENV ~/cinema-assistant/lib/identity_fusion.py --root "$DISK" --propagate

# 2. Re-exportar el Lua data con los campos nuevos
python3 ~/cinema-assistant/bin/export_lua_data.py --root "$DISK" \
  --out ~/cinema-assistant/resolve/<project>_data.lua --project-prefix ""

# 3. luac -p validar y aplicar en Resolve
luac -p ~/cinema-assistant/resolve/<project>_data.lua
# En Resolve (RUTA ABSOLUTA: ni "~" ni "$HOME" expanden dentro de un string de Lua):
#   dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/asistente_<project>.lua")
```

**Campos nuevos en `<project>_data.lua`**:

- Por sync entry:
  - `speaker` — canonical_name del speaker dominante del audio (vía
    voice_catalog → identity_fusion propagated from face_catalog).
- Por clip:
  - `highlights = {{s, e, group, conf}, ...}` — eventos sonoros de hito.

**Refinamiento sub-segundo (Fase 4+, 2026-05-26)**:

```bash
# Después de sync básico, refinar offsets con resolución 10ms.
# Cross-correlate envelope log-RMS en banda de voz 300-3400 Hz.
$VENV ~/cinema-assistant/bin/refine_sync_pairs.py --root "$DISK" --dry-run
$VENV ~/cinema-assistant/bin/refine_sync_pairs.py --root "$DISK"
# luego re-exportar Lua para que el nuevo offset llegue a Resolve
```

Solo aplica si `prominence ≥ 0.30` (peak claramente dominante). Skipea
deltas < 30ms (despreciables) y > 2500ms (sospechosos). Mantiene los
sync curados manualmente (method='manual').

**Marker types en Resolve tras Fase 4**:

| Color | Tipo | Origen |
|---|---|---|
| Red | "Posible descarte" | clip_analysis status=cull |
| Yellow | "Revisar" | clip_analysis status=review |
| Yellow | "😄 laughter / 👏 applause / 🎵 whistle / 💥 bang" | sound_events (Fase 3) |
| Green | "Tramo N [shot/angle]" | clip_curated_segments con full_text |
| Purple | "QN: pregunta" | question_segments (entrevistas) |
| Cyan | "AN — Speaker" | sync pair con identidad de voz |

## Entry-point único para material nuevo: `bin/run_pipeline.py`

**Para procesar material nuevo el comando es uno solo:**

```bash
python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK"
```

Tres modos que conviene conocer antes de lanzarlo sobre un disco:

```bash
python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK" --plan-only
python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK" --listar
python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK" --desde sync-v2
python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK" --solo transcribir-audios
```

Es declarativo: cada paso declara qué capacidad necesita (`requiere=`) y qué
hacer si falla (`al_fallar=abort|warn`). `lib/entorno.py` resuelve con qué
intérprete lanzarlo — el núcleo va con el `python3` de siempre y solo las
capacidades que ese no tiene (caras, voz, eventos) caen al venv. Un paso cuya
capacidad no exista **no se ejecuta y no falla la corrida**: se anota en la
sección "no corrió y por qué" con el comando para habilitarlo.

Razón de existir: antes de este orquestador los pasos se corrían a mano y era
trivial olvidar un sector o un flag — el caso ESCALANDO MEXICO, donde solo
JILOTEPEC quedó completo y los otros 6 sectores sin sync ni markers.

> **`run_full_pipeline.sh` se retiró el 2026-08-05** y hoy es un shim que
> reenvía aquí con `exec`. No se rompe nada si lo tecleas, pero **no lo uses**:
> quien lo hace nunca descubre `--plan-only`, `--listar` ni el modelo de
> capacidades. Se retiró porque llevaba roto desde el 31 de julio (llamaba a
> `verify_coverage.py` sin el `--project-prefix` que ese día se hizo
> obligatorio, así que moría en su línea 110 antes de indexar un solo audio) y
> nadie lo notó porque ninguna prueba ejecutaba un `.sh`. Además tenía cinco
> hardcodes de ESCALANDO MEXICO invisibles al linter, guardas que comparaban
> contra un piso absoluto, ocho pasos que seguían adelante tras fallar —incluido
> el sync principal— y un solo intérprete, por lo que ningún paso de identidad
> podía correr.
>
> El **TSV de sectores** (`bin/sector_video_audio_map.tsv`) era suyo y ya no lo
> lee nadie: el orquestador nuevo deduce la forma del proyecto con
> `lib/proyecto.py`. Si buscabas dónde declarar sectores nuevos, ya no hay que
> declararlos.

> **Diagnóstico antes de empezar**, sobre todo en una máquina que no sea la de
> siempre:
>
> ```bash
> python3 ~/cinema-assistant/bin/doctor.py
> ```
>
> Dice, capacidad por capacidad, qué puede y qué no puede hacer esa Mac, y
> —esto es lo importante— cuáles están instaladas en un intérprete que el
> pipeline NO usa.

---

# Actualización v0.3.0 — lo aprendido en Morsa (2026-08-03)

Morsa fue el primer proyecto con **tres cámaras** y el primero en un **concierto**.
Las dos cosas rompieron supuestos que venían de los cinco proyectos anteriores.
Esto es lo que cambia en el orden de trabajo. Lo anterior sigue valiendo salvo
donde aquí se diga.

## Pasos nuevos y dónde entran

**Antes del paso 2 (indexar), si el proyecto es nuevo:**

```bash
python3 bin/nuevo_asistente_proyecto.py --disco "<disco>" --nombre "MORSA" --slug morsa
```

Genera el `asistente_<proyecto>.lua`. **No copiar el de otro proyecto a mano**:
así se arrastran cabeceras, rutas y banners ajenos, y los scripts fueron
divergiendo hasta que tres de cinco estaban desfasados.

**Después del paso 8 (sync) y ANTES de la multicám:**

```bash
python3 bin/derive_show_windows.py --root "$DISK"
```

Mide en qué tramos del día domina la música. Solo hace falta si el material
tiene bloques largos de música o espectáculo; en un rodaje normal no cambia
nada. Ver `metodologia/resolve-integracion.md`.

**El merge del Media Pool deja de ser opcional** y entra ANTES del bake:

```bash
python3 bin/export_merge_plan.py --root "$DISK" --out "$BAKE/<p>_merge.lua" --project-prefix ''
# y en Resolve, SOBRE PROYECTO DUPLICADO:
#   MERGE_PLAN = "..."; dofile(".../merge_pool.lua")
```

Es lo único que liga el audio con su clip de verdad. Sin él, el lavalier va en
pista aparte y se queda atrás al mover el clip — petición permanente del usuario
del 2026-08-03: *"linkéalos a partir de ahora siempre"*.

## Cambios en pasos que ya existían

**Paso 8 — el orden real del sync.** `derive_chrono_sync` va **después** de
`sync_transcript`: mide el skew a partir de los pares de transcript y aborta sin
ellos. Y `sync_siblings_rule` necesita `init_sync_schema` corrido antes.

**Paso 10/11 — dependencias de `curate_segments`.** Necesita tres tablas
previas: `derive_shot_value`, `derive_camera_angle` y `derive_content_segments`.
Sin ellas revienta y `clip_curated_segments` queda vacía; el bake sale con
`curated: 0` sin avisar de nada raro.

**Paso 11 — las timelines.** Ya no se generan `TODO EL MATERIAL` ni
`ENTREVISTAS`. Quedan **A-ROLL, B-ROLL**, una por cámara y `AUDIOS EXTERNOS`,
con una garantía: entre A-roll y B-roll tienen que estar TODOS los clips, y el
script lo comprueba al cerrar con números.

**Paso 11 — orden de las pistas de cámara.** Regla del usuario: **V1 la cámara
con más material, V2 la siguiente, V3 la que menos**. Lo calcula
`export_multicam_lua.py` y viaja en el bake como `track_order`; la cámara base
ya no se elige con `--base-group`, se deduce. En Morsa: Ayan 110 min, Vic 89.5,
Iban 50.7.

**Paso 11 — `AUDIOS EXTERNOS` es la cronología del día.** Todo posicionado por
tiempo real, con t=0 en el primer clip: cada cadena de audio en su pista y las
cámaras arriba en su hora.

## Cuando el motor no puede medir un reloj

Si una cámara tiene el A1 tapado (música, viento, sala ruidosa) puede quedarse
sin skew medible. **El default silencioso es 0, y eso ordena sus clips con su
reloj crudo**: si ese reloj está corrido, su material se intercala en el lugar
equivocado. El script ahora avisa por grupo y en grande al cerrar.

Antes de dar un reloj por no medible, **comprobar que el rango de búsqueda pueda
contener la respuesta**. Un desfase de cámara puede ser de HORAS, no de
segundos. En Morsa se falló seis veces buscando en ±180–300 s cuando el desfase
real era de una hora.

Cuando el editor lo detecta a ojo, eso es una medición y se declara:

```json
"camera_skews_manual": {"Iban": -3600.0},
"camera_skews_manual_nota": "quién lo midió, cómo y con qué evidencia"
```

Negativo = la cámara va atrasada respecto al tiempo real. Sirve para ORDENAR la
cronología y generar candidatos de multicám; la precisión de frame la da después
el refinamiento por contenido. En Morsa, con el valor declarado, **nueve pares
de esa cámara pasaron la verificación por contenido** — corroboración
independiente de que el número era correcto.

## Transcripts sobre música

Whisper colapsa en bucles de invención cuando hay música fuerte. El audit
estadístico no basta porque lo limpio y lo tóxico van intercalados. La técnica
que funcionó: borrar toda palabra atrapada en un n-grama de 8 palabras repetido
4 veces o más. Detalle y validación contra VAD en
`lecciones/patrones-exitosos.md`.

**Consecuencia editorial que hay que decir en voz alta:** una hora sin habla en
los lavalieres NO es una hora perdida. En un concierto es el show, y el video de
esa ventana es cobertura.

---

# Actualización v0.4.0 — la pieza con guion (IMODAE, 2026-08-07)

IMODAE fue el primer **comercial**: seis cápsulas rodadas el mismo día, con
prompter y sin audio externo. Doctrina completa en
[`reels-y-cortes.md`](reels-y-cortes.md); aquí va sólo dónde entra en el orden.

## El track de comercial

Declarar primero `"project_kind": "comercial"` en `project_config.json` — no se
infiere nunca. Después del paso 5 (transcribir) y en lugar de los pasos de sync,
entrevistas y multicám, que no aplican:

```bash
python3 bin/derive_takes.py  --root "$DISK"          # agrupa por texto
python3 bin/derive_reels.py  --root "$DISK"          # las reparte por cápsula
#   ← LEER reports/reels-<fecha>.md y confirmar con el editor antes de seguir
python3 bin/derive_roll.py   --root "$DISK" --project-prefix ''
python3 bin/detect_pauses.py --root "$DISK" --seleccion takes --min-dur 1.5
python3 bin/derive_silence_cuts.py --root "$DISK"    # OPT-IN, no siempre aplica
python3 bin/verify_cortes.py --root "$DISK"          # la garantía
```

Antes de `derive_reels`, **leer el brief y llenar `palabras_clave`** de cada
cápsula. El texto que se rodó no es el del brief (en IMODAE se reescribió en el
set y no comparte una frase), así que el matcheo va por vocabulario del tema; con
tres palabras por cápsula el matcher no tiene con qué trabajar.

## Cambios en pasos que ya existían

**Paso 11 — `export_multicam_lua` hace falta aunque no haya multicám.** Es el
vehículo de los `camera_skews_manual` hasta el Lua: sin él, `MC.skews` llega
vacío y cada cámara se ordena con su reloj crudo. Un rodaje a varias cámaras con
sólo audio de cámara los necesita igual.

**Paso 12b — un verificador más** cuando el proyecto lleva cortes:

```bash
python3 bin/verify_cortes.py --root "$DISK"   # exit 1 si un corte se come habla
```

## Lo que este track NO hace

No hay sync, no hay entrevistas, no hay multicám y no hay merge del Media Pool:
si todo el diálogo va por el A1 de cámara, no hay nada que ligar. Decirlo en voz
alta vale más que dejar seis pasos en "0 procesados".

---

# Actualización v0.7.0 — el paso que faltaba: entregar (Morsa, 2026-08-19)

Todo el orden anterior termina cuando las timelines están en Resolve. Lo que
pasa **después** —el corte, el render, los subtítulos, los reels— no tenía
ningún paso, así que nadie miraba el archivo que sale por la puerta. Al mirarlo
por primera vez en Morsa salieron siete exports clipeando y un SRT que terminaba
3:17 antes que la pieza.

## Paso 14 — medir lo que se entrega

```bash
python3 bin/verify_audio.py --root "$DISK"              # tabla de todos los exports
python3 bin/verify_audio.py --root "$DISK" --detalle    # + curva, tramos y bandas
```

Escribe `reports/audio-<fecha>.md` y **sale con 0 siempre**: mide y avisa, no
reprueba (decisión de Victor, ver
[`preferencias-del-usuario.md`](../lecciones/preferencias-del-usuario.md)). Lo
que hay que mirar a ojo es la columna de **muestras a 0 dBFS**: un pico alto
puede ser una muestra, un millón es un archivo destruido.

## Paso 14b — MIRAR lo que se entrega (2026-08-26)

Medir el sonido no es mirar la pieza. Un salto de imagen en un corte, un fundido
que se comió un fotograma, un subtítulo detrás de una capa: nada de eso sale en
un número, y todo eso viaja al entregable igual.

```bash
python3 bin/verify_export.py --root "$DISK"
# con los cortes REALES de la timeline, no los que se ven:
python3 bin/verify_export.py --root "$DISK" --archivo "$EXPORT" \
    --proyecto "<proyecto>" --timeline "<nombre>" --config "$CFG" --bloque 1
```

Genera una vista de ±1.5 s en cada frontera de corte —imagen, onda y regla en la
misma figura, `bin/vista_tramo.py`— más arranque, tres medios y cierre, y
escribe `reports/export-<fecha>.md` con las rutas y qué comprobar en cada una.
Mide y avisa, no reprueba: si un corte salta es juicio editorial.

**Generar las vistas no es mirarlas.** El script pone las imágenes delante; el
trabajo empieza después, y es del asistente.

**Tope de tres pasadas.** Corregir, volver a mirar y corregir otra vez converge
o no converge. Si a la tercera sigue habiendo algo, se le DICE al editor con la
lista de lo que sigue mal y por qué no se corrigió. El script se niega a una
cuarta (`--pasada 4` sale con error).

Ojo con `--bloque`: la duración de la timeline no es la duración del corte (en
Morsa, 78:10 de timeline y 16:35.7 entregados). Doctrina completa en
[`ver-el-material.md`](ver-el-material.md).

## Paso 14c — leer un montaje ya hecho (opcional, para aprender de él)

Cuando el editor ya montó, su timeline es el dato más valioso del proyecto: dice qué toma eligió,
dónde se fue a B-roll y qué descartó. Se lee sin abrir Resolve:

```bash
python3 bin/analizar_montaje.py --proyecto "<Proyecto de Resolve>" \
    --timeline "<Timeline>::<carpeta de Exports>::<project_config.json>" \
    --out <disco>/.cinema_assistant/reports/corpus-montaje-<fecha>.json
```

Doctrina completa en [`montaje-de-reels.md`](montaje-de-reels.md): la anatomía de un reel, dónde
entra un inserto respecto al habla (71 % a media frase, no en las pausas), y las tres reglas que se
pusieron a prueba — con la que falló declarada como tal.

## Paso 15 — subtitular con los cortes de la timeline

```bash
python3 bin/build_subtitles.py --video "$EXPORT" --config "$CFG" \
    --timeline "<nombre de la timeline de la que salió el export>"
python3 bin/verify_subtitulos.py --srt "$SRT" --video "$EXPORT" \
    --config "$CFG" --timeline "<nombre>" --max-cruces N
```

`--timeline` es lo que hace que los cortes manden sobre los bordes de los
subtítulos; sin él el script se comporta como antes. El `N` de `--max-cruces` lo
imprime `build_subtitles` al terminar: son los cues que no se podían partir sin
romper otra regla, y se **declaran**, no se esconden. Doctrina completa en
[`reels-y-cortes.md`](reels-y-cortes.md), actualización v0.7.0.

Para reimportar en Resolve sólo lo nuevo sobre subtítulos ya colocados:

```bash
python3 bin/build_subtitles.py --video "$EXPORT" --config "$CFG" \
    --timeline "<nombre>" --desde 00:00:23:03
```

## Paso 16 — los settings, una vez por proyecto

Con el proyecto abierto en Resolve, en la consola Lua:

```lua
DESTINO = "<disco>/.cinema_assistant/resolve"
dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/auditar_settings.lua")
```

Vuelca los ajustes de proyecto y de cada timeline a JSON. Es el único camino:
del `Project.db` no se pueden leer (ver [`formatos.md`](formatos.md)).

## Lo que este bloque NO hace

No re-renderiza, no re-mezcla y no cambia el estilo de los subtítulos quemados.
Mide, dice, y deja las decisiones donde tienen que estar.

---

# Actualización 2026-09-28 — un proyecto nuevo de punta a punta (Asistente)

El video de Diez50 sobre el asistente fue el primer proyecto NUEVO que se corrió
entero con el orquestador. No pasó solo: se atoró tres veces, y las tres eran
huecos del motor, no del material. Quedan arreglados; esto es el orden que salió.

## Proyecto nuevo, en orden

```bash
DISK="/Volumes/<disco>/<proyecto>"
python3 bin/index_project.py --root "$DISK"
python3 bin/inventario_camaras.py --root "$DISK" --project-prefix ''
python3 bin/nuevo_asistente_proyecto.py --disco "$DISK" --nombre "<N>" --slug <s> \
    --referencia asistente_morsa.lua      # documental: la de Morsa trae la cronologia
```

**Antes de la puerta G0, declarar en `project_config.json` lo que el motor no infiere:**

- `cadenas_lavalier`: sin ella, todo sale "sin hermano" en silencio.
- `mapa_audio`, si los nombres de carpeta no se parecen: `VIDEO 01` no casa con
  `AUDIO`, y los dos sectores quedaban como "solo audio de cámara".
- `camaras_por_prefijo`, si una carpeta trae más de un prefijo (lección "la carpeta
  es la tarjeta").
- `vocabulary_fixes`, cada una comprobada contra el corpus completo antes de
  declararla.

```bash
python3 bin/verify_asistente.py --root "$DISK" --project-prefix '' --puerta G0
python3 bin/run_pipeline.py --root "$DISK" --project-prefix '' --plan-only
python3 bin/run_pipeline.py --root "$DISK" --project-prefix ''
```

El orquestador ahora incluye:

- `analizar-clips`, `categorias`, `valor-de-plano`, `angulo` y
  `tramos-de-contenido`. Sin ellos, `curar-tramos` abortaba con `no such table:
  clip_descriptions`.
- El `--audio-like` de `transcribir-audios`, sacado de `mapa_audio`.

**El paso `pruebas` aborta mientras las `references/` del plugin vayan atrás de la
doctrina viva** (`test_doctrina_empaquetada`), y `test_capas` falla hasta el primer
bake de un proyecto recién generado. Si hay que correr sin reempaquetar: `--solo
lint` y luego `--desde baseline`, y **escribir por qué**.

## Después del pipeline

- `derive_chrono_sync.py`: va DESPUÉS del sync por transcript.
- `export_multicam_lua.py --out "$BAKE/<s>_multicam.lua"`: hace falta aunque no haya
  multicám, porque lleva los skews al Lua.
- `export_lua_data.py`: vuelve a hornear con todo lo anterior.
- `export_merge_plan.py`, luego `merge_pool.lua`, luego `asistente_<s>.lua`.

**El aplicador NO importa video**: usa lo que ya esté en el Media Pool, y solo
importa WAV, a la carpeta que esté activa en ese momento. Antes de aplicar hay que
importar todo el material a bins propios del asistente, sin duplicar lo que el
editor ya importó.

## En Studio: sin Consola

En la Mac de Victor, todo lo de Resolve se hace desde fuera: el respaldo `.drp`,
comprobado reimportándolo como proyecto aparte; la importación; el merge; el
aplicador por `fusion.Execute`, y la lectura de vuelta. Detalle en
`resolve-integracion.md`, sección "En la Mac de Victor". El merge exige un proyecto
cuyo nombre diga COPIA/PRUEBA/TEST/MERGE, o `MERGE_FORZAR = true`. Se fuerza SOLO
si el respaldo ya se reimportó y se comprobó que tiene lo mismo que el original.

## Voz sintética del asistente (opcional, por pedido)

Cuando el editor quiere que el asistente "aparezca":

- **Guion.** Lo escribe el asistente en `VOZ CLAUDE/guion-voz-claude.md` después de
  leer los transcripts curados. Cada intervención tiene `Texto` (ortografía real),
  `Voz` (cómo tiene que sonar) y `Responde a` (clip y minuto). Escritura neutra en
  género, y solo datos comprobables.
- **Síntesis.** Piper, en su propio venv (`~/.venvs/piper`, Python 3.12), con voces
  de `rhasspy/piper-voices`. Antes de usar una voz se lee la licencia de su dataset.
- **Verificación.** Whisper de ida y vuelta sobre cada toma, contra el `Texto`.
  Corrige primero reescribiendo la frase, no forzando la ortografía. Tope de TRES
  pasadas; Piper no tiene semilla, así que solo se regeneran las tomas que fallaron.
- **Resolve.** Timeline `VOZ CLAUDE — tomas`, FUERA del prefijo del proyecto, porque
  el aplicador borra y rehace todo lo que empieza por ese prefijo en cada corrida.
- La carpeta `VOZ CLAUDE` está en `SKIP_DIRS`: nunca entra al índice como un tercer
  lavalier.

---

# Actualización 2026-09-29 — el segundo día en el mismo proyecto (Asistente)

El material nuevo cayó en la misma carpeta del disco (`VIDEO 03`, `VIDEO 04` y dos WAV sueltos
en `AUDIO/`), así que es la misma asistencia con un día más, no un proyecto nuevo. Re-correr el
pipeline sobre un manifest con trabajo a mano es otro terreno: tres pasos lo borraban (ver
`errores-comunes-a-corregir.md`, "Sumar un segundo día...").

## En orden

1. **Respaldar el manifest** antes de tocar nada:
   `cp manifest.sqlite backups/manifest-<fecha>-antes.sqlite`. Al terminar, comparar el
   número de filas tabla por tabla. Una tabla que baja es trabajo perdido.
2. `index_project.py`. Si una carpeta cambió solo de mayúsculas, ahora la reconoce. Si el
   conteo de "Indexing" es mayor que lo nuevo, mirar qué entró antes de seguir.
3. **`project_config.json`**:
   - `mapa_audio` con los sectores nuevos;
   - `shoot_days` con el día;
   - `lavalier_tx` si hay WAV fuera de las carpetas por TX;
   - `vocabulary_fixes` de lo nuevo.
4. `inventario_camaras.py --project-prefix ''`.
5. El orquestador desde el principio, o `--desde` el paso que haga falta. `pruebas` aborta
   mientras el plugin no se reempaquete (`test_doctrina_empaquetada`), así que se corre con
   `--desde baseline`.
6. **El sync a mano, ACOTADO al material nuevo.** `sync_transcript.py --location "VIDEO 04/"
   --audio-like 'audio/000%' --max-pairs-per-video 2`. Luego `derive_chrono_sync --per-chain`,
   `sync_siblings_rule`, `refine_all_pairs --include-locked-like '%-locked'` (ya no re-mide lo
   medido) y `refine_pairs_longwin --only-interviews`. Después, el orquestador `--desde
   verify-sync`.
7. **Curaduría solo de lo nuevo.** Los clips de `question_clips_curados` no se tocan. Al cerrar
   la Capa 2c de los nuevos, se agregan a esa tabla.
8. **Hornear y aplicar un día a la vez** (`DIA = "AAAA-MM-DD"`). Ver
   `resolve-integracion.md`, "Varios días de rodaje en UNA asistencia".

## Los lavalieres debajo del corte del editor

Cuando el editor ya empezó a cortar y pide "ponle el audio de los lavas a lo que llevo del
corte": `resolve/lavas_al_corte.lua`. Duplica su timeline y pone el lavalier de cada audio de
cámara que usó, en su sitio y en pistas `LAVA <tx>` al final. Detalle en
`resolve-integracion.md`, "Lavalieres debajo del corte del editor".
