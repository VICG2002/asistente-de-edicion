---
description: Arranca el asistente de edición sobre un proyecto y deja el estado medido hasta la puerta G0.
argument-hint: "[disco o proyecto]"
---

Invoca el skill `asistente-de-edicion` y corre su protocolo de arranque
completo sobre `$ARGUMENTS`. Si no viene argumento, detecta el proyecto activo
por los discos montados y por la última corrida en `~/cinema-assistant/logs/`,
y di cuál elegiste antes de tocar nada.

El protocolo, en orden, sin saltarte pasos:

1. Si `~/cinema-assistant/bin/` no existe, para aquí e invoca el skill
   `instalar-motor-edicion`. Sin motor no hay nada que arrancar.
2. Lee `~/memoria-asistente-edicion/metodologia/pasos-a-seguir.md`.
3. Mide el estado real del proyecto: clips, transcripts y `audio_sync_pairs`
   en `<disco>/.cinema_assistant/manifest.sqlite`, más la última corrida.
   Si no hay `.cinema_assistant/`, el proyecto no pasó por el pipeline y ese
   es el dato, no un error.
4. `verify_coverage.py --report-only`. En proyecto plano pasa
   `--project-prefix ''`: con el default heredado el verificador mira cero
   material y da el proyecto por bueno.
5. `doctrina_novedades.py`. Lo que traiga es un aviso, no una orden: el editor
   decide qué incorpora.
6. Puerta G0 de `verify_asistente.py`.

Termina diciendo dos cosas y nada más: en qué paso del playbook quedó el
proyecto, y cuál es el siguiente trabajo. Si G0 falló, ese es el único
siguiente trabajo.
