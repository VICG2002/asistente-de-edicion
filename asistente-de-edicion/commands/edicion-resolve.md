---
description: Prepara el horneado de un proyecto y entrega la línea lista para la Consola de Resolve.
argument-hint: "[proyecto]"
---

Para `$ARGUMENTS`: verifica que el horneado esté al día y entrega la línea de
la Consola.

1. Comprueba que existe `~/cinema-assistant/resolve/asistente_<proyecto>.lua`
   y que su `<proyecto>_data.lua` no es más viejo que el manifest. Si lo es,
   vuelve a correr `export_lua_data.py` antes de entregar nada.
2. Pasa la puerta G2 de `verify_asistente.py`, que es la que va justo antes de
   entregarle algo al editor.
3. Entrega la línea.

Cómo se entrega, que ya falló una vez por hacerlo mal:

- La Consola de Resolve es **Lua**, no una terminal. `dofile` no existe en el
  modo Py3.
- La línea va **suelta**, sin `echo` delante, sin comillas envolventes y sin
  bloque de shell. Un bloque etiquetado como bash invita a pegarla en la
  terminal, y ahí da error.
- Recuérdale al editor que la Consola tiene que estar en modo Lua, y que el
  campo donde se pega es la franja delgada al fondo de la ventana, no el área
  de salida de arriba.
- Se abre con Workspace > Console. Al terminar, guardar con Command+S.

Después de que corra, no des por bueno lo que el script haya impreso. La API
de Resolve devuelve valores que no siempre significan que hizo lo que se le
pidió. Pide el resultado y compruébalo contra lo que tenía que quedar.
