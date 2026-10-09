# Merge de audio en el Media Pool (v0.2.0)

> ## CAMBIO DE PAPEL (2026-08-13) — leer antes que nada
>
> El merge **dejó de ser una forma de entrega y pasó a ser un instrumento de
> medición**. Todo lo que sigue en este documento sobre cómo funciona
> `AutoSyncAudio` es correcto; lo que cambia es PARA QUÉ se usa.
>
> **Por qué.** El B-ROLL cronológico pone el WAV del lavalier ENTERO en su
> propia pista (ver `roll.md`). El merge pega ese mismo lavalier DENTRO de cada
> clip, o sea lo fragmenta en un trozo por clip. Las dos cosas a la vez hacen
> que el lavalier suene **dos veces** y desalineado consigo mismo.
>
> **Cómo conviven.** Son dos proyectos con papeles distintos:
>
> | | Proyecto DUPLICADO | Proyecto de ENTREGA |
> |---|---|---|
> | Qué pasa ahí | Se mergea y se mide | Se arman las timelines |
> | `AutoSyncAudio` | Sí | Nunca |
> | Qué produce | **Offsets**, no timelines | La entrega |
> | `MODO_MERGE` | — | **`false`, declarado** |
>
> El producto del duplicado no es una timeline: son los offsets que midió
> DaVinci, que `leer_sync_merge.lua` lee y `import_iban_offsets.py` escribe al
> manifest como `method='resolve-merge'`. El duplicado se tira después.
>
> **`MODO_MERGE = false` hay que DECLARARLO**, no basta con no correr
> `merge_pool.lua`: el script lo autodetecta mirando si los clips del pool traen
> audio ligado, y el merge no se deshace por API. Un proyecto donde alguna vez
> se mergeó lo volvería a encender solo.
>
> ```
> MODO_MERGE = false; dofile(".../asistente_<proyecto>.lua")
> ```
>
> **Lo que NO cambia:** el merge sigue siendo la única vía al *waveform sync*
> nativo de DaVinci, y sigue siendo la salida cuando el correlador propio no
> converge — el caso fundador son los 97 clips de la a6700 de Iban en Morsa, que
> cinco métodos no pudieron medir porque la música del concierto es periódica.

## Lo que cambia

Antes el sync vivía **en la timeline**: cada timeline nueva había que
reconstruirla con el Lua para tener el audio bueno. Con el merge el lavalier se
pega **dentro del clip**, en el Media Pool. Cualquier timeline que armes a mano
— arrastrando desde el pool — ya trae el audio sincronizado.

## Medido en Resolve 21.0.2 Free (2026-07-30)

No son suposiciones. Se corrió `resolve/spike_merge.lua` y `spike_merge2.lua`
sobre un par sintético con offset conocido de 5 s, en un proyecto de prueba
aparte:

| Pregunta | Resultado |
|---|---|
| ¿`AutoSyncAudio` existe y corre en **Free**? | **Sí** |
| ¿`RETAIN_EMBEDDED_AUDIO=true` conserva el audio de cámara? | **Sí** — `embedded_audio_channels` > 0 |
| ¿El offset de Resolve coincide con el real? | **Sí — 0 ms de error** |
| ¿El clip mergeado baja con sus dos pistas separadas? | **Sí, pero…** (ver abajo) |
| ¿`AddTrack{index=2}` inserta y desplaza el **contenido**? | **Sí** |

### El "pero" que casi cuesta el lavalier

`AppendToTimeline` **NO crea pistas de audio al vuelo**. Si el clip mergeado
trae 2 pistas y la timeline tiene 1, **la segunda se pierde en silencio**:

```
timeline con 1 pista →  A1: 1 item   A2: 0 items   ← el lavalier no bajó
timeline con 2 pistas → A1: 1 item   A2: 1 item    ← correcto
```

Por eso `buildTimeline` prepara las pistas **antes** del append
(`LIB.prepararPistasAudio`). Es exactamente la clase de bug que este proyecto
persigue: el script corre, dice OK, y el audio no está.

## La decisión arquitectónica

