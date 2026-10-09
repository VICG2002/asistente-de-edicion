-- ============================================================
--  reel_subtitulado.lua — timeline de un export con sus subtitulos.
--
--  Consola de DaVinci Resolve (modo Lua), en UNA linea (aqui va partida para
--  leerla; en la Consola se pega entera):
--    dofile("<MOTOR>/resolve/reel_subtitulado.lua").correr({resolve = resolve,
--      lib = "<MOTOR>/resolve/asistente_lib.lua",
--      video = "/ruta/al/Reel 001 v3.mov", hay_srt = true})
--
--  Opcionales en ctx:
--    srt     = "/ruta/al/subtitulo.srt"   -- por defecto: el .srt hermano
--    nombre  = "IMODAE — REEL 001"        -- por defecto: del nombre del archivo
--    pfx     = "IMODAE — "                -- prefijo si se deduce el nombre
--    hay_srt = true | false               -- si el .srt existe; lo comprueba
--                                            QUIEN LLAMA (ver abajo)
--    decir   = funcion para el reporte (por defecto print)
--  Devuelve {ok, timeline, items_v1, pista_st, srt, hay_srt}; si algo impide
--  terminar, {ok = false, error = <por que>}.
--
--  Es un MODULO: cargarlo no hace nada y `correr(ctx)` hace el trabajo, como
--  restaurar_bins.lua y resolve/diagnostico.lua. Hasta el 2026-10-05 leia los
--  globals REEL_*, buscaba la libreria con debug.getinfo y miraba si existian
--  el video y el SRT con io.open. En el menu de Resolve 21.1 Free no hay
--  `debug` ni `io` (plan del plugin de Resolve Free, §2): el script tronaba en
--  su primera linea. Ahora la ruta de la libreria la pasa quien llama, y que el
--  SRT exista lo dice quien llama, que si puede leer el disco (el asistente, o
--  el motor al escribir el pedido). Sin `hay_srt`, el reporte lo dice: "sin
--  comprobar". El video no hace falta comprobarlo: si no existe, ImportMedia
--  no devuelve nada y eso ya es el error.
--
--  QUE HACE Y HASTA DONDE LLEGA
--  Importa el export, arma su timeline con la resolucion del propio archivo
--  (un reel vertical en un proyecto 16:9 saldria con bandas si hereda los
--  ajustes del proyecto), abre la pista de subtitulos e INTENTA meter el SRT.
--
--  La API de Resolve NO tiene forma documentada de crear items de subtitulo:
--  AppendToTimeline conoce mediaType 1 (video) y 2 (audio), y para el subtitulo
--  no hay equivalente. Aqui se prueba de todos modos —las builds cambian— y si
--  no funciona se dice, con los tres clics exactos que faltan. Lo que NO se hace
--  es dar por puesto algo que no se puso: un subtitulo que no aparece y un
--  script que dice "listo" es la peor combinacion posible.
--
--  No toca las fuentes ni el resto del proyecto. Re-correrlo reconstruye SOLO
--  esta timeline.
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

  -- La libreria llega ya cargada (el aplicador la tiene) o como ruta.
  local LIB = ctx.lib
  if type(LIB) == "string" then
    local okLib, cargada = pcall(dofile, LIB)
    if not okLib then return falla("ERROR cargando asistente_lib.lua: " .. tostring(cargada)) end
    LIB = cargada
  end
  if type(LIB) ~= "table" or type(LIB.binHijo) ~= "function" then
    return falla("Falta ctx.lib, la ruta de asistente_lib.lua (o la tabla ya cargada).")
  end

  line(); decir("  REEL SUBTITULADO"); line()

  -- 1. Entradas -----------------------------------------------------------
  if type(ctx.video) ~= "string" or ctx.video == "" then
    return falla("Falta ctx.video, la ruta del export. "
                 .. "La linea completa esta en la cabecera de reel_subtitulado.lua.")
  end
  local video = ctx.video
  local srt = ctx.srt
  if not srt or srt == "" then
    srt = string.gsub(video, "%.[^.]+$", ".srt")
  end
  local base = string.match(video, "([^/]+)%.[^.]+$") or "REEL"
  local nombre = ctx.nombre
  if not nombre or nombre == "" then
    -- Se conserva el nombre del archivo tal cual, version incluida: con tres
    -- exports del mismo reel en la carpeta, saber CUAL esta en la timeline vale
    -- mas que un titulo bonito.
    nombre = (ctx.pfx or "") .. base
  end
  -- true: existe; false: no existe; nil: nadie lo comprobo.
  local haySrt = ctx.hay_srt

  decir("Video : " .. video)
  decir("SRT   : " .. srt .. ((haySrt == true and "")
                              or (haySrt == false and "   (NO EXISTE — sigo sin el)")
                              or "   (sin comprobar: este script no lee archivos)"))

  -- 2. Proyecto -----------------------------------------------------------
  local r = ctx.resolve or resolve
  local pm = r and r:GetProjectManager()
  local proj = pm and pm:GetCurrentProject()
  if not proj then return falla("ERROR: sin proyecto abierto.") end
  local mp = proj:GetMediaPool()
  decir("Proyecto: " .. proj:GetName())

  -- 3. Bin del asistente --------------------------------------------------
  local function findSub(folder, namelow)
    for _, s in ipairs(folder:GetSubFolderList() or {}) do
      if string.find(string.lower(s:GetName()), namelow, 1, true) then return s end
    end
    return nil
  end
  local raiz = mp:GetRootFolder()
  local tlBin = findSub(raiz, "timeline") or raiz
  local destBin = findSub(tlBin, "asistente de edici") or LIB.binHijo(mp, tlBin, "asistente de edicion")
  if destBin then mp:SetCurrentFolder(destBin) end

  -- 4. El export en el Media Pool (sin duplicarlo si ya esta) -------------
  local function buscarPorRuta(folder, ruta)
    for _, it in ipairs(folder:GetClipList() or {}) do
      local ok, p = pcall(function() return it:GetClipProperty("File Path") end)
      if ok and p == ruta then return it end
    end
    for _, s in ipairs(folder:GetSubFolderList() or {}) do
      local hit = buscarPorRuta(s, ruta)
      if hit then return hit end
    end
    return nil
  end
  local item = buscarPorRuta(raiz, video)
  if item then
    decir("El export ya estaba en el Media Pool.")
  else
    local imp = mp:ImportMedia({video})
    item = imp and imp[1]
    if not item then
      return falla("ERROR: no se pudo importar el export. ¿Existe la ruta? " .. video)
    end
    decir("Export importado al Media Pool.")
  end

  -- 5. Timeline limpia ----------------------------------------------------
  do
    local viejas = {}
    for i = 1, proj:GetTimelineCount() do
      local tl = proj:GetTimelineByIndex(i)
      if tl and tl:GetName() == nombre then viejas[#viejas+1] = tl end
    end
    if #viejas > 0 then
      mp:DeleteTimelines(viejas)
      decir("Timeline anterior borrada (se reconstruye): " .. nombre)
    end
  end
  local tl = mp:CreateEmptyTimeline(nombre)
  if not tl then return falla("ERROR creando la timeline " .. nombre) end
  proj:SetCurrentTimeline(tl)

  -- 6. Resolucion Y FRAME RATE del PROPIO archivo, no los del proyecto ----
  -- Un reel vertical (1080x1920) en un proyecto 16:9 hereda 1920x1080 y sale con
  -- bandas negras a los lados. Se fuerza a lo del export.
  --
  -- El frame rate faltaba hasta 2026-08-19: se fijaba la resolucion y el fps se
  -- heredaba del proyecto, asi que un export a 29.97 en un proyecto a 23.976
  -- quedaba con la timeline al fps equivocado. El propio script lo sabia —
  -- terminaba sugiriendo --fps a mano— y no lo arreglaba.
  --
  -- OJO: `timelineFrameRate` solo admite cambio con la timeline VACIA. Por eso
  -- este bloque va ANTES del AppendToTimeline y no despues.
  do
    local res = item:GetClipProperty("Resolution") or ""
    local w = tonumber(string.match(res, "^(%d+)"))
    local h = tonumber(string.match(res, "x(%d+)"))
    local fps = tonumber(item:GetClipProperty("FPS") or "")
    if w and h then
      pcall(function()
        tl:SetSetting("useCustomSettings", "1")
        tl:SetSetting("timelineResolutionWidth", tostring(w))
        tl:SetSetting("timelineResolutionHeight", tostring(h))
      end)
      -- La comprobacion no es "¿devolvio?" sino "¿quedo puesto?".
      local gw, gh = "", ""
      pcall(function()
        gw = tostring(tl:GetSetting("timelineResolutionWidth") or "")
        gh = tostring(tl:GetSetting("timelineResolutionHeight") or "")
      end)
      local bien = (gw == tostring(w) and gh == tostring(h))
      decir(string.format("Resolucion de la timeline: %dx%d%s", w, h,
                          bien and " (comprobada)" or
                          ("  <- QUEDO EN " .. gw .. "x" .. gh .. ", revisa a mano")))
    end
    if fps and fps > 0 then
      pcall(function() tl:SetSetting("timelineFrameRate", string.format("%.3f", fps)) end)
      local g = ""
      pcall(function() g = tostring(tl:GetSetting("timelineFrameRate") or "") end)
      local puesto = tonumber(g)
      local bien = puesto and math.abs(puesto - fps) < 0.01
      decir(string.format("Frame rate de la timeline: %.3f%s", fps,
                          bien and " (comprobado)" or
                          ("  <- QUEDO EN " .. g .. ", revisa a mano")))
    else
      decir("Frame rate del archivo: no lo dice el Media Pool — la timeline hereda "
            .. "el del proyecto. Comprueba a mano si el export no es del proyecto.")
    end
  end

  -- 7. El reel en V1 ------------------------------------------------------
  local puesto = mp:AppendToTimeline({item})
  if not puesto or #puesto == 0 then
    return falla("ERROR: el export no se coloco en la timeline.")
  end
  decir("Reel colocado en V1.")

  -- 8. Pista de subtitulos + intento de colocar el SRT --------------------
  local function nV1() return #(tl:GetItemListInTrack("video", 1) or {}) end

  -- La pista ST tiene que EXISTIR antes de importar: sin ella el File -> Import
  -- -> Subtitle de Resolve se ejecuta y no pasa nada, sin un mensaje de error.
  -- Se abre tambien cuando nadie comprobo el SRT: una pista vacia no estorba, y
  -- sin ella el import fallaria en silencio.
  --
  -- Y no basta con que AddTrack no reviente: `pcall` devuelve true tambien cuando
  -- la llamada devolvio false, que es la misma trampa de creer que "no dio error"
  -- equivale a "funciono". Se cuenta la pista despues, que es lo unico que
  -- demuestra algo.
  local pistaST = false
  if haySrt ~= false then
    local okAdd, res = pcall(function() return tl:AddTrack("subtitle") end)
    local n = 0
    pcall(function() n = tonumber(tl:GetTrackCount("subtitle")) or 0 end)
    pistaST = (n > 0) or (okAdd and res == true and n == 0)
    if n > 0 then
      decir("Pista de subtitulos: " .. n .. " (comprobada).")
    elseif okAdd and res == true then
      decir("Pista de subtitulos: creada (esta build no expone GetTrackCount).")
    else
      decir("Pista de subtitulos: NO se pudo crear por script.")
      pistaST = false
    end
  end

  -- POR QUE EL SRT NO SE PONE AQUI
  --
  -- No hay API de subtitulos en Resolve: ni Free ni Studio exponen la creacion de
  -- items en una pista ST. `AppendToTimeline` conoce mediaType 1 (video) y 2
  -- (audio) y nada mas.
  --
  -- La tentacion era probar mediaType=3 "por si acaso, que el coste es cero". El
  -- coste NO es cero: medido el 2026-08-07, la llamada DEVUELVE un item y lo
  -- coloca en V1 — o sea, el SRT entra como si fuera un clip de video, encima del
  -- reel, y el script se queda tan ancho anunciando "subtitulos colocados en ST1".
  -- Y `DeleteClips` no siempre esta para deshacerlo. Ensuciar la timeline del
  -- editor para intentar algo que no existe no vale la pena: se abre la pista, y
  -- los tres clics que faltan se dicen con todas las letras.

  -- 9. Cierre con la verdad -----------------------------------------------
  local function comoGenerar()
    decir("              python3 ~/cinema-assistant/bin/build_subtitles.py \\")
    decir('                --video "' .. video .. '" \\')
    decir("                --config <disco>/.cinema_assistant/project_config.json")
  end
  decir("")
  line()
  decir("  TIMELINE: " .. nombre)
  line()
  decir(string.format("  video   : %d item(s) en V1", nV1()))
  if haySrt == false then
    decir("  subs    : no hay .srt junto al export. Generalo con:")
    comoGenerar()
  else
    decir("  pista ST: " .. (pistaST and "abierta y lista" or "NO EXISTE"))
    if haySrt == nil then
      decir("  subs    : nadie comprobo que el .srt exista. Si no esta, generalo con:")
      comoGenerar()
    end
    decir("")
    decir("  El SRT NO lo pone el script: Resolve no expone API de subtitulos.")
    decir("  Con ESTA timeline abierta (no otra):")
    decir("")
    if not pistaST then
      decir("     0. Clic derecho en los encabezados de pistas ->")
      decir("        Add Subtitle Track.   SIN pista, el import no hace NADA")
      decir("        y Resolve no avisa.")
    end
    decir("     1. File -> Import -> Subtitle…")
    decir("     2. Elige:  " .. srt)
    decir("     3. Aterriza solo en ST1, alineado al frame 0.")
    decir("")
    decir("  El SRT esta cronometrado DESDE 0, asi que cuadra sin arrastrar nada.")
    decir("")
    decir("  Si aun asi no aparece nada: comprueba que el SRT tenga saltos CRLF")
    decir("  (Resolve rechaza los LF en silencio). Se regenera con:")
    decir("    python3 ~/cinema-assistant/bin/build_subtitles.py \\")
    decir('      --video "' .. video .. '" --fps 29.97')
  end
  decir("")
  decir("  Comando+S para guardar.")
  return {ok = true, timeline = nombre, items_v1 = nV1(), pista_st = pistaST,
          srt = srt, hay_srt = haySrt}
end

return M
