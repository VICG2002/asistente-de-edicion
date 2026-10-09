-- ============================================================
--  lavas_al_corte.lua — el audio de los lavalieres, debajo del corte del editor
--
--  Consola de DaVinci Resolve (Lua):
--    LAVAS_DATA = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proy>_data.lua"
--    LAVAS_TIMELINE = "V.1.0"     -- opcional; por defecto, la timeline abierta
--    LAVAS_SOLO_PLAN = true       -- opcional; dice lo que haria sin tocar nada
--    dofile("<MOTOR>/resolve/lavas_al_corte.lua")
--
--  Otros globals opcionales:
--    LAVAS_COPIA     nombre de la copia (por defecto "<timeline> + lavas")
--    LAVAS_BIN       bin donde importar los WAV que falten, como ruta de bins
--                    ("Rodaje (asistente)/AUDIO"); por defecto el bin abierto
--    LAVAS_ORDEN_TX  orden de las pistas por TX, p. ej. {"izq", "drc"}
--
--  QUE HACE (Asistente, 2026-09-29: "ponle el audio de los lavas a lo que llevo
--  del corte")
--   1. DUPLICA la timeline del editor. La suya no se toca.
--   2. Por cada item de AUDIO DE CAMARA de la copia —habilitado y a 100 %— busca
--      los lavalieres de ese clip en el horneado (DATA.sync) y coloca el tramo
--      que le corresponde, en pistas nuevas AL FINAL: "LAVA izq", "LAVA drc".
--   3. Liga cada lavalier con su audio de camara y el video de este, si esta
--      version de Resolve tiene Timeline:SetClipsLinked.
--
--  POR QUE DESDE EL AUDIO DE CAMARA Y NO DESDE EL VIDEO. El editor ya decidio
--  donde suena cada clip. Un plano que uso sin sonido no lleva lavalier; un J-cut
--  lo lleva con el corte de su audio, no con el de su imagen.
--
--  CUANDO DOS CLIPS SE ENCIMAN unos cuadros (un item en A1 que empieza antes de
--  que acabe el de A2), sus lavalieres del mismo TX no caben en una pista: el de
--  la pista de camara mas alta baja a "LAVA izq 2", y el de A1 se queda arriba.
--  Es el ajedrezado de siempre en dialogos; recortar a uno de los dos seria
--  decidir por el editor que cuadros se oyen.
--
--  La convencion del offset es la del manifest: offset = inicio_audio -
--  inicio_video, asi que el segundo t del video cae en el t - offset del WAV.
--
--  Como lee AppendToTimeline el endFrame (inclusivo o exclusivo) se MIDE con el
--  primer tramo: cambio entre versiones de Resolve (ver `anexar`).
-- ============================================================

local function line() print(string.rep("=", 62)) end
local function head(t) print(""); line(); print("  " .. t); line() end

head("LAVALIERES AL CORTE — asistente de edicion")

-- Los globals de entrada se consumen y se borran. En el estado Lua de Resolve
-- (la Consola o fusion:Execute) sobreviven a la corrida: un LAVAS_SOLO_PLAN
-- olvidado volvio plan la corrida siguiente (2026-10-01).
local ARG = {data = LAVAS_DATA, timeline = LAVAS_TIMELINE, solo_plan = LAVAS_SOLO_PLAN,
             copia = LAVAS_COPIA, bin = LAVAS_BIN, orden_tx = LAVAS_ORDEN_TX}
LAVAS_DATA, LAVAS_TIMELINE, LAVAS_SOLO_PLAN = nil, nil, nil
LAVAS_COPIA, LAVAS_BIN, LAVAS_ORDEN_TX = nil, nil, nil

if not ARG.data or ARG.data == "" then
  print("Falta la ruta del horneado. En la Consola, antes del dofile:")
  print('  ARG.data = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proy>_data.lua"')
  return
end
local okD, DATA = pcall(dofile, ARG.data)
if not okD or type(DATA) ~= "table" or type(DATA.sync) ~= "table" then
  print("ERROR leyendo el horneado: " .. tostring(DATA))
  return
end

-- El disco de la Mac no distingue mayusculas y Resolve guarda la ruta con la
-- grafia con que se importo: "Video 02" y "VIDEO 02" son la misma carpeta.
local function bajo(s) return string.lower(s or "") end
local SYNC, AUDIOS = {}, {}
for k, v in pairs(DATA.sync) do SYNC[bajo(k)] = v end
for k, v in pairs(DATA.audios or {}) do AUDIOS[bajo(k)] = v end

local pm = resolve:GetProjectManager()
local proj = pm and pm:GetCurrentProject()
if not proj then print("ERROR: sin proyecto abierto.") return end
local mp = proj:GetMediaPool()
print("Proyecto: " .. proj:GetName())

local function timelinePorNombre(nombre)
  for i = 1, proj:GetTimelineCount() do
    local t = proj:GetTimelineByIndex(i)
    if t and t:GetName() == nombre then return t end
  end
  return nil
end

local origen = (ARG.timeline and timelinePorNombre(ARG.timeline)) or
               (not ARG.timeline and proj:GetCurrentTimeline()) or nil
if not origen then
  print("ERROR: no encuentro la timeline \"" .. tostring(ARG.timeline) .. "\".")
  return
end
local nombreOrigen = origen:GetName()
local nombreCopia = ARG.copia or (nombreOrigen .. " + lavas")
print("Timeline del editor: " .. nombreOrigen)

local EXACTOS = {{23.976, 24000 / 1001}, {29.97, 30000 / 1001}, {47.952, 48000 / 1001},
                 {59.94, 60000 / 1001}, {119.88, 120000 / 1001}}
local function fpsExacto(v)
  local f = tonumber(v) or 24
  for _, par in ipairs(EXACTOS) do
    if math.abs(f - par[1]) < 0.002 then return par[2] end
  end
  return f
end
local FPS = fpsExacto(origen:GetSetting("timelineFrameRate"))

local VIDEO_EXT = {mov = true, mp4 = true, m4v = true, mxf = true, avi = true, mts = true}
local function extension(fp) return bajo(string.match(fp or "", "%.([^./]+)$")) end
local function carpetaDe(fp) return string.match(fp or "", "([^/]+)/[^/]+$") or "" end
local function nombreDe(fp) return string.match(fp or "", "([^/]+)$") or (fp or "") end

-- ---------- 1. el plan -------------------------------------------------
-- Se calcula sobre la timeline que se va a tocar: la copia al aplicar, la del
-- editor al solo planear (son identicas recien duplicadas).
local function planDe(tl)
  local segs, saltados = {}, {}
  local nTracks = tl:GetTrackCount("audio")
  local lavasPrevios = 0
  for t = 1, nTracks do
    for _, it in ipairs(tl:GetItemListInTrack("audio", t) or {}) do
      local mpi = it:GetMediaPoolItem()
      local fp = mpi and mpi:GetClipProperty("File Path") or ""
      if AUDIOS[bajo(fp)] then lavasPrevios = lavasPrevios + 1 end
      if fp == "" or not VIDEO_EXT[extension(fp)] then goto sigue end
      do
        local nombre = it:GetName() or nombreDe(fp)
        local function saltar(motivo)
          saltados[#saltados + 1] = {nombre = nombre, pista = t, inicio = it:GetStart(), motivo = motivo}
        end
        local pares = SYNC[bajo(fp)]
        if it.GetClipEnabled and it:GetClipEnabled() == false then
          saltar("apagado en el corte")
          goto sigue
        end
        local sp = it.GetSpeed and it:GetSpeed() or nil
        local pct = (type(sp) == "table" and tonumber(sp.Percentage)) or 100
        if math.abs(pct - 100) > 0.01 then
          saltar(string.format("a %.0f %% de velocidad", pct))
          goto sigue
        end
        if not pares or #pares == 0 then
          saltar("sin lavalier (ningun TX grababa, o sin sync)")
          goto sigue
        end
        local fpsClip = fpsExacto(mpi:GetClipProperty("FPS"))
        if math.abs(fpsClip - FPS) > 0.001 then
          saltar(string.format("clip a %.3f fps en timeline a %.3f", fpsClip, FPS))
          goto sigue
        end
        local srcIn = it:GetLeftOffset(true) / fpsClip          -- s dentro del video
        local recIn = it:GetStart(true)                         -- frame de timeline
        local dur = it:GetDuration(true) / FPS                  -- s
        for _, p in ipairs(pares) do
          local a = AUDIOS[bajo(p.audiopath)]
          local adur = a and tonumber(a.dur)
          local lavIn = srcIn - (tonumber(p.offset) or 0)
          local lavOut = lavIn + dur
          if not adur then
            saltar("el horneado no trae la duracion de " .. nombreDe(p.audiopath))
          elseif lavOut <= 0 or lavIn >= adur then
            saltar(nombreDe(p.audiopath) .. " no grababa en este tramo")
          else
            local cab = math.max(0, -lavIn)
            local col = math.max(0, lavOut - adur)
            local segIn = lavIn + cab
            local sf = math.floor(segIn * FPS + 0.5)
            local rec = math.floor(recIn + cab * FPS + 0.5)
            local nfr = math.floor((dur - cab - col) * FPS + 0.5)
            if nfr >= 1 then
              local tx = (a.tx and a.tx ~= "") and a.tx or carpetaDe(p.audiopath)
              segs[#segs + 1] = {
                item = it, pista = t, nombre = nombre, tx = tx,
                audiopath = p.audiopath, lav = nombreDe(p.audiopath),
                offset = tonumber(p.offset) or 0, conf = tonumber(p.conf) or 0,
                rec = rec, sf = sf, nfr = nfr,
                -- lo que el redondeo a cuadro le quita al sync, en ms
                redondeo_ms = ((sf - segIn * FPS) - (rec - (recIn + cab * FPS))) / FPS * 1000,
                cab = cab, col = col,
              }
            end
          end
        end
      end
      ::sigue::
    end
  end

  -- ajedrezado por TX: cada tramo a la primera pista de su TX donde quepa.
  -- Se reparte primero lo de A1, luego A2...: asi los lavalieres del audio
  -- principal quedan siempre en "LAVA izq" y solo baja a "LAVA izq 2" el
  -- pedazo de A2/A3 que se encima.
  table.sort(segs, function(x, y)
    if x.pista ~= y.pista then return x.pista < y.pista end
    return x.rec < y.rec
  end)
  local ocupado, ordenTx = {}, {}          -- ocupado[tx][nivel] = {{ini, fin}, ...}
  for _, s in ipairs(segs) do
    local niveles = ocupado[s.tx]
    if not niveles then niveles = {}; ocupado[s.tx] = niveles; ordenTx[#ordenTx + 1] = s.tx end
    local fin = s.rec + s.nfr
    local nivel = 1
    while true do
      local libre = true
      for _, iv in ipairs(niveles[nivel] or {}) do
        if s.rec < iv[2] and iv[1] < fin then libre = false; break end
      end
      if libre then break end
      nivel = nivel + 1
    end
    niveles[nivel] = niveles[nivel] or {}
    table.insert(niveles[nivel], {s.rec, fin})
    s.nivel = nivel
  end
  table.sort(segs, function(x, y)
    if x.rec ~= y.rec then return x.rec < y.rec end
    return x.pista < y.pista
  end)
  if type(ARG.orden_tx) == "table" then
    local rango = {}
    for i, tx in ipairs(ARG.orden_tx) do rango[tx] = i end
    table.sort(ordenTx, function(x, y)
      return (rango[x] or 99) < (rango[y] or 99) or
             ((rango[x] or 99) == (rango[y] or 99) and x < y)
    end)
  else
    table.sort(ordenTx)
  end
  -- pistas nuevas: nivel 1 de cada TX, luego nivel 2...
  local pistas, idxDe = {}, {}
  local maxNivel = 0
  for _, niveles in pairs(ocupado) do maxNivel = math.max(maxNivel, #niveles) end
  for nivel = 1, maxNivel do
    for _, tx in ipairs(ordenTx) do
      if ocupado[tx][nivel] then
        local nom = "LAVA " .. tx .. (nivel > 1 and (" " .. nivel) or "")
        pistas[#pistas + 1] = nom
        idxDe[tx .. "#" .. nivel] = nTracks + #pistas
      end
    end
  end
  for _, s in ipairs(segs) do s.destino = idxDe[s.tx .. "#" .. s.nivel] end
  return segs, saltados, pistas, nTracks, lavasPrevios
end

local function tc(frame)
  local f = math.floor(frame + 0.5)
  local base = math.floor(FPS + 0.5)
  local h = math.floor(f / (base * 3600)); f = f - h * base * 3600
  local m = math.floor(f / (base * 60)); f = f - m * base * 60
  local s = math.floor(f / base); f = f - s * base
  return string.format("%02d:%02d:%02d:%02d", h, m, s, f)
end

local function imprimirPlan(segs, saltados, pistas, nTracks, lavasPrevios)
  head("Plan")
  print(string.format("  pistas de audio del editor: A1-A%d (no se tocan)", nTracks))
  for i, nom in ipairs(pistas) do
    print(string.format("  pista nueva A%d  %s", nTracks + i, nom))
  end
  if lavasPrevios > 0 then
    print(string.format("  AVISO: el corte ya tiene %d item(s) de lavalier puestos a mano; "
      .. "se agregan los del asistente igual, en sus pistas.", lavasPrevios))
  end
  print("")
  local maxRed = 0
  for _, s in ipairs(segs) do
    maxRed = math.max(maxRed, math.abs(s.redondeo_ms))
    print(string.format("  %s  A%d %-26s -> A%d %-12s %s desde %.3fs  %5.1fs%s",
      tc(s.rec), s.pista, s.nombre, s.destino or -1, "(" .. s.tx .. ")", s.lav,
      s.sf / FPS, s.nfr / FPS,
      (s.cab > 0 or s.col > 0) and string.format("  [el WAV no cubre %.1fs]", s.cab + s.col) or ""))
  end
  print("")
  print(string.format("  tramos de lavalier: %d   redondeo a cuadro: max %.1f ms", #segs, maxRed))
  if #saltados > 0 then
    print(string.format("  audios de camara sin lavalier: %d", #saltados))
    local motivos = {}
    for _, x in ipairs(saltados) do motivos[x.motivo] = (motivos[x.motivo] or 0) + 1 end
    for m, n in pairs(motivos) do print(string.format("    %3d  %s", n, m)) end
  end
end

-- ---------- 2. solo plan ------------------------------------------------
if ARG.solo_plan then
  local segs, saltados, pistas, nTracks, previos = planDe(origen)
  imprimirPlan(segs, saltados, pistas, nTracks, previos)
  print("")
  print("SOLO PLAN: no se toco nada. Para aplicarlo, quita LAVAS_SOLO_PLAN.")
  return
end

if timelinePorNombre(nombreCopia) then
  print("ABORTADO: ya existe la timeline \"" .. nombreCopia .. "\". No la piso: puede")
  print("tener trabajo del editor. Pasa LAVAS_COPIA con otro nombre.")
  return
end

-- ---------- 3. WAV en el Media Pool, con la duracion correcta -----------
-- Con dos TX, el mismo nombre de archivo se repite entre carpetas (00034 en izq
-- y en drc). Un Relink por nombre puede dejar un item apuntando a un archivo
-- con la duracion del otro; por eso se elige el item cuya duracion cuadra.
local wavs = {}
local function recolectar(folder)
  for _, c in ipairs(folder:GetClipList() or {}) do
    local fp = c:GetClipProperty("File Path") or ""
    if AUDIOS[bajo(fp)] then
      local lista = wavs[bajo(fp)] or {}
      lista[#lista + 1] = c
      wavs[bajo(fp)] = lista
    end
  end
  for _, s in ipairs(folder:GetSubFolderList() or {}) do recolectar(s) end
end
recolectar(mp:GetRootFolder())

-- En un WAV "Frames" viene vacio; la duracion sale del timecode de "Duration",
-- que cuenta con base entera (24 para 23.976).
local function duracionDe(c)
  local f = fpsExacto(c:GetClipProperty("FPS"))
  local fr = tonumber(c:GetClipProperty("Frames") or "")
  if fr then return fr / f end
  local h, m, s, q = string.match(c:GetClipProperty("Duration") or "",
                                  "(%d+)[:;](%d+)[:;](%d+)[:;](%d+)")
  if not h then return nil end
  local base = math.floor(f + 0.5)
  return (((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * base + tonumber(q)) / f
end

local function binDestino()
  if not ARG.bin or ARG.bin == "" then return mp:GetCurrentFolder() end
  local folder = mp:GetRootFolder()
  for parte in string.gmatch(ARG.bin, "[^/]+") do
    local hallado
    for _, s in ipairs(folder:GetSubFolderList() or {}) do
      if s:GetName() == parte then hallado = s; break end
    end
    folder = hallado or mp:AddSubFolder(folder, parte)
    if not folder then return mp:GetCurrentFolder() end
  end
  return folder
end

local importados = 0
local function wavPara(path)
  local a = AUDIOS[bajo(path)]
  local esperado = a and tonumber(a.dur)
  for _, c in ipairs(wavs[bajo(path)] or {}) do
    local d = duracionDe(c)
    if d and esperado and math.abs(d - esperado) < 1.0 then return c end
  end
  local previo = mp:GetCurrentFolder()
  mp:SetCurrentFolder(binDestino())
  local imp = mp:ImportMedia({path})
  mp:SetCurrentFolder(previo)
  local c = imp and imp[1]
  if c then
    importados = importados + 1
    local lista = wavs[bajo(path)] or {}
    lista[#lista + 1] = c
    wavs[bajo(path)] = lista
    local d = duracionDe(c)
    if d and esperado and math.abs(d - esperado) >= 1.0 then
      print(string.format("  AVISO: %s importado dura %.1fs y el manifest dice %.1fs",
        nombreDe(path), d, esperado))
    end
  end
  return c
end

-- ---------- 4. duplicar y colocar ----------------------------------------
head("Aplicando")
-- la copia nace en el bin abierto: se abre el de la timeline del editor
local function binDeTimeline(folder, nombre)
  for _, c in ipairs(folder:GetClipList() or {}) do
    if c:GetClipProperty("Type") == "Timeline" and c:GetName() == nombre then return folder end
  end
  for _, s in ipairs(folder:GetSubFolderList() or {}) do
    local b = binDeTimeline(s, nombre)
    if b then return b end
  end
  return nil
end
local binPrevio = mp:GetCurrentFolder()
local binOrigen = binDeTimeline(mp:GetRootFolder(), nombreOrigen)
if binOrigen then mp:SetCurrentFolder(binOrigen) end
local copia = origen:DuplicateTimeline(nombreCopia)
if binPrevio then mp:SetCurrentFolder(binPrevio) end
if not copia then print("ERROR: DuplicateTimeline no devolvio la copia.") return end
if not proj:SetCurrentTimeline(copia) then
  print("ERROR: no pude abrir la copia \"" .. nombreCopia .. "\".")
  return
end
print("Copia: " .. copia:GetName() .. "  (la timeline \"" .. nombreOrigen .. "\" no se toca)")

local segs, saltados, pistas, nTracks, previos = planDe(copia)
imprimirPlan(segs, saltados, pistas, nTracks, previos)

for i, nom in ipairs(pistas) do
  local antes = copia:GetTrackCount("audio")
  if not copia:AddTrack("audio", "mono") or copia:GetTrackCount("audio") ~= antes + 1 then
    print("ERROR: no pude agregar la pista " .. nom .. ". Me detengo sin colocar nada.")
    return
  end
  copia:SetTrackName("audio", nTracks + i, nom)
end

-- COMO LEE endFrame ESTA VERSION, se mide con el primer tramo. La doctrina lo
-- tenia por inclusivo (LIB.colocarAudioPartido, Free 21.0.2); en Studio 21.1.0.17
-- salio exclusivo (2026-10-01: los 53 tramos quedaron un cuadro cortos). En vez
-- de suponerlo: el primero se coloca, se mide, y si sobra o falta se rehace.
local EF_EXTRA = nil
local function anexar(wav, s)
  local extra = EF_EXTRA or 0
  local function poner(x)
    local res = mp:AppendToTimeline({{
      mediaPoolItem = wav, mediaType = 2, trackIndex = s.destino,
      recordFrame = s.rec, startFrame = s.sf, endFrame = s.sf + s.nfr + x,
    }})
    return res and res[1]
  end
  local it = poner(extra)
  if it and EF_EXTRA == nil then
    local d = it:GetDuration(true) - s.nfr
    if math.abs(d) >= 0.5 then
      extra = extra - math.floor(d + 0.5)
      copia:DeleteClips({it})
      it = poner(extra)
    end
    EF_EXTRA = extra
    print(string.format("  endFrame en esta version: %s", EF_EXTRA == 0 and "EXCLUSIVO"
      or (EF_EXTRA == -1 and "INCLUSIVO" or ("corrimiento " .. EF_EXTRA))))
  end
  return it
end

local colocados, fallos, errMax = 0, 0, 0
local informe = {}
local porItem = {}
for _, s in ipairs(segs) do
  local wav = wavPara(s.audiopath)
  local puesto
  if wav then puesto = anexar(wav, s) end
  -- ¿esta donde tenia que estar? (que devuelva item no basta)
  local okPista, dStart, dDur, dSrc = false, nil, nil, nil
  if puesto then
    local ti = puesto:GetTrackTypeAndIndex() or {}
    okPista = (ti[1] == "audio" and tonumber(ti[2]) == s.destino)
    dStart = puesto:GetStart(true) - s.rec
    dDur = puesto:GetDuration(true) - s.nfr
    dSrc = puesto:GetLeftOffset(true) - s.sf
  end
  local bien = puesto and okPista and math.abs(dStart) < 0.5 and math.abs(dDur) < 0.5
               and math.abs(dSrc) < 0.5
  if bien then
    colocados = colocados + 1
    errMax = math.max(errMax, math.abs(s.redondeo_ms))
    local lista = porItem[s.item] or {}
    lista[#lista + 1] = puesto
    porItem[s.item] = lista
  else
    fallos = fallos + 1
    print(string.format("  [lava-FAIL] %s -> A%d %s: %s", s.nombre, s.destino or -1, s.lav,
      not wav and "no hay WAV en el Media Pool ni se pudo importar"
      or not puesto and "AppendToTimeline no devolvio item"
      or string.format("quedo en otra parte (pista ok=%s, dStart=%.2f, dDur=%.2f, dSrc=%.2f)",
                       tostring(okPista), dStart or 0, dDur or 0, dSrc or 0)))
  end
  informe[#informe + 1] = string.format(
    '  {"clip": "%s", "pista_camara": %d, "pista_lava": %d, "tx": "%s", "lavalier": "%s", '
    .. '"record_frame": %d, "source_frame": %d, "frames": %d, "offset": %.3f, '
    .. '"redondeo_ms": %.2f, "ok": %s}',
    s.nombre, s.pista, s.destino or -1, s.tx, s.audiopath, s.rec, s.sf, s.nfr, s.offset,
    s.redondeo_ms, tostring(bien and true or false))
end

-- ---------- 5. ligar ----------------------------------------------------
local ligados, sinLink = 0, 0
local linkOk = type(copia.SetClipsLinked) == "function"
for item, lavas in pairs(porItem) do
  if linkOk then
    local grupo = {item}
    for _, l in ipairs(item:GetLinkedItems() or {}) do grupo[#grupo + 1] = l end
    for _, l in ipairs(lavas) do grupo[#grupo + 1] = l end
    local ok, res = pcall(function() return copia:SetClipsLinked(grupo, true) end)
    if ok and res ~= false then ligados = ligados + 1 else sinLink = sinLink + 1 end
  else
    sinLink = sinLink + 1
  end
end

-- ---------- 6. informe --------------------------------------------------
local rutaInf = string.gsub(ARG.data, "_data%.lua$", "_lavas_resultado.json")
do
  local f = io.open(rutaInf, "w")
  if f then
    f:write("{\n")
    f:write(string.format('"timeline_editor": "%s",\n"copia": "%s",\n', nombreOrigen, nombreCopia))
    f:write(string.format('"colocados": %d,\n"fallos": %d,\n"ligados": %d,\n', colocados, fallos, ligados))
    f:write('"tramos": [\n' .. table.concat(informe, ",\n") .. "\n]\n}\n")
    f:close()
  end
end

head("LISTO")
print(string.format("  copia                     : %s", nombreCopia))
print(string.format("  pistas nuevas             : %d (A%d-A%d)", #pistas, nTracks + 1, nTracks + #pistas))
print(string.format("  tramos de lavalier        : %d colocados, %d fallos", colocados, fallos))
print(string.format("  redondeo a cuadro         : max %.1f ms", errMax))
print(string.format("  WAV importados al pool    : %d", importados))
if linkOk then
  print(string.format("  clips ligados             : %d%s", ligados,
    sinLink > 0 and string.format("  (%d rechazados)", sinLink) or ""))
else
  print("  ligado                    : esta version no tiene SetClipsLinked;")
  print("                              seleccionar y Clip > Link Clips")
end
print(string.format("  sin lavalier              : %d audios de camara", #saltados))
print("  informe: " .. rutaInf)
