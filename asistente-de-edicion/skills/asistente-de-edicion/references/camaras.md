# Identificación de cámaras

## Roles

| Rol             | Para qué se usa típicamente                    | Audio externo?           |
|-----------------|------------------------------------------------|--------------------------|
| **Principal**   | Entrevistas + A-roll                           | Sí (sync con dual-system)|
| **Acción**      | POV, climbing, deportivo                       | No (embebido alcanza)    |
| **Drone**       | Aéreo, B-roll                                  | No                       |
| **Misc**        | Timelapses, masters, pruebas, edits            | No                       |

## Detección por prefijo + códec

| Cámara                | Prefijo nombre           | Códec       | Rol         |
|-----------------------|--------------------------|-------------|-------------|
| Canon EOS 6D / DSLRs  | `MVI_`                   | h264 1080p  | Principal   |
| Blackmagic Pocket/etc | `BLAC` o nombre largo BMD| ProRes      | Principal   |
| GoPro Hero antiguas   | `GOPR`, `GP01`–`GP09`    | h264        | Acción      |
| GoPro Hero 5+         | `GH01`–`GH99`            | h264        | Acción      |
| GoPro Hero 7+         | `GX01`–`GX99`            | h264 4K     | Acción      |
| DJI drone             | `DJI_`                   | h264        | Drone       |

## Registro de perfiles (v0.2.0 — reemplaza las funciones sueltas)

**Ya no hay ninguna `classify_camera()` en los scripts.** La clasificación es
declarativa y vive en un solo lugar:

| Archivo | Qué es |
|---|---|
| `config/camera_profiles.json` | registro del motor, se distribuye con el plugin |
| `<disco>/.cinema_assistant/camera_profiles.json` | override del proyecto, se **fusiona** encima (mismo `id` reemplaza; `id` nuevo agrega) |
| `lib/cameras.py` | única fuente de verdad que lo consume |

```python
from lib import cameras

perfil = cameras.classify(filename, camera_model, camera_make, ext)
perfil.role          # main | gopro | drone | other
perfil.decodable     # False en BRAW/R3D: ffmpeg no los abre
perfil.scratch_audio # el cuerpo graba audio de referencia sincronizable

# Para los scripts que filtran en SQL — reemplaza los MAIN_CAM hardcodeados:
sql = cameras.main_cam_sql(disk_root=root)
conn.execute(f"SELECT id FROM clips WHERE file_kind='video' AND {sql}")
```

### Por qué existe

La clasificación estaba escrita **cinco veces con contenidos ya divergentes**
(más tres variantes menores). El síntoma real: `sync_waveform.py` sólo conocía
`MVI_`/`BLAC`/Canon 6D, así que **todo el material de FX30 y a6700 quedaba
fuera del sync por envelope**, en silencio y con exit 0. Agregar una cámara
era editar ocho archivos y acordarse de todos.

### Cómo agregar una cámara

1. Correr `python3 bin/inventario_camaras.py --root <disco>` — agrupa el
   material por cámara y marca lo que ningún perfil reconoce.
2. Revisar el borrador que deja en `camera_profiles.suggested.json`: trae el
   `match` ya armado y el `role` en `REVISAR`.
3. Mover el perfil corregido al registro del proyecto (sólo este disco) o al
   del motor (todos los proyectos).
4. `bash bin/run_tests.sh` — `tests/test_camaras.py` falla si el perfil nuevo
   quedó fuera de algún consumidor o si no se puede expresar en SQL.

No hace falta tocar ningún `.py`.

### Perfiles genéricos = red de seguridad

`sony-generico`, `canon-generico`, `panasonic-generico`, `nikon-generico`
capturan cualquier cuerpo de esas marcas como **principal**. Un cuerpo nuevo
entra al pipeline en vez de desaparecer sin avisar (lección 50). El inventario
los reporta como `<-- generico` para que se les haga perfil propio y se les
puedan afinar `sidecar`, `decodable` y `audio`.

### Formatos que ffmpeg no decodifica

BRAW, R3D, ARRIRAW y CinemaDNG llevan `decodable: false`. Resolve **sí** los
conforma nativo, así que se indexan y se llevan a timeline con normalidad,
pero los pasos que necesitan decodificar (`analyze_segments`,
`transcribe_clips`) los saltan **registrando el motivo**. Un salto silencioso
sería el mismo bug de la lección 50 con otro disfraz.

## Cómo verificar (queries útiles)

Inventario por cámara, project-wide:
```sql
SELECT COALESCE(camera_make,'?') mk, COALESCE(camera_model,'?') modelo,
       COUNT(*) n
FROM clips WHERE file_kind='video'
GROUP BY mk, modelo ORDER BY n DESC;
```

Cámaras por ubicación (extraer la ubicación del rel_path):
```sql
SELECT TRIM(REPLACE(substr(rel_path,18,instr(substr(rel_path,18)||'/','/')-1),
                    '_',' ')) ubic,
       COALESCE(camera_model, codec_name||' '||width||'x'||height) cam,
       COUNT(*) n
FROM clips WHERE file_kind='video' AND rel_path LIKE '<RAIZ>/%'
GROUP BY ubic, cam ORDER BY ubic, n DESC;
```

