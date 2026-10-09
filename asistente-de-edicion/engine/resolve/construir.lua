-- ============================================================
--  construir.lua — la accion `construir` del aplicador (plan Free, Fase 1)
--
--  Arma en Resolve las timelines de un proyecto DOCUMENTAL desde su horneado
--  (<slug>_data.lua y <slug>_multicam.lua), con la logica del script por
--  proyecto mas reciente, asistente_asistente.lua (2026-10-05):
--
--    <pfx>A-ROLL           empacada, con las companeras de multicam, el audio
--                          de sync y los markers de entrevista
--    <pfx>B-ROLL           por hora real, con los lavalieres como espina
--                          dorsal (o empacada, si no hay de donde colgarla)
--    <pfx>AUDIOS EXTERNOS  cada WAV entero en la pista de su TX y los clips
--                          encima, en su hora
--    <pfx><subcarpeta>     una por subcarpeta, si hay mas de una
--
--  Con `dia` (AAAA-MM-DD) entra solo el material de ese dia y los nombres
--  llevan el sufijo (" 28-sep").
--
--  QUE CAMBIA RESPECTO DEL SCRIPT POR PROYECTO (2026-10-06)
--    - Nada de globals de Consola: todo llega en la accion del pedido.
--    - Nunca borra una timeline: la que ya existe se aparta renombrandola
--      (aplicar.lua, apartarAnterior). Antes se borraban todas las del PFX.
--    - Sin `print`, `io` ni `os`: lo que el script imprimia es una anotacion
--      del reporte, agrupada por tipo (un reporte con 300 lineas no se lee), y
--      el layout de pistas vuelve por el .drt, no por _layout.json.
--    - La hora de un clip sale de un ISO con zona (LIB.isoEpochConZona). Un
--      ISO sin zona no se adivina con la hora de la Mac: el clip queda sin
--      hora y se dice.
--    - Cada timeline construida lleva el sello del pedido en el frame 0 y se
--      registra para que `exportar` la saque a <recibos>/<clave>.drt.
--    - Los clips del horneado que no estan en el Media Pool se cuentan y se
--      nombran (decision de Victor, 2026-10-06: el aplicador no importa video;
--      los WAV de sync de los clips del dia si, como siempre).
--
--  REVISION DEL 2026-10-07
--    - El orden cronologico es total (con hora primero, luego sin hora): el
--      del script mezclaba hora y fecha escrita y podia tronar table.sort.
--    - La cobertura cuenta por clip, no por ruta; una timeline esperada sin
--      material se nombra en el reporte ("timeline sin material (...)").
--    - El pool se recorre una vez, con el indice del aplicador; la ruta de
--      cada item se pide una vez.
--    - El orden de las pistas LAVA viene del pedido (lavalier_tx del
--      project_config.json), y el B-ROLL nombra sus pistas: la regla dura se
--      comprueba tambien ahi.
--
--  Es un MODULO: cargarlo no hace nada. aplicar.lua lo carga de ctx.motor y
--  llama C.correr(E, accion, {A = aplicador, LIB = asistente_lib}).
--  Este archivo no usa io, os, require, debug ni print (regla 5 del lint).
--  Lua 5.1 / LuaJIT (el de Resolve) y 5.5 (el de las pruebas).
-- ============================================================

local C = {}

C.MESES = {"ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic"}
C.VIDEO_EXT = {mov = true, mp4 = true, m4v = true, mxf = true, avi = true, mts = true}
C.AUDIO_EXT = {wav = true, aif = true, aiff = true, mp3 = true, m4a = true}
C.NOMBRE = {aroll = "A-ROLL", broll = "B-ROLL", audios_externos = "AUDIOS EXTERNOS"}
C.MAX_EJEMPLOS = 5
-- Las notas que EXPLICAN por que un clip no esta en A-ROLL ni B-ROLL llevan
-- la lista entera (hasta este tope): el verificador las cruza por nombre con
-- lo que falta en el .drt, y con cinco ejemplos solo podia contar (revision
-- del 2026-10-07). Los textos son los que lee lib/regreso.py.
C.NOTA_FALTAN = "faltan en el pool"
C.NOTA_SIN_HORA = "clips sin hora utilizable (fuera del B-ROLL)"
C.EJEMPLOS_COMPLETOS = {[C.NOTA_FALTAN] = 60, [C.NOTA_SIN_HORA] = 60}

-- " 28-sep" para "2026-09-28"; "" sin dia; nil si el dia no se entiende.
function C.sufijoDe(dia)
  if dia == nil or dia == "" then return "" end
  local _, m, d = string.match(tostring(dia), "^(%d%d%d%d)-(%d%d)-(%d%d)$")
  if not m or not C.MESES[tonumber(m)] then return nil end
  return string.format(" %d-%s", tonumber(d), C.MESES[tonumber(m)])
end

-- ---------- anotaciones agrupadas ----------------------------------------
--
-- El script por proyecto imprimia una linea por clip que fallaba. En el
-- reporte cada anotacion es un marker: se agrupan por tipo, con cuantos y
-- unos ejemplos, y se emiten al final.

