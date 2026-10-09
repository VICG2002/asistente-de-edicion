-- Mock de Resolve con MEDIA POOL REAL, para ejercitar decorate() y la
-- construccion de timelines sin abrir Resolve.
--
--   lua mock_resolve_full.lua asistente_fcc.lua
--
-- Que cubre y el mock viejo no:
--   - Media Pool con clips de verdad -> `walk`, `dataFor` y `decorate` corren.
--   - AddMarker con la MISMA regla que Resolve: rechaza dos markers en el
--     mismo frame del mismo item. Sin esto no se ve la perdida silenciosa
--     de markers por colision.
--   - intercepta el `dofile` del <proyecto>_data.lua y le inyecta datos
--     sinteticos, para no depender de que el disco este montado.
--
-- Al terminar imprime cuantos markers se colocaron y cuantos se rechazaron.
-- Si algun AddMarker fue rechazado, sale con codigo 1.
--
-- Modos (Fase 0, kit de diagnostico). Son globales que se ponen ANTES de
-- cargar el mock; sin ellas el mock se comporta exactamente como antes.
--
--   SANDBOX_ESTRICTO = true
--     Justo antes de correr el objetivo pone en nil io, os, debug, require,
--     package, ffi, arg y print. Es lo mas cerrado que terceros reportan para
--     el menu de Resolve 21.1 build 17: si un script sobrevive aqui, sobrevive
--     alla. El protocolo de salida del mock usa `_print`, guardado al cargar,
--     por eso sigue hablando aunque el objetivo ya no tenga `print`.
--       lua -e "SANDBOX_ESTRICTO=true" mock_resolve_full.lua objetivo.lua
--
--   MOCK_SOLO_MODELO = true
--     Define el modelo (resolve, fusion, MOCK_REGISTRO) y retorna la tabla
--     MOCK antes de la seccion 'correr'. Asi otro runner reutiliza el mismo
--     Resolve falso sin heredar las comprobaciones de los asistente_*.lua
--     (tests/test_lua_sandbox.py lo hace con resolve/diagnostico.lua).
--
--   MOCK_PROYECTO   nombre del proyecto abierto (por defecto "MOCK FULL").
--                   El diagnostico solo actua si se llama DIEZ50_DIAG...
--   MOCK_PREFS_PATH ruta donde fusion:SavePrefs() escribe un archivo con el
--                   formato de texto de Fusion.prefs (solo si hay `io`).
--   MOCK_DOFILE_REAL = true
--                   no interceptar los *_data.lua: el kit de diagnostico
--                   tiene un `grande_data.lua` real que SI hay que cargar.
--   MOCK_POOL = { {ruta=, nombre=, bin=, frames=, duracion=}, ... }
--                   el Media Pool con el que arranca, en lugar del de
--                   siempre. `bin` cuelga el clip de una subcarpeta del raiz;
--                   `duracion` es el timecode de "Duration" (los WAV).
--                   Lo usa tests/test_construir.py (2026-10-06).
--
-- MOCK_REGISTRO guarda cada timeline, marker y bin que se crea, para que el
-- runner lo inspeccione despues (MOCK.volcar). Resolve no ofrece esa vista
-- desde fuera; el mock si, y es lo que permite afirmar "se creo X" en un test.

-- El protocolo del mock se queda con sus propias referencias ANTES de que el
-- modo estricto las borre: si no, el mock no podria ni reportar su resultado.
local _print = print
local _salir = os and os.exit or function() end

local RECHAZADOS = 0
local PUESTOS = 0
local POR_COLOR = {}

-- ---------- datos sinteticos --------------------------------------------
-- TRES grupos de camara (v0.3.0). Con dos, el mock no ejercitaba la asignacion
-- de pista por camara (V2/V3, A2/A3) ni el caso de dos companeras sobre la
-- misma base — que es justo donde se rompia el comportamiento anterior, que
-- mandaba todo a V2 y usaba V3 solo como red de seguridad.
local V1 = "/x/Ayan/ASA_0001.MP4"     -- camara BASE
local V2 = "/x/Ayan/ASA_0002.MP4"
local W1 = "/x/Vic/VICG0001.MP4"      -- companera 1
local I1 = "/x/Iban/C0001.MP4"        -- companera 2
local A1 = "/x/Audio/Izq/00001.WAV"
local A2 = "/x/Audio/Derecha/00001.WAV"

-- Epoch de referencia (fijo: el mock no puede depender de la hora del reloj).
local T0 = 1785695473

