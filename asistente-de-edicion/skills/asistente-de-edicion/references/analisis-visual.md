# Análisis visual de tramos (v13)

A partir de la v13 cada tramo curado recibe una descripción generada por
un **Vision LLM local** que ve los frames reales del tramo. Es el primer
componente del pipeline que interpreta CONTENIDO VISUAL del clip (los
demás trabajan sobre transcripts, metadata técnica, o face embeddings sin
descripción semántica).

## Filosofía del stack

Sigue las dos directivas raíz del proyecto (ver `lecciones/preferencias-del-usuario.md`):

1. **Todo gratis y local** — Ollama + qwen2.5vl + YOLO + ffmpeg. Cero
   APIs de paga. Material confidencial NUNCA sale del disco.
2. **Optimización constante** — frames adaptivos (1 si <10s, 3 si <30s,
   5 si más), skip de discard/export/cull/<5s, smoke test antes del batch
   grande, modelo más pequeño primero (3B antes que 7B).

## Stack instalado

| Componente | Versión | Tamaño | Función |
|------------|---------|--------|---------|
| Ollama | 0.24.0 | ~50 MB | Runtime de modelos locales |
| qwen2.5vl:3b | 3.2 GB | — | Vision LLM (Alibaba, ArcFace) |
| ultralytics (YOLOv8) | 8.4+ | yolov8n.pt 6 MB | Detección de objetos |
| InsightFace | 1.0.1 | 280 MB | Face embeddings (legacy, no se usa en v13) |
| MediaPipe | 0.10+ | — | (skipped en v13 por cambio de API) |

Instalación en proyectos nuevos:
```bash
brew install ollama
ollama serve &                    # background
ollama pull qwen2.5vl:3b
pip3 install --user --break-system-packages ultralytics opencv-python-headless
```

### 1. `bin/extract_segment_frames.py`

Para cada `clip_curated_segments` elegible, extrae frames con ffmpeg al
ancho 720 px (suficiente para vision LLM, ahorra 40% vs 960 px).

**Frames adaptivos** según duración del tramo (motor `porcentaje`, el default):
- `dur < 10s`  → 1 frame (50%)
- `dur < 30s`  → 3 frames (25, 50, 75 %)
- `dur ≥ 30s`  → 5 frames (10, 30, 50, 70, 90 %)

**Desde el 2026-08-26 hay dos motores más**, opt-in, en `lib/frames.py`:
`--motor escena` pone los frames donde la imagen CAMBIA y tira los casi
idénticos (9 frames de un plano sostenido colapsan a 1), y `--motor uniforme`
reparte un presupuesto por duración sin detectar nada. Los dos aceptan
`--cues beats,silencios`, que fuerza un frame en los tiempos que el habla
señala y que la selección visual se pierde. El default no cambia: ningún
proyecto existente se comporta distinto sin pedirlo.
→ `metodologia/ver-el-material.md`

**Filtros automáticos**:
- Skip clips con `clip_descriptions.category IN ('discard','export')`
- Skip clips con `clip_analysis.status = 'cull'`
- Skip tramos con `curated_by = 'claude'` (ya escritos a mano)
- Skip si los frames ya existen (`--force` para re-extraer)

Output: `<disco>/.cinema_assistant/seg_frames/<seg_id>/<pct>.jpg`.

### 2. `bin/analyze_segment_objects.py`

Corre YOLOv8 (nano) sobre los frames de cada tramo. Identifica:
person, casco, mochila, cuerda, perro, vehículos, vegetación, gear.

Output: tabla `segment_objects(seg_id, frame_pct, label, conf, bbox)`.

Skip tramos que ya tienen entries.

### 3. `bin/describe_segments_local_llm.py`

Para cada tramo:

1. Carga sus frames (filtrando `._*` xattrs de macOS).
2. Carga objetos YOLO detectados como contexto.
3. Carga snippet del transcript word-level en `[start, end]`.
4. Compone prompt **estricto** que prohíbe inventar nombres de
   personajes — sólo descripción visual.
