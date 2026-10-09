-- ============================================================
--  asistente_lib.lua — logica compartida de los asistente_<proyecto>.lua
--
--  Se carga desde el script de proyecto:
--    local LIB = dofile("<MOTOR>/resolve/asistente_lib.lua")
--
--  Existe para que la logica de markers viva en UN solo lugar. Antes cada
--  proyecto tenia su copia de decorate() y fueron divergiendo: FCC quito los
--  duration markers y MAB los conservo, asi que "arreglar los markers"
--  significaba editar seis archivos.
-- ============================================================

local LIB = {}

-- ---------------------------------------------------------------
-- A QUIEN SE LE CUENTA LO QUE PASA (2026-10-06)
--
-- Los scripts por proyecto corren en la Consola, donde `print` se ve. El
-- aplicador del menu de Free (aplicar.lua + construir.lua) no tiene donde
-- imprimir: alli `print` existe pero no se ve (medido en 21.1.0.17 y
-- 21.1.1.10), y en el sandbox de las pruebas ni existe. Por eso la libreria
-- no llama a `print` directo: llama a LIB.decir, que por defecto es `print` (los
-- scripts por proyecto no cambian) y que el aplicador cambia por su anotador,
-- para que lo que aqui se dice llegue al reporte.
-- ---------------------------------------------------------------
LIB.decir = print
local function decir(texto)
  local f = LIB.decir
  if type(f) == "function" then f(texto) end
end

-- ---------------------------------------------------------------
-- POLITICA DE MARKERS
--
-- Instruccion del usuario (FCC, 2026-07-11), literal:
--   "los duration markers nunca han funcionado como deberian"
-- Por eso TODO lo que emite este modulo son marcadores DE PUNTO
-- (duration = 1). La duracion del tramo va escrita en la nota, que si
-- se lee bien en el panel de markers.
-- ---------------------------------------------------------------
LIB.DURACION_MARKER = 1

-- Colores por tipo de beat. Purple ya significaba "pregunta" en las
-- versiones anteriores: se respeta para no reeducar el ojo del editor.
LIB.COLOR = {
  pregunta  = "Purple",
  respuesta = "Blue",
  pausa     = "Sand",
  keyword   = "Mint",
  emocion   = "Lemon",
}

-- ---------------------------------------------------------------
-- Asignador de frames libres
--
-- Resolve rechaza dos markers en el MISMO frame del mismo item: el segundo
-- AddMarker devuelve false y el marker se pierde en silencio. Con pregunta +
-- respuesta + pausas + palabras clave en la misma entrevista las colisiones
-- son seguras. El parche anterior era `if fStart < 3 then fStart = 3 + i end`,
-- que solo cubria el arranque del clip.
--
-- Este asignador lleva la cuenta de los frames ya usados y corre el marker al
-- siguiente frame libre. El corrimiento es de milisegundos y se reporta.
-- ---------------------------------------------------------------
function LIB.nuevoAsignador(frameMinimo)
  local usados = {}
  local minimo = frameMinimo or 0
  local corridos = 0
  return {
    tomar = function(frameDeseado)
      local f = math.floor(frameDeseado or 0)
      if f < minimo then f = minimo end
      local intentos = 0
      while usados[f] and intentos < 2000 do
        f = f + 1
        intentos = intentos + 1
        corridos = corridos + 1
      end
      usados[f] = true
      return f
    end,
    corridos = function() return corridos end,
  }
end

-- ---------------------------------------------------------------
-- AddMarker con customData y respaldo
--
-- `customData` permite re-hornear sin borrar la timeline entera:
-- DeleteMarkerByCustomData("beat:<clip>:<i>") quita SOLO lo que puso el
-- asistente y respeta los markers que el editor agrego a mano.
-- Si la build no lo acepta, se reintenta sin el: mejor un marker sin
-- identificador que ningun marker.
-- ---------------------------------------------------------------
function LIB.addMarker(item, frame, color, nombre, nota, customData)
  local ok, res = pcall(function()
    return item:AddMarker(frame, color, nombre, nota, LIB.DURACION_MARKER,
                          customData or "")
  end)
  if ok and res then return true, true end
  local ok2, res2 = pcall(function()
    return item:AddMarker(frame, color, nombre, nota, LIB.DURACION_MARKER)
  end)
  return (ok2 and res2) and true or false, false
end

-- Borra los markers que puso el asistente en corridas anteriores, dejando
-- intactos los del editor. Devuelve cuantos borro.
function LIB.limpiarBeats(item, clipKey, maxBeats)
  local n = 0
  for i = 0, (maxBeats or 200) do
    local ok, res = pcall(function()
      return item:DeleteMarkerByCustomData("beat:" .. tostring(clipKey) .. ":" .. i)
    end)
    if ok and res then n = n + 1 end
  end
  return n
end

-- ---------------------------------------------------------------
-- Etiquetas legibles
-- ---------------------------------------------------------------
local function segundos(s)
  s = tonumber(s) or 0
  if s >= 60 then
    return string.format("%dm %02ds", math.floor(s / 60), math.floor(s % 60))
  end
  return string.format("%.1fs", s)
end

local function recorta(s, n)
  s = s or ""
  if #s <= n then return s end
  return string.sub(s, 1, n - 1) .. "…"
end

-- Nombre y nota de cada beat. `quien` es el entrevistado (info.who), que el
-- usuario pidio ver como Name del marker (FCC 2026-07-11).
function LIB.etiquetaDe(b, quien)
  local dur = (tonumber(b.e) or 0) - (tonumber(b.s) or 0)
  local kind = b.kind or ""

  if kind == "pregunta" then
    return (quien ~= "" and quien or "Pregunta"), (b.text or "")

  elseif kind == "respuesta" then
    local nombre = "▸ Respuesta"
    if quien ~= "" then nombre = "▸ " .. quien .. " responde" end
    local nota = b.text or ""
    if nota ~= "" then nota = nota .. "\n\n" end
    nota = nota .. "dura " .. segundos(dur)
    -- Cuando el limite es estimado hay que decirlo: el editor no debe
    -- confiar en el frame exacto.
    if b.src == "estimado" then
      nota = nota .. "  (inicio APROXIMADO — sin master ni silencios medidos)"
    elseif b.src == "silencio" then
      nota = nota .. "  (inicio por silencio)"
    elseif b.src == "master_src" and b.speaker and b.speaker ~= "" then
      nota = nota .. "  (inicio por cambio de microfono: " .. b.speaker .. ")"
    end
    return nombre, nota

  elseif kind == "pausa" then
    return "Pausa " .. segundos(dur), "silencio de " .. segundos(dur)
      .. " dentro de la respuesta — punto de corte limpio"

  elseif kind == "keyword" then
    return recorta(b.text or "palabra clave", 40), "termino del proyecto: "
      .. (b.text or "")

  elseif kind == "emocion" then
    return recorta(b.text or "candidato", 48),
      (b.text or "") .. "\n\nconfianza " .. string.format("%.2f", b.conf or 0)
      .. " — es un CANDIDATO, confirmalo viendo el clip"
  end
  return recorta(b.text or kind, 40), (b.text or "")
end

