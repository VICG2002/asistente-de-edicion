# Montaje de reels — lo que se aprendió leyendo 13 montajes hechos

Doctrina nacida el **2026-08-27** de la ingeniería inversa de los 13 reels de CLIENTE_1
(3 días de rodaje, 105 insertos de B-roll). No es teoría de montaje: es lo que **este** editor hizo,
medido sobre su propia timeline, con el número de aciertos al lado de cada regla.

Sirve para dos cosas: leer un montaje ajeno, y —cuando las piezas que faltan estén— proponer uno.

## 0. El montaje se puede leer sin abrir Resolve

`lib/timeline_resolve.py` abre el `Project.db` y devuelve cada item con su fotograma de entrada,
duración, pista y ruta de origen. `bin/analizar_montaje.py` lo interpreta: separa las piezas
entregadas del material de trabajo y reparte los items en A-roll, B-roll, reencuadres y gráficos.

```bash
python3 bin/analizar_montaje.py --proyecto "<Proyecto>" \
    --timeline "<Timeline>::<carpeta de Exports>::<project_config.json>" \
    --out <disco>/.cinema_assistant/reports/corpus-montaje-<fecha>.json
```

### Identificar una pieza necesita DOS filtros, no uno

Una timeline de trabajo mezcla lo entregado con los selects. `Reels 11-13` dura 21.4 min y las tres
piezas suman 2.1.

1. **Duración contra el export**, ±0.15 s. Un umbral («todo bloque > 25 s») no sirve: hay selects de
   B-roll más largos que la pieza más corta.
2. **El patrón de cortes.** Dos piezas distintas pueden durar lo mismo — `Reel 001 v4` mide 40.33 s y
   `Reel 3 V003` 40.37 s. Se detectan los cortes visibles del export y se comparan con los que la
   timeline declara: el acierto casa 55–88 %, los rivales 12–20 %.

> **No comparar la IMAGEN del export contra la del clip fuente.** Se intentó y no discrimina: el
> reencuadre a 9:16 es distinto en cada inserto y hay corrección S-Log3→Rec709, así que el mismo
> plano correlaciona 0.80 en un inserto y 0.12 en el siguiente.

### El `In` de Resolve va en fotogramas de la FUENTE

No de la timeline. Comprobado por rango: un clip de 19.5 s tenía un item con `In=1067`, imposible a
los 29.97 fps de la timeline (585 fotogramas) y correcto a los 59.94 reales del clip. Importa cuando
la timeline y el material van a tasas distintas — el día 1 monta a 23.976 material de 29.97.

## 1. La anatomía de un reel

```
V3+  ── insertos de B-roll ──────────  7-8 por pieza, 2-3 s cada uno
V2   ── reencuadres (punch-in) ──────  la MISMA toma, más cerrada
V1   ── A-roll: UNA toma, troceada ──  los cortes de silencio
A1   ── su sonido, mismos cortes ────
A3   ── música, una pista entera ────
       └ cortinilla de cierre ───────  13 de 13 piezas
```

**Un reencuadre no es B-roll.** Un item en pista alta cuya fuente es la MISMA toma del A-roll es un
punch-in. En el reel 0011 hay 12 de ésos: contarlos como B-roll triplica la cuenta y arruina
cualquier medida.

**Una placa animada tampoco.** `Hook Apertura.mp4` no es selección de material, es diseño. Si el
clasificador sólo busca «cortinilla» y «título», la placa entra como B-roll y hace decir que la pieza
«abre con recurso en el segundo 0.0».

## 2. Dónde entra el B-roll — la medida que rompe la intuición

| | |
|---|---:|
| **Dentro de una frase** | **71 %** |
| En el arranque de una frase | 28 % |
| En un hueco entre frases | 1 % |

Y el **52 %** de los insertos cruza a la frase siguiente.

> **El B-roll no respeta la puntuación del habla: la atraviesa.** La intuición dice que se corta a
> imagen en las pausas. Lo medido dice lo contrario, y con una razón: un inserto que empieza y acaba
> con la frase la subraya; uno que entra a media frase y sale a media de la siguiente **cose** las
> dos. Es lo que impide que el reel se sienta troceado.

**Repetir un plano es la norma.** Las 11 piezas con B-roll vuelven al menos a un clip ya usado;
`Reels 0008` construye 6 insertos con 2 clips.

## 3. El plano ilustra la palabra

La correspondencia es literal y se repite en los tres reels del día 3: «Verde» → collar de cuentas
verdes; «Blanco / sofisticación, luz» → el vestido blanco entero; «perlas» → macro de perlas;
«metales, dorados» → macro de monedas; «tonos neutros» → plano blanco sobre blanco.

Dos corolarios:

1. **Una enumeración pide una ráfaga.** «piedras, perlas, metales, dorados» recibe tres insertos
   seguidos de 1.3, 2.1 y 3.9 s.
2. **El remate conceptual pide el plano más abierto.** Cuando el texto pasa de la cosa a la idea
   («Ya es vanguardia»), la imagen se va al general.

**Está descrito, no medido.** Convertirlo en algo ejecutable exige un vocabulario visual por clip, y
eso es justo lo que produce la rama visual del pipeline (§5).

## 4. Reglas puestas a prueba

| Regla | Acierto | Veredicto |
|---|---|---|
| La toma elegida es la **«completa» más tardía** del grupo, **sólo cámara principal** | **10/11 = 91 %** | **Sirve** |
| El clip de B-roll más largo es el que entra | 24–38 %, contra 24–34 % de azar | **Descartada** |
| El Plano Americano entra más que el General | 71 % vs 19 %, un solo día | Hipótesis |

**La restricción «sólo cámara principal» es la que hace funcionar la primera.** Sin ella, en un día
con segunda cámara agrupada como toma, la regla predice un clip de la Osmo y el acierto cae a 60 %.

**La duración del clip no predice nada.** En un día la mediana de los usados doblaba a la de los
descartados y parecía señal; usada como criterio acierta lo que el azar. *Una diferencia de medianas
no es un predictor* — hay que probarla como tal antes de escribirla.

## 5. Lo que bloquea llegar a proponer un montaje

**Sin la rama visual no se puede aprender qué plano elige el editor.** Los días 1 y 2 no tienen
`clip_curated_segments`, así que no hay `shot_value` con el que validar la única regla prometedora de
selección. Correrla es barato y desbloquea el resto:

```
analyze_clips → analyze_segments → derive_video_categories → derive_shot_value
  → derive_camera_angle → derive_content_segments → curate_segments
  → extract_segment_frames → enrich_curated_segments
```

## 6. Subtítulos: dos oraciones no comparten cue

De 39 subtítulos entregados, el editor corrigió dos, y son el mismo defecto con causas distintas:

- **Cambio de interlocutor** («Alice, jamás tradicional.» + «¿Quién dijo eso?»). Resemblyzer sitúa el
  mínimo de similitud de voz a **0.10 s** del corte que hizo el editor.
- **Misma voz** («…histórica de nuestro país.» + «México no se viste literal.»): el cierre de una
  frase pegado a la apertura de otra. La diarización **no** encuentra cambio aquí.

Por eso la regla se escribe sobre la **puntuación**, no sobre la voz: una basada sólo en diarización
habría arreglado uno de los dos. Con un guarda de **3 palabras mínimo por lado** — sin él parte
«Verde. Poder, naturaleza. Blanco.» en cues de una palabra que parpadean.

El umbral de 3 palabras está ajustado a cuatro casos reales. Es un punto de partida medido, no una
constante universal.
