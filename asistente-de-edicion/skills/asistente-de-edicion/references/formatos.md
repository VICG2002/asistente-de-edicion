# Formatos de cine y audio de campo (v0.2.0)

## Material que ffmpeg no decodifica

BRAW, R3D, ARRIRAW y CinemaDNG **no los abre ffmpeg** — no hay decoder libre.
Resolve **sí** los conforma nativo, así que:

- se **indexan** con `mediainfo` / `exiftool` y llegan a la timeline con
  normalidad;
- los pasos que necesitan **decodificar** (`analyze_segments`,
  `transcribe_clips`) los **saltan**;
- ese salto queda **registrado y dicho en pantalla**, nunca en silencio.

El perfil de cámara lo declara con `"decodable": false` y una `note` que explica
por qué. `index_project.py` copia eso a `clips.decodable` y `clips.skip_reason`.

```
⊘ transcribe_clips: 3 clip(s) SALTADOS (ffmpeg no los decodifica)
     2  Blackmagic RAW: ffmpeg no lo decodifica. Resolve lo conforma nativo…
           ej: A001.braw, A002.braw
     1  RED R3D: requiere REDline para decodificar…
           ej: B001.R3D
   Esos clips SI llegan a la timeline: Resolve los conforma nativo.
```

**Por qué importa**: un salto silencioso es la lección 50 con otro disfraz — el
pipeline corre, dice OK, y ese material simplemente no está.

| Formato | Sonda | ffmpeg lo abre? |
|---|---|---|
| MXF (XAVC, XF-AVC, DNxHD) | ffprobe | **Sí** — sólo faltaba en el registro |
| BRAW | mediainfo | No |
| R3D | mediainfo | No (necesitaría REDline) |
| ARRIRAW (.ari/.arx) | mediainfo | No |
| CinemaDNG | exiftool | No (es secuencia de imágenes) |

### Migración automática del manifest

`clips.decodable` y `clips.skip_reason` se agregan con `ALTER TABLE` al abrir el
manifest (`manifest.migrate()`). Los discos de proyectos ya cerrados **no hay
que re-indexarlos**.

### El Lua tolera fps y duración nulos

BRAW/R3D pueden llegar sin `fps` ni `duration_sec`. Antes,
`math.floor(b.s * fps)` con `fps` nil reventaba el script entero y el editor se
quedaba sin **ningún** marker por culpa de un puñado de clips.
`LIB.fpsSeguro(fps, timeline)` cae al frame rate de la timeline, y si tampoco lo
hay, a 24.

---

## Audio de campo poly-WAV (Zoom F3/F6/H6, MixPre, Tascam)

### El supuesto que estaba roto

El motor asumía que un WAV externo es **un lavalier en estéreo**. Una grabadora
de campo escribe otra cosa: un WAV **polifónico** de 2 a 8 canales donde cada
canal es un micrófono distinto.

Con el supuesto viejo:

- el sync emparejaba el **archivo** con el video, así que el "lavalier de ESCALADOR_A"
  llevaba encima la voz de todos los demás;
- los nombres de pista se perdían — y son el único dato que dice qué canal es de
  quién;
- el `bext.TimeReference` se ignoraba.

### `lib/bwf.py` — lector sin dependencias

`struct` sobre el RIFF. Lee `fmt `, `data`, `bext` e `iXML`. Soporta **RF64**
(los archivos de campo pasan de 4 GB con facilidad). No toca el archivo.

```python
from lib import bwf
i = bwf.leer("/Volumes/MI_DISCO/…/ZOOM0001.WAV")
i.channels          # 4
i.tc_segundos       # 37425.0  ← bext.TimeReference / sample_rate
i.track_names       # {1: 'ESCALADOR_A lav', 2: 'Ana lav', 3: 'Boom', 4: 'Ambiente'}
i.scene, i.take     # '12', '3'
i.es_polifonico     # True
```

### Sync por timecode

`bext.TimeReference` es la posición de la primera muestra en la línea de tiempo
del día, **en muestras**. Si la cámara también graba TC, el offset sale de una
resta — más barato y más exacto que correlacionar waveforms:

```python
offset = bwf.offset_por_timecode(info, bwf.timecode_a_segundos(video_tc, fps))
```

Convención del motor: `offset = audio_start - video_start`.

**Si falta cualquiera de los dos timecodes, devuelve `None`.** No adivina: un
offset inventado coloca el audio en el lugar equivocado y el editor lo descubre
a mitad del corte.

`index_audio_tracks.py` rellena `clips.timecode` desde `bext` cuando ffprobe no
lo trajo — eso es lo que habilita esta vía.

