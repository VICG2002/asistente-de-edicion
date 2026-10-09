# Sync de audio externo (dual-system)

> **Documento principal para proyectos nuevos**:
> [`sync-sin-ground-truth.md`](sync-sin-ground-truth.md) — flujo end-to-end,
> heurísticas empíricas, umbrales descubiertos en Zezzions, y política de
> qué hacer cuando NO hay `.drt` del editor para validar.
>
> Este documento ahora es histórico (evolución del método). El flujo
> operativo actual está en el doc nuevo.

### 1. Por timecode (LEGACY, no usar como única señal)
`lib/audio_sync.py`. Lee BWF `OriginationDate`/`OriginationTime` de los
WAV + `creation_time`/`timecode` del video; empareja por solapamiento
temporal.

**Por qué falla:** los relojes de cámara consumer y recorder consumer no
están jam-sincronizados. El solapamiento de hora-del-día empareja por
coincidencia. Resultado en ESCALANDO MEXICO: 11 pares con GoPro, todos
basura.

### 2. Por correlación de waveform (legacy, mejor que TC pero no verificado)
`lib/waveform_sync.py` + `bin/sync_waveform.py`. Decodifica el audio
scratch del video y los WAV externos a una envolvente de loudness a
100 Hz; cross-correla por FFT; pico normalizado → offset y confianza.

Mejor que TC porque no depende del reloj. Pero **no verifica
contenido** — la correlación mide solo ritmo de loudness. Audios cortos
producen falsos positivos.

Si se usa: filtrar audio < 60 s (esos fragmentos dan picos espurios
con alta confianza engañosa).

### 3. Por contenido del transcript — **MÉTODO ACTUAL**
`lib/transcript_sync.py` + `bin/sync_transcript.py`.

#### Idea
Transcribir el audio del video y los WAV externos a palabras con
timestamps. Si las **mismas palabras** aparecen en ambos, su diferencia
de tiempo es el offset. Cientos de n-gramas coincidentes votan el
mismo offset → confianza altísima.

#### Algoritmo
- Normalizar palabras (lowercase, sin acentos, solo alfanumérico).
- Indexar 4-gramas de la grabación → tiempos de inicio.
- Para cada 4-grama del clip, buscar en la grabación. Cada match vota
  `t_rec - t_clip`.
- El clúster denso (ancho ~0.6 s) es el offset; el % de 4-gramas del
  clip que votan ese clúster es la confianza.
- **Negar** el offset → guardar `audio_start - video_start`.

#### Por qué es robusto
El emparejamiento viene de las **palabras exactas dichas**, no de la
intensidad del sonido. Imposible emparejar dos grabaciones distintas
por accidente; las palabras coinciden o no.

#### Para que funcione
Ambos lados necesitan **habla legible**. La GoPro queda fuera (no es
dual-system; trae su audio embebido).

#### Filtros recomendados
- Clip con ≥ 60 **palabras distintas** (descarta no-habla y
  alucinaciones de Whisper como "Suscríbete al canal" repetido).
- Confianza ≥ 0.5 al placement (matches reales dan 0.6–0.85; ruido ~0.4).

## Convención de signo del offset

Toda la pipeline usa **`offset = audio_start - video_start`**.

- **Negativo** → el WAV empezó antes que el clip (se coloca atrás en
  la timeline).
- **Positivo** → el WAV empezó después (corto, encaja dentro del clip).

El Lua de la Consola coloca el WAV con:
```lua
recordFrame = clip_timeline_start + sy.offset * fps
```

`transcript_sync.align` internamente calcula `t_rec - t_clip` (la
mediana del clúster) y **niega** antes de devolver, para mantener la
convención.

## Marker en el timeline

Cyan en el primer frame del clip:
```
ZOOM0007_LR.WAV  off -5.4s  conf 0.72
```

Suficiente para que el editor verifique de un vistazo y haga el ajuste
fino si hace falta.

## Cómo correr

```bash
python3 ~/cinema-assistant/bin/sync_transcript.py \
  --root "$DISK" --location <substring> \
  --min-distinct 60 --min-confidence 0.5
```

Borra `audio_sync_pairs` y los reescribe con `method='transcript'`.
Re-correr cuando haya nuevos transcripts (sin re-transcribir).

---

## Barrido por CONSENSO DE RELOJ (Adrián, The Shelter 2026-08-03)

Para material donde el transcript no alcanza —concierto, evento musical, B-roll con
ambiente— el barrido masivo por waveform con **verificación por reloj** recupera mucho sync
que los métodos por contenido dejan fuera. En The Shelter pasó de 9 videos a 33 en una
corrida, y con los onsets encima, de 33 a 103.

> **Nota**: `bin/sweep_sync_clock_consensus.py` es de la bifurcación de Adrián y **no está
> en este motor**. Lo que sigue es el método, que es lo portable.

