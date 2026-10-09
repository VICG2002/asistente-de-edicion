# Errores comunes a corregir

Errores que han ocurrido en proyectos reales. Releer al inicio de cada uno.

## 1. Asumir una sola cámara principal
Un documental con varios shoots tiene a menudo varias A-cameras. En ESCALANDO
MEXICO había Canon EOS 6D **y** Blackmagic. Asumir solo Canon ignoró 302 clips
de cine. → **Inventario de cámaras por ubicación** es paso obligatorio del playbook.

## 2. Tablas indexadas por filename
GoPro y DSLR reciclan nombres entre tarjetas/sesiones. Una table keyed por
filename **pierde clips en silencio** (el último escritor gana). En el bake
de `escalando_data.lua` costó 77 clips y 68 segmentos.
→ Indexar por **ruta absoluta**; `byname` solo como fallback.

## 3. TC sync entre cámara y recorder consumer = basura
Los relojes de GoPro y Zoom no están jam-sincronizados. El solapamiento de
timestamps empareja por coincidencia, no por contenido.
→ No usar TC sync entre consumer devices. **La GoPro nunca emparejarse con
audio externo** (trae el suyo y no es dual-system).

## 4. Waveform con audio corto = falsos positivos
Correlacionar un clip largo con WAVs de 6-14 s da confianza alta espuria.
Cualquier patrón corto encuentra picos en una señal larga.
→ Filtrar audio `duration_sec >= 60` antes de correlacionar.

## 23. Entrevistas con transcript de cámara mayormente alucinado

Caso Zezzions (2026-05-26, dos veces): clip 2786 (ENTREVISTADO_2) y 2794
(ENTREVISTADO_1). Su A1 de cámara tenía las preguntas del entrevistador
audibles al inicio (~5 preguntas, ~70-100 palabras limpias) y después
solo música del evento. Whisper alucinó "No, ya tenemos artística" ×100
y similares.

- `analyze_transcript()` marcó esos transcripts como `garbage_ratio > 50%`
- La categorización requería ≥ 60 palabras distintivas limpias — esas
  preguntas solas tenían solo ~50 palabras distintas
- Resultado: NO se marcaron como 'entrevista' → `derive_question_segments`
  no los procesó → no entraron a `sync_by_questions` → quedaron como
  B-roll desconocido. El usuario los encontró visualmente al editar.

→ **Garantía verificable construida**: `bin/verify_interviews.py` detecta
  videos ≥60s con ≥2 patrones de pregunta de entrevista que NO están
  marcados como tales ni tienen sync. Validado empíricamente: detecta
  los dos casos cuando se desmarcan. Correr OBLIGATORIO antes de cerrar
  cualquier proyecto (paso 12b del playbook).

→ **Refinamiento futuro de `analyze_transcript`**: añadir señal "tiene
  patrones de pregunta-entrevista en zona limpia" como criterio
  alternativo a "≥60 palabras distintas". Si hay ≥2 preguntas del
  entrevistador, marcar como `entrevista-posible` aunque el resto sea
  basura.

## 22. Mención del nombre ≠ persona en cuadro
Caso Zezzions (2026-05-26): clip 2598 fue marcado como entrevista de
**ENTREVISTADO_4** porque `clip_characters` decía "ENTREVISTADO_4(3)" — el
nombre aparece 3 veces en el transcript. Pero el verdadero entrevistado
es **ENTREVISTADO_9**: la primera pregunta del entrevistador dice
*"vi que ahorita traías el acordeón, que es bastante único como para ver
en vivo a un DJ, como tocando el acordeón"*. ENTREVISTADO_4 es solo mencionada
como referencia en la conversación.

→ **Regla**: en `clip_characters`, **distinguir asterisco (auto-id) de
  mención**. El asterisco (`Nombre*(N)`) viene de `[Yy]o\s+soy|[Mm]e\s+
  llamo|[Mm]i\s+nombre\s+es` — es evidencia FUERTE de que la persona
  está en cuadro hablando. Sin asterisco solo significa que el nombre fue
  pronunciado, no que esté en cuadro.

→ **Para curaduría manual**: priorizar nombres con asterisco. Si todas
  las menciones son sin asterisco, leer la primera pregunta del
  entrevistador en el transcript — suele identificar al entrevistado
  ("Cuéntanos cómo te aproximas a la música, vi que traías el acordeón...").

→ **Para `export_lua_data.is_interview`**: cuando el clip tiene un nombre
  con `*` en `clip_characters`, esa identidad es protagonista. Solo
  menciones sin `*` no son suficientes para asumir protagonismo.

## 21. Bulk `mp:AppendToTimeline` no garantiza orden cronologico
Caso Zezzions (2026-05-26): el Lua llamaba `chronoSort(list)` correctamente
y construía `mpitems` en orden, pero `mp:AppendToTimeline(mpitems)` con
253 items en una sola llamada producía timelines fuera de orden. El usuario
detectó clips desordenados.

→ **Fix**: insertar **uno-por-uno**:
```lua
for _, r in ipairs(list) do
  mp:AppendToTimeline({r.clip})
end
```
Marginalmente más lento (~5s extra) pero garantiza secuencia.

## 19. Filtro de alucinaciones demasiado agresivo descarta entrevistas reales
Caso Zezzions (2026-05-26, feedback fuerte del usuario): la versión 1 de
`lib/transcript_quality.is_hallucinated()` declaraba el transcript ENTERO
como basura si encontraba una ráfaga de alucinación en cualquier lugar.
Resultado: 8+ entrevistas reales (cada una con varios minutos de diálogo
genuino del entrevistador + sujetos) fueron descartadas porque al final del
clip Whisper inventó "gracias ×100" sobre música del evento. El usuario
detectó el problema directamente: "faltan TODAS las entrevistas que se
grabaron cuando más ruido había".

Reparación (v3 del detector):

- `analyze_transcript()` ahora devuelve un dict con:
  - `garbage_ratio` (fracción del transcript afectada por ventanas tóxicas)
  - `clean_until_idx` y `clean_until_sec` (donde empieza la basura)
  - `clean_words` (palabras en zona limpia)
  - `is_hallucinated` solo True si > 50% basura **Y** la zona limpia
    NO tiene contenido sustancial (< 100 palabras limpias o < 30s de habla).
- `derive_question_segments` lee `clean_until_sec` del `clip_descriptions.description`
  y filtra preguntas que cayeron en la zona basura.

→ **Regla**: el detector de alucinaciones debe operar a nivel de
  **ventana** (~50 palabras), no a nivel de transcript entero. Una ráfaga
  no invalida un buen prefijo; un buen prefijo no perdona una ráfaga
  posterior. Guardar el rango limpio y propagarlo a downstream.

→ **Doctrina general**: cualquier filtro de calidad de datos en el pipeline
  debe degradar gradualmente, no en blanco-negro. Cuando se descarta material
  editorial real del usuario, eso es un bug crítico — vale más mantener algo
  imperfecto que perder lo confiable. El usuario decide al editar.

## 5. Whisper alucina en silencio Y en música alta
Audio sin habla → Whisper inventa: "¡Suscríbete al canal!", "Gracias.
Gracias...", "No, no, no...".

**Refinamiento Zezzions (2026-05-25):** la densidad léxica NO es suficiente
filtro cuando la fuente es **audio de cámara con música encima del diálogo**.
Whisper genera muchas palabras (alta densidad léxica falsa) pero son
alucinación masiva: `'gracias' ×100`, `'sí,' ×134`, `'no, no, no' ×52`,
`'sí, sí, sí' ×69`, `'empezaron a hacer' ×113`.

Estos transcripts pasaron el filtro de 60 palabras distintas y fueron
marcados como entrevista, generando 100+ preguntas Purple falsas y un
sync pair de confianza inflada (2644↔00006, conf=1.05, AMBOS_DEGRADADOS).

→ **Detector obligatorio antes de usar un transcript como evidencia:**
  `lib/transcript_quality.is_hallucinated(text, words)`. Criterios:
  - Frase tipica (`gracias`, `suscríbete`, `amara.org`, etc.) ≥ 8 veces.
  - n-grama de 3-4 palabras consecutivas repetido ≥ 8 veces.
  - Palabra dominante (excluyendo stopwords) ≥ 15% y ≥ 20 ocurrencias.

  Si dispara, marcar la categoría con sufijo `-degradada` y NO usar para:
  detección de entrevista, derivación de preguntas, sync por transcript,
  ni descripción LLM.

→ **Para proyectos con audio de cámara contaminado por música**
  (entrevistas en eventos, conciertos, calle), **NO confiar en
  transcribe_clips/main**. Priorizar los lavaliers / audio externo y
  aplicar el sync por transcript desde su lado.

## 6. Signo del offset mal documentado
Dos métodos de sync (waveform y transcript) emparejaron el mismo par con
signos opuestos. Si uno se aplicaba con la convención del otro, el audio se
colocaba al revés.
→ Convención única en toda la pipeline: `offset = audio_start - video_start`
(negativo = audio empezó antes). Lua coloca en
`recordFrame = clip_start + offset * fps`.

## 7. Heurística de entrevista falsa con tomas fijas
"Cámara principal + ≥60 s + estática (motion < 8)" marca como entrevista una
toma fija filmando al climber arriba del paredón.
→ El **transcript desambigua** (sin habla real = no entrevista).
→ Combinar con: carpeta `entrevista` OR transcript denso.

## 8. ImageMagick / Pillow no vienen instalados
No asumir herramientas de imagen.
→ Pre-requisito del playbook: `pip3 install --user Pillow`.
→ En macOS reciente con Homebrew Python (≥ 3.12) `pip3 install --user` falla
  por PEP 668. Agregar `--break-system-packages`:
  `pip3 install --user --break-system-packages numpy Pillow zstandard`.

## 9. Volúmenes con caracteres invisibles
El disco de ESCALANDO MEXICO tenía `U+F029` invisible en el nombre.
→ Siempre resolver con glob: `DISK=$(ls -d /Volumes/<nombre>*/ ... | head -1)`.

## 10. Setup pesado pospuesto
numpy faltaba para waveform. El modelo Whisper de 1.5 GB no estaba descargado.
→ Pre-requisitos al **inicio** del playbook, no en mitad del flujo.

## 11. `AddMarker` rechaza frames duplicados
Resolve no acepta dos markers en el mismo frame. Si dos segmentos empiezan
en frames idénticos (clamped al 2), el segundo se pierde silenciosamente.
→ Deduplicar `floor(start * fps)` antes de llamar `AddMarker`.

## 12. Audio externo no es solo entrevistas
`AUDIOS/` incluye ambientes, room tones, efectos, música — para diseño de
sonido, no solo sync.
→ Indexar todo `AUDIOS/`, **catalogar** por categoría, sync solo a la
categoría `dialogo`.

## 13. Pistas `_Tr` duplicadas
Zoom multi-track produce `_LR` (mix) + `_TrN` (un mic por pista). Transcribir
las 6 versiones es 6× el trabajo para el mismo resultado.
→ Filtrar `filename NOT GLOB '*_[Tt][Rr][0-9]*'`.

## 18. "Estos audios son distintos" — protocolo de verificación multi-método

Caso Zezzions (2026-05-25): el usuario me reportó que dos archivos eran
distintos. Mi `cmp -l` y `shasum -a 256` dijeron idénticos. El usuario
volvió a insistir. Yo era demasiado confiado en un solo método.

**Lección**: cuando alguien (usuario u otro asistente) reporta que dos
archivos son distintos y un primer método dice idénticos, **NO descartar
la observación con un solo método contrario**. Aplicar al menos 4 métodos
independientes antes de declarar la disputa cerrada:

```bash
# 1. Inodes (filesystem level)
stat -f "%i  %z  %m  %N" "$A" "$B"

# 2. Hash completo (md5 + shasum, dos algoritmos)
md5 -q "$A"; md5 -q "$B"
shasum -a 256 "$A" | awk '{print $1}'; shasum -a 256 "$B" | awk '{print $1}'

# 3. Hashes de ventanas (forzar lectura de payload, no solo header)
for off in 0 100 200; do
  dd if="$A" bs=1M count=1 skip=$off 2>/dev/null | shasum -a 256 | awk '{print substr($1,1,16)}'
  dd if="$B" bs=1M count=1 skip=$off 2>/dev/null | shasum -a 256 | awk '{print substr($1,1,16)}'
done

# 4. Comparación acústica (independiente del encoding)
for f in "$A" "$B"; do
  ffmpeg -hide_banner -nostats -i "$f" -t 30 \
    -af "astats=metadata=1:reset=5,ametadata=print:key=lavfi.astats.Overall.RMS_level" \
    -f null - 2>&1 | grep "RMS_level"
done

# 5. Buscar archivos con nombres similares en TODO el disco (puede haber
#    otra copia que el usuario escuchó pensando que era esta)
find /Volumes/<DISK> -iname "<filename>" 2>/dev/null
```

Solo si los **5 métodos coinciden** se puede declarar "los archivos en
disco son idénticos". Si la persona aún insiste que los escuchó distintos,
**la carpeta o el reproductor podría estar mostrando otra fuente** — buscar
copias con el mismo nombre en otras ubicaciones (caso fundador: el usuario
había escuchado `Pruebas Lavas/Audio/Lavas/Drecho/00001` que SÍ era
distinto del `Izq/00001` correspondiente; los del shoot real en
`Zezzions VICG/Audio 001 VICG/Lavas/` eran los duplicados).

## 17. Dual-lavalier (Rode Wireless PRO) — diagnóstico correcto
Caso Zezzions (2026-05-25): el usuario uso el Rode Wireless PRO con 2
transmisores (TX1, TX2) para capturar dos interlocutores. La estructura
esperada era `Lavas/Dr/` + `Lavas/Izq/` con contenido distinto en cada
carpeta. En la primera entrega al T9 los 9 pares estaban bit-idénticos
(8 métodos confirmaron). Yo (asistente) **especulé incorrectamente** que el
RX estaba en modo "merge". El usuario corrigió: los archivos estaban
duplicados por un error al copiar del receptor al disco — había 2 archivos
reales en el receptor pero solo se copió uno a las dos carpetas.

Después de re-volcar correctamente:
- Dr e Izq tienen tamaños y md5 distintos en los 9 pares.
- **Las duraciones difieren bastante por archivo** (ej: 00009 Dr = 60 min vs
  Izq = 5.75 min; 00005 Dr = 5.9 min vs Izq = 15.3 min).
- Dr ahora tiene 12 archivos (00001-00012) vs Izq 9 (00001-00009).
- TX1 y TX2 graban en su propia memoria con momentos start/stop
  independientes — los archivos no tienen por qué coincidir 1:1.

→ **Lecciones para el asistente:**
  1. **NO especular sobre el modo del dispositivo de grabación** cuando hay
     una explicación más simple (error de copia). Preguntar al usuario antes
     de cerrar el diagnóstico.
  2. **Esperar duraciones desiguales entre TX1 y TX2** — no exigir 1:1.
     `index_audios` y `sync_transcript` deben manejar cualquier cantidad de
     archivos en cada carpeta.
  3. **Pipeline dual-lavalier**:
     - Borrar transcripts cacheados de los IDs afectados antes de
       re-transcribir (los caches son de los archivos viejos).
     - `index_project --reindex --scope "Audio 001 VICG"` refresca size/sha
       en el manifest.
     - Borrar `audio_sync_pairs` y `question_segments` viejos de los IDs
       audio + `clip_curated_segments` de los IDs audio.
     - Re-transcribir + re-sync con `--audio-like '%lavas%'` (catchea ambas
       carpetas en una corrida).
     - Para que un video tenga 2 pares (uno con TX1 y otro con TX2): el
       sync_transcript actual elige el MEJOR match por video — necesita un
       parche para emitir top-2 si hay 2 audios con confianza alta.
     - El Lua `placeSyncAudio` debe poner el primer par en A2 y el segundo
       en A3 cuando hay 2 sync pairs para el mismo video (extensión
       pendiente del motor).

→ Modo "merge" del RX (no aplicable acá pero documentado para futuros casos):
  Si Dr/Izq transcriben el MISMO texto y se oyen 2+ voces simultáneas, el
  RX combinó TX1+TX2 en un solo mix mono. Recuperar TX separados desde la
  memoria interna de cada transmisor vía RØDE Central.

→ **La información del TX2 nunca llego al disco.** Tres causas posibles que
  el usuario debe investigar en el grabador:
  1. RX configurado en single-channel → solo grabó TX1.
  2. RX grabó ambos pero la copia al T9 trajo solo TX1 (carpeta arrastrada
     2 veces por error).
  3. Los TX1/TX2 conservan grabacion 32f local en su memoria interna —
     recuperable desde **RØDE Central app** conectada a los transmisores.

→ **Protocolo para futuras sesiones dual-lavalier** (`metodologia/sync.md`):
  - Convención de carpetas en el T9: `Lavas/TX1/` y `Lavas/TX2/` (no Dr/Izq).
  - **Antes de procesar**, verificar `cmp -l TX1/00001.WAV TX2/00001.WAV` —
     si son idénticos, **parar** y avisar al usuario para que recupere TX2
     desde RØDE Central antes de seguir.
  - Sync por transcript: correr `sync_transcript` dos veces — una con
     `--audio-like '%tx1%'` y otra con `--audio-like '%tx2%'` — y el bake
     debe colocar TX1 en A2 y TX2 en A3 sincronizados al mismo video.
  - El Lua adapta los placements: track 2 = TX1, track 3 = TX2 cuando
     ambos están disponibles para el mismo video.

→ Mientras tanto en Zezzions: el sync actual usa solo el lapel que sí está
  (lo que llamamos Dr/00001..00009). Cuando recuperes TX2, re-importar a
  `Lavas/TX2/`, re-indexar y re-correr el pipeline desde index_audios.

## 15. Rename de la carpeta del proyecto durante el procesamiento
En Zezzions (2026-05-25) el usuario renombró `Diez50/Zezzions/` a
`Diez50/Zezzions VICG/` mientras el asistente tenía background jobs corriendo
(`analyze_clips`). Resultado:
- 64 errores "file not found" en `analyze_clips` (los clips indexados con el
  path viejo dejaron de existir cuando llegó su turno de análisis).
- Siguientes scripts (`sync_transcript`, etc.) fallaron con
  `unable to open database file` porque mi shell aún apuntaba al path viejo.

El `.cinema_assistant/` se movió con la carpeta (era subdir del padre), así
que los transcripts cacheados y el manifest sobrevivieron — solo las rutas
absolutas en `clips.path` quedaron obsoletas. Reparación rápida:

```sql
UPDATE clips
SET path = REPLACE(path, '/Volumes/.../Zezzions/', '/Volumes/.../Zezzions VICG/')
WHERE path LIKE '/Volumes/.../Zezzions/%';
```

`rel_path` no cambia (es relativo al root). `audio_sync_pairs` tampoco (guarda
IDs). Después del UPDATE, re-correr lo que falló por path (`analyze_clips`
para los `missing`) y seguir.

→ Antes de batch largos, **fijar la ruta canonical del proyecto y pedirle al
  usuario que no la renombre mientras se procesa**. Si pasa, hacer el UPDATE
  arriba y re-evaluar qué pasos hay que repetir.

## 20. Tablas SQL "satelitales" requeridas por scripts downstream
Caso Zezzions (2026-05-26): al correr `describe_segments_local_llm.py` por
primera vez en un proyecto nuevo, el script trona con `no such table:
segment_objects` / `face_detections` / etc. Cada paso del pipeline downstream
asume que los scripts upstream del flow completo de Jilotepec ya se corrieron
(YOLO objects, pose detection, face detection, face catalog, identity
attribution). En un primer pass sin todos esos modelos pesados, las tablas
no existen.

→ **Solución pragmática**: crear las tablas VACÍAS al inicio del proyecto:

```sql
CREATE TABLE IF NOT EXISTS segment_objects (seg_id INTEGER, label TEXT, conf REAL, source TEXT);
CREATE TABLE IF NOT EXISTS segment_poses (seg_id INTEGER, pose TEXT, conf REAL);
CREATE TABLE IF NOT EXISTS face_detections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER, frame_t_sec REAL,
  face_idx INTEGER, bbox TEXT, det_score REAL, embedding BLOB,
  crop_path TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS face_catalog (
  identity_id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_name TEXT UNIQUE,
  aliases TEXT, notes TEXT, n_detections INTEGER, n_clips INTEGER,
  centroid BLOB, cluster_id INTEGER, updated_at REAL);
CREATE TABLE IF NOT EXISTS face_identities (
  detection_id INTEGER PRIMARY KEY, identity_id INTEGER, confidence REAL);
CREATE TABLE IF NOT EXISTS face_attributions (
  clip_id INTEGER, face_idx INTEGER, identity_name TEXT, confidence REAL,
  source TEXT, updated_at REAL);
```

LEFT JOIN a tablas vacías = NULL → el script downstream sigue su flujo sin
esos features. Si después se quiere correr el modelo (YOLO, InsightFace),
las tablas se llenan en su lugar.

→ **Pendiente del motor**: que los scripts downstream creen sus propias
  tablas con `CREATE TABLE IF NOT EXISTS` antes de hacer SELECT (ya lo hacen
  varios, pero no todos).

## 16. `clip_descriptions` / `clip_characters` requeridas por curate y bake
`curate_segments.py` y `build_metadata_payload.py` esperan que existan las
tablas `clip_descriptions`, `clip_shot_values`, `clip_angles`, `clip_characters`
aunque no estén pobladas (LEFT JOIN). En un proyecto nuevo recién indexado
esas tablas no existen y los scripts truenan con `no such table`.

→ Crear las tablas vacías al inicio del flujo (parte de la metodología de
  proyecto plano) o garantizar que `derive_shot_value`, `derive_camera_angle`,
  etc., se hayan corrido (todos crean sus tablas con `CREATE TABLE IF NOT
  EXISTS`). Para skip el LLM, basta con dejar `clip_descriptions` vacía.

## 22. Hardcodes geográficos en queries SQL fallan silenciosamente
ESCALANDO MEXICO (2026-05-25): `analyze_segments.py:181` tenía
`WHERE rel_path LIKE 'ESCALANDO MEXICO/JILOTEPEC/%'` hardcodeado. El log
reportaba `"Multi-signal segment analysis on N JILOTEPEC clip(s)"` — sin
gritar "esto cubre 1 de 5 sectores". Resultado: 6 sectores se quedaron sin
`clip_segments`, y como `curate_segments` depende de eso, también sin
tramos curados, sin descripciones LLM, sin markers en Resolve. El usuario
percibió "falta el sync y los duration markers de las entrevistas".
→ Scripts que procesan multi-sector deben aceptar `--sector` **opcional**
  (default = todos) y nunca hardcodear el filtro. El linter
  `bin/lint_pipeline.py` ahora caza estos patrones literales
  (`JILOTEPEC`, `GUADALAJARA`, etc.) cerca de `LIKE` / `rel_path` /
  `video-folder` y aborta el pipeline si los encuentra.

## 23. DELETE global en tablas multi-sector destruye trabajo entre corridas
Mismo proyecto: `sync_transcript.py:125` hacía `DELETE FROM audio_sync_pairs`
antes de insertar. Al correrlo sector por sector, cada iteración mataba los
pares del sector anterior. Detecté el daño cuando GUANAJUATO borró los 11
pares de JILOTEPEC. Mismo patrón latente en `sync_audio.py:190`
(`DROP TABLE IF EXISTS audio_sync_pairs`).
→ En cualquier script con flag de scope (`--location`, `--sector`,
  `--video-folder`, etc.), el DELETE debe ser **selectivo por scope**, no
  global:
  ```python
  conn.execute(
      "DELETE FROM audio_sync_pairs WHERE video_clip_id IN ("
      " SELECT id FROM clips WHERE lower(rel_path) LIKE ?)",
      (f"%{args.location.lower()}%",))
  ```
  Lint pipeline ya caza estos; permite override con
  `# lint:ok delete-global` cuando el script no tiene flag de scope.

