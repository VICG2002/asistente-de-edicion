-- ============================================================
--  aplicar.lua — el aplicador del menu de Resolve (plan Free, §3)
--
--  Lo lanza el stub "Diez50 Aplicar" (Workspace > Scripts > Utility), que lo
--  carga con dofile y llama:
--    A.correr({ ctx = "menu_utility", resolve = R,
--               buzon = "<HOME>/Library/Application Support/Diez50/buzon.lua",
--               errores = "<HOME>/Library/Application Support/Diez50/errores/",
--               motor = "<carpeta de aplicar.lua>/" })
--  Un error (E01, E02...) se exporta ademas a <errores>/error_<proyecto>.drt.
--  En la Consola es la misma llamada en una linea, con `decir = print`.
--
--  Es un MODULO: cargarlo no hace nada (asi se prueba con el mock). Las rutas
--  llegan en ctx porque en el menu de Free 21.1 no hay `debug` para ubicarse,
--  ni `io` u `os` (medido el 2026-10-05 en 21.1.0.17 y 21.1.1.10). Este
--  archivo no usa io, os, require, debug ni print: lo vigila la regla 5 del lint.
--
--  Que hace
--  --------
--  1. Lee el buzon con loadfile en un entorno vacio: solo datos. Lo escribe
--     bin/pedido.py (.tmp + rename); nadie lo edita a mano.
--  2. Toma el pedido del proyecto abierto (proj:GetName()). Sin pedido, una
--     timeline "Diez50 · ERROR · E02 · ...".
--  3. Si ya existe el reporte de ese sello, no hace nada (salvo
--     politica.reaplicar): un doble clic no construye dos veces.
--  4. Corre las acciones EN ORDEN, cada una en su pcall.
--  5. Deja la timeline de reporte "Diez50 · <sello_corto> · <estado> · <n> de <m>",
--     con un marker por aviso, y la exporta a <recibos>/reporte.drt. Ese .drt
--     es el recibo: el canal de regreso elegido el 2026-10-05 es
--     Timeline:Export, que escribe el archivo desde el menu de Free aunque Lua
--     no tenga io.
--
--  El sello va en dos sitios legibles desde fuera: el nombre del reporte y un
--  marker en el frame 0 de cada timeline construida, con
--  customData "diez50:sello=<sello>" (en el reporte, "diez50:reporte=<sello>").
--
--  Acciones que conoce (contrato del pedido, formato 2):
--    prueba_regreso  una timeline con un orden de pistas conocido. Es la
--                    prueba de ida y vuelta de la Fase 0 y queda como prueba
--                    del canal tras cada update de Resolve.
--    construir       las timelines de un proyecto documental (A-ROLL,
--                    B-ROLL, AUDIOS EXTERNOS) desde su horneado. Vive en
--                    construir.lua, que se carga de ctx.motor junto con
--                    asistente_lib.lua (Fase 1, 2026-10-06).
--    exportar        Timeline:Export a .drt de lo que construyeron las
--                    acciones anteriores, a <recibos>/<clave>.drt.
--  Una accion que no conoce no se salta en silencio: cuenta como no hecha y el
--  reporte sale en ERROR.
--
--  Que NUNCA hace
--  --------------
--  Borrar timelines, clips o bins. Una timeline que se va a reconstruir y ya
--  existe se renombra a "<nombre> · anterior <sello_corto>". Borrarla lo
--  decide el editor.
--
--  Lua 5.1 / LuaJIT (el de Resolve) y 5.5 (el de las pruebas).
-- ============================================================

