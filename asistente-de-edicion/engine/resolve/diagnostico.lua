-- ============================================================
--  DIAGNOSTICO de la Fase 0 — asistente de edicion (Diez50)
--
--  Por que existe
--  --------------
--  El plugin de Resolve Free se apoya en hechos que nadie ha medido en esta
--  maquina: que globales deja vivos el menu de la 21.1 build 17 (terceros
--  dicen que ni io, ni os.execute, ni require, ni print), si dofile lee desde
--  un volumen o una ruta con acentos, cuales de los ~70 metodos que usa la
--  capa Lua contestan en Free, y por que canal puede volver un resultado sin
--  Claude. Los spikes S9-S11 (spikes_v2.lua) preguntaban eso con `print`, que
--  en el menu no se ve. Este archivo pregunta lo mismo y deja la respuesta
--  DENTRO del proyecto, donde se puede leer despues sin depender de nada.
--
--  Como se usa (resumen del contrato, formato 1)
--  ---------------------------------------------
--  - Hace `return D` y NO ejecuta nada al cargarse. Lo llama un stub:
--      D.correr({ ctx = "menu_utility"|"menu_sub"|"menu_edit"|"consola",
--                 config = "<KIT>/config.lua", resolve = resolve })
--  - Guardia: si el proyecto abierto no empieza con config.proyecto_requerido
--    no crea NADA en Resolve; solo intenta avisar por print y por
--    fusion:SetPrefs, cada uno en su pcall. El prefijo SIEMPRE empieza con
--    DIEZ50_DIAG: config.lua puede alargarlo, no acortarlo (D.prefijoRequerido).
--    Si la clave de prefs ya tiene una corrida buena, el aviso va a
--    "Global.Diez50.Diag.<ctx>_aborta" para no pisarla.
--  - Corrida r = (mayor r de los resumenes previos con el mismo sello y ctx)
--    + 1; clave = <sello>-<ctx>-r<r>. Se cuenta por el mayor r y no por el
--    numero de timelines porque, si SetName falla, una corrida deja DOS
--    resumenes y contar nombres saltaria un numero.
--  - Cada prueba de D.PRUEBAS da {id, familia, estado, valor}; estado es
--    OK | NO | INFO | ERR. valor: ASCII, sin '|' ni '=', una linea, <= 120.
--    Un metodo que no existe o que lanza es NO (es una medicion); ERR queda
--    solo para cuando la prueba misma revienta (un bug de este archivo).
--  - Canales de reporte, TODOS intentados y cada uno en su pcall:
--      1. timeline "DIAG RESUMEN <clave> <ok> de <total>" (nace como
--         "- de <total>" y se renombra al final; si SetName no sirve se crea
--         otra). Sin '/': Resolve rechaza / \ : * ? " < > | en nombres de
--         timeline y de bin (Studio 21.1.0.17, medido 2026-10-01), y con
--         "93/181" este canal no nacia. Todo nombre pasa por nombreSeguro.
--      2. timeline "DIAG MARCAS <clave>" con el negro y un marker por prueba
--         en el frame k*24, customData "diez50diag:<id>=<estado>|<valor>"
--      3. bin "DIEZ50 DIAG <clave>" con una subcarpeta por prueba
--         "<id> <estado> <valor[1..60]>" (legible en Project.db)
--      4. fusion:SetPrefs("Global.Diez50.Diag.<ctx>", <json>) + SavePrefs
--         (es la prueba PREFS-01; esa copia lleva "parcial":true porque se
--         escribe antes de conocer PREFS y SAVE)
--      5. print: "DIEZ50DIAG <id> <estado> <valor>" y "DIEZ50DIAG FIN ..."
--      6. io: <recibos>/<clave>.json con el documento completo
--      7. pm:SaveProject() (SAVE-01, y otra vez en silencio al final)
--  - Exportes (EXP), contrato §8: nombres PLANOS <recibos>/<clave>_<tipo>.<ext>
--    y <recibos>/<clave>_metadata.csv (igual en recibos_volumen), porque
--    nadie puede crear una carpeta por clave antes de correr. El valor nombra
--    el destino: "EXPORT_DRT recibos devolvio true" / "... volumen ...".
--  - Calibracion (API-01): si el binding inventa metodos que devuelven algo
--    verdadero, los OK que solo miran el retorno bajan a INFO.
--
--  Ojo con los entornos (medido en fuscript, 2026-09-24): el script que llama
--  corre en SU propio entorno (con __index hacia _G) y este archivo, cargado
--  con dofile, corre en _G. Lo que el stub asigne como global no llega aqui;
--  por eso `resolve` (y opcionalmente `fusion`) viajan dentro de ctx, y por
--  eso ENV describe el _G de este chunk, que puede no ser el del stub.
--
--  Que NUNCA hace
--  --------------
--  CreateProject, LoadProject, DeleteTimelines sobre timelines ajenas, ni
--  nada sobre objetos que no creo el propio diagnostico. De `os` solo llama
--  time, date, getenv y clock; jamas execute, remove, rename, exit, tmpname.
--  No llama `Resolve()` (en fuscript intentaria conectarse a la app).
--
--  Lua 5.1 / LuaJIT (el de Resolve) y 5.5 (el de los tests): sin operadores
--  de bits, sin //, `unpack` con respaldo, y `goto` solo dentro de un
--  loadstring, que es justo donde se mide.
--
--  Regla de estilo que un test comprueba: print, io., os. y debug. solo
--  aparecen entre las marcas "-- [mide]" y "-- [/mide]". Fuera de ellas este
--  archivo no depende de nada que el sandbox del menu pueda quitar.
-- ============================================================

local D = {}
D.FORMATO = 1
D.PROYECTO_POR_DEFECTO = "DIEZ50_DIAG"