local DATA_FALSA = {
  clips = {
    [V1] = {
      name = "ASA_0001.MP4", folder = "Ayan", rel_path = "Ayan/ASA_0001.MP4",
      status = "", notes = "", dur = 90.0, created = "2026-07-01T10:00:00.000000Z",
      camera = "main", interview = true, category = "entrevista",
      who = "ESCALADOR_A", roll = "A", roll_score = 0.90,
      roll_reason = "entrevista — regla documental",
      meta_description = "Entrevista a ESCALADOR_A", meta_shot = "PM",
      meta_scene = "entrevista", meta_keywords = "entrevista,PM,ESCALADOR_A",
      meta_comments = "",
      curated_segments = {},
      questions = {
        {s = 1.0, e = 40.0, q = "Primer recuerdo del futbol", r = "Su papa lo llevo al Azteca."},
        {s = 40.0, e = 90.0, q = "Que sintio al entrar", r = "Como entrar a otro mundo."},
      },
      beats = {
        {kind = "pregunta",  s = 1.0,  e = 5.0,  text = "Primer recuerdo del futbol",
         speaker = "video_a1", conf = 0.9, src = "master_src", qi = 0},
        {kind = "respuesta", s = 5.0,  e = 40.0, text = "Su papa lo llevo al Azteca en 1986.",
         speaker = "lavalier_146", conf = 0.9, src = "master_src", qi = 0},
        -- MISMO segundo que la respuesta: prueba la colision de frames.
        {kind = "keyword",   s = 5.0,  e = 5.5,  text = "Estadio Azteca",
         speaker = "", conf = 0.7, src = "vocabulario", qi = -1},
        {kind = "pausa",     s = 20.0, e = 24.8, text = "pausa de 4.8s",
         speaker = "", conf = 0.8, src = "silencedetect", qi = 0},
        {kind = "emocion",   s = 20.0, e = 24.8, text = "candidato: pausa larga",
         speaker = "", conf = 0.4, src = "silencio_largo", qi = 0},
        {kind = "pregunta",  s = 40.0, e = 44.0, text = "Que sintio al entrar",
         speaker = "video_a1", conf = 0.9, src = "master_src", qi = 1},
        {kind = "respuesta", s = 44.0, e = 90.0, text = "Como entrar a otro mundo.",
         speaker = "lavalier_146", conf = 0.9, src = "master_src", qi = 1},
      },
      highlights = {},
    },
    -- Clip SIN beats: comprueba el respaldo a `questions` (compat con bakes viejos)
    [V2] = {
      name = "ASA_0002.MP4", folder = "Ayan", rel_path = "Ayan/ASA_0002.MP4",
      status = "review", notes = "revisar foco", dur = 60.0,
      created = "2026-07-01T11:00:00.000000Z",
      camera = "main", interview = true, category = "entrevista", who = "Ana",
      roll = "B", roll_score = 0.12, roll_reason = "densidad baja",
      curated_segments = {}, beats = {}, highlights = {},
      questions = {
        {s = 2.0, e = 30.0, q = "De donde eres", r = "De Coyoacan."},
      },
    },
    -- Companera 1 (otra camara, mismo instante que V1): debe ir a V2/A2.
    [W1] = {
      name = "VICG0001.MP4", folder = "Vic", rel_path = "Vic/VICG0001.MP4",
      status = "", notes = "", dur = 88.0, created = "2026-07-01T10:00:02.000000Z",
      camera = "main", interview = true, category = "entrevista", who = "ESCALADOR_A",
      roll = "A", roll_score = 0.88, roll_reason = "entrevista — regla documental",
      curated_segments = {}, beats = {}, highlights = {}, questions = {},
    },
    -- Companera 2: debe ir a V3/A3 y NO pelearse la pista con la anterior.
    [I1] = {
      name = "C0001.MP4", folder = "Iban", rel_path = "Iban/C0001.MP4",
      status = "", notes = "", dur = 70.0, created = "2026-07-01T10:00:05.000000Z",
      camera = "main", interview = false, category = "", who = "",
      roll = "A", roll_score = 0.80, roll_reason = "cobertura del show",
      curated_segments = {}, beats = {}, highlights = {}, questions = {},
    },
  },
  byname = { ["ASA_0001.MP4"] = V1, ["ASA_0002.MP4"] = V2,
             ["VICG0001.MP4"] = W1, ["C0001.MP4"] = I1 },
  sync = {
    [V1] = {
      {audio = "00001.WAV", audiopath = A1, offset = -30.0, conf = 0.95, speaker = ""},
      {audio = "00001.WAV", audiopath = A2, offset = -28.2, conf = 0.95, speaker = ""},
    },
  },
  audios = {
    [A1] = {name = "00001.WAV", folder = "Izq", dur = 600.0, created = "",
            category = "", meta_description = "", meta_shot = "", meta_scene = "",
            meta_keywords = "", meta_comments = "", content_segments = {},
            chain = "Audio/Izq#00001.WAV", chain_pos = 0.0, epoch_start = T0,
            questions = {}},
    [A2] = {name = "00001b.WAV", folder = "Derecha", dur = 600.0, created = "",
            category = "", meta_description = "", meta_shot = "", meta_scene = "",
            meta_keywords = "", meta_comments = "", content_segments = {},
            chain = "Audio/Derecha#00001b.WAV", chain_pos = 0.0,
            epoch_start = T0 - 1.8, questions = {}},
  },
}

-- Pares multicam sinteticos: DOS companeras sobre la MISMA base, una
-- confirmada por contenido y otra alineada por reloj dentro del show.
local MULTICAM_FALSO = {
  skews = {Ayan = 0.0, Vic = 0.0, Iban = 0.0},
  base_group = "Ayan",
  -- Orden por CANTIDAD DE MATERIAL, no alfabetico: Vic rodo mas que Iban, asi
  -- que Vic va a V2 y Iban a V3. Alfabeticamente seria al reves, que es justo
  -- el error que esta regla corrige.
  track_order = {"Ayan", "Vic", "Iban"},
  pairs = {
    {a = V1, b = W1, delta = 2.0, overlap = 88.0, verified = true,
     place = true, basis = "contenido"},
    {a = V1, b = I1, delta = 5.0, overlap = 70.0, verified = false,
     place = true, basis = "reloj-show"},
  },
}


-- ---------- objetos ------------------------------------------------------

-- Lo que el runner puede inspeccionar al final. Se llena al crear, nunca se
-- vacia: un test que pregunta "se creo alguna timeline?" necesita ver tambien
-- las que despues se borraron o renombraron.
MOCK_REGISTRO = {timelines = {}, markers = {}, bins = {}, guardados = 0,
                 exportes = {}, borradas = 0, importados = {}}

