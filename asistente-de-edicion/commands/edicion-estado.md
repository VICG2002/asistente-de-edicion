---
description: Reporta en qué punto del pipeline quedó un proyecto, sin escribir nada.
argument-hint: "[disco o proyecto]"
---

Lectura pura sobre `$ARGUMENTS`. No escribas, no derives, no hornees: este
comando existe para mirar.

Reporta:

- Qué hay en `<disco>/.cinema_assistant/manifest.sqlite`: clips indexados,
  transcritos, curados, y pares en `audio_sync_pairs`.
- `verify_coverage.py --report-only` por sector, con `--project-prefix ''` si
  el proyecto es plano.
- La última corrida en `logs/`, con su fecha.
- Qué horneados existen en `<disco>/.cinema_assistant/resolve/` y de cuándo
  son comparados con el manifest. Un horneado más viejo que el manifest está
  desactualizado y hay que decirlo.

Cierra con una línea: cuál es el siguiente paso del playbook, sin darlo.
