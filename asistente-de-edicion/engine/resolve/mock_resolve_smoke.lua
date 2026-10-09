-- Smoke test runtime de un asistente_*.lua SIN Resolve.
-- Uso:  lua mock_resolve_smoke.lua asistente_mab.lua
-- Ejecuta el script con un Media Pool vacío: recorre carga de datos,
-- multicam, construcción de timelines y cierre. Caza errores runtime que
-- luac -p no ve (globals nil tras renombres, métodos mal escritos).
-- Limitación: no ejerce placeSyncAudio/placeCompanions con items reales.
local Folder = {}
Folder.__index = Folder
function Folder.new(name) return setmetatable({name=name}, Folder) end
function Folder:GetName() return self.name end
function Folder:GetClipList() return {} end
function Folder:GetSubFolderList() return {} end

local mp = {}
function mp:GetRootFolder() return Folder.new("Master") end
function mp:AddSubFolder(parent, name) return Folder.new(name) end
function mp:SetCurrentFolder() return true end
function mp:AppendToTimeline() return nil end
function mp:CreateEmptyTimeline(name) return nil end
function mp:ImportMedia() return {} end

local proj = {}
function proj:GetName() return "MOCK" end
function proj:GetTimelineCount() return 0 end
function proj:GetTimelineByIndex() return nil end
function proj:GetMediaPool() return mp end
function proj:SetCurrentTimeline() return true end
function proj:DeleteTimelines() return true end
function proj:GetSetting() return "24" end

local pm = {}
function pm:GetCurrentProject() return proj end
resolve = setmetatable({}, {__index=function() return function() return pm end end})

local target = arg and arg[1] or "asistente_mab.lua"
local ok, err = pcall(dofile, target)
print("== SMOKE " .. target .. " ==", ok and "OK" or ("FALLO: " .. tostring(err)))
if not ok then os.exit(1) end