local A = {}
A.FORMATO = 2
A.PREFIJO = "Diez50"
-- " · " en UTF-8, con escapes para que el archivo siga siendo ASCII. Si
-- Resolve rechazara el punto medio, crearTimeline cae a " - ".
A.SEP = " \194\183 "
A.SEP_ASCII = " - "
-- Resolve rechaza / \ : * ? " < > | en nombres de timeline y de bin, sin dar
-- error: CreateEmptyTimeline devuelve nil y SetName false (LANG-11, Studio
-- 21.1.0.17 y Free 21.1.0.17 y 21.1.1.10). Todo nombre pasa por nombreSeguro.
A.PROHIBIDOS = '[/\\:%*%?"<>|]'
A.CD_SELLO = "diez50:sello="
A.CD_REPORTE = "diez50:reporte="
-- Lo que lib/regreso.py busca en la nota del marker del reporte.
A.SIN_MARKER = " | sin marker: "
A.CODIGOS = {
  E01 = "buzon ilegible",
  E02 = "sin pedido para este proyecto",
  E03 = "disco no montado",
  E04 = "datos ilegibles",
  E05 = "fallo la API",
}
A.COLOR = {info = "Blue", aviso = "Yellow", error = "Red",
           sello = "Cream", reporte = "Cream"}

-- ---------- utilidades puras ---------------------------------------------

local function nombreSeguro(s)
  return (string.gsub(tostring(s), A.PROHIBIDOS, "_"))
end
A.nombreSeguro = nombreSeguro

-- Una linea, sin '|' (separa campos en el customData) ni caracteres de control.
local function limpio(s)
  s = string.gsub(tostring(s or ""), "%c", " ")
  return (string.gsub(s, "|", "/"))
end

local function selloCorto(sello)
  return string.sub(tostring(sello or ""), -4)
end
A.selloCorto = selloCorto

local function carpeta(ruta)
  if string.sub(ruta, -1) == "/" then return ruta end
  return ruta .. "/"
end

