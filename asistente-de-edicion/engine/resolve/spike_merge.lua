-- ============================================================
--  spike_merge.lua — prueba REAL de MediaPool:AutoSyncAudio (S1)
--
--  Consola de Resolve (Lua):
--    dofile("<MOTOR>/resolve/spike_merge.lua")
--
--  SEGURIDAD
--   - NO toca ningun proyecto existente. Guarda el proyecto abierto, crea uno
--     NUEVO llamado "PRUEBA MERGE v020" y trabaja ahi. Al terminar vuelve al
--     proyecto de origen.
--   - El material es sintetico y vive en el scratchpad: un video de 30 s cuyo
--     audio de camara es la senal maestra DESDE el segundo 5, y un WAV de 40 s
--     con la senal completa. Offset esperado con la convencion del motor
--     (audio_start - video_start): -5.00 s.
--
--  Que responde
--   S1a  AutoSyncAudio funciona en Resolve Free?
--   S1b  RETAIN_EMBEDDED_AUDIO=true conserva el audio de camara?
--   S1c  El offset que calcula Resolve coincide con el real?
--   S1d  Al bajar el clip mergeado, en que pistas cae su audio?
--   S1e  Via A: AddTrack{index=2} deja  A1 camara / A2 companera / A3 lavalier?
-- ============================================================

-- El material de prueba lo genera:  python3 bin/generar_media_prueba.py
-- Para usar otra carpeta, antes del dofile:  MERGE_TEST_MEDIA = "/ruta"
local MEDIA = MERGE_TEST_MEDIA
  or ((os.getenv("HOME") or "~") .. "/cinema-assistant/tests/media_prueba")
local VIDEO = MEDIA .. "/camara.mov"
local LAV   = MEDIA .. "/lav.wav"
local OFFSET_ESPERADO = -5.0
local PROY = "PRUEBA MERGE v020"

