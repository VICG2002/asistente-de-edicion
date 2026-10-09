-- ============================================================
--  SPIKES v0.4.0 — asistente de edicion (Diez50)
--  Consola de DaVinci Resolve (modo Lua):
--    dofile("<MOTOR>/resolve/spikes_v2.lua")
--
--  Responde las preguntas de API que bloquean el desarrollo y que solo
--  se pueden contestar MIDIENDO en Resolve Free:
--    S1..S6 (v0.2.0) merge, markers, pistas.
--    S7/S8  (v0.3.0) linkear los angulos de multicam entre si.
--    S9..S11 (v0.4.0) el salto a Resolve 21.1:
--      S9  ¿el binding inventa stubs? Calibra como leer todo lo demas.
--      S10 que version y que edicion es esta realmente.
--      S11 cuales de las 20 APIs nuevas de 21.1 contestan en esta edicion.
--  Cada spike imprime VEREDICTO: <id> <SI|NO|?> — copiar el bloque
--  RESUMEN del final y pegarlo en la sesion.
--
--  SEGURIDAD
--   - S2..S6 son reversibles: markers que se borran solos y una timeline
--     de trabajo "ZZ SPIKE — borrar" que se elimina al terminar.
--   - S9..S11 son de lectura, salvo unos pocos que escriben en la timeline
--     desechable y se van con ella. Lo que toca el Media Pool esta detras de
--     PERMITIR_POOL, apagado por la misma razon que S1.
--   - S1 (AutoSyncAudio) NO es reversible por API: liga el WAV dentro del
--     clip del Media Pool y no hay forma de deshacerlo con script.
--     Por eso viene APAGADO. Para correrlo:
--       1. Duplica el proyecto en el Project Manager (clic derecho > Duplicate).
--       2. Abre la copia y renombrala con PRUEBA en el nombre.
--       3. Cambia PERMITIR_MERGE = true aqui abajo.
--     El spike se niega a correr si el proyecto no dice PRUEBA/COPIA/TEST.
-- ============================================================

local PERMITIR_MERGE = false
local PERMITIR_POOL  = false   -- S11: multicam y mapeo de audio escriben en el
                               -- Media Pool y no se deshacen por API.

