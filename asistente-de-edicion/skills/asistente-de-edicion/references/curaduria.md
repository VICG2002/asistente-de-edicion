# Curaduría de tramos (clip_curated_segments)

A partir de la v11, cada clip recibe **N tramos curados** con
descripción real. Los tramos vienen de dos fuentes:

1. **`bin/curate_segments.py`** genera candidatos automáticos con
   filtros técnicos (motion, blur, duración).
2. **Sesión interactiva** donde yo (Claude) reviso las hojas de
   contacto y escribo descripciones que responden las **8 preguntas
   estándar** por tramo.

Los rows quedan en la tabla `clip_curated_segments` con un campo
`curated_by` que distingue `auto` (candidato sin descripción) vs
`claude` (revisado y con full_text).

## Las 8 preguntas obligatorias por tramo

| Pregunta                                  | Campo               |
|-------------------------------------------|---------------------|
| ¿Qué están haciendo los personajes?       | `what_action`       |
| ¿Quién o quiénes lo está haciendo?        | `characters`        |
| ¿Qué es lo principal que resalta?         | `what_stands`       |
| ¿En dónde están?                          | `where_at`          |
| ¿Qué objetos / animales hay?              | `objects`           |
| ¿Desde dónde y de qué tamaño? (plano + ángulo) | `shot_value` + `angle` |
| ¿Hay diálogo?                             | (transcript link)   |
| ¿Cuál es la idea principal de lo dicho?   | `dialogue_idea`     |

El campo `full_text` es la composición legible para el marker Green
en la timeline:

```
[Personajes] | [Plano + Ángulo] | [Acción + lugar + objetos + idea diálogo]
```

Ejemplo:

```
ESCALADOR_A, ESCALADORA_B | Plano Medio + Normal | ESCALADOR_A explica el origen de la
caravana en la base del crag; cuerdas y equipo dispersos. Idea:
"queríamos romper nuestros límites como escaladores".
```

## Criterios automáticos (`curate_segments.py`)

| Caso                                                  | Acción                                                              |
|-------------------------------------------------------|---------------------------------------------------------------------|
| Tramo con `avg_activity > 50`                         | **Descartar** (shake extremo, reacomodo de cámara)                  |
| Tramo `< 3 s`                                         | Descartar                                                           |
| Clip largo (> 120 s) NO entrevista/charla             | Tomar muestra central de **10 s**; `total_dur_note = "clip total Xs, muestra de 10s"` |
| Timelapse                                             | 1 muestra central de 10 s + descripción placeholder                 |
| Entrevista / charla-equipo / accion-dialogo           | Usar todos los `content_segments` (habla densa) como base           |
| Otros clips cortos                                    | Tramos del `clip_segments` filtrados (sin motion extremo)           |

## Cuándo es "uno solo" vs "varios"

- **Un tramo**: el contenido es uniforme (timelapse, b-roll de cueva,
  muestra representativa de clip largo).
- **2-4 tramos**: el clip cambia (pegue completo = lectura + intento +
  caída + post-pegue; entrevista = pregunta 1 + 2 + 3).

La heurística automática se queda corta en clips de escalada largos
con varias secciones. La revisión visual mía (sesión interactiva) es
necesaria para refinar el número y los límites.

## Filtros por blur (futuro)

`clip_analysis.blur_mean` existe pero `curate_segments.py` aún no lo
usa. Próximo refinamiento: descartar tramos con `blur > X` salvo que
sea un zoom intencional o un desenfoque corto. Pendiente decidir el
umbral con un par de tomas problemáticas como referencia.

## Reglas de no-curaduría

- **No descartar a ciegas por motion**. Un pegue de escalada legítimo
  tiene motion 20-40; eso es contenido bueno, no shake. Solo descartar
  > 50 (donde la cámara se reacomoda).
- **No fragmentar entrevistas en exceso**. Cada pregunta genera su
  marker Purple por separado. Los tramos Green en entrevistas se
  alinean con las respuestas largas, no con cada cambio de tema.
- **El editor decide al final**. Los tramos curados son sugerencias,
  no obligaciones. El editor puede borrar un marker Green en Resolve
  sin afectar la metadata del clip.