**No** seleccionar 200 clips y dejar que Resolve empareje. Resolve empareja por
waveform sobre todo el set y se equivoca — es el fallo que el motor ya resuelve
(falsos positivos de B-roll "con energía RMS parecida",
`patrones-exitosos.md:531`).

En vez de eso: **una llamada de `AutoSyncAudio` por grupo verificado**, donde el
grupo es `{video, wav1[, wav2]}` que el motor ya emparejó con sus cuatro señales.

> El motor aporta el **emparejado** — lo difícil.
> Resolve aporta la **alineación sub-frame y el link nativo** — lo único que
> sólo él puede hacer.

## Convención de signos

```
manifest:  offset = audio_start - video_start        →  -5.000 s
Resolve :  linked_audio[n].offset, en MUESTRAS       →  +240000  (+5.000 s)

           offset_resolve_seg  ==  -offset_manifest_seg
```

`bin/verify_merge.py` usa esa igualdad. Si Resolve cambia el signo en una
versión futura, `tests/test_merge.py` lo delata.

## El layout de pistas — la regla dura

```
A1                cámara base
A2 .. A(1+C)      cámaras compañeras (multicám)
A(2+C) ..         lavaliers
```

Con el clip mergeado, su audio cae en A1 (cámara) y A2 (lavalier). Para meter la
cámara compañera **entre** las dos sin romper nada, se usa la **Vía A**:

1. Preparar las pistas y hacer el append normal → `A1 cámara · A2 lavalier`.
   **El clip base conserva su link video/audio** (es un solo item).
2. `AddTrack("audio", {index = 2})` → se inserta una A2 vacía y el lavalier baja
   a A3, **con su contenido**.
3. Colocar el audio de la compañera en A2.

Resultado: `A1 cámara base · A2 cámara compañera · A3 lavalier`.

Esta era la vía que en el plan iba marcada como "a verificar". Se verificó y
funciona, así que no hizo falta caer a la Vía B (que dejaba el clip base con
video y audio desligados).

## Flujo

```bash
# 1. Hornear el plan (sólo pares con confianza >= 0.50)
python3 ~/cinema-assistant/bin/export_merge_plan.py --root <disco>
```

```lua
-- 2. En la Consola de Resolve, sobre un proyecto DUPLICADO:
MERGE_PLAN = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proy>_merge.lua"
dofile(os.getenv("HOME") .. "/cinema-assistant/resolve/merge_pool.lua")
```

```bash
# 3. Comprobar que el merge quedó donde el motor dijo
python3 ~/cinema-assistant/bin/verify_merge.py --root <disco>
python3 ~/cinema-assistant/bin/verify_track_order.py --root <disco>
```

Después, el `asistente_<proyecto>.lua` **detecta solo** que los clips están
mergeados y cambia a MODO MERGE. Para forzarlo: `MODO_MERGE = true|false` antes
del `dofile`.

## El merge NO se deshace por API

No existe `UnlinkAudio`; `UnlinkClips` es otra cosa (deja los clips offline).
Mitigaciones, en orden:

1. **`merge_pool.lua` exige** que el nombre del proyecto diga
   PRUEBA / COPIA / TEST / MERGE. Para saltarse el candado hay que poner
   `MERGE_FORZAR = true` a conciencia.
2. **`RETAIN_EMBEDDED_AUDIO = true`** garantiza que el A1 de cámara nunca se
   pierde.
3. **Idempotencia**: salta los clips que `GetAudioMapping()` ya reporta ligados,
   así que re-correrlo es seguro.
4. **Confianza mínima 0.50**, más exigente que el 0.30 del bake de timelines:
   el bake se rehace, el merge no.

## Verificadores

`verify_merge.py` compara el offset de cada clip mergeado contra el manifest y
falla si se desvía más de **un frame**. Un desfase así **no se ve a simple
vista**: el clip parece sincronizado y el problema aparece a mitad del corte.

También falla si algún clip perdió su `embedded_audio_channels` — es decir, si
`RETAIN_EMBEDDED_AUDIO` no se aplicó y se perdió el audio de cámara.

`verify_track_order.py` lee el layout que vuelca el Lua
(`<proy>_layout.json`) y falla si en alguna timeline un audio externo quedó por
encima de uno de cámara.