-- Referencias tomadas al cargar. `_G` puede no existir en un entorno
-- recortado; entonces las pruebas de ENV lo dicen en vez de reventar.
local G = _G
-- Respaldo en Lua puro por si un entorno recortado quita `unpack` (LuaJIT no
-- siempre trae table.unpack): sin el, cada llamada con argumentos daria NO
-- por un motivo ajeno a lo que se mide.
local desempacar = unpack or table.unpack
if not desempacar then
  local function desde(t, i, j)
    if i > j then return end
    return t[i], desde(t, i + 1, j)
  end
  desempacar = function(t, i, j) return desde(t, i or 1, j or #t) end
end
local function empaquetar(...) return {n = select("#", ...), ...} end

-- ---------- utilidades puras ---------------------------------------------

-- Transliteracion minima del espanol. El contrato pide valores ASCII porque
-- viajan por nombres de bin, notas de marker y JSON, y cada canal trata los
-- bytes altos a su manera; ASCII llega igual por todos.
local TRANSLIT = {
  ["\195\161"] = "a", ["\195\169"] = "e", ["\195\173"] = "i", ["\195\179"] = "o",
  ["\195\186"] = "u", ["\195\129"] = "A", ["\195\137"] = "E", ["\195\141"] = "I",
  ["\195\147"] = "O", ["\195\154"] = "U", ["\195\177"] = "n", ["\195\145"] = "N",
  ["\195\188"] = "u", ["\195\156"] = "U",
}
local function ascii(s)
  s = string.gsub(s, "\195[\128-\191]", function(c) return TRANSLIT[c] or "?" end)
  s = string.gsub(s, "[\128-\255]+", "?")
  s = string.gsub(s, "%c", " ")
  return s
end

-- '|' separa estado y valor dentro del customData y '=' separa id y estado:
-- si aparecieran dentro del valor, el lector partiria mal la cadena.
local function limpiarValor(v)
  local s = ascii(tostring(v))
  s = string.gsub(s, "|", "/")
  s = string.gsub(s, "=", ":")
  s = string.gsub(s, "%s+", " ")
  s = string.gsub(s, "^ ", "")
  s = string.gsub(s, " $", "")
  if #s > 120 then s = string.sub(s, 1, 120) end
  return s
end
D.limpiarValor = limpiarValor

-- Mensaje de error sin la ruta del archivo: la ruta puede traer acentos y
-- gasta los 120 caracteres en algo que el lector ya sabe.
local function msg(e)
  local s = tostring(e)
  s = string.gsub(s, "^[^:]*/", "")
  return s
end

-- Codificador JSON minimo. No hay `require` en el menu, asi que no puede
-- venir de una libreria. Objetos con claves ordenadas (salida estable);
-- una tabla vacia sale como [] porque aqui solo puede ser una lista vacia.
local ESCAPES = {['"'] = '\\"', ["\\"] = "\\\\", ["\b"] = "\\b", ["\f"] = "\\f",
                 ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t"}
local function jsonTexto(s)
  local cuerpo = string.gsub(s, '[%c"\\]', function(c)
    return ESCAPES[c] or string.format("\\u%04x", string.byte(c))
  end)
  return '"' .. cuerpo .. '"'
end
local function json(v)
  local t = type(v)
  if v == nil then return "null" end
  if t == "boolean" then return v and "true" or "false" end
  if t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    return string.format("%.14g", v)
  end
  if t == "string" then return jsonTexto(v) end
  if t == "table" then
    local n = #v
    if n > 0 or next(v) == nil then
      local partes = {}
      for i = 1, n do partes[i] = json(v[i]) end
      return "[" .. table.concat(partes, ",") .. "]"
    end
    local claves = {}
    for k in pairs(v) do claves[#claves + 1] = tostring(k) end
    table.sort(claves)
    local partes = {}
    for _, k in ipairs(claves) do
      partes[#partes + 1] = jsonTexto(k) .. ":" .. json(v[k])
    end
    return "{" .. table.concat(partes, ",") .. "}"
  end
  return jsonTexto(tostring(v))
end
D.json = json

local function patronLiteral(s)
  return (string.gsub(s, "[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0"))
end

-- Resolve rechaza / \ : * ? " < > | en el nombre de una timeline y de un bin:
-- CreateEmptyTimeline y AddSubFolder devuelven nil y SetName false (Studio
-- 21.1.0.17, medido 2026-10-01). Un nombre armado con un valor lo pierde todo
-- por un ':' de un mensaje de error; aqui se cambian por '_'.
D.PROHIBIDOS_EN_NOMBRE = '[/\\:%*%?"<>|]'
local function nombreSeguro(s)
  return (string.gsub(tostring(s), D.PROHIBIDOS_EN_NOMBRE, "_"))
end

local function contar(t)
  local n = 0
  if type(t) == "table" then for _ in pairs(t) do n = n + 1 end end
  return n
end

local function describir(v)
  local t = type(v)
  if t == "table" then return "tabla n " .. contar(v) end
  if t == "userdata" then return "objeto" end
  return string.sub(tostring(v), 1, 60)
end

local function global(nombre)
  if type(G) ~= "table" then return nil end
  return G[nombre]
end

-- [mide] reloj: os.clock solo para cronometrar. Sin os no hay tiempo, y la
-- prueba que lo usa sigue igual, solo sin el dato.
local function reloj()
  local ok, v = pcall(function() return os.clock() end)
  if ok and type(v) == "number" then return v end
  return nil
end
-- [/mide]

local function lapso(t0)
  local t1 = reloj()
  if not t0 or not t1 then return "" end
  return " ms " .. string.format("%.1f", (t1 - t0) * 1000)
end

-- ---------- llamadas protegidas a la API ----------------------------------

-- obj:metodo(...) sin dejar escapar nada. Devuelve como salio ("ok",
-- "no existe", "lanza", "sin objeto") y el resultado o el mensaje.
local function llamar(obj, metodo, ...)
  if obj == nil or obj == false then return "sin objeto" end
  local okm, fn = pcall(function() return obj[metodo] end)
  if not okm or fn == nil then return "no existe" end
  local args = empaquetar(...)
  local okc, res = pcall(function() return fn(obj, desempacar(args, 1, args.n)) end)
  if not okc then return "lanza", msg(res) end
  return "ok", res
end

-- La forma corta de medir un metodo: OK solo si contesta algo no falso.
-- false/nil es NO porque, segun el README de 21.1, asi responde Free a una
-- funcion de Studio: existe, pero no hace nada.
local function interpretar(metodo, como, res)
  if como ~= "ok" then
    return "NO", metodo .. " " .. como .. (res ~= nil and (" " .. tostring(res)) or "")
  end
  if res == nil or res == false then return "NO", metodo .. " devolvio " .. tostring(res) end
  return "OK", metodo .. " " .. describir(res)
end
-- medir solo mira el retorno. Si la calibracion (API-01) encontro que el
-- binding fabrica metodos Y que esos metodos falsos devuelven algo verdadero,
-- un retorno verdadero ya no distingue un metodo real de uno inventado: el OK
-- baja a INFO. Las pruebas que comprueban el efecto (ida y vuelta, conteos)
-- no pasan por aqui y no se degradan.
local function medir(E, obj, metodo, ...)
  local como, res = llamar(obj, metodo, ...)
  local est, val = interpretar(metodo, como, res)
  if est == "OK" and E.stubVerdadero then
    return "INFO", metodo .. " " .. describir(res) .. " retorno sin efecto comprobado"
  end
  return est, val
end

-- Igual, pero el dato es informativo (versiones, ajustes): INFO en vez de OK.
-- Una sola llamada: algunos metodos no son idempotentes.
local function consultar(obj, metodo, ...)
  local como, res = llamar(obj, metodo, ...)
  local est, val = interpretar(metodo, como, res)
  if est == "OK" then return "INFO", describir(res) end
  return est, val
end

local function constante(E, nombre)
  local ok, v = pcall(function() return E.R[nombre] end)
  -- Si el binding fabrica stubs (S9), una constante inexistente llega como
  -- funcion: eso no es una constante.
  if ok and v ~= nil and type(v) ~= "function" then return v end
  return nil
end

-- El stub puede pasar `fusion` en ctx: en fuscript (y quiza en el menu) los
-- globales del stub viven en SU entorno y no llegan a este dofile, que corre
-- en _G. Despues se prueba el global y, al final, resolve:Fusion().
local function fusionObj(E)
  local F = E.ctxFusion or fusion or fu
  if F then return F end
  local como, res = llamar(E.R, "Fusion")
  if como == "ok" and res then return res end
  return nil
end

-- Toda timeline que crea el diagnostico pasa por aqui y queda anotada: es la
-- unica lista sobre la que se permite borrar. El nombre sale limpio de los
-- caracteres que Resolve rechaza (la prueba LANG que los mide no pasa por aqui).
local function crearTimeline(E, nombre)
  nombre = nombreSeguro(nombre)
  local ok, tl = pcall(function() return E.mp:CreateEmptyTimeline(nombre) end)
  if ok and tl then
    E.creadas[#E.creadas + 1] = tl
    return tl
  end
  return nil
end

local function nombreDe(obj)
  local como, res = llamar(obj, "GetName")
  if como == "ok" then return res end
  return nil
end

local function ponerActual(E, tl)
  pcall(function() return E.proj:SetCurrentTimeline(tl) end)
end

-- Busca el negro de una corrida previa. En la segunda corrida ImportMedia
-- puede no devolver nada (el archivo ya esta en el pool). Solo mira la raiz y
-- los bins "DIEZ50 DIAG ...": nunca recorre ni toca material del editor.
local function buscarNegroPrevio(E, ruta)
  local function mira(carpeta)
    local como, clips = llamar(carpeta, "GetClipList")
    if como == "ok" and type(clips) == "table" then
      for _, c in ipairs(clips) do
        local c2, p = llamar(c, "GetClipProperty", "File Path")
        if c2 == "ok" and p == ruta then return c end
      end
    end
    return nil
  end
  local hallado = mira(E.root)
  if hallado then return hallado end
  local como, subs = llamar(E.root, "GetSubFolderList")
  if como ~= "ok" or type(subs) ~= "table" then return nil end
  for _, s in ipairs(subs) do
    local n = nombreDe(s)
    if type(n) == "string" and string.sub(n, 1, 12) == "DIEZ50 DIAG " then
      hallado = mira(s)
      if hallado then return hallado end
    end
  end
  return nil
end

-- Timeline de trabajo: donde se ejercen los metodos que escriben, para no
-- ensuciar la de marcas (que es el reporte) ni ninguna ajena.
local function trabajo(E)
  if E.trabajo ~= nil then return E.trabajo end
  E.trabajo = false
  local tl = crearTimeline(E, "DIAG TRABAJO " .. E.clave)
  if not tl then return false end
  E.trabajo = tl
  if E.negro then
    ponerActual(E, tl)
    local ok, res = pcall(function()
      return E.mp:AppendToTimeline({{mediaPoolItem = E.negro, startFrame = 0,
                                     endFrame = 239, trackIndex = 1}})
    end)
    E.appendTabla = {ok = ok, res = res}
    if ok and type(res) == "table" and res[1] then E.item = res[1] end
  end
  return tl
end

-- ---------- catalogo -----------------------------------------------------
-- Cada entrada da UN resultado. Los ids se numeran por familia en el orden en
-- que se declaran; el orden importa (API crea lo que usan EXP y META, PREFS y
-- SAVE van al final porque escriben lo ya medido).
D.PRUEBAS = {}
local CONTADOR = {}
local function prueba(familia, que, f)
  CONTADOR[familia] = (CONTADOR[familia] or 0) + 1
  local id = familia .. "-" .. string.format("%02d", CONTADOR[familia])
  D.PRUEBAS[#D.PRUEBAS + 1] = {id = id, familia = familia, que = que, f = f}
end

-- ===== CTX: desde donde se corrio y con que ================================
local CTX_CONOCIDOS = {menu_utility = true, menu_sub = true, menu_edit = true,
                       consola = true}
prueba("CTX", "ctx recibido del stub", function(E)
  return CTX_CONOCIDOS[E.ctx] and "OK" or "INFO", E.ctx
end)
prueba("CTX", "ctx.resolve recibido", function(E)
  local t = type(E.ctxResolve)
  return (t == "table" or t == "userdata") and "OK" or "NO", t
end)
prueba("CTX", "global resolve", function(E) return "INFO", type(resolve) end)
-- Solo el tipo: llamarlo en fuscript intentaria conectarse a la app.
prueba("CTX", "global Resolve (no se llama)", function(E) return "INFO", type(Resolve) end)
prueba("CTX", "proyecto abierto", function(E) return "INFO", E.nombreProyecto end)
prueba("CTX", "config cargada", function(E)
  if E.cfgError then return "NO", E.cfgError end
  return (E.cfg.formato == D.FORMATO) and "OK" or "NO", "formato " .. tostring(E.cfg.formato)
end)

-- ===== VER: que version y que edicion dice ser =============================
prueba("VER", "GetVersionString", function(E) return consultar(E.R, "GetVersionString") end)
prueba("VER", "GetProductName", function(E) return consultar(E.R, "GetProductName") end)
prueba("VER", "GetVersion", function(E)
  local como, v = llamar(E.R, "GetVersion")
  if como ~= "ok" then return "NO", "GetVersion " .. como end
  if type(v) ~= "table" then return "INFO", describir(v) end
  local partes = {}
  for i = 1, #v do partes[i] = tostring(v[i]) end
  return "INFO", table.concat(partes, ".")
end)
prueba("VER", "edicion inferida", function(E)
  local p = string.upper(tostring(E.producto or ""))
  if p == "" or p == "?" then return "INFO", "desconocida" end
  if string.find(p, "STUDIO", 1, true) then return "INFO", "Studio" end
  return "INFO", "Free"
end)

-- ===== ENV: que globales deja vivos este contexto ==========================
-- La lista sale de lo que terceros reportan para el menu de la build 17 y de
-- lo que la capa Lua necesita. type() no llama nada: es seguro para todas.
local GLOBALES = {"io", "os", "require", "package", "ffi", "debug", "jit", "bit",
  "lpeg", "string", "table", "math", "coroutine", "load", "loadstring", "loadfile",
  "dofile", "pcall", "xpcall", "setfenv", "getfenv", "print", "arg", "unpack",
  "select", "collectgarbage", "rawget", "bmd", "fu", "fusion", "app", "comp",
  "resolve", "Resolve"}
for _, nombre in ipairs(GLOBALES) do
  prueba("ENV", "type(" .. nombre .. ")", function(E)
    if type(G) ~= "table" then return "INFO", nombre .. " sin _G" end
    return "INFO", nombre .. " " .. type(global(nombre))
  end)
end
prueba("ENV", "_VERSION", function(E) return "INFO", tostring(global("_VERSION")) end)
prueba("ENV", "jit.version", function(E)
  local j = global("jit")
  if type(j) ~= "table" then return "INFO", "sin jit" end
  return "INFO", tostring(j.version)
end)
prueba("ENV", "claves de _G", function(E)
  if type(G) ~= "table" then return "NO", "sin _G" end
  return "INFO", "n " .. contar(G)
end)
prueba("ENV", "claves de bmd", function(E)
  local b = global("bmd")
  if type(b) ~= "table" then return "INFO", "bmd " .. type(b) end
  local ks = {}
  for k in pairs(b) do ks[#ks + 1] = tostring(k) end
  table.sort(ks)
  local tope = #ks
  if tope > 12 then tope = 12 end
  return "INFO", "n " .. #ks .. " " .. table.concat(ks, ",", 1, tope)
end)

-- [mide] familia OS. Unica zona (con reloj) donde se toca `os`. Solo se
-- LLAMAN funciones inocuas; de las peligrosas solo se pregunta el tipo.
prueba("OS", "type(os)", function(E) return "INFO", type(os) end)
for _, fn in ipairs({"clock", "date", "difftime", "execute", "exit", "getenv",
                     "remove", "rename", "setlocale", "time", "tmpname"}) do
  prueba("OS", "type de os." .. fn, function(E)
    if type(os) ~= "table" then return "INFO", fn .. " sin os" end
    return "INFO", fn .. " " .. type(os[fn])
  end)
end
local function llamarOS(fn, ...)
  if type(os) ~= "table" then return "NO", "sin os" end
  if type(os[fn]) ~= "function" then return "NO", fn .. " " .. type(os[fn]) end
  local args = empaquetar(...)
  local ok, v = pcall(function() return os[fn](desempacar(args, 1, args.n)) end)
  if not ok then return "NO", fn .. " lanza " .. msg(v) end
  if v == nil then return "NO", fn .. " nil" end
  return "OK", tostring(v)
end
prueba("OS", "os.time()", function(E) return llamarOS("time") end)
-- La fecha fija da la paridad con time.mktime del preparador: si difiere, la
-- zona horaria de Resolve no es la de la maquina.
prueba("OS", "os.time(tabla fija)", function(E)
  local t = E.cfg.os_time_fijo
  if type(t) ~= "table" then return "NO", "config sin os_time_fijo" end
  local est, val = llamarOS("time", {year = t.year, month = t.month, day = t.day,
                                    hour = t.hour, min = t.min, sec = t.sec})
  if est == "OK" then
    local esperado = E.cfg.os_time_esperado_local
    val = val .. " esperado " .. tostring(esperado)
      .. ((tonumber(val) == tonumber(esperado)) and " igual" or " distinto")
  end
  return est, val
end)
prueba("OS", "os.date('%z')", function(E) return llamarOS("date", "%z") end)
prueba("OS", "os.date UTC ISO", function(E) return llamarOS("date", "!%Y-%m-%dT%H:%M:%S") end)
prueba("OS", "os.getenv('HOME')", function(E) return llamarOS("getenv", "HOME") end)
prueba("OS", "os.clock()", function(E) return llamarOS("clock") end)
-- [/mide]

-- [mide] familia IO. popen solo se pregunta, nunca se llama.
prueba("IO", "type(io)", function(E) return "INFO", type(io) end)
prueba("IO", "type(io.open)", function(E)
  if type(io) ~= "table" then return "INFO", "sin io" end
  return "INFO", type(io.open)
end)
prueba("IO", "escribir recibo con io", function(E)
  if type(io) ~= "table" or type(io.open) ~= "function" then return "NO", "sin io.open" end
  local dir = E.cfg.recibos
  if type(dir) ~= "string" then return "NO", "config sin recibos" end
  local ruta = dir .. "/" .. E.clave .. "_io.txt"
  local texto = "diez50diag " .. E.clave .. "\n"
  local f, err = io.open(ruta, "w")
  if not f then return "NO", "open " .. msg(err) end
  f:write(texto)
  f:close()
  local g = io.open(ruta, "r")
  if not g then return "NO", "escrito pero no se relee" end
  local leido = g:read("*a")
  g:close()
  return (leido == texto) and "OK" or "NO", (leido == texto) and "escrito y releido" or "releido distinto"
end)
prueba("IO", "leer fixture con io", function(E)
  if type(io) ~= "table" or type(io.open) ~= "function" then return "NO", "sin io.open" end
  local ruta = E.cfg.fixtures and E.cfg.fixtures.ok
  if type(ruta) ~= "string" then return "NO", "config sin fixtures.ok" end
  local f, err = io.open(ruta, "r")
  if not f then return "NO", "open " .. msg(err) end
  local txt = f:read("*a")
  f:close()
  local hay = type(txt) == "string" and string.find(txt, "12345", 1, true) ~= nil
  return hay and "OK" or "NO", hay and "leido" or "sin la suma"
end)
prueba("IO", "type(io.popen) sin llamarlo", function(E)
  if type(io) ~= "table" then return "INFO", "sin io" end
  return "INFO", type(io.popen)
end)
-- [/mide]

-- ===== LOAD: como entran los datos horneados ===============================
-- Un fixture vale si trae suma = 12345, o (el horneado grande) una lista de
-- clips no vacia; si ademas trae `n`, tiene que coincidir con el conteo.
local function verificarFixture(t)
  if type(t) ~= "table" then return false, "devolvio " .. type(t) end
  local n = nil
  if type(t.clips) == "table" then n = contar(t.clips) end
  if t.suma ~= nil and t.suma ~= 12345 then return false, "suma " .. tostring(t.suma) end
  if t.n ~= nil and n ~= nil and t.n ~= n then
    return false, "n " .. tostring(t.n) .. " clips " .. n
  end
  if t.suma == nil and (n == nil or n == 0) then return false, "sin suma ni clips" end
  local det = ""
  if t.suma ~= nil then det = "suma " .. tostring(t.suma) end
  if n ~= nil then det = det .. " clips " .. n end
  return true, det
end
local function probarDofile(E, clave)
  local ruta = E.cfg.fixtures and E.cfg.fixtures[clave]
  if type(ruta) ~= "string" then return "INFO", "sin ruta " .. clave end
  local t0 = reloj()
  local ok, t = pcall(dofile, ruta)
  local ms = lapso(t0)
  if not ok then return "NO", "lanza " .. msg(t) end
  local bien, det = verificarFixture(t)
  return bien and "OK" or "NO", det .. ms
end
prueba("LOAD", "dofile desde el kit", function(E) return probarDofile(E, "ok") end)
prueba("LOAD", "dofile desde /Volumes (dmg)", function(E) return probarDofile(E, "volumen") end)
prueba("LOAD", "dofile con acentos y espacios", function(E) return probarDofile(E, "acentos") end)
prueba("LOAD", "dofile desde Application Support", function(E) return probarDofile(E, "appsupport") end)
prueba("LOAD", "dofile con error de sintaxis", function(E)
  local ruta = E.cfg.fixtures and E.cfg.fixtures.error
  if type(ruta) ~= "string" then return "INFO", "sin ruta error" end
  local ok, err = pcall(dofile, ruta)
  if ok then return "NO", "no lanzo" end
  return "OK", "capturado " .. msg(err)
end)
prueba("LOAD", "dofile del horneado grande", function(E) return probarDofile(E, "grande") end)
-- Asi se cargara el buzon: sin acceso a los globales del script que lo lee.
-- Dos partes. (1) El fixture del kit se carga con el mismo mecanismo y tiene
-- que traer su suma. (2) El aislamiento se comprueba aparte, con un chunk que
-- SI mira afuera: el fixture no toca globales, asi que por si solo daria OK
-- aunque setfenv no aislara nada. El chunk de prueba lee `type` y `_G` (que
-- existen en cualquier entorno donde corre este archivo) y escribe un global;
-- aislado, no ve ninguno de los dos y su escritura cae en la tabla vacia.
local TEXTO_AISLADO = "diez50_aislado = 1 return { t = type, g = _G }"
local function cargarAislado(via)
  local env = {}
  local ch, err
  if via == "setfenv" then
    local cargar = loadstring or load
    if type(cargar) ~= "function" then return nil, "sin loadstring ni load" end
    ch, err = cargar(TEXTO_AISLADO)
    if not ch then return nil, "no compila " .. msg(err) end
    setfenv(ch, env)
  else
    if type(load) ~= "function" then return nil, "sin load" end
    ch, err = load(TEXTO_AISLADO, "=aislado", "t", env)
    if not ch then return nil, "no compila " .. msg(err) end
  end
  local ok, r = pcall(ch)
  -- Si no aislo, el global se escribio en el _G de verdad: se limpia.
  if type(G) == "table" then pcall(function() G.diez50_aislado = nil end) end
  if not ok then return nil, "lanza " .. msg(r) end
  if type(r) ~= "table" then return nil, "devolvio " .. type(r) end
  return r.t == nil and r.g == nil and env.diez50_aislado == 1, "ve type " .. type(r.t)
    .. " ve _G " .. type(r.g) .. " escribe en env " .. tostring(env.diez50_aislado == 1)
end
prueba("LOAD", "loadfile + setfenv(chunk, {})", function(E)
  local ruta = E.cfg.fixtures and E.cfg.fixtures.ok
  if type(ruta) ~= "string" then return "INFO", "sin ruta ok" end
  if type(loadfile) ~= "function" then return "NO", "sin loadfile" end
  local chunk, err, via
  if type(setfenv) == "function" then
    chunk, err = loadfile(ruta)
    if not chunk then return "NO", "loadfile " .. msg(err) end
    setfenv(chunk, {})
    via = "setfenv"
  else
    chunk, err = loadfile(ruta, "t", {})
    if not chunk then return "NO", "loadfile " .. msg(err) end
    via = "env de loadfile"
  end
  local ok, t = pcall(chunk)
  if not ok then return "NO", via .. " lanza " .. msg(t) end
  local bien, det = verificarFixture(t)
  if not bien then return "NO", via .. " " .. det end
  local aislado, como = cargarAislado(via)
  if aislado == nil then return "NO", via .. " " .. det .. " aislamiento " .. como end
  if not aislado then return "NO", via .. " " .. det .. " sin aislamiento " .. como end
  return "OK", via .. " " .. det .. " aislado"
end)
prueba("LOAD", "loadstring('return 1+1')", function(E)
  if type(loadstring) ~= "function" then return "NO", "loadstring " .. type(loadstring) end
  local f = loadstring("return 1+1")
  local ok, v = pcall(f)
  return (ok and v == 2) and "OK" or "NO", tostring(v)
end)
prueba("LOAD", "load(cadena)", function(E)
  if type(load) ~= "function" then return "NO", "load " .. type(load) end
  local ok, f = pcall(load, "return 1+1")
  if not ok or type(f) ~= "function" then return "NO", "no acepta cadena " .. msg(f) end
  local ok2, v = pcall(f)
  return (ok2 and v == 2) and "OK" or "NO", tostring(v)
end)
prueba("LOAD", "bmd.readstring", function(E)
  local b = global("bmd")
  if type(b) ~= "table" or b.readstring == nil then return "INFO", "sin bmd.readstring" end
  local ok, t = pcall(b.readstring, "{ suma = 12345 }")
  if not ok then return "NO", "lanza " .. msg(t) end
  return (type(t) == "table" and t.suma == 12345) and "OK" or "NO", describir(t)
end)
-- Si _G sobrevive entre corridas del menu, un script puede dejar estado para
-- el siguiente; si no, cada corrida arranca en blanco.
prueba("LOAD", "_G persiste entre corridas", function(E)
  if type(G) ~= "table" then return "NO", "sin _G" end
  local previo = G.DIEZ50_DIAG_PERSISTE
  G.DIEZ50_DIAG_PERSISTE = E.clave
  return (previo ~= nil) and "OK" or "INFO", "encontrado " .. tostring(previo)
end)

-- ===== LANG: diferencias de lenguaje que ya mordieron ======================
prueba("LANG", "pcall captura error", function(E)
  local ok = pcall(error, "x")
  return (ok == false) and "OK" or "NO", "pcall devolvio " .. tostring(ok)
end)
prueba("LANG", "xpcall con manejador", function(E)
  local ok, v = xpcall(function() error("x") end, function() return "manejado" end)
  return (ok == false and v == "manejado") and "OK" or "NO", tostring(v)
end)
prueba("LANG", "string.format('%d', 2.5)", function(E)
  local ok, v = pcall(string.format, "%d", 2.5)
  if ok then return "INFO", "devuelve " .. tostring(v) end
  return "INFO", "lanza " .. msg(v)
end)
prueba("LANG", "string.format('%q') con salto", function(E)
  local ok, v = pcall(string.format, "%q", "a\nb")
  if not ok then return "NO", "lanza " .. msg(v) end
  if string.find(v, "\\\n", 1, true) then return "INFO", "barra y salto real" end
  if string.find(v, "\\n", 1, true) then return "INFO", "barra n" end
  return "INFO", "otro " .. v
end)
prueba("LANG", "goto (via loadstring)", function(E)
  local cargar = loadstring or load
  if type(cargar) ~= "function" then return "NO", "sin loadstring ni load" end
  local f, err = cargar("local x = 1 goto fin x = 2 ::fin:: return x")
  if not f then return "NO", "no compila " .. msg(err) end
  local ok, v = pcall(f)
  return (ok and v == 1) and "OK" or "NO", tostring(v)
end)
prueba("LANG", "bit.band", function(E)
  local b = global("bit")
  if type(b) ~= "table" or type(b.band) ~= "function" then return "INFO", "sin bit" end
  local ok, v = pcall(b.band, 255, 15)
  return (ok and v == 15) and "OK" or "NO", tostring(v)
end)
prueba("LANG", "unpack contra table.unpack", function(E)
  return "INFO", "unpack " .. type(global("unpack")) .. " table.unpack " .. type(table.unpack)
end)
prueba("LANG", "largo en bytes de 'Ano' con enie", function(E)
  return "INFO", "bytes " .. #"A\195\177o"
end)
prueba("LANG", "largo maximo de nombre de timeline", function(E)
  local partes = {}
  for _, L in ipairs({60, 120, 250}) do
    local base = "DIAG LN" .. L .. " " .. E.clave .. " "
    local nombre = base .. string.rep("x", L - #base)
    local tl = crearTimeline(E, nombre)
    local got = tl and nombreDe(tl)
    local r
    if not tl then r = "nil"
    elseif got == nombre then r = "ok"
    else r = "recorta " .. tostring(got and #got) end
    partes[#partes + 1] = L .. " " .. r
  end
  return "INFO", table.concat(partes, " ")
end)
prueba("LANG", "largo maximo de nombre de marker", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local partes = {}
  local frame = 2
  for _, L in ipairs({60, 120, 250}) do
    local nombre = string.rep("m", L)
    local c1, r1 = llamar(tl, "AddMarker", frame, "Cyan", nombre, "", 1,
                          "diez50prueba:largo" .. L)
    local r = "nil"
    if c1 == "ok" and r1 then
      local c2, ms = llamar(tl, "GetMarkers")
      local m = (c2 == "ok" and type(ms) == "table") and ms[frame] or nil
      local got = type(m) == "table" and m.name or nil
      if got == nombre then r = "ok"
      elseif got then r = "recorta " .. #got end
    end
    partes[#partes + 1] = L .. " " .. r
    frame = frame + 1
  end
  return "INFO", table.concat(partes, " ")
end)
prueba("LANG", "caracteres que Resolve rechaza en nombres", function(E)
  -- Studio 21.1.0.17 (2026-10-01) rechaza los nueve en timelines y en bins.
  -- Se mide en cada build y SIN nombreSeguro, para saber si Free hace lo
  -- mismo: el reporte del plugin vive de nombres de timeline. Lo que Resolve
  -- acepte queda creado (el diagnostico no borra); los bins van en "_car".
  local CARS = {{"/", "barra"}, {"\\", "contrabarra"}, {":", "dospuntos"},
                {"*", "asterisco"}, {"?", "interrogacion"}, {'"', "comillas"},
                {"<", "menor"}, {">", "mayor"}, {"|", "pleca"}}
  local okc, car = false, nil
  if E.bin then okc, car = pcall(function() return E.mp:AddSubFolder(E.bin, "_car") end) end
  local tlAcepta, binAcepta, tlNo, binNo = {}, {}, 0, 0
  for i, c in ipairs(CARS) do
    local c1, tl = llamar(E.mp, "CreateEmptyTimeline", "DIAG CAR " .. E.clave .. " " .. i .. c[1])
    if c1 == "ok" and tl then
      E.creadas[#E.creadas + 1] = tl
      tlAcepta[#tlAcepta + 1] = c[2]
    else
      tlNo = tlNo + 1
    end
    if okc and car then
      local c2, f = llamar(E.mp, "AddSubFolder", car, "car " .. i .. c[1])
      if c2 == "ok" and f then binAcepta[#binAcepta + 1] = c[2] else binNo = binNo + 1 end
    end
  end
  local v = "timeline rechaza " .. tlNo .. " de " .. #CARS
  if okc and car then v = v .. " bin rechaza " .. binNo .. " de " .. #CARS
  else v = v .. " bin sin medir" end
  if #tlAcepta > 0 then v = v .. " tl acepta " .. table.concat(tlAcepta, ",") end
  if #binAcepta > 0 then v = v .. " bin acepta " .. table.concat(binAcepta, ",") end
  return "INFO", v
end)

-- ===== API: la capa Lua sobre objetos del diagnostico ======================
-- Primero la calibracion (S9): si el binding inventa metodos, la presencia no
-- prueba nada y solo cuenta el efecto. Por eso aqui casi todo se LLAMA.
-- Ademas de indexarlo, se LLAMA el metodo falso (como hacia S9): si el stub
-- devuelve algo verdadero, un retorno verdadero no prueba nada y `medir`
-- degrada sus OK a INFO (E.stubVerdadero). Llamar algo que no existe no puede
-- tocar el proyecto: el binding no tiene a donde mandarlo.
prueba("API", "calibracion: metodo inexistente (S9)", function(E)
  local FALSO = "MetodoQueNoExisteEnNingunaVersion20260911"
  local ok, v = pcall(function() return E.mp[FALSO] end)
  if not ok then return "INFO", "indexar lanza " .. msg(v) end
  E.bindingMiente = (v ~= nil)
  if v == nil then return "INFO", "nil la presencia es evidencia" end
  local okc, ret = pcall(function() return v(E.mp) end)
  E.stubVerdadero = okc and ret ~= nil and ret ~= false
  local dice = okc and ("devuelve " .. describir(ret)) or ("lanza " .. msg(ret))
  return "INFO", "stub " .. type(v) .. " " .. dice
    .. (E.stubVerdadero and " retornos sin efecto no cuentan" or " la presencia no prueba nada")
end)
prueba("API", "Resolve.GetProjectManager", function(E) return medir(E, E.R, "GetProjectManager") end)
prueba("API", "ProjectManager.GetCurrentProject", function(E) return medir(E, E.pm, "GetCurrentProject") end)
prueba("API", "Project.GetMediaPool", function(E) return medir(E, E.proj, "GetMediaPool") end)
prueba("API", "MediaPool.GetRootFolder", function(E) return medir(E, E.mp, "GetRootFolder") end)
prueba("API", "MediaPool.AddSubFolder (bin DIAG)", function(E)
  if not E.bin then return "NO", "AddSubFolder " .. tostring(E.setup.bin) end
  local n = nombreDe(E.bin)
  return (n == "DIEZ50 DIAG " .. E.clave) and "OK" or "NO", "nombre " .. tostring(n)
end)
prueba("API", "Folder.GetSubFolderList ve el bin", function(E)
  local como, subs = llamar(E.root, "GetSubFolderList")
  if como ~= "ok" or type(subs) ~= "table" then return "NO", "GetSubFolderList " .. como end
  for _, s in ipairs(subs) do
    if nombreDe(s) == "DIEZ50 DIAG " .. E.clave then return "OK", "n " .. #subs end
  end
  return "NO", "bin ausente en " .. #subs
end)
prueba("API", "MediaPool.SetCurrentFolder y GetCurrentFolder", function(E)
  if not E.bin then return "NO", "sin bin" end
  local c1, r1 = llamar(E.mp, "SetCurrentFolder", E.bin)
  local c2, r2 = llamar(E.mp, "GetCurrentFolder")
  local igual = c2 == "ok" and r2 and nombreDe(r2) == nombreDe(E.bin)
  return (c1 == "ok" and r1 and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. (igual and "igual" or (c2 .. " " .. tostring(r2 and nombreDe(r2))))
end)
prueba("API", "MediaPool.ImportMedia (negro)", function(E)
  local s = E.setup.importar
  if not s then return "NO", "config sin media.negro" end
  if not s.ok then return "NO", "lanza " .. msg(s.res) end
  if E.negro and s.previo then return "INFO", "ya estaba en el pool" end
  return E.negro and "OK" or "NO", describir(s.res)
end)
prueba("API", "Folder.GetClipList ve el negro", function(E)
  if not E.negro then return "NO", "sin negro" end
  local como, clips = llamar(E.bin, "GetClipList")
  if como ~= "ok" or type(clips) ~= "table" then return "NO", "GetClipList " .. como end
  for _, c in ipairs(clips) do if c == E.negro then return "OK", "n " .. #clips end end
  -- La identidad de objetos no siempre se conserva en el binding: por nombre.
  local n = nombreDe(E.negro)
  for _, c in ipairs(clips) do if nombreDe(c) == n then return "OK", "por nombre n " .. #clips end end
  return "NO", "no aparece en " .. #clips
end)
prueba("API", "MediaPoolItem.GetClipProperty File Path", function(E)
  if not E.negro then return "NO", "sin negro" end
  local como, p = llamar(E.negro, "GetClipProperty", "File Path")
  if como ~= "ok" then return "NO", como end
  local esperado = E.cfg.media and E.cfg.media.negro
  return (p == esperado) and "OK" or "NO", (p == esperado) and "coincide" or ("dice " .. tostring(p))
end)
prueba("API", "MediaPoolItem.GetClipProperty FPS y Frames", function(E)
  if not E.negro then return "NO", "sin negro" end
  local _, fps = llamar(E.negro, "GetClipProperty", "FPS")
  local _, fr = llamar(E.negro, "GetClipProperty", "Frames")
  return "INFO", "fps " .. tostring(fps) .. " frames " .. tostring(fr)
end)
prueba("API", "MediaPoolItem.GetMediaId", function(E) return medir(E, E.negro, "GetMediaId") end)
prueba("API", "MediaPoolItem.GetName", function(E) return medir(E, E.negro, "GetName") end)
prueba("API", "MediaPool.CreateEmptyTimeline (marcas)", function(E)
  if not E.marcas then return "NO", "no se creo DIAG MARCAS" end
  local n = nombreDe(E.marcas)
  return (n == "DIAG MARCAS " .. E.clave) and "OK" or "NO", "nombre " .. tostring(n)
end)
prueba("API", "MediaPool.AppendToTimeline plano", function(E)
  local s = E.setup.append
  if not s then return "NO", "no se intento" end
  if not s.ok then return "NO", "lanza " .. msg(s.res) end
  return (type(s.res) == "table" and s.res[1]) and "OK" or "NO", describir(s.res)
end)
prueba("API", "MediaPool.AppendToTimeline con clipInfo", function(E)
  trabajo(E)
  local s = E.appendTabla
  if not s then return "NO", "sin negro o sin timeline de trabajo" end
  if not s.ok then return "NO", "lanza " .. msg(s.res) end
  return E.item and "OK" or "NO", describir(s.res)
end)
prueba("API", "Project.SetCurrentTimeline y GetCurrentTimeline", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local c1, r1 = llamar(E.proj, "SetCurrentTimeline", tl)
  local c2, r2 = llamar(E.proj, "GetCurrentTimeline")
  local igual = c2 == "ok" and r2 and nombreDe(r2) == nombreDe(tl)
  return (c1 == "ok" and r1 and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. (igual and "igual" or c2)
end)
prueba("API", "Project.GetTimelineCount y GetTimelineByIndex", function(E)
  local como, n = llamar(E.proj, "GetTimelineCount")
  if como ~= "ok" or type(n) ~= "number" then return "NO", "GetTimelineCount " .. como end
  for i = 1, n do
    local _, tl = llamar(E.proj, "GetTimelineByIndex", i)
    if tl and nombreDe(tl) == "DIAG MARCAS " .. E.clave then return "OK", "n " .. n end
  end
  return "NO", "marcas no aparece en " .. n
end)
prueba("API", "Timeline.GetTrackCount video", function(E)
  return consultar(trabajo(E), "GetTrackCount", "video")
end)
local function agregaPista(E, tipo, opciones)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local _, antes = llamar(tl, "GetTrackCount", tipo)
  local como, r
  if opciones == nil then como, r = llamar(tl, "AddTrack", tipo)
  else como, r = llamar(tl, "AddTrack", tipo, opciones) end
  local _, despues = llamar(tl, "GetTrackCount", tipo)
  local sube = type(antes) == "number" and type(despues) == "number" and despues == antes + 1
  return (como == "ok" and r and sube) and "OK" or "NO",
    "AddTrack " .. como .. " " .. tostring(r) .. " " .. tostring(antes) .. " a " .. tostring(despues)
end
prueba("API", "Timeline.AddTrack video", function(E) return agregaPista(E, "video") end)
prueba("API", "Timeline.AddTrack audio stereo", function(E) return agregaPista(E, "audio", "stereo") end)
prueba("API", "Timeline.AddTrack audio con index", function(E)
  return agregaPista(E, "audio", {audioType = "stereo", index = 2})
end)
prueba("API", "Timeline.SetTrackName y GetTrackName", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local c1, r1 = llamar(tl, "SetTrackName", "video", 1, "DIAG V1")
  local c2, r2 = llamar(tl, "GetTrackName", "video", 1)
  return (c1 == "ok" and r1 and r2 == "DIAG V1") and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. tostring(r2)
end)
prueba("API", "Timeline.GetItemListInTrack", function(E)
  local como, items = llamar(trabajo(E), "GetItemListInTrack", "video", 1)
  if como ~= "ok" or type(items) ~= "table" then return "NO", "GetItemListInTrack " .. como end
  return (#items > 0) and "OK" or "NO", "n " .. #items
end)
prueba("API", "TimelineItem.GetMediaPoolItem", function(E)
  local como, m = llamar(E.item, "GetMediaPoolItem")
  if como ~= "ok" or not m then return "NO", "GetMediaPoolItem " .. como end
  return (nombreDe(m) == nombreDe(E.negro)) and "OK" or "NO", tostring(nombreDe(m))
end)
prueba("API", "TimelineItem.GetStart GetEnd GetDuration", function(E)
  if not E.item then return "NO", "sin item" end
  local _, s = llamar(E.item, "GetStart")
  local _, e = llamar(E.item, "GetEnd")
  local _, d = llamar(E.item, "GetDuration")
  local ok = type(s) == "number" and type(e) == "number" and type(d) == "number"
  return ok and "OK" or "NO", tostring(s) .. " " .. tostring(e) .. " " .. tostring(d)
end)
prueba("API", "TimelineItem.SetClipColor y GetClipColor", function(E)
  local c1, r1 = llamar(E.item, "SetClipColor", "Orange")
  local c2, r2 = llamar(E.item, "GetClipColor")
  return (c1 == "ok" and r1 and r2 == "Orange") and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. tostring(r2)
end)
prueba("API", "Timeline.SetClipsLinked", function(E)
  local tl = trabajo(E)
  if not tl or not E.item then return "NO", "sin item" end
  local _, audios = llamar(tl, "GetItemListInTrack", "audio", 1)
  local a = type(audios) == "table" and audios[1] or nil
  if not a then return "NO", "sin item de audio para ligar" end
  return medir(E, tl, "SetClipsLinked", {E.item, a}, true)
end)
prueba("API", "Project.GetSetting timelineFrameRate", function(E)
  return consultar(E.proj, "GetSetting", "timelineFrameRate")
end)
prueba("API", "Timeline.GetSetting timelineFrameRate", function(E)
  return consultar(trabajo(E), "GetSetting", "timelineFrameRate")
end)
prueba("API", "Timeline.GetStartFrame", function(E) return consultar(trabajo(E), "GetStartFrame") end)
prueba("API", "MediaPoolItem.GetAudioMapping", function(E) return medir(E, E.negro, "GetAudioMapping") end)
prueba("API", "Timeline.SetName", function(E)
  local tl = crearTimeline(E, "DIAG RENOMBRAR " .. E.clave)
  if not tl then return "NO", "no se creo la timeline" end
  local nuevo = "DIAG RENOMBRADA " .. E.clave
  local c1, r1 = llamar(tl, "SetName", nuevo)
  local got = nombreDe(tl)
  return (c1 == "ok" and r1 and got == nuevo) and "OK" or "NO",
    "SetName " .. c1 .. " " .. tostring(r1) .. ((got == nuevo) and " igual" or " distinto")
end)
-- Items en todas las pistas de video: el generador cae donde Resolve decida
-- (la pista activa), asi que se cuenta todo y se exige uno mas.
local function itemsDeVideo(tl)
  local _, n = llamar(tl, "GetTrackCount", "video")
  if type(n) ~= "number" then return nil end
  local total = 0
  for i = 1, n do
    local _, items = llamar(tl, "GetItemListInTrack", "video", i)
    if type(items) == "table" then total = total + #items end
  end
  return total
end
prueba("API", "Timeline.InsertGeneratorIntoTimeline", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  ponerActual(E, tl)
  local antes = itemsDeVideo(tl)
  local como, res = llamar(tl, "InsertGeneratorIntoTimeline", "Solid Color")
  local despues = itemsDeVideo(tl)
  local sube = antes ~= nil and despues == antes + 1
  return (como == "ok" and res and sube) and "OK" or "NO",
    "Insert " .. como .. " " .. describir(res) .. " items " .. tostring(antes) .. " a " .. tostring(despues)
end)
-- MoveClips es la unica operacion destructiva de la capa Lua. Se mide solo
-- con el negro del diagnostico y entre bins del diagnostico, ida y vuelta.
prueba("API", "MediaPool.MoveClips (solo bins DIAG)", function(E)
  if not (E.negro and E.bin) then return "NO", "sin negro o sin bin" end
  local ok, mover = pcall(function() return E.mp:AddSubFolder(E.bin, "_mover") end)
  if not ok or not mover then return "NO", "no se creo la subcarpeta _mover" end
  local c1, r1 = llamar(E.mp, "MoveClips", {E.negro}, mover)
  local _, clips = llamar(mover, "GetClipList")
  local llego = type(clips) == "table" and #clips > 0
  local c2, r2 = llamar(E.mp, "MoveClips", {E.negro}, E.bin)
  local _, quedan = llamar(mover, "GetClipList")
  local volvio = type(quedan) == "table" and #quedan == 0
  return (c1 == "ok" and r1 and llego and c2 == "ok" and r2 and volvio) and "OK" or "NO",
    "ida " .. tostring(r1) .. " llego " .. tostring(llego) .. " vuelta " .. tostring(r2)
    .. " volvio " .. tostring(volvio)
end)
prueba("API", "MediaPool.DeleteTimelines (solo propia)", function(E)
  local nombre = "DIAG BORRAR " .. E.clave
  local tl = crearTimeline(E, nombre)
  if not tl then return "NO", "no se creo la timeline a borrar" end
  local est, val = interpretar("DeleteTimelines", llamar(E.mp, "DeleteTimelines", {tl}))
  local _, n = llamar(E.proj, "GetTimelineCount")
  for i = 1, (type(n) == "number" and n or 0) do
    local _, t = llamar(E.proj, "GetTimelineByIndex", i)
    if t and nombreDe(t) == nombre then return "NO", val .. " pero sigue ahi" end
  end
  return est, val
end)
prueba("API", "MediaPool.ImportMedia (par sintetico)", function(E)
  local m = E.cfg.media
  if type(m) ~= "table" or type(m.video) ~= "string" or type(m.wav) ~= "string" then
    return "INFO", "config sin par"
  end
  if E.bin then pcall(function() return E.mp:SetCurrentFolder(E.bin) end) end
  local como, lista = llamar(E.mp, "ImportMedia", {m.video, m.wav})
  if como ~= "ok" then return "NO", "ImportMedia " .. como .. " " .. tostring(lista) end
  if type(lista) == "table" and lista[1] and lista[2] then E.par = lista end
  return E.par and "OK" or "NO", describir(lista)
end)
prueba("API", "MediaPool.AutoSyncAudio (par)", function(E)
  if not E.cfg.permitir_pool then return "INFO", "omitido permitir_pool false" end
  if not E.par then return "NO", "sin par" end
  local ajustes = {}
  local MODE, WAVE = constante(E, "AUDIO_SYNC_MODE"), constante(E, "AUDIO_SYNC_WAVEFORM")
  local CHAN, AUTO = constante(E, "AUDIO_SYNC_CHANNEL_NUMBER"), constante(E, "AUDIO_SYNC_CHANNEL_AUTOMATIC")
  local RA = constante(E, "AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO")
  local RM = constante(E, "AUDIO_SYNC_RETAIN_VIDEO_METADATA")
  if MODE ~= nil and WAVE ~= nil then ajustes[MODE] = WAVE end
  if CHAN ~= nil and AUTO ~= nil then ajustes[CHAN] = AUTO end
  if RA ~= nil then ajustes[RA] = true end
  if RM ~= nil then ajustes[RM] = true end
  return medir(E, E.mp, "AutoSyncAudio", {E.par[1], E.par[2]}, ajustes)
end)
prueba("API", "colision de dos markers en el mismo frame", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local _, a = llamar(tl, "AddMarker", 20, "Sand", "diez50 A", "", 1, "diez50prueba:colision:a")
  local _, b = llamar(tl, "AddMarker", 20, "Lemon", "diez50 B", "", 1, "diez50prueba:colision:b")
  return "INFO", "primero " .. tostring(a) .. " segundo " .. tostring(b)
end)
-- S10: las utility functions nuevas de 21.1 sobre el objeto resolve.
for _, nombre in ipairs({"GetCurrentProject", "GetCurrentTimeline", "GetMediaPool", "GetGallery"}) do
  prueba("API", "Resolve." .. nombre .. " (21.1)", function(E) return medir(E, E.R, nombre) end)
end
-- S11: las 20 APIs nuevas de 21.1. Las de escritura van sobre la timeline de
-- trabajo; las que tocan el Media Pool, detras de permitir_pool.
prueba("API", "Project.GetAudioRenderCodecs", function(E) return medir(E, E.proj, "GetAudioRenderCodecs") end)
prueba("API", "Project.GetAudioRenderFormats", function(E) return medir(E, E.proj, "GetAudioRenderFormats") end)
prueba("API", "Resolve.GetKeyboardPresetList", function(E) return medir(E, E.R, "GetKeyboardPresetList") end)
prueba("API", "Project.GetPresetList", function(E) return medir(E, E.proj, "GetPresetList") end)
prueba("API", "Timeline.GetNormalizeAudioModes", function(E) return medir(E, trabajo(E), "GetNormalizeAudioModes") end)
prueba("API", "Timeline.GetOutputBlanking", function(E) return medir(E, trabajo(E), "GetOutputBlanking") end)
prueba("API", "MediaStorage.GetCloneStatus", function(E)
  local como, ms = llamar(E.R, "GetMediaStorage")
  if como ~= "ok" or not ms then return "NO", "GetMediaStorage " .. como end
  return medir(E, ms, "GetCloneStatus")
end)
prueba("API", "MediaPoolItem.GetTranscription", function(E) return medir(E, E.negro, "GetTranscription") end)
prueba("API", "TimelineItem.GetType", function(E) return medir(E, E.item, "GetType") end)
prueba("API", "TimelineItem.GetFades", function(E) return medir(E, E.item, "GetFades") end)
prueba("API", "TimelineItem.GetSpeed", function(E) return medir(E, E.item, "GetSpeed") end)
-- Ida y vuelta: GetFades tiene que devolver el fadeIn que se puso. Si la
-- forma de lo que devuelve no es la esperada, se describe y queda en INFO.
prueba("API", "TimelineItem.SetFades y GetFades", function(E)
  if not E.item then return "NO", "sin item" end
  local c1, r1 = llamar(E.item, "SetFades", {fadeIn = 12})
  if c1 ~= "ok" or not r1 then return "NO", "SetFades " .. c1 .. " " .. tostring(r1) end
  local c2, r2 = llamar(E.item, "GetFades")
  if c2 ~= "ok" then return "NO", "set true GetFades " .. c2 end
  if type(r2) ~= "table" then return "INFO", "set true get " .. describir(r2) end
  local v = r2.fadeIn or r2.FadeIn or r2.fade_in
  if tonumber(v) == 12 then return "OK", "set true get fadeIn 12" end
  if v ~= nil then return "NO", "set true get fadeIn " .. tostring(v) end
  return "INFO", "set true get " .. describir(r2)
end)
prueba("API", "Timeline.AutoAlignClips", function(E) return medir(E, trabajo(E), "AutoAlignClips") end)
prueba("API", "Timeline.NormalizeAudioLevel", function(E)
  if not E.item then return "NO", "sin item" end
  return medir(E, trabajo(E), "NormalizeAudioLevel", {E.item}, -23.0, "ITU-R BS.1770-4", false)
end)
prueba("API", "TimelineItem.AddTransition", function(E) return medir(E, E.item, "AddTransition", "start") end)
prueba("API", "Timeline.SetOutputBlanking y GetOutputBlanking", function(E)
  local tl = trabajo(E)
  if not tl then return "NO", "sin timeline de trabajo" end
  local c1, r1 = llamar(tl, "SetOutputBlanking", "1.85")
  if c1 ~= "ok" or not r1 then return "NO", "SetOutputBlanking " .. c1 .. " " .. tostring(r1) end
  local c2, r2 = llamar(tl, "GetOutputBlanking")
  if c2 ~= "ok" then return "NO", "set true GetOutputBlanking " .. c2 end
  local hay = string.find(tostring(r2), "1.85", 1, true) ~= nil
  return hay and "OK" or "NO", "set true get " .. describir(r2)
end)
prueba("API", "MediaPool.CreateMulticamClip", function(E)
  if not E.cfg.permitir_pool then return "INFO", "omitido permitir_pool false" end
  if not E.negro then return "NO", "sin negro" end
  return medir(E, E.mp, "CreateMulticamClip", {E.negro}, {startTimecode = "01:00:00:00"})
end)
prueba("API", "TimelineItem.FlattenMulticam", function(E)
  if not E.cfg.permitir_pool then return "INFO", "omitido permitir_pool false" end
  return medir(E, E.item, "FlattenMulticam")
end)
prueba("API", "TimelineItem.PerformMulticamSmartSwitch", function(E)
  if not E.cfg.permitir_pool then return "INFO", "omitido permitir_pool false" end
  return medir(E, E.item, "PerformMulticamSmartSwitch", 1, 0)
end)
prueba("API", "MediaPoolItem.SetAudioMapping", function(E)
  -- Reescribe el mapeo del clip y no hay forma honesta de devolverlo; aunque
  -- el clip sea del diagnostico, medirlo exige un mapeo valido que aun no
  -- conocemos. Queda anotado, no medido.
  return "INFO", "no se mide destructivo sin mapeo conocido"
end)

-- ===== EXP: exportes de la timeline de marcas (contrato §8) ================
local TIPOS_EXP = {
  {tipo = "drt", ext = "drt", k = {"EXPORT_DRT"}},
  {tipo = "otio", ext = "otio", k = {"EXPORT_OTIO"}},
  {tipo = "fcpxml", ext = "fcpxml", k = {"EXPORT_FCPXML_1_10", "EXPORT_FCPXML_1_11",
                                         "EXPORT_FCPXML_1_9", "EXPORT_FCPXML_1_8"}},
  {tipo = "edl", ext = "edl", k = {"EXPORT_EDL"}, sub = "EXPORT_NONE"},
  {tipo = "aaf", ext = "aaf", k = {"EXPORT_AAF"}, sub = "EXPORT_AAF_NEW"},
  {tipo = "csv", ext = "csv", k = {"EXPORT_TEXT_CSV"}},
  {tipo = "tab", ext = "txt", k = {"EXPORT_TEXT_TAB"}},
}
-- Dentro de Resolve el estado es solo el retorno: que el archivo exista lo
-- confirma el lector desde fuera.
--
-- Destino PLANO (contrato §8): <base>/<clave>_<tipo>.<ext>. No se usa una
-- carpeta por clave porque nadie puede crearla: la clave lleva r, que solo se
-- sabe al correr, y desde el sandbox del menu no hay os ni lfs para hacer una
-- carpeta. Si Resolve no la creara al exportar, Export fallaria por la
-- carpeta y la matriz diria que Free no exporta. <base> (recibos/ y el de la
-- .dmg) si lo crea preparar.
--
-- El valor nombra el destino ("EXPORT_DRT recibos devolvio true", "...
-- volumen ..."): el lector lo usa para saber en que carpeta buscar el
-- archivo, sin depender del orden de este catalogo.
local function exportar(E, base, destino, t)
  if type(base) ~= "string" then return "INFO", t.tipo .. " " .. destino .. " sin destino" end
  if not E.marcas then return "NO", t.tipo .. " " .. destino .. " sin timeline de marcas" end
  local c, cnombre = nil, nil
  for _, n in ipairs(t.k) do
    c = constante(E, n)
    if c ~= nil then cnombre = n; break end
  end
  if c == nil then return "NO", t.k[1] .. " " .. destino .. " no existe" end
  local s = nil
  if t.sub then
    s = constante(E, t.sub)
    if s == nil then return "NO", t.sub .. " " .. destino .. " no existe" end
  end
  local ruta = base .. "/" .. E.clave .. "_" .. t.tipo .. "." .. t.ext
  local como, res
  if s ~= nil then
    como, res = llamar(E.marcas, "Export", ruta, c, s)
  else
    como, res = llamar(E.marcas, "Export", ruta, c)
  end
  if como ~= "ok" then return "NO", cnombre .. " " .. destino .. " " .. como .. " " .. tostring(res) end
  return res and "OK" or "NO", cnombre .. " " .. destino .. " devolvio " .. tostring(res)
end
for _, t in ipairs(TIPOS_EXP) do
  prueba("EXP", "Export " .. t.tipo .. " a recibos", function(E)
    return exportar(E, E.cfg.recibos, "recibos", t)
  end)
end
for _, t in ipairs(TIPOS_EXP) do
  prueba("EXP", "Export " .. t.tipo .. " al volumen", function(E)
    return exportar(E, E.cfg.recibos_volumen, "volumen", t)
  end)
end
-- Igual que Export: el lector confirma el archivo, asi que aqui manda el
-- retorno (sin la degradacion de `medir`). Mismo destino plano.
prueba("EXP", "MediaPool.ExportMetadata", function(E)
  if type(E.cfg.recibos) ~= "string" then return "INFO", "ExportMetadata recibos sin destino" end
  if not E.negro then return "NO", "ExportMetadata recibos sin negro" end
  local ruta = E.cfg.recibos .. "/" .. E.clave .. "_metadata.csv"
  local est, val = interpretar("ExportMetadata", llamar(E.mp, "ExportMetadata", ruta, {E.negro}))
  return est, (string.gsub(val, "^ExportMetadata", "ExportMetadata recibos", 1))
end)

-- ===== META: metadatos y customData, ida y vuelta ==========================
prueba("META", "SetThirdPartyMetadata forma tabla", function(E)
  if not E.negro then return "NO", "sin negro" end
  local v = "tabla " .. E.clave
  local c1, r1 = llamar(E.negro, "SetThirdPartyMetadata", {Diez50Diag = v})
  local c2, r2 = llamar(E.negro, "GetThirdPartyMetadata", "Diez50Diag")
  local igual = c2 == "ok" and r2 == v
  return (c1 == "ok" and r1 and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. (igual and "igual" or (c2 .. " " .. tostring(r2)))
end)
prueba("META", "SetThirdPartyMetadata forma vieja (2 args)", function(E)
  if not E.negro then return "NO", "sin negro" end
  local v = "vieja " .. E.clave
  local c1, r1 = llamar(E.negro, "SetThirdPartyMetadata", "Diez50Diag2", v)
  local c2, r2 = llamar(E.negro, "GetThirdPartyMetadata", "Diez50Diag2")
  local igual = c2 == "ok" and r2 == v
  return (c1 == "ok" and r1 and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. (igual and "igual" or (c2 .. " " .. tostring(r2)))
end)
prueba("META", "SetMetadata Comments", function(E)
  if not E.negro then return "NO", "sin negro" end
  local v = "diez50diag " .. E.clave
  local c1, r1 = llamar(E.negro, "SetMetadata", "Comments", v)
  local c2, r2 = llamar(E.negro, "GetMetadata", "Comments")
  local igual = c2 == "ok" and r2 == v
  return (c1 == "ok" and r1 and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " get " .. (igual and "igual" or (c2 .. " " .. tostring(r2)))
end)
-- Re-hornear sin borrar timelines depende de esto: poner, encontrar y quitar
-- SOLO lo que puso el asistente, por su customData.
local function idaVueltaMarker(obj, frame, cd)
  if not obj then return "NO", "sin objeto" end
  local c1, r1 = llamar(obj, "AddMarker", frame, "Purple", "diez50 prueba", "customData", 1, cd)
  local c2, r2 = llamar(obj, "GetMarkerByCustomData", cd)
  local hallado = c2 == "ok" and type(r2) == "table" and next(r2) ~= nil
  local c3, r3 = llamar(obj, "GetMarkers")
  local m = (c3 == "ok" and type(r3) == "table") and r3[frame] or nil
  local enLista = type(m) == "table" and m.customData == cd
  local c4, r4 = llamar(obj, "DeleteMarkerByCustomData", cd)
  local c5, r5 = llamar(obj, "GetMarkerByCustomData", cd)
  local limpio = c5 == "ok" and not (type(r5) == "table" and next(r5) ~= nil)
  local todo = c1 == "ok" and r1 and hallado and enLista and c4 == "ok" and r4 and limpio
  return todo and "OK" or "NO", "add " .. tostring(r1) .. " get " .. tostring(hallado)
    .. " lista " .. tostring(enLista) .. " del " .. tostring(r4) .. " limpio " .. tostring(limpio)
end
prueba("META", "customData en marker de timeline", function(E)
  return idaVueltaMarker(trabajo(E), 30, "diez50prueba:meta:timeline:" .. E.clave)
end)
prueba("META", "customData en marker de TimelineItem", function(E)
  return idaVueltaMarker(E.item, 5, "diez50prueba:meta:item:" .. E.clave)
end)
prueba("META", "customData en marker de MediaPoolItem", function(E)
  return idaVueltaMarker(E.negro, 30, "diez50prueba:meta:clip:" .. E.clave)
end)

-- ===== LARGO: muchos AppendToTimeline seguidos =============================
prueba("LARGO", "N AppendToTimeline en una timeline aparte", function(E)
  local n = math.floor(tonumber(E.cfg.largo_n) or 0)
  if n <= 0 then return "INFO", "largo_n 0" end
  if not E.negro then return "NO", "sin negro" end
  local tl = crearTimeline(E, "DIAG LARGO " .. E.clave)
  if not tl then return "NO", "no se creo la timeline" end
  ponerActual(E, tl)
  local t0 = reloj()
  local bien = 0
  for _ = 1, n do
    local ok, res = pcall(function()
      return E.mp:AppendToTimeline({{mediaPoolItem = E.negro, startFrame = 0, endFrame = 23}})
    end)
    if ok and type(res) == "table" and res[1] then bien = bien + 1 end
  end
  local ms = lapso(t0)
  local c, items = llamar(tl, "GetItemListInTrack", "video", 1)
  local en = (c == "ok" and type(items) == "table") and #items or -1
  return (en == n) and "OK" or "NO", "pedidos " .. n .. " ok " .. bien .. " en pista " .. en .. ms
end)

-- ===== IDEM: correr dos veces con el mismo sello ===========================
prueba("IDEM", "corrida previa con el mismo sello y ctx", function(E)
  return "INFO", "previa:" .. (E.previa and "si" or "no") .. " r:" .. E.r
end)

-- ---------- documento JSON (§7) ------------------------------------------
local function documento(E, parcial)
  local ok = 0
  for _, r in ipairs(E.resultados) do if r.estado == "OK" then ok = ok + 1 end end
  local doc = {formato = D.FORMATO, clave = E.clave, sello = E.sello, ctx = E.ctx,
               r = E.r, version = E.version, producto = E.producto, ok = ok,
               total = #E.resultados, resultados = E.resultados}
  if parcial then doc.parcial = true end
  return json(doc)
end

-- ===== PREFS: canal 4, medido al escribirlo ================================
-- Un solo SetPrefs + SavePrefs (lo que hace AutoSubs en Free 21.1). Va casi al
-- final para llevar todo lo medido; PREFS y SAVE no pueden ir dentro de si
-- mismos, por eso esta copia se marca "parcial".
prueba("PREFS", "SetPrefs + SavePrefs + GetPrefs", function(E)
  local F = fusionObj(E)
  if not F then return "NO", "sin fusion" end
  local clave = "Global.Diez50.Diag." .. E.ctx
  local doc = documento(E, true)
  local c1, r1 = llamar(F, "SetPrefs", clave, doc)
  local c2, r2 = llamar(F, "SavePrefs")
  local c3, r3 = llamar(F, "GetPrefs", clave)
  local igual = c3 == "ok" and r3 == doc
  return (c1 == "ok" and c2 == "ok" and igual) and "OK" or "NO",
    "set " .. c1 .. " " .. tostring(r1) .. " save " .. c2 .. " " .. tostring(r2)
    .. " get " .. (igual and "igual" or c3)
end)

-- ===== SAVE: canal 7 =======================================================
prueba("SAVE", "ProjectManager.SaveProject", function(E) return medir(E, E.pm, "SaveProject") end)

-- ---------- canales de reporte --------------------------------------------
local COLOR = {OK = "Green", NO = "Red", ERR = "Red", INFO = "Yellow"}
local ESTADOS = {OK = true, NO = true, INFO = true, ERR = true}

local function canalMarcas(E, k, r)
  if not E.marcas then return end
  pcall(function()
    return E.marcas:AddMarker(k * 24, COLOR[r.estado], r.id, r.estado .. " " .. r.valor, 1,
                              "diez50diag:" .. r.id .. "=" .. r.estado .. "|" .. r.valor)
  end)
end

local function canalBin(E, r)
  if not E.bin then return end
  -- Antes solo se cambiaba '/': en la primera corrida real (Studio 21.1.0.17,
  -- 2026-10-01) OS-16, LOAD-05 y API-33 se quedaron sin bin por un ':' o un '"'.
  local nombre = r.id .. " " .. r.estado .. " " .. string.sub(r.valor, 1, 60)
  nombre = nombreSeguro(nombre)
  nombre = string.gsub(nombre, " +$", "")
  pcall(function() return E.mp:AddSubFolder(E.bin, nombre) end)
end

-- [mide] canal 5: print. En el menu de la build 17 puede no existir.
local function canalPrint(E, ok, total)
  for _, r in ipairs(E.resultados) do
    pcall(function() print("DIEZ50DIAG " .. r.id .. " " .. r.estado .. " " .. r.valor) end)
  end
  pcall(function() print("DIEZ50DIAG FIN " .. E.clave .. " " .. ok .. "/" .. total) end)
end
-- [/mide]

-- [mide] canal 6: io. El recibo completo en disco, si hay io.
local function canalIO(E)
  pcall(function()
    local dir = E.cfg.recibos
    if type(dir) ~= "string" or type(io) ~= "table" then return end
    local f = io.open(dir .. "/" .. E.clave .. ".json", "w")
    if not f then return end
    f:write(documento(E, false), "\n")
    f:close()
  end)
end
-- [/mide]

-- [mide] guardia: el unico aviso posible sin tocar el proyecto ajeno.
-- El contrato (§3) pone el aviso en la misma clave que la corrida buena. Si
-- esa clave ya guarda el documento de una corrida buena, pisarla borraria el
-- canal 4 de esa corrida (en memoria y, si Fusion guarda al salir, en
-- Fusion.prefs) por un error de dedo. Entonces el aviso va a la clave hermana
-- "<ctx>_aborta", que el lector reconoce igual (busca Diez50.Diag.<algo> y
-- separa los documentos con "aborta").
local function abortar(E, nombre)
  pcall(function() print("DIEZ50DIAG ABORTA proyecto=" .. nombre) end)
  pcall(function()
    local F = fusionObj(E)
    local doc = json({aborta = "proyecto", proyecto = nombre})
    local clave = "Global.Diez50.Diag." .. E.ctx
    local okg, previo = pcall(function() return F:GetPrefs(clave) end)
    if okg and type(previo) == "string" and string.find(previo, '"formato"', 1, true)
       and not string.find(previo, '"aborta"', 1, true) then
      clave = clave .. "_aborta"
    end
    return F:SetPrefs(clave, doc)
  end)
  return {aborta = "proyecto", proyecto = nombre}
end
-- [/mide]

-- ---------- preparacion ---------------------------------------------------
local function calcularCorrida(E)
  local patron = "^DIAG RESUMEN " .. patronLiteral(E.sello .. "-" .. E.ctx .. "-r") .. "(%d+)"
  local maximo = 0
  local _, n = llamar(E.proj, "GetTimelineCount")
  for i = 1, (type(n) == "number" and n or 0) do
    local _, tl = llamar(E.proj, "GetTimelineByIndex", i)
    local nombre = tl and nombreDe(tl)
    if type(nombre) == "string" then
      local r = tonumber(string.match(nombre, patron))
      if r and r > maximo then maximo = r end
    end
  end
  return maximo + 1, maximo > 0
end

local function preparar(E)
  local _, v = llamar(E.R, "GetVersionString")
  local _, p = llamar(E.R, "GetProductName")
  E.version = (type(v) == "string") and v or "?"
  E.producto = (type(p) == "string") and p or "?"
  E.mp = select(2, llamar(E.proj, "GetMediaPool"))
  E.root = select(2, llamar(E.mp, "GetRootFolder"))
  E.r, E.previa = calcularCorrida(E)
  E.clave = E.sello .. "-" .. E.ctx .. "-r" .. E.r

  -- Canal 1 primero: si todo lo demas falla, al menos esta timeline dice que
  -- el diagnostico corrio, con que clave y cuantas pruebas tenia.
  E.resumen = crearTimeline(E, "DIAG RESUMEN " .. E.clave .. " - de " .. #D.PRUEBAS)

  -- Canal 3: el bin propio; el negro se importa DENTRO de el.
  local okb, bin = pcall(function() return E.mp:AddSubFolder(E.root, "DIEZ50 DIAG " .. E.clave) end)
  E.bin = (okb and bin) or nil
  E.setup.bin = okb and tostring(bin) or msg(bin)
  if E.bin then pcall(function() return E.mp:SetCurrentFolder(E.bin) end) end

  local negro = type(E.cfg.media) == "table" and E.cfg.media.negro or nil
  if type(negro) == "string" then
    local ok, lista = pcall(function() return E.mp:ImportMedia({negro}) end)
    E.setup.importar = {ok = ok, res = lista}
    if ok and type(lista) == "table" and lista[1] then
      E.negro = lista[1]
    else
      E.negro = buscarNegroPrevio(E, negro)
      E.setup.importar.previo = E.negro ~= nil
    end
  end

  -- Canal 2: la timeline de marcas con el negro, lista antes de la primera
  -- prueba para que cada resultado se marque en cuanto sale.
  E.marcas = crearTimeline(E, "DIAG MARCAS " .. E.clave)
  if E.marcas and E.negro then
    ponerActual(E, E.marcas)
    local ok, res = pcall(function() return E.mp:AppendToTimeline({E.negro}) end)
    E.setup.append = {ok = ok, res = res}
  end
end

-- ---------- correr --------------------------------------------------------
-- El prefijo que exige la guardia. config.lua puede AFINARLO
-- ("DIEZ50_DIAG_FREE") pero no aflojarlo: tiene que empezar con
-- DIEZ50_DIAG. Si no (vacio, "E", otro tipo), vale el de fabrica. Sin esto,
-- proyecto_requerido = "" dejaba pasar cualquier proyecto, uno real incluido,
-- porque en Lua "" es verdadero y todo nombre empieza con "".
function D.prefijoRequerido(valor)
  local base = D.PROYECTO_POR_DEFECTO
  if type(valor) == "string" and string.sub(valor, 1, #base) == base then return valor end
  return base
end

function D.correr(ctx)
  ctx = (type(ctx) == "table") and ctx or {}
  local E = {resultados = {}, setup = {}, creadas = {}, cfg = {}}
  E.ctx = string.gsub(tostring(ctx.ctx or "desconocido"), "[^%w_]", "_")
  E.ctxResolve = ctx.resolve
  E.ctxFusion = ctx.fusion
  E.R = ctx.resolve
  if E.R == nil then E.R = resolve end

  local okc, cfg = false, "sin ruta de config"
  if type(ctx.config) == "string" then okc, cfg = pcall(dofile, ctx.config) end
  if okc and type(cfg) == "table" then E.cfg = cfg
  else E.cfgError = "config " .. msg(cfg) end
  E.sello = string.gsub(tostring(E.cfg.sello or "sinsello"), "[^%w_%-]", "_")
  local requerido = D.prefijoRequerido(E.cfg.proyecto_requerido)

  -- Guardia (§3): sin el proyecto desechable, nada se crea.
  local _, pm = llamar(E.R, "GetProjectManager")
  local _, proj = llamar(pm, "GetCurrentProject")
  local _, nombre = llamar(proj, "GetName")
  if type(nombre) ~= "string" then nombre = "(sin proyecto)" end
  E.nombreProyecto = nombre
  if not proj or string.sub(nombre, 1, #requerido) ~= requerido then
    return abortar(E, nombre)
  end
  E.pm, E.proj = pm, proj

  local okp, errp = pcall(preparar, E)
  if not okp then E.setup.error = msg(errp) end
  if not E.clave then E.clave = E.sello .. "-" .. E.ctx .. "-r" .. tostring(E.r or 0) end

  for k, p in ipairs(D.PRUEBAS) do
    local ok, estado, valor = pcall(p.f, E)
    if not ok then
      valor = estado
      estado = "ERR"
    elseif not ESTADOS[estado] then
      valor = "estado invalido " .. tostring(estado)
      estado = "ERR"
    end
    local r = {id = p.id, familia = p.familia, estado = estado, valor = limpiarValor(valor)}
    E.resultados[#E.resultados + 1] = r
    canalMarcas(E, k, r)
    canalBin(E, r)
  end

  local ok = 0
  for _, r in ipairs(E.resultados) do if r.estado == "OK" then ok = ok + 1 end end
  local total = #E.resultados

  -- Canal 1: el resumen toma su nombre final (o nace ahora si no hay SetName).
  local final = "DIAG RESUMEN " .. E.clave .. " " .. ok .. " de " .. total
  local renombrado = false
  if E.resumen then
    local c1, r1 = llamar(E.resumen, "SetName", final)
    renombrado = c1 == "ok" and r1 and nombreDe(E.resumen) == final
  end
  if not renombrado then
    local tl = crearTimeline(E, final)
    if tl then E.resumen = tl end
  end
  if E.resumen then ponerActual(E, E.resumen) end

  canalPrint(E, ok, total)
  canalIO(E)
  -- Otra vez, en silencio: SAVE-01 guardo antes de renombrar el resumen.
  pcall(function() return E.pm:SaveProject() end)

  return {clave = E.clave, ok = ok, total = total, resultados = E.resultados,
          setup_error = E.setup.error}
end

return D
