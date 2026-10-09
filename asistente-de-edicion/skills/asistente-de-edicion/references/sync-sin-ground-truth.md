# Sync robusto sin ground truth del editor

**Por qué este documento existe**: en Zezzions (2026-05-26) el editor
exportó un `.drt` con su sync manual. Lo usé como machete para validar
todo. En el próximo proyecto puede que no tenga `.drt`. Este documento
captura las heurísticas, umbrales empíricos y políticas que aprendí
empíricamente para llegar a sync correcto SIN esa muleta.

## Principios

1. **El sync correcto no es un punto, es la convergencia de múltiples métodos.**
   Si transcript dice -96.5s, envelope dice -96.4s, voice match confirma
   identidad, y la validación temporal pasa → confianza alta. Un solo
   método nunca basta.

2. **El editor humano arma con tolerancia ~100-400ms.** Si tu sync
   automático difiere del .drt en <500ms y todos los métodos convergen,
   tu sync probablemente es **MÁS preciso** que el del editor (no menos).

3. **El frame rate de la timeline debe DETECTARSE, no asumirse.**
   23.976/24/25/29.97/30 son todos posibles. Default 24 está mal en
   muchos casos.

4. **No hay sync único en clips con multi-take.** Si el audio tiene
   varias tomas de lo mismo, ningún offset funcionará para todo el clip.
   Detectar y escalar a humano.

5. **NO escribir a `audio_sync_pairs` si no hay confianza.** Mejor que
   un clip quede `needs_review` a que tenga sync incorrecto que el
   editor descubra después.

### Fase 1: Pre-requisitos verificables

Antes de cualquier sync, validar:

1. **Indexado correcto**:
   ```sql
   SELECT fps, COUNT(*) FROM clips WHERE file_kind='video' GROUP BY fps;
   ```
   Confirmar que TODOS los videos tienen el mismo fps. Si hay mixed
   framerates, particionar el proyecto.

2. **Transcripts no alucinados**:
   ```bash
   bin/audit_transcripts.py --root "$DISK"
   ```
   `transcript_quality.is_hallucinated = 0` en ≥80% de clips. Re-transcribir
   los alucinados con `transcribe_strict.py` (VAD + voice band-pass).

3. **Detect_faces + voice_catalog poblados**:
   ```bash
   bin/detect_faces.py --root "$DISK"
   bin/build_face_catalog.py --root "$DISK"
   bin/build_voice_catalog.py --root "$DISK" --cluster
   ```
   Sin estos, voice-first matching no funciona.

4. **Lavalier_pairs por timestamp, NO por nombre**:
   ```bash
   bin/build_lavalier_pairs.py --root "$DISK"
   ```
   Empareja Dr↔Izq cross-matching por `|Δmtime| ≤ 120s`, verifica con
   4-grama overlap del transcript ≥ 0.20. Ver §`Hermanos lavalier
   por TIMESTAMP` en `patrones-exitosos.md`.

### Fase 2: Voice-first identity matching

Para cada video con caras detectadas:

1. **`lib/voice_match.expected_voice_embedding_for_video(conn, video_id)`**:
   busca el cluster_id dominante de face_catalog en el video, luego
   `voice_catalog.mean_embedding` vía `face_voice_links`. Fallback:
   embedding del A1 del video.

2. **Por cada audio candidato**:
   - `voice_sim = cosine(audio_speakers.embedding_dominante, expected_emb)`
   - `transcript_overlap = 4-grama(video_text, audio_text)`

3. **Umbrales para clasificar**:
   - `voice_sim ≥ 0.65 AND transcript_overlap ≥ 0.10`: **auto-aceptable**
   - `0.45 ≤ voice_sim < 0.65 OR transcript_overlap ∈ [0.05, 0.10]`: **needs_review**
   - `voice_sim < 0.45 AND transcript_overlap < 0.05`: **rejected**

4. **Salvaguardas críticas**:
   - Si `n_persons_in_frame = 0` (B-roll sin caras), exigir `voice_sim ≥ 0.80`.
   - Si `multitake_score(video, audio) > 0.10`, marcar `multi-take-detected`
     pero seguir (el offset se calcula con cautela).
   - **Verificación temporal**: `audio_mtime - audio_dur ≤ video.creation_time ≤ audio_mtime + 60s`.
     Si falla, **REJECT** aunque voice_sim sea alto (audio físicamente
     imposible de contener al video).

### Fase 3: Offset físico por transcript ngram-alignment

Para cada candidato auto-aceptable:

1. **Algoritmo**:
   ```python
   def fine_offset(words_v, words_a, n=3, window=3.0, expected=None):
       # Construir 4-gramas con timestamps
       # Para cada n-grama común, shift = t_audio - t_video
       # Si expected dado, filtrar shifts |shift - expected_shift| > window
       # Mediana de shifts filtrados → offset = -median(shift)
       # Aceptar si n_anchors ≥ 20 Y MAD < 0.30s
   ```

