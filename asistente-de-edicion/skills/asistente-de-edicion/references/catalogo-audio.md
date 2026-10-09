# Catálogo del audio externo

El audio externo no es solo entrevistas — incluye **ambientes**, **room
tones**, **efectos** y a veces **música**, todo útil para diseño de
sonido. El catálogo clasifica cada archivo para que sound design pueda
filtrar.

## Indexar todo `AUDIOS/`

```bash
python3 ~/cinema-assistant/bin/index_audios.py "$DISK"
```

Recorre el árbol completo y mete cada archivo en el manifest
(`file_kind='audio'`). Reusa `sync_audio.ensure_audio_indexed`.

## Estructura típica

`AUDIOS/` suele estar organizada por entrevistado o por sesión:
```
AUDIOS/
├── AUDIOS CARLOS ARIZA/
│   ├── 4TO DINAMO/
│   └── VIPS/
├── AUDIOS JUAN GUZMAN HUASTECA/
│   ├── RODE JUAN GUZMAN/
│   ├── YO ESCALO MEXICO JUAN GUZMAN/
│   └── ZOOM00XX/
├── AUDIOS SERAPIO/
│   └── ENTREVISTA ESCALADOR_A LARGA EN CASA/
└── ...
```

Recorders típicos: **Zoom** H6/H8, **RODE** (lavalier/shotgun),
**Sennheiser**, **iPhone**. A veces marcados en el nombre del folder o
del archivo.

## Saltar `_Tr` (pistas duplicadas)

Los Zoom producen un **`_LR`** (mix estéreo) + varios **`_TrN`** (un
mic por pista). Para transcript y catálogo, usar el `_LR` o el archivo
nombrado — los `_Tr` son redundantes (mismo contenido, una sola pista).

Filtro SQL:
```sql
... AND filename NOT GLOB '*_[Tt][Rr][0-9]*'
```

## Transcribir

```bash
python3 ~/cinema-assistant/bin/transcribe_audios.py \
  --root "$DISK" \
  --model ~/cinema-assistant/models/ggml-large-v3-turbo.bin
```

Solo mixes/nombrados. Para archivos sin habla, cachea un transcript
vacío — eso es la señal de "no es diálogo".

## Clasificación

| Categoría     | Cómo se identifica                                                   |
|---------------|----------------------------------------------------------------------|
| `dialogo`     | Transcript con ≥ 60 palabras distintas, poca repetición.             |
| `ambiente`    | Transcript vacío/alucinado; filename `AMBIENTE`, `AMB`, sin diálogo. |
| `room-tone`   | Transcript vacío/alucinado; filename `ROOM TONE`, `RT`.              |
| `efectos`     | Transcript vacío; filename con nombre del efecto (`CHIFLIDO`, etc.). |
| `musica`      | Identificable por contexto/nombre; transcript suele alucinar lyrics. |
| `desconocido` | No coincide con lo anterior — flagear.                               |

Pseudocódigo:
```python
def classify_audio(filename, transcript):
    fn = filename.lower()
    distinct = len({normalize(w) for w, _ in (transcript or {}).get("words", [])} - {""})
    if distinct >= 60:
        return "dialogo"
    if "room tone" in fn or " rt " in f" {fn} ":
        return "room-tone"
    if "ambiente" in fn or "amb" == fn[:3]:
        return "ambiente"
    if any(k in fn for k in ["chiflido","barras","silbido","fx","sfx"]):
        return "efectos"
    if any(k in fn for k in ["musica","music","cancion","song"]):
        return "musica"
    if distinct > 10:
        return "dialogo"   # diálogo escaso pero presente
    return "desconocido"
```

## Tabla del catálogo (esquema sugerido)

A crear en el manifest cuando se implemente:
```sql
CREATE TABLE audio_catalog (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id),
    category TEXT,                -- dialogo | ambiente | room-tone | efectos | musica | desconocido
    description TEXT,              -- nota libre
    recorder TEXT,                 -- Zoom / RODE / Sennheiser / iPhone
    source_folder TEXT,            -- la subcarpeta canon (= AUDIOS/X)
    classified_at REAL
);
```

## Exportar para sound design

Reporte filtrable por categoría:
```sql
SELECT category, COUNT(*), ROUND(SUM(c.duration_sec)/60.0) min_total
FROM audio_catalog ac JOIN clips c ON c.id=ac.clip_id
GROUP BY category;

SELECT ac.category, c.path, c.duration_sec, ac.description
FROM audio_catalog ac JOIN clips c ON c.id=ac.clip_id
WHERE ac.category='ambiente' ORDER BY c.duration_sec DESC;
```