-- ---------------------------------------------------------------
-- Decorador principal
--
-- opts = { asignador=, clipKey=, quien=, omitir={pausa=true, ...} }
-- Devuelve (n_markers, n_con_customdata)
-- ---------------------------------------------------------------
-- fps utilizable. BRAW / R3D / MXF pueden llegar sin fps ni duracion en el
-- manifest (ffprobe no los abre y mediainfo no siempre los trae). Sin esto,
-- `math.floor(b.s * fps)` con fps nil revienta el script entero y el editor se
-- queda sin NINGUN marker por un puñado de clips.
function LIB.fpsSeguro(fps, timeline)
  local f = tonumber(fps)
  if f and f > 0 then return f, false end
  if timeline and timeline.GetSetting then
    local t = tonumber(timeline:GetSetting("timelineFrameRate"))
    if t and t > 0 then return t, true end
  end
  return 24.0, true
end

function LIB.decorarBeats(item, beats, fps, opts)
  opts = opts or {}
  fps = LIB.fpsSeguro(fps, opts.timeline)
  local asign = opts.asignador or LIB.nuevoAsignador(3)
  local quien = opts.quien or ""
  local omitir = opts.omitir or {}
  local clipKey = opts.clipKey or "x"
  local n, nCustom = 0, 0

  for i, b in ipairs(beats or {}) do
    local kind = b.kind or ""
    if not omitir[kind] then
      local color = LIB.COLOR[kind] or "Cream"
      local nombre, nota = LIB.etiquetaDe(b, quien)
      local frame = asign.tomar(math.floor((tonumber(b.s) or 0) * fps))
      local cd = "beat:" .. tostring(clipKey) .. ":" .. tostring(i - 1)
      local puesto, conCustom = LIB.addMarker(item, frame, color, nombre, nota, cd)
      if puesto then
        n = n + 1
        if conCustom then nCustom = nCustom + 1 end
      end
    end
  end
  return n, nCustom
end

-- ===============================================================
-- LAYOUT DE PISTAS DE AUDIO
--
-- REGLA DURA DEL USUARIO: los audios externos (lavaliers, grabadora de campo)
-- van SIEMPRE despues de todos los audios de camara.
--
--   A1                cámara base
--   A2 .. A(1+C)      cámaras compañeras (multicám)
--   A(2+C) ..         lavaliers
--
-- Dos cosas medidas en Resolve 21.0.2 Free (2026-07-30) que hacen esto posible:
--
--  1. AppendToTimeline NO crea pistas de audio al vuelo. Si el clip mergeado
--     trae 2 pistas y la timeline tiene 1, la segunda **se pierde en silencio**
--     (medido: A1 con 1 item, A2 con 0). Hay que PREPARAR las pistas antes.
--  2. AddTrack("audio", {index=N}) INSERTA y desplaza las pistas de abajo
--     CON SU CONTENIDO (medido: el item del lavalier paso de A2 a A3). Eso
--     permite abrir hueco para la cámara compañera sin romper el link
--     video/audio del clip base.
-- ===============================================================

-- Añade pistas al final hasta tener `n`.
function LIB.prepararPistasAudio(tl, n, tipo)
  local puestas = 0
  while tl:GetTrackCount("audio") < n do
    if not tl:AddTrack("audio", tipo or "stereo") then break end
    puestas = puestas + 1
  end
  return puestas
end

-- Inserta `cuantas` pistas en `indice`, desplazando hacia abajo lo que haya.
function LIB.insertarPistasAudio(tl, cuantas, indice, tipo)
  local puestas = 0
  for _ = 1, (cuantas or 0) do
    local ok = tl:AddTrack("audio", {audioType = tipo or "stereo", index = indice})
    if not ok then break end
    puestas = puestas + 1
  end
  return puestas
end

-- Nombra las pistas segun el layout, para que se lea de un vistazo cual es cual.
-- `compañeras` y `lavaliers` son listas de etiquetas.
function LIB.nombrarPistas(tl, camBase, companeras, lavaliers)
  local i = 1
  tl:SetTrackName("audio", i, "CAM " .. (camBase or "base"))
  for _, nom in ipairs(companeras or {}) do
    i = i + 1
    if i <= tl:GetTrackCount("audio") then
      tl:SetTrackName("audio", i, "CAM " .. nom)
    end
  end
  for _, nom in ipairs(lavaliers or {}) do
    i = i + 1
    if i <= tl:GetTrackCount("audio") then
      tl:SetTrackName("audio", i, "LAVA " .. nom)
    end
  end
  return i
end

