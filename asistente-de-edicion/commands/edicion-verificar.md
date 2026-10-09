---
description: Corre las puertas del acta de conducta sobre un proyecto (G0 a G3, o todas).
argument-hint: "[G0|G1|G2|G3|todas] [disco o proyecto]"
---

Corre `verify_asistente.py` sobre `$ARGUMENTS`. Si no se nombra puerta, corre
las cuatro en orden y para en la primera que falle.

| Puerta | Cuándo aplica |
|---|---|
| G0 | antes de tocar material |
| G1 | cada vez que una etapa termina |
| G2 | antes de entregarle algo al editor o a Resolve |
| G3 | al cerrar el proyecto |

Son veintitrés checkpoints y cada uno comprueba por evidencia física. Reporta
qué pasó y qué falló con el checkpoint por su identificador, no con un
resumen. Un checkpoint fallado no se interpreta ni se justifica: se dice cuál
es y qué evidencia falta.

Si quieres además las capas que verifican el código y los datos, corre
`lint_pipeline.py` y los `verify_*.py` que apliquen al sector en curso.
