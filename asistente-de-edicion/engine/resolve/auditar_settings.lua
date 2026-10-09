-- ============================================================
--  auditar_settings.lua — vuelca los settings del proyecto y de sus timelines
--
--  Consola de DaVinci Resolve (modo Lua), con el proyecto ABIERTO:
--    DESTINO = "/Volumes/.../.cinema_assistant/resolve"   -- opcional
--    dofile("<MOTOR>/resolve/auditar_settings.lua")   -- ~/cinema-assistant
--
--  POR QUE HACE FALTA UN SCRIPT PARA ESTO
--  Los cortes, las pistas y los items de una timeline se pueden leer desde
--  fuera: el Project.db de la base de disco es SQLite y ahi esta todo
--  (lib/timeline_resolve.py). Los SETTINGS no. Comprobado el 2026-08-19 sobre
--  Resolve 21.0.2: viven en `SM_Config.SetupBA`, un struct binario de 2704
--  bytes SIN nombres de campo. No hay forma honesta de leerlos de ahi, y
--  adivinar el offset de cada uno seria inventar.
--
--  Asi que este es el unico camino: pedirselos a la API con el proyecto
--  abierto. Es un script de LECTURA — no toca ni un ajuste.
--
--  QUE SACO LA PRIMERA VEZ (Morsa, 2026-08-19)
--  Nada verificaba los settings de proyecto en todo el motor. Tres cosas que
--  el editor sabia y el motor no: las timelines de corte van a 3840x2160 y las
--  de reel a 2160x3840; todo el proyecto esta a 23.976; y existe un
--  `cut 1.3 gamma2.4.mov` al lado de `cut 1.3.mov`, o sea que hubo una pelea
--  de gamma que no quedo escrita en ningun sitio.
-- ============================================================

local function line() print(string.rep("=", 60)) end

local resolve = Resolve()
if not resolve then print("No hay Resolve."); return end
local pm = resolve:GetProjectManager()
local proj = pm and pm:GetCurrentProject()
if not proj then print("No hay proyecto abierto."); return end

local nombre = proj:GetName()
line(); print("  SETTINGS — " .. tostring(nombre)); line()

-- Los que importan para entregar. El resto se vuelca al JSON igual, pero estos
-- se imprimen para que se vean sin abrir el archivo.
local CLAVE = {
  "timelineFrameRate", "timelinePlaybackFrameRate",
  "timelineResolutionWidth", "timelineResolutionHeight",
  "timelineOutputResolutionWidth", "timelineOutputResolutionHeight",
  "superScale", "timelineInterlaceProcessing",
  "colorScienceMode", "colorSpaceTimeline", "colorSpaceOutput",
  "colorSpaceInput", "inputDRT", "outputDRT",
  "timelineWorkingLuminanceMode", "useCATransform", "useColorSpaceAwareGradingTools",
  "audioCaptureNumChannels", "timelineOutputPixelAspectRatio",
  "videoMonitorFormat", "videoDataLevels", "videoMonitorUseRec601CSC",
  "useCustomSettings", "timelineDropFrameTimecode", "timelineSaveThumbs",
  "audioOutputHasTimecode", "isAutoColorEnable",
}

local function esc(s)
  s = tostring(s)
  s = s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "")
  return s
end

local function volcarTabla(f, t, sangria)
  local claves = {}
  for k in pairs(t or {}) do claves[#claves + 1] = tostring(k) end
  table.sort(claves)
  for i, k in ipairs(claves) do
    f:write(string.format('%s"%s": "%s"%s\n', sangria, esc(k), esc(t[k]),
                          (i < #claves) and "," or ""))
  end
end

-- 1. settings de PROYECTO --------------------------------------------------
local ps = {}
do
  local ok, res = pcall(function() return proj:GetSetting() end)
  if ok and type(res) == "table" then
    ps = res
  else
    -- Algunas builds no devuelven el diccionario completo sin argumento. Se
    -- pide clave por clave, que siempre funciona.
    for _, k in ipairs(CLAVE) do
      local ok2, v = pcall(function() return proj:GetSetting(k) end)
      if ok2 and v ~= nil and v ~= "" then ps[k] = v end
    end
  end
end
print(string.format("  proyecto: %d ajustes leidos", (function()
  local n = 0; for _ in pairs(ps) do n = n + 1 end; return n end)()))
for _, k in ipairs(CLAVE) do
  if ps[k] ~= nil then print(string.format("    %-38s %s", k, tostring(ps[k]))) end
end

-- 2. settings de cada TIMELINE ---------------------------------------------
local n = proj:GetTimelineCount() or 0
local tls = {}
print("")
print(string.format("  %d timelines", n))
for i = 1, n do
  local tl = proj:GetTimelineByIndex(i)
  if tl then
    local s = {}
    local ok, res = pcall(function() return tl:GetSetting() end)
    if ok and type(res) == "table" then
      s = res
    else
      for _, k in ipairs(CLAVE) do
        local ok2, v = pcall(function() return tl:GetSetting(k) end)
        if ok2 and v ~= nil and v ~= "" then s[k] = v end
      end
    end
    local w = s["timelineResolutionWidth"] or ps["timelineResolutionWidth"] or "?"
    local h = s["timelineResolutionHeight"] or ps["timelineResolutionHeight"] or "?"
    local fr = s["timelineFrameRate"] or ps["timelineFrameRate"] or "?"
    local custom = s["useCustomSettings"] or "0"
    print(string.format("    %-32s %sx%s @ %s%s", tl:GetName(), tostring(w),
                        tostring(h), tostring(fr),
                        (tostring(custom) == "1") and "  (custom)" or ""))
    tls[#tls + 1] = {nombre = tl:GetName(), settings = s,
                     inicio = tl:GetStartTimecode()}
  end
end

-- 3. al disco --------------------------------------------------------------
local destino = DESTINO
if not destino or destino == "" then
  destino = os.getenv("HOME") .. "/cinema-assistant/logs"
  print("")
  print("  (sin DESTINO: se escribe en " .. destino .. ")")
end
local slug = tostring(nombre):lower():gsub("[^%w]+", "_"):gsub("^_+", ""):gsub("_+$", "")
local ruta = destino .. "/" .. slug .. "_settings.json"
local f = io.open(ruta, "w")
if not f then
  print("")
  print("  NO se pudo escribir " .. ruta .. " — ¿existe la carpeta?")
  return
end
f:write("{\n")
f:write(string.format('  "proyecto": "%s",\n', esc(nombre)))
f:write('  "settings": {\n'); volcarTabla(f, ps, "    "); f:write("  },\n")
f:write('  "timelines": [\n')
for i, t in ipairs(tls) do
  f:write(string.format('    {"nombre": "%s", "inicio": "%s", "settings": {\n',
                        esc(t.nombre), esc(t.inicio or "")))
  volcarTabla(f, t.settings, "      ")
  f:write(string.format('    }}%s\n', (i < #tls) and "," or ""))
end
f:write("  ]\n}\n")
f:close()

print("")
line()
print("  escrito: " .. ruta)
print("  Este script NO cambia ningun ajuste. Solo mira.")
line()
