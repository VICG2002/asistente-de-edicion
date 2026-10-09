# Identificación de personajes (face recognition)

Pipeline automático de detección + identificación de caras usando
**InsightFace** (modelo `buffalo_l`, ArcFace embeddings de 512 dimensiones).
Reemplaza la extracción por whitelist del transcript de la v10, que era
inexacta (ESCALADORA_B aparecía en clips donde solo está ESCALADOR_A por la mención
del transcript).

## Por qué InsightFace (no face_recognition / dlib)

`face_recognition` requiere compilar `dlib` con `cmake`, que falla en
macOS Apple Silicon sin Homebrew. InsightFace:

- Viene con wheels precompilados (`pip install insightface onnxruntime`).
- Usa modelos ONNX descargables (~300 MB la primera vez).
- ArcFace embeddings son de mayor precisión que face_recognition (FaceNet-ish).
- Sin compilación nativa = funciona out-of-the-box.

```
pip3 install --user insightface onnxruntime opencv-python-headless scikit-learn
```

### 1. `bin/detect_faces.py`

Para cada clip de video:

- Sample N frames con `ffmpeg` (4 frames si dur < 60 s, 1 cada 15-30 s si
  más largo).
- Cada frame se downsamplea a 960 px de ancho (acelera detección sin
  perder precisión).
- `FaceAnalysis(name='buffalo_l').get(frame)` devuelve detecciones con
  bbox + embedding 512-D + score.
- Filtros: `det_score ≥ 0.55`, bbox lado ≥ 40 px (descarta caras chicas
  o de baja confianza).
- Guarda crop JPG en `<disco>/.cinema_assistant/faces/<clip_id>/<t>_<i>.jpg`
  con 20 % de padding alrededor del bbox.
- Inserta fila en `face_detections(clip_id, frame_t, bbox, det_score,
  embedding BLOB, crop_path)`.

Rendimiento: CPU ~1 s/frame. Para JILOTEPEC (~210 clips main + GoPros)
≈ 25-40 min total.

### 2. `bin/build_face_catalog.py` (interactivo)

- Carga todos los embeddings.
- **Clustering DBSCAN** con `metric='cosine'`, `eps=0.55`, `min_samples=3`.
- Cada cluster representa una identidad. Para cada cluster, genera un
  mosaico de los crops (`/tmp/jilo_faces/cluster_NNN.jpg`) ordenado por
  número de detecciones.
- El usuario revisa los mosaicos y dice "cluster 0 = ESCALADOR_A, cluster 1 =
  ESCALADORA_B, …".
- Re-correr con `--assign 0=ESCALADOR_A 1=ESCALADORA_B 2=ESCALADORA_D`: persiste el catálogo en
  `face_catalog(identity_id, canonical_name, centroid BLOB)` y crea
  asignaciones por detection en `face_identities(detection_id,
  identity_id, confidence)`.

### 3. `bin/identify_faces.py`

- Para cada `face_detection`, computa distancia coseno contra cada
  centroide del catálogo.
- Si la distancia más baja es ≤ `0.40`, asigna esa identidad.
- Actualiza `clip_characters.characters` = `"ESCALADOR_A (12), ESCALADORA_B (4)"`
  agrupado por clip — el conteo es número de detecciones, no de personas.

## Limitaciones conocidas

- **Caras pequeñas / lejos**: en planos generales (PG, GPG) y POV de
  escalada, las caras suelen tener < 40 px → no se detectan.
- **Cascos y arneses**: la mitad del material es escalada con casco; la
  detección de caras laterales o desde arriba falla.
- **Misma persona en distintos clusters**: si las condiciones de luz son
  muy distintas, una misma persona puede caer en 2 clusters. Solución:
  re-correr `build_face_catalog.py --assign N=ESCALADOR_A M=ESCALADOR_A` apuntando
  los dos clusters al mismo nombre canónico (el último gana al INSERT
  OR REPLACE; mejor agregar lógica de merge).

## Mantener el catálogo entre proyectos

El catálogo (`face_catalog`) es por-disco / por-proyecto. Cuando
arranque un proyecto nuevo:

1. Correr `detect_faces.py` sobre el nuevo material.
2. `build_face_catalog.py` genera clusters nuevos.
3. Si una persona ya existía en el proyecto anterior, **importar su
   centroid** (copiar el BLOB de `face_catalog` y persistirlo en el
   nuevo manifest).

Esto evita re-identificar a ESCALADOR_A cada vez. **TODO**: script
`bin/import_face_identity.py` que toma `--from <disco_origen>
--name ESCALADOR_A` y copia el row.

## Validación cruzada audio + visión (v15)

`bin/attribute_faces_via_transcript.py` añade una **3ª señal** que
usa el AUDIO como ground truth para validar / corregir el clustering
visual de InsightFace. Combina tres fuentes:

1. **Respuesta-en-entrevista**: detecta el entrevistado de cada clip
   leyendo el transcript (busca "¿cuál es tu nombre?" o el nombre del
   cast más mencionado). Las caras detectadas durante las respuestas
   (entre `Qn+5s` y `Qn+1`) se atribuyen a ese entrevistado.
2. **Mención + cara ±5s**: si el transcript menciona un nombre del
   cast y hay una cara detectada en ±5s, vota por esa identidad.
3. **Cluster auto-label**: para cada cluster InsightFace, si ≥ 55 %
   de las caras votan por un nombre y hay ≥ 3 detecciones, se
   confirma esa asignación.

**Comportamiento defensivo**:
- Si una asignación ya existe en `face_catalog`, el script REPORTA el
  conflicto pero NO sobrescribe. El usuario decide.
- Si no se puede determinar el entrevistado de un clip (sin transcript
  o sin nombres del cast mencionados), el script salta señal 1 para
  evitar atribuciones falsas.

**Caso ejemplo JILOTEPEC**:
- Mi asignación visual inicial: cluster 3 = "ESCALADOR_G".
- El audio reveló: cluster 3 recibe 100 % de votos para "ESCALADOR_C"
  (4 detecciones, todas en clips donde ESCALADOR_A menciona a ESCALADOR_C).
- Re-asignamos cluster 3 = ESCALADOR_C. Confirmado.

## El LLM usa identidades confirmadas (v15)

`describe_segments_local_llm.py` ahora pasa al prompt un campo
`{confirmed_identities}` con los nombres validados que aparecen en el
tramo (de `face_attributions` + `face_identities`). El prompt cambió
de "NUNCA uses nombres" a "**SOLO** usa nombres si están en
IDENTIDADES CONFIRMADAS".

Resultado: en clips donde face_recognition + audio coinciden en
identidad, el LLM puede usar el nombre. En el resto, sigue
describiendo visualmente.

Ejemplo (cid 1761, MVI_6027 post-encadene):
- Identidades confirmadas: ESCALADOR_A, ESCALADORA_B.
- Descripción del LLM: "ESCALADOR_A, hombre con barba, 30-40 años, ropa
  casual (camiseta amarilla, chaqueta gris). | PA + Normal | ESCALADOR_A
  se sienta en una roca, mirando hacia la cámara…"
