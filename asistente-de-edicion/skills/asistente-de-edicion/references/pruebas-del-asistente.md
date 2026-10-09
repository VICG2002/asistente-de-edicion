# Pruebas del asistente -- la quinta capa de verificación

`tests/README.md` documenta cuatro capas: `bin/lint_pipeline.py` (el código
fuente), `tests/` (código + una base sintética), `lib/guards.py` (durante la
corrida) y `bin/verify_*.py` (la base real del proyecto). Las cuatro
verifican **el código y los datos**. Ninguna verifica **al asistente**: si
leyó la doctrina antes de tocar material, si corrió los pasos en orden, si
pasó los seis verificadores del §12b antes de decir "listo para Resolve", si
curó por comprensión o solo corrió el script, si reportó números honestos, si
no rehízo trabajo ya hecho.

Esta página es esa quinta capa. La herramienta es `bin/verify_asistente.py`
-- **el acta de conducta**. Un checkpoint por línea, cada uno con su caso
fundador (la lección real que lo motivó) y su forma de comprobación (qué lee
el script para decidir, nunca la palabra del asistente).

```bash
python3 bin/verify_asistente.py --root <disco> --project-prefix '' --listar
python3 bin/verify_asistente.py --root <disco> --project-prefix '' --puerta G0
python3 bin/verify_asistente.py --root <disco> --project-prefix '' --todas --acta
```

`--acta` escribe `<disco>/.cinema_assistant/reports/acta-<fecha>.md` y su
hermano `.json` -- son las garantías que se citan al cerrar un proyecto (§13).

## Principio de diseño: ninguna declaración sin correlato físico

Hay dos clases de checkpoint y no se mezclan.

- **De evidencia** -- el script los decide solo, leyendo manifest, logs,
  reports y horneados. El asistente no puede falsearlos.
