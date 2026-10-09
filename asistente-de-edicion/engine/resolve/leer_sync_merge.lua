-- ============================================================
--  leer_sync_merge.lua — que consiguio AutoSyncAudio, y con que offset
--
--  Consola de DaVinci Resolve (modo Lua):
--    SALIDA = "/ruta/al/disco/.cinema_assistant/resolve/<proyecto>_merge.json"
--    dofile("<MOTOR>/resolve/leer_sync_merge.lua")
--
--  PARA QUE
--  El motor empareja video con audio por cuatro senales y mide el offset por
--  correlacion. Cuando NO puede —porque el A1 de esa camara esta tapado por la
--  musica del evento y el correlador solo devuelve picos falsos— queda la
--  opcion de que lo intente Resolve con su propio AutoSyncAudio.
--
--  Este script NO sincroniza nada: LEE. Recorre el Media Pool despues del
--  merge y anota, clip por clip, si quedo con audio ligado y con que offset.
--  Con eso `bin/import_iban_offsets.py` puede convertir lo que Resolve logro en
--  anclas del manifest, y de ahi el resto sale por reloj como siempre.
--
--  CONVENCION DE SIGNOS (metodologia/merge-media-pool.md):
--      manifest: offset = audio_start - video_start        -> -5.000 s
--      Resolve : linked_audio[n].offset, en MUESTRAS       -> +240000
--      offset_manifest_seg == -offset_resolve_seg
--
--  Es de solo lectura: no crea timelines, no toca clips, no borra nada.
-- ============================================================

local function line() print(string.rep("=", 56)) end
line(); print("  LEER SYNC DEL MERGE"); line()

local pm = resolve:GetProjectManager()
local proj = pm and pm:GetCurrentProject()
if not proj then print("ERROR: no hay proyecto abierto."); return end
local mp = proj:GetMediaPool()
print("Proyecto: " .. tostring(proj:GetName()))

local SALIDA_PATH = SALIDA or ""
if SALIDA_PATH == "" then
  print("ERROR: falta la ruta de salida. Antes del dofile:")
  print('  SALIDA = "<disco>/.cinema_assistant/resolve/<proyecto>_merge.json"')
  return
end

-- ---------- recorrer el pool --------------------------------------------
local clips = {}
local function walk(folder)
  for _, c in ipairs(folder:GetClipList() or {}) do clips[#clips+1] = c end
  for _, s in ipairs(folder:GetSubFolderList() or {}) do walk(s) end
end
walk(mp:GetRootFolder())
print("Clips en el Media Pool: " .. #clips)

-- ---------- leer el mapping de audio ------------------------------------
-- Mismo lector que usa el asistente para detectar MODO MERGE, mas la
-- extraccion del offset y de la ruta del WAV ligado.
local function mappingDe(mpi)
  if not (mpi and mpi.GetAudioMapping) then return nil end
  local ok, m = pcall(function() return mpi:GetAudioMapping() end)
  if not ok or not m then return nil end
  return tostring(m)
end

local function esc(s)
  s = tostring(s or "")
  s = string.gsub(s, "\\", "\\\\")
  s = string.gsub(s, '"', '\\"')
  s = string.gsub(s, "\n", " ")
  return s
end

local filas, nLig, nSin = {}, 0, 0
for _, c in ipairs(clips) do
  local fp = c.GetClipProperty and c:GetClipProperty("File Path") or ""
  local nombre = c.GetName and c:GetName() or "?"
  local m = mappingDe(c)
  local ligados, offsets, rutas = 0, {}, {}
  if m and string.match(m, '"linked_audio"%s*:%s*{%s*"') then
    -- El mapping es JSON; se lee con patrones porque el Lua de la Consola no
    -- trae parser. Cada entrada de linked_audio aporta su path y su offset.
    for bloque in string.gmatch(m, '"%d+"%s*:%s*({[^}]-})') do
      local ruta = string.match(bloque, '"path"%s*:%s*"([^"]*)"')
      local off = string.match(bloque, '"offset"%s*:%s*(%-?%d+)')
      if ruta then
        ligados = ligados + 1
        rutas[#rutas+1] = ruta
        offsets[#offsets+1] = off or ""
      end
    end
  end
  if ligados > 0 then nLig = nLig + 1 else nSin = nSin + 1 end
  filas[#filas+1] = string.format(
    '  {"clip": "%s", "path": "%s", "ligados": %d, "audios": [%s], "offsets_muestras": [%s]}',
    esc(nombre), esc(fp), ligados,
    (function()
      local t = {}
      for i, r in ipairs(rutas) do t[i] = '"' .. esc(r) .. '"' end
      return table.concat(t, ", ")
    end)(),
    (function()
      local t = {}
      for i, o in ipairs(offsets) do t[i] = (o ~= "" and o or "null") end
      return table.concat(t, ", ")
    end)())
end

print(string.format("  con audio ligado: %d | sin ligar: %d", nLig, nSin))

-- ---------- escribir ------------------------------------------------------
local f, err = io.open(SALIDA_PATH, "w")
if not f then
  print("ERROR escribiendo " .. SALIDA_PATH .. ": " .. tostring(err))
  return
end
f:write("{\n")
f:write('  "_doc": "Salida de resolve/leer_sync_merge.lua. offsets en MUESTRAS ')
f:write('con el signo de Resolve; el manifest usa el contrario: ')
f:write('offset_manifest_seg == -offset_resolve_muestras/sample_rate.",\n')
f:write('  "proyecto": "' .. esc(proj:GetName()) .. '",\n')
f:write('  "con_audio_ligado": ' .. nLig .. ',\n')
f:write('  "sin_ligar": ' .. nSin .. ',\n')
f:write('  "clips": [\n')
f:write(table.concat(filas, ",\n"))
f:write("\n  ]\n}\n")
f:close()

print("Escrito: " .. SALIDA_PATH)
print("")
print("Siguiente paso, fuera de Resolve:")
print("  python3 <MOTOR>/bin/import_iban_offsets.py \\")
print("    --root <disco> --merge-json " .. SALIDA_PATH .. " --grupo <Camara>")
line()
