-- ============================================================
--  merge_pool.lua — pega el audio externo DENTRO del clip, en el Media Pool
--
--  Consola de DaVinci Resolve (Lua):
--    MERGE_PLAN = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proy>_merge.lua"
--    dofile("<MOTOR>/resolve/merge_pool.lua")
--
--  ⚠ EL MERGE NO SE DESHACE POR API. No existe UnlinkAudio; `UnlinkClips` es
--  otra cosa (deja los clips offline). Por eso este script:
--    - EXIGE que el nombre del proyecto diga PRUEBA / COPIA / TEST / MERGE,
--      salvo que pongas MERGE_FORZAR = true a conciencia;
--    - es IDEMPOTENTE: salta los clips que GetAudioMapping ya reporta ligados;
--    - usa RETAIN_EMBEDDED_AUDIO = true, asi que el audio de camara NUNCA se
--      pierde (regla dura del usuario: el externo va DESPUES del de camara).
--
--  Verificado en Resolve 21.0.2 Free (2026-07-30): AutoSyncAudio existe, corre
--  en Free, y sobre un par con offset conocido de 5 s clavo el sync con 0 ms
--  de error.
--
--  Al terminar vuelca un informe a <plan>_resultado.json que lee
--  bin/verify_merge.py para comparar los offsets contra el manifest.
-- ============================================================

local function line() print(string.rep("=", 62)) end
local function head(t) print(""); line(); print("  " .. t); line() end

head("MERGE EN EL MEDIA POOL — asistente de edicion")

if not MERGE_PLAN or MERGE_PLAN == "" then
  print("Falta la ruta del plan. En la Consola, antes del dofile:")
  print('  MERGE_PLAN = "/Volumes/<disco>/<proyecto>/.cinema_assistant/resolve/<proy>_merge.lua"')
  print("El plan lo genera:  python3 bin/export_merge_plan.py --root <disco>")
  return
end

local okPlan, PLAN = pcall(dofile, MERGE_PLAN)
if not okPlan or type(PLAN) ~= "table" or not PLAN.grupos then
  print("ERROR leyendo el plan: " .. tostring(PLAN))
  return
end
print("Plan: " .. MERGE_PLAN)
print("Grupos: " .. #PLAN.grupos .. "  (confianza minima " ..
      tostring(PLAN.min_conf) .. ")")

local pm = resolve:GetProjectManager()
local proj = pm and pm:GetCurrentProject()
if not proj then print("ERROR: sin proyecto abierto.") return end
local mp = proj:GetMediaPool()
local nombre = proj:GetName()
print("Proyecto: " .. nombre)

-- ---------- candado -----------------------------------------------------
local up = string.upper(nombre)
local esCopia = string.find(up, "PRUEBA", 1, true) or string.find(up, "COPIA", 1, true)
             or string.find(up, "TEST", 1, true)   or string.find(up, "MERGE", 1, true)
if not esCopia and not MERGE_FORZAR then
  print("")
  print("ABORTADO. El merge NO se puede deshacer con script y este proyecto no")
  print("parece una copia (\"" .. nombre .. "\").")
  print("")
  print("Recomendado:")
  print("  1. Project Manager > clic derecho en el proyecto > Duplicate")
  print("  2. Renombra la copia con PRUEBA en el nombre y abrela")
  print("  3. Vuelve a correr esto")
  print("")
  print("Si de verdad quieres hacerlo sobre este proyecto, antes del dofile:")
  print("  MERGE_FORZAR = true")
  return
end

-- ---------- indexar el Media Pool por ruta ------------------------------
local porRuta = {}
local function recolectar(folder)
  for _, c in ipairs(folder:GetClipList() or {}) do
    local fp = c.GetClipProperty and c:GetClipProperty("File Path")
    if fp and fp ~= "" then porRuta[fp] = c end
  end
  for _, s in ipairs(folder:GetSubFolderList() or {}) do recolectar(s) end
end
recolectar(mp:GetRootFolder())

local function mapping(c)
  if not (c and c.GetAudioMapping) then return "" end
  local ok, v = pcall(function() return c:GetAudioMapping() end)
  return (ok and v) and tostring(v) or ""
end
local function yaLigado(c, apath)
  local m = mapping(c)
  if m == "" then return false end
  -- basta con que el path del audio aparezca en linked_audio
  return string.find(m, apath, 1, true) ~= nil
end