## 24. Métricas de cobertura con denominador inflado
Mismo proyecto: el reporte inicial decía "17% de cobertura en
`clip_curated_segments`". La realidad era **98% por sector**. El denominador
contaba **audios + videos** mientras el numerador solo refleja videos. Mi
diagnóstico inicial fue equivocado y casi pongo a re-procesar 6 horas de
trabajo innecesario.
→ Cualquier ratio porcentual filtra denominador al mismo dominio que el
  numerador. Para clips de video: `WHERE c.file_kind='video'` en ambos
  lados del JOIN. `bin/verify_coverage.py` ahora aplica este filtro
  uniformemente y reporta PASS/FAIL por sector × por tipo de procesamiento.

## 25. La causa raíz no es el primer cuello de botella visible
Mismo proyecto: el usuario percibió "faltan duration markers". Mi diagnóstico
inicial fue "falta procesamiento del pipeline". La causa real era una cadena
de dependencias ocultas:
`clip_descriptions.category` vacío en 4 sectores → `derive_question_segments`
filtra por `category='entrevista'` → 0 preguntas en esos sectores → 0
Purple Q markers. La solución no era re-correr scripts sino **crear un
derivador que no existía** (`bin/derive_video_categories.py`) para asignar
`category='entrevista'` automáticamente a videos con `entrevista` en su
rel_path.
→ Antes de aplicar fixes a gran escala, **mapear las dependencias entre
  pasos** del pipeline. Si un paso depende de una columna y esa columna está
  vacía, falla silenciosamente. El verifier de cobertura por tipo de
  procesamiento expone estas brechas (`question_segments_video: 0 OK` vs
  `clip_curated_segments: 98% OK` revela que el problema NO es curaduría
  sino preguntas — y eso señala hacia categorías faltantes).

## 26. Defaults de modelo Ollama no coinciden con lo instalado
ESCALANDO MEXICO (2026-05-26): `describe_segments_local_llm.py` tenía
`DEFAULT_MODEL = "qwen2.5vl:7b"` pero el modelo descargado era `:3b`.
Resultado: 1878 tramos fallaron con HTTP 404 silencioso (el script registró
`fail` pero no abortó). Detecté el problema al revisar el log al final
("Done. ok=0 skip=10 fail=1878").
→ Defaults de scripts deben **coincidir con lo realmente instalado** en el
  entorno objetivo, no con "lo recomendado teóricamente". Mejor todavía:
  el script debe hacer un `ollama list` o `/api/tags` al inicio y fallar
  fuerte con mensaje claro si el modelo configurado no está disponible.

## 27. `nohup` + `logging.info` sin flush oculta vivacidad
Mismo proyecto: lancé `describe_segments_local_llm.py` en background con
nohup. El log permaneció **vacío durante 30+ min** mientras el script
realmente procesaba (109 tramos descritos visible vía DB). Casi diagnostico
"murió" cuando estaba sano.
→ Para procesos largos en background usar **`python3 -u`** (unbuffered) o
  `PYTHONUNBUFFERED=1`, o configurar `logging.StreamHandler` con flush
  explícito. Y al chequear estado de un proceso en background, **medir via
  los efectos en la DB**, no solo via tail del log.

## 28. Caffeinate obligatorio para procesos > 30 min en background
Mismo proyecto: `analyze_segments` murió silenciosamente a las **6 horas**
(1425 de 1793 clips procesados, log limpio sin errores). El laptop entró en
sleep. nohup no es suficiente — protege contra hangup del shell, no contra
power management.
→ Cualquier proceso de cómputo >30 min en background se lanza envuelto en
  `caffeinate -i` (idle sleep) o `-dims` (display + idle + mouse + system).
  Patrón: `nohup caffeinate -i python3 script.py ... &`.

## 31. `attribute_faces_via_transcript` sobrescribe curaduría manual
En Zezzions (2026-05-26): después de mejorar el script con fallback "clip
sin questions → usar clip_characters", el clip 2573 (ENTREVISTADO_5 hermano de
ENTREVISTADO_11 — entrevista sin audio externo, curado manualmente con identidad
clara) fue **sobreescrito** con "ENTREVISTADO_13 (~4)" porque el transcript de un
clip vecino mencionaba a ENTREVISTADO_13 y el voto por face_attributions ganó.

Causa raíz: el código hacía `INSERT OR REPLACE INTO clip_characters` sin
distinguir entre filas curadas manualmente (sin formato `(~N)`) y filas
generadas por el propio script (con `(~N)`).

→ **Fix**: si `clip_characters.characters` NO contiene ningún `(~`, es
  curaduría manual y se PRESERVA. Solo se actualiza si el contenido ya
  tenía formato auto-asignado del script anterior, o no había nada.

→ **Patrón generalizable**: cualquier script que escriba en una tabla
  compartida con curaduría humana debe DETECTAR si la fila actual es
  manual (marcador específico o ausencia del propio marcador del script).
  La regla "auto siempre gana" destruye trabajo del usuario.

## 30. `placeSyncAudio` ignora `recordOffsetFrames` cuando offset > 0
En Zezzions (2026-05-26) el usuario reportó "las entrevistas tienen delay,
la entrevista de ENTREVISTADO_1 está mal sincronizada". Causa raíz: el código
en `asistente_*.lua` para `offset > 0` no desplazaba el `recordFrame` —
solo manejaba bien el caso `offset < 0`.

Convención del manifest: `offset = audio_start - video_start`.
- `off < 0`: audio empezó antes del video. Skipear los primeros `|off|`s del
  audio. recordFrame = inicio del video clip. **Correcto en código v1.**
- `off > 0`: audio empezó **después** del video por `off` segundos. Los
  primeros `off`s del video no tienen lavalier. Audio debe colocarse en la
  timeline **desplazado `off`s a la derecha** del recordFrame del video.
  **Roto en código v1**: `recordFrame = item:GetStart()` sin sumar
  `off*fps`. Resultado: el audio aparece `off`s ANTES de donde debe, el
  labio dice "Buenas noches mi nombre es Jorge" pero el audio sale antes
  que la voz visible → lip-sync delay perceptible.

Entrevistas afectadas en Zezzions (offset > 0 + conf ≥ 0.30):
- 2644 ENTREVISTADO_4: offset +45s (00006 Izq) + 162s (00001 Dr)
- 2794 ENTREVISTADO_1: offset +14.8s — caso documentado por usuario
- 2786 ENTREVISTADO_2: offset +3.4s — delay leve
- 2796 ENTREVISTADO_8+ENTREVISTADO_7: offset -1.7/-1.8s → no afectada

Fix aplicado simultáneamente en `asistente_zezzions.lua` y
`asistente_jilotepec.lua`. Patrón:

```lua
local recordOffsetFrames = 0
if off < 0 then
  sourceStart = math.floor((-off) * fps)
  sourceEnd   = sourceStart + math.floor(math.min(vdur, adur + off) * fps)
else
  sourceStart = 0
  sourceEnd   = math.floor(math.min(vdur - off, adur) * fps)
  recordOffsetFrames = math.floor(off * fps)  -- ← LO QUE FALTABA
end
local recordFrame = math.floor(item:GetStart()) + recordOffsetFrames
```

→ **Cualquier asistente futuro debe heredar este patrón completo**. Está
   ahora en doctrina del meta-pattern (`architecture_assistant_pattern.md`
   pendiente de actualizar).

→ **Test obligatorio para sync**: cuando hay entrevistas con offset > 0,
   abrir Resolve y verificar lip-sync con el playhead en una zona donde
   la persona esté hablando con la boca abierta. Si labios desincronizados
   con audio, debugear `recordFrame`/`sourceStart`. No basta con luac -p.

## 29. Verifier conservador deja pasar entrevistas con preguntas atípicas
En Zezzions (2026-05-26 — después de implementar `verify_interviews.py`)
el usuario encontró visualmente en Resolve la entrevista 2645 (ENTREVISTADO_13 y ENTREVISTADO_10,
322s, dual-lavalier) sin sync ni categoría. Mi verifier había reportado
"0 entrevistas potenciales" — falso negativo.

Causa raíz: la lista `QUESTION_PATTERNS` v1 era específica a las preguntas
que YO había observado en las primeras entrevistas (Sessions GPI, Jorge
Pico, ENTREVISTADO_2), no exhaustiva del estilo Sessions. El transcript de
2645 contenía:
- "¿Cómo se conocieron?" → no en patterns
- "¿Y cuándo empezaron a colaborar?" → matchea `¿[Cc]uándo empez`
- "¿Qué prefieren grabar o tocar?" → matchea pero parcial
- "¿Cuáles son sus inspiraciones?" → no en patterns

Solo 1 patrón matched; el default `min-questions=2` lo descartó.

→ **El verifier no es un oracle — su valor es la cobertura empírica de
  patrones**. Cada proyecto nuevo trae giros lingüísticos del entrevistador
  (formal, coloquial, en otra región). Los patterns deben construirse
  **leyendo entrevistas-confirmadas del proyecto actual** antes de cerrar.

→ **Multi-señal mejor que mono-señal alta**: una sola pregunta + audio
  activo + dur ≥ 120s + transcript no-alucinado = confluencia de 4
  señales débiles, más fiable que 2 patrones de pregunta sin contexto.
  Fix aplicado en `verify_interviews.py` v2.

→ **Detectar entrevistas-sin-sync explícitamente**: clip 2573 (ENTREVISTADO_5,
  organizador) está marcado entrevista pero no tiene audio externo
  grabable (sin lavalier). El verifier debe reportar esos casos como
  "entrevista-solo-video" para que el editor confirme — no es bug,
  pero es información ausente del Lua/markers.

→ **Auditoría exhaustiva final** (paso 12c añadido al playbook): después
  de los verifiers, hacer audit ad-hoc de TODO el material ≥ 30s con
  audio + transcript no-alucinado que no encaje en ninguna categoría.