Nota: el TimeReference se parte en dos words de 32 bits. A 192 kHz un día
completo son 1.66e10 muestras; leyendo sólo la parte baja el timecode sale
disparatado. `lib/bwf.py` reconstruye los 64 bits (hay prueba que lo fija).

### Un lavalier por canal

Tabla `audio_tracks(clip_id, channel_idx, track_name, role, mono_path)`.

El **rol** se deduce del nombre de pista y decide qué canales van al sync de
diálogo — mandar el ambiente o el mix estéreo produce falsos positivos:

| Rol | Se reconoce por |
|---|---|
| `lavalier` | lav, lava, lapel, tx, wireless, radio |
| `boom` | boom, shotgun, caña, perch |
| `ambiente` | amb, ambien, room, atmos, nat, wild |
| `mix` | mix, master, lr, stereo, mezcla |
| `?` | lo demás |

### Separación de canales

```bash
python3 ~/cinema-assistant/bin/index_audio_tracks.py --root <disco> --split --solo-polifonicos
```

`ffmpeg -filter_complex pan=mono|c0=cN` extrae cada canal a
`<disco>/.cinema_assistant/audio_split/<clip_id>_ch<n>.wav`.

- **No toca el original** — respeta la regla de no modificar material fuente.
- Es **idempotente**: no re-extrae lo que ya existe.
- Escribe a `.parcial.wav` y renombra, así que nunca queda un mono a medias.

**Por qué separar antes y no después**: `AutoSyncAudio` liga el archivo
completo, y el mapeo de canales es de sólo lectura por API. Separar es la única
vía para sincronizar y mergear canal por canal.

### Orden en el pipeline

`bin/run_pipeline.py` — justo después de `index_audios.py` y **antes** de
cualquier sync.

Si hay poly-WAVs y no se corrió con `--split`, el script avisa:

> Hay N audio(s) polifónico(s). Sin `--split`, el sync empareja el ARCHIVO
> completo con el video: el 'lavalier de ESCALADOR_A' llevará encima la voz de todos.

## Los settings de un proyecto de Resolve

**Se leen por la API, no del `Project.db`.** Comprobado sobre Resolve 21.0.2
(`DbAppVer 21.0.2.0004`, `DbPrjVer 17`) el 2026-08-19: los ajustes de proyecto
viven en `SM_Config.SetupBA`, un struct binario de 2704 bytes **sin nombres de
campo**. No hay forma honesta de sacarlos de ahí; adivinar el offset de cada uno
sería inventar.

Lo que sí se lee del `Project.db`, y sin abrir Resolve
(`lib/timeline_resolve.py`): resolución y frame rate **de cada timeline**, su TC
de inicio, sus pistas y sus items. Para el resto —color science, gamma,
monitorización, data levels— está `resolve/auditar_settings.lua`, que los
vuelca a `<disco>/.cinema_assistant/resolve/<slug>_settings.json` con el
proyecto abierto. Es un script de lectura: no toca ni un ajuste.

**Los settings de proyecto y los de timeline no son lo mismo.** Una timeline con
`useCustomSettings = 1` ignora los del proyecto. En Morsa conviven, en el mismo
proyecto:

| timeline | resolución | fps |
|---|---|---|
| `Cut 1.2 sonido ayan`, `color`, `Cut 1.2` | 3840×2160 | 23.976 |
| `Reels verticales`, `MORSA — Reel 2/3` | **2160×3840** | 23.976 |

De ahí la regla de `reel_subtitulado.lua`: la timeline de un reel se fija a la
resolución **y al frame rate del propio archivo**, no a los del proyecto. Un
reel vertical en un proyecto 16:9 hereda 1920×1080 y sale con bandas negras a
los lados; un export a 29.97 en un proyecto a 23.976 queda con la timeline al
fps equivocado. Y como siempre: la comprobación no es «¿devolvió la llamada?»
sino «¿quedó puesto?» — se vuelve a leer con `GetSetting` y se compara.

Referencia de entrega medida en Morsa (documental, tres FX30/A6700 a 23.976p 4K
S-Log3): render HEVC 10 bit `yuv420p10le`, `bt709` en transferencia y primarios,
audio **PCM 24 bit 48 kHz estéreo**, TC de inicio 01:00:00:00, 2 canales de
salida y ganancia de máster 0.0.

Un detalle que conviene anotar cuando pase: en `Exports/` conviven `cut 1.3.mov`
y `cut 1.3 gamma2.4.mov`, renderizados con 22 minutos de diferencia. Hubo una
pelea de gamma —el clásico desplazamiento de QuickTime— y no quedó escrita en
ningún sitio. Cuando se rinde dos veces lo mismo con distinto ajuste, el motivo
va al `project_config.json` o se pierde.
