# Descripciones por clip + crónica del proyecto

Cada clip lleva una **descripción de una oración** que dice qué pasa.
Se ve como marker al principio del clip en la timeline y como
`Notes` en la metadata del clip en el Media Pool. Encadenadas
cronológicamente, forman la **crónica** del proyecto.

## Componentes de una descripción

Cada descripción combina:

1. **Visual** — qué se ve. Lectura de una hoja de contacto (1 cuadro
   por clip).
2. **Habla** — qué se dice. Del transcript del clip (si lo tiene).
3. **Metadata** — cámara, ubicación, fecha/hora, duración.

Para una **entrevista**, lo que se dice domina la descripción:
> "ESCALADOR_A habla de los grados duros de Jilotepec y del Petzl RocTrip."

Para una **toma de acción**, el visual y el contexto cronológico
dominan:
> "GoPro POV de la ruta Lujuria, primer largo."

Para **B-roll/escénica**:
> "Paisaje del crag al atardecer (Blackmagic, gran angular)."

## Hojas de contacto

`bin/build_contact_sheets.py --root <disco> --location <substring>
--out <dir>`:

1. Toma 1 cuadro a la **mitad** de cada clip con ffmpeg (la mitad es
   más representativa que el primer frame, evita fade-ins).
2. Escala a 420×236 (16:9).
3. Tilea 5×5 con **Pillow** (`PIL`) en mosaicos etiquetados — cada
   celda tiene un label con índice, nombre, fecha y duración.
4. Guarda `sheets_index.json` que mapea `(sheet, cell_row, cell_col) →
   clip_id`.

281 clips → ~12 hojas en JILOTEPEC. Lectura visual = ~12 imágenes,
tractable.

## Categorías recomendadas

| Categoría             | Cuándo                                              |
|-----------------------|-----------------------------------------------------|
| `entrevista`          | Cámara principal + ≥ 60 palabras distintas.         |
| `pov-static`          | GoPro estática (cap. de sesión chaptered larga).    |
| `accion`              | GoPro o cámara siguiendo movimiento.                |
| `b-roll`              | Toma corta, sin diálogo, complementaria.            |
| `escenica`            | Paisaje, establishing.                              |
| `preparacion-gear`    | Gear up — calzar pies de gato, atar arnés, etc.     |
| `drone`               | DJI aéreo.                                          |
| `discard`             | False start, < 5 s, claramente inutilizable.        |

## Pipeline propuesto

```
clip → frame (mid-point)
     → hoja de contacto (Pillow, 5×5, etiquetado)
     → lectura visual del modelo → categoría + nota visual
     → join con transcript (si interview) y metadata
     → "descripción" final (1 oración)
     → bake en escalando_data.lua como campo `description`
     → Lua marker (Pink) en frame 1 + Notes del clip
```

## Crónica

Las descripciones en orden cronológico (`ORDER BY creation_time, filename`)
forman la crónica. Producir un Markdown legible:

```markdown
# Crónica — JILOTEPEC (ESCALANDO MEXICO)

## 2017-01-25
- 12:40 — Blackmagic — paisaje del crag al amanecer (5.5 min)
- 12:41 — Blackmagic — primer recorrido al campo base (4.5 min)

## 2017-02-08
- 18:20 — Canon — ESCALADOR_A habla sobre el descubrimiento de las rutas...
- 18:24 — Canon — ESCALADOR_A continúa: sobre cómo se aferraron los pioneros...
```

Exportable a PDF o HTML. Sirve como referencia narrativa del proyecto.

## Storage de descripciones

A formalizar en una tabla del manifest:
```sql
CREATE TABLE clip_descriptions (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id),
    category TEXT,
    description TEXT,           -- una oración para el marker
    visual_note TEXT,           -- lo que vi en la hoja
    speech_summary TEXT,        -- resumen del transcript si lo hay
    described_at REAL
);
```

`bin/export_lua_data.py` lee `clip_descriptions` y bake en el campo
`description` por clip.