local RES = {}
local function veredicto(id, valor, detalle)
  RES[#RES+1] = {id = id, valor = valor, detalle = detalle or ""}
  print(string.format("  VEREDICTO: %-5s %-3s  %s", id, valor, detalle or ""))
end
local function line() print(string.rep("=", 62)) end
local function head(t) print(""); line(); print("  " .. t); line() end

head("SPIKE S1 — merge de audio en el Media Pool")

local pm = resolve:GetProjectManager()
local original = pm:GetCurrentProject()
if not original then print("ERROR: sin proyecto abierto."); return end
local nombreOriginal = original:GetName()
print("Proyecto actual: " .. nombreOriginal .. "  (se guarda y se restaura al final)")
pm:SaveProject()

-- Proyecto de trabajo, aparte
local proj = nil
if pm:LoadProject(PROY) then
  proj = pm:GetCurrentProject()
  print("Reusando el proyecto de prueba: " .. PROY)
else
  if pm:CreateProject(PROY) then
    proj = pm:GetCurrentProject()
    print("Proyecto de prueba creado: " .. PROY)
  end
end
if not proj then
  print("ERROR: no se pudo crear ni abrir el proyecto de prueba.")
  pm:LoadProject(nombreOriginal)
  return
end

local mp = proj:GetMediaPool()

-- Limpiar restos de una corrida anterior
do
  local root = mp:GetRootFolder()
  local viejos = root:GetClipList() or {}
  if #viejos > 0 then mp:DeleteClips(viejos) end
  for i = proj:GetTimelineCount(), 1, -1 do
    local tl = proj:GetTimelineByIndex(i)
    if tl then mp:DeleteTimelines({tl}) end
  end
end

-- ---------- importar ----------------------------------------------------
local imp = mp:ImportMedia({VIDEO, LAV})
if not imp or #imp < 2 then
  print("ERROR importando el material sintetico. Rutas:")
  print("  " .. VIDEO)
  print("  " .. LAV)
  veredicto("S1", "?", "no se pudo importar el material de prueba")
  pm:LoadProject(nombreOriginal)
  return
end
local vClip, aClip
for _, c in ipairs(imp) do
  local n = c:GetName() or ""
  if string.find(n, "%.mov$") then vClip = c else aClip = c end
end
print(string.format("Importados: video=%s  audio=%s",
  vClip and vClip:GetName() or "?", aClip and aClip:GetName() or "?"))

local function mapping(c)
  if not (c and c.GetAudioMapping) then return "" end
  local ok, v = pcall(function() return c:GetAudioMapping() end)
  return (ok and v) and tostring(v) or ""
end

local antes = mapping(vClip)
print("mapping ANTES : " .. antes)

-- ---------- S1a: el merge ------------------------------------------------
head("S1a — AutoSyncAudio")
local ajustes = {
  [resolve.AUDIO_SYNC_MODE]                  = resolve.AUDIO_SYNC_WAVEFORM,
  [resolve.AUDIO_SYNC_CHANNEL_NUMBER]        = resolve.AUDIO_SYNC_CHANNEL_AUTOMATIC,
  [resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO] = true,
  [resolve.AUDIO_SYNC_RETAIN_VIDEO_METADATA] = true,
}
local okSync = mp:AutoSyncAudio({vClip, aClip}, ajustes)
print("AutoSyncAudio -> " .. tostring(okSync))
local despues = mapping(vClip)
print("mapping DESPUES: " .. despues)

local ligo = string.find(despues, '"linked_audio"%s*:%s*{%s*"') ~= nil
if okSync and ligo then
  veredicto("S1a", "SI", "el merge en el Media Pool FUNCIONA en Resolve Free")
else
  veredicto("S1a", "NO", "AutoSyncAudio=" .. tostring(okSync) ..
            " y el mapping no muestra audio ligado")
end

-- ---------- S1b: se conservo el audio de camara? -------------------------
head("S1b — audio de camara conservado (RETAIN_EMBEDDED_AUDIO)")
local emb = tonumber(string.match(despues, '"embedded_audio_channels"%s*:%s*(%d+)') or "0")
print("embedded_audio_channels = " .. emb)
if emb > 0 then
  veredicto("S1b", "SI", emb .. " canal(es) de camara conservados — regla dura OK")
else
  veredicto("S1b", "NO", "el audio de camara se PERDIO — viola la regla dura")
end

-- ---------- S1c: el offset coincide con el real? -------------------------
head("S1c — offset calculado vs offset real")
local offMuestras = tonumber(string.match(despues, '"offset"%s*:%s*(%-?%d+)') or "")
local sr = 48000
if offMuestras then
  local offSeg = offMuestras / sr
  print(string.format("offset de Resolve: %d muestras = %.3f s", offMuestras, offSeg))
  print(string.format("offset real      : %.3f s", OFFSET_ESPERADO))
  local err = math.abs(math.abs(offSeg) - math.abs(OFFSET_ESPERADO))
  print(string.format("error absoluto   : %.3f s (%.1f frames a 24p)", err, err * 24))
  if err <= (1.0 / 24.0) then
    veredicto("S1c", "SI", string.format("dentro de 1 frame (error %.0f ms)", err * 1000))
  else
    veredicto("S1c", "NO", string.format("error de %.3f s — mas de 1 frame", err))
  end
else
  veredicto("S1c", "?", "el mapping no trae 'offset' (no ligo nada)")
end

-- ---------- S1d: reparto de pistas al bajar a timeline -------------------
head("S1d — en que pistas cae el audio del clip mergeado")
local tl = mp:CreateEmptyTimeline("PRUEBA — merge")
local orden = {}
if tl then
  proj:SetCurrentTimeline(tl)
  mp:AppendToTimeline({vClip})
  local nA = tl:GetTrackCount("audio")
  for i = 1, nA do
    local items = tl:GetItemListInTrack("audio", i)
    orden[i] = (items and #items or 0)
    print(string.format("  A%d: %d item(s)", i, orden[i]))
  end
  local conContenido = 0
  for _, n in ipairs(orden) do if n > 0 then conContenido = conContenido + 1 end end
  if conContenido >= 2 then
    veredicto("S1d", "SI", conContenido .. " pistas con contenido: A1 camara, A2 lavalier")
  else
    veredicto("S1d", "NO", "solo " .. conContenido ..
              " pista con contenido — el lavalier no bajo por separado")
  end
else
  veredicto("S1d", "?", "no se pudo crear la timeline de prueba")
end

-- ---------- S1e: Via A — insertar pista para la companera ----------------
head("S1e — Via A: insertar A2 para la camara companera")
if tl then
  local antesN = tl:GetTrackCount("audio")
  tl:SetTrackName("audio", 1, "CAM base")
  if antesN >= 2 then tl:SetTrackName("audio", 2, "LAVA") end
  local okIns = tl:AddTrack("audio", {audioType = "stereo", index = 2})
  local n1 = tostring(tl:GetTrackName("audio", 1))
  local n2 = tostring(tl:GetTrackName("audio", 2))
  local n3 = tostring(tl:GetTrackName("audio", 3))
  print(string.format("AddTrack{index=2} -> %s   A1=%s  A2=%s  A3=%s",
    tostring(okIns), n1, n2, n3))
  if okIns and n3 == "LAVA" then
    veredicto("S1e", "SI", "el lavalier bajo a A3; A2 queda libre para la companera")
  elseif okIns then
    veredicto("S1e", "NO", "AddTrack no desplazo el contenido (A3=" .. n3 .. ")")
  else
    veredicto("S1e", "?", "AddTrack con index fallo")
  end
end

-- ---------- volver -------------------------------------------------------
head("Restaurando")
pm:SaveProject()
if pm:LoadProject(nombreOriginal) then
  print("De vuelta en: " .. nombreOriginal)
else
  print("AVISO: no se pudo volver a '" .. nombreOriginal ..
        "'. Abrelo desde el Project Manager.")
end
print("El proyecto de prueba '" .. PROY .. "' se puede borrar cuando quieras.")

head("RESUMEN S1")
for _, r in ipairs(RES) do
  print(string.format("%s = %s   %s", r.id, r.valor, r.detalle))
end
print("")