Prefijos de nombre (para descubrir cámaras desconocidas):
```sql
SELECT upper(substr(filename,1,4)) pfx, COUNT(*) n
FROM clips WHERE file_kind='video'
GROUP BY pfx ORDER BY n DESC LIMIT 25;
```

## Trampas

- **No asumir una sola cámara principal.** En ESCALANDO MEXICO había
  Canon 6D **y** Blackmagic. Asumir solo Canon ignoraba 302 clips de
  cine.
- **4K no es señal de cine** — GoPro Hero recientes graban 4K.
- **ProRes puede ser transcode** (no cámara nativa). Comprobar:
  ¿hay un gemelo h264 con el mismo basename? Si sí, es transcode.
- **Resoluciones raras** (`2704×2028`, `2720×1530`, `2688×1512`) son
  modos 2.7K de GoPro — sigue siendo acción.
- `camera_make` / `camera_model` están con frecuencia en `NULL` —
  ffprobe no las extrae siempre. Necesitas exiftool. Fallback: prefijo
  del nombre + códec + resolución/fps.

---

# Input Color Space e Input Gamma (2026-08-18)

Petición del editor: *«necesito que puedas decirme el input color space e input
gamma de cada cámara»*. Es lo primero que se rellena al abrir un proyecto con
Color Management, y equivocarse ahí **no da error**: da una imagen plana o
quemada que parece un problema de etalonaje y se persigue durante horas en el
sitio equivocado.

```bash
python3 ~/cinema-assistant/bin/color_por_camara.py --root "$DISK" --medir
```

Escribe `reports/color-<fecha>.md` con los nombres **exactos** de los
desplegables de Resolve. Si no coinciden letra por letra, el editor no los
encuentra en la lista y el informe no sirve.

## La gamma es dato; el gamut casi nunca

Es la misma distinción que con el rol de una cámara, y aquí es más afilada:

- **La gamma se mide.** Sony la escribe literal en el sidecar XAVC
  (`CaptureGammaEquation`); las demás cámaras la dejan —o no— en los tags del
  contenedor (`color_transfer`).
- **El gamut no.** Sony pone `CaptureColorPrimaries: rec709` en el sidecar
  **aunque la cámara esté en S-Gamut3.Cine**: ese campo describe la codificación
  del contenedor, no el espacio de captura. Quien puso la cámara en PP8 o en PP9
  lo sabe; el archivo no.

Así que el script mide lo medible, **propone** lo más probable para lo que no, y
lo dice con esas palabras. Nunca afirma un gamut que no puede ver.

## Tercera señal: la imagen, cuando el contenedor miente

Un tag puede mentir. DJI etiqueta `bt709` tanto en Normal como en D-Log M, así
que el contenedor no distingue. La luma sí: una curva log **levanta el negro y
comprime el rango**. En 10 bits el negro legal es 64 y el blanco 940; S-Log3
deja el negro sobre ~95.

Medido el 2026-08-18 sobre material real:

| Cámara | YMIN | YMAX | Lectura |
|---|---:|---:|---|
| Sony A7 IV (`s-log3` confirmado por sidecar) | 94–110 | 933 | negro levantado → log |
| DJI Osmo Pocket 3 | 73–86 | hasta 990 | usa el rango entero → Rec.709 |

Es **corroboración, no veredicto**: un plano nocturno también tiene el negro
alto. Cuando la luma contradice al metadato, el script lo dice y manda mirar un
fotograma antes de fijar nada.

## El rango de datos también se reporta

El A7 IV marca `color_range = pc` (rango completo). En Resolve eso es
**Data Levels = Full**; dejarlo en *Video* recorta los extremos. Se avisa por
cámara, porque es tan silencioso como el resto.

## Se declara y gana sobre todo

```json
"camera_color": {
  "Video 01": {"input_color_space": "S-Gamut3.Cine",
               "input_gamma": "S-Log3",
               "nota": "PP8, confirmado por quien puso la cámara"}
}
```

Mismo patrón que `camera_skews_manual`, `roll_overrides` o `take_overrides`: el
motor mide y propone, el editor firma, y lo firmado gana.

## Tabla de referencia

Nombres tal cual salen en Resolve:

| Encoding | Input Color Space | Input Gamma |
|---|---|---|
| Sony S-Log3 (PP8) | `S-Gamut3.Cine` | `S-Log3` |
| Sony S-Log3 (PP9) | `S-Gamut3` | `S-Log3` |
| Sony S-Log2 | `S-Gamut` | `S-Log2` |
| Rec.709 | `Rec.709` | `Rec.709 Gamma 2.4` |
| HLG | `Rec.2020` | `Rec.2100 HLG` |
| DJI D-Log | `DJI D-Gamut` | `DJI D-Log` |
| Panasonic V-Log | `V-Gamut` | `V-Log` |
| Canon Log 3 | `Cinema Gamut` | `Canon Log 3` |
| ARRI LogC3 | `ARRI Wide Gamut` | `ARRI LogC3` |

Vive en `lib/color_pipeline.RESOLVE`, con prueba
(`tests/test_color_pipeline.py`) que falla si alguien le cambia un nombre.