5. Envía a Ollama (`POST /api/generate` con `images=base64...`).
6. Recibe JSON con las 8 preguntas + observación de plano/ángulo.
7. Update `clip_curated_segments` con campos descriptivos + `full_text`.
   `curated_by = 'llm'`.

**Skip**: tramos con `curated_by='claude'` (preserva curaduría manual).

**Modelo por defecto**: `qwen2.5vl:3b`. Si la calidad es insuficiente,
escalar a `qwen2.5vl:7b` (mejor pero 2x más lento).

### 4. `bin/vista_tramo.py` — resuelto (2026-08-26)

Este hueco estaba abierto desde la v13 como "`bin/visual_qa.py` — pendiente:
preguntas ad-hoc sobre clips o tramos durante la edición". Lo cierra
`bin/vista_tramo.py`, que va más lejos de lo que pedía el pendiente: no sólo
saca el frame del segundo 45 del clip X, sino la imagen, la forma de onda y las
palabras del transcript **en la misma regla de tiempo**, y con dos fuentes de
audio superpuestas cuando hace falta leer un offset a ojo.

```bash
python3 bin/vista_tramo.py --root "$DISK" --clip-id 1234 --desde 40 --hasta 50
python3 bin/vista_tramo.py --root "$DISK" --clip-id 1234 --desde 12 --hasta 24 \
    --audio ambos
```

Es una herramienta de punto de decisión, no de barrido. Doctrina completa en
`metodologia/ver-el-material.md`.

## El prompt — reglas críticas

```
REGLA CRÍTICA: NUNCA uses nombres propios de personas. NO escribas "ESCALADOR_A",
"ESCALADORA_D", "ESCALADORA_B" ni ningún nombre. Si ves alguien, descríbelo visualmente:
"hombre con barba y casco rojo", "mujer con audífonos azules", etc.
```

Razón: el LLM tiende a tomar nombres del contexto del prompt y asumir
identidad. Hasta que haya curaduría humana validada (cluster face → nombre),
las descripciones se mantienen como observaciones visuales sin nombres.

## 8 preguntas que responde el LLM

```json
{
  "what_action":         "Qué hacen las personas en cuadro",
  "characters":          "Descripción visual (sin nombres)",
  "what_stands":         "Qué resalta visualmente",
  "where_at":            "Dónde están",
  "objects":             "Objetos / animales / equipo visibles",
  "dialogue_idea":       "Idea principal del transcript (si hay)",
  "shot_value_observed": "Confirma o corrige plano",
  "angle_observed":      "Confirma o corrige ángulo"
}
```

## Rendimiento esperado (Apple Silicon CPU)

| Modelo | Tiempo/tramo (3 frames) | Tramos/hora | Total JILOTEPEC (1700) |
|--------|-------------------------|-------------|------------------------|
| qwen2.5vl:3b | ~5-10 s     | ~400-700    | 3-5 horas              |
| qwen2.5vl:7b | ~15-25 s    | ~150-250    | 7-12 horas             |

Lanzar en background. Re-bake al final.

## Limitaciones conocidas

- **Frames de silueta / contraluz**: el modelo 3B puede decir "no hay
  personas visibles" cuando hay figuras pequeñas o silueteadas. 7B mejora
  pero no resuelve todo.
- **Falsos positivos de YOLO**: detecta "horse", "cow", "surfboard" en
  contextos extraños. Se pasa al LLM como referencia, pero el LLM ignora
  las que no concuerdan visualmente.
- **Sin identidad de personajes**: el LLM nunca dice nombres. La
  identificación viene de las tablas `face_catalog` + `face_identities` ya
  pobladas, validadas a mano si el editor las acepta.

## Cuándo NO procesar con vision LLM

- Discards y exports (`category` lo indica).
- Tramos que tu (Claude) escribiste a mano (`curated_by='claude'`).
- Tramos < 5 s (poco contenido visual significativo).
- Re-procesar lo ya hecho (skip-if-exists).

Estas reglas están codificadas en los filtros de cada script.