local AJUSTES = {
  [resolve.AUDIO_SYNC_MODE]                  = resolve.AUDIO_SYNC_WAVEFORM,
  [resolve.AUDIO_SYNC_CHANNEL_NUMBER]        = resolve.AUDIO_SYNC_CHANNEL_AUTOMATIC,
  -- CRITICO: sin esto Resolve DESCARTA el audio de camara, que viola la regla
  -- dura del usuario (el audio de camara se conserva; el externo va debajo).
  [resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO] = true,
  [resolve.AUDIO_SYNC_RETAIN_VIDEO_METADATA] = true,
}

-- ---------- ejecutar ----------------------------------------------------
head("Ligando")
local nOk, nSalt, nFallo, nSinClip, nImport = 0, 0, 0, 0, 0
local informe = {}

for _, g in ipairs(PLAN.grupos) do
  local vClip = porRuta[g.video]
  if not vClip then
    nSinClip = nSinClip + 1
    print(string.format("  [sin clip] %s no esta en el Media Pool", g.name or "?"))
  else
    -- importar los WAV que falten
    local audios, faltantes = {}, {}
    for _, a in ipairs(g.audios) do
      local ac = porRuta[a.path]
      if not ac then
        local imp = mp:ImportMedia({a.path})
        if imp and imp[1] then
          ac = imp[1]; porRuta[a.path] = ac; nImport = nImport + 1
        end
      end
      if ac then audios[#audios+1] = {clip = ac, info = a}
      else faltantes[#faltantes+1] = a.name or a.path end
    end

    if #audios == 0 then
      nFallo = nFallo + 1
      print(string.format("  [FALLO] %s: no se pudo importar %s",
        g.name or "?", table.concat(faltantes, ", ")))
    else
      -- idempotencia: si ya estan ligados todos, no repetir
      local pendientes = {}
      for _, a in ipairs(audios) do
        if not yaLigado(vClip, a.info.path) then pendientes[#pendientes+1] = a end
      end
      if #pendientes == 0 then
        nSalt = nSalt + 1
      else
        local lista = {vClip}
        for _, a in ipairs(audios) do lista[#lista+1] = a.clip end
        local ok = mp:AutoSyncAudio(lista, AJUSTES)
        local m = mapping(vClip)
        local ligados = 0
        for _, a in ipairs(audios) do
          if string.find(m, a.info.path, 1, true) then ligados = ligados + 1 end
        end
        if ok and ligados > 0 then
          nOk = nOk + 1
          print(string.format("  [ok] %-34s %d audio(s)", g.name or "?", ligados))
        else
          nFallo = nFallo + 1
          print(string.format("  [FALLO] %s: AutoSyncAudio=%s, ligados=%d",
            g.name or "?", tostring(ok), ligados))
        end
        informe[#informe+1] = {video = g.video, mapping = m, ok = ok}
      end
    end
  end
end

-- ---------- informe para verify_merge.py --------------------------------
local rutaInf = string.gsub(MERGE_PLAN, "%.lua$", "_resultado.json")
do
  local f = io.open(rutaInf, "w")
  if f then
    f:write("[\n")
    for i, r in ipairs(informe) do
      local m = string.gsub(r.mapping, "\\", "\\\\")
      m = string.gsub(m, '"', '\\"')
      m = string.gsub(m, "\n", " ")
      f:write(string.format('  {"video": "%s", "ok": %s, "mapping": "%s"}%s\n',
        string.gsub(r.video, '"', '\\"'), tostring(r.ok and true or false), m,
        (i < #informe) and "," or ""))
    end
    f:write("]\n")
    f:close()
    print("\nInforme para verificar: " .. rutaInf)
  else
    print("\nAVISO: no se pudo escribir el informe en " .. rutaInf)
  end
end

head("LISTO")
print("  clips mergeados        : " .. nOk)
print("  ya estaban ligados     : " .. nSalt .. "  (idempotente)")
print("  WAV importados al pool : " .. nImport)
if nSinClip > 0 then
  print("  videos ausentes del pool: " .. nSinClip)
end
if nFallo > 0 then
  print("  FALLOS                 : " .. nFallo)
end
print("")
print("Siguiente paso — comprobar que el merge quedo donde el motor dijo:")
print("  python3 ~/cinema-assistant/bin/verify_merge.py --root <disco>")
print("")
print("Tip: comando+S para guardar el proyecto.")
print("")
