# Ver el material

Cómo mirar sin gastar de más, y cómo mirar lo que sale.

Nació el 2026-08-26 de evaluar dos herramientas de edición con agentes
—[`bradautomates/claude-video`](https://github.com/bradautomates/claude-video)
y [`browser-use/video-use`](https://github.com/browser-use/video-use)— contra lo
que este motor ya hacía. Casi todo lo que se solapaba, aquí ya estaba y mejor.
Lo que aportaron son estas cuatro cosas. Lo que no se adoptó está al final, con
el porqué, para que nadie lo reintroduzca dentro de seis meses.

---

## 1. El presupuesto de frames es por duración, no por posición

Hasta ahora los frames de un tramo salían por porcentaje fijo: 10, 30, 50, 70,
90. Es un reparto ciego, y falla en las dos direcciones. En una entrevista a
plano fijo los cinco frames son la misma imagen, y cada una se paga tres veces
—disco, batch de YOLO y descripción del vision LLM, que son de tres a cinco
horas sobre 1700 tramos—. En un tramo con movimiento pasa lo contrario: los
cinco caen donde caen y el corte que importaba queda fuera.

`lib/frames.presupuesto(dur)` decide cuántos frames merece el tramo por lo que
dura. Los números no salen de una fórmula sino de lo que cuesta describir: por
debajo de 10 s no hay dos momentos distintos que contar, y por encima de dos
minutos el vision LLM ya no mejora su descripción con más imágenes.

**El coste, para que esté escrito.** Ochenta frames a 512 px de ancho son del
orden de 50-80k tokens de imagen. Doblar el ancho los cuadruplica. Por eso el
ancho de trabajo es 720 px —medido como suficiente para el vision LLM— y por
eso el presupuesto se calcula en vez de estimarse a ojo.

## 2. Dónde: por cambio de escena, con caída declarada

`select='gt(scene,0.20)'` pone los frames donde la imagen cambia de verdad. Dos
detalles que no son opcionales:

- **Se detecta sobre el rango completo y se recorta después.** Capar la
  detección con `-frames:v` conserva los primeros cortes y tira la cola del
  tramo. Detectar todo y repartir es lo único que garantiza cobertura de punta
  a punta.
- **Si salen menos de 8 escenas, el tramo es estático** (plano fijo de
  entrevista, prompter) y se cae a muestreo uniforme. La caída **se declara**:
  el acta que devuelve `elegir_frames` lleva siempre `motor`, `candidatos`,
  `duplicados`, `elegidos` y `caida`. Un consumidor que no sepa cómo se
  eligieron sus frames no puede juzgar lo que describe.

## 3. Qué sobrevive: el dedup por diferencia de luminancia

Los casi idénticos se tiran comparando la diferencia media por píxel de una
miniatura de 16x16 en gris; umbral 2.0 sobre 0-255. ffmpeg hace el decode en
**una sola pasada** sobre la secuencia de JPEG y el resto es stdlib: ni una
dependencia nueva.

Tres decisiones de diseño que hay que respetar si se toca:

- **Se compara contra el último CONSERVADO, no contra el anterior.** Contra el
  anterior, veinte frames que cambian poquísimo cada uno acaban siendo otra
  imagen y ninguno se tira: la deriva se escapa entera.
- **A diferencia de un hash perceptual dentro del frame, esto distingue frames
  planos por su luminancia.** Un fundido, un cielo, dos diapositivas lisas de
  distinto gris: un phash los ve iguales, la luma no.
- **Fail-open.** Si ffmpeg falla, si los nombres no forman secuencia o si los
  bytes no cuadran uno a uno, no se deduplica nada y se devuelve la lista
  intacta. Un dedup que adivina es peor que no deduplicar: tiraría frames
  buenos en silencio.

Medido sobre un plano sostenido de 20 s: 9 frames uniformes colapsan a 1. Con
`--sin-dedup`, los 9.

```bash
# El default no cambia: 'porcentaje' es lo de siempre, ningún proyecto
# existente se comporta distinto sin pedirlo.
python3 bin/extract_segment_frames.py --root "$DISK" --motor escena --force
python3 bin/extract_segment_frames.py --root "$DISK" --motor uniforme --tope 6
```

El acta de la corrida sale por pantalla: `duplicados_tirados`, `cues`,
`cues_fuera_del_tramo`, `tramos_estaticos_caidos_a_uniforme`. Sin esos números
no se puede juzgar si el motor eligió bien. **Un dedup que no tira nada está mal
calibrado**, y una caída masiva a uniforme dice que el material es estático y
que detectar escenas no aporta en ese proyecto: las dos cosas son información,
no ruido.

## 4. Los cues: los tiempos que el habla señala

La selección visual se pierde justo los momentos que alguien está señalando,
porque señalar algo apenas cambia la imagen. En este motor esos tiempos ya
existen y no había que inventarlos:

| Fuente | De dónde sale | Qué marca |
|---|---|---|
| `beats` | `interview_beats` (`bin/derive_interview_beats.py`) | la pregunta y dónde empieza a responder |
| `silencios` | `clip_silences` (`bin/detect_pauses.py`) | el centro de cada pausa real medida sobre el lavalier |

```bash
python3 bin/extract_segment_frames.py --root "$DISK" --motor escena \
    --cues beats,silencios --force
```

**Los cues se descuentan del tope ANTES de elegir**, así que el reparto no los
desaloja: un cue no compite con un frame de escena, gana siempre. Si por sí
solos llenan el tope, no se detecta nada más y el acta lo dice (`motor:
solo-cues`). Un cue que cae fuera del tramo se descarta y se cuenta.

Los dos scripts que producen esas tablas **tienen que haber corrido antes**. Si
se piden cues y no sale ninguno, `extract_segment_frames.py` avisa en vez de
seguir como si nada.

**Lo que NO está aquí, y por qué.** Los cortes de la timeline también son cues,
pero viven en tiempo de timeline y no en tiempo de clip. Mapearlos exige la
tabla de items, y una conversión mal hecha pondría frames en el sitio
equivocado sin avisar. Donde ese tiempo es el nativo es en
`bin/verify_export.py`, que trabaja sobre el export; ahí sí se usan.

---

## 5. La vista de tramo: el drill-down que faltaba

`bin/vista_tramo.py`. Un PNG con la imagen, el sonido y las palabras en la misma
regla de tiempo.

Este motor vive del audio —sync, pausas, offsets de lavalier, cortes— y todo lo
que devolvía eran números. `verify_lav_offsets.py` dice que un par está a 4.19 s
y el editor tiene que creérselo o irse a Resolve a comprobarlo a mano. **Un
desfase no se discute con una tabla: se ve.**

Qué dibuja:

1. Filmstrip del rango, con la hora de cada frame.
2. Una cinta de forma de onda por fuente de audio. El A1 de cámara arriba; el
   lavalier sincronizado debajo, con su offset escrito.
3. Las palabras del transcript cacheado, cada una en su tiempo.
4. Los silencios de `clip_silences` sombreados, cruzando las dos cintas.
5. Una regla de tiempo común a todo lo anterior.

```bash
python3 bin/vista_tramo.py --root "$DISK" --clip-id 1234 --desde 12 --hasta 24 \
    --audio ambos
python3 bin/vista_tramo.py --video "$EXPORT" --desde 0 --hasta 30 --out v.png
```

**Cómo se lee un offset aquí.** Las dos cintas comparten la regla y están ya
convertidas a tiempo de vídeo (convención del motor: `offset = audio_start -
video_start`, así que `t_audio = t_video - offset`). Si el sync está bien, los
picos coinciden en vertical. Si no, se ve cuánto y hacia dónde.

**El pico de normalización es COMÚN a las dos cintas.** Normalizar cada una
contra su propio máximo es lo que hace que un lavalier limpio y un A1 de sala
se dibujen igual de altos y parezcan la misma señal. Con un pico común, la que
grabó más bajo se ve más baja, que es la verdad.

**Es una herramienta de punto de decisión, no de barrido.** No se corre en bucle
sobre cada tramo del proyecto: cada vista cuesta una extracción de frames y un
decode de audio, y mirar mil imágenes no es mirar. Se corre cuando algo está EN
DUDA: un par de sync que no cuadra, una pausa que no se sabe si es contenido, un
corte que suena raro. Para el barrido están los verificadores, que devuelven
números y son baratos.

No escribe en el manifest. Lee y dibuja.

---

## 6. El entregable se mira, no solo se mide

`bin/verify_export.py`. Mismo contrato que `verify_audio.py`: **mide y avisa, no
reprueba**. Aquí además es obligado: si un corte salta o no es juicio editorial,
y ese juicio no lo firma un umbral.

El motor ya medía el sonido del export —y encontró siete de catorce con clipping
duro que nadie había visto, uno con 1,031,972 muestras a fondo de escala—. Pero
medir el sonido no es mirar la pieza. Un salto de imagen en un corte, un fundido
que se comió un fotograma, un subtítulo que quedó detrás de una capa: nada de
eso sale en un número, y todo eso viaja al entregable igual.

Genera una vista de ±1.5 s en cada frontera de corte, más el arranque, tres
medios y el cierre, y escribe `reports/export-<fecha>.md` con las rutas y qué
comprobar en cada una:

1. salto o flash de imagen en el corte;
2. pico de onda en la frontera — el clic que se coló pese al fundido;
3. subtítulo tapado por una capa superior;
4. continuidad de color entre los dos planos.

```bash
python3 bin/verify_export.py --root "$DISK"
python3 bin/verify_export.py --root "$DISK" --archivo "$EXPORT" \
    --proyecto "<proyecto>" --timeline "<nombre>" --config project_config.json
```

**De dónde salen las fronteras.** Con `--proyecto` y `--timeline`, de la
timeline de Resolve leída en frío (`lib/timeline_resolve.py`): son los cortes
que el editor DECIDIÓ. Sin ellos, de la detección de escena sobre el propio
export: son los cortes que se VEN. No es lo mismo y el reporte lo dice — un
corte solo-imagen sobre audio continuo se detecta igual, pero uno entre dos
planos casi idénticos no.

**La trampa de los bloques.** La duración de la timeline no es la duración del
corte. En Morsa la timeline medía 78:10 y sólo 24:56 eran contenido, en 7
bloques; lo entregado era el PRIMER bloque, 16:35.7. Confundirlos es un error de
61 minutos. Los tiempos de timeline se trasladan al export restando el inicio
del bloque, el bloque se elige con `--bloque` y cuál se usó queda escrito.

**No se miran dos puntos que dan la misma vista.** Dos fronteras a menos de una
ventana de distancia se fusionan, y cuando se pisan gana el corte sobre el punto
de muestreo: el corte es una decisión del editor; el medio es sólo un sitio
donde asomarse.

### El tope de tres pasadas

Corregir, volver a mirar y corregir otra vez converge o no converge. **Si a la
tercera sigue habiendo algo, no es un problema que se arregle mirando más: se le
dice al editor**, con la lista de lo que sigue mal y por qué no se corrigió. El
script se niega a una cuarta pasada (`--pasada 4` sale con error).

Y una cosa que el script no hace y el asistente sí: **mirar las vistas**.
Generarlas es poner las imágenes delante; el trabajo empieza después.

---

## Lo que se evaluó y NO se adoptó

Está aquí para que no vuelva a entrar por la puerta de atrás.

| Qué | Por qué no |
|---|---|
| **ElevenLabs Scribe** (video-use), **Whisper por Groq / OpenAI** (claude-video) | Material confidencial saliendo del disco. Ver `lecciones/lo-que-no-hacer.md`. La transcripción es `whisper.cpp` local y se queda local. |
| El anti-patrón *"running Whisper locally on CPU"* | Cierto en CPU, falso aquí: corre en Metal a ~14x tiempo real con `large-v3-turbo`. Antes de adoptar el anti-patrón de alguien, comprobar que su supuesto se cumple en esta máquina. |
| **yt-dlp como fuente de material** | El material de edición viene de discos. (yt-dlp sigue vigente en el pipeline de memoria-creativa, que es otro trabajo.) |
| **Remotion, HyperFrames, Manim** | Animaciones para piezas de producto. Fuera del documental. |
| **EDL propio, render y corte automático** (video-use) | El asistente NO monta. El corte lo firma el editor en Resolve. Adoptarlo rompería el principio rector. |
| `--detail token-burner` (claude-video) | Pensado para clips de diez minutos. Aquí el material son horas. |

Y lo que ya estaba, y estaba mejor: transcripción word-level cacheada, corte de
silencios con `silencedetect` y verificador independiente por RMS, subtítulos
con la regla del corte sincrónico, descripción visual con vision LLM local,
contact sheets, y el acta de conducta.

## Lo que sí conviene recordar de ellos

Dos ideas de método, no de código:

- **`video-use` evalúa su propia salida antes de enseñarla.** Esa es la
  disciplina que entró aquí como `verify_export.py`. Si no lo enseñarías, no lo
  enseñes.
- **`claude-video` escribe lo que cuesta cada cosa** —frames, resolución,
  tokens— en su propia documentación. Un presupuesto que no está escrito se
  vuelve a inventar en cada corrida.