-- Lee el layout actual: cuantos items hay por pista y como se llama cada una.
function LIB.layoutDe(tl)
  local out = {}
  for i = 1, tl:GetTrackCount("audio") do
    local items = tl:GetItemListInTrack("audio", i)
    out[i] = {indice = i, nombre = tostring(tl:GetTrackName("audio", i) or ""),
              items = items and #items or 0}
  end
  return out
end

-- Comprueba la regla dura sobre el layout: ningun audio EXTERNO puede estar
-- por encima de un audio de CAMARA. Se decide por el nombre de la pista, que
-- es lo que `nombrarPistas` deja escrito.
function LIB.verificarOrdenPistas(tl)
  local layout = LIB.layoutDe(tl)
  local ultimaCam, primerExterno = 0, nil
  for _, p in ipairs(layout) do
    if p.items > 0 then
      if string.sub(p.nombre, 1, 4) == "CAM " then
        ultimaCam = p.indice
      elseif string.sub(p.nombre, 1, 5) == "LAVA " then
        primerExterno = primerExterno or p.indice
      end
    end
  end
  local ok = true
  local motivo = ""
  if primerExterno and primerExterno < ultimaCam then
    ok = false
    motivo = string.format("audio externo en A%d por ENCIMA de audio de camara en A%d",
                           primerExterno, ultimaCam)
  end
  return ok, motivo, layout
end

-- Vuelca el layout de todas las timelines a un JSON que lee
-- bin/verify_track_order.py. Sin esto la regla depende de que el codigo la
-- respete por costumbre.
function LIB.volcarLayouts(ruta, layouts)
  local f = io.open(ruta, "w")
  if not f then return false end
  f:write("[\n")
  for i, L in ipairs(layouts) do
    f:write(string.format('  {"timeline": %q, "pistas": [', L.timeline))
    for j, p in ipairs(L.pistas) do
      f:write(string.format('{"i": %d, "nombre": %q, "items": %d}%s',
        p.indice, p.nombre, p.items, (j < #L.pistas) and ", " or ""))
    end
    f:write(string.format(']}%s\n', (i < #layouts) and "," or ""))
  end
  f:write("]\n")
  f:close()
  return true
end

-- ===============================================================
-- A-ROLL / B-ROLL
-- ===============================================================

-- Color del clip en la timeline. Antes todo era Apricot y no se distinguia
-- nada al recorrerla.
LIB.COLOR_ROLL = {
  A = "Orange",
  B = "Teal",
  -- Behind the scenes: material de detras de camara. Ni sostiene la pieza (A)
  -- ni la ilustra (B) — es otra cosa, y en la timeline tiene que cantarse a la
  -- primera para no confundirlo con recurso al buscar un plano.
  BTS = "Violet",
  descarte = "Chocolate",
}

-- ---------------------------------------------------------------
-- Linkeo de items en la timeline
--
-- Peticion permanente del usuario (2026-08-03): "los distintos angulos y el
-- audio linkealos a partir de ahora siempre, luego resulta enredoso querer
-- pasar algo pero que no se mueva con todo lo demas".
--
-- Son dos mecanismos distintos:
--   - El audio con SU clip lo resuelve el MERGE del Media Pool
--     (MediaPool:AutoSyncAudio): queda un solo item y se mueve entero. Eso no
--     pasa por aqui.
--   - Ligar el clip base con la companera de otra camara es un link de
--     TIMELINE, y para eso hace falta Timeline:SetClipsLinked, que NO estaba
--     en la lista de API medida de Resolve Free. Se mide con los spikes S7/S8
--     de resolve/spikes_v2.lua.
--
-- Esta funcion no asume que exista: comprueba el metodo, lo intenta una vez y
-- recuerda el resultado. Si Resolve Free no lo expone, devuelve false en vez
-- de fingir que ligo — prometer un link que no se aplico es peor que decirlo.
-- ---------------------------------------------------------------
LIB.LINK_DISPONIBLE = nil   -- nil = sin probar, true/false = medido

function LIB.ligarGrupo(tl, items)
  if not tl or not items or #items < 2 then return false end
  if LIB.LINK_DISPONIBLE == false then return false end
  if type(tl.SetClipsLinked) ~= "function" then
    if LIB.LINK_DISPONIBLE == nil then
      LIB.LINK_DISPONIBLE = false
      decir("  · Timeline:SetClipsLinked no existe en esta version de Resolve: "
        .. "los angulos NO quedan ligados entre si.")
      decir("    (el audio de cada clip SI se mueve con el suyo si corriste el "
        .. "merge del Media Pool)")
      decir("    Para ligarlos a mano: seleccionar los items -> Clip -> Link Clips")
    end
    return false
  end
  local ok, res = pcall(function() return tl:SetClipsLinked(items, true) end)
  if not ok or res == false then
    if LIB.LINK_DISPONIBLE == nil then
      LIB.LINK_DISPONIBLE = false
      decir("  · SetClipsLinked existe pero rechazo el grupo: "
        .. tostring(ok and res or res))
    end
    return false
  end
  if LIB.LINK_DISPONIBLE == nil then
    LIB.LINK_DISPONIBLE = true
    decir("  · Linkeo de angulos ACTIVO (Timeline:SetClipsLinked responde).")
  end
  return true
end

function LIB.colorDeRoll(roll, porDefecto)
  return LIB.COLOR_ROLL[roll or ""] or porDefecto or "Apricot"
end

-- ---------------------------------------------------------------
-- COLOR POR GRUPO DE TOMAS (2026-08-18, cobertura de agosto dia 2)
--
-- Peticion del editor: "en la timeline de A-Roll quiero que asignes colores
-- dependiendo de la toma que es y sus intentos, para poder distinguir los
-- diferentes Reels que hay".
--
-- Todos los intentos del MISMO texto comparten color; donde cambia el color,
-- empieza otra pieza. Es la lectura que el rol solo no da: con `colorDeRoll`
-- el A-roll entero sale naranja y catorce intentos seguidos son una mancha.
--
-- POR QUE ESTOS DOCE Y NO LOS DIECISEIS. Resolve tiene 16 colores de clip, pero
-- cuatro ya significan otra cosa en esta timeline y reusarlos haria que una
-- toma se leyera como recurso al pasar de largo:
--     Orange    = A-roll sin grupo (la entrevista: habla, pero no repite texto)
--     Teal      = B-roll
--     Violet    = behind the scenes
--     Chocolate = descarte
-- Quedan doce. Con mas de doce grupos el ciclo se repite, y se avisa: dos
-- grupos del mismo color quedan a doce de distancia, nunca pegados.
--
-- El color sale del ID del grupo, no de su posicion: asi re-hornear no
-- recolorea la timeline entera y el editor no pierde la referencia de ayer.
LIB.COLORES_TOMA = {
  "Apricot", "Yellow", "Lime", "Olive", "Green", "Navy",
  "Blue", "Purple", "Pink", "Tan", "Beige", "Brown",
}

function LIB.colorDeGrupo(g, porDefecto)
  local n = tonumber(g)
  if not n or n <= 0 then return porDefecto end
  return LIB.COLORES_TOMA[(math.floor(n - 1) % #LIB.COLORES_TOMA) + 1]
end

-- Color de un clip en la timeline de A-ROLL: manda el grupo de tomas; si el
-- clip no tiene grupo (entrevista, recurso), manda el rol.
function LIB.colorDeClip(info, porDefecto)
  local i = info or {}
  local toma = i.toma
  if toma and toma.g and tonumber(toma.g) and tonumber(toma.g) > 0 then
    return LIB.colorDeGrupo(toma.g, porDefecto)
  end
  return LIB.colorDeRoll(i.roll, porDefecto)
end

-- Filtra una lista de recs por rol. `recs` son los registros que arma el
-- script de proyecto ({clip=, name=, fpath=, info=}).
function LIB.filtrarPorRoll(recs, roll)
  local out = {}
  for _, r in ipairs(recs or {}) do
    if (r.info and r.info.roll or "") == roll then out[#out+1] = r end
  end
  return out
end

-- ---------------------------------------------------------------
-- REELS / CAPSULAS y CORTE DE SILENCIOS  (v0.4.0)
--
-- Un rodaje de campaña graba cinco o seis piezas el mismo dia y vuelve como una
-- cronologia plana. `bin/derive_reels.py` decide a que pieza pertenece cada
-- clip; aqui solo se pinta. Y en una pieza con prompter, el locutor se para a
-- esperar a que avance el texto: `bin/derive_silence_cuts.py` calcula que
-- tramos se conservan y aqui se colocan uno detras de otro.
--
-- Colores NUEVOS, elegidos para no pisar la tabla de entrevistas de
-- metodologia/marcadores.md (Purple/Blue/Sand/Mint/Lemon/Green/Cyan ya
-- significan otra cosa y reeducar el ojo del editor sale caro):
--   Sky      — empieza el bloque de un reel
--   Lavender — empieza una toma
--   Rose     — la toma trae un fallo cantado ("me equivoque", "no, esperate")
--   Sand     — aqui se quito un silencio  (opt-in; Sand ya es "pausa" en
--              entrevista, y aqui significa lo mismo: hubo una pausa)
-- ---------------------------------------------------------------
LIB.COLOR_REEL     = "Sky"
LIB.COLOR_FIN_REEL = "Cocoa"
-- Otra capsula DENTRO de un clip ya asignado. Color propio porque no es lo
-- mismo que abrir bloque: el clip sigue perteneciendo a su capsula, pero a
-- partir de aqui habla de otra. Sin este marcador esa segunda capsula solo
-- existia en un informe, y se llego a dar por no rodada.
LIB.COLOR_REEL_DENTRO = "Fuchsia"
LIB.COLOR_TOMA     = "Lavender"
LIB.COLOR_FALLO    = "Rose"
LIB.COLOR_CORTE    = "Sand"

-- Marcador en la REGLA de la timeline, no en un clip.
--
-- La diferencia importa para trabajar: un marcador de item vive DENTRO del clip
-- y hay que estar encima de el para verlo; uno de timeline se lee de un vistazo
-- recorriendo la regla, que es como se busca "donde acaban los intentos de este
-- reel" (peticion del usuario, 2026-08-07).
--
-- `frameAbsoluto` es el que devuelve TimelineItem:GetStart(), que incluye el
-- arranque de la timeline (01:00:00:00 en un proyecto normal). AddMarker los
-- quiere RELATIVOS, asi que hay que restar GetStartFrame() — sin eso los
-- marcadores caen una hora mas alla del material y no se ve ninguno.
function LIB.marcarEnTimeline(tl, frameAbsoluto, color, nombre, nota, asign)
  local base = 0
  pcall(function() base = tonumber(tl:GetStartFrame()) or 0 end)
  local f = math.floor((tonumber(frameAbsoluto) or 0) - base)
  if f < 0 then f = 0 end
  if asign then f = asign.tomar(f) end
  local ok, res = pcall(function()
    return tl:AddMarker(f, color, nombre, nota or "", LIB.DURACION_MARKER, "")
  end)
  return (ok and res) and true or false
end

-- Filtra recs por numero de reel. reel == 0 o nil significa "sin capsula".
function LIB.filtrarPorReel(recs, n)
  local out = {}
  for _, r in ipairs(recs or {}) do
    if ((r.info and r.info.reel) or 0) == n then out[#out+1] = r end
  end
  return out
end

-- Etiqueta de una toma para el marker Lavender: "G96 T3/13 — completa".
function LIB.etiquetaDeToma(toma)
  if not toma then return nil end
  local estado = toma.completa and "completa" or "se queda corta"
  if toma.marcador and toma.marcador ~= "" then estado = toma.marcador end
  return string.format("G%d T%d/%d — %s", toma.g or 0, toma.n or 0,
                       toma.de or 0, estado)
end

-- Coloca un clip en la timeline respetando `info.cortes`.
--
-- `cortes` son los tramos que se CONSERVAN, en segundos de la fuente. Se
-- colocan seguidos, asi que las esperas de prompter desaparecen del montaje sin
-- que nada se borre del disco: son coordenadas.
--
-- Sin `cortes`, o con `cortar` en falso, hace el append normal de siempre. Ese
-- es el default: el corte de silencios NO aplica a todos los proyectos —en una
-- entrevista la pausa es contenido— y por eso se pide expresamente.
--
-- Devuelve (items colocados, tramos pedidos, segundos quitados).
function LIB.appendConCortes(mp, rec, fps, cortar)
  local info = rec.info or {}
  local cortes = info.cortes
  if not cortar or type(cortes) ~= "table" or #cortes == 0 then
    local res = mp:AppendToTimeline({rec.clip})
    return (res and #res or 0), 0, 0
  end
  local f = tonumber(fps) or 24
  local colocados, quitado, cursor = 0, 0, 0
  for _, tramo in ipairs(cortes) do
    local a, b = tonumber(tramo[1]) or 0, tonumber(tramo[2]) or 0
    if b > a then
      quitado = quitado + (a - cursor)
      cursor = b
      local ini = math.floor(a * f + 0.5)
      local fin = math.floor(b * f + 0.5) - 1
      if fin > ini then
        local res = mp:AppendToTimeline({{
          mediaPoolItem = rec.clip, startFrame = ini, endFrame = fin,
        }})
        if res and #res > 0 then
          colocados = colocados + #res
        else
          -- Un tramo que no aterriza es habla que desaparece de la timeline sin
          -- que nadie lo note. Se dice en voz alta.
          decir(string.format("  [corte-FAIL] %s tramo %.2f–%.2fs "
            .. "(frames %d..%d) no se coloco", rec.name or "?", a, b, ini, fin))
        end
      end
    end
  end
  local dur = tonumber(info.dur) or cursor
  quitado = quitado + math.max(0, dur - cursor)
  return colocados, #cortes, quitado
end

-- ---------------------------------------------------------------
-- CRONOLOGIA REAL: el lavalier como espina dorsal
--
-- POR QUE (2026-08-13). La timeline de B-ROLL se armaba con AppendToTimeline
-- clip tras clip: quedaba EMPACADA, espalda con espalda, sin los huecos reales
-- del dia. Un lavalier que grabo tres horas seguidas no puede cuadrar contra
-- eso — se desfasa exactamente por la suma de los huecos eliminados, y el error
-- crece a lo largo de la timeline. Eso es el "desfase acumulativo" que reporto
-- el editor, y no se arregla midiendo mejor: se arregla colocando por hora.
--
-- El hueco NO es un defecto. El video no rueda todo el tiempo y el lavalier si;
-- la timeline tiene que enseñar esa forma.
--
-- QUE HACE
--   · t=0 en el instante mas temprano de todo lo que se coloca.
--   · Cada WAV entra INTEGRO —sin perder un segundo— en la pista de su cadena
--     y en su hora, pero CORTADO en los instantes que se le pasen. Los pedazos
--     quedan contiguos: el corte es editorial, no un hueco.
--   · Cada video en su pista y en su hora.
--   · Lo que no tiene hora utilizable NO se coloca, y se dice cuanto.
--
-- QUIEN MARCA LOS CORTES lo decide quien llama: con varias camaras rodando a
-- la vez, los bordes de todas darian un picado inutil. Lo natural es pasar solo
-- los de la camara base —la que llevaba el lavalier—, que es la unica con
-- relacion fisica con ese audio.
--
-- No hay API de razor en Resolve. El corte se consigue apilando appends con
-- startFrame/endFrame, el mismo mecanismo de LIB.appendConCortes.
-- ---------------------------------------------------------------

local function aFrame(seg, fps)
  return math.floor(seg * fps + 0.5)
end

-- Dias desde 1970-01-01 de una fecha del calendario gregoriano. Aritmetica
-- pura (days_from_civil, H. Hinnant): no depende de la zona de la Mac ni de os.
local function diasDesdeEpoch(y, m, d)
  if m <= 2 then y = y - 1 end
  local era = math.floor(y / 400)
  local yoe = y - era * 400
  local doy = math.floor((153 * ((m + 9) % 12) + 2) / 5) + d - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end
LIB.diasDesdeEpoch = diasDesdeEpoch

-- Epoch de un ISO 8601. Si trae zona (Z, +hh:mm, -hh:mm) se respeta; si no, es
-- hora local. os.time() lee la tabla como hora LOCAL: leer asi un "...Z" corria
-- 6 h (UTC-6) a los clips que se colocan por el reloj de su camara, mientras
-- los que se colocan por su par de sync iban en epoch verdadero. En la B-ROLL
-- los clips de Adrian caian casi 6 h despues que el resto (Asistente, 2026-10-01).
--
-- Con zona, el epoch sale de la aritmetica del calendario y no de os.time. La
-- version anterior sacaba el desfase local-UTC con os.time(os.date("!*t", t)),
-- cuya tabla trae isdst=false: en verano mktime la leia como hora estandar y
-- el desfase salia una hora corto. Con TZ=America/Los_Angeles o Europe/Madrid,
-- "2026-07-01T10:00:00Z" daba 3600 s de menos (revision del PR #1, 2026-10-05).
-- En Mexico no se veia porque ya no hay horario de verano.
-- Las partes de un ISO 8601, o nil si no lo es. `zona` es nil si no trae.
local function partesIso(s)
  if type(s) ~= "string" or s == "" then return nil end
  local y, mo, d, h, mi, sec, resto = string.match(s,
    "^(%d+)-(%d+)-(%d+)[T ](%d+):(%d+):(%d+)(.*)$")
  if not y then return nil end
  local frac = tonumber(string.match(resto or "", "^(%.%d+)") or "0") or 0
  local zona = string.match(resto or "", "([Zz])$")
               or string.match(resto or "", "([%+%-]%d%d:?%d%d)$")
  return {y = tonumber(y), mo = tonumber(mo), d = tonumber(d), h = tonumber(h),
          mi = tonumber(mi), sec = tonumber(sec), frac = frac, zona = zona}
end

-- Epoch de un ISO CON zona, por aritmetica del calendario: no usa `os`, asi
-- que vale en el menu de Free (plan Free, principio 2). Sin zona devuelve
-- (nil, "sin zona"): la hora local de la Mac que corre el script no es la del
-- rodaje, y adivinarla es justo lo que corria 6 h la cronologia. Quien llama
-- decide que hacer con ese clip; el aplicador lo deja sin hora y lo dice.
function LIB.isoEpochConZona(s)
  local p = partesIso(s)
  if not p then return nil, "no es ISO" end
  if not p.zona then return nil, "sin zona" end
  local e = diasDesdeEpoch(p.y, p.mo, p.d) * 86400 + p.h * 3600 + p.mi * 60 + p.sec
  if p.zona ~= "Z" and p.zona ~= "z" then
    local sg, zh, zm = string.match(p.zona, "([%+%-])(%d%d):?(%d%d)")
    e = e - (tonumber(zh) * 3600 + tonumber(zm) * 60) * (sg == "-" and -1 or 1)
  end
  return e + p.frac
end

-- La de los scripts por proyecto: con zona es la de arriba; sin zona, hora
-- local con os.time (deuda DEUDA_LUA del lint: el aplicador no pasa por aqui).
function LIB.isoEpoch(s)
  local e, porque = LIB.isoEpochConZona(s)
  if e or porque ~= "sin zona" then return e end
  local p = partesIso(s)
  local y, mo, d, h, mi, sec, frac = p.y, p.mo, p.d, p.h, p.mi, p.sec, p.frac
  return os.time({year = y, month = mo, day = d, hour = h, min = mi, sec = sec}) + frac
end

-- El frame donde arranca la timeline (01:00:00:00 = 86400 a 24 fps). Todo lo
-- que se coloca por tiempo CALCULADO va sumado a esto: `recordFrame` es
-- absoluto. Era la trampa que la doctrina dejo anotada en The Shelter
-- (2026-08-04) y en la que cayo la cronologia: el primer tramo del dia quedaba
-- antes del arranque de la timeline.
function LIB.inicioDe(tl)
  local base = 0
  if tl and tl.GetStartFrame then
    pcall(function() base = tonumber(tl:GetStartFrame()) or 0 end)
  end
  return base
end

-- COMO LEE AppendToTimeline EL endFrame se mide con el primer pedazo, una vez
-- por corrida. En Free 21.0.2 era inclusivo (2026-08-13); en Studio 21.1.0.17
-- es exclusivo (2026-10-01): cada pedazo salia un frame corto y cada corte del
-- WAV dejaba un hueco de un frame en la timeline y un frame de audio fuera. La
-- garantia de abajo no lo veia porque sumaba lo PEDIDO, no lo colocado.
LIB.EF_EXTRA = nil
local function anexarPedazo(mp, tl, clip, pista, fIni, fFin, rec)
  local function poner(extra)
    local res = mp:AppendToTimeline({{
      mediaPoolItem = clip, mediaType = 2, trackIndex = pista,
      startFrame = fIni, endFrame = fFin - 1 + extra, recordFrame = rec,
    }})
    return res and res[1]
  end
  local it = poner(LIB.EF_EXTRA or 0)
  if it and LIB.EF_EXTRA == nil and type(it) ~= "boolean" and it.GetDuration then
    local falta = math.floor((fFin - fIni) - tonumber(it:GetDuration()) + 0.5)
    if falta ~= 0 and tl and tl.DeleteClips then
      tl:DeleteClips({it})
      it = poner(falta)
    end
    LIB.EF_EXTRA = falta
  end
  return it
end

-- Coloca un WAV entero, partido en los instantes de `cortes` (epoch absoluto).
-- Devuelve (pedazos_colocados, frames_totales, aviso|nil).
--
-- Los limites se calculan UNA vez en frames y se reusan como fin de un pedazo y
-- principio del siguiente. Redondear cada borde por separado dejaria huecos de
-- un frame o solapes segun cayera el decimal.
function LIB.colocarAudioPartido(mp, audio, t0, fps, cortes, pista, tl)
  local epoch, dur = tonumber(audio.epoch), tonumber(audio.dur)
  if not epoch or not dur or dur <= 0 then
    return 0, 0, "sin epoch o sin duracion"
  end

  local bordes = {0.0}
  for _, c in ipairs(cortes or {}) do
    local rel = c - epoch
    if rel > 0.0 and rel < dur then bordes[#bordes + 1] = rel end
  end
  bordes[#bordes + 1] = dur
  table.sort(bordes)

  local base = LIB.inicioDe(tl) + aFrame(epoch - t0, fps)
  local colocados, frames, anterior = 0, 0, nil
  for i = 1, #bordes - 1 do
    local fIni = aFrame(bordes[i], fps)
    local fFin = aFrame(bordes[i + 1], fps)          -- exclusivo
    if fFin > fIni and fIni ~= anterior then
      local it = anexarPedazo(mp, tl, audio.clip, pista, fIni, fFin, base + fIni)
      if it then
        colocados = colocados + 1
        -- lo COLOCADO, si Resolve lo dice; si no, lo pedido
        local puesto = (type(it) == "table" and it.GetDuration and tonumber(it:GetDuration()))
        frames = frames + (puesto or (fFin - fIni))
      else
        -- Un pedazo que no aterriza es audio que desaparece sin avisar.
        decir(string.format("  [audio-FAIL] %s pedazo %d (frames %d..%d) no se coloco",
          audio.name or "?", i, fIni, fFin - 1))
      end
      anterior = fIni
    end
  end

  -- La garantia: los pedazos tienen que sumar el WAV entero.
  local esperado = aFrame(dur, fps)
  if colocados > 0 and frames ~= esperado then
    return colocados, frames, string.format(
      "%s: los pedazos suman %d frames y el WAV dura %d (faltan %d)",
      audio.name or "?", frames, esperado, esperado - frames)
  end
  return colocados, frames, nil
end

-- Arma una timeline entera por tiempo real.
--
-- plan = {
--   fps      = 24,
--   t0       = epoch del instante mas temprano (opcional: se deduce),
--   audios   = { {clip=, name=, epoch=, dur=, pista=}, ... },
--   videos   = { {clip=, name=, epoch=, pista=, ligar=bool}, ... },
--   cortes   = { epoch, ... }   -- bordes con los que partir los WAV
-- }
--
-- Devuelve un resumen: {audios=, pedazos=, videos=, fuera=, avisos={...}}
function LIB.construirPorTiempoReal(mp, tl, plan)
  local fps = LIB.fpsSeguro(plan.fps, tl)
  local res = {audios = 0, pedazos = 0, videos = 0, fuera = 0, avisos = {}}

  local t0 = plan.t0
  if not t0 then
    for _, a in ipairs(plan.audios or {}) do
      local e = tonumber(a.epoch)
      if e and (not t0 or e < t0) then t0 = e end
    end
    for _, v in ipairs(plan.videos or {}) do
      local e = tonumber(v.epoch)
      if e and (not t0 or e < t0) then t0 = e end
    end
  end
  if not t0 then
    res.avisos[#res.avisos + 1] =
      "ningun elemento tiene hora: no se puede construir la cronologia"
    return res
  end
  res.t0 = t0

  for _, a in ipairs(plan.audios or {}) do
    local n, _, aviso = LIB.colocarAudioPartido(
      mp, a, t0, fps, plan.cortes, a.pista or 1, tl)
    if n > 0 then res.audios = res.audios + 1 end
    res.pedazos = res.pedazos + n
    if aviso then res.avisos[#res.avisos + 1] = aviso end
  end

  res.desbordados = 0
  -- Lo ya colocado por pista: {{ini, fin}, ...} en frames de timeline.
  local ocupado = {}
  local function libre(p, ini, fin)
    for _, iv in ipairs(ocupado[p] or {}) do
      if ini < iv[2] and iv[1] < fin then return false end
    end
    return true
  end
  for _, v in ipairs(plan.videos or {}) do
    local e = tonumber(v.epoch)
    local inicio = LIB.inicioDe(tl)
    local rec = e and (inicio + aFrame(e - t0, fps)) or nil
    if rec and rec >= inicio then
      -- COLOCAR SIN PERDER NADA.
      --
      -- Dos clips de la misma camara se solapan en tiempo real cuando el reloj
      -- de alguno esta mal (Morsa: 3 clips perdidos en silencio) o cuando son
      -- CAMARA LENTA: un clip a 120p que se reproduce a 24 dura en el archivo
      -- cinco veces lo que duro la toma (Asistente, 29-sep: VIDEO 03 entero).
      --
      -- La pista libre se elige AQUI, con lo que ya se coloco, y no se espera a
      -- que Resolve rechace el solape: en Free 21.0.2 AppendToTimeline fallaba
      -- sobre un hueco ocupado, pero en Studio 21.1.0.17 PISA lo que hay y el
      -- clip de abajo desaparece sin aviso (2026-10-01: 9806 y 9809). Un clip
      -- en la pista de al lado es un problema de orden; un clip que desaparece
      -- es material perdido, y eso no se negocia.
      local pista = v.pista or 1
      local nfr = tonumber(v.frames)
        or (v.clip and v.clip.GetClipProperty and tonumber(v.clip:GetClipProperty("Frames") or ""))
        or 0
      local rv, ra, usada
      for intento = 0, 3 do
        local p = pista + intento
        if nfr <= 0 or libre(p, rec, rec + nfr) then
          while tl:GetTrackCount("video") < p do tl:AddTrack("video") end
          while tl:GetTrackCount("audio") < p do tl:AddTrack("audio", "stereo") end
          rv = mp:AppendToTimeline({{mediaPoolItem = v.clip, mediaType = 1,
                                     trackIndex = p, recordFrame = rec}})
          if rv and rv[1] then
            usada = p
            ocupado[p] = ocupado[p] or {}
            local puesta = (rv[1].GetDuration and tonumber(rv[1]:GetDuration())) or 0
            table.insert(ocupado[p], {rec, rec + math.max(nfr, puesta)})
            ra = mp:AppendToTimeline({{mediaPoolItem = v.clip, mediaType = 2,
                                       trackIndex = p, recordFrame = rec}})
            break
          end
        end
      end
      if rv and rv[1] then
        res.videos = res.videos + 1
        if usada ~= pista then
          res.desbordados = res.desbordados + 1
          res.avisos[#res.avisos + 1] = string.format(
            "%s no cabia en V%d (choca con otro clip a la misma hora): va en V%d",
            v.name or "?", pista, usada)
        end
        if v.ligar ~= false and LIB.ligarGrupo and ra and ra[1] then
          LIB.ligarGrupo(tl, {rv[1], ra[1]})
        end
      else
        res.fuera = res.fuera + 1
        res.avisos[#res.avisos + 1] = string.format(
          "%s NO SE COLOCO: ninguna pista libre en su hora", v.name or "?")
      end
    else
      res.fuera = res.fuera + 1
    end
  end

  -- MARCAS DE NAVEGACION. En una timeline por tiempo real el material previo y
  -- los huecos empujan el contenido muy adentro: en Morsa la primera cancion
  -- pasa del minuto 19 (empacada) al 65. Sin una marca hay que buscarla a mano,
  -- y no encontrarla se parece demasiado a que no este.
  for i, w in ipairs(plan.marcas or {}) do
    local ini = tonumber(w.inicio)
    if ini then
      LIB.marcarEnTimeline(tl, LIB.inicioDe(tl) + aFrame(ini - t0, fps), w.color or "Sky",
                           w.nombre or ("Bloque " .. i), w.nota or "")
    end
  end

  return res
end

-- ---------------------------------------------------------------
-- LA TIMELINE DE LOS AUDIOS: los lavalieres como timecode (2026-10-01)
--
-- Peticion del editor: "ahi quiero que metas los lavas, sin cortar, y sobre
-- eso coloques el B-roll y el A-roll". Lo habia pedido Berni el 28-sep con
-- otras palabras: "los lavalieres funcionan como timecode".
--
--   · Cada WAV ENTERO en la pista de su TX ("LAVA izq", "LAVA drc"), en su
--     hora. Las horas vienen alineadas por contenido desde el horneado: los
--     relojes de dos TX no coinciden (3 s el 29-sep en Asistente).
--   · Encima, cada clip en su hora: una pista de video por rol (A-ROLL, B-ROLL),
--     y otra del mismo rol si dos clips se enciman.
--   · El audio de un clip SOLO si no tiene lavalier ("CAM sin lava"). El de uno
--     con lavalier ya esta debajo, entero; y el clip mergeado traeria el lava
--     otra vez, regado en la pista siguiente (medido en Studio 21.1).
--
-- Todo se PLANEA antes de tocar Resolve, con intervalos, porque Studio 21.1
-- pisa en vez de fallar cuando un hueco esta ocupado. Las pistas de lavalier van
-- al final (regla dura): se crean despues de saber cuantas de camara hacen falta.
--
-- plan = {fps=, ordenTx={"izq","drc"},
--   audios = {{clip=, name=, epoch=, dur=, tx=, info=}, ...},
--   videos = {{clip=, name=, epoch=, roll="A"|"B", conLava=bool, frames=}, ...}}
-- Devuelve {videos=, camaras=, wavs=, fuera=, encimados=, avisos={},
--           wavItems={{item=, info=}}, pistasV={}, pistasA={}}
function LIB.construirLineaDeLavas(mp, tl, plan)
  local fps = LIB.fpsSeguro(plan.fps, tl)
  local inicio = LIB.inicioDe(tl)
  local res = {videos = 0, camaras = 0, wavs = 0, fuera = 0, encimados = 0,
               avisos = {}, wavItems = {}, pistasV = {}, pistasA = {}}
  local t0 = plan.t0
  for _, lista in ipairs({plan.audios or {}, plan.videos or {}}) do
    for _, x in ipairs(lista) do
      local e = tonumber(x.epoch)
      if e and (not t0 or e < t0) then t0 = e end
    end
  end
  if not t0 then
    res.avisos[#res.avisos + 1] = "nada tiene hora: no hay linea de tiempo"
    return res
  end
  res.t0 = t0
  local function frameDe(e) return inicio + aFrame(e - t0, fps) end
  local function choca(ivs, a, b)
    for _, iv in ipairs(ivs) do if a < iv[2] and iv[1] < b then return true end end
    return false
  end
  -- la primera pista de `clave` donde [a, b) cabe; si ninguna, una nueva
  local function asignar(pistas, clave, a, b)
    local vistas = 0
    for _, p in ipairs(pistas) do
      if p.clave == clave then
        vistas = vistas + 1
        if not choca(p.ivs, a, b) then
          table.insert(p.ivs, {a, b})
          return p, vistas > 1
        end
      end
    end
    local p = {clave = clave, n = vistas + 1, ivs = {{a, b}}}
    pistas[#pistas + 1] = p
    return p, vistas > 0
  end

  -- 1) plan de video: A-roll primero, para que A-ROLL quede en V1
  local videos = {}
  for _, v in ipairs(plan.videos or {}) do videos[#videos + 1] = v end
  table.sort(videos, function(x, y)
    local rx, ry = (x.roll == "A") and 0 or 1, (y.roll == "A") and 0 or 1
    if rx ~= ry then return rx < ry end
    return (tonumber(x.epoch) or 0) < (tonumber(y.epoch) or 0)
  end)
  local pistasV, pistasA, pistasW = {}, {}, {}
  for _, v in ipairs(videos) do
    local e = tonumber(v.epoch)
    local nfr = tonumber(v.frames)
      or (v.clip and v.clip.GetClipProperty and tonumber(v.clip:GetClipProperty("Frames") or ""))
      or 0
    if not e or nfr <= 0 then
      res.fuera = res.fuera + 1
      res.avisos[#res.avisos + 1] = (v.name or "?") .. ": sin hora o sin duracion, no se coloca"
    else
      v._ini = frameDe(e)
      v._fin = v._ini + nfr
      local rol = (v.roll == "A") and "A-ROLL" or "B-ROLL"
      local encimado
      v._pv, encimado = asignar(pistasV, rol, v._ini, v._fin)
      if encimado then res.encimados = res.encimados + 1 end
      if not v.conLava then v._pa = asignar(pistasA, "CAM sin lava", v._ini, v._fin) end
    end
  end
  -- 2) plan de lavalieres: una pista por TX, en el orden pedido
  local rango = {}
  for i, tx in ipairs(plan.ordenTx or {}) do rango[tx] = i end
  local audios = {}
  for _, a in ipairs(plan.audios or {}) do audios[#audios + 1] = a end
  table.sort(audios, function(x, y)
    local rx, ry = rango[x.tx or ""] or 99, rango[y.tx or ""] or 99
    if rx ~= ry then return rx < ry end
    if (x.tx or "") ~= (y.tx or "") then return (x.tx or "") < (y.tx or "") end
    return (tonumber(x.epoch) or 0) < (tonumber(y.epoch) or 0)
  end)
  for _, a in ipairs(audios) do
    local e, d = tonumber(a.epoch), tonumber(a.dur)
    if e and d and d > 0 then
      a._ini = frameDe(e)
      a._pw = asignar(pistasW, "LAVA " .. (a.tx or "?"), a._ini, a._ini + aFrame(d, fps))
    else
      res.avisos[#res.avisos + 1] = (a.name or "?") .. ": WAV sin hora, no se coloca"
    end
  end

  -- 3) pistas: video por rol; audio = camara sin lava y DESPUES los lavalieres
  local function nombre(p) return p.clave .. (p.n > 1 and (" " .. p.n) or "") end
  for i, p in ipairs(pistasV) do p.idx = i end
  for i, p in ipairs(pistasA) do p.idx = i end
  for i, p in ipairs(pistasW) do p.idx = #pistasA + i end
  while tl:GetTrackCount("video") < #pistasV do tl:AddTrack("video") end
  local nAudio = #pistasA + #pistasW
  while tl:GetTrackCount("audio") < nAudio do
    local siguiente = tl:GetTrackCount("audio") + 1
    if not tl:AddTrack("audio", siguiente <= #pistasA and "stereo" or "mono") then break end
  end
  for _, p in ipairs(pistasV) do tl:SetTrackName("video", p.idx, nombre(p)); res.pistasV[p.idx] = nombre(p) end
  for _, p in ipairs(pistasA) do tl:SetTrackName("audio", p.idx, nombre(p)); res.pistasA[p.idx] = nombre(p) end
  for _, p in ipairs(pistasW) do tl:SetTrackName("audio", p.idx, nombre(p)); res.pistasA[p.idx] = nombre(p) end

  -- 4) colocar: video (y el audio de camara de los que no tienen lava)
  for _, v in ipairs(videos) do
    if v._pv then
      local rv = mp:AppendToTimeline({{mediaPoolItem = v.clip, mediaType = 1,
                                       trackIndex = v._pv.idx, recordFrame = v._ini}})
      local item = rv and rv[1]
      if item then
        res.videos = res.videos + 1
        if item.SetClipColor then item:SetClipColor(LIB.colorDeRoll(v.roll)) end
        if v._pa then
          local ra = mp:AppendToTimeline({{mediaPoolItem = v.clip, mediaType = 2,
                                           trackIndex = v._pa.idx, recordFrame = v._ini}})
          if ra and ra[1] then
            res.camaras = res.camaras + 1
            if LIB.ligarGrupo then LIB.ligarGrupo(tl, {item, ra[1]}) end
          end
        end
      else
        res.fuera = res.fuera + 1
        res.avisos[#res.avisos + 1] = (v.name or "?") .. ": Resolve no lo coloco"
      end
    end
  end
  -- 5) los lavalieres, ENTEROS (sin startFrame/endFrame), al final
  for _, a in ipairs(audios) do
    if a._pw then
      local rw = mp:AppendToTimeline({{mediaPoolItem = a.clip, mediaType = 2,
                                       trackIndex = a._pw.idx, recordFrame = a._ini}})
      if rw and rw[1] then
        res.wavs = res.wavs + 1
        res.wavItems[#res.wavItems + 1] = {item = rw[1], info = a.info}
      else
        res.avisos[#res.avisos + 1] = (a.name or "?") .. ": Resolve no coloco el WAV"
      end
    end
  end
  return res
end

-- ---------------------------------------------------------------
-- Bins
--
-- ADVERTENCIA: la API solo tiene MoveClips — no hay alias ni copia, un clip
-- vive en UN bin. Mover clips los saca de la organizacion manual del editor.
-- Por eso: opt-in, y ANTES de mover se graba el bin de origen de cada clip en
-- <disco>/.cinema_assistant/resolve/<proyecto>_bins_origen.lua, que
-- restaurar_bins.lua sabe leer.
-- ---------------------------------------------------------------

-- Recorre el Media Pool y devuelve {ruta -> Folder} y {Folder -> ruta}.
function LIB.mapaDeBins(rootFolder)
  local porRuta, rutaDe = {}, {}
  local function rec(folder, ruta)
    local aqui = (ruta == "") and folder:GetName() or (ruta .. "/" .. folder:GetName())
    porRuta[aqui] = folder
    rutaDe[folder] = aqui
    for _, s in ipairs(folder:GetSubFolderList() or {}) do rec(s, aqui) end
  end
  rec(rootFolder, "")
  return porRuta, rutaDe
end

-- Bin hijo por nombre; lo crea si no existe.
function LIB.binHijo(mp, padre, nombre)
  for _, s in ipairs(padre:GetSubFolderList() or {}) do
    if s:GetName() == nombre then return s end
  end
  return mp:AddSubFolder(padre, nombre)
end

-- Escribe el registro de origen para poder deshacer.
function LIB.guardarOrigenBins(ruta, registros)
  local f, err = io.open(ruta, "w")
  if not f then return false, tostring(err) end
  f:write("-- Generado por el asistente de edicion al mover clips a bins\n")
  f:write("-- A-ROLL / B-ROLL. Lo lee restaurar_bins.lua para devolver cada\n")
  f:write("-- clip a su bin original. NO borrar hasta estar conforme.\n")
  f:write("return {\n")
  for _, r in ipairs(registros) do
    f:write(string.format("  {path=%q, origen=%q, destino=%q},\n",
                          r.path or "", r.origen or "", r.destino or ""))
  end
  f:write("}\n")
  f:close()
  return true
end

-- Nombre de bin por rol. Un rol que no este aqui NO se mueve: mover a un bin
-- sin nombre acordado dejaria clips donde nadie los busca.
-- ===============================================================
-- VARIAS ASISTENCIAS EN UN MISMO PROYECTO DE RESOLVE
--
-- Peticion permanente del editor (2026-08-18): "adapta el asistente para que
-- siempre permita hacer varias asistencias diferentes en un proyecto". El caso
-- llego solo: abrio el dia 2 de un cliente en el mismo proyecto donde tenia los
-- reels del dia 1.
--
-- Son TRES cosas distintas y las tres tienen que cumplirse:
--
--   1. Las timelines no se pisan  -> cada script borra y crea solo las que
--      empiezan por su PFX. Ya estaba.
--   2. Los clips ajenos no se procesan -> `LIB.esAjeno`, aqui abajo. Antes se
--      colaban y la garantia de cobertura los denunciaba como perdidos: 178
--      falsos positivos en el caso real, tapando los de verdad.
--   3. Los bins no se mezclan -> `LIB.moverABins` acepta prefijo. Sin el, dos
--      asistencias vuelcan su material en el MISMO bin "A-ROLL" y deshacerlo
--      deja de ser posible por separado.
--
-- Lo comprueba `tests/test_multiproyecto.py`, que falla si un
-- asistente_<proyecto>.lua se queda sin la guarda.
-- ===============================================================

-- ¿Este video del Media Pool pertenece a OTRO proyecto?
--
-- `dataFor` devuelve una tabla vacia cuando el clip no esta en el horneado. Eso
-- es la definicion operativa de "no es de este proyecto": el horneado tiene
-- TODOS los clips indexados del disco, asi que si no esta, no es nuestro.
function LIB.esAjeno(info)
  return (type(info) ~= "table") or (next(info) == nil)
end

-- Se dice con numero y con nombres. Callarlo seria peor que el bug: el editor
-- necesita saber que el script vio material que no proceso.
function LIB.reportarAjenos(ajenos)
  if not ajenos or #ajenos == 0 then return end
  decir(string.format("Videos del Media Pool que NO son de este proyecto: %d "
    .. "(ignorados; sus timelines y sus bins no se tocan)", #ajenos))
  for i = 1, math.min(5, #ajenos) do decir("   · " .. tostring(ajenos[i])) end
  if #ajenos > 5 then decir(string.format("   ... y %d mas", #ajenos - 5)) end
end

LIB.BIN_DE_ROLL = {
  A = "A-ROLL",
  B = "B-ROLL",
  BTS = "BEHIND THE SCENES",
}

-- Mueve los clips a los bins A-ROLL / B-ROLL / BEHIND THE SCENES bajo `binPadre`.
-- Devuelve (movidos, registros). NO mueve nada si `rutaRegistro` no se puede
-- escribir: sin registro no hay vuelta atras, y mover sin poder deshacer no
-- vale el riesgo.
-- `prefijo` (opcional) va delante del nombre del bin: con dos asistencias en el
-- mismo proyecto, "DE2 — A-ROLL" y "IMODAE — A-ROLL" son bins distintos. Sin el,
-- las dos vuelcan en el mismo y restaurar_bins ya no puede deshacer una sola.
-- Vacio = comportamiento de siempre, para no mover los bins de nadie.
function LIB.moverABins(mp, rootFolder, recs, binPadre, rutaRegistro, prefijo)
  local _, rutaDe = LIB.mapaDeBins(rootFolder)
  local porRoll = {}
  local registros = {}

  for _, r in ipairs(recs or {}) do
    local roll = (r.info and r.info.roll) or ""
    local nombreBin = LIB.BIN_DE_ROLL[roll]
    if nombreBin then nombreBin = (prefijo or "") .. nombreBin end
    if nombreBin then
      porRoll[roll] = porRoll[roll] or {}
      table.insert(porRoll[roll], r.clip)
      local mpi = r.clip
      local origen = ""
      -- El bin de origen se conoce por el recorrido que hizo el script de
      -- proyecto; si no viene, se queda vacio y restaurar_bins lo reporta.
      if r.binpath and r.binpath ~= "" then origen = r.binpath end
      registros[#registros+1] = {
        path = (mpi.GetClipProperty and mpi:GetClipProperty("File Path")) or "",
        origen = origen,
        destino = nombreBin,
      }
    end
  end

  if #registros == 0 then return 0, {} end

  local ok, err = LIB.guardarOrigenBins(rutaRegistro, registros)
  if not ok then
    decir("  ABORTADO: no se pudo escribir el registro de origen (" ..
          tostring(err) .. ").")
    decir("  Sin registro no hay como deshacer el movimiento — no muevo nada.")
    return 0, {}
  end

  local movidos = 0
  for roll, clips in pairs(porRoll) do
    local nombre = (prefijo or "") .. LIB.BIN_DE_ROLL[roll]
    local destino = LIB.binHijo(mp, binPadre, nombre)
    if destino and #clips > 0 then
      if mp:MoveClips(clips, destino) then
        movidos = movidos + #clips
        decir(string.format("  bin %s: %d clips", nombre, #clips))
      else
        decir("  AVISO: MoveClips fallo para " .. nombre)
      end
    end
  end
  decir("  registro de origen: " .. rutaRegistro)
  return movidos, registros
end

-- Resumen por tipo, para el reporte final del script de proyecto.
function LIB.contarBeats(clips)
  local por = {}
  for _, c in pairs(clips or {}) do
    for _, b in ipairs(c.beats or {}) do
      por[b.kind or "?"] = (por[b.kind or "?"] or 0) + 1
    end
  end
  return por
end

return LIB