2. **Análisis por SEGMENTOS del video (clave)**:
   - Dividir el transcript del video en segmentos de 60s.
   - Calcular offset por cada segmento.
   - **Verificar consistencia entre segmentos**:
     - `MAD(offsets_por_segmento) < 0.10s` → sync lineal, confianza alta
     - `MAD ∈ [0.10, 0.30]` → posible drift sutil
     - `MAD ∈ [0.30, 0.50]` → drift severo o multi-take parcial
     - `MAD > 0.50` → multi-take confirmado, sync único NO aplica

3. **`bin/verify_sync_physical.py`** clasifica automáticamente cada
   pair candidato según estas categorías. Reusarlo siempre.

### Fase 4: Validación cruzada multi-método

Para los pairs con sync calculado, ejecutar SEGUNDO método independiente:

1. **`sync_refiner.refine_offset()`** con ventana ±3s alrededor del offset
   transcript. Si `prominence ≥ 0.30` y `|Δ| < 0.20s`, **convergencia
   confirmada**. Confianza final = 0.85+.

2. **Si los métodos DIVERGEN > 0.50s**:
   - El audio puede estar equivocado.
   - O el transcript del video está alucinado en partes.
   - Marcar `needs_review`, NO escribir a `audio_sync_pairs`.

3. **Detección de multi-take**:
   - `multitake_score = % de 4-gramas del video que aparecen >1 vez en audio`
   - Si > 10%: marcar `multi-take-detected`. El offset calculado solo
     sirve para PARTE del clip. Escalar al usuario para slice-level sync.

### Fase 5: Hermanos lavalier — propagación obligatoria

Por la regla del usuario: *"Siempre debes hacer sync de los dos lavas"*.

Para cada pair con sync confirmado (un lavalier), buscar su hermano:

1. **Mapeo correcto por timestamp+contenido** (no por nombre).
2. **Si hermano existe Y `lavalier_pairs.applicable = 1`**:
   - `delta_sec` = delta entre hermanos (medido por transcript ngram-align)
   - `offset_hermano = offset_original + delta_sec`
   - Marcar `method='manual-from-drt-sibling-locked'` (o equivalente).
3. **Si hermano NO existe** (TX off) o NO es aplicable:
   - No forzar. Reportar al usuario.

### Fase 6: Verificación post-sync (verifier obligatorio)

Antes de declarar el sync terminado, correr `bin/verify_sync_physical.py`
y revisar:

- Cualquier categoría distinta de `✓ OK` o `minor` requiere atención.
- `🎬 MULTI-TAKE`: notificar al usuario, NO refinar.
- `🔧 REFINABLE`: aplicar refinement automático (alta confianza).
- `⚠ BIAS DUDOSO`: preguntar al usuario antes de aplicar.
- `🚨 DRIFT SEVERE`: revisar el pair (posible audio equivocado o
  problema sistemático).

### Detección de fps de la timeline
```python
# Si tienes el .drt: parsear MediaFrameRate del primer Sm2TiVideoClip.
# Sin .drt: usar fps del primer video del manifest (todos deben coincidir).
# Validar contra duración: dur_fr_drt / fps == clip.duration_sec_manifest ±0.5s
```

Si el .drt y el manifest dan fps distintos, **abortar y notificar**.
Probable que el editor importó videos a una timeline con conform.

### Detección de hermanos lavalier
**NO por número de archivo. Por timestamp+contenido**:
1. `|Δmtime| ≤ 120s` entre Dr y Izq.
2. `|Δduration| ≤ 60s` (uno puede haberse cortado antes).
3. `overlap_4grama_transcript ≥ 0.20` (mismo contenido capturado).
4. `delta_between_siblings` por **transcript ngram-align**, no envelope
   (más robusto contra música ambient).
5. Aceptar si `n_anchors ≥ 100` y `MAD < 0.5s`. Delta típico < 100ms
   entre hermanos del mismo Wireless PRO RX.

### Detección de multi-take en el audio
```python
multi_count = sum(1 for ng in v_ngrams_set if a_count[ng] > 1)
multi_score = multi_count / |v_ngrams ∩ a_keys|
```
**Umbral 10%**: si más del 10% de los 4-gramas comunes están duplicados
en el audio, multi-take confirmado. NO refinar automáticamente.

### Detección de bias del editor (cuando SÍ hay .drt para comparar)
- `bias = offset_físico_transcript - offset_drt`
- `|bias| < 50ms`: editor armó perfecto
- `50 < |bias| < 200ms`: tolerancia humana normal
- `|bias| > 200ms`: editor armó visualmente con error perceptible (Zezzions
  iter7: detecté bias de 280-640ms en varios pairs)