local function nota(K, tipo, nivel, ejemplo)
  local g = K.notas[tipo]
  if not g then
    g = {nivel = nivel, n = 0, ejemplos = {}}
    K.notas[tipo] = g
    K.ordenNotas[#K.ordenNotas + 1] = tipo
  end
  g.n = g.n + 1
  if ejemplo and #g.ejemplos < (C.EJEMPLOS_COMPLETOS[tipo] or C.MAX_EJEMPLOS) then
    g.ejemplos[#g.ejemplos + 1] = tostring(ejemplo)
  end
end

local function emitirNotas(K)
  for _, tipo in ipairs(K.ordenNotas) do
    local g = K.notas[tipo]
    local texto = tipo .. ": " .. g.n
    if #g.ejemplos > 0 then
      texto = texto .. " (" .. table.concat(g.ejemplos, "; ")
      if g.n > #g.ejemplos then texto = texto .. "; ..." end
      texto = texto .. ")"
    end
    K.E.anotar(g.nivel, texto)
  end
end

-- ---------- datos ----------------------------------------------------------

-- El horneado se carga con loadfile en un entorno vacio: es solo datos. Sin
-- `io` no se puede preguntar si el archivo existe; el mensaje de loadfile lo
-- dice ("cannot open"), y en /Volumes eso es casi siempre un disco sin montar.
local function cargar(K, ruta, que)
  local datos, err = K.U.cargarDatos(ruta)
  if datos then return datos end
  err = tostring(err)
  if string.find(err, "cannot open", 1, true) or string.find(err, "No such file", 1, true) then
    return nil, "E03 no se pudo abrir " .. que .. " (" .. tostring(ruta)
                .. "): disco sin montar o archivo movido"
  end
  return nil, "E04 " .. que .. " ilegible: " .. err
end

-- ---------- Media Pool -----------------------------------------------------

-- El pool recorrido UNA vez, con el indice del aplicador (A.util.indexarPool):
-- ruta -> clip para clipDe y la lista de clips con su bin. Se rehace fresco al
-- empezar, por si una accion anterior del pedido importo algo.
local function recorrerPool(K)
  K.E.porRuta, K.E.pool = nil, nil
  K.U.indexarPool(K.E)
  return K.E.pool or {}
end

local function esRepetido(binpath)
  return string.find(string.lower(binpath or ""), "repetido", 1, true) ~= nil
end

-- Los datos de un clip: por RUTA (clave unica) y, si falla, por NOMBRE.
-- Devuelve tambien la clave del horneado, para saber que clips faltan.
local function dataFor(K, fpath, name)
  local clips = K.DATA.clips or {}
  if fpath and fpath ~= "" and clips[fpath] then return clips[fpath], fpath end
  local p = K.DATA.byname and K.DATA.byname[name]
  if p and clips[p] then return clips[p], p end
  return {}, nil
end

-- Duracion de un clip de audio por el timecode de "Duration" ("Frames" viene
-- vacio en los WAV). Sirve para no quedarse con un item confundido por un
-- Relink por nombre (Asistente, 2026-09-28).
local function duracionAudio(c)
  local f = tonumber(c:GetClipProperty("FPS")) or 24
  if math.abs(f - 23.976) < 0.002 then f = 24000 / 1001 end
  local h, m, s, q = string.match(c:GetClipProperty("Duration") or "",
                                  "(%d+)[:;](%d+)[:;](%d+)[:;](%d+)")
  if not h then return nil end
  return (((tonumber(h) * 60 + tonumber(m)) * 60 + tonumber(s)) * math.floor(f + 0.5)
          + tonumber(q)) / f
end

local function cuadra(K, c, afp)
  local a = K.DATA.audios and K.DATA.audios[afp]
  local d = c and duracionAudio(c)
  return a and d and math.abs(d - (tonumber(a.dur) or -1e9)) < 1.0
end

local function inventario(K)
  local recs, ajenos, otroDia, repetidos = {}, {}, 0, 0
  local visto, presentes = {}, {}
  for _, e in ipairs(recorrerPool(K)) do
    local name = tostring(e.clip:GetName() or "")
    local ext = string.lower(string.match(name, "%.([^.]+)$") or "")
    if C.AUDIO_EXT[ext] then
      local afp = e.fpath
      if afp then
        if not cuadra(K, K.audioByPath[afp], afp) then K.audioByPath[afp] = e.clip end
        local base = string.match(afp, "([^/]+)$") or name
        local padre = string.match(afp, "([^/]+)/[^/]+$") or ""
        K.audioByName[padre .. "/" .. base] = e.clip
        K.audioByName[base] = e.clip
      end
    elseif C.VIDEO_EXT[ext] then
      if esRepetido(e.binpath) then
        repetidos = repetidos + 1
      else
        local fpath = e.fpath
        local key = fpath or name
        if not visto[key] then
          visto[key] = true
          local info, clave = dataFor(K, fpath, name)
          if K.LIB.esAjeno(info) then
            ajenos[#ajenos + 1] = name
          elseif not K.esDelDia(info.dia) then
            otroDia = otroDia + 1
          else
            presentes[clave] = true
            recs[#recs + 1] = {clip = e.clip, name = name, fpath = fpath,
                               loc = string.match(info.rel_path or "", "^([^/]+)/")
                                     or "(sin grupo)",
                               info = info}
          end
        end
      end
    end
  end

  -- clipDe (aplicar.lua) busca en E.porRuta, que ya tiene el pool ENTERO;
  -- para los WAV manda la eleccion de `cuadra`.
  for afp, c in pairs(K.audioByPath) do K.E.porRuta[afp] = c end
  for _, r in ipairs(recs) do
    if r.fpath then K.videoByPath[r.fpath] = r.clip end
  end

  -- Lo que el horneado tiene y el pool no: no se importa (lo importa el
  -- editor), pero se dice con nombre. El verificador lo cruza con el .drt.
  local faltan = {}
  for ruta, info in pairs(K.DATA.clips or {}) do
    if K.esDelDia(info.dia) and not presentes[ruta] then
      faltan[#faltan + 1] = tostring(info.name or ruta)
    end
  end
  table.sort(faltan)
  for _, n in ipairs(faltan) do nota(K, C.NOTA_FALTAN, "aviso", n) end
  K.faltan = #faltan
  for _, n in ipairs(ajenos) do
    nota(K, "videos del pool que no son de este proyecto (ignorados)", "info", n)
  end
  if otroDia > 0 then
    K.E.anotar("info", otroDia .. " clip(s) de otro dia: van en las timelines de su dia")
  end
  if repetidos > 0 then
    K.E.anotar("info", repetidos .. " video(s) en un bin 'repetido': no se usan")
  end
  return recs
end

-- El WAV de esa ruta: el del pool o, si falta, importado (una sola vez).
local function wavDe(K, ruta)
  if not ruta or ruta == "" then return nil end
  local c = K.audioByPath[ruta]
  if c then return c end
  c = K.U.clipDe(K.E, ruta)
  if c then
    K.audioByPath[ruta] = c
    K.importados = K.importados + 1
  else
    nota(K, "WAV que no se pudieron importar", "aviso", string.match(ruta, "([^/]+)$") or ruta)
  end
  return c
end

-- ---------- multicam y hora real -------------------------------------------

local function prepararMulticam(K)
  local MC = K.MC or {}
  local pares = MC["pairs"] or MC     -- v3 trae {skews, base_group, pairs}
  if type(pares) ~= "table" then pares = {} end
  K.skews = type(MC.skews) == "table" and MC.skews or {}
  K.base = (type(MC.base_group) == "string" and MC.base_group ~= "") and MC.base_group or ""
  -- QUE PARES SE COLOCAN: los que el export marca con `place` (contenido o
  -- reloj dentro de una ventana de show); un multicam viejo sin `place` cae
  -- a `verified`. Ver asistente_asistente.lua para la historia de la regla.
  local nVer, nReloj, nSkip, nApagado = 0, 0, 0, 0
  for _, p in ipairs(pares) do
    local colocar
    if p.place ~= nil then colocar = p.place else colocar = p.verified end
    if not colocar then
      nSkip = nSkip + 1
    elseif not K.conMulticam then
      nApagado = nApagado + 1
    else
      if p.verified then nVer = nVer + 1 else nReloj = nReloj + 1 end
      p.key = nVer + nReloj
      K.companionsOf[p.a] = K.companionsOf[p.a] or {}
      table.insert(K.companionsOf[p.a], p)
    end
  end
  K.nPares = nVer + nReloj
  if K.nPares + nSkip > 0 then
    K.E.anotar("info", string.format("multicam: %d por contenido + %d por reloj-show se "
      .. "colocan; %d fuera de ventana no", nVer, nReloj, nSkip))
  end
  if nApagado > 0 then
    K.E.anotar("info", nApagado .. " par(es) de multicam no se colocan: el pedido "
      .. "apaga el multicam")
  end

  -- PISTA FIJA POR CAMARA, en el orden de `track_order` (cantidad de
  -- material, regla del usuario 2026-08-03). Alfabetico solo para bakes viejos.
  local declarado = MC.track_order
  if type(declarado) == "table" and #declarado > 0 then
    for _, g in ipairs(declarado) do
      if g ~= "" and g ~= K.base then K.ordenCam[#K.ordenCam + 1] = g end
    end
  else
    local vistos = {}
    for _, p in ipairs(pares) do
      local g = K.grupoDe(p.b)
      if g ~= "" and g ~= K.base and not vistos[g] then
        vistos[g] = true
        K.ordenCam[#K.ordenCam + 1] = g
      end
    end
    table.sort(K.ordenCam)
    if #K.ordenCam > 0 then
      K.E.anotar("aviso", "el multicam no trae track_order: orden alfabetico (re-hornea "
        .. "con export_multicam_lua.py)")
    end
  end
  for i, g in ipairs(K.ordenCam) do K.pistaCam[g] = i + 1 end
end

local function grupoDe(K, fpath)
  if not fpath then return "" end
  for k in pairs(K.skews) do
    if string.find(fpath, "/" .. k .. "/", 1, true) then return k end
  end
  return ""
end

-- El skew de la camara del clip. Una camara sin skew medido se ordena con su
-- reloj crudo, y se dice (Morsa 2026-08-03: la a6700 de Iban, una hora atras).
local function skewFor(K, info, fpath)
  local s = K.skews[info.folder or ""]
  if s then return s end
  if fpath then
    for k, v in pairs(K.skews) do
      if string.find(fpath, "/" .. k .. "/", 1, true) then return v end
    end
  end
  local g = info.folder or "?"
  if g ~= "?" then K.sinSkew[g] = true end
  return 0
end

local function realTime(K, info, fpath)
  local ep, porque = K.LIB.isoEpochConZona(info.created)
  if not ep then
    if porque == "sin zona" then nota(K, "clips con hora sin zona (sin hora)", "aviso", info.name) end
    return nil
  end
  return ep - skewFor(K, info, fpath)
end

-- La hora real de un clip, con la mejor evidencia que haya: el par de sync
-- medido y, si no, el reloj de su camara menos su skew. nil si no hay ni una
-- cosa ni la otra: inventarle hora es peor que dejarlo fuera y decirlo.
-- offset = audio_start - video_start  =>  video_start = wav_start - offset
local function horaDe(K, r)
  local plist = r.fpath and K.DATA.sync and K.DATA.sync[r.fpath]
  if plist and plist[1] then
    local ai = K.DATA.audios and K.DATA.audios[plist[1].audiopath]
    if ai and tonumber(ai.epoch_start) then
      return tonumber(ai.epoch_start) - (plist[1].offset or 0), "sync"
    end
  end
  if r.t then return r.t, "reloj" end
  return nil, nil
end

-- Orden cronologico TOTAL: primero los que tienen hora, por hora; despues los
-- que no, por la fecha escrita. El script por proyecto mezclaba las dos claves
-- en una comparacion (hora si las dos la tenian, si no la cadena `created`):
-- con skews grandes eso no es transitivo (a<c por hora, c<b y b<a por fecha) y
-- table.sort puede tronar con "invalid order function" (revision del
-- 2026-10-07). Desempata la fecha escrita y luego el nombre.
local function creado(r)
  local c = r.info and r.info.created
  return (c and c ~= "") and c or "9999"
end

function C.chronoSort(list)
  table.sort(list, function(a, b)
    local ta, tb = a.t, b.t
    if (ta ~= nil) ~= (tb ~= nil) then return ta ~= nil end
    if ta and ta ~= tb then return ta < tb end
    local ca, cb = creado(a), creado(b)
    if ca ~= cb then return ca < cb end
    return tostring(a.name) < tostring(b.name)
  end)
end
local chronoSort = C.chronoSort

-- ---------- timelines ------------------------------------------------------

local function tlFps(tl)
  local v = tonumber(tl:GetSetting("timelineFrameRate"))
  if v and v > 0 then return v end
  return 24.0
end

-- Una timeline nueva con ese nombre. La que ya exista se aparta renombrada
-- (nunca se borra); si no se puede apartar o crear, se anota y se sigue.
local function nuevaTimeline(K, nombre)
  nombre = K.U.nombreSeguro(nombre)
  local ok, destino = pcall(K.U.apartarAnterior, K.E, nombre, K.A.CD_SELLO)
  if not ok then
    nota(K, "timelines que no se pudieron apartar (no se reconstruyen)", "error", destino)
    return nil
  end
  local tl, final = K.U.crearTimeline(K.E, destino)
  if not tl then
    nota(K, "timelines que Resolve no creo", "error", destino)
    return nil
  end
  K.proj:SetCurrentTimeline(tl)
  return tl, final
end

-- El sello va AL FINAL: en una timeline vacia Resolve no tiene donde colgar
-- un marker. Despues van los markers de la regla (las ventanas de show), con
-- un asignador que deja libre el frame 0.
local function cerrarTimeline(K, clave, tl, final, marcas, fps, t0)
  K.U.marcarSello(K.E, tl, final, K.A.CD_SELLO .. K.E.sello)
  if marcas and #marcas > 0 and t0 then
    local asign = K.LIB.nuevoAsignador(1)
    local inicio = K.LIB.inicioDe(tl)
    for i, w in ipairs(marcas) do
      local ini = tonumber(w.inicio)
      if ini then
        local f = inicio + math.floor((ini - t0) * fps + 0.5)
        if not K.LIB.marcarEnTimeline(tl, f, w.color or "Sky", w.nombre or ("Bloque " .. i),
                                      w.nota or "", asign) then
          nota(K, "marcas de show que no entraron", "aviso", w.nombre)
        end
      end
    end
  end
  local ok, motivo = K.LIB.verificarOrdenPistas(tl)
  if not ok then nota(K, "regla dura rota (audio externo sobre audio de camara)", "error",
                      final .. ": " .. motivo) end
  K.E.construidas[#K.E.construidas + 1] = {clave = clave, tl = tl, nombre = final}
  K.hechas[#K.hechas + 1] = final
end

-- ---------- metadata y markers ----------------------------------------------

-- El MediaPoolItem y la ruta de un item de timeline, pedidos UNA vez: cada
-- helper los necesita y en el menu de Free cada llamada al API cuesta
-- (revision del 2026-10-07). La lista de items de una timeline se pide una vez
-- y se pasa a todos, asi que el mismo objeto es la clave.
local function mpiOf(K, item)
  local m = K.mpiItem[item]
  if m == nil then
    m = item:GetMediaPoolItem() or false
    K.mpiItem[item] = m
  end
  return m or nil
end

local function pathOf(K, item)
  local r = K.rutaItem[item]
  if r == nil then
    local mpi = mpiOf(K, item)
    local fp = mpi and mpi:GetClipProperty("File Path")
    r = (fp and fp ~= "") and fp or false
    K.rutaItem[item] = r
  end
  return r or nil
end


local function applyMetadata(K, mpi, info)
  if not mpi then return end
  local key = (mpi.GetMediaId and mpi:GetMediaId()) or tostring(mpi)
  if K.metaPuesta[key] then return end
  K.metaPuesta[key] = true
  for campo, meta in pairs({meta_description = "Description", meta_shot = "Shot",
                            meta_scene = "Scene", meta_keywords = "Keywords",
                            meta_comments = "Comments"}) do
    if info[campo] and info[campo] ~= "" then mpi:SetMetadata(meta, info[campo]) end
  end
end

-- VIDEO: color por rol, metadata, cull/review y los beats de entrevista. Todo
-- de punto (FCC 2026-07-11: los de duracion no funcionan como deberian).
local function decorate(K, item, rec, fps)
  local info = rec.info or {}
  item:SetClipColor(K.LIB.colorDeRoll(info.roll, "Apricot"))
  applyMetadata(K, mpiOf(K, item), info)
  if info.status == "cull" then
    item:AddMarker(0, "Red", "Posible descarte", info.notes or "", 1, "")
  elseif info.status == "review" then
    item:AddMarker(1, "Yellow", "Revisar", info.notes or "", 1, "")
  end
  local who = (info.who and info.who ~= "") and info.who or "Entrevista"
  local asign = K.LIB.nuevoAsignador(3)
  if info.beats and #info.beats > 0 then
    local nm = K.LIB.decorarBeats(item, info.beats, fps, {
      asignador = asign, clipKey = rec.fpath or rec.name, quien = who,
    })
    K.corridos = K.corridos + asign.corridos()
    return nm
  end
  local nm = 0
  for _, q in ipairs(info.questions or {}) do
    if item:AddMarker(asign.tomar(math.floor(q.s * fps)), "Purple", who, q.q or "", 1, "") then
      nm = nm + 1
    end
  end
  K.corridos = K.corridos + asign.corridos()
  return nm
end

-- AUDIO externo: metadata y un Purple por pregunta detectada.
local function decorateAudio(K, item, ainfo, fps)
  applyMetadata(K, mpiOf(K, item), ainfo or {})
  local nm = 0
  for i, q in ipairs((ainfo or {}).questions or {}) do
    local f = math.floor(q.s * fps)
    if f < 3 then f = 3 + i end
    if item:AddMarker(f, "Purple", string.sub(q.q or "", 1, 72), q.q or "", 1, "") then
      nm = nm + 1
    end
  end
  return nm
end

-- ---------- MODO MERGE -------------------------------------------------------

local function clipEstaMergeado(mpi)
  if not (mpi and mpi.GetAudioMapping) then return false, 0 end
  local ok, m = pcall(function() return mpi:GetAudioMapping() end)
  if not ok or not m then return false, 0 end
  m = tostring(m)
  local ligados = 0
  for _ in string.gmatch(m, '"%d+"%s*:%s*{[^}]-"path"') do ligados = ligados + 1 end
  if not string.match(m, '"linked_audio"%s*:%s*{%s*"') then return false, 0 end
  return true, ligados
end

local function rutasLigadas(mpi)
  local out = {}
  if not (mpi and mpi.GetAudioMapping) then return out end
  local ok, m = pcall(function() return mpi:GetAudioMapping() end)
  if not ok or not m then return out end
  for ruta in string.gmatch(tostring(m), '"path"%s*:%s*"([^"]+)"') do out[ruta] = true end
  return out
end

-- Pistas de audio que la timeline necesita ANTES del append: Resolve no las
-- crea al vuelo y el audio extra de un clip mergeado se pierde en silencio
-- (medido en 21.0.2 Free, 2026-07-30).
local function pistasNecesarias(K, list)
  local maxP = 1
  for _, r in ipairs(list or {}) do
    local esM, ligados = clipEstaMergeado(r.clip)
    local total = 1
    if esM then total = 1 + ligados
    elseif r.fpath and K.DATA.sync[r.fpath] then total = 1 + #K.DATA.sync[r.fpath] end
    if total > maxP then maxP = total end
  end
  return maxP
end

local function maxLavaliers(K, items)
  local maxL = 0
  for _, item in ipairs(items or {}) do
    local mpi = mpiOf(K, item)
    local fp = pathOf(K, item)
    local esM, ligados = clipEstaMergeado(mpi)
    local n = esM and ligados or (fp and K.DATA.sync[fp] and #K.DATA.sync[fp] or 0)
    if n > maxL then maxL = n end
  end
  return maxL
end

local function camBaseDe(list)
  for _, r in ipairs(list or {}) do
    if r.info and r.info.folder and r.info.folder ~= "" then return r.info.folder end
  end
  return "base"
end

-- ---------- audio de sync ------------------------------------------------------

-- Coloca el lavalier de cada clip con par de sync, DESPUES de los audios de
-- camara (regla del usuario, MAB 2026-06-12). Convencion: offset =
-- audio_start - video_start. `filtro(item, sy)`: si da false, no se coloca.
local function placeSyncAudio(K, tl, items, fps, filtro)
  local maxPairs = 0
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    for rank, sy in ipairs((fp and K.DATA.sync[fp]) or {}) do
      if (not filtro or filtro(item, sy)) and rank > maxPairs then maxPairs = rank end
    end
  end
  if maxPairs == 0 then return 0 end
  local pre = tl:GetTrackCount("audio")
  tl:AddTrack("audio", "stereo")
  if maxPairs >= 2 then tl:AddTrack("audio", "stereo") end

  local placed = 0
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    for rank, sy in ipairs((fp and K.DATA.sync[fp]) or {}) do
      if not filtro or filtro(item, sy) then
        local trackIdx = pre + rank
        local wav = K.audioByPath[sy.audiopath]
        if not wav and sy.audiopath then
          local base = string.match(sy.audiopath, "([^/]+)$")
          local padre = string.match(sy.audiopath, "([^/]+)/[^/]+$") or ""
          if base then
            wav = K.audioByName[padre .. "/" .. base] or K.audioByName[base]
            if wav then nota(K, "lavalier encontrado por nombre, no por ruta", "aviso", base) end
          end
        end
        if not wav then
          nota(K, "lavalier sin clip en el Media Pool", "aviso", sy.audio or sy.audiopath)
        else
          local off = sy.offset or 0
          local vdur = (K.DATA.clips[fp] or {}).dur or 0
          local ainfo = K.DATA.audios[sy.audiopath] or {}
          local adur = ainfo.dur or 0
          -- off < 0: el audio empezo ANTES; se salta |off| s del audio.
          -- off > 0: empezo DESPUES; se coloca off s a la derecha (BUG
          -- 2026-05-26: sin el desplazamiento, lip-sync corrido).
          local sourceStart, sourceEnd, recOff = 0, 0, 0
          if off < 0 then
            sourceStart = math.floor((-off) * fps)
            sourceEnd = sourceStart + math.floor(math.min(vdur, adur + off) * fps)
          else
            sourceEnd = math.floor(math.min(vdur - off, adur) * fps)
            recOff = math.floor(off * fps)
          end
          if sourceEnd <= sourceStart then sourceEnd = sourceStart + math.floor(fps) end
          local recordFrame = math.floor(item:GetStart()) + recOff
          if recordFrame < 0 then
            nota(K, "lavalier con recordFrame negativo (no se coloca)", "aviso", item:GetName())
          else
            local res = K.mp:AppendToTimeline({{
              mediaPoolItem = wav, mediaType = 2, trackIndex = trackIdx,
              recordFrame = recordFrame, startFrame = sourceStart, endFrame = sourceEnd,
            }})
            if not res then
              nota(K, "lavalier que Resolve no coloco", "aviso",
                   string.format("%s A%d", tostring(item:GetName()), trackIdx))
            else
              placed = placed + 1
              local gap = ""
              if off > 0.5 then
                gap = string.format("  GAP %.1fs al inicio", off)
              elseif off < -0.5 and (adur + off) < vdur and vdur - (adur + off) > 0.5 then
                gap = string.format("  GAP %.1fs al final", vdur - (adur + off))
              end
              local speaker = (sy.speaker and sy.speaker ~= "") and sy.speaker or "?"
              item:AddMarker(rank, "Cyan", string.format("A%d — %s", trackIdx, speaker),
                string.format("%s  off %.1fs  conf %.2f  tramo %.1fs%s", sy.audio or "?",
                              off, sy.conf or 0, (sourceEnd - sourceStart) / fps, gap), 1, "")
            end
          end
        end
      end
    end
  end
  return placed
end

-- ---------- companeras de multicam ---------------------------------------------

local function contarCompaneras(K, items, enLista)
  if K.nPares == 0 or not items then return 0 end
  local grupos, n = {}, 0
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    for _, p in ipairs((fp and K.companionsOf[fp]) or {}) do
      local g = K.grupoDe(p.b)
      if g ~= "" and not grupos[g] and (enLista == nil or enLista[p.b]) then
        grupos[g] = true; n = n + 1
      end
    end
  end
  return n
end

local function etiquetasCompaneras(K, items, enLista)
  local presentes = {}
  for _, item in ipairs(items or {}) do
    local fp = pathOf(K, item)
    for _, p in ipairs((fp and K.companionsOf[fp]) or {}) do
      local g = K.grupoDe(p.b)
      if g ~= "" and (enLista == nil or enLista[p.b]) then presentes[g] = true end
    end
  end
  local out = {}
  for _, g in ipairs(K.ordenCam) do
    if presentes[g] then out[#out + 1] = g end
  end
  return out
end

-- Coloca cada camara companera en SU pista fija, encima de su base y recortada
-- a la ventana de la base (FCC 2026-07-11, caso 8628). `enLista`: solo las
-- companeras cuyo clip pertenece a ESTA timeline (MAB 2026-06-12: "se repite
-- el angulo de mi camara").
local function placeCompanions(K, tl, items, fps, pistaAudio, enLista, tlname)
  if K.nPares == 0 or not items then return 0 end
  local function admitida(p) return (enLista == nil) or (enLista[p.b] == true) end
  local needed = false
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    for _, p in ipairs((fp and K.companionsOf[fp]) or {}) do
      if admitida(p) then needed = true; break end
    end
    if needed then break end
  end
  if not needed then return 0 end
  local nCams = math.max(2, 1 + #K.ordenCam)
  while tl:GetTrackCount("video") < nCams do tl:AddTrack("video") end
  local aComp = pistaAudio or 2
  local aMax = aComp + math.max(0, #K.ordenCam - 1)
  while tl:GetTrackCount("audio") < aMax do tl:AddTrack("audio", "stereo") end
  local placed, failed = 0, 0
  local placedPair = {}
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    for _, p in ipairs((fp and K.companionsOf[fp]) or {}) do
      local bclip = (not placedPair[p.key]) and admitida(p) and K.videoByPath[p.b]
      if bclip then
        placedPair[p.key] = true
        local baseStart, baseEnd = item:GetStart(), item:GetEnd()
        local srcFps = tonumber(bclip:GetClipProperty("FPS")) or fps
        local totalSrc = tonumber(bclip:GetClipProperty("Frames"))
        local recordFrame = math.floor(baseStart + (p.delta or 0) * fps + 0.5)
        local srcIn = 0
        if recordFrame < baseStart then
          srcIn = math.floor(((baseStart - recordFrame) / fps) * srcFps + 0.5)
          recordFrame = baseStart
        end
        local availTl = baseEnd - recordFrame
        local endFrame = srcIn + math.floor((availTl / fps) * srcFps + 0.5) - 1
        if totalSrc and endFrame > totalSrc - 1 then endFrame = totalSrc - 1 end
        local nombreB = string.match(p.b, "([^/]+)$") or p.b
        if availTl <= 0 or endFrame <= srcIn then
          failed = failed + 1
          K.mc.fallos[#K.mc.fallos + 1] = string.format("%s sobre %s (%s): sin traslape util",
            nombreB, tostring(item:GetName()), tostring(tlname))
        else
          local function clipInfo(mediaType, trackIdx)
            return {mediaPoolItem = bclip, mediaType = mediaType, trackIndex = trackIdx,
                    recordFrame = recordFrame, startFrame = srcIn, endFrame = endFrame}
          end
          local vIdx = K.pistaCam[K.grupoDe(p.b)] or 2
          local aIdx = aComp + (vIdx - 2)
          while tl:GetTrackCount("video") < vIdx do tl:AddTrack("video") end
          while tl:GetTrackCount("audio") < aIdx do tl:AddTrack("audio", "stereo") end
          local res = K.mp:AppendToTimeline({clipInfo(1, vIdx)})
          if not (res and res[1]) then
            for alt = vIdx + 1, vIdx + 2 do
              while tl:GetTrackCount("video") < alt do tl:AddTrack("video") end
              res = K.mp:AppendToTimeline({clipInfo(1, alt)})
              if res and res[1] then break end
            end
          end
          local resA = K.mp:AppendToTimeline({clipInfo(2, aIdx)})
          if res and res[1] then
            placed = placed + 1
            if p.basis == "reloj-show" and res[1].AddMarker then
              K.LIB.addMarker(res[1], 0, "Yellow", "sync por reloj",
                "Alineado por reloj dentro de una ventana de show (no confirmado por "
                .. "contenido A1<->A1). delta=" .. string.format("%.2f", p.delta or 0) .. "s")
            end
            local grupo = {item, res[1]}
            if resA and resA[1] then grupo[#grupo + 1] = resA[1] end
            K.LIB.ligarGrupo(tl, grupo)
          else
            failed = failed + 1
            K.mc.fallos[#K.mc.fallos + 1] = string.format(
              "%s sobre %s (%s): AppendToTimeline no devolvio item en V%d",
              nombreB, tostring(item:GetName()), tostring(tlname), vIdx)
          end
          if not (resA and resA[1]) then
            nota(K, "audio de companera que no se coloco", "aviso",
                 string.format("%s A%d", nombreB, aIdx))
          end
        end
      end
    end
  end
  K.mc.placed = K.mc.placed + placed
  K.mc.failed = K.mc.failed + failed
  return placed
end

-- ---------- constructores ------------------------------------------------------

-- Una timeline EMPACADA (A-ROLL, o B-ROLL sin espina dorsal, o una por
-- subcarpeta). `universo`: las rutas que pertenecen a la timeline ANTES de
-- sacar las companeras de V1 (sin el, una companera se excluiria a si misma).
local function buildTimeline(K, clave, tlname, list, withMulticam, universo)
  if #list == 0 then return nil, 0 end
  chronoSort(list)
  local tl, final = nuevaTimeline(K, tlname)
  if not tl then return nil, 0 end
  if K.modoMerge then
    local n = pistasNecesarias(K, list)
    if n > 1 then K.LIB.prepararPistasAudio(tl, n, "stereo") end
  end
  -- Uno por uno: el append en bloque no siempre respeta el orden (Zezzions
  -- 2026-05-26).
  for _, r in ipairs(list) do K.mp:AppendToTimeline({r.clip}) end
  local fps = tlFps(tl)
  local items = tl:GetItemListInTrack("video", 1) or {}
  if #items < #list then
    nota(K, "clips que no aterrizaron en V1", "error",
         string.format("%s: %d de %d", final, #items, #list))
  end
  local bypath, byname = {}, {}
  for _, r in ipairs(list) do
    if r.fpath then bypath[r.fpath] = r end
    if not byname[r.name] then byname[r.name] = r end
  end
  for _, item in ipairs(items) do
    local fp = pathOf(K, item)
    local r = (fp and bypath[fp]) or byname[item:GetName()]
    if r then K.markers = K.markers + decorate(K, item, r, fps) end
  end
  local enLista = universo
  if not enLista then
    enLista = {}
    for _, r in ipairs(list) do if r.fpath then enLista[r.fpath] = true end end
  end
  if K.modoMerge then
    local nComp = withMulticam and contarCompaneras(K, items, enLista) or 0
    if nComp > 0 then
      K.LIB.insertarPistasAudio(tl, nComp, 2, "stereo")
      placeCompanions(K, tl, items, fps, 2, enLista, final)
    end
    -- Lo que el merge NO ligo va aparte (Asistente, 2026-09-28).
    placeSyncAudio(K, tl, items, fps, function(item, sy)
      return not rutasLigadas(mpiOf(K, item))[sy.audiopath]
    end)
  else
    if withMulticam then placeCompanions(K, tl, items, fps, nil, enLista, final) end
    placeSyncAudio(K, tl, items, fps)
  end
  local lavs = {}
  for i = 1, maxLavaliers(K, items) do lavs[i] = "TX" .. i end
  local comps = withMulticam and etiquetasCompaneras(K, items, enLista) or {}
  K.LIB.nombrarPistas(tl, camBaseDe(list), comps, lavs)
  cerrarTimeline(K, clave, tl, final)
  return tl, #items
end

-- B-ROLL por hora real, con el lavalier como espina dorsal (2026-08-13): cada
-- cosa en SU hora, los WAV enteros cortados en los bordes de la camara base.
local function buildTimelineCronologica(K, clave, tlname, lista)
  local tl, final = nuevaTimeline(K, tlname)
  if not tl then return nil, 0 end
  local fps = tlFps(tl)
  local vistas, ordenCad = {}, {}
  for _, r in ipairs(K.audioRecs) do
    local ch = r.ainfo.chain or ""
    if not vistas[ch] then vistas[ch] = true; ordenCad[#ordenCad + 1] = ch end
  end
  table.sort(ordenCad)
  local nCam = 1 + #K.ordenCam
  local idxCad = {}
  for i, ch in ipairs(ordenCad) do idxCad[ch] = nCam + i end
  K.LIB.prepararPistasAudio(tl, nCam + #ordenCad, "stereo")
  while tl:GetTrackCount("video") < nCam do tl:AddTrack("video") end

  local cortes = {}
  for _, r in ipairs(lista) do
    if ((r.info and r.info.folder) or "") == K.base then
      local ep = horaDe(K, r)
      local dur = tonumber(r.info and r.info.dur) or 0
      if ep and dur > 0 then
        cortes[#cortes + 1] = ep
        cortes[#cortes + 1] = ep + dur
      end
    end
  end
  table.sort(cortes)

  local marcas = {}
  for i, w in ipairs(K.DATA.show_windows or {}) do
    marcas[#marcas + 1] = {
      inicio = w.inicio, color = "Sky", nombre = string.format("SHOW %d", i),
      nota = string.format("%s · %.1f min · %.1f dB", w["local"] or "", w.dur_min or 0,
                           w.nivel_db or 0),
    }
  end

  -- Las marcas no van en el plan: van despues del sello (cerrarTimeline).
  local plan = {fps = fps, cortes = cortes, audios = {}, videos = {}, marcas = {}}
  for _, r in ipairs(K.audioRecs) do
    plan.audios[#plan.audios + 1] = {
      clip = r.clip, name = r.name, epoch = tonumber(r.ainfo.epoch_start),
      dur = tonumber(r.ainfo.dur), pista = idxCad[r.ainfo.chain or ""] or (nCam + 1),
    }
  end
  for _, r in ipairs(lista) do
    local g = (r.info and r.info.folder) or ""
    local pista = 1
    if g ~= K.base and g ~= "" then pista = K.pistaCam[g] or 1 end
    local hora = horaDe(K, r)
    if not hora then
      -- Sin par de sync ni reloj utilizable no hay donde ponerlo: se nombra,
      -- porque el verificador lo va a echar de menos en el .drt.
      nota(K, C.NOTA_SIN_HORA, "aviso", r.name)
    end
    plan.videos[#plan.videos + 1] = {clip = r.clip, name = r.name, epoch = hora, pista = pista}
  end
  local res = K.LIB.construirPorTiempoReal(K.mp, tl, plan)
  for _, aviso in ipairs(res.avisos or {}) do nota(K, "B-ROLL por hora real", "aviso", aviso) end

  -- Pistas con nombre, como en las demas timelines: sin nombre, la regla dura
  -- (CAM antes que LAVA) no se podia comprobar aqui, ni en el reporte ni en
  -- el motor (revision del 2026-10-07). Solo las que tienen algo: una pista
  -- con nombre y vacia el verificador la cuenta como aviso.
  local txDe = {}
  for _, r in ipairs(K.audioRecs) do
    local tx = (r.ainfo.tx and r.ainfo.tx ~= "") and r.ainfo.tx or nil
    if tx and not txDe[r.ainfo.chain or ""] then txDe[r.ainfo.chain or ""] = tx end
  end
  local function nombrar(tipo, i, nombre)
    local items = tl:GetItemListInTrack(tipo, i) or {}
    if #items > 0 then tl:SetTrackName(tipo, i, nombre) end
  end
  for i = 1, nCam do
    local grupo = (i == 1) and K.base or K.ordenCam[i - 1]
    nombrar("video", i, "CAM " .. tostring(grupo))
    nombrar("audio", i, "CAM " .. tostring(grupo))
  end
  for i, ch in ipairs(ordenCad) do
    nombrar("audio", nCam + i, "LAVA " .. (txDe[ch] or ch))
  end

  local bypath, byname = {}, {}
  for _, r in ipairs(lista) do
    if r.fpath then bypath[r.fpath] = r end
    if not byname[r.name] then byname[r.name] = r end
  end
  for v = 1, nCam do
    for _, item in ipairs(tl:GetItemListInTrack("video", v) or {}) do
      local fp = pathOf(K, item)
      local r = (fp and bypath[fp]) or byname[item:GetName()]
      if r then K.markers = K.markers + decorate(K, item, r, fps) end
    end
  end
  cerrarTimeline(K, clave, tl, final, marcas, fps, res.t0)
  return tl, res.videos or 0
end

-- AUDIOS EXTERNOS: los lavalieres como timecode (2026-10-01). Cada WAV entero
-- en la pista de su TX y, encima, cada clip en su hora.
local function buildLineaDeLavas(K, clave, tlname)
  local tl, final = nuevaTimeline(K, tlname)
  if not tl then return nil, 0 end
  local fps = tlFps(tl)
  local plan = {fps = fps, ordenTx = K.ordenTx, audios = {}, videos = {}}
  for _, r in ipairs(K.audioRecs) do
    plan.audios[#plan.audios + 1] = {
      clip = r.clip, name = r.name, info = r.ainfo,
      epoch = tonumber(r.ainfo.epoch_start), dur = tonumber(r.ainfo.dur),
      tx = (r.ainfo.tx and r.ainfo.tx ~= "") and r.ainfo.tx or (r.ainfo.chain or "?"),
    }
  end
  for _, r in ipairs(K.recs) do
    local plist = r.fpath and K.DATA.sync[r.fpath]
    plan.videos[#plan.videos + 1] = {
      clip = r.clip, name = r.name, epoch = (horaDe(K, r)),
      roll = ((r.info or {}).roll == "A") and "A" or "B",
      conLava = (plist ~= nil and #plist > 0),
    }
  end
  local res = K.LIB.construirLineaDeLavas(K.mp, tl, plan)
  for _, w in ipairs(res.wavItems or {}) do
    K.markers = K.markers + decorateAudio(K, w.item, w.info, fps)
  end
  for _, aviso in ipairs(res.avisos or {}) do nota(K, "AUDIOS EXTERNOS", "aviso", aviso) end
  if (res.encimados or 0) > 0 then
    K.E.anotar("info", res.encimados .. " clip(s) se enciman con otro de su rol en "
      .. final .. " y van en otra pista")
  end
  cerrarTimeline(K, clave, tl, final)
  return tl, res.wavs or 0
end

-- ---------- la carpeta de las timelines -----------------------------------------

local function binDestino(K)
  local function hijo(folder, nombreMin)
    for _, s in ipairs(folder:GetSubFolderList() or {}) do
      if string.find(string.lower(tostring(s:GetName() or "")), nombreMin, 1, true) then
        return s
      end
    end
    return nil
  end
  local root = K.mp:GetRootFolder()
  local tlBin = hijo(root, "timeline") or root
  local dest = hijo(tlBin, "asistente de edici") or K.mp:AddSubFolder(tlBin, "asistente de edicion")
  if dest then
    K.mp:SetCurrentFolder(dest)
  else
    K.E.anotar("aviso", "no se pudo crear el bin 'asistente de edicion': las timelines "
      .. "quedan en el bin actual")
  end
end

-- ---------- correr ------------------------------------------------------------

-- Valida la accion antes de tocar Resolve. Lo mismo lo valida lib/pedido.py
-- al escribir el pedido; aqui se repite porque el buzon pudo llegar de otro
-- lado, y un pedido que no se entiende no construye a medias.
local function validar(E, a)
  if type(E.rutas.data) ~= "string" or E.rutas.data == "" then
    return "E04 el pedido no trae rutas.data (el horneado)"
  end
  if type(E.pedido.pfx) ~= "string" or E.pedido.pfx == "" then
    return "el pedido no trae pfx (el prefijo de las timelines)"
  end
  if a.mover_a_bins then return "mover_a_bins esta fuera de Free v1" end
  if a.cortar_silencios or a.marcar_cortes then
    return "cortar_silencios y marcar_cortes llegan con los proyectos comerciales"
  end
  if C.sufijoDe(a.dia) == nil then
    return "dia '" .. tostring(a.dia) .. "' no es AAAA-MM-DD"
  end
  return nil
end

function C.correr(E, a, dep)
  local problema = validar(E, a)
  if problema then return false, problema end
  local A, LIB = dep.A, dep.LIB
  local quiere = type(a.timelines) == "table" and a.timelines or {}
  local function activa(k) return quiere[k] ~= false end

  local K = {E = E, a = a, A = A, LIB = LIB, U = A.util, mp = E.mp, proj = E.proj,
             notas = {}, ordenNotas = {}, audioByPath = {}, audioByName = {},
             videoByPath = {}, companionsOf = {}, ordenCam = {}, pistaCam = {},
             skews = {}, base = "", nPares = 0, sinSkew = {}, metaPuesta = {},
             mpiItem = {}, rutaItem = {},
             mc = {placed = 0, failed = 0, fallos = {}}, hechas = {}, markers = 0,
             corridos = 0, importados = 0, faltan = 0,
             conMulticam = (a.multicam ~= false),
             -- El orden de las pistas LAVA lo da el pedido (lavalier_tx del
             -- project_config.json); sin el, por nombre. Antes era {"izq",
             -- "drc"} fijo, el de Asistente, para todos (revision 2026-10-07).
             ordenTx = type(a.orden_tx) == "table" and a.orden_tx or {}}
  K.grupoDe = function(fpath) return grupoDe(K, fpath) end
  local sufijo = C.sufijoDe(a.dia)
  K.esDelDia = function(dia) return sufijo == "" or (dia or "") == a.dia end
  local pfx = E.pedido.pfx
  local erroresAntes = 0
  for _, x in ipairs(E.anotaciones) do
    if x.nivel == "error" then erroresAntes = erroresAntes + 1 end
  end

  -- Lo que la libreria cuenta (linkeo, pedazos que no aterrizan) va al
  -- reporte, agrupado. Un FAIL es aviso; lo demas, informacion.
  LIB.decir = function(texto)
    texto = tostring(texto or "")
    if string.find(texto, "FAIL", 1, true) then
      nota(K, "la libreria reporto fallas", "aviso", texto)
    elseif string.find(texto, "SetClipsLinked", 1, true) then
      nota(K, "linkeo de angulos", "info", texto)
    end
  end

  -- 1. datos
  local err
  K.DATA, err = cargar(K, E.rutas.data, "el horneado")
  if not K.DATA then return false, err end
  K.DATA.clips = K.DATA.clips or {}
  K.DATA.sync = K.DATA.sync or {}
  K.DATA.audios = K.DATA.audios or {}
  K.MC = {}
  if type(E.rutas.multicam) == "string" and E.rutas.multicam ~= "" then
    local mc, errMc = cargar(K, E.rutas.multicam, "el multicam")
    if mc then K.MC = mc else E.anotar("aviso", errMc .. ": se construye sin multicam") end
  end

  -- 2. Media Pool y carpeta de destino. Si ningun video del horneado esta en
  -- el pool, el editor no importo el material (o abrio otro proyecto): no se
  -- construye nada ni se importa ningun WAV, y se dice.
  K.recs = inventario(K)
  if #K.recs == 0 then
    emitirNotas(K)   -- que faltan, y cuales: tambien cuando no se construye nada
    if K.faltan > 0 then
      return false, "ninguno de los " .. K.faltan .. " video(s) del horneado"
             .. (sufijo ~= "" and (" del " .. a.dia) or "") .. " esta en el Media Pool: "
             .. "importa el material y vuelve a aplicar"
    end
    return false, "el horneado no trae clips" .. (sufijo ~= "" and (" del " .. a.dia) or "")
  end
  binDestino(K)
  prepararMulticam(K)
  for _, r in ipairs(K.recs) do r.t = realTime(K, r.info, r.fpath) end

  -- 3. los WAV de sync que falten en el pool, solo los de los clips que entran
  -- (con `dia`, los de otros dias ni se importan ni avisan), y los del dia
  -- para la espina.
  for _, r in ipairs(K.recs) do
    for _, sy in ipairs((r.fpath and K.DATA.sync[r.fpath]) or {}) do
      wavDe(K, sy.audiopath)
    end
  end
  K.audioRecs = {}
  for apath, ainfo in pairs(K.DATA.audios) do
    if K.esDelDia(ainfo.dia) then
      local item = wavDe(K, apath)
      if item then
        K.audioRecs[#K.audioRecs + 1] = {clip = item, name = ainfo.name,
                                         created = ainfo.created, ainfo = ainfo}
      end
    end
  end
  table.sort(K.audioRecs, function(x, y)
    local cx = (x.created and x.created ~= "") and x.created or "9999"
    local cy = (y.created and y.created ~= "") and y.created or "9999"
    if cx ~= cy then return cx < cy end
    return tostring(x.name or "") < tostring(y.name or "")
  end)
  if K.importados > 0 then E.anotar("info", K.importados .. " WAV importados al Media Pool") end

  -- 4. MODO MERGE: el que pida el pedido o, si no dice, el que se vea en el pool.
  if type(a.modo_merge) == "boolean" then
    K.modoMerge = a.modo_merge
  else
    local n = 0
    for _, r in ipairs(K.recs) do
      if clipEstaMergeado(r.clip) then n = n + 1 end
    end
    K.modoMerge = (n > 0)
  end

  -- 5. una por subcarpeta, si hay mas de una
  if activa("por_subcarpeta") then
    local porLoc, locs = {}, {}
    for _, r in ipairs(K.recs) do
      if not porLoc[r.loc] then porLoc[r.loc] = {}; locs[#locs + 1] = r.loc end
      table.insert(porLoc[r.loc], r)
    end
    table.sort(locs)
    if #locs > 1 then
      for i, loc in ipairs(locs) do
        buildTimeline(K, "sub_" .. i, pfx .. loc .. sufijo, porLoc[loc], false)
      end
    end
  end

  -- 6. A-ROLL y B-ROLL: entre las dos, TODOS los clips (garantia de cobertura)
  local aroll = LIB.filtrarPorRoll(K.recs, "A")
  local broll = LIB.filtrarPorRoll(K.recs, "B")
  local function sinCompaneras(lista)
    local universo, comp = {}, {}
    for _, r in ipairs(lista) do if r.fpath then universo[r.fpath] = true end end
    for _, r in ipairs(lista) do
      for _, p in ipairs((r.fpath and K.companionsOf[r.fpath]) or {}) do
        if universo[p.b] then comp[p.b] = true end
      end
    end
    local out = {}
    for _, r in ipairs(lista) do
      if not (r.fpath and comp[r.fpath]) then out[#out + 1] = r end
    end
    return out, universo
  end
  -- Una timeline que el pedido espera y que no tiene con que construirse se
  -- dice con su nombre: el verificador la busca en el reporte antes de dar
  -- rojo por un .drt que no llego (revision del 2026-10-07).
  local esperadas = {}
  local esp = type(E.pedido.esperado) == "table" and E.pedido.esperado.timelines or nil
  for _, n in ipairs(type(esp) == "table" and esp or {}) do esperadas[n] = true end
  local function sinMaterial(nombre, razon)
    if esperadas[nombre] then
      E.anotar("aviso", "timeline sin material (" .. razon .. "): " .. nombre)
    end
  end

  local colocados = {}   -- por rec, no por ruta: un clip sin File Path tambien cuenta
  for _, par in ipairs({{"aroll", aroll}, {"broll", broll}}) do
    local clave, lista = par[1], par[2]
    local nombre = pfx .. C.NOMBRE[clave] .. sufijo
    if activa(clave) and #lista == 0 then
      sinMaterial(nombre, "ningun clip de ese rol en el Media Pool")
    elseif activa(clave) then
      local cronologica = (clave == "broll") and a.cronologia_broll ~= false
                          and #K.audioRecs > 0 and K.base ~= ""
      local tl
      if cronologica then
        tl = buildTimelineCronologica(K, clave, nombre, lista)
      else
        local base, universo = sinCompaneras(lista)
        tl = buildTimeline(K, clave, nombre, base, K.conMulticam, universo)
      end
      if tl then
        for _, r in ipairs(lista) do colocados[r] = true end
      end
    end
  end
  for _, r in ipairs(K.recs) do
    if not colocados[r] then
      nota(K, "clips fuera de A-ROLL y B-ROLL (cobertura incompleta)", "error",
           r.name .. (((r.info or {}).roll or "") == "" and " sin roll: re-hornea con "
                      .. "derive_roll.py" or ""))
    end
  end

  -- 7. AUDIOS EXTERNOS
  local nombreAudios = pfx .. C.NOMBRE.audios_externos .. sufijo
  if activa("audios_externos") and #K.audioRecs > 0 then
    buildLineaDeLavas(K, "audios_externos", nombreAudios)
  elseif activa("audios_externos") then
    sinMaterial(nombreAudios, "ningun WAV del dia en el Media Pool ni importable")
  end

  -- 8. las garantias, como anotaciones
  if K.mc.failed > 0 then
    for _, d in ipairs(K.mc.fallos) do
      nota(K, "angulos de multicam que no se colocaron", "error", d)
    end
  end
  local sinSkew = {}
  for g in pairs(K.sinSkew) do sinSkew[#sinSkew + 1] = g end
  table.sort(sinSkew)
  for _, g in ipairs(sinSkew) do
    nota(K, "camaras sin skew de reloj (ordenadas con el reloj crudo)", "aviso", g)
  end
  if K.corridos > 0 then
    E.anotar("info", K.corridos .. " marker(s) corridos al siguiente frame libre")
  end
  emitirNotas(K)

  local errores = -erroresAntes
  for _, x in ipairs(E.anotaciones) do
    if x.nivel == "error" then errores = errores + 1 end
  end
  local detalle = string.format("%d timeline(s): %s; %d clip(s), %d marker(s), %d angulo(s)",
    #K.hechas, table.concat(K.hechas, ", "), #K.recs, K.markers, K.mc.placed)
  if #K.hechas == 0 then
    return false, "no se construyo ninguna timeline (" .. #K.recs .. " clip(s) del "
                  .. "horneado en el pool; faltan " .. K.faltan .. ")"
  end
  if errores > 0 then return false, detalle .. "; con " .. errores .. " error(es)" end
  return true, detalle
end

return C