local function empieza(s, prefijo)
  return string.sub(tostring(s or ""), 1, #prefijo) == prefijo
end

-- Llama un metodo dentro de pcall. Devuelve (true, resultado) o (false, por que).
local function llamar(obj, metodo, ...)
  if obj == nil then return false, "objeto nil para " .. metodo end
  local okf, f = pcall(function() return obj[metodo] end)
  if not okf or f == nil then return false, metodo .. " no existe" end
  local r = {pcall(f, obj, ...)}
  if not r[1] then return false, tostring(r[2]) end
  return true, r[2]
end

-- Las constantes viven en el objeto resolve (resolve.EXPORT_DRT). Si el binding
-- fabrica stubs, una constante inexistente llega como funcion: eso no cuenta.
local function constante(R, nombre)
  local ok, v = pcall(function() return R[nombre] end)
  if ok and v ~= nil and type(v) ~= "function" then return v end
  return nil
end

-- ---------- el buzon ------------------------------------------------------

-- loadfile en un entorno vacio: el buzon solo puede devolver datos. En 5.5 el
-- entorno va en loadfile; en 5.1/LuaJIT, con setfenv. Se intentan los dos.
local function cargarDatos(ruta)
  if type(ruta) ~= "string" or ruta == "" then return nil, "sin ruta" end
  local env = {}
  local chunk, err = loadfile(ruta, "t", env)
  if not chunk then return nil, tostring(err) end
  if setfenv then pcall(setfenv, chunk, env) end
  local ok, datos = pcall(chunk)
  if not ok then return nil, tostring(datos) end
  if type(datos) ~= "table" then return nil, "no devolvio una tabla" end
  return datos
end
A.cargarDatos = cargarDatos

-- ---------- timelines -----------------------------------------------------

local function listaTimelines(proj)
  local out = {}
  local ok, n = llamar(proj, "GetTimelineCount")
  for i = 1, (ok and tonumber(n) or 0) do
    local okt, tl = llamar(proj, "GetTimelineByIndex", i)
    if okt and tl then out[#out + 1] = tl end
  end
  return out
end

local function nombreDe(tl)
  local ok, n = llamar(tl, "GetName")
  return ok and tostring(n) or ""
end

local function porNombre(proj, nombre)
  for _, tl in ipairs(listaTimelines(proj)) do
    if nombreDe(tl) == nombre then return tl end
  end
  return nil
end

local function alterno(nombre)
  return (string.gsub(nombre, A.SEP, A.SEP_ASCII))
end

-- La timeline con ese nombre o, si Resolve rechazo el punto medio y se creo
-- con ' - ', con esa version. Devuelve tambien el separador que lleva, para
-- renombrarla con uno que Resolve acepte. Sin esto, el segundo pedido no
-- encontraba la anterior y CreateEmptyTimeline se negaba porque el nombre
-- ASCII ya existia (revision del PR #1, 2026-10-05).
local function existente(proj, nombre)
  local tl = porNombre(proj, nombre)
  if tl then return tl, A.SEP end
  if string.find(nombre, A.SEP, 1, true) then
    tl = porNombre(proj, alterno(nombre))
    if tl then return tl, A.SEP_ASCII end
  end
  return nil, A.SEP
end

-- Nombre de archivo de un proyecto: solo ASCII alfanumerico, '-' y '_'; todo
-- otro byte (acentos incluidos) es '_'. lib.pedido.archivo_de_proyecto hace lo
-- mismo byte a byte, para que el motor encuentre el archivo.
local function archivoDeProyecto(proyecto)
  return (string.gsub(tostring(proyecto), "[^%w%-_]", "_"))
end
A.archivoDeProyecto = archivoDeProyecto

-- El customData del marker del frame 0 que empieza con `prefijo`, o nil.
local function customEnCero(tl, prefijo)
  local ok, ms = llamar(tl, "GetMarkers")
  if not ok or type(ms) ~= "table" then return nil end
  for f, m in pairs(ms) do
    if tonumber(f) == 0 and type(m) == "table" then
      local cd = tostring(m.customData or m.cd or "")
      if empieza(cd, prefijo) then return cd end
    end
  end
  return nil
end
A.customEnCero = customEnCero

-- Crea una timeline. Si el nombre lleva el punto medio y Resolve lo rechaza
-- (y no es porque ya exista), reintenta con " - " y lo anota.
local function crearTimeline(E, nombre)
  local ok, tl = llamar(E.mp, "CreateEmptyTimeline", nombre)
  if ok and tl then return tl, nombre end
  if string.find(nombre, A.SEP, 1, true) then
    local alt = alterno(nombre)
    if not porNombre(E.proj, alt) then
      ok, tl = llamar(E.mp, "CreateEmptyTimeline", alt)
      if ok and tl then
        E.anotar("aviso", "Resolve rechazo el punto medio en '" .. nombre
                 .. "'; se uso ' - '")
        return tl, alt
      end
    end
  end
  return nil, nombre
end

-- Si ya hay una timeline con ese nombre, la aparta renombrandola. Nunca borra.
-- Devuelve el nombre con el que hay que crear la nueva.
local function apartarAnterior(E, nombre, prefijoCd)
  local viejo, sep = existente(E.proj, nombre)
  if not viejo then return nombre end
  local sc = "sin sello"
  local cd = customEnCero(viejo, prefijoCd)
  if cd then sc = selloCorto(string.sub(cd, #prefijoCd + 1)) end
  if E.politica.anteriores == "conservar" then
    -- La anterior se queda con su nombre; la nueva lleva el sello.
    local nuevo, k = nombre .. A.SEP .. selloCorto(E.sello), 1
    while porNombre(E.proj, nuevo) do
      k = k + 1
      nuevo = nombre .. A.SEP .. selloCorto(E.sello) .. " " .. k
    end
    E.anotar("info", "'" .. nombre .. "' ya existia y se conserva; la nueva es '" .. nuevo .. "'")
    return nuevo
  end
  local base = nombreDe(viejo) .. sep .. "anterior " .. sc
  local destino, k = base, 1
  while porNombre(E.proj, destino) do
    k = k + 1
    destino = base .. " " .. k
  end
  local ok, res = llamar(viejo, "SetName", destino)
  if ok and res then
    E.anotar("info", "la timeline anterior se renombro a '" .. destino .. "'")
    return nombre
  end
  error("no se pudo apartar la timeline anterior '" .. nombre .. "': SetName devolvio "
        .. tostring(res), 0)
end

local function marcarSello(E, tl, nombre, cd)
  local ok, res = llamar(tl, "AddMarker", 0, A.COLOR.sello, "Diez50 sello",
                         "pedido " .. E.sello, 1, cd)
  if not (ok and res) then
    E.anotar("error", "el marker del sello no entro en '" .. nombre .. "': " .. tostring(res))
    return false
  end
  return true
end

-- ---------- media ---------------------------------------------------------

-- Ruta -> clip de todo el Media Pool, armado UNA vez por corrida. Buscar cada
-- ruta recorriendo el pool entero eran rutas x clips llamadas al API, y en el
-- menu de Free cada llamada cuesta (revision del PR #1, 2026-10-05).
--
-- Deja tambien E.pool: cada clip con su ruta (o nil) y la ruta del bin donde
-- vive. Es el UNICO recorrido del pool del aplicador: construir.lua lo usa en
-- vez de tener el suyo, y si una accion necesita uno fresco (despues de
-- importar), pone E.porRuta en nil y vuelve a llamar (revision del 2026-10-07:
-- con dos recorridos, construir dejaba un indice parcial que apagaba este).
local function indexarPool(E)
  if E.porRuta and E.pool then return end
  E.porRuta, E.pool = {}, {}
  local function recorrer(folder, ruta, prof)
    if prof > 30 then return end
    local okn, nombre = llamar(folder, "GetName")
    local aqui = ruta .. "/" .. tostring(okn and nombre or "")
    local okc, clips = llamar(folder, "GetClipList")
    for _, c in ipairs((okc and type(clips) == "table") and clips or {}) do
      local okp, fp = llamar(c, "GetClipProperty", "File Path")
      fp = (okp and type(fp) == "string" and fp ~= "") and fp or nil
      if fp and not E.porRuta[fp] then E.porRuta[fp] = c end
      E.pool[#E.pool + 1] = {clip = c, fpath = fp, binpath = aqui}
    end
    local oks, subs = llamar(folder, "GetSubFolderList")
    for _, s in ipairs((oks and type(subs) == "table") and subs or {}) do
      recorrer(s, aqui, prof + 1)
    end
  end
  local okr, root = llamar(E.mp, "GetRootFolder")
  if okr and root then recorrer(root, "", 0) end
end

-- ImportMedia no se repite: primero se busca el clip por ruta en el Media Pool.
local function clipDe(E, ruta)
  indexarPool(E)
  local c = E.porRuta[ruta]
  if not c then
    local ok, res = llamar(E.mp, "ImportMedia", {ruta})
    c = ok and type(res) == "table" and res[1] or nil
    if c then E.porRuta[ruta] = c end
  end
  return c
end

-- Lo que una accion que vive en otro archivo (construir.lua) necesita del
-- aplicador. Una sola implementacion de "nunca borrar", del sello y de la
-- busqueda de media: si construir.lua tuviera las suyas, divergirian.
A.util = {
  llamar = llamar, nombreSeguro = nombreSeguro, selloCorto = selloCorto,
  cargarDatos = cargarDatos, listaTimelines = listaTimelines, nombreDe = nombreDe,
  porNombre = porNombre, customEnCero = customEnCero, crearTimeline = crearTimeline,
  apartarAnterior = apartarAnterior, marcarSello = marcarSello,
  indexarPool = indexarPool, clipDe = clipDe, carpeta = carpeta,
}

-- ---------- acciones ------------------------------------------------------

local ACCIONES = {}
A.ACCIONES = ACCIONES

-- Una timeline con pistas nombradas y un clip por pista, todos en el arranque.
--   { tipo = "prueba_regreso", clave = "prueba", nombre = "...",
--     media  = { cam_a = "/ruta.mov", lava = "/ruta.wav" },
--     pistas = { video = { {nombre = "CAM A", media = "cam_a"} },
--                audio = { {nombre = "CAM A", media = "cam_a"},
--                          {nombre = "LAVA izq", media = "lava", tipo = "mono"} } } }
-- La pista A1 ya existe con el tipo del proyecto; las demas se crean con el
-- suyo. El orden lo decide el pedido; aqui solo se cumple y se comprueba.
function ACCIONES.prueba_regreso(E, a)
  if type(a.pistas) ~= "table" or type(a.media) ~= "table" then
    return false, "prueba_regreso sin pistas o sin media"
  end
  local clave = tostring(a.clave or "prueba")
  local nombre = nombreSeguro(a.nombre or (A.PREFIJO .. A.SEP .. "prueba de regreso"))

  -- Primero la media: si un archivo no entra, no se crea nada a medias.
  for k, ruta in pairs(a.media) do
    if not clipDe(E, ruta) then
      return false, "E03 no se pudo importar " .. tostring(k) .. " (" .. tostring(ruta)
                    .. "); disco sin montar o archivo movido"
    end
  end

  nombre = apartarAnterior(E, nombre, A.CD_SELLO)
  local tl, final = crearTimeline(E, nombre)
  if not tl then return false, "E05 CreateEmptyTimeline devolvio nil para '" .. nombre .. "'" end
  llamar(E.proj, "SetCurrentTimeline", tl)
  local okI, inicio = llamar(tl, "GetStartFrame")
  inicio = okI and tonumber(inicio) or 0

  local fallas = {}
  for _, tipo in ipairs({"video", "audio"}) do
    for i, p in ipairs(a.pistas[tipo] or {}) do
      local _, n = llamar(tl, "GetTrackCount", tipo)
      if i > (tonumber(n) or 0) then
        local okA, res
        if tipo == "audio" then
          okA, res = llamar(tl, "AddTrack", "audio", p.tipo or "stereo")
        else
          okA, res = llamar(tl, "AddTrack", "video")
        end
        if not (okA and res) then
          fallas[#fallas + 1] = string.format("AddTrack %s %d devolvio %s", tipo, i, tostring(res))
        end
      end
      local okN, resN = llamar(tl, "SetTrackName", tipo, i, p.nombre)
      if not (okN and resN) then
        fallas[#fallas + 1] = string.format("SetTrackName %s %d devolvio %s", tipo, i, tostring(resN))
      end
      local clip = a.media[p.media] and E.porRuta[a.media[p.media]] or nil
      if not clip then
        fallas[#fallas + 1] = string.format("%s %d: media '%s' no esta en el pedido",
                                            tipo, i, tostring(p.media))
      else
        local okP, res = llamar(E.mp, "AppendToTimeline", {{
          mediaPoolItem = clip, mediaType = (tipo == "video") and 1 or 2,
          trackIndex = i, recordFrame = inicio}})
        if not (okP and type(res) == "table" and res[1]) then
          fallas[#fallas + 1] = string.format("AppendToTimeline %s %d devolvio %s",
                                              tipo, i, tostring(res))
        end
      end
    end
  end

  -- Lo que se compara es lo que QUEDO en la timeline, no lo que se pidio:
  -- en la 21.1.0.17 Free perdio 1 de 500 AppendToTimeline sin error.
  for _, tipo in ipairs({"video", "audio"}) do
    local etq = (tipo == "video") and "V" or "A"
    for i, p in ipairs(a.pistas[tipo] or {}) do
      local okL, items = llamar(tl, "GetItemListInTrack", tipo, i)
      local n = (okL and type(items) == "table") and #items or 0
      if n < 1 then
        fallas[#fallas + 1] = string.format("%s%d (%s) quedo vacia", etq, i, tostring(p.nombre))
      end
      local _, puesto = llamar(tl, "GetTrackName", tipo, i)
      if tostring(puesto) ~= tostring(p.nombre) then
        fallas[#fallas + 1] = string.format("%s%d se llama '%s' y no '%s'", etq, i,
                                            tostring(puesto), tostring(p.nombre))
      end
    end
  end

  local selloOk = marcarSello(E, tl, final, A.CD_SELLO .. E.sello)
  E.construidas[#E.construidas + 1] = {clave = clave, tl = tl, nombre = final}
  if #fallas > 0 then return false, table.concat(fallas, "; ") end
  if not selloOk then return false, "sin marker de sello" end
  return true, final
end

-- Las timelines de un proyecto documental desde su horneado. La logica vive en
-- construir.lua y se apoya en asistente_lib.lua; los dos se cargan aqui, de
-- la carpeta del motor que dio el stub (ctx.motor), porque en el menu de Free
-- no hay `debug` para que aplicar.lua sepa donde esta.
function ACCIONES.construir(E, a)
  local motor = E.ctx.motor
  if type(motor) ~= "string" or motor == "" then
    return false, "E00 el stub no dio la carpeta del motor (ctx.motor): "
                  .. "vuelve a escribirlo con bin/pedido.py stub"
  end
  local okL, LIB = pcall(dofile, carpeta(motor) .. "asistente_lib.lua")
  if not okL or type(LIB) ~= "table" then
    return false, "E00 no cargo asistente_lib.lua: " .. tostring(LIB)
  end
  local okC, C = pcall(dofile, carpeta(motor) .. "construir.lua")
  if not okC or type(C) ~= "table" or type(C.correr) ~= "function" then
    return false, "E00 no cargo construir.lua: " .. tostring(C)
  end
  return C.correr(E, a, {A = A, LIB = LIB})
end

-- Timeline:Export a .drt de lo construido en este pedido. Solo cuenta lo que
-- Export devuelve aqui; que el archivo exista y se lea lo comprueba el motor.
function ACCIONES.exportar(E, a)
  if type(E.rutas.recibos) ~= "string" or E.rutas.recibos == "" then
    return false, "sin rutas.recibos: no hay donde exportar"
  end
  if #E.construidas == 0 then
    return false, "nada que exportar: ninguna accion anterior construyo una timeline"
  end
  local c = constante(E.R, "EXPORT_DRT")
  if c == nil then return false, "E05 resolve.EXPORT_DRT no existe" end
  for _, f in ipairs(a.formatos or {"drt"}) do
    if f ~= "drt" then E.anotar("aviso", "formato de export '" .. tostring(f) .. "' no soportado: solo drt") end
  end
  local fallas = {}
  for _, t in ipairs(E.construidas) do
    llamar(E.proj, "SetCurrentTimeline", t.tl)
    local ruta = carpeta(E.rutas.recibos) .. t.clave .. ".drt"
    local ok, res = llamar(t.tl, "Export", ruta, c)
    if not (ok and res) then
      fallas[#fallas + 1] = "Export de " .. t.clave .. " devolvio " .. tostring(res)
    end
  end
  if #fallas > 0 then return false, table.concat(fallas, "; ") end
  return true, #E.construidas .. " drt"
end

-- ---------- reporte y errores --------------------------------------------

local function exportarA(E, tl, ruta)
  local c = constante(E.R, "EXPORT_DRT")
  if c == nil then return false end
  llamar(E.proj, "SetCurrentTimeline", tl)
  local ok, res = llamar(tl, "Export", ruta, c)
  return ok and res and true or false
end

-- Timeline vacia con el error en el nombre. Si ya existe (otro clic con el
-- mismo error), solo se abre. Si el stub dio una carpeta de errores, se
-- exporta ahi para que el motor se entere sin que nadie mire Resolve.
local function timelineError(E, codigo, msg)
  local res = {ok = false, codigo = codigo, error = msg}
  if not E.mp then return res end
  local nombre = nombreSeguro(A.PREFIJO .. A.SEP .. "ERROR" .. A.SEP .. codigo .. A.SEP
                              .. string.sub(limpio(msg), 1, 60))
  local tl = existente(E.proj, nombre)
  if not tl then tl, nombre = crearTimeline(E, nombre) end
  if tl then
    llamar(E.proj, "SetCurrentTimeline", tl)
    res.timeline = nombreDe(tl)
    -- Un archivo por proyecto: con uno solo para todos, el motor le atribuia
    -- al pedido de un proyecto el error de otro (revision del PR #1).
    if type(E.ctx.errores) == "string" and E.ctx.errores ~= "" and E.proyecto then
      res.exportado = exportarA(E, tl, carpeta(E.ctx.errores) .. "error_"
                                .. archivoDeProyecto(E.proyecto) .. ".drt")
    end
  end
  E.decir("Diez50 ERROR " .. codigo .. ": " .. msg)
  return res
end

local function estadoDe(E)
  if E.hechas < E.total then return "ERROR" end
  for _, x in ipairs(E.anotaciones) do
    if x.nivel == "error" then return "ERROR" end
  end
  for _, x in ipairs(E.anotaciones) do
    if x.nivel == "aviso" then return "AVISOS" end
  end
  return "OK"
end

local function escribirReporte(E, estado)
  local nombre = nombreSeguro(A.PREFIJO .. A.SEP .. selloCorto(E.sello) .. A.SEP .. estado
                              .. A.SEP .. E.hechas .. " de " .. E.total)
  local okA, apartado = pcall(apartarAnterior, E, nombre, A.CD_REPORTE)
  if not okA then return nil, tostring(apartado) end
  local tl, final = crearTimeline(E, apartado)
  if not tl then return nil, "CreateEmptyTimeline devolvio nil para el reporte" end
  llamar(E.proj, "SetCurrentTimeline", tl)
  -- Un generador da a la timeline una duracion donde colgar los markers.
  local paso, dur = 24, nil
  local okG, gen = llamar(tl, "InsertGeneratorIntoTimeline", "Solid Color")
  if okG and gen then
    local _, d = llamar(gen, "GetDuration")
    dur = tonumber(d)
    if dur and dur > 0 then
      paso = math.max(1, math.min(24, math.floor(dur / (#E.anotaciones + 2))))
    else
      dur = nil
    end
  end
  -- Las anotaciones van primero y el marker del frame 0 al final: si alguna no
  -- entra como marker (fuera del generador, o sin generador), su texto viaja en
  -- la nota del frame 0, que es la que el motor lee siempre. Antes se perdian
  -- sin aviso (revision del PR #1, 2026-10-05).
  local perdidas = {}
  for i, x in ipairs(E.anotaciones) do
    local f = i * paso
    local ok, res = false, nil
    if not dur or f < dur then
      ok, res = llamar(tl, "AddMarker", f, A.COLOR[x.nivel] or "Yellow",
                       string.upper(x.nivel) .. " " .. i, x.texto, 1,
                       "diez50:" .. x.nivel .. "=" .. limpio(x.texto))
    end
    if not (ok and res) then
      perdidas[#perdidas + 1] = string.upper(x.nivel) .. " " .. x.texto
    end
  end
  local nota = string.format("pedido %s, motor %s, %d de %d acciones", E.sello,
                             tostring(E.buzon.motor_version or "?"), E.hechas, E.total)
  if #perdidas > 0 then
    nota = nota .. A.SIN_MARKER .. string.sub(table.concat(perdidas, " / "), 1, 800)
  end
  llamar(tl, "AddMarker", 0, A.COLOR.reporte, "Diez50 reporte", nota, 1, A.CD_REPORTE .. E.sello)
  local exportado = nil
  if type(E.rutas.recibos) == "string" and E.rutas.recibos ~= "" then
    exportado = exportarA(E, tl, carpeta(E.rutas.recibos) .. "reporte.drt")
  end
  return tl, final, exportado
end

-- ---------- correr --------------------------------------------------------

function A.correr(ctx)
  ctx = ctx or {}
  local E = {ctx = ctx, anotaciones = {}, construidas = {},
             hechas = 0, total = 0, politica = {}, rutas = {}}
  E.decir = type(ctx.decir) == "function" and ctx.decir or function() end
  function E.anotar(nivel, texto)
    E.anotaciones[#E.anotaciones + 1] = {nivel = nivel, texto = limpio(texto)}
    E.decir("  [" .. nivel .. "] " .. tostring(texto))
  end

  E.R = ctx.resolve
  local ok
  ok, E.pm = llamar(E.R, "GetProjectManager")
  if ok and E.pm then ok, E.proj = llamar(E.pm, "GetCurrentProject") end
  if not E.proj then
    -- Sin proyecto no hay donde dejar nada visible: lo dice el CLI antes.
    return {ok = false, codigo = "E05", error = "sin proyecto abierto"}
  end
  ok, E.mp = llamar(E.proj, "GetMediaPool")
  if not E.mp then return {ok = false, codigo = "E05", error = "sin Media Pool"} end
  local proyecto = nombreDe(E.proj)
  E.proyecto = proyecto

  local buzon, err = cargarDatos(ctx.buzon)
  if not buzon then return timelineError(E, "E01", "buzon ilegible " .. tostring(err)) end
  if buzon.formato ~= A.FORMATO then
    return timelineError(E, "E01", "buzon en formato " .. tostring(buzon.formato)
                         .. ", este aplicador entiende el " .. A.FORMATO)
  end
  E.buzon = buzon
  local pedido = type(buzon.pedidos) == "table" and buzon.pedidos[proyecto] or nil
  if type(pedido) ~= "table" then
    return timelineError(E, "E02", "sin pedido para " .. proyecto)
  end
  if type(pedido.sello) ~= "string" or not string.match(pedido.sello, "^%d%d%d%d%d%d%d%d%-%d%d%d%d%-%w%w%w%w$") then
    return timelineError(E, "E01", "pedido sin sello valido")
  end
  if type(pedido.acciones) ~= "table" or #pedido.acciones == 0 then
    return timelineError(E, "E01", "pedido sin acciones")
  end
  E.sello = pedido.sello
  E.pedido = pedido
  E.politica = type(pedido.politica) == "table" and pedido.politica or {}
  if E.politica.anteriores ~= "conservar" then E.politica.anteriores = "renombrar" end
  E.rutas = type(pedido.rutas) == "table" and pedido.rutas or {}
  E.decir("Diez50 Aplicar: " .. proyecto .. ", pedido " .. E.sello)

  -- Idempotencia: el reporte de este sello ya existe.
  if not E.politica.reaplicar then
    local cd = A.CD_REPORTE .. E.sello
    for _, tl in ipairs(listaTimelines(E.proj)) do
      if empieza(nombreDe(tl), A.PREFIJO) and customEnCero(tl, cd) == cd then
        llamar(E.proj, "SetCurrentTimeline", tl)
        E.decir("pedido ya aplicado: " .. nombreDe(tl))
        return {ok = true, ya_aplicado = true, sello = E.sello, reporte = nombreDe(tl)}
      end
    end
  end

  E.total = #pedido.acciones
  for i, a in ipairs(pedido.acciones) do
    local tipo = type(a) == "table" and tostring(a.tipo) or "?"
    local f = ACCIONES[tipo]
    if not f then
      E.anotar("error", string.format("accion %d '%s': este aplicador no la conoce", i, tipo))
    else
      local okP, hecho, detalle = pcall(f, E, a)
      if not okP then
        E.anotar("error", string.format("accion %d %s trono: %s", i, tipo, tostring(hecho)))
      elseif hecho then
        E.hechas = E.hechas + 1
        E.decir(string.format("  accion %d %s: %s", i, tipo, tostring(detalle or "ok")))
      else
        E.anotar("error", string.format("accion %d %s: %s", i, tipo, tostring(detalle)))
      end
    end
  end

  local estado = estadoDe(E)
  local tl, final, exportado = escribirReporte(E, estado)
  if not tl then
    return timelineError(E, "E05", "el reporte no nacio: " .. tostring(final))
  end
  if E.politica.guardar_al_final ~= false then llamar(E.pm, "SaveProject") end
  local abrir = not (type(pedido.reporte) == "table" and pedido.reporte.abrir_al_terminar == false)
  if abrir then llamar(E.proj, "SetCurrentTimeline", tl) end
  E.decir("Reporte: " .. final)
  return {ok = (estado ~= "ERROR"), estado = estado, sello = E.sello, hechas = E.hechas,
          total = E.total, reporte = final, reporte_exportado = exportado,
          construidas = #E.construidas, anotaciones = #E.anotaciones}
end

return A