- **De declaración** -- el asistente firma que hizo algo (p. ej. "curé el
  transcript por comprensión"). El acta **solo acepta la firma si existe el
  artefacto que la respalda**: un reporte generado con fecha posterior a los
  transcripts, filas con `curated_by='claude'`, un journal sin solapes, una
  nota escrita a mano en `acta-notas.md`.

Nunca un booleano pelado. Es la diferencia entre una garantía y una promesa
(lecciones/preferencias-del-usuario.md, "Lo que valora del asistente").

### `acta-notas.md`

Vive en `<disco>/.cinema_assistant/reports/acta-notas.md`. Lo escribe el
asistente A MANO cuando un checkpoint de declaración necesita justificar algo
que el script no puede decidir solo (un paso con trabajo cero que SÍ era
legítimo, un script corrido suelto por una razón real). Formato:

```
- <id_de_paso_o_checkpoint>: texto libre explicando por qué es legítimo.
```

Escribir la nota es la parte que vuelve la declaración física: sin el
archivo, el checkpoint falla aunque el razonamiento del asistente fuera
correcto. Eso es intencional.

## Veredictos

`PASA` / `FALLA` / `NO-APLICA`, siempre con motivo. Un checkpoint marcado
`obligatorio=False` es informativo: aparece en el acta pero no cuenta para el
exit code -- mismo patrón que `doctrina_novedades.py` (avisa, no bloquea).

---

## Puerta G0 -- Arranque (antes de tocar material)

### A1 — línea base tomada antes del primer paso de escritura

**Caso fundador:** §13 "cobertura del pipeline" -- sin saber de dónde partió
un proyecto, "7 syncs verificados" no dice si eso es todo el trabajo o una
fracción.

**Cómo se comprueba:** la primera corrida del acta en un proyecto escribe
`acta-baseline.json` con los conteos de las tablas clave. Si ya hay corridas
de `run_pipeline.py` registradas ANTES de que exista ese archivo, la línea
base se tomó tarde: FALLA.

### A2 — `--project-prefix` declarado y casa con material real

**Caso fundador:** lección 47 -- el default heredado `'ESCALANDO MEXICO/'` no
casaba con nada en Fantástico Cómics y el proyecto quedó a medias sin que
nadie se enterara (exit 0).

**Cómo se comprueba:** con prefix no vacío, cuenta clips bajo ese prefijo
-- cero clips es FALLA. Proyecto plano (`prefix=''`) siempre PASA.

### A3 — capacidades de la máquina conocidas y su consecuencia listada

**Caso fundador:** la identidad por cara llevaba meses sin correr porque el
orquestador invocaba `python3` pelado, sin `cv2`/`insightface` (razón de ser
de `bin/doctor.py`, 2026-08-05).

**Cómo se comprueba:** `lib/entorno.mapa_de_capacidades()`; toda capacidad
ausente debe tener consecuencia documentada en `bin/doctor.py:CONSECUENCIA`
-- si no la tiene, FALLA (el diccionario de consecuencias quedó desactualizado).

### A4 — novedades de doctrina revisadas _(informativo)_

**Caso fundador:** el plugin llegó a distribuir `errores-comunes` con 44
líneas menos que la doctrina viva -- justo las lecciones del fallo que
motivó la auditoría 2026-07-27.

**Cómo se comprueba:** corre `bin/doctrina_novedades.py --strict` y surface
el resultado. Es aviso, no compuerta -- el propio script lo documenta así
("informativo, no una compuerta") y el editor decide qué incorporar.

---

## Puerta G1 -- Entre etapas (cada vez que una etapa termina)

### B1 — ninguna etapa con trabajo cero sin motivo reconocido

**Caso fundador:** lecciones 40, 47, 50, 51 -- "0 procesados" con exit 0 no es
"no había nada que hacer", es un bug de selección hasta demostrar lo
contrario.

**Cómo se comprueba:** lee las corridas de `run_pipeline.py` de este
proyecto (sidecar `.json` o el `.log` de texto como respaldo). Todo paso en
estado `sin-trabajo` necesita una entrada en `acta-notas.md` con su mismo id
-- sin ella, FALLA.

### B2 — dependencias de tabla satisfechas antes de cada paso

**Caso fundador:** `curate_segments.py` NO llama `guards.exigir_tablas()` y
muere con `no such table` si faltan `clip_shot_values`, `clip_angles` o
`content_segments` -- el bake sale con `curated: 0` sin avisar de nada raro
(documentado en la memoria "rama-visual-orden-de-dependencias" y en
`pasos-a-seguir.md` §10/11).

**Cómo se comprueba:** si `clip_segments` tiene filas (la rama visual
arrancó) pero `clip_curated_segments` está vacía mientras alguna de sus
tres dependencias también lo está, FALLA -- es la firma exacta del fallo.

### B3 — nunca dos Whisper a la vez

**Caso fundador:** lección 43 -- comparten la GPU Metal y el más largo se
arrastra sin límite.

**Cómo se comprueba:** YA NO es solo doctrina en prosa. `lib/guards.lock_exclusivo()`
lo hace estructural -- `transcribe_clips.py` y `transcribe_audios.py` toman
un lock de fichero (`fcntl.flock` exclusivo, no bloqueante) antes de
transcribir; el segundo proceso aborta al instante (exit 5) en vez de
competir por la GPU. Cada toma/suelta queda en `logs/whisper-journal.jsonl`.
Este checkpoint lee ese journal y busca intervalos solapados entre pids
distintos -- si los hay, alguien transcribió sin pasar por el lock (código
viejo, bypass manual): FALLA.

### B4 — sin re-trabajo mudo

**Caso fundador:** directiva raíz 2 -- "1 procesar bien" gana a "10 procesar
de más"; antes de escalar, comprobar si se puede reusar un resultado
anterior.

**Cómo se comprueba:** compara corridas sucesivas de `run_pipeline.py`. Un
mismo paso con delta=0 dos veces (o más) y sin nota en `acta-notas.md` es
re-trabajo sin reconocer: FALLA.

### B5 — pasos sueltos fuera del orquestador, justificados _(informativo)_

**Caso fundador:** ESCALANDO MEXICO -- seis sectores quedaron sin sync ni
markers porque los pasos se corrieron a mano y era trivial olvidar uno.

**Cómo se comprueba:** MEJOR ESFUERZO, no exhaustivo (no todos los scripts
dejan log propio). Cada script deja su log bajo
`<disco>/.cinema_assistant/logs/`; si el timestamp de ese log cae fuera de
la ventana de cualquier corrida orquestada registrada y no hay nota en
`acta-notas.md`, es indicio de un paso corrido a mano sin documentar por qué.

---

## Puerta G2 -- Antes de que el editor vea nada (antes del bake y de Resolve)

### C1 — los seis verificadores del §12b en verde o NO-APLICA estructural

**Caso fundador:** Zezzions perdió dos entrevistas completas (clips 2794 y
2786) por no correr los verificadores antes de declarar el proyecto listo.

**Cómo se comprueba:** corre `verify_interviews.py --fail-on-found`,
`verify_coverage.py`, `lint_pipeline.py`, `verify_lav_offsets.py`,
`verify_multicam_placement.py` (NO-APLICA si no hay `*_multicam.lua`) y
`surface_transcript_suspects.py`. Cualquier exit distinto de cero es FALLA
-- "no dio tiempo" no es un motivo válido.

### C2 — curaduría por comprensión con correlato físico

**Caso fundador:** lección 46 -- el garble plausible que el audit
estadístico NO ve: "Satriacit Drive" por Satyajit Ray, "la plodería" por "la
curaduría". Eso solo lo caza la comprensión.

**Cómo se comprueba:** exige LOS TRES a la vez -- cero `question_short`
vacíos, un reporte `curaduria-transcript-*.md` con fecha POSTERIOR al
último transcript (si es anterior, la curaduría no vio el material más
reciente), y cuenta cuántos tramos llevan `curated_by='claude'` (§11b). Sin
el reporte y con preguntas sin curar: FALLA.

### C3 — invariantes SQL del §13 en cero

**Caso fundador:** el checklist de cierre del §13 -- tramos que exceden la
duración del clip, offsets de sync más grandes que el propio audio, rutas
que ya no existen.

**Cómo se comprueba:** las tres queries literales del §13, más una muestra
aleatoria de 5 paths verificados contra el disco.

### C4 — el bake es más nuevo que el último dato que lo alimenta

**Caso fundador:** §11b paso 4 -- curar tramos y olvidar re-hornear deja el
Green marker de Resolve mostrando texto viejo.

**Cómo se comprueba:** mtime de `*_data.lua` contra el mtime de
`manifest.sqlite` (proxy grueso pero honesto: no distingue QUÉ tabla
cambió, solo que algo se escribió después del bake).

### C5 — el Lua carga limpio

**Caso fundador:** "`luac -p` no caza globals nil" -- hace falta el smoke
con mock (`resolve/mock_resolve_smoke.lua`) para cazar errores de runtime
(renombres, métodos mal escritos).

**Cómo se comprueba:** `luac -p` sobre el `_data.lua` Y sobre
`asistente_<proyecto>.lua`, más una corrida real de
`lua mock_resolve_smoke.lua asistente_<proyecto>.lua`.

### C6 — que la API devolviera algo no significa que lo hiciera

**Caso fundador:** regla transversal v0.4.0. FCC 2026-07-11: un par
multicám triple-verificado se perdió en silencio por colisiones de
colocación en timelines empacadas.

**Cómo se comprueba:** DOS capas independientes, no una. `verify_multicam_placement.py`
simula la colocación fuera de Resolve (exit≠0 si un ángulo se perdería), Y
además se confirma que `asistente_<proyecto>.lua` trae el assert de cierre
`MC_PLACED`/`MC_FAILED` -- la defensa en profundidad que avisa en la
Consola aunque el verifier no haya corrido.

---

## Puerta G3 -- Cierre

### D1 — auto-review del §13 con números honestos

**Caso fundador:** cierre obligatorio 2026-05-25 -- "7 syncs verificados"
vale más que "45 dudosos" como si fueran lo mismo.

**Cómo se comprueba:** cobertura por etapa (videos, analizados, curados,
sync, preguntas) MÁS al menos un reporte escrito en `reports/`. Cobertura
sin ningún reporte es FALLA -- los números existen pero nadie los escribió
para el editor.

### D2 — auditoría exhaustiva del §12c resuelta ítem por ítem

**Caso fundador:** Zezzions 2026-05-26, instrucción literal del usuario:
"revises a TODO el material para que los duration markers tengan la
información más exacta posible".

**Cómo se comprueba:** el query ad-hoc exacto del §12c -- clips ≥30s con
audio, sin sync, sin categoría. Cualquier fila es FALLA: quedó material sin
mirar.

### D3 — lo aprendido escrito de vuelta en la doctrina _(informativo)_

**Caso fundador:** directiva raíz 0 -- lo aprendido en un proyecto vive en
el motor y en la doctrina, nunca en archivos ad-hoc; el próximo proyecto no
debe reinventarlo.

**Cómo se comprueba:** ¿alguna página de `~/memoria-asistente-edicion/` se
tocó desde que se tomó la línea base (A1)? Informativo porque puede ser
legítimo que una sesión no produjera ninguna lección nueva.

### D4 — puente a la bóveda de memoria creativa (opcional) _(informativo)_

**Caso fundador:** §13b -- entre junio y julio de 2026 se procesaron cinco
proyectos y nada llegó a la bóveda, porque el paso de vuelta no existía.

**Cómo se comprueba:** si hay bóveda en esta máquina, ¿existe una propuesta
para este proyecto en `_cambios/pendientes/`? Sin bóveda: NO-APLICA (§13b
es opcional por diseño).

---

## Invariantes transversales (se comprueban en cualquier puerta)

### I1 — ninguna fuente borrada, movida ni renombrada

**Caso fundador:** regla dura nº1 del colectivo -- el material rodado es
irreemplazable.

**Cómo se comprueba:** censa las rutas indexadas contra el disco. Proyectos
con más de 5000 archivos se muestrean (se anota como muestra, nunca en
silencio). Cualquier ruta indexada que ya no exista es FALLA inmediata.

### I2 — audios externos siempre después de los de cámara

**Caso fundador:** regla dura v0.2.0 -- `A1` cámara base, `A2` compañeras,
lavaliers al final. La verifica `verify_track_order.py` y falla el
pipeline si se rompe.

**Cómo se comprueba:** delega en `verify_track_order.py`. Si aún no existe
`*_layout.json` (se genera al construir las timelines DESDE la Consola de
Resolve, después del bake), es NO-APLICA -- no confundir con una violación
de la regla.

### I3 — sin duration markers en lo que se hornea

**Caso fundador:** política del usuario, FCC 2026-07-11 -- solo marcadores
de punto.

**Cómo se comprueba:** `LIB.DURACION_MARKER` debe valer `1` en
`asistente_lib.lua`, y todas las llamadas `AddMarker(...)` de
`engine/resolve/*.lua` deben pasar `1` o `LIB.DURACION_MARKER` como quinto
argumento (duración). El parseo cruza un nivel de paréntesis anidados
(`string.format(...)` dentro de la llamada) para no cortar la lista de
argumentos a la mitad.

### I4 — convención de offset única (`audio_start − video_start`)

**Caso fundador:** lección 6 -- signo del offset mal documentado, distintos
módulos con convenciones distintas.

**Cómo se comprueba:** delega en `verify_sync_physical.py` sobre los pares
reales del proyecto. NO-APLICA si el proyecto no tiene `audio_sync_pairs`
todavía.

---

## Qué hacer cuando un checkpoint falla

1. **Corregir lo corregible sin pedir permiso** (mismo principio que el
   §13 general) -- un bake desactualizado se re-hornea, un reporte que
   falta se genera.
2. **Lo que exige juicio editorial se escribe en `acta-notas.md`**, con el
   id del checkpoint y el motivo. Eso es lo que lo vuelve un correlato
   físico, no una promesa verbal.
3. **Lo que de verdad no aplica a este proyecto** ya sale como NO-APLICA --
   no hace falta nota.
4. Nunca declarar el proyecto cerrado con checkpoints obligatorios en FALLA
   sin haber hecho 1 o 2 primero.