## 14. SHA-256 idéntico ≠ autorización para descartar
En Zezzions (2026-05-26) las 9 grabaciones de Wireless PRO en `Lavas/Dr/`
salieron SHA-256 idénticas a las de `Lavas/Izq/`. El asistente declaró
unilateralmente que Izq era "dup descartable" y propuso transcribir/syncar
solo Dr. El usuario corrigió: el contenido *debería* ser distinto (son dos
lavaliers separados); los archivos en disco quedaron iguales por un error
en la transferencia desde el grabador, no por intención.
→ **El SHA solo describe los bytes en disco hoy, no autoriza descartar un
  archivo.** Si dos rutas distintas comparten SHA, indexar ambas como
  independientes (por ruta absoluta, según patrón #2) y dejar que el sync
  las trate por separado. Si el caso huele a copia rota desde el grabador,
  **avisarle al usuario con la evidencia** (lista de pares + SHA) y dejar
  que él decida — nunca skipear archivos del proyecto silenciosamente por
  parecido binario.

## 34. Asumir hermanos lavalier por número de archivo

Caso Zezzions iter4 (2026-05-26). `lavalier_pairs` se construía
emparejando `Dr/0000N.WAV` con `Izq/0000N.WAV` por mismo N. **Falso**
cuando uno de los dos TX se apaga durante el shoot — los contadores
se desincronizan independientemente:

- Mi asunción: `Dr/00007 ↔ Izq/00007` (ENTREVISTADO_13/ENTREVISTADO_10 por mi voice_match)
- Realidad por timestamp: Dr/00007 grabado 23:27 (ENTREVISTADO_5 DJ), Izq/00007
  grabado 21:20 (ENTREVISTADO_13/ENTREVISTADO_10). **Audios completamente independientes**.

Esto causó:
- 2645 (ENTREVISTADO_13/ENTREVISTADO_10, video 21:12) recibió sync con audio Dr/00007 (ENTREVISTADO_5 DJ
  a las 23:27). Físicamente imposible.
- `voice_match` eligió mal porque tomaba "el de mismo número en la otra
  carpeta" como candidato natural.

→ **`lavalier_pairs` se construye por TIMESTAMP** (`os.stat.st_mtime`):
  cross-match Dr×Izq exigiendo `|Δmtime| ≤ 120s`. Verificar contenido
  con 4-grama overlap del transcript ≥ 0.20. Calcular delta empírico
  con **transcript ngram-alignment** (no envelope — falla con música).
  Ver doctrina en `patrones-exitosos.md` § "Identidad de hermanos
  lavalier por TIMESTAMP".

## 35. Ground truth importado sin validación temporal

Caso Zezzions iter4. `import_drt_ground_truth.py` resolvía paths del
`.drt` por basename + carpeta parent (`Dr/00007.WAV`). Cuando el editor
reorganizó archivos físicos entre exports del `.drt` (T7 → T9 con
renumeración), el match por basename apuntaba a un archivo distinto.

- `.drt` decía 2645 ↔ Dr/00007 con offset -19.25.
- En T7 (donde el editor armó), Dr/00007 probablemente era ENTREVISTADO_13/ENTREVISTADO_10.
- En T9 (donde corro), Dr/00007 es ENTREVISTADO_5 DJ grabado 2h después.
- Mi importador aceptó el match silenciosamente.

→ **Validar plausibilidad temporal** al importar GT:
  ```python
  audio_end = os.stat(audio_path).st_mtime
  audio_start = audio_end - audio_dur_sec
  if not (audio_start - 60 <= video.creation_time <= audio_end + 60):
      reject(reason="Audio no contiene al video temporalmente")
  ```
  Si rechaza, escribir a `sync_candidates.status='needs_review'` para
  que el usuario decida.

## 49. Detección de preguntas que exige `¿?` + categoría incompleta + verbo por substring

Caso Zezzions iter10 (2026-05-28). La entrevista **solo-video 2573**
(ENTREVISTADO_5 organizador, sin lavalier, A1 limpio) tenía **0 Purple Q**. Tres
bugs encadenados, todos genéricos:

1. **El detector exigía `¿...?`.** `RE_QUESTION = r"¿([^?¿]+)\?"` solo
   capturaba preguntas con ambos signos. Whisper **omite el `¿` de apertura**
   en preguntas del entrevistador grabadas en el audio de cámara. El prompt
   real *"Pero igual de cómo empezaste a echarle la mano…"* nunca se extraía.
   → Fix: `lib/question_filter.py` detecta también oraciones con interrogativo
   **acentuado** al frente. El acento es el ancla: en español el interrogativo
   lleva tilde (`cómo`) y el relativo no (`como`); Whisper respeta la distinción.

2. **`entrevista-solo-video` no estaba en el WHERE** de
   `derive_question_segments.py`. Aunque el detector funcionara, el clip nunca
   se procesaba. → Fix: agregar la categoría (y `dialogo-audio`) a la query.

3. **`has_verb` por substring** (`any(v in norm for v in INTERVIEW_VERBS)`):
   `"es"` matcheaba dentro de `"este"`, `"eventos"`, `"emergente"`. Eso dejaba
   pasar monólogo del entrevistado como falsas preguntas ("Como dice, pues,
   todos somos…"). → Fix: match por **palabra completa** (set intersection);
   substring solo para frases con clítico ("te dedicas").

**Bug gemelo (falso negativo) del whitelist de verbos:** 2596 perdió
*"cómo te encargas de promover los eventos"* porque "encargas" no estaba en
`INTERVIEW_VERBS`. → Cuando un proyecto pierda una pregunta real por un verbo
no listado, **agregar el verbo** a la lista curada, NO relajar el filtro.

**Lección de método:** al endurecer/relajar CUALQUIER filtro de markers,
re-correr y **comparar conteos por clip antes/después**. Cada delta negativo
se inspecciona: ¿era ruido (filler/monólogo/dup) o pregunta real? En Zezzions
el total bajó 94→80 sin perder ninguna pregunta real. Confiar en el conteo
total agregado oculta regresiones puntuales.

**Principio:** precisión > recall en markers. Un Purple basura cuesta más
que una pregunta perdida — el editor confía en cada salto.

## 48. Abandonar clips con A1 alucinado pierde entrevistas con lavalier limpio

Caso Zezzions iter9.7 (2026-05-27). El usuario mostró una entrevista de
**11:29 minutos con 3 personas** que mi pipeline había completamente
ignorado (clip 2644). Causa raíz:

1. **A1 (audio cámara) alucinado**: Whisper transcribió `"qué es piso
   estudio"` ×184 veces sobre música ambient. Solo 41 palabras limpias
   en 13.65s antes de la basura.
2. **Marcado como `entrevista-degradada`** por `transcript_quality.is_hallucinated=1`.
3. **`verify_interviews.py` lo excluía** con `if looks_hallucinated(text): continue`.
4. **`compute_sync_offset.py` y `derive_question_segments.py`** también
   excluyen entrevistas degradadas.
5. **PERO**: el clip tenía **36 caras detectadas** + duración 689s. Y
   había **2 audios externos disponibles sin sync** (Dr/00004 e Izq/00006,
   ambos 644s, hermanos del mismo Wireless PRO RX a las 21:12:42).

→ **El A1 estaba contaminado por música, pero los lavaliers eran limpios**.
  Al re-transcribir Dr/00004 con prompt contextual: 1148 palabras de
  conversación coherente sobre **Piso Estudio**, **Sessions**, **Mau**,
  Ferdi, Andresa, "nosotros tres".

→ **6 preguntas reales detectadas** después del rescate:
  "Y hace cuánto empezaron en el negocio", "Cómo conocieron Sessions",
  "Cómo se conocieron ustedes", "Cómo llegaron con Piso Estudio", etc.

### Fix metodológico

**Regla operativa nueva** (`verify_interviews.py` iter9.7):

```python
if is_hallucinated:
    # NO abandonar — verificar si hay señal visual
    if n_face_detections >= 20 AND duration_sec >= 120:
        # Buscar audios externos NO sincronizados con duración similar
        # Reportar como "rescate posible — probar waveform A1↔candidatos"
        report_rescue_candidate(...)
    continue
```

### Heurística generalizable: "señal visual contradice transcript pobre"

Cuando dos señales del asistente disagree sobre si un clip es entrevista:
- **Transcript**: dice basura → categoría `entrevista-degradada`
- **Faces detectadas**: dice >=20 personas en cuadro

**Confiar en faces + duración + audio activo (rms > -45dB)**. La señal
visual NO miente — si hay 36 caras en 689s de video, hay habla humana
ahí, aunque Whisper no la haya transcrito en el A1.

### Pasos del rescate (operacional)

1. Marcar el clip como CANDIDATE en verify_interviews (no skip).
2. Listar audios externos con `n_syncs=0 AND duration_sec >= clip_dur*0.5`.
3. Probar `waveform_sync_full` (A1↔candidato) — el A1 alucinado AÚN
   tiene su envelope de energía RMS real, sirve para waveform sync.
4. Si prominence ≥ 0.30: aplicar sync.
5. Re-transcribir el audio externo con prompt contextual.
6. Build master transcript (combina A1+lavaliers ya con sync).
7. Re-categorizar de `entrevista-degradada` a `entrevista`.
8. Re-derive question_segments sobre master.
9. Re-export Lua.

## 47. Tratar transcript como evento atómico en vez de proceso iterativo

Caso Zezzions iter9.7 (2026-05-27). Feedback adicional del usuario:

> *"El transcript debe hacerse combinando el audio de la cámara con los
> audios externos. Es muy importante que revises el transcript y lo
> corrijas los errores a partir del contexto que tienes de todo el
> proyecto."*

**Patrón anterior (incorrecto)**: transcribir UNA VEZ con Whisper raw
sobre cada archivo individual, después usar esos transcripts tal cual
para todo el pipeline. Limitaciones:
- Whisper sin contexto produce errores en nombres propios y jerga
- Cada archivo se procesó aislado, sin info de los demás
- El master transcript se construyó como paso tardío opcional

**Patrón correcto (iter9.7)**: el transcript es un **PROCESO ITERATIVO
DE 4 CAPAS** que enriquece con cada capa:

| Capa | Output | Usa info de |
|---|---|---|
| 1 | Raw Whisper por archivo | nada (Whisper aislado) |
| 2 | Corregido con vocabulario | cast detectado, términos del dominio |
| 3 | Master combinado | sync entre A1 y lavaliers |
| 4 | Re-derived entities | corpus completo del proyecto |

**Implicación práctica**: cada vez que el proyecto descubre un nombre
nuevo (cast), un término del dominio, o un sync correcto, **re-correr
la corrección de vocabulario** sobre los transcripts existentes. No
hay un "transcript final" — el transcript SE MEJORA durante todo el
proyecto.

→ **Doctrina generalizable**: cualquier dato que el asistente derive
de fuentes inciertas (Whisper transcripts, LLM descriptions, face
clustering) debe tratarse como CAPA REVISABLE, no como ground truth.
Cada paso posterior puede corregir capas previas.

## 46. Procesos del asistente NO se retroalimentan entre sí

Caso Zezzions iter9.6 (2026-05-27). Feedback del usuario al cierre:

> *"Todavía tienes muchas fallas en tu metodología. Creo que no estás
> optimizando los procesos para que se retroalimenten entre sí."*

**Patrón identificado**: durante el desarrollo iterativo, agregué
componentes nuevos (master transcript, waveform sync, vocabulario,
filtros) pero cada uno operaba en aislamiento. El pipeline no
aprovechaba que un paso enriquece al siguiente. Ejemplos:

- Sync se hacía ANTES de transcribir audios completos → menos anchors disponibles
- Marcadores se generaban SIN verificar transcript primero → ruido
- Vocabulario del proyecto NO alimentaba el re-transcribe
- Identidad por cara NO alimentaba el prompt contextual de Whisper

→ **Doctrina del orden correcto** (instrucción literal del usuario):

```
1. INDEX + cámaras + audio externo (rápido)
2. TRANSCRIBIR todo (A1 video + audio externo) ← PRIMERO
   ↓ alimenta:
3. AUDIT transcripts (clean / hallucinated / needs_retranscribe)
4. DETECTAR entrevistas (transcript denso + filtro alucinaciones)
5. SYNC POR TRANSCRIPT (n-gramas anchor; usa transcripts ya listos)
6. CORROBORAR CON WAVEFORM (DaVinci auto-sync; verifica el offset
   por método físico independiente)
7. MASTER TRANSCRIPT por entrevista (combina A1+lavaliers)
8. QUESTION DETECTION sobre master
9. MARCADORES sólo en clips validados con transcript
   (entrevistas + escenas con diálogo)
10. EXPORT Lua + Resolve
```

→ **Regla operativa**: antes de implementar cualquier paso nuevo,
preguntarse "¿qué outputs de pasos previos puedo aprovechar para hacer
esto mejor?". El motor ahora tiene MUCHOS datos por proyecto — usarlos.

## 44. Waveform A1↔lavalier FALLA en entrevistas duales

Caso Zezzions iter9.5/9.6 (2026-05-27). El sync por waveform A1↔lavalier
funciona excelente para UN solo entrevistado (prominence 0.50-0.78) pero
**falla en entrevistas duales** (dos personas):

- 2645 ENTREVISTADO_13/ENTREVISTADO_10: prom=0.50 (Dr) y **0.01** (Izq) — el Izq dio basura
- 2796 ENTREVISTADO_8+ENTREVISTADO_7: prom=0.39 (Dr) y **0.29** (Izq) — ambos mediocres

**Razón física**: el A1 de cámara captura AMBAS voces mezcladas. El
lavalier individual captura principalmente UNA voz. El patrón de energía
RMS del A1 (ENTREVISTADO_13+ENTREVISTADO_10 hablando alternado) NO matchea con el patrón del
lavalier (solo ENTREVISTADO_13 o solo ENTREVISTADO_10). Cross-correlation encuentra peak
espurio o muy débil.

→ **Fix iter9.6**: skip waveform A1↔lavalier para videos con
  `n_personas >= 2` en face_catalog. Para esos pairs:
  1. Usar sibling sync directo Dr↔Izq por waveform (más confiable
     porque ambos lavaliers tienen señal similar del MISMO entorno).
  2. O usar transcript ngram-align.
  3. O preservar offset manual del editor.

## 45. lavalier_pairs.applicable=1 falsamente por transcript overlap alto

Caso Zezzions iter9.6. Mi `lavalier_pairs` marcaba Dr/00005 ↔ Izq/00007
como `applicable=1` con `delta_sec=0.000s` por transcript ngram-align.
Pero waveform_sync_siblings reveló que NO son hermanos del mismo Wireless
PRO RX: delta varía -14s a -33s con prom 0.02-0.08 (basura, peak
espurio).

**¿Por qué transcript dijo applicable?** Porque ambos audios capturan
A LAS MISMAS PERSONAS (ENTREVISTADO_13/ENTREVISTADO_10) diciendo las mismas palabras desde
distintas posiciones. El overlap de 4-gramas era 0.54 (alto). Pero
**eso NO significa que sean hermanos del mismo recorder hardware** —
significa que comparten contenido sonoro.

→ **Validación adicional** (`bin/build_lavalier_pairs.py`):
  Para confirmar hermanos del mismo RX:
  ```python
  # Después de transcript-based delta:
  wave_r = waveform_sync_siblings(...)
  if wave_r.prominence >= 0.30 AND |wave_r.delta_sec - transcript_delta| < 0.5:
      applicable = 1  # Hermanos verdaderos del mismo RX
  elif transcript_delta_n_anchors >= 100 AND mtime_diff < 60:
      applicable = 1  # Probable hermanos (timestamp + contenido coincide)
  else:
      applicable = 0  # Solo contenido compartido, no hardware
  ```

→ **Doctrina**: lavalier hermanos verdaderos tienen **TRES indicadores**:
  1. `|Δmtime| ≤ 120s` (mismo RX cerró WAVs casi al mismo tiempo)
  2. `transcript_overlap_4grama ≥ 0.20`
  3. **`waveform_sibling.prominence ≥ 0.30 AND |delta| < 1s`** (mismo RX
     → reloj de hardware compartido → delta < 100ms)

## 43. Re-transcribir con prompt contextual a veces ALUCINA en favor del contexto

Caso Zezzions iter9.3 (2026-05-27). Whisper.cpp con `--prompt "Entrevista a
ENTREVISTADO_3..."` produjo transcripciones MEJORES en algunos casos
(ENTREVISTADO_5: puntuación correcta, "Avicii" en lugar de "Vici") pero PEORES
en otros:

- ENTREVISTADO_3 146: cambió "gato" por "momento", agregó basura "-h. -. -." y
  "ch chino" — Whisper alucinó palabras inglesas/raras tratando de
  encajar el contexto del prompt.
- ENTREVISTADO_13/ENTREVISTADO_10 149: agregó "What's up, what's up?", "Sup, sup, sup", "Alex
  pobre, cuando ya está listo" — frases en inglés no presentes en el audio.

**Lección**: el prompt contextual de Whisper es un **arma de doble filo**.
Mejora la transcripción cuando hay incertidumbre del modelo, pero también
puede llevarlo a "completar" lo que cree que debe escuchar con basura del
prompt.

→ **Política safer**: re-transcribir con prompt SÍ, pero validar con
  detector de regresión:
```python
# Patrones de alucinación con contexto musical/entrevista
GARBAGE_PATTERNS = [
    r"-h\.", r"-\.", r"ch chino", r"sup,? sup", r"jeem", r"alex pobre",
]
ENG_PATTERNS = [
    r"\bwhat's\b", r"\blet's\b", r"\byou know\b", r"\bsup\b",
]
# Si NEW transcript matches ANY → REVERTIR a legacy_pre_context_*.json
```

En Zezzions iter9.4 de 13 audios re-transcritos, 2 se revirtieron por
estos criterios (ENTREVISTADO_3, ENTREVISTADO_13/ENTREVISTADO_10). Los otros 11 mantuvieron mejora.

→ **Más seguro aún**: usar SOLO el corrector de vocabulario sin
  re-transcribir. Mejora ~5-10% de palabras (Vici→Avicii, etc.) sin
  riesgo de alucinación. Re-transcripción con prompt: solo si el modelo
  de Whisper actualmente da output muy malo.

## 42. Concatenar palabras de varias fuentes mezcla voces

Caso Zezzions iter9.0 (2026-05-27). Mi primer master_transcript hacía
`text = " ".join(words)` mezclando A1 + lavalier_Dr + lavalier_Izq
ordenados por tiempo. Al detectar preguntas con `split_questions(text)`,
las palabras del entrevistador y del entrevistado quedaban PEGADAS en
el texto y el regex producía preguntas absurdas:

> "Cómo soy mi ENTREVISTADO_13 niño y aquí estaba perdido, mi niño perdido"

(Era "¿Cómo se conocieron?" del entrevistador + "Soy ENTREVISTADO_13 y aquí estaba
mi niño perdido" del entrevistado.)

**Lección generalizable**: cuando combinas fuentes de audio sincrónicas
(A1+lavaliers), conserva la **separación por source** para análisis
semántico. La fusión solo es válida para ANCHOR detection (sync) o
análisis ESTADÍSTICO (frequency counts).

→ **Fix iter9.2**: `master['by_source']` con text+words por cada fuente
  individual. Detección de preguntas por source, deduplicación entre
  sources con ventana dinámica.

→ Aplica también a: cualquier análisis que requiera FRASES COMPLETAS
  (descripciones LLM, sentiment analysis, named entity extraction).

## 41. Markers Green/Purple basados en transcript alucinado

Caso Zezzions iter8 (2026-05-27). 56 de 282 `clip_curated_segments` tenían
su `full_text` derivado de un transcript donde `is_hallucinated=1`. Esos
markers mostraban en Resolve:
- Personajes inventados (Whisper produjo nombres random)
- Acciones que no ocurrían en el video
- Frases clave de "Suscríbete al canal" tomadas como contenido editorial

**Bug raíz**: `bin/describe_segments_local_llm.py` y curaduría manual
de Claude no verificaban `transcript_quality.is_hallucinated` antes de
escribir `full_text`. El editor abrió el clip esperando ver lo que decía
el marker — y vio otra cosa. Pérdida de confianza en el asistente.

→ **Fix**: `bin/export_lua_data.py` filtra al export. Lee
  `transcript_quality.is_hallucinated=1` y NO incluye markers Green ni
  Purple Q de esos clips. Otros markers (Red de descarte, Cyan de sync,
  Yellow de PANNs) sí siguen porque no dependen del transcript.

**Doctrina generalizable**: cualquier dato derivado del transcript debe
verificar la quality del transcript ANTES de exponerlo. Patrón:
```python
if transcript_quality.is_hallucinated == 1:
    skip()  # NO usar transcript de este clip para descripciones, sync, identidad
```

Esto aplica también a:
- `derive_question_segments` (ya respeta `is_hallucinated`)
- `attribute_faces_via_transcript` (ya lo hace)
- `compute_sync_offset` (usar clean_until_sec para truncar zonas alucinadas)
- LLM-based segment description (debe ignorar clips alucinados)

## 40. Depender del .drt del editor como muleta para validar sync

Caso Zezzions iter5-7. El editor exportó un `.drt` con su sync manual.
Lo usé para:
- Detectar fps de la timeline (decodifiqué MediaFrameRate hex)
- Identificar qué audio corresponde a qué video (.drt me lo decía)
- Validar mis offsets (comparar fps fix vs .drt)
- Detectar audio equivocado (2645 .drt vs físico)

**Problema**: en proyectos futuros sin `.drt`, no tendré esa muleta.
Y dependerla ocultó que mi pipeline tenía bugs sistemáticos que solo se
hicieron visibles cuando el .drt me confrontó con la realidad.

→ **Doctrina nueva**: `metodologia/sync-sin-ground-truth.md`. El motor
  debe poder llegar al sync correcto SIN ground truth, usando solo:
  - Detección de fps del manifest (todos los videos al mismo fps)
  - Voice-first matching (face_voice_links + voice_catalog)
  - Transcript ngram-align con análisis por segmentos
  - Validación temporal (audio_mtime debe contener video.creation_time)
  - Convergencia multi-método (transcript + envelope + voice)
  - **Política "no escribir si no hay confianza"**: mejor `needs_review`
    que sync silenciosamente incorrecto.

## 38. Refinement por transcript en clips con multi-take del audio

Caso Zezzions iter7 (ENTREVISTADO_4 2671). El audio Dr/00006 contiene 3+ tomas
de ENTREVISTADO_4 diciendo lo mismo. El video monta fragmentos de cada toma.

**Síntoma**: `bin/refine_pairs_by_transcript.py` calcula offset físico
con mediana sobre todos los anchors comunes. Mezcla anchors de TOMAS
DIFERENTES y produce offset que NO sirve para ninguna toma.

**Detección automática**: `bin/verify_sync_physical.py` calcula
`multitake_score` = % de 4-gramas comunes que aparecen >1 vez en el
audio. Umbral 10% para clasificar como multi-take.

**Política**: clips con multi-take NO se refinan automáticamente. Se
preserva offset del .drt manual del editor. Se reporta al usuario para
ajuste fino por segmentos en Resolve.

**Falsos positivos del refinement (iter6)**: ENTREVISTADO_4 2671 recibió offset
-10.47 (mediana de tomas distintas) cuando el .drt manual del editor
era -9.83. Iter7 revirtió y mantuvo .drt original.

## 39. Pocos anchors comunes NO implica audio equivocado

Caso Zezzions iter7. Mi primera versión de `verify_sync_physical.py`
clasificaba como "AUDIO EQUIVOCADO" cualquier pair con <30 anchors
comunes. Falso positivo en:

| Pair | Causa real |
|---|---|
| 2645 ENTREVISTADO_13/ENTREVISTADO_10 | Video tiene PREGUNTAS del entrevistador, audio tiene RESPUESTAS — overlap esperado bajo, pero ambos audios SÍ son ENTREVISTADO_13/ENTREVISTADO_10 |
| 2794 ENTREVISTADO_1 | Video transcript tiene solo 5 palabras ("Paterista ya está abajo") — audio sí es ENTREVISTADO_1 |
| 2786 ENTREVISTADO_2 | Video transcript alucinado ("¿Qué te gusta de tocar música?" ×10) — audio correcto |

**Fix iter7**: el verificador NO clasifica como "audio equivocado"
basándose solo en transcript overlap. Detección de audio equivocado
requiere voice-match (face_voice_links) o validación temporal,
NO transcript overlap.

## 37. Asumir fps=24 al parsear .drt con timeline NTSC 23.976

Caso Zezzions iter5 (2026-05-26). `import_drt_ground_truth.py` tenía
`FPS_DEFAULT = 24` hardcoded. La timeline del .drt estaba a **23.976 fps**
(NTSC, Sony FX30). Los `Start`, `Duration`, `In` del .drt son frames de
la timeline — dividir por 24 introduce drift proporcional al offset:

| offset | drift = offset × (24/23.976 − 1) | perceptible |
|---|---|---|
| -9s (2671)   | -9ms   | NO |
| -19s (2645)  | -19ms  | NO |
| -87s (2598)  | -87ms  | marginal |
| -117s (2570) | -117ms | sí |
| -334s (2566) | -334ms | claro |
| -411s (2570 Dr) | -411ms | claro |
| **-502s (2597 ENTREVISTADO_12)** | **-503ms** | **muy claro — reportado por usuario** |
| -531s (2572) | -532ms | muy claro |

El usuario lo notó como *"distintos niveles de desfase de clip a clip"*
— porque el bug es proporcional, NO constante.

### Hex del MediaFrameRate en el .drt
El .drt de Resolve almacena fps como **IEEE 754 double little-endian
hex**:
- `872211b5dcf93740` = 23.976023976023978 fps (NTSC, 24000/1001)
- `0000000000003840` = 24.0 fps
- `e8e1d5fe6c0c3940` = 25.0 fps (PAL)
- `0000000000803e40` = 30.0 fps

Para detectarlo desde Python:
```python
import struct
fps = struct.unpack('<d', bytes.fromhex(hex_str[:16]))[0]
```

### Política para `import_drt_ground_truth.py`
1. Default `--fps None` → autodetecta del primer `Sm2TiVideoClip.MediaFrameRate`.
2. Permitir override manual con `--fps 23.976` o `--fps 25` para casos
   donde el header está corrupto.
3. Validar: si fps detectado < 20 o > 60, abortar (probable parse error).
4. Logear el fps usado en cada corrida.

### Cómo confirmar empíricamente fps de la timeline
Si tienes un video con duración conocida del manifest, comparar contra
`Duration` del `Sm2TiVideoClip`:
```
dur_real = clip.duration_sec del manifest  # ej. 399.4s para 2571
dur_fr = videoclip.Duration del .drt        # ej. 9575 frames
fps_correcto = dur_fr / dur_real            # = 23.976 ✓
```

## 36. Refiner ciego con prominence baja destruye sync validado

Caso Zezzions iter3 (2026-05-26). `bin/refine_all_pairs.py` con
`--min-prominence 0.15` aceptó deltas grandes en pairs YA validados
visualmente por el usuario:

| Clip | Offset bueno | Offset después de refine | Δ |
|---|---|---|---|
| 2712 ENTREVISTADO_5 | +80.94 | +82.30 | **+1.36s** |
| 2645 ENTREVISTADO_13/ENTREVISTADO_10 | -19.30 | -17.96 | **+1.34s** |

En ambiente con música ambient fuerte, el envelope log-RMS tiene
múltiples peaks con prominencia 0.15-0.25 — falsos positivos que parecen
refinamientos legítimos. **El refiner ciego degradó pairs que ya
estaban bien**.

→ El refiner debe respetar SIEMPRE:
  - `method='manual'` (curaduría del usuario)
  - `method='manual-from-drt'` y `manual-from-drt-sibling-locked`
  - `method LIKE '%-locked'` (rescates manuales, derivaciones cronológicas/hermano)
- Default `--min-prominence` debe subir a `0.30+` (no `0.15`).
- Default `--max-delta-ms` debe bajar a `500ms` (no `2500ms`).
- Si `sound_events` registra >50% music en el audio, exigir
  `prominence ≥ 0.50` adicional.

## 32. Material 10-bit rompe los umbrales 8-bit de analyze_clips
Caso Más Allá del Balón (2026-06-11): las dos FX30 grabaron HEVC Main10
(10-bit). `signalstats` de ffmpeg reporta luma en la escala NATIVA del
pixel format → YAVG ~512 (gris medio 10-bit) disparaba "sobreexpuesto
100%, highlights quemados" con los umbrales 8-bit del motor (200/250).
Resultado: **253 de 254 clips marcados `cull` y 0 tramos derivados** en
material recién salido de cámara.

→ **Señal de alarma**: si >90% del material nuevo sale `cull`, el bug está
  en el analizador, no en el material. Nunca aceptar esa distribución sin
  investigar (regla §25: la causa raíz no es el primer síntoma visible).
→ Fix en motor: `format=yuv420p` antes de `signalstats` en
  `lib/analysis.py::build_filter_chain` — normaliza cualquier fuente a
  8-bit antes de medir. Genérico, sin tocar umbrales.
→ Nota S-Log: material log plano puede dar blur_mean ~8 (umbral "foco
  débil") por bajo contraste, no por desenfoque real. `review` es
  aceptable (no destructivo); no subir el umbral a ciegas.

## 33. Archivos AppleDouble (`._*`) en discos externos contaminan globs
Caso Más Allá del Balón (2026-06-11): macOS crea `._<nombre>.json` (4 KB,
no-UTF8) junto a cada archivo en volúmenes exFAT/NTFS. Un glob `*.json`
sobre `transcripts/` los incluye y `json.load()` truena con
UnicodeDecodeError. El conteo "530 JSON" cuando esperabas 265 es el
síntoma clásico (justo el doble).

→ Filtrar `p.name.startswith('._')` en todo glob de archivos de datos
  sobre discos externos (fix aplicado en `correct_transcripts_vocab.py`;
  `audit_transcripts.py` ya filtraba bien).
→ Robustez adicional: capturar `UnicodeDecodeError/JSONDecodeError` por
  archivo y saltarlo con warning en vez de tronar el batch entero.

## 34. Vocabulario de dominio hardcodeado al proyecto anterior
Caso Más Allá del Balón (2026-06-11): `lib/project_vocabulary.py` traía
`DEFAULT_DOMAIN_TERMS` con el vocabulario de Zezzions (Avicii, Ableton,
EDM...) hardcodeado — en un docu de fútbol eso es ruido de corrección
fuzzy. Mismo anti-patrón que el CAST de Jilotepec (§ tabla de hardcodes
en patrones-exitosos).

→ Fix en motor: `vocabulary_hints` en `project_config.json` del proyecto;
  `build_project_vocabulary()` los usa como dominio y solo cae al legacy
  de Zezzions si el config no existe (compat). Los MEXICANISMOS quedan
  siempre (transversales).
→ Doctrina reforzada: al arrancar proyecto nuevo, poblar
  `vocabulary_hints` desde el tema del proyecto ANTES de correr la
  corrección de vocabulario.

## 35. DELETE "selectivo por location" degenera en global con `--location ""`
Caso MAB (2026-06-11): el DELETE de `sync_transcript.py` se había vuelto
selectivo tras Zezzions (§23) filtrando por `--location`. Pero en proyectos
PLANOS se pasa `--location ""` → `LIKE '%%'` matchea todo → el DELETE vuelve
a ser global de facto. Una corrida de pesca de anchors (`--audio-like
'%zoom%'`, 0 matches) borró los 196 pares existentes y escribió 0.

→ Un filtro "selectivo" que depende de un argumento que puede ser vacío NO
  es una garantía. El DELETE debe acotarse por TODAS las dimensiones del
  universo que el run re-escribe (video location Y audio pattern).
→ Métodos blindados (`manual%`, `%-locked`) jamás se borran automáticamente
  — añadido al WHERE del DELETE (fix en sync_transcript.py).
→ Síntoma de detección: un paso downstream reporta "0 evaluados" sobre una
  tabla que debería estar poblada. Verificar COUNT(*) inmediatamente, no
  asumir que el filtro del downstream está mal.
→ Recuperación barata SI los pares vienen de procesos deterministas
  (transcript + chrono): re-correr. Por eso importa que TODO sync sea
  re-derivable de insumos durables (transcripts cacheados + manifest).

## 36. Routing de prompts LLM: "sin datos YOLO" ≠ "sin personas"
Caso MAB (2026-06-11): `describe_segments_local_llm.py` ruteaba al prompt
de PAISAJE cuando YOLO no reportaba 'person'. Pero `segment_objects` estaba
VACÍA (YOLO nunca corrió — tabla satelital creada vacía, §20). Resultado:
los 218 tramos de un rodaje callejero de vox pops se describieron con el
prompt de paisaje → "Nubes en movimiento. Sector vacío" en serie.

→ Ausencia de evidencia ≠ evidencia de ausencia. Si la señal auxiliar no
  corrió, usar el default seguro del dominio (documental = personas), no
  la rama "no hay personas". Fix en `pick_prompt()`.
→ Los prompts traían vocabulario de Jilotepec ("equipo de escalada",
  "sector vacío", "peñasco") — el LLM rellena con el dominio que le
  sugieres. Genericiados 2026-06-11; pendiente: ejemplos de dominio desde
  project_config.json (misma familia que §34).
→ Detección: muestrear SIEMPRE 5-10 descripciones del LLM apenas arranca
  el batch (smoke test del usuario) — "Sector vacío" en un Mundial canta
  desde la muestra 1. No esperar a que terminen 218.

## 37. Master transcripts intercalan palabras duplicadas (A1+lavalier)
Caso MAB (2026-06-12): `build_master_transcripts.py` mergea por timestamp
las palabras de A1 y lavalier. Cuando ambos canales capturan LO MISMO (el
caso normal en entrevistas), el merge intercala duplicados palabra por
palabra: "Soy ENTREVISTADO_14 ENTREVISTADO_14 García y me dedico... futbolistas.
futbolistas." Eso rompe preguntas detectadas, resúmenes y cualquier
lectura editorial del master.

→ Mitigación aplicada: pasada de corrección contextual (workflow de
  agentes con contexto del proyecto + doble refutador) — 281 correcciones
  en 24 entrevistas, ~90% eran des-intercalados de fusión.
→ Fix de motor PENDIENTE: en el merge, deduplicar palabras idénticas (o
  fuzzy-iguales) cuando caen dentro de ±1.5s entre fuentes — conservar la
  del canal con mejor SNR (lavalier). Hasta entonces, el master sirve para
  sync/preguntas pero NO citarlo textualmente sin revisar.
→ Bonus del mismo workflow: correcciones contextuales reales tipo
  "chamearlo"→"chambearlo", "antamos"→"anotamos" — la capa 2 iterativa
  del playbook (corregir transcript con pistas de contexto) ES rentable
  y debe correrse en todo proyecto tras detectar cast y dominio.

## 38. `luac -p` no caza globals nil — el smoke runtime con mock es obligatorio
Caso MAB (2026-06-12): un refactor renombró `MULTICAM` → `MC` pero una
referencia vieja quedó dentro de `placeCompanions`. `luac -p` pasó (es
sintaxis válida — Lua resuelve globals en runtime) y el script tronó EN
DAVINCI con "attempt to get length of a nil value" al construir
ENTREVISTAS, en la máquina del usuario.

→ Después de CUALQUIER edición a un asistente_*.lua, correr SIEMPRE:
  `lua ~/cinema-assistant/resolve/mock_resolve_smoke.lua asistente_<p>.lua`
  (mock permanente del motor, Media Pool vacío, recorre todos los caminos
  estructurales). El paso 13 del playbook ya exigía "carga real con lua
  nativo" — esta vez se saltó para el asistente (solo se probó el data) y
  el usuario pagó el crash. El mock ahora existe como herramienta fija
  para que el check cueste un comando.
→ Tras un rename, `grep -n "<nombre_viejo>"` sobre el archivo ANTES de
  entregar. Un refactor no está terminado hasta que el viejo símbolo da
  cero hits.

## 39. Pipeline monolingüe en material políglota — entrevistas invisibles
Caso MAB (2026-06-12): la entrevista de los sudafricanos (¡a dos cámaras,
165s!) fue INVISIBLE para todo el pipeline: transcripción forzada
`--lang es` sobre habla en inglés → Whisper alucina español → el filtro
anti-alucinación la marca basura → señal 3 nunca la categoriza →
`verify_interviews` (patrones de pregunta EN ESPAÑOL) tampoco la ve. El
escaneo de idioma encontró **18 clips no-españoles** (17 en, 1 de) en un
solo día de rodaje mundialista.

→ **Pista retrospectiva que ya estaba ahí**: estos clips fueron los del
  sync con confianza absurda (19.49, 10k anchors) — la alucinación
  española sobre inglés genera texto ultra-repetitivo que rompe el
  alineador. Confianza >1 era síntoma de IDIOMA, no solo de repetición.
→ **Regla nueva para TODO proyecto de evento internacional**: después de
  la transcripción inicial, correr detección de idioma (`whisper-cli -dl`
  sobre 30s del centro) sobre clips >=40s no categorizados o alucinados.
  Re-transcribir los no-españoles con su idioma real.
→ Las preguntas en inglés se detectan desde 2026-06-12
  (`lib/question_filter.py::_split_questions_en`, aditivo, sin tocar el
  español). Otros idiomas (de, fr, pt...) = pendiente; mínimo quedan
  categorizados como entrevista por densidad.
→ `verify_interviews` sigue siendo monolingüe — su garantía "0 entrevistas
  perdidas" SOLO cubre español. Hasta extenderlo, el escaneo de idioma es
  el verificador de facto para lo demás.

## 40. Defaults geográficos en argparse — el hardcode que el linter no veía (FCC 2026-07-09)

**Síntoma**: `transcribe_clips.py` reportó "Listo. 0 transcritos, 0 fallos"
en Film Club Café — sin error, sin warning. La fase entera fue un no-op
silencioso.

**Causa**: CINCO scripts traían `add_argument(..., default="jilo")` (o
`"%audios%jilo%"`, `"JILOTEPEC"`): transcribe_clips, sync_transcript,
build_contact_sheets, sync_waveform, generate_fcpxml. El linter solo
buscaba tokens geográficos cerca de `LIKE`/`rel_path` en la MISMA línea —
un default de argparse vive lejos de la query y se le escapaba.

→ Fix: los 5 defaults ahora son genéricos (`""` / `"%"`); el linter tiene
  una regla nueva que caza `default="<token geográfico>"` en argparse
  (validada: detectó los 2 que faltaban al primer intento).
→ Regla operativa: cuando una fase reporta 0 items procesados en un
  proyecto con material, es un BUG de selección hasta demostrar lo
  contrario — no "no había nada que hacer".

## 41. Proyecto fresco: scripts truenan por tablas de pasos posteriores (FCC 2026-07-09)

**Síntoma**: en el primer proyecto que corre el pipeline DESDE CERO en orden
"transcribir primero", varios scripts truenan con `no such table`:
`audit_transcripts` (clip_descriptions), `init_sync_schema`
(audio_sync_pairs antes del primer sync), `enrich_curated_segments`
(clip_shot_values → clip_angles → content_segments, cadena de 3),
`describe_segments_local_llm` (segment_objects, face_detections,
clip_characters).

**Causa**: ESC/Zezzions/MAB corrieron los pasos en otro orden o con tablas
ya creadas; nadie había corrido el orden canónico del playbook en un
manifest virgen.

→ Fix aplicado: los consumidores de tablas OPCIONALES (YOLO, faces,
  clip_characters, clip_descriptions) usan try/except OperationalError con
  fallback vacío. Los que REQUIEREN la tabla (derive_chrono_sync) abortan
  con mensaje que dice qué correr primero.
→ Orden real de dependencias descubierto para tramos curados:
  `derive_shot_value` → `derive_camera_angle` → `derive_content_segments`
  (+ `derive_audio_segments`) → `curate_segments` →
  `extract_segment_frames` → `enrich_curated_segments` →
  `describe_segments_local_llm`.

## 42. Refine por envelope con música de fondo: revertir, no confiar (FCC 2026-07-09)

**Síntoma**: `refine_all_pairs.py --min-prominence 0.15` (default del
playbook 8c) "refinó" 6 anchors de transcript moviéndolos 0.7–1.5 s, con
signos mezclados y prominence 0.17–0.61.

**Diagnóstico**: la música del café produce picos de correlación falsos.
`verify_sync_physical` (mediana de offsets por segmentos de transcript)
confirmó los offsets ORIGINALES a ±30–130 ms y refutó TODOS los
refinamientos. Se revirtieron los 6.

→ Regla: en material con música ambiente, el refine por envelope exige
  prominence ≥ 0.40 (ya era doctrina Zezzions) Y NUNCA se aplica sobre
  anchors de transcript sin corroboración física por segmentos.
→ El aplicador correcto es `verify_sync_physical.py --apply-refinable`
  (nuevo): aplica la mediana física de segmentos — transcript-based, la
  música no lo engaña.

**Un nivel más abajo (Adrián, FCC 2026-08-10).** Corrió esto mismo en
full-spectrum, sin filtro de voz, y 17 de 97 pares se "corrigieron" igual de errático, uno
con `prominence=5.74` (el valor debería estar en [0,1] — correlación rota). O sea: **el
problema de fondo no es la banda de frecuencia, es el feature.** `lib/sync_refiner.py`
correlaciona envelope **log-RMS**, que mide *cuánta* energía hay; dos cámaras en puntos
distintos de un concierto no comparten envelope de energía (otra mezcla, otra reverb, otro
público encima) aunque sí compartan los mismos onsets.

Consecuencia práctica: en concierto, `refine_all_pairs.py` no sirve para el refinamiento
sub-segundo **en ninguno de sus dos modos**. Quedarse con el offset de la etapa anterior y,
si hace falta sub-frame, que lo verifique el editor a oído en Resolve — hasta que exista un
refinador basado en onsets con ventana de búsqueda.

## 43. No paralelizar dos Whisper en una sola GPU Metal (FCC 2026-07-09)

Dos jobs de whisper-cli simultáneos (clips + WAVs) comparten la GPU: el
CPU% bajo (5-20%) delata que corren en Metal, y el job de archivos largos
se arrastra (~1× tiempo real vs ~14× esperado). Secuenciar SIEMPRE los
batches de transcripción; el paralelismo útil es whisper + trabajo de CPU
(análisis técnico, sync por transcript).

## 44. Timelines empacadas: las compañeras multicám se derraman desalineadas (FCC 2026-07-11)

**Síntoma**: el usuario reporta "falta este ángulo VICG" señalando un par
verificado con triple evidencia física (cadena 00038 −10.792, cadena 00024
−10.818, directa A1↔A1 −10.804 — ±13 ms; 519 4-gramas comunes).

**Causa**: las timelines del asistente van EMPACADAS (V1 espalda con
espalda, sin los huecos reales). `placeCompanions` colocaba la compañera
completa a `base.start + delta`: (1) fuera de la ventana del base, la
alineación NO vale (el empaque elimina los gaps reales) — la cola de una
compañera larga queda desalineada sobre el V1 vecino; (2) los derrames
provocan colisiones en cascada V2→V3→FALLA (simulado en FCC: 7 compañeras
en V3, 1 pérdida total — el 8628 que el usuario notó, compañera de DOS
bases a la vez).

→ Fix en `placeCompanions` (asistente_fcc.lua): **clamp de la compañera a
  la ventana [start, end) de su clip base** — recorte de cabeza y cola de
  la fuente. Consecuencias verificadas por simulación: 26/26 compañeras en
  V2, 0 colisiones, y una compañera larga verificada contra dos bases se
  coloca una vez POR PAR, cada tramo alineado a su base.
→ Regla: en timelines empacadas, el material sincronizado (compañeras Y
  lavalieres) solo es válido DENTRO de la huella de su clip ancla.
→ Método de diagnóstico reusable: simular la colocación fuera de Resolve
  (posiciones = suma acumulada de duraciones + deltas del multicam lua)
  detecta colisiones sin abrir el proyecto.

## 46. Garble plausible: la alucinación que el audit estadístico NO ve (FCC 2026-07-12)

**Síntoma**: transcripts que PASAN el filtro de alucinaciones (densidad
léxica ok, sin repeticiones masivas) pero contienen texto inventado que
parece válido — concentrado en NOMBRES PROPIOS y términos raros:
"Satriacit Drive" (Satyajit Ray), "Regin/Reign Bull" (Raging Bull), "la
plodería" (la curaduría), "Marcielo" (Marisela), "el P.O.P. / el peón /
la palma del peón" (el nombre del club), "Acatenti" (apellido/banda por
identificar). Ese garble se propagó a preguntas Purple, cast y crónica
hasta que el usuario lo señaló: *"falta un proceso de verdadera curaduría
del transcript para verificar que no haya alucinaciones."*

**Causa raíz**: `audit_transcripts` e `is_hallucinated()` son
ESTADÍSTICOS (ratios, repetición, densidad). El garble plausible es
localmente coherente — solo lo caza la COMPRENSIÓN del contexto.

→ Fix estructural = **Capa 2c del playbook** (pasos-a-seguir §5):
  1. `bin/surface_transcript_suspects.py --root "$DISK"` — junta en UN
     reporte lo que hay que leer: preguntas sin `question_short`,
     auto-IDs del cast, tokens capitalizados raros CON contexto, recap
     del audit. Exit 1 si hay preguntas sin curar.
  2. Claude LEE el reporte y cura por comprensión (question_short,
     cast.json, vocabulary_hints) + re-corre `correct_transcripts_vocab`.
  3. Candado en el bake: `export_lua_data` grita "⚠⚠ N preguntas SIN
     CURAR" — no se entrega con ese aviso.
→ Regla operativa: ningún texto de transcript llega CRUDO a un artefacto
  visible para el editor (markers, crónica, nombres de cast). Todo pasa
  por curaduría de comprensión primero. Los nombres propios son la zona
  roja: verificar cada uno contra contexto antes de usarlo.

## Lección 47 — Defaults legacy de path-filter rompen proyectos planos en silencio (Fantástico Cómics 2026-07-17)

En un proyecto plano con `Audio/TX1|TX2` (carpeta=cadena), CUATRO scripts
no procesaron nada (o procesaron lo equivocado) por defaults heredados,
todos SIN error visible:

| Script | Default roto | Flag correcto |
|---|---|---|
| `index_audios.py` | espera carpeta `AUDIOS/` | (innecesario si `index_project` ya cubrió el árbol) |
| `transcribe_audios.py` | `--audio-like '%audios%'` | `--audio-like 'audio/%'` |
| `build_voice_catalog.py` | `--audio-like '%lavas%'` | `--audio-like 'audio/%'` |
| `analyze_segments.py` / `derive_video_categories.py` / `verify_coverage.py` | `--project-prefix 'ESCALANDO MEXICO'` | `--project-prefix ''` |

→ Regla operativa: en proyecto nuevo, ANTES de correr cada etapa revisar
  su `--help` por filtros de path con default legacy. La señal de alarma
  es "0 procesados" o "Listo. 0 con texto" saliendo con exit 0.
→ `derive_characters.py` sigue con el cast de JILOTEPEC hardcodeado —
  para markers Name=entrevistado, poblar `clip_characters` a mano (SQL)
  desde el mapa entrevista→persona verificado (patrón FCC iter-6).
→ `lint_pipeline` NO caza estos defaults (solo hardcodes geográficos en
  el flujo principal); no confiar en él para esto.

## Lección 48 — mirror_questions_to_audio NO copia question_short (Fantástico Cómics 2026-07-17)

El espejo crea filas con `question_text = '[del video sync] <texto>'` y
`question_short` NULL. Si la curaduría (Capa 2c) corrió ANTES del mirror,
los espejos quedan "sin curar" y el candado del bake/verifier 6 falla.

→ Fix aplicado (SQL): copiar `question_short` a los espejos matcheando
  `question_text` sin el prefijo `[del video sync] `.
→ Orden ideal: curar → mirrorear (el mirror hereda). Si se mirroreó antes,
  correr la copia SQL. Verificar siempre: `COUNT(*) WHERE question_short
  IS NULL` debe ser 0 ANTES del bake.

## Lección 49 — Rutas del §0a movidas (memoria-creativa reorganizada)

`autor/biografia/sintesis.md` y `proyectos/_registro.json` YA NO EXISTEN.
Desde la reorganización (~2026-05-28): síntesis en
`~/memoria-creativa/Autor/Autor-Biografia/Autor-Sintesis.md`, registro en
`~/memoria-creativa/_registro.json` (raíz), y las fichas de proyecto se
proponen vía `~/memoria-creativa/_cambios/pendientes/` (precedente:
`2026-07-09-ficha-film-club-cafe.md`). Leer también `_foco.md` (hot cache).

## 50. MAIN_CAM sin un modelo de cámara = transcripción parcial SILENCIOSA (AVA 2026-07-20)

`transcribe_clips.py --cameras main` filtró solo la FX30: la ILCE-6700
(a6700 de CAMAROGRAFO_1 y CAMAROGRAFO_2, cámaras PRINCIPALES del proyecto) no estaba en
`MAIN_CAM`. 52 de 112 clips quedaron sin transcript y el log decía "Listo"
sin avisar. Fix aplicado al motor (ILCE-6700 añadida). Regla: al inventariar
cámaras (paso 3), COTEJAR los camera_model del manifest contra MAIN_CAM
antes de transcribir; si falta uno, añadirlo al motor.

## 51. Scripts con default 'ESCALANDO MEXICO' en proyectos planos (AVA 2026-07-20)

`analyze_segments.py` y `verify_coverage.py` procesan 0 clips en silencio
si no se les pasa `--project-prefix ''`. Igual que la lección FCC del
hardcode 'jilo'. En proyectos planos SIEMPRE pasar el prefix vacío
explícito y verificar que el conteo procesado > 0.

## 52. Emparejar multicám por CATEGORÍA pierde ángulos B (AVA 2026-07-20)

En eventos con música alta, el ángulo B de una entrevista suele tener el
A1 alucinado (música/porra) → NO queda categorizado 'entrevista' → el
emparejador que cruza solo entrevistas↔entrevistas lo pierde. Victor lo
detectó ("siempre teníamos dos cámaras grabando"). Regla: los pares
multicám se buscan por TRASLAPE EN TIEMPO REAL (offsets a la cadena del
lav) contra TODOS los clips de otras cámaras, no contra la categoría.
Verificación en 3 capas: delta físico A1↔A1 (probar voz/full/onset — en
escena musical gana onset/full; en entrevista limpia gana voz) +
convergencia de variantes + contenido del transcript (frases compartidas,
p.ej. "aquí está la cámara" en ambos ángulos). Bonus: cada delta medido
puentea el offset al lav del ángulo B (derivado → medido).

---

## Lección 53 — Anonimizar por sustitución de texto rompe el código en silencio

**Fecha**: 2026-07-31. **Contexto**: auditoría previa a publicar el plugin en abierto.

El plugin llevaba 174 menciones de nombres reales de personas de los rodajes. Para
publicarlo había que anonimizarlas, y se hizo con una sustitución de texto sobre el
motor (`ESCALADOR_A` → `ESCALADOR_A`, etc.), asumiendo que los nombres vivían solo en
comentarios y docstrings.

**Vivían en cinco sitios donde el nombre era un DATO OPERATIVO**, y ahí la
sustitución no lo oculta: lo rompe.

| Sitio | Qué era | Efecto |
|---|---|---|
| `rebake_after_llm.sh:8` | El glob que RESUELVE el disco | `DISK` vacío → el script aborta. Lo llama `run_full_pipeline.sh:294`: mataba el paso final del pipeline |
| `generate_drp_v7.py:101` | Un `LIKE '%<nombre>%'` dentro del SQL | 234 clips casaban antes, 0 después |
| `reformat_descriptions.py:64` | `PROJECT_PROTAGONIST`, valor que se **escribe** en `clip_descriptions` | 59 de 91 clips de entrevista |
| `reformat_descriptions.py:60`, `clean_character_identity.py:48` | El set `INTERVIEWERS`, comparado contra `clip_characters` | El manifest tiene `ESCALADORA_B` en 9 clips y `ESCALADORA_B` en 0: el filtro dejó de excluir a nadie |

**Por qué es peligroso y no solo molesto**: ninguno de los cinco tira una excepción.
El código sigue corriendo y devuelve cero, vacío, o el valor por defecto. Es la misma
familia que la lección 47 (el default heredado) y la 40 (trabajo cero con exit 0): un
paso que no hace nada y dice que todo salió bien.

**La regla**:

1. Antes de sustituir un nombre en código, clasificarlo. Si está en un docstring,
   comentario o `help=` de argparse, es documentación y se puede sustituir. Si es
   clave de diccionario, elemento de un set, patrón de búsqueda, valor por defecto o
   algo que se persiste, **NO se sustituye**.
2. Los datos de personas del proyecto **no se anonimizan: se sacan del código.** Van a
   `<disco>/.cinema_assistant/cast.json` (ver `lib/cast.py`: `load_cast`,
   `load_interviewers`, `load_protagonist`). El motor trae la lógica; el reparto es
   de cada rodaje. Es la misma regla de capas que ya aplicaba a los horneados.
3. Verificar la sustitución **contra el manifest real**, no solo leyendo el diff:
   `SELECT COUNT(*) ... LIKE '%<nombre real>%'` contra `'%<seudónimo>%'`. Si el real
   devuelve filas y el seudónimo devuelve 0, esa sustitución rompió algo.
4. Un scan con AST distingue docstrings de literales de datos mucho mejor que un grep:
   `ast.get_docstring()` sobre cada nodo, y todo `ast.Constant` de tipo str que NO sea
   docstring es candidato a revisión manual.

**Cómo se detectó**: verificando el motor contra el proyecto real con el disco
conectado, no en las pruebas. Las 197 pruebas pasaban, el linter pasaba y los 93
scripts respondían a `--help` — con los cinco bugs dentro. Ninguna prueba compara el
código contra los datos de un proyecto de verdad, y esa es justo la clase de fallo que
solo aparece ahí.

## No forzar un número de reloj que el material no da (Morsa, 2026-08-02)

Iban (ILCE-6700, 97 clips) quedó con **0 pares de sync**: su A1 es scratch de
cámara en un concierto, el habla queda tapada, `sync_transcript` no le dio
anclas y sin anclas `derive_chrono_sync` no puede medirle el skew. Sin skew no
entra ni al sync de lavalieres ni a la multicámara.

Se intentaron **cuatro métodos** para medirlo:

1. A1 de Iban ↔ A1 de otra cámara con skew conocido, por pares traslapados.
2. A1 de Iban ↔ la cadena de audio continua, ventana ±180 s.
3. Lo mismo pero por **consenso**: K picos por clip y el valor donde coinciden
   más clips distintos.
4. Lo mismo restringido a la ventana **previa al concierto**, donde el audio es
   voz y no música.

Ninguno converge. El 2 dio ±94 s de dispersión; el 3 sacó "consensos" de 4 de 24
y 6 de 98 combinaciones, con la distribución plana; el 4, 2 de 6.

**La razón es física y hay que recordarla: la música es PERIÓDICA.** En una
ventana ancha, la correlación encuentra un pico plausible en cada compás
repetido. La prominencia alta engaña — mide cuánto destaca el pico sobre el
fondo, no si el pico es el correcto. Un correlador que en material hablado es
decisivo, en material musical es un generador de falsos positivos con buena
pinta.

**Lo que se hizo:** reportarlo como es. Los 97 clips de Iban entran como
cobertura sin lavalier, marcados, y se deja que `AutoSyncAudio` de Resolve lo
intente por su cuenta en el merge del Media Pool. **No se inventó un número.**
Un skew equivocado habría desplazado 97 clips en la timeline y el error habría
aparecido semanas después, en el corte.

La regla: cuando varios métodos independientes no coinciden, el resultado del
paso es "no medible", no la mediana de las mediciones malas.

## Dos cuerpos de la misma cámara colapsan en un solo perfil (Morsa, 2026-08-02)

El perfil `sony-fx30` del motor matchea el modelo `ILME-FX30` **y** el prefijo
`VICG` a la vez. En un proyecto con DOS FX30 —Ayan (`ASA_`) y Victor (`VICG`)—
ambos caen en el mismo id y los reportes no pueden distinguirlos.

Se resuelve sin tocar el motor, con un override por proyecto en
`<disco>/.cinema_assistant/camera_profiles.json`: un perfil por cuerpo, que
matchea por `filename_prefix` y con `priority` MENOR que la del perfil genérico
(se evalúa antes, primer match gana). Comprobarlo antes de indexar:

```python
from lib import cameras
cameras.classify("ASA_20260802_8867.MP4", "ILME-FX30", "Sony", ".mp4", disk_root=D).id
cameras.sql_python_conflicts("main", disk_root=D)   # debe ser []
```

Ojo con `classify`: `disk_root` es **keyword-only**. Pasarlo posicional falla en
silencio hacia el registro del motor y la prueba parece decir que el override no
sirve, cuando lo que falló fue la llamada.

## Los scripts de proyecto se copiaban a mano y fueron divergiendo (2026-08-03)

El plugin distribuye `resolve/asistente_lib.lua`, pero **no había plantilla del
script por proyecto**. Cada proyecto nuevo nacía copiando ~1000 líneas del
anterior y cambiando rutas a mano. Medido:

| comparación | líneas distintas |
|---|---|
| `asistente_morsa.lua` vs `asistente_fcc.lua` | **19 de 993** |
| `asistente_avalanches.lua` vs `asistente_fcc.lua` | **278** |

O sea: uno es una copia literal del otro, y el tercero es una generación vieja
que nunca recibió los arreglos posteriores. Tres de los cinco scripts estaban
desfasados. El síntoma visible fue tonto y delator: el de Morsa imprimía
`LISTO — FILM CLUB CAFE` al terminar, porque nadie relee 993 líneas al copiarlas.

**El arreglo**: `bin/nuevo_asistente_proyecto.py` genera el script del proyecto
a partir del más reciente, sustituye cabecera, rutas, prefijo y los dos banners,
y **comprueba que no quede ni una mención al proyecto de referencia** — que es
exactamente lo que falla al copiar a mano. Valida con `luac -p` y sugiere la
corrida del mock.

Pendiente para cerrar el problema de raíz: mover la orquestación entera a
`LIB.correr(CONFIG)` en `asistente_lib.lua`, para que un arreglo llegue a todos
los proyectos sin regenerar nada. Lo que ya vive en la librería (markers,
pistas, roll, bins, linkeo) se arregla en un solo sitio; lo que sigue en el
script del proyecto, no.

## Un verificador que "pasa" porque su regex dejó de matchear (2026-08-03)

`verify_multicam_placement.py` anclaba el final de cada par con `\}`:

```python
PAIR_RE = re.compile(r'...overlap=([\d.]+), verified=(true|false)\}')
```

Al añadir `place` y `basis` al export, el regex dejó de matchear **nada**, y el
verificador imprimió:

```
Sin pares verificados — OK trivial.
```

Verde, exit 0, y sin haber mirado un solo par — con 16 pares verificados en el
archivo. Es la familia de las lecciones 40, 47 y 51: **un paso que no hace nada
y dice que todo salió bien es peor que uno que truena**.

**La regla**: un parser de un formato propio no ancla el final del registro si
el formato puede crecer. Y cuando un verificador reporta "0 elementos" en un
proyecto con material, eso es un bug de selección hasta demostrar lo contrario
—incluso, y sobre todo, cuando el mensaje dice "OK trivial".

**El contraste que faltaba (Adrián, Central de abastos 2026-08-27).** Al adaptar un mock
de un proyecto a otro se cambiaron las rutas del chequeo de pistas pero quedó **otra
ocurrencia** de la ruta vieja en el chequeo de sync, con otro texto alrededor, así que el
replace no la tocó. Efecto: el verificador solo comprobaba una de las dos cadenas y
reportaba "166 comprobados" con toda confianza. Al corregirlo pasó a **664**.

**Un verificador que pasa no vale nada si no se sabe cuánto midió.** La regla que cierra
esta familia: contrastar el número de comprobaciones contra el esperado —aquí,
clips × cadenas × timelines—, e imprimirlo siempre. Si el número es sospechosamente
redondo, o menor de lo que debería, algo no se está mirando. Y al adaptar un mock, `grep`
de las rutas y nombres del proyecto anterior en TODO el archivo, no solo donde se recuerda
haberlos puesto.

## Buscar el desfase en el rango equivocado (Morsa, 2026-08-03)

La a6700 de Iban resistió **seis** métodos de medición de reloj: correlación de
envelope en ventana ancha, consenso entre clips contra el WAV, cámara contra
cámara, ventana pre-concierto, huella espectral propia y chromaprint. Se
concluyó "no medible por audio" y se reportó así.

Era falso, y el error era de encuadre: **todas las búsquedas usaron ventanas de
±180 a ±300 s, y el desfase real era de una hora.** Nunca estuvo dentro del
rango. Peor: el emparejamiento elegía el archivo WAV que contenía la hora *cruda*
del clip, así que ni siquiera se comparaba contra el audio correcto. Ningún
método podía acertar, por bueno que fuera.

Lo detectó Victor viendo el material: en los clips de Iban sonaba una canción
del concierto mezclada con tomas donde se oye al presentador previo al show. Una
hora de diferencia, a ojo, en dos minutos.

**Las reglas que salen de aquí:**

1. Antes de declarar algo "no medible", **comprobar que el rango de búsqueda
   pueda contener la respuesta**. Un desfase de reloj de cámara no está acotado
   por nada: puede ser de horas (zona horaria, hora mal puesta) y no de
   segundos. El rango por defecto tiene que cubrir al menos ±2 h, o decir en
   voz alta cuál es su límite.
2. **Que varios métodos independientes fallen no prueba que el dato no exista**;
   puede probar que todos comparten la misma premisa equivocada. Los seis míos
   compartían el rango.
3. El **ojo del editor es un instrumento de medición**, y en este caso el más
   preciso disponible. `camera_skews_manual` en `project_config.json` existe
   para eso, con una nota de quién lo midió y cómo.

**Corroboración posterior**: con el skew declarado en −3600 s, nueve pares
Iban↔otra cámara pasaron la verificación por contenido (envelope A1↔A1). Con el
reloj mal no habrían sido ni candidatos, porque no habría traslape temporal.

## Una cámara sin skew se ordenaba con su reloj crudo, en silencio

`skewFor()` devolvía 0 cuando el grupo no tenía skew medido. Eso ordena esa
cámara por su reloj tal cual — y si el reloj está corrido, sus clips se
intercalan en el lugar equivocado de la cronología sin que nada lo diga. En
Morsa el B-roll mezcló tomas del concierto con tomas de antes de que empezara.

Ahora se avisa por grupo al detectarlo y en grande al cerrar, con la
instrucción concreta de cómo declararlo a mano. El default 0 sigue existiendo
—no hay nada mejor que hacer— pero deja de ser silencioso.

## El signo del offset, y las compensaciones que lo escondían (2026-08-13)

El motor tiene UNA convención, escrita en el SKILL y en media docena de
docstrings:

    offset = audio_start − video_start

De ahí sale lo único que importa al colocar:

    audio_t = video_t − offset          ← la que se usa para buscar en el audio
    video_t = audio_t + offset          ← la INVERSA, y sí lleva `+`

**Las dos son ciertas y se parecen mucho. Eso es lo que hace que el error se
propague, y por qué "corregir" a ojo es peligroso**: `bin/detect_pauses.py:278`
usa la inversa y está bien.

### Qué estaba mal

`lib/sync_refiner.py` documentaba la contraria y la implementaba: centraba la
búsqueda en `v_start + offset`, o sea a 2× offset del sitio correcto. Medido
contra `tests/media_prueba` (offset real −5.00 s), devolvía **−4.000 con
prominence 0.995** desde una estimación de −4.70: un segundo de error con la
máxima confianza posible, y pasando los tres filtros de `refine_all_pairs.py`.

Con lavalieres continuos el daño no fue "2× offset". `a_center` se iba a
negativo, `a_start = max(0.0, ...)` lo recortaba a 0, y el refinador acababa
correlacionando el vídeo contra los PRIMEROS segundos del WAV, donde la
coincidencia no está. Lo que escribía era **ruido acotado por la ventana de
búsqueda**: 33 pares desplazados hasta 4.19 s en MAB y FILM CLUB CAFÉ.

El mismo desacuerdo, por separado, en `lib/lavalier_pairs.envelope_lag`: sus dos
consumidores documentan `delta = b_start − a_start` y la función devolvía lo
contrario. Y en `lib/identity_fusion.py`, que hacía `at = vt + off`.

### La parte que cuesta caro aprender: las compensaciones

`bin/export_multicam_lua.py` pasaba `−delta` al refinador y **negaba el
resultado**. No era un capricho: la doble negación CANCELABA el bug, así que ese
sitio medía bien. Al arreglar el refinador, la compensación pasó a ser el error
y los pares verificados de Morsa cayeron de 25 a 10.

**Regla:** al corregir un signo, buscar quién lo estaba compensando ANTES de
dar el arreglo por bueno. El síntoma es un número que empeora justo donde
debería mejorar. Con los dos lados arreglados, Morsa pasó de 25 a **54** pares
verificados por contenido.

### Por qué duró meses

De las 428 pruebas del motor, **ninguna tocaba un offset**. 21 de los 37 módulos
de `lib/` —4.660 líneas, todo el sync— no los importaba ningún test. El bug
habría muerto el primer día contra una prueba de veinte líneas: dos señales con
desfase conocido y comprobar signo y magnitud. Eso es ahora
`tests/test_sync_convencion.py`.

### Un fixture puede mentir

`tests/media_prueba` son beeps cada 3.0 s: su envolvente es PERIÓDICA, así que
un desfase de 5 s es indistinguible de 5±3k y la medida es ambigua por
construcción. Sirve para el merge —que compara forma de onda— y NO para medir
lag por envolvente. Es la misma patología del caso Iban: la música del concierto
es periódica y la correlación encuentra un pico creíble en cada compás. Para
probar lag hace falta habla sintética con huecos irregulares.

### Qué mirar si vuelve a pasar

```bash
python3 -m unittest discover -s tests -t tests -k test_sync_convencion
```

Y para saber si algún proyecto quedó tocado:

```bash
python3 bin/revertir_refinamientos_iter2.py --buscar-bajo /Volumes/<disco>
```

El delta aplicado va escrito en la nota de cada par, así que revertir es
aritmética exacta: `offset_previo = offset_actual − delta`. No hace falta el
material.

### Aviso para quien lea doctrina de otra copia (2026-08-28)

La bifurcación de Adrián redescubrió este bug el 2026-08-27 —dos semanas después de que se
arreglara aquí— y lo documentó con otro diagnóstico y **otro arreglo**: negar el offset al
llamar (`refine_offset(..., -off, ...)`), dejando el interior de la función como estaba.

**Ese arreglo, aplicado a ESTE motor, lo vuelve a romper.** Aquí el interior ya es el
correcto (`a_center = v_start - current_offset`), así que negar en la llamada da 2× offset:
es literalmente la compensación que esta lección describe, montada de nuevo desde el otro
lado. Su copia además conserva el `a_start = max(0.0, ...)` que aquí se sustituyó por
correr la ventana de análisis en el vídeo.

Si alguna vez esta lección y un documento externo se contradicen sobre el signo, la
autoridad es `tests/test_sync_convencion.py`, no la prosa — y la prueba de un minuto sigue
siendo la misma: pasar `+off` y `−off` sobre un par que ya se sabe bueno; el que devuelve
≈0 es la convención correcta.

## Vocabulario de curaduría escrito en prosa = cero reglas aplicadas (Morsa, 2026-08-18)

Al subtitular el Reel 2 salieron 28 reglas de `vocabulary_notes`, pero probando
una por una **la mayoría de los garbles que la curaduría del 2026-08-02 había
cazado no se corregían**: 'Morda', 'Bosa' y 'bolsas' seguían pasando; sólo
'Able Road = Abbey Road' funcionaba.

La causa es el formato. `load_vocab()` parte la nota por `;` y salto de línea,
y de cada trozo toma el **primer** `=`: todo lo que queda a la izquierda se trata
como lista de garbles separados por `/`. Una nota redactada como prosa —"el
nombre de la banda aparece como 'Morda', 'Bosa' y 'bolsas'"— no tiene `=`, así
que no produce ninguna regla, y el script anuncia un número de reglas alto porque
las está contando de otros trozos. **El contador de reglas no dice si tus reglas
están dentro.**

Dos consecuencias prácticas:

1. **Una regla no puede compartir trozo con la prosa que la introduce.** Poner
   "Garbles cazados el 2026-08-18: vitlemaníaco/bitlemaníaco = beatlemaníaco"
   deja el primer garble pegado al preámbulo y sólo funciona el segundo. Cada
   regla en su propio segmento `;`, y el comentario en un segmento aparte.
2. **A la izquierda va el SINGULAR.** El reemplazo es por substring, así que
   'bitlemano = beatlemano' arregla también 'bitlemanos'; si se escribe el plural
   a la izquierda, 'bitlemanos = beatlemano' se **come la -s**.

Y la trampa de fondo: **como el reemplazo es por substring y sin límites de
palabra, muchos garbles no se pueden meter como regla.** 'Morda = Morsa'
convertiría "mordaza" en "Morsaza"; 'Bosa = Morsa' convertiría "rebosa" en
"reMorsa"; 'bolsas' es palabra común. La nota original de Morsa ya avisaba de
esto para 'Morsa' — vale para casi toda la familia. Esos garbles se resuelven en
la curaduría por comprensión del artefacto, no en el vocabulario. Verificar
siempre con `bs.apply_vocab()` sobre una frase de prueba antes de dar una regla
por buena, incluidos los falsos positivos que podría causar.

## El generador elegía la referencia por mtime, no por capacidad (CLIENTE_1 día 2, 2026-08-18)

`nuevo_asistente_proyecto.py` parte del `asistente_*.lua` **más reciente**, y eso
no es lo mismo que el más completo. Medido ese día: el más reciente era
`asistente_mab.lua` (documental, 14-ago) y **no tenía nada** de la lógica de
pieza con guion — ni bloques de toma, ni corte de silencios, ni marcadores de
cápsula. El único que la tenía era `asistente_imodae.lua`, tres semanas más
viejo.

Generar desde ahí habría dado un script que **se carga limpio, imprime `luac -p`
OK y `LISTO`, y no puede hacer lo que se le pidió**. El generador nació justo
para que los scripts no divergieran; sin comprobación, propaga la divergencia en
vez de cazarla.

→ El generador ahora **lista las capacidades de la referencia** (tomas, cortes,
reels, bts, cronología), y si el `project_kind` del disco exige alguna que falta,
**aborta y nombra la referencia que sí la trae**. Se dice siempre, aunque no
falte nada: la divergencia entre scripts es invisible hasta que se busca.

→ Y queda una divergencia real anotada: `asistente_imodae.lua` nunca recibió el
modelo de B-ROLL cronológica ni el arreglo de colisiones que sí tienen mab y
morsa. Elegirlo es elegir eso también. **Cuando dos referencias tienen cada una
la mitad, la elección se declara, no se hereda por fecha.**

## Un verificador obligatorio que moría en un rodaje sólo cámara (2026-08-18)

`verify_interviews.py` es de los obligatorios del paso 12b, y **reventaba con
`no such table: audio_sync_pairs`** en un proyecto sin audio externo. Esa tabla
la crean los scripts de sync, que un rodaje sólo cámara nunca corre. El propio
script ya tenía el patrón correcto para `face_detections` (comprobar si existe);
faltaba para ésta.

Es peor de lo que parece: si el verificador truena, **la garantía no corre**, y
quien mire sólo la última línea de la corrida no se entera de nada.

→ Crear la tabla vacía al arrancar. El `NOT IN` opera sobre cero filas, que es
exactamente lo que significa «ningún clip tiene sync».

### Y además gritaba lobo

Con la tabla creada, el verificador reportaba **5 de 5** entrevistas como
«entrevista marcada SIN sync». Ese caso existe para que el editor confirme que no
se perdió un lavalier; en un rodaje sin audio externo es el **único estado
posible**, así que salta en todas. Un verificador que grita lobo en cada proyecto
sólo cámara se deja de leer — y entonces tampoco avisa del caso que sí pierde
material.

→ Si el proyecto no tiene audio externo, ese caso pasa a informativo. El caso
grave —entrevista NO detectada— sigue siendo fallo.

→ **Ojo con cómo se pregunta «¿hay audio externo?»**: la primera versión contaba
`file_kind='audio'` y daba 5, porque los `.AAC` hermanos de los clips de cámara
lenta de la Osmo son archivos de audio… de 0.8 a 7.7 s. El criterio bueno es el
que la doctrina ya usaba para el waveform: **audio de 60 s o más**. Dos
definiciones de lo mismo en el mismo motor es una de más.

## `-v error` oculta lo que silencedetect escribe (2026-08-18)

Midiendo a mano si las entrevistas tenían pausas, `ffmpeg -v error -af
silencedetect` devolvía **0 silencios a cualquier umbral, incluso a 0 dB** —
donde todo el clip tendría que ser silencio. La causa: `silencedetect` escribe
sus `silence_start` / `silence_duration` a nivel **info**, y `-v error` los
tiraba. El motor lo hace bien (`detect_pauses.py` usa `-v info`); el fallo fue de
la comprobación manual.

→ **Un resultado imposible es un fallo de instrumento hasta que se demuestre lo
contrario.** El control que lo destapó cuesta una línea: correr con un umbral
absurdo (0 dB) y comprobar que el filtro dice algo.

## Garble plausible que sólo caza la comprensión: Gilles Lipovetsky (2026-08-18)

En una charla sobre el kitsch, Whisper escribió **«Gili Povetsky»** tres veces.
Ni el audit estadístico ni la corrección por vocabulario podían verlo: son dos
tokens que parecen un nombre propio y ninguno es palabra del español.

Lo destapó el propio transcript, dos frases más abajo: dice que esa persona
escribió *La Era del Vacío* y *La Tercera Mujer* y que acaba de publicar en
Francia *La Era del Kitsch*. Son los libros de **Gilles Lipovetsky**. El apellido
partido en dos por el fonema.

→ Es el caso de manual de la Capa 2c: **la evidencia para corregir un nombre
propio suele estar en el mismo transcript, unas frases más allá.** Leer, no sólo
contar.

→ El mismo material dio el patrón inverso y también útil: el nombre bueno del
artista, **Pedro Friedeberg**, lo acertó Whisper en un clip (`C1216`) y lo derivó
en los otros cuatro intentos del mismo texto — «Friedberg», «Fiedeberg»,
«Friede», «Fidel». **Cuando una pieza se rodó varias veces, los intentos son
lectores independientes del mismo nombre: el que más se repite, o el que encaja
con el mundo real, es el bueno.**

## Un dron DJI y una Osmo Pocket son indistinguibles para el matcher (2026-08-18)

El perfil `dji-drone` casa por `filename_prefix: ["DJI"]` y declara
`role=drone` + `audio.scratch=false`. Una **Osmo Pocket 3** escribe archivos con
el mismísimo patrón (`DJI_YYYYMMDDHHMMSS_NNNN_D.MP4`) y ninguna de las dos deja
Make/Model. Con el perfil del motor, el material de la Osmo queda fuera de
`--cameras main` y **sin transcript, en silencio**.

Es el mismo error que Victor ya corrigió a mano en IMODAE y que nunca se arregló
en el motor, porque **no se puede arreglar con el vocabulario de `match` actual**:
el único discriminador vive en `raw_metadata_json` → `format.tags.encoder`
(`DJI OsmoPocket3`).

→ Mientras tanto se resuelve con el **override por proyecto**
(`<disco>/.cinema_assistant/camera_profiles.json`), que es exactamente para lo
que existe. Añadir una clave `encoder` al matcher tocaría la generación de SQL de
todos los proyectos y no debe viajar de polizón en una entrega.

→ Y la comprobación que sí hay que hacer siempre: después de indexar, **cotejar
que `main_cam_sql` cubre los clips que crees** (aquí, 108/108) y que
`sql_python_conflicts` sale vacío.

## "El usuario es responsable de no mezclar material" no es una garantía (2026-08-18)

El script de proyecto aceptaba **cualquier** video del Media Pool y dejaba
escrito en un comentario que no mezclar rodajes era responsabilidad del editor.
Eso no es una garantía: es un aviso, y encima escondido en el código.

El caso llegó al día siguiente de escribirlo, y es el normal, no el raro: el
editor abrió el día 2 **en el mismo proyecto de Resolve** donde ya tenía los
reels del día 1. Consecuencia medida: los 178 clips del día 1 entraban como
`recs` con `info = {}`, sin `roll`, y por tanto no caían en A-ROLL ni B-ROLL ni
BTS — así que la garantía de cobertura los denunciaba a todos como clips
perdidos. **178 falsos positivos tapando los de verdad.** Un aviso que salta
siempre se deja de leer.

→ Un video que no está en el horneado **no es de este proyecto**: se salta y se
cuenta, con los primeros cinco nombres. Las timelines ajenas nunca corrieron
peligro (el borrado sólo mira las que empiezan por `PFX`), pero eso había que
comprobarlo, no suponerlo.

→ Probado que la rama se ejecuta: con un `C1194.MP4` del día 1 inyectado en el
pool del mock, el script reporta «Videos del Media Pool que NO son de este
proyecto: 1» y la cobertura cierra en `4/4 ✓` en vez de `1 de 5` incompleta.

### Y un error de presentación, que también cuenta

Al entregar, el comando de la Consola de Resolve se dio dentro de un bloque de
shell y con `echo` delante. El editor lo pegó tal cual en la Consola —que es
Lua— y le dio error. **El `dofile(...)` de Resolve no es un comando de shell y
no se escribe como tal.** Se entrega como una línea suelta, sin `echo`, sin
comillas envolventes y sin bloque `bash`, más el recordatorio de que la Consola
tiene que estar en modo **Lua** (en Py3 no existe `dofile`).

## Dos reglas de subtitulaje aplicadas en el orden equivocado (Morsa, 2026-08-19)

`build_cues()` ajustaba el CPS **y después** recortaba a `MAX_DUR`. El recorte
vuelve a subir los caracteres por segundo de lo que se acababa de ajustar, así
que el cue salía por encima del tope sin que nada lo dijera.

Medido sobre `cut 1.3.srt` de Morsa, 107 subtítulos ya entregados con el corte:
**18 cues por encima de 17 CPS, hasta 20.5**, con `MAX_CPS = 17` puesto. Y
**17 cues clavados exactamente en 6.000 s**, que es la firma del error: la frase
de Whisper duraba más, se recortó, y el texto se quedó en pantalla después de
que el hablante paró.

> Cuando dos reglas actúan sobre la misma magnitud, el orden **es** la regla. El
> tope de duración va primero; el CPS se comprueba sobre la duración final.

## La fusión de cues no miraba el tope de duración (Morsa, 2026-08-19)

El paso que junta dos cues consecutivos «del mismo aliento» hacía
`merged[-1]["end"] = c["end"]` sin comprobar nada. El recorte a `MAX_DUR` se
aplica **antes** de fusionar, así que la fusión podía deshacerlo.

De ahí salía el cue de **9.91 s** de `cut 1.3.srt`, con el tope en 6.0. Estaba
entregado. Ninguna prueba unitaria lo veía porque todas ejercitaban el reparto
en líneas, no la línea de tiempo.

Mismo patrón que la lección anterior, en otro sitio: **un paso posterior deshace
la garantía de un paso anterior**. Cuando una invariante importa, se vuelve a
cerrar al final, no sólo donde se estableció.

## Un SRT que termina antes que la pieza y no lo dice (Morsa, 2026-08-19)

`cut 1.3.srt` termina en **13:19.47** de un máster de **16:36**. Son **3:17
finales sin un solo subtítulo, el 20 % de la pieza**. El script imprimía
"107 subtítulos, 799.1s de habla" y salía con 0, que es exactamente el patrón
de la lección 40: un número que suena bien y no dice lo que falta.

Resultó ser la coda de concierto —no hay habla que subtitular— pero eso es una
conclusión, no un dato: hay que mirarlo para saberlo. Ahora `build_subtitles.py`
compara con la duración real del archivo y avisa, y `verify_subtitulos.py` lo
repite.

> Un entregable parcial se declara con su timecode. "Terminé" y "terminé hasta
> el minuto 13 de 16" no son la misma frase.

## Nadie medía el sonido del archivo entregado (Morsa, 2026-08-19)

El motor sabía medir el RMS de un tramo para decidir un corte, y nada más. El
archivo que sale por la puerta no lo miraba nadie. Al mirarlo por primera vez,
sobre los 14 exports de Morsa:

- **7 de los 14 clipean.** `Cut 1.mov` tiene **1,031,972 muestras** clavadas a
  fondo de escala —el 1.5 % del archivo— con true peak de **+2.4 dBTP**.
  `Morsa ultimate cut.mov`, el corte bueno, todavía tiene 32,411 y +1.0 dBTP.
- **La dispersión entre entregables es de 15.4 LU**, de −6.4 a −21.8 LUFS. No
  hay ningún número de referencia en el proyecto.
- El máster tiene **LRA de 20.6 LU**: el diálogo vive entre −22 y −29 LUFS y los
  últimos 2:06 van sostenidos por encima de −10. El espectador sube el volumen
  para oír la entrevista y se lo come el concierto.

El dato que lo delató **no es el true peak**, que no distingue un pico legítimo
de un archivo aplastado: es el **conteo de muestras a 0 dBFS** de `astats`
(`Abs Peak count`). Un pico a +2.4 dBTP puede ser una muestra; un millón es otra
cosa.

`bin/verify_audio.py` los mide todos y escribe `reports/audio-<fecha>.md`. **No
reprueba contra ningún objetivo**: decisión de Victor del 2026-08-19, el número
al que se entrega lo firma el editor. Pero ya no puede pasar que nadie mire.

Dos trampas de instrumento, por si se reescribe: `astats` y `silencedetect`
escriben a nivel **`info`** (con `-v error` el script sale limpio y sin una sola
medida), y el `Noise floor` de `astats` es un mínimo instantáneo que no sirve
para calibrar nada.

## El aviso de cola tapó un cue corto (cobertura de agosto dia 2, 2026-08-20)

`build_subtitles.py` cerró el SRT de un reel de 54.4 s con un aviso honesto:
*"sin subtítulos desde 00:00:47,960 hasta el final (6 s, 12 % de la pieza)"*.
La lectura cómoda es "son los segundos de la cortinilla". Y la cortinilla estaba
ahí —el frame de 53.8 s es el logo del cliente sobre naranja—, así que el aviso
parecía explicado y cerrado.

No lo estaba. `silencedetect` a **tres umbrales distintos** (−25, −30 y −35 dB)
coincide en que la voz **sigue hasta 49.6 s**: el último cue desaparecía **1.7 s
antes de que la hablante terminara la frase**. El VAD Silero cortó el segmento
donde la música de outro empieza a montarse sobre la voz, y ahí perdió el remate
del quiasmo ("...y el arte en una joya"). Whisper aislando el tramo 48.0 → fin lo
devuelve solo: `en una joya.`

Dos reglas que salen de aquí:

1. **Un aviso de cola no se cierra con una explicación plausible, se cierra con
   una medida.** Que exista una cortinilla no demuestra que no haya habla debajo.
   Cuesta un `silencedetect` comprobarlo.
2. **Auditar TODOS los cues, no el que el aviso señala.** Los otros once cerraban
   dentro de ±0.09 s de su habla; el defecto estaba en el único que el aviso
   mencionaba de refilón. La comprobación buena es la inversa: construir el mapa
   de habla y buscar tramos hablados **sin ningún cue encima**.

Vale como verificación de salida para cualquier SRT: mapa de habla por
`silencedetect`, y por cada cue —solape con el anterior, CPS, líneas, ancho de
línea, y si hay habla debajo—. Sobre el reel corregido: 12 cues, cero huecos,
CPS máx 16.3.

Y de la misma pasada, un garble que ningún estadístico ve (Capa 2c): la primera
transcripción dio **"joyas para hacer portadas"**; con más contexto y
`whisper-cli -ml 1`, **"joyas para ser portadas"**. Las joyas se portan, y el cue
siguiente dice "portarse cercano al corazón". Ambas frases son español correcto:
solo el sentido las separa.

## El TC de pegado salia de un fps supuesto (cobertura de agosto dia 2, 2026-08-22)

`build_subtitles.py` cierra diciendo en que timecode poner el playhead para pegar
el SRT al fotograma. Ese numero se calculaba con `--fps`, cuyo **default era
23.976**, y el script **nunca miraba el frame rate real del archivo** — aunque ya
lo sondeaba con ffprobe para saber su duracion.

Los dos reels de esta cobertura son 29.97. El resultado:

| reel | primer cue | TC impreso | TC correcto |
|---|---|---|---|
| uno | 0.130 s | `00:00:00:03` | `00:00:00:04` |
| otro | 0.290 s | `00:00:00:07` | `00:00:00:09` |

Un fotograma y dos. **El fallo es invisible**: el TC impreso tiene forma valida,
Resolve lo acepta sin protestar y el subtitulo queda corrido lo justo para que
nadie sepa por que. Solo aparece si alguien rehace la cuenta.

Arreglado: `fps_video()` lee `r_frame_rate` del archivo, `--fps` pasa a
`default=None`, y si no se puede leer se supone 23.976 **diciendolo en voz alta**.
Con `--timeline`, el fps de la timeline sigue mandando. Cuatro pruebas en
`tests/test_subtitulos.py`, comprobadas contra el codigo viejo.

La regla general: **un default numerico plausible es peor que ninguno** cuando el
dato esta en el archivo que ya tienes abierto. 23.976 no falla nunca de forma
ruidosa; solo produce un numero equivocado con pinta de correcto.

## El instrumento tiene que casar con el material (cobertura de agosto dia 2, 2026-08-22)

La verificacion que funciono en el reel anterior —mapa de habla por
`silencedetect`— **no sirve de nada** en un reel con cama musical continua: a
−25, −30 y −35 dB no encuentra una sola pausa hasta el segundo 52.8. No es que el
material no tenga pausas: es que la musica las tapa.

El instrumento que si sirve ahi: **una transcripcion independiente del archivo
entero con `whisper-cli -ml 1`** (un token por linea). Da tiempos de palabra que
la musica no enmascara, y de paso es una segunda opinion sobre el texto. Comparar
esa pasada contra el SRT es lo que delato que **el VAD se habia comido la primera
palabra del reel** ("Cada generacion cree que..." entraba como "generacion cree
que..."). Un subtitulo que empieza a media frase se lee como una decision de
estilo, no como un fallo.

Dos avisos de esa pasada:

- **`-ml 1` inventa palabras funcionales.** Dio "cambia **a** la mujer que lo
  interpreta" donde las otras dos pasadas dan "cambia la mujer". Partir en tokens
  fuerza al modelo a rellenar huecos. Se usa para TIEMPOS; para TEXTO manda una
  transcripcion normal, y si hay duda se resuelve por sentido.
- **Sobre musica sin voz, Whisper alucina creditos de subtitulado.** En la cola
  de este reel devolvio `CC por Antarctica Films Argentina`. Es un artefacto de
  sus datos de entrenamiento y, leido al reves, es una CONFIRMACION util: donde
  aparece uno de estos, no hay habla.

Tercer hallazgo, este de proceso: **regenerar un SRT tira las correcciones a
mano sin avisar.** El reel anterior se re-exporto, se volvio a correr el
generador y el SRT nuevo traia otra vez el garble y otra vez el cue corto. El
audio de las dos versiones era identico bit a bit (mismo md5 del PCM), asi que
las correcciones se reaplicaron tal cual — pero nadie lo habria notado. Al
re-exportar, o se re-verifica, o se parte del SRT ya curado.

## El transcript del MATERIAL resuelve lo que el export no puede (cobertura de agosto dia 2, 2026-08-23)

Un reel abria con una frase en ingles —"Vintage and Now"— dentro de una pieza
entera en espanol. Sospechoso por dos motivos: el idioma, y que **"y ahora" suena
casi igual que "and now"** con la palabra "vintage" cebando al modelo hacia el
ingles.

Tres transcripciones del export coincidieron en "Vintage and Now", una de ellas
con prompt en espanol. **Eso no prueba nada**: prueba que Whisper es consistente,
no que acierte. Y el intento de aislar el tramo sin el cebo salio peor —
recortar 0.95 s y rellenar con silencio devolvio `Gracias por ver el video.` y
`Thank you.`, alucinaciones puras. **Un fragmento de menos de ~2 s no se
transcribe: se alucina.** No sirve como evidencia y hay que decirlo en vez de
leer entre lineas.

Lo que si lo resolvio: **el transcript del clip fuente, en el manifest**. La toma
completa termina asi —

> ...y llevarlo a quien eres hoy. **Vintage and Now**. Ya esta... es como el
> titulo que le vamos a poner.

Estan proponiendo el titulo del reel al acabar la toma, y el montaje lo movio al
arranque como gancho. Habla real, bien transcrita, y encima explica su funcion.

**La regla:** cuando una frase del export es ambigua, el sitio donde mirar no es
el export —que llega ya cortado, sin lo de antes ni lo de despues— sino el
transcript del clip del que salio. El manifest lo tiene. Buscar por la frase
vecina (`grep -l` sobre `transcripts/*.json`) da el clip en un paso.

Tres cosas mas de la misma pasada, para la lista de verificacion de cualquier SRT:

1. **Ningun limite de cue puede caer DENTRO de una palabra.** Aqui dos lo hacian:
   uno cerraba en 38.213 con "epoca" sonando de 38.01 a 38.42, y otro en 35.390
   dentro de la primera silaba de "Tomar". Se ve solo si se cruzan los bordes de
   los cues contra los tiempos de palabra; ninguna regla de formato lo caza.
2. **El aviso de cola tiene un punto ciego en 5 s.** El umbral es `cola > 5`, y
   esta pieza dejaba 4.78 s sin subtitular: cero aviso. La cola se comprueba
   siempre, no cuando el script lo pida.
3. **CPS por encima del limite no siempre es culpa del reparto.** Antes de tocar
   nada, medir el tramo entero: 162 caracteres en 9.30 s son 17.4 car/s, asi que
   NINGUN reparto de esas mismas palabras baja de 17. Es la velocidad a la que
   habla. Lo unico que lo arregla es condensar, y condensar cambia las palabras
   de quien habla: eso lo firma el editor, no el asistente.

## El filtro anti-alucinación se comía las descripciones curadas a mano (2026-08-23)

`export_lua_data.py` descartaba **todo** `clip_curated_segments` de un clip con
`transcript_quality.is_hallucinated = 1`. La razón original es buena: el `full_text` que escribe
`enrich_curated_segments` se construye **pegando el transcript** (`"Escena | PM + Normal | Diálogo:
…"`), así que un A1 alucinado se propagaría literal a un marcador visible en Resolve.

**Pero no aplica a un tramo curado a mano.** Un `full_text` con `curated_by='claude'` se escribe
*mirando el frame*, no leyendo el A1: vale justo cuando el transcript no vale, y de hecho suele
decirlo («transcript alucinado, sin diálogo real»).

En el día 3 de CLIENTE_1 se perdían **7 de 67 tramos**, y en silencio — entre ellos «último
plano del día: el collar en primer término» y «arranca la sesión de exteriores». **Era justo el
recurso sin diálogo el que se caía, que es donde la descripción más falta hace para encontrar el
plano.** El bake decía `curated: 60` y nadie sumaba.

Arreglado en `bin/export_lua_data.py`: el filtro salta los tramos con `curated_by='claude'` y ahora
imprime `✓ curated_segments preservados: N`. Es el mismo razonamiento que la excepción de
`master_transcript` que ya vivía diez líneas más arriba para `question_segments`.

**La regla general:** un filtro de calidad debe mirar **la procedencia del dato que filtra**, no la de
un dato vecino. Aquí filtraba una descripción por la calidad de un transcript del que esa descripción
ya no dependía.

## `mock_resolve_full.lua` escribe su layout ficticio en el disco del proyecto (2026-08-23)

Correr `lua mock_resolve_full.lua asistente_<proyecto>.lua` deja un
`<disco>/.cinema_assistant/resolve/<proyecto>_layout.json` **con el material ficticio del mock**, que
son las cámaras de Morsa: `Ayan`, `Iban`, `Vic`. Un test escribiendo en datos de producción.

Cómo se ve: el layout del proyecto trae timelines y pistas con nombres de cámaras que no existen en
ese rodaje. Apareció primero como una rareza inexplicable en `despinoza_d2_layout.json` y se entendió
al reproducirlo en el día 3.

Por qué importa: `verify_track_order.py` **lee ese archivo**. Con el layout del mock encima,
verificaría el orden de pistas de un proyecto que no es el suyo y daría un OK que no significa nada.

Mientras no se arregle en el motor: **borrar el `*_layout.json` después de correr el mock completo.**
El layout bueno lo escribe el Lua cuando corre de verdad en Resolve. Y `verify_track_order` sólo se
puede correr después de esa pasada real, no antes.

## Un grupo de UNA toma no es una toma — pero partir los intentos sí rompe el A-roll (2026-08-23)

Las dos caras de `derive_takes` en un mismo proyecto:

- **De más**: abrió 12 grupos de un solo clip con charla de set. El motor ya sabe descartarlos
  (`HAVING COUNT(*) > 1`, lección del día 2) y bajan solos a B-roll. **No hay que tocarlos.**
- **De menos**: partió las dos piezas principales en ocho grupos porque **el vocativo se movía de
  sitio en cada intento** («Lo mexicano, mi querida Alice, no tiene…» / «Mi querida Alice, lo mexicano
  no tiene…» / «Lo mexicano no tiene que verse, mi querida Alice, jamás…»). El agrupador compara las
  12 primeras palabras, así que el arranque cambiaba y el texto era el mismo.

Sin los `take_overrides`, **seis intentos completos de las dos cápsulas principales habrían caído a
B-ROLL**, porque en un comercial el A-roll es el clip agrupado como toma y un grupo de uno no cuenta.

**No se baja el `--umbral`**: arreglaría estos dos casos y juntaría las cápsulas 1 y 2, que comparten
la frase «la moneda deja de ser moneda». Es conocimiento del editor y se declara.

**Señal para buscarlo en el próximo comercial:** si el número de grupos es mucho mayor que el número
de piezas que se rodaron, hay intentos partidos. Aquí fueron 22 grupos para 3 piezas.

## La segunda camara es una segunda opinion sobre las PALABRAS (cobertura de agosto dia 2, 2026-08-24)

Un reel traia dos frases que no cerraban: "¿que buscas en un accesorio para
sentirte **arreglar**?" (agramatical) y "No necesitas tener **cinco, siete**"
(le faltaba algo). El export decia eso, y re-transcribir el export lo repetia:
es el mismo audio, asi que la segunda pasada no es una segunda opinion.

Lo que si lo es: **la otra camara**. Esa pieza se rodo con Sony y Osmo a la vez,
tres intentos cada una — seis grabaciones del mismo texto, y dos de ellas del
MISMO instante por microfonos distintos.

| | Sony (el audio del export) | Osmo (misma toma) |
|---|---|---|
| | "para sentirte **arreglar**" | "para sentirte **arreglada**" |
| | "tener **cinco, siete**" | "tener **5 o 7**" |

Cuatro de las seis grabaciones dicen "arreglada"; la unica que dice "arreglar"
es justo la que se monto. Y con mas contexto, el propio export devuelve "cinco
**o** siete": la "o" estaba, el VAD la habia partido.

**La regla:** ante una frase dudosa en un rodaje multicamara, ir al transcript de
la OTRA camara del mismo momento. Dos microfonos en posiciones distintas fallan
de maneras distintas; donde uno se come una silaba, el otro suele tenerla. Es
mas barato y mas concluyente que discutir con el modelo sobre el mismo waveform.

Un tercer termino de la misma pieza NO se resolvio y se entrego tal cual: el
cierre "un tip de **madrazo**". Las seis grabaciones dan `madrazo`, `madraso`,
`madraza` y `Maradona` — todas el mismo sonido, ninguna decisiva, y es habla
coloquial mexicana que quien estuvo en el set resuelve de un oido. Se deja lo que
dice la toma montada y se pregunta. **No todo se cierra midiendo, y decir cual
quedo abierta es parte del entregable.**

Nota de reparto: en esta pieza cuatro cues quedaron sobre 17 CPS (uno en 24.4).
Medido, el tramo va a 19.5 car/s: ningun reparto de esas palabras baja de 17, y
robarle tiempo al cue vecino solo pasa el problema de sitio (probado: el 3 baja
de 24.4 a 22.8 y a cambio el 2 sube de 16.7 a 17.6). Cuando pase, ofrecer al
editor una condensacion CONCRETA —el texto exacto y el CPS que quedaria— en vez
de aplicarla: cambiar las palabras de quien habla es firma suya.

## Cortes que separan el verbo de su auxiliar (cobertura de agosto dia 2, 2026-08-24)

`build_subtitles` parte los cues por los segmentos del VAD, y eso deja
sistematicamente cortes que ninguna regla de formato caza porque el ancho de
linea, el numero de lineas y el CPS estan todos en verde. En una sola pieza
salieron tres:

| como quedaba | lo que se lee |
|---|---|
| `...una coleccion, estas` / `construyendo una identidad` | "estas" colgando al final de la tarjeta |
| `...puedes evolucionar, cambiar` / `colecciones, crear tendencias` | el verbo sin su objeto |
| `...la atencion hoy, tiene` / `que tener una vision` | "tiene" separado de "que tener" |

Los tres se leen como un tropiezo: el ojo cierra la frase donde la tarjeta se
acaba y llega la siguiente a desmentirlo. **La comprobacion que falta es
sintactica, no metrica**: ningun cue deberia terminar en auxiliar, preposicion,
articulo, conjuncion o pronombre relativo. Se arregla moviendo el limite a la
coma o al punto mas cercano, y casi siempre el CPS mejora de paso porque las
pausas de puntuacion regalan tiempo.

Aqui costo una subida de 17.7 a 18.7 CPS en un cue para que el corte cayera
limpio en la coma. Vale la pena: 1 CPS no se nota, una palabra colgando si.

## Dos avisos sobre las fuentes de verdad

**El transcript del material tambien se equivoca.** En esta pieza decia "una
vision que le **permite** seguir siendo relevante"; el export, transcrito dos
veces, dice "**permita**", y las otras dos tomas completas tambien. La forma
correcta es el subjuntivo. Es decir: el clip fuente sirve para dar CONTEXTO que
al export le falta —lo de antes y lo de despues, que fue lo que resolvio el
titulo "Vintage and Now" y la palabra "arreglada"— pero no es una autoridad
superior sobre el mismo audio. Cuando ambos ven el mismo momento, se cuentan
votos; cuando solo el fuente ve el contexto, manda el fuente.

**Un verificador propio puede dar falsos positivos y hay que leerlo, no
obedecerlo.** El chequeo de "el cue sale antes de que acabe su ultima palabra"
tomaba la ultima palabra que SOLAPA la ventana del cue, no la ultima palabra de
SU texto. Con un cue que cierra con 80 ms de aire sobre la primera palabra del
cue siguiente —que es lo correcto— cantaba un fallo inexistente. Comparar
siempre contra las palabras propias del cue.

## `-ml 1` sin `-sow` fragmenta las palabras, y el motor ya sabia esto (cobertura de agosto dia 2, 2026-08-25)

Para partir cues sin perder las correcciones a mano, hacia falta el tiempo de
CADA PALABRA de cuatro reels ya curados. La forma obvia parecia
`whisper-cli -ml 1` (un token por linea) — es lo que se uso en toda la sesion
para verificar frases sueltas, y ahi nunca importo.

Aqui si importo, y goloe: un cue que debia partirse en "...cinco o siete, sino"
/ "una en particular..." salio con **80 ms de duracion** (CPS de 437) en el
segundo trozo. La causa: `-ml 1` sin mas parte por TOKEN, no por palabra —
"necesitas" sale como `neces` + `itas`, dos lineas distintas. Comparar esos
tokens contra las palabras del SRT por igualdad de texto falla en cualquier
palabra de mas de una silaba-BPE, y la interpolacion que rellena los huecos se
dispara a donde sea que el ultimo match casual haya caido.

**El motor ya tenia la respuesta**: `lib/transcribe.py` pide siempre
`-ml 1 -sow` juntos — `--split-on-word` esta documentado en el propio
`whisper-cli --help` como *"split on word rather than on token"*. Con `-sow`,
"accesorio" sale entero en una sola linea. La leccion no es del motor, que
nunca tuvo el bug: es de trabajar en la terminal por fuera de sus
convenciones. **Cualquier pasada de whisper-cli hecha a mano para verificar
timing de palabra necesita `-sow`**, no solo `-ml 1`.

Segunda vuelta de tuerca, para cuando `-sow` no este a mano o el texto venga de
un dump ya hecho sin el: el formato de `whisper-cli -ml 1` es
`[ts --> ts]  TOKEN` con **dos espacios fijos**, y el propio TOKEN trae o no un
tercer espacio inicial — CON espacio = empieza palabra nueva, SIN espacio =
fragmento que continua la anterior. Es la unica marca de donde empieza cada
palabra, y un `.strip()` de conveniencia la borra sin avisar. Reconstruir
palabras completas es fundir tokens consecutivos sin ese espacio en el token
anterior, sumando su rango de tiempo.

## El candado de MIN_DUR se comía a sí mismo (CLIENTE_1 día 3, reel 0012, 2026-08-27)

En `build_subtitles.py`, un cue corto pegado entre dos frases largas —aquí, «¿Por qué una moneda?»,
con huecos de 20 ms a cada lado— pasaba por dos pasos que se anulaban:

1. `end = max(end, start + MIN_DUR)` fuerza el mínimo de 1.0 s, **sin comprobar contra el siguiente
   cue**. El cue creció hasta chocar con el de después.
2. El "candado final" veía el choque y **encogía el mismo cue** hasta `max(a_start + 0.4, b_start −
   0.02)` — un piso de **0.4 s**, la mitad del mínimo que la línea anterior acababa de exigir.

Resultado: el cue salía a 0.88 s, exactamente por debajo del mínimo que el propio script acababa de
imponerse. `verify_subtitulos.py` lo cazó (*"dura 0.88s, minimo 1.0"*); el SRT ya había salido así.

**La corrección que ya existía en el archivo, aplicada tres pasos antes de donde hacía falta.** Más
abajo, el bloque del imán (paso 5, sólo corre si hay `cortes_s` declarado con `--timeline`) ya resolvía
este mismo choque intentando **retrasar el cue siguiente** antes de aplastar el corto. Ese bloque nunca
llegaba a correr en el caso más común: un reel para subir a redes, generado **sin** `--timeline` (es la
doctrina — *"sin --offset-tc da tiempos desde 0, que es lo que hace falta para subir el reel"*). El
candado del paso 3 corre siempre y no tenía ese respaldo.

Arreglado replicando la misma estrategia en el paso 3. Los 81 tests de `test_subtitulos*` seguían en
verde antes del cambio —ninguno cubría un cue corto flanqueado por huecos casi nulos sin `cortes_s`— y
siguen en verde después.

**Patrón que se repite en esta sesión:** una garantía que se declaró en un sitio (aquí, MIN_DUR) se
deshace en el paso siguiente sin que nada lo diga. Cuando una invariante importa, hay que cerrarla en
CADA punto donde algo posterior pueda romperla, no sólo donde se estableció — es la misma lección que
ya vive arriba en este archivo sobre A-roll/B-roll y sobre `question_segments`.

## Una regla de vocabulario propia deshacía otra regla propia, en el mismo texto (CLIENTE_1 día 3, reel 0013, 2026-08-27)

`load_vocab()` en `build_subtitles.py` construye las reglas en tres bloques —`vocabulary_fixes`,
`vocabulary_hints`, `vocabulary_notes`— y `apply_vocab()` las corre **en ese orden, cada una sobre el
texto que dejó la anterior**. Un `fixes` que capitaliza una frase corre primero; un `hints` de la MISMA
palabra en minúscula corre después y la vuelve a bajar.

Pasó exactamente así: se agregó `"maximalismo puro": "Maximalismo puro"` a `vocabulary_fixes` para
arreglar un cue que arrancaba en minúscula tras un punto. El SRT regenerado seguía en minúscula. La
causa: `vocabulary_hints` ya traía `"maximalismo"` (dos veces, una heredada del día 2 y otra agregada
esa misma sesión) — un hint construye su regla como `\bmaximalismo\b` case-insensitive **reemplazando
por la cadena tal cual se escribió en la lista**, o sea, siempre minúscula. Corría después del fix y lo
pisaba.

**No se dio la corrección por buena sin releer el archivo regenerado.** Si no se hubiera vuelto a abrir
el SRT, habría salido así.

El hint nunca hacía falta: los garbles reales de "maximalismo" en este proyecto eran otras palabras
("Maximila...", "Maximilizar"), y un hint sobre la grafía CORRECTA no puede cazar una grafía distinta —
sólo arregla mayúscula/acento de una palabra que Whisper ya escribió bien. Sin ningún caso que resolver,
sólo quedaba el efecto colateral. Se retiraron las dos copias.

**La regla que generaliza:** en un sistema con varias capas de corrección aplicadas en secuencia sobre
el mismo texto, una regla nueva puede ser corregida-y-correcta y aun así no sobrevivir si una regla más
tardía en el orden de aplicación toca la misma palabra con otro criterio. Antes de dar una corrección
por resuelta, mirar si algo más en la cadena habla de la misma palabra — y comprobar el archivo de
salida, no sólo el diff de la regla que se acaba de escribir.

---

# Lecciones traídas de la copia de Adrián (2026-08-28)

Adrián corrió una bifurcación de este motor desde AVA (20-jul) sobre tres proyectos
suyos: **The Shelter** (concierto, 2-4 ago) y **Central de abastos** (27 ago), más una
sesión del 10 de agosto de **FILM CLUB CAFÉ** — que es el MISMO proyecto de las lecciones
FCC de julio, no otro con el mismo nombre. Lo que sigue es lo que de ahí resultó **nuevo
para este motor**, comprobado contra el código de aquí antes de escribirse.

Lo que él trajo y aquí ya estaba resuelto no se copió: `bootstrap.sh` y `config/` (ya
arreglado), el default `%audios%` (ya lo caza `lib/guards.assert_selected`), el
emparejamiento multicám por categoría (es nuestra lección 52), y el signo de
`refine_offset` — ver la nota al final de "El signo del offset, y las compensaciones que
lo escondían".

Tres de sus lecciones citan herramientas que **no existen en este motor**
(`lib/onset_envelope.py`, `bin/derive_sync_from_skew.py`,
`bin/sweep_sync_clock_consensus.py`, `resolve/mock_resolve_stacked.lua`). Se escriben
como método, no como receta: el número de la herramienta es suyo, el razonamiento es
portable.

## `identity_score` falta en cinco `CREATE TABLE` y truena en proyecto fresco (Adrián, The Shelter 2026-08-02)

`bin/init_sync_schema.py` declara `identity_score REAL` y sabe añadirla por `ALTER TABLE`
a una base vieja. Pero **cinco scripts crean `audio_sync_pairs` por su cuenta** con
`CREATE TABLE IF NOT EXISTS` y ninguno la declaraba: `sync_acoustic.py`, `sync_audio.py`,
`sync_transcript.py`, `sync_waveform.py` y `verify_interviews.py`.

En un proyecto fresco el orden decide: si cualquiera de esos cinco corre antes que
`init_sync_schema.py`, la tabla nace incompleta y `sync_siblings_rule.py:144` —que la lee
por SELECT explícito— truena la primera vez que se aplica la regla de hermanos.

**La regla**: cuando una tabla tiene un dueño del esquema, los demás no la crean con una
definición paralela. O importan la del dueño, o la copian entera y se comprueba que las
copias no divergen. Un `CREATE TABLE IF NOT EXISTS` repetido en N sitios es N
oportunidades de que el esquema se bifurque en silencio; el `IF NOT EXISTS` garantiza
justamente que **la primera versión que corra gana y las otras callen**.

## `sibling_audio_id()` adivinaba la convención de carpetas del rodaje (Adrián, The Shelter 2026-08-02)

`bin/sync_siblings_rule.py:58` sabía encontrar el canal hermano de un lavalier solo si las
carpetas se llamaban `/Dr/` y `/Izq/` — la convención de Zezzions, hardcodeada. Cualquier
otra (`Audio/01/`+`Audio/02/` en The Shelter, `Audio/izquierdo/`+`Audio/derecho/` en
Central de abastos, `TX1/`+`TX2/` en FANTÁSTICO CÓMICS) devolvía `None` y **todo salía
"sin-hermano" en silencio**: no por falta de par real, sino por el heurístico incompleto.

Adrián lo arregló añadiendo su par de patrones y admitiendo que "es una función que crece
por proyecto, como MAIN_CAM crece por cámara nueva". **Aquí se arregló al revés**, porque
este motor ya tiene la regla contraria escrita y probada: lo que es del rodaje se
**declara** en `<disco>/.cinema_assistant/project_config.json` y el motor no lo infiere
(ver `lib/timeline_resolve.py:204`, `lib/dialogue_density.py:215`,
`lib/project_vocabulary.py:53`, y la lección 53 sobre sacar los datos del código).

La función lee ahora `cadenas_lavalier` del `project_config.json`:

```json
"cadenas_lavalier": [["Audio/izquierdo/", "Audio/derecho/"], ["TX1/", "TX2/"]]
```

`/Dr/`↔`/Izq/` se conserva como fallback por compatibilidad con Zezzions. **Una función
que crece por proyecto es una función que hay que recordar tocar**; una que lee la
declaración del proyecto no se olvida, porque el proyecto que no la declara no encuentra
hermanos y eso se ve.

## Los onsets de música son PERIÓDICOS: un z alto no prueba nada (Adrián, The Shelter 2026-08-03)

Para material musical el feature correcto no es la energía sino el **onset** (spectral
flux): el envelope log-RMS mide *cuánta* energía hay, y dos cámaras en puntos distintos de
una sala captan mezclas distintas (otro balance, otra reverb, otro público encima), así
que sus envelopes no correlacionan aunque sea el mismo instante. Medido por Adrián:
cámara contra lavalier daba z≈2, ruido puro. El golpe de batería, en cambio, ocurre en el
mismo instante en toda la sala — la posición cambia el timbre y el nivel, no el momento.
El mismo material reprodujo **8 offsets conocidos con error ≤ 0.04 s**.

Esto confirma desde otro material lo que aquí ya sabíamos ("probar voz/full/onset y
quedarse con la mejor", AVA/FCC). **Lo que es nuevo y caro es la trampa**:

> Los onsets de música son PERIÓDICOS. Un offset corrido por compases enteros correlaciona
> casi igual de bien. En el control negativo, un offset **aleatorio** llegó a **z = 10.4**:
> indistinguible de un match real.

Es la misma patología del fixture de beeps de `tests/media_prueba` y la del caso Iban. Un
z alto sobre señal periódica no es evidencia; es aritmética.

**Los tres candados que sí discriminan** (hay que poner los tres, atacan fallas distintas):

1. **Ventana de búsqueda centrada en la predicción del reloj** — mata los falsos lejanos.
2. **Acuerdo entre energía y onsets dentro de 0.5 s** — mata la ambigüedad de beat,
   porque la energía no comparte esa periodicidad.
3. **`|desviación vs predicción| <= 2 s`** — mata los enganches pegados al borde de la
   ventana. Medido: 126 de 128 candidatos dentro de 2 s (mediana 0.48 s); los 2 que no,
   eran los sospechosos.

## Las timelines de Resolve no empiezan en el frame 0 (Adrián, The Shelter 2026-08-04)

`recordFrame` es un frame **absoluto** de la timeline, y las timelines de Resolve arrancan
por defecto en **01:00:00:00 = frame 86400** a 24 fps. Colocar en `0..N` deja todo fuera
de rango: los contadores de pista dicen que hay clips y no se ve ninguno. El síntoma que
reportó el editor fue *"algo ocurrió que ahora falta mucho material... los tracks dicen
que hay más clips pero no están"*.

```lua
local tlStart = 0
if tl.GetStartFrame then
  local s = tl:GetStartFrame()
  if type(s) == "number" then tlStart = s end
end
-- y SIEMPRE: recordFrame = tlStart + math.floor(pos * fps + 0.5)
```

**Aquí no ha mordido todavía y conviene saber por qué**: nuestro Lua posiciona con
`math.floor(item:GetStart()) + recordOffsetFrames`, y `item:GetStart()` ya viene en frames
absolutos. La trampa está armada para el día en que se coloque algo por **tiempo
calculado** en vez de relativo a un item existente — que es exactamente lo que pide el
apilado multicámara.

**Meta-lección, que es la que vale**: el mock daba todo OK con el bug presente, porque no
modelaba el frame de arranque. Un mock que no reproduce el entorno real da confianza
falsa.

## El `mtime` miente, y de tres maneras distintas (Adrián, The Shelter 2026-08-04)

Tres casos medidos, el mismo día y el mismo proyecto:

- **Lavaliers del canal 02**: `mtime` corrido **~1.93 s** respecto al contenido real. El
  contenido de los dos canales está a 0.07 s; sus `mtime` dicen 2.0 s.
- **CCTV**: `mtime` corrido **6 horas** (zona horaria del equipo). El nombre del archivo sí
  estaba en hora local.
- **Todos**: `mtime` tiene resolución de **1 segundo**, así que `t0 = mtime − dur` arrastra
  hasta 1 s de error de cuantización aunque el reloj esté bien.

**La regla**: el lavalier se coloca SIEMPRE relativo a su clip con el offset medido del par
(`sourceStart = -offset`), nunca por posición absoluta. El offset es una medida **física
del contenido**; el `mtime` es un metadato del sistema de archivos y puede estar mal por
horas. Adrián rompió un sync que ya estaba bien al colocar por tiempo absoluto.

Para lo que sí necesita tiempo absoluto (apilar cámaras), guardar un `t0` **medido** en
tablas dedicadas (`audio_time_base`, `video_time_base`) con el método y la justificación, y
que el exportador las prefiera sobre el `mtime`.

## El reloj de un Rode Wireless PRO puede ir una HORA corrido (Adrián, Central de abastos 2026-08-27)

Los `mtime` de los WAV del Rode ponían la grabación de 05:04 a 07:56, mientras las cámaras
rodaban de 06:00 a 08:45. Con ese modelo, los últimos 50 minutos de rodaje —incluida una
entrevista de 13.5 min— quedaban **sin audio**. Era falso: el reloj del grabador iba
**+3573 s** (una hora menos 27 s; seguramente horario de verano mal puesto en la app).

**Cómo se detectó** (el método es lo que hay que retener): búsqueda de waveform a ciegas
sobre el rango completo, de **dos clips largos separados 2 h entre sí**, contra los WAV de
la cadena. Los dos anclaron con prominencia ~0.75 en archivos distintos y arrojaron **el
mismo drift** (+3574.1 y +3571.7 s). Un drift compartido por anclas independientes no es
coincidencia: es el reloj.

Con la corrección, la cobertura pasó de "50 min sin audio" a **164 de 168 clips**.

**La regla**: cuando el rango del audio no cubre el del video, NO concluir "no se grabó".
Medir dos anclas largas a ciegas y comparar el drift primero. Aplica directamente a este
motor: aquí se rueda con Rode Wireless PRO.

## El timecode de las Sony FX30 es REC-RUN: no sirve como reloj (Adrián, Central de abastos 2026-08-27)

Tentador: el XML sidecar (`*M01.XML`) trae `LtcChangeTable` con timecode a resolución de
frame. Pero por default la FX30 corre el TC en **rec-run** — avanza solo mientras graba.
Verificado sobre 99 clips: `dTC` acumulaba exactamente la duración grabada (3042 s)
mientras `dCreation` acumulaba el tiempo de pared (9872 s).

Sirve para orden y para duración exacta; **no** para ubicar clips en un reloj común. Para
eso, `CreationDate` del XML (= `creation_time` del contenedor MP4, en UTC) — pero solo a
**resolución de 1 segundo**, así que un offset derivado solo del reloj arrastra ±0.5 s
(±12 frames a 23.976).

Comprobar antes de confiar:

```
max |dTC − dCreation| < 3 s   ->  free-run (sirve como reloj)
crece con el material          ->  rec-run (NO sirve)
```

Aplica a casi todo lo que se rueda aquí: dos FX30 son la configuración habitual.

## Colocar audio por tiempo ABSOLUTO exige tres correcciones a la base de tiempo (Adrián, Central de abastos 2026-08-27)

Colocar cada lavalier **relativo a su clip** con el offset del par es correcto y no depende
de ningún reloj. Pero cuando el editor pide **los WAV enteros** (para abrir la grabación
continua y buscar dentro), ya no se puede: un archivo entero solo tiene una posición
posible, y esa posición es absoluta.

Para que la colocación absoluta sea tan buena como la relativa, el `t0` de cada audio
necesita tres cosas:

1. **`audio_time_base` medido**, no `mtime − dur`.
2. **Constante dual-lav impuesta entre cadenas.** Los delta por archivo se ajustan con los
   votos de *sus* clips, que arrastran el truncado a 1 s de `creation_time`; con n=10 votos
   la incertidumbre es ~0.16 s. La relación **entre cadenas**, en cambio, se mide directo
   envelope-contra-envelope con MAD de 5 ms. Imponerla bajó la discrepancia del `t0` por
   clip de 55 ms a **5.4 ms** de mediana. El residuo se reparte a la mitad entre las dos
   cadenas: conserva el punto medio, que es el promedio de dos ajustes independientes.
3. **Continuidad en los cortes automáticos.** Un archivo que llega al límite de corte
   (3600.23 s en el Wireless PRO) es seguido SIN pérdida de contenido. Con los `t0`
   ajustados por separado, el siguiente arrancaba 0.46 s **antes** de que terminara el
   anterior — físicamente imposible, y en la timeline habría encimado dos WAV en la misma
   pista. Regla: `dur ≈ límite de corte` → el siguiente arranca exactamente donde termina
   éste. Un archivo más corto significa parada real del grabador y su `t0` medido se
   respeta.

Ninguna de las tres se nota con colocación relativa: el sync sale bien igual y el error
queda escondido en la posición absoluta. Por eso conviene imponer las tres **siempre**.

## Nombres de archivo REPETIDOS entre carpetas hacen que Resolve se salte audios, en silencio (Adrián, Central de abastos 2026-08-27)

Lo encontró el editor mirando la timeline: *"faltó el 00022 del lava izquierdo"*. El
asistente había reportado "8 de 8 WAV colocados" y era falso en Resolve.

**Causa**: los grabadores numeran por unidad, no por proyecto, así que las dos cadenas
traen nombres que chocan:

```
Audio/izquierdo/00022_Wireless PRO.WAV
Audio/derecho/00022_Wireless PRO.WAV     <- mismo nombre, otro archivo
```

`mp:ImportMedia({ruta})` **no devuelve item** cuando Resolve considera que ese nombre ya
está en la carpeta destino. Y el código hacía `if imp and imp[1] then ... end` sin `else`:
el segundo archivo de cada par se caía sin decir nada.

**El arreglo, en tres capas**:

1. Resolver SIEMPRE **por ruta exacta**, nunca por nombre — con nombres repetidos, resolver
   por nombre pone el audio de la OTRA cadena, que es peor que no ponerlo. Orden: caché por
   ruta → `ImportMedia` → re-escaneo del Media Pool por ruta exacta → importar dentro de un
   bin propio (la deduplicación es por carpeta, así que en otro bin sí entra) → re-escaneo.
2. **Contador y aviso ruidoso** de audio faltante, con assert de cierre. La omisión
   silenciosa era el defecto de fondo, no la colisión de nombres.
3. **El mock tiene que modelar la deduplicación por carpeta.** Sin eso, el verificador vive
   en un mundo donde importar siempre funciona.

**Regla de arranque de proyecto**: buscar nombres repetidos entre carpetas de origen y
anotarlos. Un `SELECT filename, COUNT(*) ... GROUP BY filename HAVING COUNT(*) > 1` sobre
el manifest cuesta un segundo y predice esta clase de bug antes de que la vea el editor.

Aquí tenemos exactamente esa exposición: `nuestros` scripts por proyecto importan con
`if imp and imp[1] then` en `asistente_<proyecto>.lua` (líneas 261 y 697 del de FANTÁSTICO
y sus copias), sin rama `else` y sin contador.

### Otro síntoma del mismo choque: un item con la ruta de una cadena y los datos de la otra (Asistente, 2026-09-28)

Las dos cadenas de Rode traían un `00034_Wireless PRO.WAV`: `Audio izq/00034` (21:14,
grabado ~19:17) y `Audio drc/00034` (31:16, grabado ~15:34). En el proyecto que el
editor armó a mano, Resolve tenía **un solo item** con `File Path` = `Audio izq/00034`
pero con la duración (00:31:14:13) y las fechas (creado 15:34:26, modificado 16:05:42)
del de `Audio drc`. Esa era la pista A2 de su timeline de prueba: el sync no podía cuadrar.

Nadie lo había tocado a propósito. Lo más probable es que el editor importara uno, que
Resolve relinkeara por nombre, y que el item conservara los metadatos del primero.

**Cómo se ve sin abrir el audio**: `GetClipProperty("Duration")` y `"Date Modified"` de
un item, contra `ffprobe` y el `mtime` del archivo al que apunta su `File Path`. Si no
cuadran, el item no es lo que dice su ruta.

**Qué se hizo** (sin tocar el bin del editor): importar un item fresco de cada WAV en un
bin propio creado DESPUÉS del suyo. El aplicador y `merge_pool.lua` indexan el Media Pool
por ruta y gana el último item que recorren, así que usan el fresco. El item confundido
queda en su bin y se reporta; decidir qué hacer con él es del editor.

## `{}` es TRUE en Lua: se da por colocado lo que Resolve rechazó (Adrián, Central de abastos 2026-08-27)

`mp:AppendToTimeline(...)` devuelve una **lista vacía** cuando Resolve rechaza la
colocación. No `nil`: `{}`. Y en Lua `{}` es verdadero.

```lua
local res = mp:AppendToTimeline({{...}})
if res then nLav = nLav + 1 end     -- cuenta como exito algo que NO se coloco
```

El contador subía, no se imprimía ningún aviso, y el verificador —que contaba las llamadas
registradas, no los items de la timeline— confirmaba el éxito. Tres capas mintiendo a la
vez.

**Las reglas**:

1. **`if res and #res > 0 then`**, siempre, en cada `AppendToTimeline`. Revisar TODOS los
   sitios del archivo, no el que dio el síntoma.
2. **Verificar contando lo que quedó**, no lo que devolvieron las llamadas:
   `#tl:GetItemListInTrack("audio", trk)` al final, contra lo esperado, e imprimirlo
   siempre — pase o falle.
3. **Reintento con destino alternativo**: si la pista designada rechaza, agregar una pista
   nueva AL FINAL y colocar ahí, avisando en qué pista quedó. Nunca reintentar sin
   `trackIndex`: Resolve lo manda a A1 y tapa el audio de cámara intocable.
4. `AddTrack` puede fallar; un `while` sin cota cuelga Resolve. Acotar el bucle.

**El estado aquí, que es lo interesante**: `resolve/asistente_lib.lua` **ya hace las cuatro
cosas bien** (`res and #res > 0`, avisos `[corte-FAIL]` / `[audio-FAIL]`, reintento en
pistas más altas con `for intento = 0, 3`). Los que están mal son los **scripts por
proyecto**: en `asistente_fantastico.lua:470-476` (y sus copias en avalanches, fcc,
despinoza_d3) conviven `if not res then ... failed + 1 end` y `if res then placed + 1 end`
sobre el mismo `res`, mientras que veinte líneas más abajo el camino de multicám sí
comprueba `res and res[1]`.

Es exactamente la divergencia que predijo la lección "Los scripts de proyecto se copiaban a
mano y fueron divergiendo": **el arreglo llega a la librería y no al script del proyecto**.
Refuerza el pendiente ya anotado allí — mover la orquestación a `LIB.correr(CONFIG)`.

## La costura entre archivos partidos no cae en frame entero, y Resolve rechaza el solape (Adrián, Central de abastos 2026-08-27)

Causa REAL de "falta el 00022 del lava izquierdo". Las dos lecciones anteriores arreglaron
cosas ciertas pero no eran ésta: los 8 WAV estaban en el Media Pool con la ruta exacta. No
era importación, era colocación.

Un WAV de 3600.229 s a 23.976 fps son **86 319.2 frames**. No es entero. El archivo
siguiente empieza donde termina éste *en segundos*, pero al cuantizar a frames aterriza
**1 frame dentro** del anterior:

```
00022  rf=  4408  largo=86320 fr  fin= 90728
00023  rf= 90727                        <-- 1 frame ENCIMA
```

Resolve rechaza colocar sobre algo ya puesto y devuelve `{}`, así que el archivo desaparece
sin aviso. **Cuál desaparecía dependía del orden arbitrario de `pairs()`**: el que se
colocara segundo era el que chocaba.

**El arreglo**:

1. **Colocar en orden CRONOLÓGICO por pista**, nunca en orden de `pairs()`. Con orden
   arbitrario el fallo es no determinista y no se puede razonar.
2. **Preguntarle a Resolve dónde terminó de verdad** cada item (`item:GetEnd()`, o
   `GetStart()+GetDuration()`) y arrancar el siguiente en `max(su sitio, fin del
   anterior)`. No adivinar si Resolve redondea hacia arriba o hacia abajo: leerlo. El
   corrimiento es de 1 frame como máximo y solo cuando de verdad chocarían.

**Dónde más vigilar esto**: cualquier timeline donde se coloquen medios ADYACENTES por
tiempo absoluto. En un empaquetado que colapsa huecos los clips quedan pegados por
construcción, así que la probabilidad de solape de 1 frame es alta en toda la timeline.

**El mock tuvo que aprender tres cosas para poder cazarlo** —y es la cuarta vez en ese
proyecto que aparece el mismo patrón:

- devolver `{}` al rechazar, no un item nuevo siempre;
- dar a los items un largo REAL en frames (`ceil(dur × fps)`), no 100 fijo;
- correr a **23.976**, no a 24. Con 24 las duraciones caían casi en frame entero y el
  solape no aparecía. *El mock corría a un frame rate que el proyecto no usa.*

**Cuando un verificador dé un resultado perfecto, la pregunta correcta no es "¿pasó?" sino
"¿qué comportamiento real del sistema no estoy modelando?".**

## La carpeta es la tarjeta, no la cámara: una tarjeta que cambia de cuerpo mezcla dos relojes (Asistente 2026-09-28)

En el rodaje del video del asistente, la tarjeta de `Video 02` grabó primero en la FX30 de
Adrián (`ASA_9754-9769`, 16:19-16:49) y desde ~18:37 en la de Victor, que siguió el contador
de la tarjeta (`VICG_9770-9780`). `inventario_camaras.py` los separaba bien, porque clasifica
por prefijo. Pero `derive_chrono_sync.py`, `export_multicam_lua.py` y el `folder` del bake
agrupan por **carpeta**, así que para la cronología y la multicám `Video 02` era UNA cámara.

**El daño no avisaba.** El skew del grupo `Video 02` salía de los clips de Victor, que sí
tenían lavalier, y se aplicaba a los de Adrián, que no tenían: sus clips iban a la
cronología con el reloj de otra cámara. Medido en proyectos anteriores, esos dos relojes se
separan 15 s (FCC, junio), 25 s (Fantástico, julio) y 36 s (Morsa, agosto). Además, la
"timeline por cámara" habría salido con dos cámaras dentro, y la de Victor en dos pistas
distintas según la tarjeta.

**Arreglo**: `lib/proyecto.py:grupo_de_camara()`, que usan los tres scripts. Se declara en
`project_config.json`:

```json
"camaras_por_prefijo": {"VICG": "Vic", "ASA_": "Ayan", "Screen Recording": "Pantalla"}
```

Sin la clave, se agrupa por carpeta, como siempre. Con ella, un cuerpo en dos tarjetas es un
solo grupo, y una tarjeta con dos cuerpos son dos. `camara_lavalier` pasa a nombrar el grupo
(`"Vic"`), no una carpeta.

**Lo que el arreglo NO resuelve, y se dice**: un cuerpo sin lavalier en su ventana no tiene
skew medible. Queda con su reloj crudo y el aviso del aplicador. En Asistente es el caso de
Adrián: sus clips son macros de pantalla, sin evento que alinear con la grabación de pantalla.

**Regla de arranque**: si una carpeta de video trae más de un prefijo, declarar
`camaras_por_prefijo` ANTES de la cronología. Se ve con
`SELECT parent_folder, substr(filename,1,4), COUNT(*) FROM clips WHERE file_kind='video' GROUP BY 1,2`.

## El orquestador daba por hecho `AUDIOS/`: en un proyecto plano los lavalieres no se transcribían (Asistente 2026-09-28)

`transcribe_audios.py` selecciona por `rel_path LIKE '%audios%'`, que es el layout de
ESCALANDO MEXICO, y el paso `transcribir-audios` de `run_pipeline.py` no le pasaba otro
filtro. En un proyecto con `AUDIO/` o `Audio/` (Asistente y Morsa) no casaba nada. El script
lo gritó bien ("TRABAJO CERO... BUG DE SELECCION hasta demostrar lo contrario"), pero el
orquestador lo registró como `sin-trabajo` y **siguió hacia el sync sin un solo transcript de
lavalier**. Se cortó a mano en `analyze-segments`.

**Arreglo**: `_args_transcribir_audios()` en `run_pipeline.py` saca `--audio-like` de
`mapa_audio`: con una sola carpeta de audio pasa `'<carpeta>/%'`. Con prueba en
`tests/test_orquestador.py`.

**Lo que queda abierto**: que un paso `abort` que reporta selección vacía cuente como
`sin-trabajo` y no como fallo. Aquí el aviso existía y nadie lo escuchó.

## El corrector de vocabulario se comía la puntuación del `words` (Asistente 2026-09-28)

`correct_transcript_words()` sustituía la palabra entera: `"10.50."` quedaba en `"Diez50"`,
`"Cloud..."` en `"Claude"` y `"Bernie,"` en `"Berni"`. `correct_text()` ya conservaba la
puntuación, así que el `text` y el `words` de un mismo transcript dejaban de decir lo mismo.
Además, el punto final es la frontera de oración que usa el propio corrector (el fuzzy no
toca la primera palabra de una oración) y la que usan los cortes de subtítulos.

**Arreglo**: `_con_puntuacion()` en `lib/project_vocabulary.py` devuelve la corrección con la
puntuación de la palabra original, en las cuatro ramas de reemplazo. Con pruebas en
`tests/test_vocabulario.py`, que no existía.

**Para rehacer un proyecto ya corregido**: el corrector deja un `legacy_vocab_<id>.json` la
PRIMERA vez que toca un transcript, y nunca lo pisa. Ese respaldo es el Whisper crudo:
copiarlo sobre `<id>.json` y volver a correr `correct_transcripts_vocab.py`.

## Un proyecto NUEVO no atravesaba el orquestador: dependía de tablas que dejaban las corridas a mano (Asistente 2026-09-28)

`run_pipeline.py` se probó contra proyectos que ya traían tablas de corridas manuales
anteriores. Con el primer proyecto nuevo se atoró en dos sitios más, además del filtro
de `transcribir-audios`:

1. **`curar-tramos` abortaba con `no such table: clip_descriptions`.** Lee cinco tablas
   que ningún paso creaba: `clip_analysis` (`analyze_clips.py`), `clip_descriptions`
   (`derive_video_categories.py`), el valor de plano, el ángulo y los tramos de
   contenido. El playbook las listaba como "dependencias de curate_segments", pero el
   orquestador no las corría. Ahora son los pasos `analizar-clips`, `categorias`,
   `valor-de-plano`, `angulo` y `tramos-de-contenido`, con una prueba de orden en
   `tests/test_orquestador.py`. `analizar-clips` decodifica todo el material: 31 min
   para 94 min de 4K HEVC 4:2:2 con cuatro workers.
2. **`sync-v2` abortaba con `no such table: face_detections`.**
   `voice_match.expected_voice_embedding_for_video` consultaba las caras sin guarda, y
   `detectar-caras` corre DESPUÉS del sync. El contrato de la función ya decía "sin caras,
   None": ahora el paso 1 lo cumple igual que el 2, y hay prueba en
   `tests/test_sync_convencion.py`.

**La lección de método**: una suite verde contra proyectos viejos no prueba que un
proyecto nuevo pase. Las tablas de las corridas manuales tapaban las dependencias que
faltaban. El próximo arreglo de fondo es una prueba del orquestador sobre un manifest
recién indexado, sin ninguna tabla derivada.

## Con TX que graban por su cuenta, el hermano de un WAV no es el del mismo número (Asistente 2026-09-28)

`sync_siblings_rule.py` buscaba el lavalier hermano por **basename**: mismo nombre en la
carpeta del otro canal. Eso vale con UN receptor que graba los dos TX y numera igual los dos
canales (MAB, Zezzions). Con TX que graban cada uno en su memoria (Morsa, Asistente), cada
uno numera por su lado. En Asistente, el "hermano" de `drc/00034` (15:34) salía `izq/00034`
(19:17). La salvaguardia de 4-gramas lo rechazó (overlap 0.00) y no hubo daño, pero el
hermano real, `izq/00031`, que grabó a la misma hora, se quedaba sin sync: 16 videos del
intro con un solo lavalier.

**Arreglo**: `sibling_audio_id()` consulta primero `lavalier_pairs`, que
`build_lavalier_pairs.py` empareja por hora y por contenido, y solo si no hay par aplicable
cae al basename. Prueba en `tests/test_sync_convencion.py` (`TestHermanoMedido`).

De paso, `voice_sim_dominant()` tronaba sin la tabla `audio_speakers`; ahora la ausencia
cuenta como "sin dato", igual que un audio sin embedding.

## La tolerancia de cadena unió dos WAV que no eran continuos (Asistente 2026-09-28)

`build_chains` junta en una "cadena" los WAV cuyo fin e inicio distan menos de
`--chain-tolerance` (5 s por defecto) y los trata como muestra a muestra. `drc/00035`
(8:14) y `drc/00036` (44:28) quedaron juntos, pero entre ambos hay ~1.7 s por mtime: el TX
se detuvo y se reanudó a mano a las 17:35. Con la cadena unida, `verify_lav_offsets` marcó 7
"DUAL VIOLA D" de ±0.68 s, con signo opuesto antes y después del corte. La tentación era
correr `enforce_dual_lav_consistency.py`, que habría movido pares **bien medidos por onda**
para cumplir una D calculada sobre un supuesto falso.

**Cómo se ve**: violaciones de D del mismo tamaño y signo opuesto a los dos lados de la
frontera entre dos archivos. **Qué hacer**: repetir la verificación con
`--chain-tolerance 1.0`. Si desaparecen, el problema era la cadena, no los pares. Los cortes
automáticos de un Rode son continuos a nivel de muestra; un stop/start a mano no.

**Pendiente de fondo**: medir la continuidad en vez de suponerla. Por ejemplo, los últimos y
primeros segundos de dos archivos seguidos, o la duración esperada del corte automático.

## La ventana larga confirmaba pares sin dejar constancia: un consejo en bucle (Asistente 2026-09-28)

`refine_pairs_longwin.py` re-mide cada par. Si la corrección sale de menos de 20 ms, el par
ya estaba bien y no hace nada, **ni siquiera anotarlo**. `verify_lav_offsets.py` cuenta como
validado un par cuya nota diga `longwin`, así que seguía marcando "ENTREVISTA SIN VALIDAR —
correr refine_pairs_longwin", y correrlo otra vez tampoco dejaba rastro. En Asistente
confirmó 24 de 26 pares y no registró ninguno.

**Arreglo**: una confirmación sin cambio se anota como `[longwin ok Δ...ms prom=...]` y no se
vuelve a medir. Prueba en `tests/test_sync_convencion.py` (`TestVentanaLargaDejaConstancia`).
**Regla general**: una medición que confirma también es un resultado, y se registra igual que
una que corrige.

## En un proyecto nuevo no existía `question_short`: el garble llegaba a los markers sin aviso (Asistente 2026-09-28)

La pregunta curada de los markers Purple vive en `question_segments.question_short` desde
FCC (2026-07-11), pero la columna se agregó a mano en aquel proyecto y **nunca entró al
`CREATE TABLE` de `derive_question_segments.py`**. En un manifest nuevo no existía. Pasaban
tres cosas a la vez:

- `export_lua_data.py` caía en silencio al texto crudo de Whisper;
- su candado ("⚠⚠ N preguntas SIN CURAR") se tragaba el `OperationalError`;
- `surface_transcript_suspects.py` decía "Todas tienen question_short ✓".

Justo lo que prohíbe la regla dura "ningún transcript llega crudo a un artefacto visible". Además,
el script hace `DELETE FROM question_segments` en cada corrida, así que una curaduría hecha antes
de re-correrlo se perdía.

**Arreglos**:

- `derive_question_segments.py` crea la columna, migra las tablas viejas
  (`asegurar_question_short`) y restaura las curadas por clip y segundo de inicio.
- `mirror_questions_to_audio.py` copia `question_short` al espejo. Era la lección 48, que hasta
  hoy se resolvía con un UPDATE a mano.

Pruebas en `tests/test_interview_beats.py`.

**Lo agregado y lo descartado, resuelto al día siguiente (2026-09-29)**: al sumar el segundo
día de rodaje, re-correr `preguntas` se iba a llevar las 4 preguntas agregadas a mano y a
resucitar las 6 descartadas. Ahora la tabla `question_clips_curados` lista los clips cuya
Capa 2c ya se cerró, y `derive_question_segments.py` no los borra ni los vuelve a detectar.
Para reabrir un clip, se saca de la tabla. El registro legible sigue en
`reports/curaduria-preguntas-<fecha>.json`. Prueba:
`test_un_clip_curado_no_se_borra_ni_se_redetecta`.

## El merge por AutoSyncAudio no liga todo: el aplicador en modo merge perdía esas patas (Asistente 2026-09-29)

Medido con `verify_merge.py`: 35 audios ligados con un error máximo de 17 ms, pero Resolve
**no ligó nada** en 5 clips de 6 a 14 s, y en la mayoría de los clips con dos lavalieres **ligó
uno solo**. En `MODO_MERGE` el aplicador daba por hecho que el lavalier venía dentro del clip y no
colocaba ninguno aparte: esas 22 patas no llegaban a A-ROLL ni a B-ROLL.

**Arreglo** (en el `asistente_asistente.lua` generado): `placeSyncAudio` acepta un filtro, y en
modo merge coloca aparte solo los pares cuya ruta NO está en `GetAudioMapping` del clip
(`rutasLigadas`). Colocó los 22 sin ningún fallo. **Pendiente**: llevar lo mismo a
`asistente_lib.lua` o a la plantilla, para que lo hereden los proyectos siguientes. El
generador usa como referencia el script más reciente, que hoy ya es este.

## Detalles menores de la misma noche (Asistente 2026-09-29)

- `exportar_a_memoria.py` tronaba con `carpeta_material_fuente: null` en `_registro.json`,
  porque `.get(k, "")` devuelve `None` cuando la clave existe con null. Ahora usa `or ""`.
- El acta, en B5, cuenta como "logs sueltos" los `._*` que macOS escribe en discos exFAT
  (AppleDouble). Pendiente: filtrarlos en `verify_asistente.py`.
- El acta, en I3, escanea TODOS los `resolve/*.lua` del motor. Las 11 llamadas con marker de
  duración son de `asistente_jilotepec.lua` (ESCALANDO MEXICO), anterior a la regla. Pendiente:
  que I3 mire solo el script del proyecto que se está verificando.

## Sumar un segundo día re-corre el pipeline sobre trabajo curado, y tres pasos lo destruían (Asistente 2026-09-29)

El 29-sep se grabó más material para el mismo proyecto. Re-correr el orquestador sobre un
manifest que ya tenía sync medido a mano y curaduría cerrada destapó tres pasos que "rehacían
su tabla" borrando lo que no era suyo:

- **`sync_audio.py`** (sync por timecode BWF) borraba TODOS los pares de los videos de cada
  sector antes de insertar los suyos. Los Rode no traen timecode: no insertó ninguno y se llevó
  los **60 pares** del día anterior, medidos por transcript, reloj y onda y confirmados por
  ventana larga. El orquestador lo reportó como `sin-trabajo (delta=-60)` y siguió. Se
  restauraron del respaldo del manifest tomado antes de correr. **Arreglo**: borra solo los de
  método `timecode`, que son los únicos que produce, y no duplica un par que otro método ya
  midió (`guardar_pares`, `TestTimecodeNoBorraLoMedido`).
- **`derive_question_segments.py`**: ver arriba, "En un proyecto nuevo no existía
  `question_short`".
- **`refine_all_pairs.py`** volvía a medir todos los pares, incluidos los ya refinados y
  confirmados. Ahora un par medido deja `iter2 ...` en las notas aunque no se aplique nada (ok,
  débil o descartado) y no se vuelve a medir; `--rehacer` lo pide a propósito
  (`TestRefinadorNoRemideLoMedido`). Es la misma regla que la ventana larga del 28-sep: una
  medición que confirma también se registra.

`sync_transcript.py` ya estaba bien: su borrado se limita a `--location` y `--audio-like`, y
nunca toca `manual*` ni `*-locked`. Para el segundo día se corre acotado al material nuevo
(`--location "VIDEO 04/" --audio-like 'audio/000%'`).

**Regla general**: cada paso borra solo lo que él mismo produce. **Regla de operación**: antes
de re-correr el pipeline sobre un proyecto con trabajo a mano, copiar el manifest a
`backups/` y, al terminar, comparar tabla por tabla. El delta negativo de `sync-audio` estaba
en el log; lo que faltaba era mirarlo antes de seguir.

## Una carpeta renombrada solo en mayúsculas duplicaba el material (Asistente 2026-09-29)

Entre un día y otro `Video 02` pasó a llamarse `VIDEO 02`. El disco (exFAT, igual que APFS por
defecto) no distingue mayúsculas: es el mismo archivo. El indexador compara rutas como texto y
metió los 30 clips **otra vez**, como nuevos, sin transcript, sin sync y sin curaduría, rumbo a
un segundo Whisper.

**Arreglo** (`index_project.py`, `seleccionar_para_indexar`): una ruta que el manifest ya tiene
con otras mayúsculas es el mismo archivo si esa otra grafía ya no está en el disco y coinciden
tamaño y mtime. **Se conserva la grafía del manifest**, porque es la que Resolve importó: el
aplicador busca por ruta exacta, y cambiarla lo dejaría sin encontrar los clips que ya están en
el Media Pool. Tres pruebas en `tests/test_proyecto.py`.

**Pendiente**: que los `lookups` por ruta del Lua no distingan mayúsculas. Un clip importado a
Resolve DESPUÉS del cambio de grafía llegaría como `VIDEO 02/...` y no casaría con el
horneado. `lavas_al_corte.lua` ya compara así.

## Los renders del editor entraban como material (Asistente 2026-09-29)

`Exports/V.1.0.mov` (el corte que exportó Victor) entró al índice como un clip de video más,
rumbo a Whisper, al análisis y al B-roll. `Exports` va ahora en `SKIP_DIRS`, y
`should_skip_dir` compara sin mayúsculas por la misma razón de arriba.

## Dos días en una timeline por hora real dejaban la noche en medio (Asistente 2026-09-29)

B-ROLL y AUDIOS EXTERNOS ponen cada cosa en su hora real (`construirPorTiempoReal`). Con el
material de dos días, `t0` es el primer instante del primero y el segundo día queda 17 horas
más adelante. **Arreglo**: el horneado trae el `dia` de rodaje de cada clip y cada WAV, su
fecha local. Con `DIA = "2026-09-29"` antes del `dofile`, el aplicador arma solo ese día, las
timelines llevan el día en el nombre (`ASISTENTE — B-ROLL 29-sep`) y **solo borra y rehace las
de ese día**. Sin `DIA`, todo como siempre. Es la misma regla que "varias asistencias en un
proyecto": nunca borrar lo que otra corrida construyó.

## De qué TX es un WAV suelto, y quién lo llevaba (Asistente 2026-09-29)

Los dos WAV del segundo día quedaron sueltos en `AUDIO/`, fuera de las carpetas por TX. La
carpeta ya no decía de qué TX eran, y uno se llama `00035` igual que un WAV de la otra
cadena del día anterior. **De qué TX**: por el contador de cada uno. Izq iba en 00034 y drc en
00037, así que `00035` es de izq y `00038` de drc. Se declara en `lavalier_tx` de
`project_config.json` (por prefijo de `rel_path`, gana el más largo) y viaja al horneado como
`tx`.

**Quién lo llevaba**: al arrancar se anunciaron, "este es el micrófono de Berni / de Víctor".
Los dos TX oyen las dos frases, así que el transcript no lo resuelve. **Lo resuelve el nivel**:
en 00035 la frase de Berni suena a −38 dB y la de Víctor a −53; en 00038, −55 y −37. **Regla**:
el anuncio al arrancar es la mejor evidencia de quién lleva qué TX, y se lee con el nivel, no
con el texto. Vale para ESE día: el reparto de TX puede cambiar de un día a otro.

## Studio 21.1 cambió dos cosas de AppendToTimeline, y el motor las suponía (Asistente 2026-10-01)

Las dos se midieron leyendo de vuelta lo colocado, no con lo que devolvió la llamada:

- **`endFrame` es EXCLUSIVO** en Studio 21.1.0.17. `LIB.colocarAudioPartido` lo tenía por
  inclusivo (medido en Free 21.0.2, 2026-08-13). Cada pedazo de WAV salía un frame corto, y
  cada corte de la B-ROLL cronológica dejaba un hueco de un frame en la timeline y un frame
  de audio fuera. La garantía "los pedazos suman el WAV entero" no lo vio porque sumaba lo
  PEDIDO. `lavas_al_corte.lua` lo destapó: sus 53 tramos salieron un frame cortos y su
  comprobación por tramo los marcó todos.
- **Un append sobre un hueco ocupado PISA en vez de fallar.** En Free devolvía nil, y la
  cronología subía el clip de pista al recibir el nil. En Studio 21.1 el clip de abajo
  desaparece sin aviso: 9806 y 9809 faltaban en la B-ROLL del 29.

**Arreglos**:

- El `endFrame` se calibra con el primer pedazo de cada corrida (`LIB.EF_EXTRA`, y `anexar`
  en `lavas_al_corte.lua`): se coloca, se mide y, si sobra o falta, se rehace.
- La pista libre se elige con lo que ya se colocó (`ocupado`), sin esperar a que Resolve
  rechace.
- Lo que se suma es lo COLOCADO.

Pruebas con los dos comportamientos en `prueba_cronologia.lua` y
`test_lavas_al_corte.py`. **Regla**: lo que se midió en una versión de Resolve se vuelve a
medir en la siguiente, y el código mide en vez de suponer.

## La cronología mezclaba dos relojes, y empezaba antes de la timeline (Asistente 2026-10-01)

Dos fallas en las timelines por hora real. Las dos estaban también en las del 28-sep que se
armaron en la copia el día anterior, y nadie miró dónde caía cada clip:

- **UTC leído como hora local.** `isoEpoch` pasaba el `created` del horneado
  (`2026-09-29T21:30:43Z`) por `os.time()`, que lee la tabla como hora LOCAL. Los clips
  colocados por el reloj de su cámara quedaban 6 h después (UTC-6) que los colocados por su
  par de sync, que van en epoch verdadero. Los de Adrián caían en la noche y el 9814 no
  entraba a AUDIOS EXTERNOS: con un `rec` negativo, se descartaba. **Arreglo**:
  `LIB.isoEpoch` respeta `Z` y `±hh:mm`. Se probó en tres zonas horarias, pero ninguna con
  horario de verano, y ahí fallaba: ver "La revisión del PR #1" más abajo.
- **El frame 0.** `recordFrame` es absoluto y la timeline arranca en 01:00:00:00 = 86400.
  La cronología contaba desde 0, así que la primera hora del día quedaba antes del arranque.
  Era la trampa que la lección de The Shelter (2026-08-04) dejó anotada "para el día en que
  se coloque algo por tiempo calculado". El mock no la veía porque su `GetStartFrame()`
  devolvía 0. **Arreglo**: `LIB.inicioDe(tl)` se suma a todo, y hay una prueba con un mock
  que arranca en 86400.

**Regla de lectura de vuelta**: en una timeline por hora real, comprobar dónde caen un clip
con sync, uno sin sync y el WAV. Contar items no basta.

## Cámara lenta: el archivo dura cinco veces la toma (Asistente 2026-10-01)

Los siete clips de `VIDEO 03` (VICG_9806-9812) son S&Q. En el `M01.XML` traen
`RecordingMode type="slowAndQuickMotion"`, `captureFps="119.88p"` y `formatFps="23.98p"`. El
9806 dura 153 s en el archivo y la toma duró unos 31. Colocados en su hora, se enciman con
el siguiente; ahora suben de pista y no se pierde ninguno. **Pendiente**: leer `captureFps`
del XML al indexar, para conocer la duración real de la toma. Hoy el motor usa la duración
del archivo para todo lo que es reloj.

## El master por fuentes no dice quién habla cuando todos los micrófonos oyen a todos (Asistente 2026-10-01)

`onset_por_fuente` busca el arranque de la respuesta como el primer cambio de fuente en el
master transcript. Con dos lavalieres en la misma mesa, cada uno oye a los dos y Whisper
transcribe también lo que entra por sangrado. En el master, cada fuente trae las palabras de
todos, así que el "cambio" aparece al segundo de empezar la pregunta: el marker azul caía a
media pregunta, con confianza 0.9. En el 28-sep pasaba igual, sin que se notara.

**Arreglo inmediato**: `question_segments.answer_sec`, el arranque de la respuesta fijado por
el curador, manda sobre lo calculado (método `curado`) y viaja a los espejos.

**Cómo se mide**: el nivel de cada TX en ventanas de 0.25 s. Quien habla suena unos 15-20 dB
más fuerte en su propio TX. En el 9814, Berni pregunta hasta 55.0 s, hay silencio, y Victor
responde desde 56.0. **Pendiente**: que el motor lo haga solo con esa misma medida, en vez de
contar fuentes.

## sync-v2 duplicaba pares ya medidos (Asistente 2026-10-01)

`sync_pipeline_full.py --skip-existing` solo salta videos con pares `manual`/`locked`. Al
re-correr, volvió a escribir 10 pares del 28-sep que ya estaban refinados y confirmados, con
el offset crudo: 98 ms de diferencia en VICG_9470, y dos filas para el mismo par. **Arreglo**:
`ya_medido` no deja escribir un par que otro método ya midió (`TestSyncV2NoDuplicaLoMedido`).

## Los globals de la Consola viven entre corridas (Asistente 2026-10-01)

En el estado Lua de Resolve (la Consola, o `fusion:Execute`) un global sobrevive a la
corrida que lo puso. Un `LAVAS_SOLO_PLAN = true` de una prueba convirtió en plan la corrida
siguiente. Peor sería un `MERGE_FORZAR = true` olvidado. **Arreglos**: el envoltorio
(`bin/aplicar_en_resolve.py`) borra al terminar los globals que asignó, y
`lavas_al_corte.lua` consume los suyos y los borra al arrancar. **Regla para la Consola**:
un global de una sola vez se pone en la misma línea que el `dofile`.

## Si el disco se desmonta a media corrida (Asistente 2026-09-30)

El T9 se desconectó unos segundos entre dos pasos: `Root no resolvible`. Antes de seguir,
`PRAGMA integrity_check` sobre el manifest y un conteo de filas por tabla contra el último
respaldo. Esa vez salió `ok` y no se perdió nada.

## Los dos TX quedaban corridos por sus relojes en las timelines por hora real (Asistente 2026-10-01)

Cada WAV se colocaba por su reloj (`mtime − duración`), y los relojes de dos TX no coinciden:
3.0 s el 29-sep y 2.2 s en un bloque del 28. Un clip con par con los dos lavalieres caía bien
contra uno y corrido contra el otro. La alineación de cadenas (v0.5.1) no lo resolvía. Compara
cada cadena con UNA de referencia y pide 5 videos en común, y con cadenas de un solo archivo
casi nunca los hay. Además el horneado unía cadenas con 5 s de tolerancia fija, no con la
declarada (1.0): `drc/00035` y `drc/00036`, con 1.7 s de hueco, quedaban pegados.

**Arreglos** en `export_lua_data.py`:

- `alinear_wavs_por_contenido`: grafo de WAV unidos por videos en común, solo con pares
  medidos por onda (`par_validado_por_onda`). Por componente, el WAV con más videos queda fijo
  y los demás se mueven con la mediana de la contradicción. Una arista que no da una
  constante no se usa.
- La tolerancia de cadena sale de `cadena_tolerancia_s`.
- El primer par de cada video, el que da su hora, es el validado por onda. El 9469 se
  colocaba por su par con drc, derivado por reloj, y quedaba 1 s corrido contra el lavalier
  bueno. Ese par se corrigió con la alineación: −907.516 → −906.517.

Medido de vuelta en la timeline de los audios: 62 pares contra su WAV, 30 ms de error máximo.

## Los beats de un clip sin preguntas sobrevivían a la curaduría (Asistente 2026-10-01)

`derive_interview_beats.py` saltaba el borrado cuando un clip ya no daba beats. Un clip cuyas
preguntas descartó la curaduría (9463, 9467, 9773 y ASA_9764 el 28-sep) conservaba su pregunta
y su respuesta viejas, y salían en los markers. **Arreglo**: `guardar_beats` reemplaza aunque
sean cero, y `quitar_beats_sin_origen` limpia los clips que ya no tienen de qué derivarlos.

## El arranque de la respuesta: por nivel donde los micros separan, por texto donde no (Asistente 2026-10-01)

Se curaron los 17 azules del proyecto (`question_segments.answer_sec`).

- **Por nivel.** En ventanas de 0.25 s, con 2-medias sobre la diferencia de nivel entre dos
  lavalieres, o entre el lava y el A1 de la cámara. Separa a 12-33 dB cuando cada micro tiene
  su voz: 7 preguntas.
- **Por texto.** En 10 preguntas la separación fue de menos de 4 dB: el A1 de la cámara
  recibía el mismo TX por el receptor. Ahí se leyó el transcript y la respuesta arranca en la
  primera palabra de quien contesta.

Dos detalles:

- Para que los beats lo encuentren, `end_sec` tiene que abarcar la RESPUESTA. En las preguntas
  agregadas a mano terminaba 4 s después de empezar, y el arranque caía estimado a media
  pregunta.
- Donde hay una repetición o un eco antes de la respuesta de verdad, el nivel da los dos
  arranques, el primero y el que sigue a una pausa, y el curador elige.

## En el API externo, un item ligado no se borra, y su MediaPoolItem se toma ANTES (Asistente 2026-10-01)

`Timeline:DeleteClips` devuelve False con un item que está ligado a otros: hay que
desligar el grupo (`SetClipsLinked(grupo, False)`), borrar y volver a ligar. Y el
`MediaPoolItem` de un item se pide antes de borrarlo: después, la referencia ya no sirve y
el `AppendToTimeline` siguiente no coloca nada. Le pasó al asistente al corregir un tramo de
"V.1.0 + lavas" y guardó con el tramo faltante. Lo restauró enseguida y lo midió de
vuelta. **Regla**: al editar una timeline del editor, contar los items antes y después de
cada paso, y no guardar si el conteo no cuadra.

## El kit de la Fase 0 ponía '/' en un nombre de timeline, y el mock lo dejaba pasar (2026-10-01)

Primera corrida real del diagnóstico, en Studio 21.1.0.17. Resolve rechaza `/ \ : * ? " < > |`
en el nombre de una timeline y de un bin, sin error: `CreateEmptyTimeline` y `AddSubFolder`
devuelven nil y `SetName` false. El kit perdía tres cosas en silencio:

- **El resumen** `DIAG RESUMEN <clave> -/181` nunca nacía. Sin él, la corrida siguiente se
  numeraba otra vez r1 y chocaba con la primera.
- **Tres bins de prueba** (`OS-16`, `LOAD-05`, `API-33`) no se crearon, por un `:` o un `"` en su
  valor. `canalBin` solo limpiaba la `/`.
- **La E01 de los stubs** no nacía justo con el kit borrado, que es su razón de ser: el mensaje
  es `cannot open /Users/...`.

Las pruebas pasaban porque el mock aceptaba cualquier nombre. Es la meta-lección de The Shelter
otra vez: un mock que no reproduce el entorno real da confianza falsa. Con el mock corregido, la
suite reprodujo las tres fallas antes del arreglo.

**Arreglo** (motor, `53573f2`):
- el resumen pasa a `93 de 181`;
- un solo limpiador (`nombreSeguro`) para todo nombre de timeline y de bin;
- `LANG-11` mide los nueve caracteres en cada build;
- el lector sigue leyendo la forma vieja.

De la misma corrida salió otro falso NO, ahora en el lector (`f44f713`): FCPXML 1.10 se exporta
como paquete, una carpeta con `Info.fcpxml`.

**Regla**: todo nombre de timeline o de bin que lleve datos (una carpeta, un valor, un mensaje de
error) pasa por un limpiador. Cada vez que se descubre una restricción de Resolve, el mock la
aprende primero. Pendiente en `resolve-integracion.md`: `freshTimeline` en `aplicar.lua`.

## La revisión del PR #1: una hora en verano, un config borrado y un buzón sin candado (2026-10-05)

La revisión del PR que lleva el motor de agosto a octubre a `main` encontró tres fallas que
ninguna prueba veía. Las tres son de una clase que se repite:

- **`os.date("!*t")` + `os.time` pierde una hora en verano.** `isoEpoch` sacaba el desfase
  local-UTC así. La tabla de `"!*t"` trae `isdst=false`, y `mktime` la lee como hora
  estándar. Con `TZ=America/Los_Angeles` o `Europe/Madrid`, `2026-07-01T10:00:00Z` daba 3600 s
  de menos. En México no se veía porque ya no hay horario de verano, y "probado en tres zonas"
  eran tres zonas sin él. **Arreglo**: con zona, el epoch es aritmética del calendario
  (`days_from_civil`), sin `os`. La prueba compara contra Python en cinco zonas, en los dos
  hemisferios, con `lua` y con `fuscript`.
- **Fusionar sobre un lector que se traga los errores borra el archivo.** `leer_config`
  devolvía `{}` cuando `project_config.json` tenía una coma de más, y `escribir_config`
  fusionaba sobre ese `{}`. Así, el archivo del editor quedaba solo con la clave nueva. **Arreglo**:
  si el archivo existe y no se lee, no se escribe, y el pipeline lo avisa.
- **El rename atómico evita un archivo a medias, no la actualización perdida.** Dos
  escritores del buzón leían N pedidos, cada uno agregaba el suyo y ganaba el último. Con dos
  procesos escribiendo 15 pedidos cada uno quedaban 17 a 21 de 30. **Arreglo**: `flock` en
  `lib/pedido.py`.

**Regla**: una prueba de zonas horarias incluye una con horario de verano, en verano. Un
escritor que fusiona exige que la lectura haya funcionado. Leer, modificar y escribir un
archivo que comparten varios procesos va con candado.