-- Registra un marker y deja la referencia para poder marcarlo borrado. Guarda
-- el objeto duenio (no su nombre): si la timeline se renombra despues, el
-- volcado debe mostrar el nombre FINAL, que es el que veria el editor.
local function registrarMarker(donde, duenio, f, color, name, note, dur, cd)
  local rec = {donde = donde, duenio = duenio, frame = f, color = color,
               name = name, note = note, duration = dur, customData = cd or ""}
  MOCK_REGISTRO.markers[#MOCK_REGISTRO.markers + 1] = rec
  return rec
end

-- Forma que Resolve devuelve en GetMarkers/GetMarkerByCustomData:
-- { [frame] = {color, duration, note, name, customData} }.
local function copiaMarker(m)
  return {color = m.color, duration = m.duration or m.dur or 1, note = m.note or "",
          name = m.name or "", customData = m.customData or m.cd or ""}
end
local function marcasPorCustomData(tabla, cd)
  local out = {}
  for f, m in pairs(tabla) do
    if (m.customData or m.cd) == cd then out[f] = copiaMarker(m) end
  end
  return out
end
local function borrarPorCustomData(tabla, cd)
  local borrados = false
  for f, m in pairs(tabla) do
    if (m.customData or m.cd) == cd then
      if m.rec then m.rec.borrado = true end
      tabla[f] = nil
      borrados = true
    end
  end
  return borrados
end

local MediaPoolItem = {}
MediaPoolItem.__index = MediaPoolItem
function MediaPoolItem.new(path, name)
  return setmetatable({path = path, name = name, meta = {}, meta3 = {},
                       markers = {}}, MediaPoolItem)
end
function MediaPoolItem:GetName() return self.name end
function MediaPoolItem:GetMediaId() return self.path end
function MediaPoolItem:GetClipProperty(k)
  if k == "File Path" then return self.path end
  if k == "FPS" then return "24" end
  if k == "Frames" then return tostring(self.frames or 2160) end
  if k == "Duration" then return self.duracion or "" end
  return ""
end
function MediaPoolItem:SetMetadata(k, v) self.meta[k] = v; return true end
function MediaPoolItem:GetMetadata(k)
  if k == nil then return self.meta end
  return self.meta[k] or ""
end
-- Resolve acepta dos formas: la vieja (clave, valor) y la de tabla. Las dos
-- se miden en el diagnostico; el mock acepta ambas para no decidir por el.
function MediaPoolItem:SetThirdPartyMetadata(k, v)
  if type(k) == "table" then
    for kk, vv in pairs(k) do self.meta3[kk] = vv end
    return true
  end
  if type(k) ~= "string" then return false end
  self.meta3[k] = v
  return true
end
function MediaPoolItem:GetThirdPartyMetadata(k)
  if k == nil then return self.meta3 end
  return self.meta3[k] or ""
end
function MediaPoolItem:AddMarker(f, color, name, note, dur, cd)
  if self.markers[f] then RECHAZADOS = RECHAZADOS + 1; return false end
  local rec = registrarMarker("clip", self, f, color, name, note, dur, cd)
  self.markers[f] = {color = color, name = name, note = note, duration = dur,
                     customData = cd or "", rec = rec}
  return true
end
function MediaPoolItem:GetMarkers()
  local out = {}
  for f, m in pairs(self.markers) do out[f] = copiaMarker(m) end
  return out
end
function MediaPoolItem:GetMarkerByCustomData(cd)
  return marcasPorCustomData(self.markers, cd)
end
function MediaPoolItem:DeleteMarkerByCustomData(cd)
  return borrarPorCustomData(self.markers, cd)
end

local TimelineItem = {}
TimelineItem.__index = TimelineItem
function TimelineItem.new(mpi, start)
  return setmetatable({mpi = mpi, start = start, markers = {}}, TimelineItem)
end
function TimelineItem:GetName() return self.mpi:GetName() end
function TimelineItem:GetMediaPoolItem() return self.mpi end
function TimelineItem:GetStart() return self.start end
-- Duracion: la del tramo pedido (startFrame..endFrame, inclusivo, como Free
-- 21.0.2) o la del clip entero; 2160 si nadie la dijo. Hasta el 2026-10-06 era
-- 2160 fija, y la garantia "los pedazos suman el WAV" de colocarAudioPartido
-- daba un aviso falso en cualquier prueba que partiera un WAV.
function TimelineItem:GetEnd()
  return self.start + (self.dur or 2160)
end
function TimelineItem:GetDuration() return self.dur or 2160 end
function TimelineItem:SetClipColor(c) self.color = c; return true end
function TimelineItem:GetClipColor() return self.color or "" end
function TimelineItem:AddMarker(f, color, name, note, dur, cd)
  -- Resolve rechaza dos markers en el mismo frame. Reproducirlo es el punto
  -- de este mock: sin esta regla las colisiones pasan desapercibidas.
  if self.markers[f] then RECHAZADOS = RECHAZADOS + 1; return false end
  local rec = registrarMarker("item", self, f, color, name, note, dur, cd)
  self.markers[f] = {color = color, name = name, note = note, dur = dur, cd = cd,
                     customData = cd or "", rec = rec}
  PUESTOS = PUESTOS + 1
  POR_COLOR[color] = (POR_COLOR[color] or 0) + 1
  return true
end
function TimelineItem:GetMarkers()
  local out = {}
  for f, m in pairs(self.markers) do out[f] = copiaMarker(m) end
  return out
end
function TimelineItem:GetMarkerByCustomData(cd)
  return marcasPorCustomData(self.markers, cd)
end
function TimelineItem:DeleteMarkerByCustomData(cd)
  return borrarPorCustomData(self.markers, cd)
end

local proj
-- Resolve rechaza / \ : * ? " < > | en el nombre de una timeline y de un bin:
-- CreateEmptyTimeline y AddSubFolder devuelven nil, y SetName devuelve false.
-- Medido en Studio 21.1.0.17 el 2026-10-01; son los prohibidos en nombres de
-- archivo de Windows. Sin esto el mock aceptaba el "93/181" del resumen del
-- diagnostico y los ":" de algunos bins, y en Resolve esos canales se perdian.
local function nombreProhibido(n)
  if type(n) ~= "string" then return false end
  -- MOCK_SIN_PUNTO_MEDIO: una build que rechazara el punto medio de los nombres
  -- del reporte (Free 21.1.1.10 lo acepta; el aplicador tiene respaldo ASCII).
  if MOCK_SIN_PUNTO_MEDIO and string.find(n, "\194\183", 1, true) then return true end
  return string.find(n, '[/\\:%*%?"<>|]') ~= nil
end
local Timeline = {}
Timeline.__index = Timeline
function Timeline.new(name)
  -- `pistas` guarda los items POR pista (v0.3.0). Antes todo caia en una sola
  -- lista y GetItemListInTrack solo respondia a video/1: con eso era imposible
  -- comprobar que cada camara aterriza en SU pista, que es justo lo que hay
  -- que garantizar para poder cortar entre angulos.
  return setmetatable({name = name, items = {}, vtracks = 1, atracks = 1,
                       tnames = {}, pistas = {}, links = {}, markers = {}},
                      Timeline)
end
function Timeline:GetName() return self.name end
-- Resolve no admite dos timelines con el mismo nombre en un proyecto: SetName
-- devuelve false. El diagnostico renombra su resumen al terminar, asi que el
-- mock tiene que poder negarse igual que Resolve.
function Timeline:SetName(n)
  if type(n) ~= "string" or n == "" or nombreProhibido(n) then return false end
  for _, t in ipairs(proj.timelines) do
    if t ~= self and t.name == n then return false end
  end
  self.name = n
  return true
end
-- Marcadores en la REGLA de la timeline (no en un clip). Misma regla dura que
-- los de item: Resolve rechaza dos en el mismo frame y devuelve false. Sin
-- reproducirlo aqui, un marcador de bloque que pisa a otro se pierde en
-- silencio y nadie se entera hasta abrir la timeline (2026-08-07).
function Timeline:AddMarker(f, color, name, note, dur, cd)
  if self.markers[f] then RECHAZADOS = RECHAZADOS + 1; return false end
  -- MOCK_MARKER_MAX_FRAME: Resolve rechaza un marker fuera de lo que dura la
  -- timeline; el mock lo emula con un tope fijo.
  if MOCK_MARKER_MAX_FRAME and f > MOCK_MARKER_MAX_FRAME then return false end
  local rec = registrarMarker("timeline", self, f, color, name, note, dur, cd)
  self.markers[f] = {color = color, name = name, note = note, cd = cd,
                     duration = dur, customData = cd or "", rec = rec}
  PUESTOS = PUESTOS + 1
  POR_COLOR[color] = (POR_COLOR[color] or 0) + 1
  return true
end
-- Los asistente_*.lua leen m.cd; Resolve devuelve m.customData. El mock
-- entrega la tabla viva con los dos campos para no romper a ninguno.
function Timeline:GetMarkers() return self.markers end
function Timeline:GetMarkerByCustomData(cd)
  return marcasPorCustomData(self.markers, cd)
end
function Timeline:DeleteMarkerByCustomData(cd)
  return borrarPorCustomData(self.markers, cd)
end
function Timeline:GetSetting() return "24" end
-- Las pistas de subtitulo llevan su propia cuenta, que empieza en 0. Hasta el
-- 2026-10-05 una pista "subtitle" sumaba una de video y GetTrackCount("subtitle")
-- devolvia las de video (1 de entrada), asi que una prueba de
-- reel_subtitulado.lua pasaba aunque el script no abriera la pista ST.
function Timeline:GetTrackCount(t)
  if t == "audio" then return self.atracks end
  if t == "subtitle" then return self.stracks or 0 end
  return self.vtracks
end
function Timeline:AddTrack(t, opts)
  if t == "audio" then self.atracks = self.atracks + 1
  elseif t == "subtitle" then self.stracks = (self.stracks or 0) + 1
  else self.vtracks = self.vtracks + 1 end
  return true
end
function Timeline:SetTrackName(t, i, n) self.tnames[t .. i] = n; return true end
function Timeline:GetTrackName(t, i) return self.tnames[t .. i] or "" end
function Timeline:GetItemListInTrack(t, i)
  return self.pistas[t .. i] or {}
end
function Timeline:GetStartFrame() return 0 end
-- Linkeo: el mock lo soporta para ejercitar el camino. Que Resolve Free lo
-- exponga o no se mide aparte, con los spikes S7/S8 de spikes_v2.lua.
function Timeline:SetClipsLinked(items, linked)
  if type(items) ~= "table" or #items < 2 then return false end
  self.links[#self.links + 1] = {n = #items, linked = linked}
  return true
end
-- Un generador (Solid Color, etc.) cae en V1 como un item mas, sin clip real.
function Timeline:InsertGeneratorIntoTimeline(nombre)
  if type(nombre) ~= "string" or nombre == "" then return nil end
  local mpi = MediaPoolItem.new("generador:" .. nombre, nombre)
  local it = TimelineItem.new(mpi, #self.items * 100)
  it.track = "video1"
  self.pistas["video1"] = self.pistas["video1"] or {}
  self.pistas["video1"][#self.pistas["video1"] + 1] = it
  self.items[#self.items + 1] = it
  return it
end
-- Export: Resolve escribe el archivo desde fuera del Lua, asi que en Resolve
-- real existe aunque el script no tenga `io`. El mock solo puede simularlo
-- con `io`; en modo estricto no hay, y entonces solo contesta true. El test
-- de modo no estricto es el que comprueba que el archivo aparece.
function Timeline:Export(ruta, tipo, subtipo)
  if type(ruta) ~= "string" or tipo == nil then return false end
  MOCK_REGISTRO.exportes[#MOCK_REGISTRO.exportes + 1] =
    {timeline = self.name, ruta = ruta, tipo = tipo, subtipo = subtipo}
  if io and io.open then
    local f = io.open(ruta, "w")
    if not f then return false end
    f:write("mock export ", tostring(tipo), " ", self.name, "\n")
    f:close()
  end
  return true
end

local Folder = {}
Folder.__index = Folder
function Folder.new(name, clips, subs, padre)
  return setmetatable({name = name, clips = clips or {}, subs = subs or {},
                       padre = padre}, Folder)
end
function Folder:GetName() return self.name end
function Folder:GetClipList() return self.clips end
function Folder:GetSubFolderList() return self.subs end

local mpItems = {MediaPoolItem.new(V1, "ASA_0001.MP4"),
                 MediaPoolItem.new(V2, "ASA_0002.MP4"),
                 MediaPoolItem.new(W1, "VICG0001.MP4"),
                 MediaPoolItem.new(I1, "C0001.MP4"),
                 MediaPoolItem.new(A1, "00001.WAV"),
                 MediaPoolItem.new(A2, "00001b.WAV")}
local rootFolder = Folder.new("Master", mpItems, {})
if type(MOCK_POOL) == "table" then
  rootFolder = Folder.new("Master", {}, {})
  local bins = {}
  for _, c in ipairs(MOCK_POOL) do
    local it = MediaPoolItem.new(c.ruta, c.nombre or string.match(c.ruta, "([^/]+)$"))
    it.frames, it.duracion = c.frames, c.duracion
    local destino = rootFolder
    if c.bin then
      if not bins[c.bin] then
        bins[c.bin] = Folder.new(c.bin, {}, {}, rootFolder)
        rootFolder.subs[#rootFolder.subs + 1] = bins[c.bin]
      end
      destino = bins[c.bin]
    end
    destino.clips[#destino.clips + 1] = it
  end
end

local mp = {actual = nil}
function mp:GetRootFolder() return rootFolder end
-- La carpeta nueva se cuelga de su padre (como en Resolve) y queda en el
-- registro. Antes el mock devolvia una carpeta suelta: GetSubFolderList del
-- padre nunca la veia y no habia forma de comprobar que el bin existe.
function mp:AddSubFolder(padre, name)
  if type(name) ~= "string" or name == "" or nombreProhibido(name) then return nil end
  local p = (type(padre) == "table" and padre.subs) and padre or rootFolder
  local f = Folder.new(name, {}, {}, p)
  p.subs[#p.subs + 1] = f
  MOCK_REGISTRO.bins[#MOCK_REGISTRO.bins + 1] = f
  return f
end
function mp:SetCurrentFolder(f)
  if type(f) == "table" and f.clips then self.actual = f end
  return true
end
function mp:GetCurrentFolder() return self.actual or rootFolder end
-- Devuelve un item real por cada ruta pedida. La version anterior devolvia {}
-- siempre, asi que cualquier script que IMPORTE un archivo nuevo —y no solo
-- recorra el Media Pool ya poblado— moria en la primera linea util y el mock no
-- podia probarlo (reel_subtitulado.lua, 2026-08-07).
-- Si alguien fijo la carpeta actual con SetCurrentFolder, el clip cae ahi, como
-- en Resolve; si no, se queda suelto (comportamiento de siempre).
function mp:ImportMedia(rutas)
  local out = {}
  for _, ruta in ipairs(rutas or {}) do
    -- MOCK_MEDIA_FALTANTE = {[ruta] = true}: como Resolve con un archivo que
    -- no esta (disco sin montar), no devuelve nada por esa ruta.
    MOCK_REGISTRO.importados[#MOCK_REGISTRO.importados + 1] = ruta
    if not (MOCK_MEDIA_FALTANTE and MOCK_MEDIA_FALTANTE[ruta]) then
      local nombre = string.match(ruta, "([^/]+)$") or ruta
      local it = MediaPoolItem.new(ruta, nombre)
      out[#out + 1] = it
      if self.actual then self.actual.clips[#self.actual.clips + 1] = it end
    end
  end
  return out
end
-- Borra de verdad de la lista del proyecto: sin esto un test no puede
-- distinguir "se borro" de "se ignoro la orden".
function mp:DeleteTimelines(lista)
  if type(lista) ~= "table" then return false end
  MOCK_REGISTRO.borradas = MOCK_REGISTRO.borradas + #lista
  for _, victima in ipairs(lista) do
    for i = #proj.timelines, 1, -1 do
      if proj.timelines[i] == victima then table.remove(proj.timelines, i) end
    end
    if proj.current == victima then proj.current = nil end
  end
  return true
end
function mp:CreateEmptyTimeline(name)
  -- Resolve devuelve nil si ya existe una timeline con ese nombre, o si el
  -- nombre lleva un caracter prohibido.
  if nombreProhibido(name) then return nil end
  for _, t in ipairs(proj.timelines) do
    if t.name == name then return nil end
  end
  local tl = Timeline.new(name)
  proj.timelines[#proj.timelines + 1] = tl
  MOCK_REGISTRO.timelines[#MOCK_REGISTRO.timelines + 1] = tl
  proj.current = tl
  return tl
end
function mp:AppendToTimeline(arg)
  local tl = proj.current
  if not tl then return nil end
  local out = {}
  for _, x in ipairs(arg) do
    local esTabla = type(x) == "table" and x.mediaPoolItem ~= nil
    local mpi = esTabla and x.mediaPoolItem or x
    if type(mpi) == "table" and mpi.GetName then
      local tipo = esTabla and ((x.mediaType == 2) and "audio" or "video") or "video"
      local idx = esTabla and (x.trackIndex or 1) or 1
      local tambienAudio = not esTabla   -- append plano: Resolve baja video+audio
      -- Resolve no crea pistas al vuelo: si la pista no existe, no coloca.
      local hay = (tipo == "audio") and tl.atracks or tl.vtracks
      if idx > hay then
        out[#out + 1] = false
      else
        local clave = tipo .. idx
        tl.pistas[clave] = tl.pistas[clave] or {}
        local start = esTabla and x.recordFrame or (#tl.items * 100)
        local it = TimelineItem.new(mpi, start or 0)
        it.track = clave
        if esTabla and tonumber(x.startFrame) and tonumber(x.endFrame) then
          it.dur = tonumber(x.endFrame) - tonumber(x.startFrame) + 1
        elseif mpi.frames then
          it.dur = tonumber(mpi.frames)
        end
        tl.pistas[clave][#tl.pistas[clave] + 1] = it
        tl.items[#tl.items + 1] = it
        out[#out + 1] = it
        if tambienAudio then
          tl.pistas["audio1"] = tl.pistas["audio1"] or {}
          local ia = TimelineItem.new(mpi, start or 0)
          ia.track = "audio1"
          tl.pistas["audio1"][#tl.pistas["audio1"] + 1] = ia
        end
      end
    end
  end
  -- Resolve devuelve nil si no coloco nada.
  if #out == 0 or out[1] == false then return nil end
  return out
end
function mp:AutoSyncAudio() return true end
-- Mueve de verdad: saca el clip de la carpeta donde este y lo pone en el
-- destino. Asi se puede comprobar el efecto, no solo el `true`.
function mp:MoveClips(clips, destino)
  if type(clips) ~= "table" or type(destino) ~= "table" or not destino.clips then
    return true   -- compat: los asistente_*.lua lo llaman con mocks minimos
  end
  local function quitar(carpeta, clip)
    for i = #carpeta.clips, 1, -1 do
      if carpeta.clips[i] == clip then table.remove(carpeta.clips, i) end
    end
    for _, s in ipairs(carpeta.subs) do quitar(s, clip) end
  end
  for _, c in ipairs(clips) do
    quitar(rootFolder, c)
    destino.clips[#destino.clips + 1] = c
  end
  return true
end
function mp:ExportMetadata(ruta, clips)
  if type(ruta) ~= "string" then return false end
  if io and io.open then
    local f = io.open(ruta, "w")
    if not f then return false end
    f:write("File Name,Comments\n")
    for _, c in ipairs(clips or {}) do
      f:write(tostring(c.name), ",", tostring(c.meta and c.meta.Comments or ""), "\n")
    end
    f:close()
  end
  return true
end

proj = {timelines = {}, current = nil}
function proj:GetName() return MOCK_PROYECTO or "MOCK FULL" end
function proj:GetMediaPool() return mp end
function proj:GetTimelineCount() return #self.timelines end
function proj:GetTimelineByIndex(i) return self.timelines[i] end
function proj:SetCurrentTimeline(tl)
  if type(tl) ~= "table" then return false end
  self.current = tl
  return true
end
function proj:GetCurrentTimeline() return self.current end
function proj:GetSetting() return "24" end

local pm = {}
function pm:GetCurrentProject() return proj end
function pm:SaveProject()
  MOCK_REGISTRO.guardados = MOCK_REGISTRO.guardados + 1
  return true
end

-- ---------- fusion (prefs) -----------------------------------------------
-- AutoSubs usa fusion:SetPrefs/SavePrefs para sacar datos de Resolve Free
-- 21.1; el diagnostico lo mide como canal de regreso. El mock guarda las
-- prefs en una tabla anidada por los puntos de la clave, igual que Fusion.
local PREFS = {}
local function prefsCamino(clave)
  local partes = {}
  for p in string.gmatch(tostring(clave), "[^%.]+") do partes[#partes + 1] = p end
  return partes
end
-- Serializa como el Fusion.prefs real: una tabla Lua con claves desnudas y
-- cadenas entre comillas (%q), para que el lector de prefs se ejercite con el
-- mismo formato que va a encontrar en disco.
local function prefsTexto(t, sangria)
  local lineas = {"{"}
  local claves = {}
  for k in pairs(t) do claves[#claves + 1] = tostring(k) end
  table.sort(claves)
  for _, k in ipairs(claves) do
    local v = t[k]
    local pre = sangria .. "\t" .. k .. " = "
    if type(v) == "table" then
      lineas[#lineas + 1] = pre .. prefsTexto(v, sangria .. "\t") .. ","
    elseif type(v) == "string" then
      lineas[#lineas + 1] = pre .. string.format("%q", v) .. ","
    else
      lineas[#lineas + 1] = pre .. tostring(v) .. ","
    end
  end
  lineas[#lineas + 1] = sangria .. "}"
  return table.concat(lineas, "\n")
end

-- Referencia LOCAL: resolve:Fusion() devuelve esta, no el global. Asi un
-- runner que borra el global `fusion` (el menu real puede no tenerlo en el
-- entorno del diagnostico) sigue ejercitando el camino resolve:Fusion().
local fusionMock = {}
fusion = fusionMock
function fusion:SetPrefs(clave, valor)
  local partes = prefsCamino(clave)
  if #partes == 0 then return false end
  local t = PREFS
  for i = 1, #partes - 1 do
    if type(t[partes[i]]) ~= "table" then t[partes[i]] = {} end
    t = t[partes[i]]
  end
  t[partes[#partes]] = valor
  return true
end
function fusion:GetPrefs(clave)
  if clave == nil then return PREFS end
  local t = PREFS
  for _, p in ipairs(prefsCamino(clave)) do
    if type(t) ~= "table" then return nil end
    t = t[p]
  end
  return t
end
function fusion:SavePrefs()
  if MOCK_PREFS_PATH and io and io.open then
    local f = io.open(MOCK_PREFS_PATH, "w")
    if not f then return false end
    f:write(prefsTexto(PREFS, ""), "\n")
    f:close()
  end
  return true
end

resolve = {}
function resolve:GetProjectManager() return pm end
function resolve:GetVersionString() return "21.0.2 (mock)" end
function resolve:GetProductName() return "DaVinci Resolve (mock)" end
function resolve:Fusion() return fusionMock end
-- Constantes de exportacion: en Resolve son enteros opacos. El mock declara
-- las que el diagnostico pide; las que no declara cuentan como "no existe".
local k = 0
for _, nombre in ipairs({"EXPORT_AAF", "EXPORT_DRT", "EXPORT_EDL", "EXPORT_OTIO",
                         "EXPORT_FCPXML_1_10", "EXPORT_TEXT_CSV", "EXPORT_TEXT_TAB",
                         "EXPORT_NONE", "EXPORT_AAF_NEW", "EXPORT_AAF_EXISTING"}) do
  resolve[nombre] = k
  k = k + 1
end

-- ---------- helpers para otros runners ------------------------------------
MOCK = {}

-- Lo mas cerrado posible: lo que terceros reportan AUSENTE en el menu de la
-- build 17 (io, os, require, package, ffi, debug, arg) mas print, que alla no
-- se ve. Devuelve lo guardado por si un runner quiere restaurarlo.
--
-- Con opciones.extremo tambien quita lo que terceros dicen que SI hay pero
-- nadie ha medido aqui, y lo que el stub del menu no garantiza: fusion, fu,
-- bmd, loadstring, setfenv, getfenv, bit, jit, unpack, resolve y Resolve.
-- Si el diagnostico aguanta asi, no depende de ninguno de ellos. El runner
-- tiene que tomar `resolve` ANTES de llamar esto y pasarlo en ctx, como el
-- stub real.
local EXTREMO = {"fusion", "fu", "bmd", "loadstring", "setfenv", "getfenv", "bit",
                 "jit", "unpack", "resolve", "Resolve"}
function MOCK.aplicarSandbox(opciones)
  local guardado = {io = io, os = os, debug = debug, require = require,
                    package = package, ffi = ffi, arg = arg, print = print}
  io, os, debug, require, package, ffi, arg, print =
    nil, nil, nil, nil, nil, nil, nil, nil
  if type(opciones) == "table" and opciones.extremo then
    for _, nombre in ipairs(EXTREMO) do
      guardado[nombre] = _G[nombre]
      _G[nombre] = nil
    end
  end
  return guardado
end

-- Vuelca MOCK_REGISTRO en lineas con tabuladores, faciles de parsear desde
-- Python sin un codificador JSON en el runner:
--   @@TL  <nombre final> <viva:1|0>
--   @@MK  <donde> <duenio> <frame> <color> <name> <customData> <borrado:1|0>
--   @@BIN <padre> <nombre>
--   @@GUARDADOS <n>
function MOCK.volcar(emitir)
  emitir = emitir or _print
  local function viva(tl)
    for _, t in ipairs(proj.timelines) do if t == tl then return "1" end end
    return "0"
  end
  local function limpio(s) return (string.gsub(tostring(s or ""), "[\t\n]", " ")) end
  for _, tl in ipairs(MOCK_REGISTRO.timelines) do
    emitir("@@TL\t" .. limpio(tl.name) .. "\t" .. viva(tl))
  end
  for _, m in ipairs(MOCK_REGISTRO.markers) do
    local duenio = m.duenio and (m.duenio.name or (m.duenio.GetName and m.duenio:GetName())) or ""
    emitir("@@MK\t" .. m.donde .. "\t" .. limpio(duenio) .. "\t" .. tostring(m.frame)
      .. "\t" .. limpio(m.color) .. "\t" .. limpio(m.name) .. "\t" .. limpio(m.customData)
      .. "\t" .. (m.borrado and "1" or "0"))
  end
  for _, b in ipairs(MOCK_REGISTRO.bins) do
    emitir("@@BIN\t" .. limpio(b.padre and b.padre.name or "") .. "\t" .. limpio(b.name))
  end
  emitir("@@GUARDADOS\t" .. tostring(MOCK_REGISTRO.guardados))
end
MOCK.fusion = fusionMock
MOCK.proyecto = proj

-- ---------- interceptar la carga del <proyecto>_data.lua -----------------
-- Con MOCK_DOFILE_REAL no se intercepta: el kit de diagnostico trae un
-- `grande_data.lua` que hay que cargar de verdad para medir dofile.
local _dofile = dofile
if not MOCK_DOFILE_REAL then
  dofile = function(path)
    if type(path) == "string" and path:match("_data%.lua$") then
      return DATA_FALSA
    end
    if type(path) == "string" and path:match("_multicam%.lua$") then
      return MULTICAM_FALSO
    end
    return _dofile(path)
  end
end
MOCK.dofile_original = _dofile

-- Otro runner solo quiere el Resolve falso: se va antes de correr nada.
if MOCK_SOLO_MODELO then return MOCK end

-- ---------- correr -------------------------------------------------------
local target = arg and arg[1] or "asistente_fcc.lua"
_print("== MOCK FULL: " .. target .. (SANDBOX_ESTRICTO and " (sandbox estricto)" or "") .. " ==")
if SANDBOX_ESTRICTO then MOCK.aplicarSandbox() end
local ok, err = pcall(_dofile, target)
_print("")
_print(string.rep("=", 56))
if not ok then
  _print("FALLO: " .. tostring(err))
  _salir(1)
end
_print(string.format("markers colocados : %d", PUESTOS))
for color, n in pairs(POR_COLOR) do
  _print(string.format("   %-10s %d", color, n))
end
_print(string.format("markers RECHAZADOS por colision de frame: %d", RECHAZADOS))
local fallos = {}
if RECHAZADOS > 0 then
  fallos[#fallos+1] = "hubo markers rechazados: el asignador de frames no cubre "
    .. "todos los AddMarker y se pierden en silencio"
end

-- ---------- comprobaciones de estructura (v0.3.0) ------------------------
--
-- Solo aplican a los scripts que declaran el contrato v0.3.0 (los que definen
-- el global TIMELINES). Los asistente_*.lua de proyectos ya cerrados —FCC,
-- Fantastico, Avalanches, MAB— se quedaron en el contrato anterior a proposito:
-- crean TODO EL MATERIAL y ENTREVISTAS, mandan las companeras a V2 y no ligan.
-- Exigirles el contrato nuevo seria romper por un cambio que no les toca.
local CONTRATO_V3 = (TIMELINES ~= nil)

local function tlPorNombre(n)
  for _, tl in ipairs(proj.timelines) do
    if tl:GetName() == n then return tl end
  end
  return nil
end

_print("")
_print("Estructura:")
for _, tl in ipairs(proj.timelines) do
  local v, a = {}, {}
  for i = 1, tl.vtracks do v[#v+1] = string.format("V%d:%d", i, #(tl.pistas["video"..i] or {})) end
  for i = 1, tl.atracks do a[#a+1] = string.format("A%d:%d", i, #(tl.pistas["audio"..i] or {})) end
  _print(string.format("  %-38s %s | %s | links:%d", tl:GetName(),
    table.concat(v, " "), table.concat(a, " "), #tl.links))
end

-- 1. No deben existir las timelines retiradas.
if CONTRATO_V3 then
for _, n in ipairs({"TODO EL MATERIAL", "ENTREVISTAS"}) do
  for _, tl in ipairs(proj.timelines) do
    if string.find(tl:GetName(), n, 1, true) then
      fallos[#fallos+1] = "se creo la timeline retirada: " .. tl:GetName()
    end
  end
end
end

-- 2. Cada camara companera en SU pista fija. Con dos companeras sobre la misma
--    base, una tiene que estar en V2 y la otra en V3 — nunca las dos en V2.
if CONTRATO_V3 then
  local tl = tlPorNombre("MORSA — A-ROLL") or tlPorNombre("MOCK — A-ROLL")
  for _, t in ipairs(proj.timelines) do
    if string.find(t:GetName(), "A%-ROLL$") then tl = t end
  end
  if tl then
    local n2 = #(tl.pistas["video2"] or {})
    local n3 = #(tl.pistas["video3"] or {})
    if n2 + n3 < 2 then
      fallos[#fallos+1] = string.format(
        "A-ROLL con %d angulos colocados; se esperaban 2 (V2:%d V3:%d)",
        n2 + n3, n2, n3)
    elseif n2 ~= 1 or n3 ~= 1 then
      fallos[#fallos+1] = string.format(
        "las dos companeras no quedaron en pistas distintas (V2:%d V3:%d) — la "
        .. "pista por camara no es fija", n2, n3)
    end
    local a2 = #(tl.pistas["audio2"] or {})
    local a3 = #(tl.pistas["audio3"] or {})
    if a2 ~= 1 or a3 ~= 1 then
      fallos[#fallos+1] = string.format(
        "el audio de cada companera debe ir en SU pista (A2:%d A3:%d)", a2, a3)
    end
  end
end

-- 2b. El orden de pistas respeta track_order (cantidad de material), no el
--     alfabetico. Con Vic e Iban, alfabetico daria Iban=V2 y Vic=V3.
if CONTRATO_V3 then
  local tl
  for _, t in ipairs(proj.timelines) do
    if string.find(t:GetName(), "A%-ROLL$") then tl = t end
  end
  if tl then
    local n2 = tl.pistas["video2"] or {}
    local n3 = tl.pistas["video3"] or {}
    local en2 = n2[1] and n2[1]:GetName() or "?"
    local en3 = n3[1] and n3[1]:GetName() or "?"
    if en2 ~= "VICG0001.MP4" then
      fallos[#fallos+1] = "V2 deberia llevar a Vic (mas material que Iban); lleva "
        .. en2
    end
    if en3 ~= "C0001.MP4" then
      fallos[#fallos+1] = "V3 deberia llevar a Iban (la de menos material); lleva "
        .. en3
    end
  end
end

-- 3. El linkeo se intento (base + angulo + su audio).
if CONTRATO_V3 then
  local total = 0
  for _, tl in ipairs(proj.timelines) do total = total + #tl.links end
  if total == 0 then
    fallos[#fallos+1] = "no se ligo ningun grupo: los angulos no se moveran con su base"
  else
    _print(string.format("  grupos ligados: %d", total))
  end
end

if #fallos > 0 then
  _print("")
  for _, f in ipairs(fallos) do _print("FALLO: " .. f) end
  _salir(1)
end
_print("OK")