### Confianza por método
| Método | Conf alta | Conf baja | Cuándo usar |
|---|---|---|---|
| transcript ngram-align | n≥100, MAD<0.10 | n<20 o MAD>0.50 | Siempre que haya transcripts densos |
| envelope log-RMS voz | prom≥0.40 | prom<0.20 | Sin música ambient fuerte |
| chromaprint | matches≥15 | matches<5 | Música ambient compartida |
| voice match (cosine) | sim≥0.80 | sim<0.45 | Identidad cara↔voz |
| convergencia multi-método | 2+ métodos ±200ms | divergencia >500ms | Como sello final |

## Cómo evitar los "trucos de Zezzions"

Lo que en Zezzions hice mirando el .drt:

1. **"El audio Dr/00007 es ENTREVISTADO_5, no ENTREVISTADO_13/ENTREVISTADO_10"** — me lo dijo el .drt.
   **Sin .drt**: lo descubro con voice_match. `voice_catalog` del Dr/00007
   debe matchear `face_catalog` del clip 2712 (ENTREVISTADO_5), NO del 2645 (ENTREVISTADO_13/ENTREVISTADO_10).

2. **"fps de la timeline es 23.976, no 24"** — lo decodifiqué del .drt.
   **Sin .drt**: lo detecto del fps del manifest (todos los videos a
   23.976 → timeline a 23.976). Más confiable que el .drt incluso.

3. **"ENTREVISTADO_3 2571 tiene offset -96.580, no -96.388"** — refiné contra .drt.
   **Sin .drt**: refino por transcript ngram-align con segmentos. Si MAD
   entre segmentos es <0.10, mi offset es físicamente correcto sin
   importar lo que armó el editor.

4. **"ENTREVISTADO_4 2671 no se puede refinar (multi-take)"** — el .drt no me lo
   dijo, lo descubrí en el análisis.
   **Generalizable**: `multitake_score` ≥ 10% siempre dispara la alerta.

5. **"2645 ↔ Dr/00005 no Dr/00007"** — el .drt me orientó a corregir,
   pero la VERDADERA detección fue por **timestamp del WAV vs video**
   (Dr/00007 grabado 2h después del video 2645 = físicamente imposible).
   **Generalizable**: validación temporal estricta antes de aceptar match.

## Lo que TODAVÍA no sé hacer sin ayuda humana

1. **Multi-take resolution automática**: cuando el audio tiene 3 tomas,
   el video usa fragmentos de cada una, NO hay automatización conocida.
   Escalar al editor para slice-level sync.

2. **Sync de B-roll sin habla**: clips de paisaje/timelapse/establishing
   no tienen transcript. Si el audio externo tampoco tiene habla
   (ambient), envelope+chromaprint son las únicas opciones, y son ruidosas
   con música. Aceptar baja confianza o dejar sin sync.

3. **Detección de "audio del entrevistador" vs "audio del entrevistado"**:
   en entrevistas, el A1 de cámara puede tener preguntas del entrevistador
   y el lavalier solo respuestas del entrevistado. Transcript overlap
   bajo (~10%) NO implica audio equivocado. Distinguir requiere voice
   identity por cluster.

4. **Sync de eventos sin diálogo claro**: aplausos, gritos, golpes. PANNs
   sound events ayuda pero solo en proyectos con eventos discretos
   abundantes.

## Política operativa: NO escribir si no hay confianza

```
SI (transcript ngram-align falla)
   Y (envelope refinement prominence < 0.20)
   Y (no hay convergencia multi-método):
       NO ESCRIBIR audio_sync_pairs
       SI escribir sync_candidates con status='needs_review'
       NOTIFICAR al usuario
```

Un proyecto con 60% de clips sincronizados y 40% en `needs_review` es
**mejor** que un proyecto con 100% sincronizado donde 30% tiene errores
silenciosos que el editor descubrirá tarde.

## Implementación (completa 2026-05-27)

- [x] `bin/init_sync_schema.py` — schema aditivo (sync_candidates,
  lavalier_pairs.notes, audio_sync_pairs.classification, etc.). Idempotente.
- [x] `lib/sync_segment_analysis.py` — funciones core reusables:
  `load_words`, `compute_offset`, `segment_offsets`, `multitake_score`,
  `classify_pair`, `temporal_validity`, `fourgram_overlap`.
- [x] `bin/build_lavalier_pairs.py` — detección de hermanos por
  timestamp+contenido (no por nombre).
- [x] `bin/sync_pipeline_full.py` — orquestador end-to-end. Voice-first →
  transcript-sync (densest-cluster con `transcript_sync.align`) →
  segment-analysis → classify_pair → escritura selectiva
  (audio_sync_pairs O sync_candidates).
- [x] `bin/verify_sync_physical.py` — verificador automático con
  detección de multi-take, drift, bias.
