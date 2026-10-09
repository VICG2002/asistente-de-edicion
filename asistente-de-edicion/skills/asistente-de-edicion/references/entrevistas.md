# Detección de entrevistas

Tres señales en orden de fortaleza creciente. Combinar todas en la
práctica.

## 1. Por carpeta (rápida, débil)

`isInterview(rel_path)`:
```python
low = rel_path.lower()
return "entrevista" in low or "visita nata" in low
```

Funciona cuando hay buena organización (algunos shoots etiquetan así).
**Pierde** entrevistas en carpetas no nombradas.

## 2. Heurística cámara + duración + movimiento (intermedia)

Una entrevista típica:
- Cámara **principal** (no acción): Canon DSLR o Blackmagic.
- Toma **larga**: ≥ 60 s.
- Cámara **estática**: `motion_score_mean < 8` (de `clip_analysis`).

```python
def is_interview_heuristic(camera, dur, motion):
    return camera == "main" and dur and dur >= 60 and motion is not None and motion < 8
```

**Trampa:** una toma fija filmando al climber arriba del paredón
**también** cumple (cámara quieta, > 60 s, "principal"). Falso positivo
de "interview" para una toma estática de acción.

En ESCALANDO MEXICO JILOTEPEC esta heurística marcó 135 clips; los
reales con diálogo eran solo ~23.

## 3. Por densidad de transcript (la más confiable)

Si el clip tiene un transcript con ≥ 60 palabras distintas y baja
repetición → es entrevista (hay habla coherente).

```python
def is_interview_transcript(transcript_words):
    distinct = len({normalize(w) for w, _ in transcript_words} - {""})
    return distinct >= 60
```

**Por qué es la mejor:** verifica contenido. Una toma estática de
escalada produce alucinación de Whisper (`"suscríbete al canal"`
repetido) → < 60 palabras distintas → descartada como entrevista.

## Criterio recomendado (compuesto)

```python
def is_interview(rel_path, camera, dur, motion, transcript_words):
    low = (rel_path or "").lower()
    if "entrevista" in low or "visita nata" in low:
        return True
    if transcript_words:
        distinct = len({normalize(w) for w, _ in transcript_words} - {""})
        if distinct >= 60:
            return True
    # fallback cuando no hay transcript todavía
    if (camera == "main" and dur and dur >= 60
            and motion is not None and motion < 8):
        return True
    return False
```

## Bake en `escalando_data.lua`

`bin/export_lua_data.py` calcula el flag `interview` por clip y lo
guarda en el bake. El Lua filtra para construir `ESC — ENTREVISTAS`:

```lua
local entrevistas = {}
for _, r in ipairs(recs) do
  if r.interview then entrevistas[#entrevistas+1] = r end
end
```