Las tres piezas importan y son inseparables:

1. **Full-spectrum por default.** El filtro de voz mata el bajo y el kick, que en música son
   la señal mejor definida temporalmente.
2. **Aceptar matches de un solo canal.** Los TX van en personas distintas; que un canal no
   matchee no es evidencia contra el otro.
3. **El discriminante es el consenso de reloj:**

   ```
   drift = offset_medido − (audio_start − video_start)      [start = mtime − dur]
   ```

   El reloj de cada cámara está corrido una cantidad ~constante respecto al grabador, así
   que **todos los pares verdaderos de una misma cámara+canal comparten el mismo drift**. Un
   falso positivo cae en cualquier lado.

   Evidencia: 36 de 38 candidatos dentro de ±1.1 s de la mediana de su cámara; los pares
   confirmados A OÍDO por el editor cayeron dentro de ±0.9 s de esas mismas constantes; los
   2 outliers (±150 s y −40 s) eran falsos positivos. Constantes medidas allí: cámara A
   −10.1/−12.4 s, cámara V −45.8/−48.1 s (canal 01/canal 02). La diferencia entre canales
   (~2.2 s) coincidía con que los dos grabadores arrancaron con ~2 s de separación:
   consistencia interna que confirma el modelo.

**Salvaguardia crítica**: el consenso se ancla en los pares **ya verificados** de la base.
Si se derivara solo de los candidatos, un grupo con **n=1** haría que la mediana FUERA el
propio candidato — desviación 0, y cualquier basura se auto-aprueba. Hay que exigir un
mínimo de votos (3) cuando no hay ancla, y marcar el resto como "no verificable" en vez de
aceptarlo. Es la misma familia que el gate anti-outlier de AVA, pero con el fallo al revés:
allí el riesgo era rechazar de más, aquí es aprobarse a sí mismo.

## Sincronizar una cámara SIN audio utilizable, por la LUZ (Adrián, The Shelter 2026-08-04)

Cámara de seguridad: su audio no sirve para nada (mono comprimido, lejos de todo). Aun así
se puede amarrar al proyecto con **dos señales que no son audio**.

### 1. El nombre del archivo suele ser un reloj

Las NVR nombran los archivos con la hora: `CCTV/21h/32m39s_alarm_280s_hd.mp4` = 21:32:39,
280 s. Verificar contra el `mtime` antes de confiar: allí coincidían con desviación estándar
de **0.65 s en 70 clips**, así que el nombre era fiable.

**Trampa encontrada**: el `mtime` iba **6 horas corrido** respecto al nombre (zona horaria
del equipo). El nombre estaba en hora local y el `mtime` no; colocar por `mtime` mandaba la
CCTV seis horas fuera del evento. Contrastar siempre el rango resultante contra el de las
cámaras principales.

### 2. La luz sincroniza aunque el audio no

Todas las cámaras de la sala ven **los mismos cambios de iluminación** (luces de escenario,
destellos). Correlacionando luminancia se obtiene el desfase contra una cámara ya
sincronizada, y por encadenamiento la CCTV queda amarrada al lavalier.

**Usar la DERIVADA de la luminancia, no la luminancia absoluta.** Medido en el mismo par:
absoluta z=3.15 (ruido), derivada **z=11.73**. La razón es la misma que con los onsets: cada
cámara tiene su propia exposición y ganancia, así que el NIVEL de luz no es comparable —
pero el INSTANTE en que la luz cambia sí es el mismo para todas.

```bash
# luminancia media por frame a 12 Hz, imagen diminuta (rapido)
ffmpeg -ss S -i CLIP -t D -vf "fps=12,scale=64:36,format=gray" -f rawvideo -pix_fmt gray -
# correlacionar np.clip(np.diff(luma), 0, None) entre CCTV y camara
```

Descartado por no funcionar: correlacionar la luz contra los onsets de la música, por si las
luces pulsaran al ritmo. z≈2-3.

### 3. Consenso, porque un z alto NO garantiza acierto

Igual que con los onsets musicales, un z alto puede ser un enganche en el lugar equivocado.
De 32 mediciones, **21 cayeron en −4.17 s con desviación estándar 0.40 s** y 11 quedaron
dispersas entre −75 y +43 s. Se toma la mediana del racimo, no los casos sueltos.

**Verificación obligatoria**: re-medir DESPUÉS de aplicar la corrección; los desfases deben
salir ≈0. Allí quedaron en +0.25 s (desv. 0.25). Señal extra de acierto: un clip que antes
medía +20.75 s pasó a +0.25 s con z=15.6 — el enganche falso se resolvió solo al partir del
reloj corregido.

### Precisión que se consigue

~±0.3 s. Suficiente para ubicar la cámara como referencia y cortar acción general; **no es
frame-accurate**. Como su audio no se usa igual, se corta con el lavalier o con el audio de
las otras cámaras debajo.