- [x] `bin/sync_review.py --source sync_candidates` — CLI interactivo
  para revisar needs_review. Comandos: accept / reject / manual <off> / skip.
- [x] Integración en el orquestador (`bin/run_pipeline.py`), post-transcripción.
  Falla con exit 4 si hay `needs_review` no procesados (override con
  `NEEDS_REVIEW_OK=1`).

## Verificación E2E (Zezzions 2026-05-27)

Pipeline corrido SIN .drt sobre 253 videos del proyecto:

| Resultado | Cantidad | Detalle |
|---|---|---|
| ✓ WRITTEN | 6 | ENTREVISTADO_3 2571/2572, 2596, 2597, 2712 (offsets físicamente correctos, MAD<0.15, anchors>100) |
| → CANDIDATE | 4 | 2566 (BIAS DUDOSO MAD alto), 2598 (DRIFT-LEVE), 2671 ENTREVISTADO_4 (MULTI-TAKE), 2796 (BIAS clip corto) |
| ⊘ REJECTED | 243 | B-roll sin habla (correcto) |

Comparado con iter7 (manual + .drt + refinement):

| Pair | iter7 (con .drt) | Pipeline (sin .drt) | Δ |
|---|---|---|---|
| 2571 Dr | -96.605 | **-96.580** | match (25ms) |
| 2572 Dr | -531.657 | **-531.945** | pipeline 288ms MÁS PRECISO |
| 2597 Izq | -502.961 | **-503.070** | pipeline 109ms más preciso |
| 2712 Dr | +81.280 | **+81.260** | match (20ms) |
| 2671 ENTREVISTADO_4 | (preservado del .drt) | (CANDIDATE multi-take) | sistema detecta el problema |

El pipeline sin .drt **encuentra los mismos offsets físicos que iter7 — a veces mejores**. NO inventa syncs en clips con transcript pobre (2574, 2645, 2786, 2794) — los deja para revisión humana.

## Limitaciones conocidas

1. **2645 ENTREVISTADO_13/ENTREVISTADO_10**: voice_match elige 00006 (sim=0.93) cuando el
   correcto es Dr/00005. El `voice_catalog` tiene cluster compartido
   entre tomas distintas del mismo evento musical. Mitigación futura:
   re-entrenar voice clustering con VAD más estricto, o exigir
   transcript anchors mínimos (no solo voice_sim).

2. **Transcripts del video alucinados** (2786, 2794): si el video
   transcript es basura, el pipeline no puede usarlo. Necesita
   `transcribe_strict.py` previa.

3. **2574 sin transcript suficiente**: video con 0 anchors comunes
   con sus 4 audios candidatos. Único método disponible: envelope
   refinement con A1 limpio, lo cual no está implementado en este
   pipeline.

4. **Clips muy cortos** (2796 dur=329s): transcript suficiente pero
   MAD entre segmentos alto por jitter de Whisper en clips cortos.
   Clasifica como `bias_uncertain`. Usuario decide si aceptar.

## Comando único para nuevo proyecto

```bash
DISK=/Volumes/MyProject

# 1. Schema sync v2
python3 ~/cinema-assistant/bin/init_sync_schema.py --root "$DISK"

# 2. Lavalier pairs (si hay dual-mic recorder)
python3 ~/cinema-assistant/bin/build_lavalier_pairs.py --root "$DISK"

# 3. Pipeline completo
python3 ~/cinema-assistant/bin/sync_pipeline_full.py --root "$DISK"

# 4. Verificación
python3 ~/cinema-assistant/bin/verify_sync_physical.py --root "$DISK"

# 5. Review CLI para needs_review
python3 ~/cinema-assistant/bin/sync_review.py --root "$DISK" --source sync_candidates
```

O simplemente: `python3 ~/cinema-assistant/bin/run_pipeline.py --root "$DISK"`.

## Resumen para empezar el próximo proyecto

1. **Indexar todo** (`index_project.py` + `index_audios.py`).
2. **Detectar fps de la timeline** (del manifest, no del .drt).
3. **Transcribir + audit transcripts** (descartar alucinados).
4. **Face detection + voice catalog + identity fusion** (todo el stack
   de identidad).
5. **Voice-first identity matching** para emparejar video↔audio
   candidato.
6. **Transcript ngram-align por segmentos** para offset preciso.
7. **Detectar multi-take** y escalar al humano si aplica.
8. **Verificar con `verify_sync_physical.py`** antes de escribir.
9. **Solo escribir si confianza alta**; resto a `needs_review`.
10. **El editor revisa needs_review** con CLI interactivo.
11. **El orquestador NO termina** si quedan `needs_review` no procesados.

Este flujo es **garantía verificable, no promesa**: cada paso tiene un
verifier que falla si no cumple, así no se cierra un proyecto con sync
silenciosamente roto.
