-- ============================================================
--  spike_merge2.lua — cierra las dos preguntas que dejo abiertas spike_merge
--
--    dofile("<MOTOR>/resolve/spike_merge2.lua")
--
--  Hallazgo de la corrida anterior: el clip mergeado SI tiene dos pistas de
--  audio (track_mapping 1 y 2), pero al hacer AppendToTimeline solo cayo
--  contenido en A1. Hipotesis: la timeline se creo con UNA sola pista de audio
--  y Resolve no crea pistas nuevas al vuelo — la segunda no tenia donde caer.
--
--  S1f  Con la timeline preparada con 2 pistas ANTES del append, ¿caen las dos?
--  S1g  Al insertar una pista en index=2, ¿se mueve el CONTENIDO (no solo el
--       nombre) del lavalier a A3, dejando A2 libre para la camara companera?
--
--  Trabaja en el proyecto "PRUEBA MERGE v020" que ya existe. No toca nada mas.
-- ============================================================

local PROY = "PRUEBA MERGE v020"

local RES = {}
local function veredicto(id, valor, detalle)
  RES[#RES+1] = {id = id, valor = valor, detalle = detalle or ""}
  print(string.format("  VEREDICTO: %-5s %-3s  %s", id, valor, detalle or ""))
end
local function line() print(string.rep("=", 62)) end
local function head(t) print(""); line(); print("  " .. t); line() end

head("SPIKE S1f/S1g — reparto de pistas del clip mergeado")

local pm = resolve:GetProjectManager()
local original = pm:GetCurrentProject()
local nombreOriginal = original and original:GetName() or ""
if nombreOriginal ~= PROY then
  pm:SaveProject()
  if not pm:LoadProject(PROY) then
    print("ERROR: no existe el proyecto '" .. PROY .. "'. Correr antes spike_merge.lua.")
    return
  end
end
local proj = pm:GetCurrentProject()
local mp = proj:GetMediaPool()
print("Proyecto: " .. proj:GetName())

-- el clip mergeado (el .mov)
local vClip
for _, c in ipairs(mp:GetRootFolder():GetClipList() or {}) do
  if string.find(c:GetName() or "", "%.mov$") then vClip = c end
end
if not vClip then
  print("ERROR: no encuentro el clip de video en el Media Pool.")
  if nombreOriginal ~= PROY then pm:LoadProject(nombreOriginal) end
  return
end

local am = vClip:GetAudioMapping()
local nPistasFuente = 0
for _ in string.gmatch(tostring(am), '"%d+"%s*:%s*{%s*"channel_idx"') do
  nPistasFuente = nPistasFuente + 1
end
print("pistas de audio del clip fuente: " .. nPistasFuente)

-- ---------- S1f: preparar la timeline ANTES de appendear ------------------
head("S1f — timeline preparada con " .. nPistasFuente .. " pistas antes del append")
for i = proj:GetTimelineCount(), 1, -1 do
  local t = proj:GetTimelineByIndex(i)
  if t and string.find(t:GetName(), "PRUEBA") then mp:DeleteTimelines({t}) end
end
local tl = mp:CreateEmptyTimeline("PRUEBA — pistas")
if not tl then
  veredicto("S1f", "?", "no se pudo crear la timeline")
  if nombreOriginal ~= PROY then pm:LoadProject(nombreOriginal) end
  return
end
proj:SetCurrentTimeline(tl)
print("pistas de audio al crear la timeline: " .. tl:GetTrackCount("audio"))
while tl:GetTrackCount("audio") < nPistasFuente do
  tl:AddTrack("audio", "mono")
end
print("pistas tras preparar: " .. tl:GetTrackCount("audio"))

mp:AppendToTimeline({vClip})

local function conteo(t)
  local out = {}
  for i = 1, t:GetTrackCount("audio") do
    local items = t:GetItemListInTrack("audio", i)
    out[i] = items and #items or 0
  end
  return out
end
local c1 = conteo(tl)
local conContenido = 0
for i, n in ipairs(c1) do
  print(string.format("  A%d: %d item(s)", i, n))
  if n > 0 then conContenido = conContenido + 1 end
end
if conContenido >= 2 then
  veredicto("S1f", "SI", "las 2 pistas del clip mergeado caen en A1 y A2 "
            .. "(camara y lavalier separados)")
else
  veredicto("S1f", "NO", "solo " .. conContenido .. " pista con contenido "
            .. "aun preparando la timeline")
end

-- ---------- S1g: insertar A2 y ver si el CONTENIDO se mueve ---------------
head("S1g — insertar pista en index=2: ¿se mueve el contenido?")
tl:SetTrackName("audio", 1, "CAM base")
if tl:GetTrackCount("audio") >= 2 then tl:SetTrackName("audio", 2, "LAVA") end
local antes = conteo(tl)
print(string.format("antes : A1=%d items (%s)  A2=%d items (%s)",
  antes[1] or 0, tostring(tl:GetTrackName("audio", 1)),
  antes[2] or 0, tostring(tl:GetTrackName("audio", 2))))

local okIns = tl:AddTrack("audio", {audioType = "mono", index = 2})
local despues = conteo(tl)
print("AddTrack{index=2} -> " .. tostring(okIns))
for i = 1, tl:GetTrackCount("audio") do
  print(string.format("  A%d: %d item(s)  nombre=%s", i, despues[i] or 0,
    tostring(tl:GetTrackName("audio", i))))
end

local lavEnA3 = (despues[3] or 0) > 0 and tostring(tl:GetTrackName("audio", 3)) == "LAVA"
local a2Libre = (despues[2] or 0) == 0
local camEnA1 = (despues[1] or 0) > 0
if okIns and lavEnA3 and a2Libre and camEnA1 then
  veredicto("S1g", "SI", "A1 camara · A2 LIBRE para la companera · A3 lavalier "
            .. "— la regla dura se cumple con el clip base LIGADO")
else
  veredicto("S1g", "NO", string.format(
    "cam_en_A1=%s a2_libre=%s lav_en_A3=%s", tostring(camEnA1),
    tostring(a2Libre), tostring(lavEnA3)))
end

-- ---------- volver -------------------------------------------------------
head("Restaurando")
pm:SaveProject()
if nombreOriginal ~= "" and nombreOriginal ~= PROY then
  pm:LoadProject(nombreOriginal)
  print("De vuelta en: " .. nombreOriginal)
end

head("RESUMEN")
for _, r in ipairs(RES) do
  print(string.format("%s = %s   %s", r.id, r.valor, r.detalle))
end
print("")