-- ---------- infraestructura del reporte ----------------------------------
local RES = {}
local function veredicto(id, valor, detalle)
  RES[#RES+1] = {id = id, valor = valor, detalle = detalle or ""}
  print(string.format("  VEREDICTO: %-3s %-3s  %s", id, valor, detalle or ""))
end
local function line() print(string.rep("=", 62)) end
local function head(t) print(""); line(); print("  " .. t); line() end

head("SPIKES v0.4.0 — asistente de edicion (Resolve 21.1)")

-- ---------- contexto -----------------------------------------------------
local pm = resolve:GetProjectManager()
local proj = pm and pm:GetCurrentProject()
if not proj then print("ERROR: no hay proyecto abierto."); return end
local mp = proj:GetMediaPool()
local projName = proj:GetName()
print("Proyecto: " .. projName)
print("Resolve : " .. tostring(resolve.GetVersionString and resolve:GetVersionString() or "?"))
print("Producto: " .. tostring(resolve.GetProductName and resolve:GetProductName() or "?"))

-- Recorrer el Media Pool para conseguir material de prueba.
local VIDEO_EXT = {mov=true, mp4=true, m4v=true, mxf=true, avi=true, mts=true}
local AUDIO_EXT = {wav=true, aif=true, aiff=true, mp3=true, m4a=true}
local videos, audios = {}, {}
local function walk(folder)
  for _, c in ipairs(folder:GetClipList() or {}) do
    local ext = string.lower(string.match(c:GetName() or "", "%.([^.]+)$") or "")
    if VIDEO_EXT[ext] then videos[#videos+1] = c
    elseif AUDIO_EXT[ext] then audios[#audios+1] = c end
  end
  for _, s in ipairs(folder:GetSubFolderList() or {}) do walk(s) end
end
walk(mp:GetRootFolder())
print(string.format("Media Pool: %d videos, %d audios", #videos, #audios))
if #videos == 0 then
  print("ERROR: el Media Pool no tiene video. Abre un proyecto con material.")
  return
end
local V1 = videos[1]
local A1 = audios[1]
print("Video de prueba: " .. (V1:GetName() or "?"))
print("Audio de prueba: " .. (A1 and A1:GetName() or "(ninguno)"))

-- ============================================================
-- S2 — customData en markers (MediaPoolItem)
-- Habilita re-hornear sin borrar timelines: si podemos borrar por
-- customData, los markers del asistente se actualizan sin tocar los
-- que el editor puso a mano.
-- ============================================================
head("S2 — customData en markers")
do
  local CD = "spike:s2:001"
  V1:DeleteMarkerByCustomData(CD)  -- por si quedo de una corrida anterior
  local added = V1:AddMarker(5, "Blue", "SPIKE S2", "borrar", 1, CD)
  print("  AddMarker con customData -> " .. tostring(added))
  local got = V1:GetMarkerByCustomData(CD)
  local hayGet = (type(got) == "table") and next(got) ~= nil
  print("  GetMarkerByCustomData    -> " .. (hayGet and "encontrado" or "vacio"))
  local del = V1:DeleteMarkerByCustomData(CD)
  print("  DeleteMarkerByCustomData -> " .. tostring(del))
  local sobra = V1:GetMarkerByCustomData(CD)
  local limpio = not (type(sobra) == "table" and next(sobra) ~= nil)
  if added and hayGet and del and limpio then
    veredicto("S2", "SI", "re-horneado idempotente por customData es viable")
  else
    veredicto("S2", "NO", string.format("add=%s get=%s del=%s limpio=%s",
      tostring(added), tostring(hayGet), tostring(del), tostring(limpio)))
  end
end

-- ============================================================
-- S3 — markers en el MediaPoolItem persisten fuera de la timeline
-- Si funciona, los beats de entrevista sobreviven a reconstruir
-- timelines y se ven en el visor de origen.
-- ============================================================
head("S3 — markers en MediaPoolItem")
do
  local CD = "spike:s3:001"
  V1:DeleteMarkerByCustomData(CD)
  local added = V1:AddMarker(11, "Mint", "SPIKE S3", "persistencia", 24, CD)
  local marks = V1:GetMarkers()
  local n = 0
  if type(marks) == "table" then for _ in pairs(marks) do n = n + 1 end end
  print("  AddMarker en MediaPoolItem -> " .. tostring(added))
  print("  GetMarkers() del clip      -> " .. n .. " marker(s)")
  -- duracion: confirmar que el marker de duracion se respeta en el clip
  local dur = nil
  if type(marks) == "table" then
    for _, m in pairs(marks) do
      if m.customData == CD then dur = m.duration end
    end
  end
  print("  duracion respetada         -> " .. tostring(dur))
  V1:DeleteMarkerByCustomData(CD)
  if added and n > 0 then
    veredicto("S3", "SI", "beats pueden vivir en el clip, no solo en la timeline")
  else
    veredicto("S3", "NO", "los markers de MediaPoolItem no responden")
  end
end

-- ============================================================
-- S4 — colision de markers en el mismo frame
-- Con pregunta + respuesta + pausas + keywords las colisiones son
-- seguras. Necesitamos saber si Resolve rechaza el segundo marker
-- para dimensionar el asignador de frames.
-- ============================================================
head("S4 — dos markers en el mismo frame")
do
  V1:DeleteMarkerByCustomData("spike:s4:a")
  V1:DeleteMarkerByCustomData("spike:s4:b")
  local a = V1:AddMarker(31, "Sand", "SPIKE S4 A", "", 1, "spike:s4:a")
  local b = V1:AddMarker(31, "Lemon", "SPIKE S4 B", "", 1, "spike:s4:b")
  print("  primer marker  en frame 31 -> " .. tostring(a))
  print("  segundo marker en frame 31 -> " .. tostring(b))
  V1:DeleteMarkerByCustomData("spike:s4:a")
  V1:DeleteMarkerByCustomData("spike:s4:b")
  if a and not b then
    veredicto("S4", "SI", "colisiona: hace falta asignador de frames libres")
  elseif a and b then
    veredicto("S4", "NO", "admite dos en el mismo frame (revisar a ojo en la UI)")
  else
    veredicto("S4", "?", "el primer AddMarker fallo — revisar")
  end
end

-- ============================================================
-- S5 — AddTrack con index: inserta desplazando, o solo agrega al final?
-- DECIDE la Via A vs la Via B del plan: si inserta, el clip base
-- conserva su link video/audio y el layout queda
-- A1 camara base / A2 camara compañera / A3 lavalier.
-- ============================================================
head("S5 — AddTrack('audio', {index=2}) inserta o agrega?")
local TL_SPIKE = "ZZ SPIKE — borrar"
local tlSpike = nil
do
  -- timeline limpia de trabajo
  for i = 1, proj:GetTimelineCount() do
    local t = proj:GetTimelineByIndex(i)
    if t and t:GetName() == TL_SPIKE then mp:DeleteTimelines({t}); break end
  end
  tlSpike = mp:CreateEmptyTimeline(TL_SPIKE)
  if not tlSpike then
    veredicto("S5", "?", "no se pudo crear la timeline de trabajo")
  else
    proj:SetCurrentTimeline(tlSpike)
    while tlSpike:GetTrackCount("audio") < 2 do tlSpike:AddTrack("audio", "stereo") end
    tlSpike:SetTrackName("audio", 1, "SPIKE-A1")
    tlSpike:SetTrackName("audio", 2, "SPIKE-A2")
    local antes = tlSpike:GetTrackCount("audio")
    print(string.format("  antes: %d pistas  A1=%s  A2=%s", antes,
      tostring(tlSpike:GetTrackName("audio", 1)), tostring(tlSpike:GetTrackName("audio", 2))))

    local okIns = tlSpike:AddTrack("audio", {audioType = "stereo", index = 2})
    local despues = tlSpike:GetTrackCount("audio")
    local n1 = tostring(tlSpike:GetTrackName("audio", 1))
    local n2 = tostring(tlSpike:GetTrackName("audio", 2))
    local n3 = tostring(tlSpike:GetTrackName("audio", 3))
    print("  AddTrack{index=2} -> " .. tostring(okIns))
    print(string.format("  despues: %d pistas  A1=%s  A2=%s  A3=%s", despues, n1, n2, n3))

    -- Si INSERTO: la vieja A2 (SPIKE-A2) bajo a A3 y la nueva A2 es anonima.
    -- Si solo AGREGO al final: A2 sigue siendo SPIKE-A2 y la nueva es A3.
    if okIns and despues == antes + 1 and n3 == "SPIKE-A2" then
      veredicto("S5", "SI", "INSERTA y desplaza — usar Via A (clip base ligado)")
    elseif okIns and despues == antes + 1 then
      veredicto("S5", "NO", "solo agrega al final (A2 sigue siendo " .. n2 .. ") — usar Via B")
    else
      veredicto("S5", "?", "AddTrack con tabla de opciones no respondio como se espera")
    end
  end
end

-- ============================================================
-- S6 — como cae el audio de un clip al hacer AppendToTimeline
-- Necesario para saber si el audio de un clip mergeado ocupa dos
-- pistas consecutivas (camara, lavalier) o se mezcla en una.
-- ============================================================
head("S6 — reparto de pistas al hacer append")
do
  if not tlSpike then
    veredicto("S6", "?", "sin timeline de trabajo")
  else
    -- limpiar la timeline de trabajo y volver a crearla vacia
    for i = 1, proj:GetTimelineCount() do
      local t = proj:GetTimelineByIndex(i)
      if t and t:GetName() == TL_SPIKE then mp:DeleteTimelines({t}); break end
    end
    tlSpike = mp:CreateEmptyTimeline(TL_SPIKE)
    proj:SetCurrentTimeline(tlSpike)
    local res = mp:AppendToTimeline({V1})
    local nAud = tlSpike:GetTrackCount("audio")
    local ocupadas = 0
    for i = 1, nAud do
      local items = tlSpike:GetItemListInTrack("audio", i)
      if items and #items > 0 then ocupadas = ocupadas + 1 end
    end
    print(string.format("  append de 1 clip -> %d pistas de audio, %d con contenido",
      nAud, ocupadas))
    -- mapeo de audio del clip fuente (cuantas pistas trae de origen)
    local am = V1.GetAudioMapping and V1:GetAudioMapping() or nil
    if am then
      print("  GetAudioMapping() del clip:")
      print("  " .. tostring(am))
      veredicto("S6", "SI", string.format("%d pista(s) con contenido; GetAudioMapping disponible", ocupadas))
    else
      veredicto("S6", "?", "GetAudioMapping no disponible en este clip/version")
    end
  end
end

-- ============================================================
-- S1 — AutoSyncAudio en Resolve Free  (DESTRUCTIVO — apagado por defecto)
-- Es el riesgo numero 1 de toda la v0.2.0.
-- ============================================================
head("S1 — AutoSyncAudio (merge en el Media Pool)")
do
  -- Primero, lo no destructivo: existen las constantes y el metodo?
  -- Ojo: el binding de Resolve puede devolver un stub invocable para
  -- cualquier nombre, asi que la presencia NO prueba nada. La prueba real
  -- es la llamada con PERMITIR_MERGE = true.
  local hayMetodo = false
  do
    local okp, v = pcall(function() return mp.AutoSyncAudio end)
    hayMetodo = okp and v ~= nil
  end
  local K = {
    MODE     = resolve.AUDIO_SYNC_MODE,
    WAVEFORM = resolve.AUDIO_SYNC_WAVEFORM,
    TIMECODE = resolve.AUDIO_SYNC_TIMECODE,
    CHAN     = resolve.AUDIO_SYNC_CHANNEL_NUMBER,
    AUTO     = resolve.AUDIO_SYNC_CHANNEL_AUTOMATIC,
    RETAIN_A = resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO,
    RETAIN_M = resolve.AUDIO_SYNC_RETAIN_VIDEO_METADATA,
  }
  local faltan = {}
  for k, v in pairs({MODE=K.MODE, WAVEFORM=K.WAVEFORM, CHAN=K.CHAN,
                     AUTO=K.AUTO, RETAIN_A=K.RETAIN_A, RETAIN_M=K.RETAIN_M}) do
    if v == nil then faltan[#faltan+1] = k end
  end
  print("  mp.AutoSyncAudio es funcion -> " .. tostring(hayMetodo))
  print("  constantes faltantes        -> " .. (#faltan == 0 and "ninguna" or table.concat(faltan, ", ")))

  local esCopia = string.find(string.upper(projName), "PRUEBA", 1, true)
                or string.find(string.upper(projName), "COPIA", 1, true)
                or string.find(string.upper(projName), "TEST", 1, true)

  if not hayMetodo then
    veredicto("S1", "NO", "AutoSyncAudio no existe en esta build — Feature 2 cambia de forma")
  elseif not PERMITIR_MERGE then
    veredicto("S1", "?", "metodo y constantes presentes; falta la prueba real "
      .. "(duplicar proyecto y poner PERMITIR_MERGE = true)")
  elseif not esCopia then
    veredicto("S1", "?", "ABORTADO: el proyecto \"" .. projName
      .. "\" no dice PRUEBA/COPIA/TEST. El merge NO se deshace — duplica primero.")
  elseif not A1 then
    veredicto("S1", "?", "no hay ningun audio en el Media Pool para probar")
  else
    local antes = V1.GetAudioMapping and V1:GetAudioMapping() or "?"
    print("  mapping ANTES: " .. tostring(antes))
    local settings = {
      [K.MODE]     = K.WAVEFORM,
      [K.CHAN]     = K.AUTO,
      [K.RETAIN_A] = true,
      [K.RETAIN_M] = true,
    }
    local okSync = mp:AutoSyncAudio({V1, A1}, settings)
    print("  AutoSyncAudio -> " .. tostring(okSync))
    local despues = V1.GetAudioMapping and V1:GetAudioMapping() or "?"
    print("  mapping DESPUES: " .. tostring(despues))
    local ligo = (tostring(despues) ~= tostring(antes))
        and string.find(tostring(despues), "linked_audio", 1, true) ~= nil
    if okSync and ligo then
      veredicto("S1", "SI", "el merge en Media Pool funciona en esta build")
    else
      veredicto("S1", "NO", "AutoSyncAudio devolvio " .. tostring(okSync)
        .. " y el mapping no muestra linked_audio")
    end
  end
end

-- ============================================================
-- S7 / S8 — LINKEAR items de timeline entre si
--
-- Peticion permanente del usuario (2026-08-03): "los distintos angulos y el
-- audio linkealos a partir de ahora siempre, luego resulta enredoso querer
-- pasar algo pero que no se mueva con todo lo demas".
--
-- El audio con SU clip ya lo resuelve el merge del Media Pool (AutoSyncAudio,
-- S1): queda un solo item. Lo que falta es ligar el clip BASE con la companera
-- de otra camara, y eso es un link de TIMELINE. La API seria
-- Timeline:SetClipsLinked({items}, true), que NO esta en la lista de API medida
-- de Resolve Free — nadie ha comprobado que exista.
--
-- S7: ¿existe y acepta el grupo?
-- S8: ¿GetLinkedItems() lo refleja? (si no, no hay como verificarlo por script)
--
-- Reversible: usa la timeline de trabajo y no toca material.
-- ============================================================
head("S7 — Timeline:SetClipsLinked existe en Free?")
do
  for i = 1, proj:GetTimelineCount() do
    local t = proj:GetTimelineByIndex(i)
    if t and t:GetName() == TL_SPIKE then mp:DeleteTimelines({t}); break end
  end
  tlSpike = mp:CreateEmptyTimeline(TL_SPIKE)
  if not tlSpike then
    veredicto("S7", "?", "no se pudo crear la timeline de trabajo")
    veredicto("S8", "?", "depende de S7")
  else
    proj:SetCurrentTimeline(tlSpike)
    while tlSpike:GetTrackCount("video") < 2 do tlSpike:AddTrack("video") end
    -- dos items en pistas distintas, como quedan base y companera
    local r1 = mp:AppendToTimeline({{mediaPoolItem = V1, mediaType = 1,
                                     trackIndex = 1, recordFrame = 0,
                                     startFrame = 0, endFrame = 47}})
    local r2 = mp:AppendToTimeline({{mediaPoolItem = V1, mediaType = 1,
                                     trackIndex = 2, recordFrame = 0,
                                     startFrame = 0, endFrame = 47}})
    local a = r1 and r1[1]
    local b = r2 and r2[1]
    if not (a and b) then
      veredicto("S7", "?", "no se pudieron colocar dos items de prueba")
      veredicto("S8", "?", "depende de S7")
    elseif type(tlSpike.SetClipsLinked) ~= "function" then
      print("  Timeline:SetClipsLinked -> el metodo NO existe")
      veredicto("S7", "NO", "Free no expone SetClipsLinked: los angulos NO se "
        .. "pueden ligar por script (queda la via manual Clip > Link Clips)")
      veredicto("S8", "?", "depende de S7")
    else
      local ok, res = pcall(function() return tlSpike:SetClipsLinked({a, b}, true) end)
      print("  SetClipsLinked({a,b}, true) -> ok=" .. tostring(ok)
        .. " res=" .. tostring(res))
      if ok and res ~= false then
        veredicto("S7", "SI", "se pueden ligar angulo y base por script")
      else
        veredicto("S7", "NO", "el metodo existe pero rechazo el grupo: "
          .. tostring(res))
      end

      -- S8: ¿se puede LEER el link? Sin esto no hay forma de verificarlo.
      if type(a.GetLinkedItems) == "function" then
        local okG, linked = pcall(function() return a:GetLinkedItems() end)
        local n = 0
        if okG and type(linked) == "table" then for _ in pairs(linked) do n = n + 1 end end
        print("  a:GetLinkedItems() -> " .. n .. " item(s)")
        if n >= 2 then
          veredicto("S8", "SI", "el link se puede leer y verificar por script")
        else
          veredicto("S8", "NO", "GetLinkedItems no refleja el grupo (n=" .. n .. ")")
        end
      else
        veredicto("S8", "NO", "GetLinkedItems no existe: el link no es verificable "
          .. "por script, solo a ojo en la timeline")
      end
    end
  end
end

-- ============================================================
-- S9 — CALIBRACION DEL BINDING  (correr ANTES que S10/S11)
--
-- El propio S1 ya lo advertia: "el binding de Resolve puede devolver un stub
-- invocable para cualquier nombre, asi que la presencia NO prueba nada".
-- Hasta ahora esa advertencia era una sospecha. Aqui se mide.
--
-- Se pregunta por un metodo que con seguridad no existe. Lo que conteste
-- decide como se lee TODO lo que viene despues:
--   - si devuelve nil  -> la presencia SI significa algo
--   - si devuelve algo -> la presencia no significa nada y solo cuenta el efecto
-- ============================================================
head("S9 — el binding fabrica stubs?")
local BINDING_MIENTE = nil
do
  local FALSO = "MetodoQueNoExisteEnNingunaVersion20260911"
  local okp, v = pcall(function() return mp[FALSO] end)
  local presente = okp and v ~= nil
  print("  mp." .. FALSO)
  print("  presencia -> " .. tostring(presente) .. "  (tipo: " .. type(v) .. ")")
  local llamable = false
  if presente then
    local okc, r = pcall(function() return mp[FALSO]() end)
    llamable = okc
    print("  llamarlo  -> ok=" .. tostring(okc) .. " res=" .. tostring(r))
  end
  BINDING_MIENTE = presente
  if presente then
    veredicto("S9", "SI", "el binding inventa stubs: la PRESENCIA no prueba nada, "
      .. "solo cuenta el efecto medido")
  else
    veredicto("S9", "NO", "nil para lo inexistente: la presencia de un metodo es "
      .. "evidencia valida")
  end
end

-- ============================================================
-- S10 — IDENTIDAD Y EDICION  (21.1)
--
-- Resolve 21.1 movio el scripting de Python a Studio y metio un servidor MCP
-- que tambien es de Studio. Antes de creerle a nadie: que dice la propia app.
-- ============================================================
head("S10 — que version y que edicion es esta")
do
  local ver  = resolve.GetVersionString and resolve:GetVersionString() or "?"
  local prod = resolve.GetProductName  and resolve:GetProductName()  or "?"
  print("  GetVersionString -> " .. tostring(ver))
  print("  GetProductName   -> " .. tostring(prod))
  local esStudio = string.find(string.upper(tostring(prod)), "STUDIO", 1, true) ~= nil
  print("  se declara Studio -> " .. tostring(esStudio))
  -- Las utility functions nuevas de 21.1. Son lectura pura.
  local util = {"GetCurrentProject", "GetCurrentTimeline", "GetMediaPool", "GetGallery"}
  local vivas = 0
  for _, nombre in ipairs(util) do
    local okc, r = pcall(function() return resolve[nombre] and resolve[nombre](resolve) end)
    local viva = okc and r ~= nil and r ~= false
    if viva then vivas = vivas + 1 end
    print(string.format("  resolve:%-18s -> %s", nombre, viva and "responde" or "no"))
  end
  veredicto("S10", esStudio and "STUDIO" or "FREE",
    tostring(ver) .. " — utility 21.1: " .. vivas .. "/4 responden")
end

-- ============================================================
-- S11 — LAS 20 APIS NUEVAS DE 21.1: cuales contestan en esta edicion
--
-- El README de scripting que la propia 21.1 dejo en el disco dice que las APIs
-- "cover a common superset of functions for both the Free and Studio versions",
-- y que una llamada de Studio desde Free devuelve False en vez de no existir.
-- Nadie ha publicado cuales de las 20 nuevas caen de que lado. Esto lo mide.
--
-- Tres resultados posibles por metodo, y solo el tercero es una capacidad:
--   NO EXISTE   el binding no lo conoce
--   GATEADO     existe y devuelve false/nil (el muro de Studio)
--   RESPONDE    existe y devolvio algo util
--
-- Solo lectura y escritura sobre la timeline desechable. Lo que toca el Media
-- Pool va detras de PERMITIR_POOL, porque no se deshace.
-- ============================================================
head("S11 — las 20 APIs nuevas de 21.1")
local API_21_1 = {}
do
  local function probar(etiqueta, fn)
    local okc, r = pcall(fn)
    local estado, detalle
    if not okc then
      estado, detalle = "NO EXISTE", tostring(r):gsub("^.*:%s*", "")
    elseif r == nil or r == false then
      estado, detalle = "GATEADO", "devolvio " .. tostring(r)
    else
      estado = "RESPONDE"
      if type(r) == "table" then
        local n = 0; for _ in pairs(r) do n = n + 1 end
        detalle = n .. " entrada(s)"
      else
        detalle = tostring(r):sub(1, 48)
      end
    end
    API_21_1[#API_21_1+1] = {api = etiqueta, estado = estado, detalle = detalle}
    print(string.format("  %-34s %-10s %s", etiqueta, estado, detalle))
    return estado
  end

  -- timeline desechable propia para este bloque
  for i = 1, proj:GetTimelineCount() do
    local t = proj:GetTimelineByIndex(i)
    if t and t:GetName() == TL_SPIKE then mp:DeleteTimelines({t}); break end
  end
  tlSpike = mp:CreateEmptyTimeline(TL_SPIKE)
  local item = nil
  if tlSpike then
    proj:SetCurrentTimeline(tlSpike)
    local r = mp:AppendToTimeline({{mediaPoolItem = V1, mediaType = 1,
                                    trackIndex = 1, recordFrame = 0,
                                    startFrame = 0, endFrame = 71}})
    item = r and r[1]
  end

  print("  -- lectura pura --")
  probar("Project.GetAudioRenderCodecs",   function() return proj:GetAudioRenderCodecs() end)
  probar("Project.GetAudioRenderFormats",  function() return proj:GetAudioRenderFormats() end)
  probar("Resolve.GetKeyboardPresetList",  function() return resolve:GetKeyboardPresetList() end)
  probar("Project.GetPresetList",          function() return proj:GetPresetList() end)
  probar("Timeline.GetNormalizeAudioModes",function() return tlSpike:GetNormalizeAudioModes() end)
  probar("Timeline.GetOutputBlanking",     function() return tlSpike:GetOutputBlanking() end)
  probar("MediaStorage.GetCloneStatus",    function() return resolve:GetMediaStorage():GetCloneStatus() end)

  -- La joya para este pipeline: transcripcion con hablante y tiempo.
  -- Hoy eso lo hace Whisper por fuera. Si contesta en Free, cambia el metodo.
  print("  -- transcripcion (lo que mas importa aqui) --")
  local estTr = probar("MediaPoolItem.GetTranscription", function() return V1:GetTranscription() end)
  if estTr == "GATEADO" then
    print("     nota: puede estar gateado por Studio O porque el clip aun no se")
    print("     transcribio en Resolve. Transcribe uno a mano y vuelve a correr")
    print("     para distinguir un caso del otro.")
  end

  print("  -- escritura sobre la timeline desechable (reversible) --")
  if item then
    probar("TimelineItem.GetType",   function() return item:GetType() end)
    probar("TimelineItem.GetFades",  function() return item:GetFades() end)
    probar("TimelineItem.GetSpeed",  function() return item:GetSpeed() end)
    probar("TimelineItem.SetFades",  function() return item:SetFades({fadeIn = 12}) end)
    probar("Timeline.AutoAlignClips",function() return tlSpike:AutoAlignClips() end)
    probar("Timeline.NormalizeAudioLevel", function()
      return tlSpike:NormalizeAudioLevel({item}, -23.0, "ITU-R BS.1770-4", false) end)
    probar("TimelineItem.AddTransition", function() return item:AddTransition("start") end)
    probar("Timeline.SetOutputBlanking",  function() return tlSpike:SetOutputBlanking("1.85") end)
  else
    print("  (sin item en la timeline: los de escritura no se pudieron medir)")
  end

  print("  -- tocan el Media Pool: NO se deshacen --")
  if PERMITIR_POOL then
    probar("MediaPool.CreateMulticamClip", function()
      return mp:CreateMulticamClip({V1}, {startTimecode = "01:00:00:00"}) end)
    probar("TimelineItem.FlattenMulticam", function() return item:FlattenMulticam() end)
    probar("TimelineItem.PerformMulticamSmartSwitch", function()
      return item:PerformMulticamSmartSwitch(1, 0) end)
  else
    print("  CreateMulticamClip / FlattenMulticam / PerformMulticamSmartSwitch")
    print("  omitidos: crean items en el Media Pool y no se borran por API.")
    print("  Para medirlos: duplica el proyecto y pon PERMITIR_POOL = true.")
    for _, n in ipairs({"MediaPool.CreateMulticamClip", "TimelineItem.FlattenMulticam",
                        "TimelineItem.PerformMulticamSmartSwitch"}) do
      API_21_1[#API_21_1+1] = {api = n, estado = "SIN MEDIR", detalle = "PERMITIR_POOL = false"}
    end
  end
  print("  MediaPoolItem.SetAudioMapping omitido siempre: reescribe el mapeo del")
  print("  clip en el Media Pool y no hay forma honesta de devolverlo.")
  API_21_1[#API_21_1+1] = {api = "MediaPoolItem.SetAudioMapping", estado = "SIN MEDIR",
                            detalle = "destructivo sobre el clip"}

  local responden, gateados = 0, 0
  for _, r in ipairs(API_21_1) do
    if r.estado == "RESPONDE" then responden = responden + 1
    elseif r.estado == "GATEADO" then gateados = gateados + 1 end
  end
  veredicto("S11", responden > 0 and "SI" or "NO",
    string.format("%d responden, %d gateados, %d medidas en total",
      responden, gateados, #API_21_1))
end

-- ---------- limpieza -----------------------------------------------------
head("Limpieza")
do
  local borradas = 0
  for i = 1, proj:GetTimelineCount() do
    local t = proj:GetTimelineByIndex(i)
    if t and t:GetName() == TL_SPIKE then
      mp:DeleteTimelines({t}); borradas = borradas + 1; break
    end
  end
  print("  timelines de spike borradas: " .. borradas)
  for _, cd in ipairs({"spike:s2:001", "spike:s3:001", "spike:s4:a", "spike:s4:b"}) do
    V1:DeleteMarkerByCustomData(cd)
  end
  print("  markers de spike borrados")
  print("  NOTA: si corriste S1, el merge del clip \"" .. (V1:GetName() or "?")
    .. "\" NO se puede deshacer. Descarta ese proyecto de prueba.")
end

-- ---------- resumen ------------------------------------------------------
head("RESUMEN — copiar y pegar en la sesion")
print("Resolve : " .. tostring(resolve.GetVersionString and resolve:GetVersionString() or "?"))
print("Producto: " .. tostring(resolve.GetProductName and resolve:GetProductName() or "?"))
print("")
for _, r in ipairs(RES) do
  print(string.format("%s = %s   %s", r.id, r.valor, r.detalle))
end
if #API_21_1 > 0 then
  print("")
  print("APIs nuevas de 21.1 en esta edicion:")
  for _, r in ipairs(API_21_1) do
    print(string.format("  %-34s %-10s %s", r.api, r.estado, r.detalle))
  end
  print("")
  print("Leer asi: RESPONDE = capacidad real de esta edicion. GATEADO = existe")
  print("pero el muro de Studio la corta. NO EXISTE = el binding no la conoce.")
  if BINDING_MIENTE then
    print("OJO: S9 dio SI, el binding fabrica stubs. Ningun NO EXISTE de arriba")
    print("es concluyente por si solo — vale el efecto, no la presencia.")
  end
end
print("")

-- ---------- el reporte se escribe solo --------------------------------------
-- Hasta v0.3.0 habia que copiar el bloque de arriba y pegarlo en la sesion a
-- mano. La Consola puede escribir archivos, asi que ya no: el reporte queda en
-- disco y la sesion lo lee sola.
do
  local destino = os.getenv("HOME") .. "/cinema-assistant/logs/spike-21.1.txt"
  local f, err = io.open(destino, "w")
  if not f then
    print("No se pudo escribir el reporte (" .. tostring(err) .. ").")
    print("Copia el bloque RESUMEN de arriba y pegalo en la sesion.")
  else
    f:write("SPIKES v0.4.0 — reporte\n")
    f:write("fecha    : " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n")
    f:write("proyecto : " .. tostring(projName) .. "\n")
    f:write("resolve  : " .. tostring(resolve.GetVersionString and resolve:GetVersionString() or "?") .. "\n")
    f:write("producto : " .. tostring(resolve.GetProductName and resolve:GetProductName() or "?") .. "\n")
    f:write("flags    : PERMITIR_MERGE=" .. tostring(PERMITIR_MERGE)
            .. " PERMITIR_POOL=" .. tostring(PERMITIR_POOL) .. "\n\n")
    f:write("VEREDICTOS\n")
    for _, r in ipairs(RES) do
      f:write(string.format("  %-4s %-8s %s\n", r.id, r.valor, r.detalle))
    end
    if #API_21_1 > 0 then
      f:write("\nAPIS NUEVAS DE 21.1\n")
      for _, r in ipairs(API_21_1) do
        f:write(string.format("  %-36s %-10s %s\n", r.api, r.estado, r.detalle))
      end
    end
    f:close()
    print("Reporte escrito en:")
    print("  " .. destino)
    print("No hace falta copiar nada: la sesion lo lee de ahi.")
  end
end
print("")
