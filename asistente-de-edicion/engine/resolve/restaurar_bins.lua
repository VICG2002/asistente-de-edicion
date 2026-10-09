-- ============================================================
--  restaurar_bins.lua — deshace el movimiento de clips a A-ROLL / B-ROLL
--
--  Consola de DaVinci Resolve (modo Lua), en UNA linea (aqui va partida para
--  leerla; en la Consola se pega entera):
--    dofile("<MOTOR>/resolve/restaurar_bins.lua").correr({resolve = resolve,
--      lib = "<MOTOR>/resolve/asistente_lib.lua",
--      registro = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proj>_bins_origen.lua"})
--
--  Es un MODULO: cargarlo no hace nada y `correr(ctx)` hace el trabajo. Asi lo
--  llaman igual la Consola, el envoltorio de Studio (bin/aplicar_en_resolve.py)
--  y, en la Fase 1 del plan de Resolve Free, el aplicador del menu. Las rutas
--  llegan en ctx porque en el menu de Resolve 21.1 Free no hay `debug` con que
--  ubicarse solo, ni globals de Consola que leer (plan, §2). Hasta el
--  2026-10-05 se buscaba la libreria con debug.getinfo y el registro llegaba
--  en el global RESTAURAR_REGISTRO; en el menu de Free eso truena al cargar.
--
--  ctx:
--    resolve   el objeto de Resolve (en la Consola, `resolve`)
--    lib       ruta de asistente_lib.lua, o la tabla ya cargada
--    registro  ruta del <proj>_bins_origen.lua que escribio el asistente
--    decir     opcional: funcion para el reporte (por defecto print)
--  Devuelve {ok, restaurados, sin_clip, sin_origen, sin_bin, faltantes}; si
--  algo impide empezar, {ok = false, error = <por que>}.
--
--  Por que en Lua y no en Python: mover clips entre bins solo se puede con la
--  API de Resolve, y en Free eso solo corre dentro de Resolve.
--
--  Por que existe: la API solo tiene MoveClips — no hay alias ni copia, un clip
--  vive en UN bin. Mandarlos a A-ROLL/B-ROLL los saca de la organizacion manual
--  del editor. Este script los devuelve a donde estaban usando el registro que
--  se escribio ANTES de moverlos.
--
--  Si el bin de origen ya no existe, ese clip se reporta y se deja donde esta:
--  crear bins a ciegas para "restaurar" seria inventar una organizacion que el
--  editor no pidio.
-- ============================================================

local M = {}

function M.correr(ctx)
  ctx = ctx or {}
  -- print existe en el menu de Free aunque no se vea; si un host lo quita,
  -- el reporte se pierde pero el trabajo se hace y vuelve en el resultado.
  local decir = ctx.decir or print or function() end
  local function line() decir(string.rep("=", 56)) end
  local function falla(msg)
    decir(msg)
    return {ok = false, error = msg}
  end
  line(); decir("  RESTAURAR BINS — asistente de edicion"); line()

  if type(ctx.registro) ~= "string" or ctx.registro == "" then
    return falla("Falta ctx.registro, la ruta del <proj>_bins_origen.lua. "
                 .. "La linea completa esta en la cabecera de restaurar_bins.lua.")
  end
  local okReg, registros = pcall(dofile, ctx.registro)
  if not okReg or type(registros) ~= "table" then
    return falla("ERROR leyendo el registro: " .. tostring(registros))
  end
  decir("Registro: " .. ctx.registro .. "  (" .. #registros .. " clips)")

  -- La libreria llega ya cargada (el aplicador la tiene) o como ruta.
  local lib = ctx.lib
  if type(lib) == "string" then
    local okLib, cargada = pcall(dofile, lib)
    if not okLib then return falla("ERROR cargando asistente_lib.lua: " .. tostring(cargada)) end
    lib = cargada
  end
  if type(lib) ~= "table" or type(lib.mapaDeBins) ~= "function" then
    return falla("Falta ctx.lib, la ruta de asistente_lib.lua (o la tabla ya cargada).")
  end

  local r = ctx.resolve or resolve
  local pm = r and r:GetProjectManager()
  local proj = pm and pm:GetCurrentProject()
  if not proj then return falla("ERROR: sin proyecto abierto.") end
  local mp = proj:GetMediaPool()
  decir("Proyecto: " .. proj:GetName())

  -- Mapa ruta -> Folder y path de archivo -> MediaPoolItem
  local porRuta = lib.mapaDeBins(mp:GetRootFolder())
  local itemPorPath = {}
  local function recolectar(folder)
    for _, c in ipairs(folder:GetClipList() or {}) do
      local fp = c.GetClipProperty and c:GetClipProperty("File Path")
      if fp and fp ~= "" then itemPorPath[fp] = c end
    end
    for _, s in ipairs(folder:GetSubFolderList() or {}) do recolectar(s) end
  end
  recolectar(mp:GetRootFolder())

  -- Agrupar por bin de destino para hacer un MoveClips por bin
  local porOrigen = {}
  local sinOrigen, sinBin, sinClip = 0, 0, 0
  local faltantes = {}
  for _, reg in ipairs(registros) do
    local item = reg.path and itemPorPath[reg.path]
    if not item then
      sinClip = sinClip + 1
    elseif not reg.origen or reg.origen == "" then
      sinOrigen = sinOrigen + 1
    else
      local destino = porRuta[reg.origen]
      if not destino then
        sinBin = sinBin + 1
        faltantes[reg.origen] = (faltantes[reg.origen] or 0) + 1
      else
        porOrigen[reg.origen] = porOrigen[reg.origen] or {folder = destino, clips = {}}
        table.insert(porOrigen[reg.origen].clips, item)
      end
    end
  end

  local restaurados = 0
  for ruta, g in pairs(porOrigen) do
    if mp:MoveClips(g.clips, g.folder) then
      restaurados = restaurados + #g.clips
      decir(string.format("  %-46s %4d clips", ruta, #g.clips))
    else
      decir("  AVISO: MoveClips fallo para " .. ruta)
    end
  end

  decir("")
  line(); decir("  LISTO"); line()
  decir("  clips restaurados        : " .. restaurados)
  if sinClip > 0 then
    decir("  no encontrados en el pool: " .. sinClip ..
          "  (se borraron o se relinkearon)")
  end
  if sinOrigen > 0 then
    decir("  sin bin de origen anotado: " .. sinOrigen ..
          "  (quedan donde estan)")
  end
  if sinBin > 0 then
    decir("  con bin de origen ya inexistente: " .. sinBin)
    for ruta, n in pairs(faltantes) do
      decir("     " .. ruta .. "  (" .. n .. " clips)")
    end
    decir("  Esos clips se dejan donde estan: crear bins a ciegas seria")
    decir("  inventar una organizacion que no pediste.")
  end
  decir("")
  decir("Tip: comando+S para guardar el proyecto.")
  decir("")
  return {ok = true, restaurados = restaurados, sin_clip = sinClip,
          sin_origen = sinOrigen, sin_bin = sinBin, faltantes = faltantes}
end

return M
