# Arquitectura

### Motor (`~/cinema-assistant/`) — reusable entre proyectos

```
~/cinema-assistant/
├── bin/
│   ├── index_project.py
│   ├── index_audios.py
│   ├── analyze_clips.py
│   ├── analyze_segments.py
│   ├── transcribe_clips.py
│   ├── transcribe_audios.py
│   ├── sync_transcript.py          # método actual
│   ├── sync_waveform.py            # legacy (sin verificar contenido)
│   ├── sync_audio.py               # legacy (timecode)
│   ├── build_contact_sheets.py
│   ├── export_lua_data.py
│   └── generate_report.py
├── lib/
│   ├── manifest.py                 # schema SQLite del manifest
│   ├── probe.py                    # ffprobe/mediainfo wrappers
│   ├── classify.py                 # tipos de archivo + filtros
│   ├── analysis.py                 # ffmpeg signalstats parsing
│   ├── audio_sync.py               # legacy timecode
│   ├── waveform_sync.py            # legacy envelope correlation
│   ├── transcript_sync.py          # alineación de transcripts (actual)
│   ├── transcribe.py               # whisper-cli wrapper
│   ├── fcpxml.py                   # legacy, NO usar
│   └── drp_template.py             # legacy, NO usar
├── resolve/
│   ├── asistente_jilotepec.lua     # script de Consola (v6+)
│   └── escalando_data.lua          # auto-generado, NO editar a mano
├── models/
│   └── ggml-large-v3-turbo.bin     # Whisper turbo (1.5 GB)
├── config/
│   └── defaults.py                 # umbrales y extensiones
└── projects.json                   # registro de proyectos conocidos
```

### Por-proyecto (`<disco>/.cinema_assistant/`) — datos del proyecto

```
<disco>/.cinema_assistant/
├── manifest.sqlite                 # FUENTE DE VERDAD
│   ├── clips                       # toda la metadata por archivo
│   ├── clip_analysis               # exposición/movimiento/audio
│   ├── clip_segments               # tramos usables (duration markers)
│   ├── audio_sync_pairs            # emparejamientos video↔audio
│   └── (audio_catalog)             # pendiente, clasificación del audio
├── transcripts/<clip_id>.json      # transcripts cacheados (word-level)
├── logs/                           # logs por corrida (timestamped)
└── reports/                        # exports, csv, html
```

Todo el flujo es: leer el disco → poblar el manifest → producir
artefactos (hojas, transcripts, `escalando_data.lua`).

## Por qué Lua y no Python en Resolve

DaVinci Resolve **Free** no expone API externa de scripting
(`scriptapp("Resolve")` devuelve `None`; eso es Studio). La integración
funcional es la **Consola interna** (Workspace → Console) en modo
**Lua**.

La razón original era de instalación: Python en la Consola requería el Python
de python.org, no el del sistema, y no valía la pena. **Esa razón caducó en
21.1**, que trae su propio Python 3.14 embebido donde `import
DaVinciResolveScript` funciona sin variables de entorno.

La conclusión no cambió, pero el motivo sí, y conviene tenerlo claro para no
defender la decisión con un argumento muerto: ahora se usa Lua porque en 21.1
**el scripting en Python quedó del lado de Studio**. El camino cómodo apareció
y se cerró en la misma versión.

## Cómo se conectan las capas

1. Motor lee `<disco>/.cinema_assistant/manifest.sqlite`.
2. Produce `~/cinema-assistant/resolve/escalando_data.lua`.
3. La Consola Lua lo lee con `dofile(...)` y reconstruye los timelines.

Los timelines son artefactos: se borran y reconstruyen cada corrida.
El manifest es persistente; las fuentes son intocables.

## Cache de transcripts

Cada transcript es caro (~3 s × tiempo de audio / 14). Una vez hecho,
se cachea como JSON keyed por `clip_id`. Re-correr cualquier paso que
necesite transcripts es instantáneo en lo cacheado.

## Bake `escalando_data.lua`

Indexado por **ruta absoluta** (la ruta es única). Un table indexado
por nombre **pierde clips silenciosamente** cuando los nombres se
repiten (GoPro/DSLR reciclan nombres). Mantener `byname` solo como
índice de fallback (nombre → ruta).
