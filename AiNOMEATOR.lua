--[[
AiNOMEATOR.lua

Integracao com o Reaper — com UX de progresso em tempo real.

O que esse script faz:
  1. Escaneia as tracks do projeto (por padrao TODAS, nao so as selecionadas).
  2. Para cada track com audio, acha o item mais "representativo" (o de
     maior duracao usada) e pega o caminho do arquivo-fonte + a janela
     (inicio/duracao) que esse item realmente usa dessa fonte.
  3. Escreve um "manifest" leve (texto, tab-separated) com essas infos.
  4. Chama `batch_rename.py` de forma NAO-BLOQUEANTE (start /B no Windows,
     & no Mac/Linux), sem abrir janela de CMD.
  5. Usa reaper.defer() pra fazer polling do arquivo de log a cada 250ms,
     exibindo progresso ao vivo no console de scripts do Reaper.
  6. Quando o resultado estiver pronto, aplica em cada track: nome, cor e icone.

Requisitos:
  - Ter rodado `setup.bat` na pasta deste script (cria o venv + .env com a
    GEMINI_API_KEY). Veja o README.md.

Como instalar no Reaper:
  Actions > Show action list... > New action... > Load ReaScript...
  selecione este arquivo (AiNOMEATOR.lua). Depois e so rodar
  a acao sempre que quiser (ou atribuir um atalho de teclado).
]]

local gui_state = "config"

local _, script_path = reaper.get_action_context()
local script_dir = script_path:match("^(.*[/\\])")

local os_name = reaper.GetOS()
local is_windows = os_name:find("Win") ~= nil
local sep = is_windows and "\\" or "/"
local ui_font = is_windows and "Segoe UI" or "Arial"

local env_path = script_dir .. ".env"
local config_path = script_dir .. "colors.ini"

local lang = reaper.GetExtState("AiNOMEATOR", "language")
if lang == "" then lang = "en" end -- default to English

local saved_panns_threads = reaper.GetExtState("AiNOMEATOR", "panns_threads")
if saved_panns_threads == "" then saved_panns_threads = "1" end

local saved_panns_workers = reaper.GetExtState("AiNOMEATOR", "panns_workers")
if saved_panns_workers == "" then saved_panns_workers = "4" end

local backend = reaper.GetExtState("AiNOMEATOR", "backend")
if backend == "" then backend = "gemini" end

local saved_sort_tracks = reaper.GetExtState("AiNOMEATOR", "sort_tracks")
if saved_sort_tracks == "" then saved_sort_tracks = "false" end

local current_theme = reaper.GetExtState("AiNOMEATOR", "theme")
if current_theme == "" then current_theme = "default" end

local saved_show_advanced = reaper.GetExtState("AiNOMEATOR", "show_advanced")
local show_advanced = (saved_show_advanced == "true")

local saved_create_folders = reaper.GetExtState("AiNOMEATOR", "create_folders")
local create_folders = (saved_create_folders == "true")

local saved_delete_silent = reaper.GetExtState("AiNOMEATOR", "delete_silent")
local delete_silent = (saved_delete_silent == "true")


local strings = dofile(script_dir .. "lua/i18n.lua")

local T
local function _update_T()
  T = strings[lang] or strings["en"]
end
_update_T()

local function t(key)
  return (T and T[key]) or (strings["en"] and strings["en"][key]) or key
end
 -- "config", "analyzing", "completed", "error"
local LOG_MAX = 500
local _log_buf = {}
local _log_head = 1  -- próxima posição de escrita (circular)
local _log_count = 0

local function push_log(line)
  _log_buf[_log_head] = line
  _log_head = (_log_head % LOG_MAX) + 1
  if _log_count < LOG_MAX then _log_count = _log_count + 1 end
end

local function iter_logs()  -- iterador em ordem cronológica
  local tail = (_log_head - _log_count - 1 + LOG_MAX) % LOG_MAX + 1
  local i = 0
  return function()
    if i >= _log_count then return nil end
    local idx = (tail + i - 1) % LOG_MAX + 1
    i = i + 1
    return _log_buf[idx]
  end
end

local DEBUG = false
local parse_log_progress
local show_text_log = false
local cancel_path = ""

local function log(msg)
  local msg_str = tostring(msg)
  for line in string.gmatch(msg_str .. "\n", "(.-)\n") do
    push_log(line)
  end
  if parse_log_progress then
    parse_log_progress(msg_str)
  end
  if DEBUG then
    reaper.ShowConsoleMsg(msg_str .. "\n")
  end
end

local function file_exists(path)
  local f = io.open(path, "rb")
  if f then f:close() return true end
  return false
end

local function file_size(path)
  local f = io.open(path, "rb")
  if not f then return 0 end
  local size = f:seek("end")
  f:close()
  return size or 0
end


local function trim(s)
  if not s then return "" end
  return s:match("^%s*(.-)%s*$")
end

local function sanitize_shell_arg(s)
  if not s then return "" end
  -- Mantém apenas caracteres alfanuméricos, espaços, vírgulas, hifens, underlines e acentuação em português
  -- Removendo metacaracteres perigosos do terminal como &, |, ;, $, <, >, `, \, ", %, !, ^, etc.
  return s:gsub("[^%w%s%,%-_áàâãéèêíóòôõúùûçÁÀÂÃÉÈÊÍÓÒÔÕÚÙÛÇ]", "")
end

local function sanitize_numeric_input(value, max_val)
  local digits = tostring(value or ""):gsub("%D", "")
  if digits == "" then
    return ""
  end

  local threads = tonumber(digits) or 1
  if threads < 1 then threads = 1 end
  if threads > max_val then threads = max_val end
  return tostring(threads)
end


-- 0-based track index -> {track=MediaTrack, name=string, audio=nil ou {filename, start, dur}}
local track_info = {}

local function hex_to_rgb(hex)
  hex = hex:gsub("#", "")
  if #hex == 3 then
    hex = hex:sub(1,1)..hex:sub(1,1)..hex:sub(2,2)..hex:sub(2,2)..hex:sub(3,3)..hex:sub(3,3)
  end
  if #hex == 6 then
    local r = tonumber(hex:sub(1, 2), 16)
    local g = tonumber(hex:sub(3, 4), 16)
    local b = tonumber(hex:sub(5, 6), 16)
    return r, g, b
  end
  return nil
end

local themes = {
  default = {
    vocal_principal   = {224, 90, 71},    -- #E05A47
    backing_vocals    = {211, 139, 128}, -- #D38B80
    bateria           = {59, 110, 140},   -- #3B6E8C
    percussao         = {74, 159, 155},   -- #4A9F9B
    baixo             = {109, 85, 122},   -- #6D557A
    guitarra_eletrica = {74, 143, 98},  -- #4A8F62
    violao            = {127, 156, 98},   -- #7F9C62
    teclado           = {224, 146, 62},   -- #E0923E
    synth             = {220, 174, 59},   -- #DCAE3B
    cordas            = {163, 112, 76},   -- #A3704C
    sopros            = {166, 75, 117},   -- #A64B75
    efeitos           = {112, 128, 144},  -- #708090
    pastas            = {62, 62, 62},     -- #3E3E3E
    outro             = {150, 150, 150}
  },
  green = {
    vocal_principal   = {100, 180, 100},  -- Verde claro
    backing_vocals    = {140, 200, 140},  -- Verde pastel
    bateria           = {34, 76, 34},     -- Verde floresta escuro
    percussao         = {60, 120, 80},    -- Verde folha
    baixo             = {107, 142, 35},   -- Verde oliva/musgo
    guitarra_eletrica = {46, 139, 87},    -- Sea green
    violao            = {143, 188, 143},  -- Dark sea green pastel
    teclado           = {180, 210, 120},  -- Verde limão pastel
    synth             = {120, 200, 160},  -- Menta
    cordas            = {85, 107, 47},    -- Dark olive green
    sopros            = {154, 205, 50},   -- Yellow green
    efeitos           = {112, 140, 120},  -- Cinza esverdeado
    pastas            = {45, 60, 50},     -- Cinza floresta escuro
    outro             = {130, 150, 135}
  },
  purple = {
    vocal_principal   = {210, 100, 210},  -- Magenta claro
    backing_vocals    = {230, 160, 230},  -- Lilás pastel
    bateria           = {75, 0, 130},     -- Índigo
    percussao         = {138, 43, 226},   -- Blue violet
    baixo             = {48, 25, 52},     -- Roxo escuro
    guitarra_eletrica = {147, 112, 219},  -- Medium purple
    violao            = {186, 85, 211},   -- Medium orchid
    teclado           = {216, 191, 216},  -- Thistle
    synth             = {123, 104, 238},  -- Medium slate blue
    cordas            = {153, 50, 204},   -- Dark orchid
    sopros            = {218, 112, 214},  -- Orchid
    efeitos           = {120, 110, 140},  -- Cinza roxo
    pastas            = {50, 45, 60},     -- Roxo acinzentado escuro
    outro             = {140, 130, 150}
  },
  blue = {
    vocal_principal   = {0, 162, 232},    -- Cyan blue
    backing_vocals    = {153, 217, 234},  -- Light cyan
    bateria           = {15, 76, 129},    -- Deep blue
    percussao         = {74, 134, 232},   -- Bright blue
    baixo             = {25, 45, 115},    -- Navy
    guitarra_eletrica = {50, 100, 180},   -- Medium blue
    violao            = {120, 160, 220},  -- Soft blue
    teclado           = {100, 180, 220},  -- Ice blue
    synth             = {160, 210, 240},  -- Light sky blue
    cordas            = {90, 120, 160},   -- Steel blue
    sopros            = {140, 180, 230},  -- Soft periwinkle
    efeitos           = {120, 130, 160},  -- Slate blue
    pastas            = {50, 55, 70},     -- Dark blue-gray
    outro             = {130, 135, 150}
  },
  red = {
    vocal_principal   = {220, 20, 60},    -- Crimson
    backing_vocals    = {240, 128, 128},  -- Light coral
    bateria           = {139, 0, 0},      -- Dark red
    percussao         = {205, 92, 92},    -- Indian red
    baixo             = {80, 10, 10},     -- Maroon
    guitarra_eletrica = {180, 40, 40},    -- Rich red
    violao            = {210, 100, 100},  -- Soft red
    teclado           = {230, 140, 100},  -- Light salmon
    synth             = {255, 180, 150},  -- Peach
    cordas            = {160, 60, 60},    -- Brownish red
    sopros            = {200, 80, 120},   -- Rose
    efeitos           = {150, 120, 120},  -- Muted red-gray
    pastas            = {65, 45, 45},     -- Dark red-gray
    outro             = {140, 125, 125}
  },
  orange = {
    vocal_principal   = {255, 100, 0},    -- Orange
    backing_vocals    = {255, 160, 90},   -- Light orange
    bateria           = {130, 50, 0},     -- Rust
    percussao         = {180, 90, 30},    -- Burnt orange
    baixo             = {60, 25, 0},      -- Dark brown
    guitarra_eletrica = {220, 80, 10},    -- Dark orange
    violao            = {240, 130, 50},   -- Soft orange
    teclado           = {250, 180, 80},   -- Gold
    synth             = {255, 210, 120},  -- Apricot
    cordas            = {150, 85, 45},    -- Warm brown
    sopros            = {210, 120, 80},   -- Terra cotta
    efeitos           = {140, 125, 110},  -- Muted orange-gray
    pastas            = {60, 50, 45},     -- Dark warm gray
    outro             = {135, 125, 120}
  },
  yellow = {
    vocal_principal   = {230, 210, 0},    -- Gold
    backing_vocals    = {245, 235, 120},  -- Pale yellow
    bateria           = {130, 120, 20},   -- Olive gold
    percussao         = {180, 170, 40},   -- Brass
    baixo             = {70, 65, 10},     -- Dark olive
    guitarra_eletrica = {200, 180, 30},   -- Rich yellow
    violao            = {220, 210, 100},  -- Soft yellow
    teclado           = {240, 230, 150},  -- Straw
    synth             = {255, 245, 190},  -- Cream
    cordas            = {150, 140, 50},   -- Khaki
    sopros            = {210, 200, 110},  -- Sand
    efeitos           = {135, 135, 115},  -- Muted yellow-gray
    pastas            = {55, 55, 45},     -- Dark olive-gray
    outro             = {130, 130, 120}
  },
  vintage = {
    vocal_principal   = {204, 78, 62},    -- Terracota
    backing_vocals    = {220, 130, 110},  -- Areia avermelhada
    bateria           = {90, 115, 135},   -- Azul vintage
    percussao         = {140, 160, 160},  -- Cinza azulado
    baixo             = {100, 80, 70},    -- Chocolate marrom
    guitarra_eletrica = {115, 135, 100},  -- Verde oliva vintage
    violao            = {165, 150, 120},  -- Cáqui / Palha
    teclado           = {215, 160, 90},   -- Mostarda
    synth             = {190, 135, 110},  -- Pêssego queimado
    cordas            = {145, 105, 80},   -- Rust
    sopros            = {180, 110, 120},  -- Dusky rose
    efeitos           = {140, 140, 130},  -- Cinza quente
    pastas            = {75, 70, 65},     -- Marrom acinzentado escuro
    outro             = {130, 125, 120}
  }
}

local function write_theme_to_ini(theme_name)
  local t_colors = themes[theme_name]
  if not t_colors then return end
  local f = io.open(config_path, "w")
  if f then
    f:write("# Paleta de Cores " .. theme_name:upper() .. " para o AiNOMEATOR\n")
    f:write("[Cores]\n")
    local keys_order = {
      "vocal_principal", "backing_vocals", "bateria", "percussao", "baixo",
      "guitarra_eletrica", "violao", "teclado", "synth", "cordas", "sopros",
      "efeitos", "pastas", "outro"
    }
    for _, k in ipairs(keys_order) do
      local v = t_colors[k]
      if v then
        f:write(string.format("%s = #%02X%02X%02X\n", k, v[1], v[2], v[3]))
      end
    end
    f:close()
  end
end

local default_config = [[
# Configuracao de Cores para o AiNOMEATOR
# Formato: chave = #HEX ou chave = R,G,B
# Linhas iniciadas com '#' ou ';' sao comentarios.

[Cores]
# 1. Vocais (Familia dos Vermelhos/Rosas)
vocal_principal = #E05A47
backing_vocals = #D38B80

# 2. Bateria e Percussao (Familia dos Azuis)
bateria = #3B6E8C
percussao = #4A9F9B

# 3. Baixo (Familia dos Roxos/Marrons)
baixo = #6D557A

# 4. Guitarras e Violoes (Familia dos Verdes)
guitarra_eletrica = #4A8F62
violao = #7F9C62

# 5. Teclados, Pianos e Synths (Familia dos Laranjas/Amarelos)
teclado = #E0923E
synth = #DCAE3B

# 6. Cordas Orquestrais (Familia dos Dourados/Marrons Terrosos)
cordas = #A3704C

# 7. Sopros e Metais (Familia dos Magentas/Purpuras Claras)
sopros = #A64B75

# 8. Utilitarios (Familia dos Cinzas/Pretos)
efeitos = #708090
pastas = #3E3E3E

# Outros / Padrao se nao identificado
outro = #969696
]]

local function load_config(path)
  local colors = {}
  local defaults = {
    vocal_principal = {224, 90, 71},    -- #E05A47
    backing_vocals  = {211, 139, 128}, -- #D38B80
    bateria         = {59, 110, 140},   -- #3B6E8C
    percussao       = {74, 159, 155},   -- #4A9F9B
    baixo           = {109, 85, 122},   -- #6D557A
    guitarra_eletrica = {74, 143, 98},  -- #4A8F62
    violao          = {127, 156, 98},   -- #7F9C62
    teclado         = {224, 146, 62},   -- #E0923E
    synth           = {220, 174, 59},   -- #DCAE3B
    cordas          = {163, 112, 76},   -- #A3704C
    sopros          = {166, 75, 117},   -- #A64B75
    efeitos         = {112, 128, 144},  -- #708090
    pastas          = {62, 62, 62},     -- #3E3E3E
    outro           = {150, 150, 150}
  }

  local f = io.open(path, "r")
  if not f then
    -- Criar arquivo com os valores padrao
    f = io.open(path, "w")
    if f then
      f:write(default_config)
      f:close()
    end
    -- Retornar os padroes
    for k, v in pairs(defaults) do
      colors[k] = v
    end
    return colors, true
  end

  -- Ler arquivo
  for line in f:lines() do
    line = line:gsub("\r", "")
    -- Ignora se for uma linha inteira de comentario (comecando com # ou ;)
    if not line:match("^%s*#") and not line:match("^%s*;") then
      local key, val = line:match("^%s*([%w_]+)%s*=%s*(.*)$")
      if key and val then
        key = key:lower()
        val = trim(val)
        -- Remove comentario inline com ';' se houver (ex: val = "#88C999 ; comentario")
        val = val:gsub("%s*;.*", "")
        
        if DEBUG then
          log("   [INI] Parsed: " .. key .. " = " .. val)
        end
        if val:sub(1,1) == "#" then
          local r, g, b = hex_to_rgb(val)
          if r and g and b then
            colors[key] = {r, g, b}
          end
        else
          -- Tenta ler como R,G,B
          local r, g, b = val:match("^%s*(%d+)%s*,%s*(%d+)%s*,%s*(%d+)%s*$")
          if r and g and b then
            colors[key] = {tonumber(r), tonumber(g), tonumber(b)}
          end
        end
      end
    end
  end
  f:close()

  -- Preenche com defaults chaves que por ventura faltaram
  for k, v in pairs(defaults) do
    if not colors[k] then
      colors[k] = v
    end
  end

  return colors, false
end

local BACKING_VOCAL_KEYWORDS = {"backing", "dobra", "harmonia", "back", "bgv", "coral", "coro", "apoio", "segunda"}
local PERCUSSION_KEYWORDS = {"percussao", "percussão", "perc", "pandeiro", "shaker", "chocalho", "conga", "tumbadora", "bongo", "bongó", "triangulo", "triângulo", "tamborim", "agogo", "agogô", "caxixi", "djembe", "timbal", "reco", "guiro", "güiro", "maraca", "cowbell", "tambourine"}
local VIOLAO_KEYWORDS = {"violao", "violão", "acustico", "acústico", "acoustic", "nylon", "steel", "aco", "aço", "ukulele", "banjo", "bandolim", "mandolin", "harpa", "harp"}
local SYNTH_KEYWORDS = {"synth", "pad", "sintetizador"}
local EFEITOS_KEYWORDS = {"fx", "reverb", "delay", "rev", "dly", "aux", "send", "retorno", "return"}

local function matches_any(str, keywords)
  for _, kw in ipairs(keywords) do
    if str:find(kw, 1, true) then
      return true
    end
  end
  return false
end

local function get_color_key(category, instrument, track_name)
  category = (category or ""):lower()
  instrument = (instrument or ""):lower()
  track_name = (track_name or ""):lower()
  local search_str = instrument .. " " .. track_name

  -- 1. Vocais
  if category == "vocal" then
    if matches_any(search_str, BACKING_VOCAL_KEYWORDS) then
      return "backing_vocals"
    else
      return "vocal_principal"
    end
  end

  -- 2. Bateria e Percussao
  if category == "bateria" then
    if matches_any(search_str, PERCUSSION_KEYWORDS) then
      return "percussao"
    else
      return "bateria"
    end
  end

  -- 3. Baixo
  if category == "baixo" then
    return "baixo"
  end

  -- 4. Guitarras e Violoes
  if category == "guitarra" then
    if matches_any(search_str, VIOLAO_KEYWORDS) then
      return "violao"
    else
      return "guitarra_eletrica"
    end
  end

  -- 5. Teclados, Pianos e Synths
  if category == "teclado" then
    if matches_any(search_str, SYNTH_KEYWORDS) then
      return "synth"
    else
      return "teclado"
    end
  end

  if category == "synth" then
    return "synth"
  end

  -- 6. Cordas Orquestrais
  if category == "cordas" then
    return "cordas"
  end

  -- 7. Sopros e Metais
  if category == "sopro" or category == "sopros" then
    return "sopros"
  end

  -- 8. Utilitarios (Efeitos) - Mapeamento com base apenas no nome da track
  if matches_any(track_name, EFEITOS_KEYWORDS) then
    return "efeitos"
  end

  return "outro"
end


-- categoria -> palavras-chave pra procurar nos icones de track que ja vem
-- com o Reaper (Data/track_icons). Busca por substring, case-insensitive.
local ICON_KEYWORDS = {
  vocal    = {"vocal", "mic", "voice", "sing"},
  guitarra = {"guitar"},
  baixo    = {"bass"},
  bateria  = {"drum", "perc"},
  teclado  = {"piano", "key"},
  synth    = {"synth"},
  sopro    = {"horn", "brass", "sax", "trumpet", "flute", "wind", "clarinet"},
  cordas   = {"string", "violin", "cello", "viola"},
}

local function split_tab(line)
  local fields = {}
  local start = 1
  while true do
    local tab_pos = line:find("\t", start, true)
    if not tab_pos then
      table.insert(fields, line:sub(start))
      break
    end
    table.insert(fields, line:sub(start, tab_pos - 1))
    start = tab_pos + 1
  end
  return fields
end

local function capitalize(s)
  if not s or s == "" then return s end
  return s:sub(1, 1):upper() .. s:sub(2)
end

local function list_icon_files(sep)
  local dir = reaper.GetResourcePath() .. sep .. "Data" .. sep .. "track_icons"
  local files = {}
  local i = 0
  while true do
    local fn = reaper.EnumerateFiles(dir, i)
    if not fn then break end
    table.insert(files, {name = fn, full = dir .. sep .. fn})
    i = i + 1
  end
  return files
end

local function find_icon(icon_files, category, instrument)
  if category == "bateria" then
    local inst_lower = (instrument or ""):lower()
    local is_digital = false
    local digi_kws = {"machine", "eletroni", "digital", "synth", "sampler", "box", "midi", "eletrônica"}
    for _, kw in ipairs(digi_kws) do
      if inst_lower:find(kw, 1, true) then
        is_digital = true
        break
      end
    end

    if is_digital then
      -- Busca preferencialmente por drumbox ou machine
      for _, f in ipairs(icon_files) do
        local name = f.name:lower()
        if name:find("drumbox", 1, true) or name:find("machine", 1, true) then
          return f.full
        end
      end
    else
      -- Bateria acústica: busca drums (sem conter drumbox)
      for _, f in ipairs(icon_files) do
        local name = f.name:lower()
        if name:find("drums", 1, true) and not name:find("drumbox", 1, true) then
          return f.full
        end
      end
      -- Outros matches com "drum" sem conter "drumbox"
      for _, f in ipairs(icon_files) do
        local name = f.name:lower()
        if name:find("drum", 1, true) and not name:find("drumbox", 1, true) then
          return f.full
        end
      end
    end
  end
  local keywords = ICON_KEYWORDS[category]
  if not keywords then return nil end
  for _, kw in ipairs(keywords) do
    for _, f in ipairs(icon_files) do
      if f.name:lower():find(kw, 1, true) then
        return f.full
      end
    end
  end
  return nil
end

local function get_track_earliest_sound(tr, threshold_db)
  threshold_db = threshold_db or -50
  local threshold_amp = 10 ^ (threshold_db / 20)
  
  local earliest_sound = 9000000 -- MAX_TIME
  local num_items = reaper.CountTrackMediaItems(tr)
  if num_items == 0 then
    return earliest_sound
  end
  
  for j = 0, num_items - 1 do
    local item = reaper.GetTrackMediaItem(tr, j)
    local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local item_len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    local take = reaper.GetActiveTake(item)
    
    if take and not reaper.TakeIsMIDI(take) then
      local accessor = reaper.CreateTakeAudioAccessor(take)
      if accessor then
        local samplerate = 1000
        local channels = 1
        local chunk_sec = 2.0
        local chunk_samples = math.ceil(chunk_sec * samplerate)
        local buffer = reaper.new_array(chunk_samples)
        
        local found = false
        -- starttime_sec is relative to the take source, which usually aligns with D_STARTOFFS
        local start_offs = reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")
        
        for pos_sec = 0, item_len, chunk_sec do
          local fetch_start = start_offs + pos_sec
          local samples_to_read = chunk_samples
          if pos_sec + chunk_sec > item_len then
            samples_to_read = math.ceil((item_len - pos_sec) * samplerate)
          end
          
          if samples_to_read > 0 then
            local ok = reaper.GetAudioAccessorSamples(accessor, samplerate, channels, fetch_start, samples_to_read, buffer)
            if ok == 1 then
              local b_table = buffer.table()
              for i = 1, samples_to_read do
                if math.abs(b_table[i] or 0) >= threshold_amp then
                  local sound_time = item_pos + pos_sec + ((i - 1) / samplerate)
                  if sound_time < earliest_sound then
                    earliest_sound = sound_time
                  end
                  found = true
                  break
                end
              end
            end
          end
          if found then break end
        end
        reaper.DestroyAudioAccessor(accessor)
      end
    elseif take and reaper.TakeIsMIDI(take) then
      local earliest_midi = 9000000
      local _, num_events = reaper.MIDI_CountEvts(take)
      for e = 0, num_events - 1 do
        local retval, selected, muted, startppqpos, chanmsg, chan, pitch, vel = reaper.MIDI_GetNote(take, e)
        if retval and not muted then
          local proj_time = reaper.MIDI_GetProjTimeFromPPQPos(take, startppqpos)
          if proj_time < earliest_midi then
            earliest_midi = proj_time
          end
        end
      end
      if earliest_midi < earliest_sound then
        earliest_sound = earliest_midi
      end
    else
      if item_pos < earliest_sound then
        earliest_sound = item_pos
      end
    end
  end
  
  return earliest_sound
end

local function sort_project_tracks(track_categories, track_instruments)
  local total_tracks = reaper.CountTracks(0)
  if total_tracks <= 1 then return end
 
  local MAX_TIME = 9000000
  local track_sound_starts = {}

  -- 1. Encontrar o tempo de entrada real de cada track
  for i = 0, total_tracks - 1 do
    local tr = reaper.GetTrack(0, i)
    track_sound_starts[tr] = get_track_earliest_sound(tr, -50)
  end

  -- 2. Encontrar o tempo de entrada mais cedo (earliest_time) de cada categoria
  local cat_earliest = {}
  for i = 0, total_tracks - 1 do
    local tr = reaper.GetTrack(0, i)
    local category = track_categories[tr] or "outro"
    local is_folder = reaper.GetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH") == 1
    if is_folder then
      category = "pastas"
    else
      local _, tr_name = reaper.GetTrackName(tr)
      local key_by_name = get_color_key("", "", tr_name or "")
      if key_by_name == "efeitos" then
        category = "efeitos"
      end
    end

    local track_earliest = track_sound_starts[tr]
    if not cat_earliest[category] or track_earliest < cat_earliest[category] then
      cat_earliest[category] = track_earliest
    end
  end

  -- build the list of track objects to sort
  local list = {}
  for i = 0, total_tracks - 1 do
    local tr = reaper.GetTrack(0, i)
    local guid = reaper.GetTrackGUID(tr)
    
    local category = track_categories[tr] or "outro"
    local instrument = track_instruments[tr] or ""
    local _, tr_name = reaper.GetTrackName(tr)
    tr_name = tr_name or ""

    -- Check if it is a folder or utility track to adjust categories
    local is_folder = reaper.GetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH") == 1
    if is_folder then
      category = "pastas"
    else
      -- check if it is effects
      local key_by_name = get_color_key("", "", tr_name)
      if key_by_name == "efeitos" then
        category = "efeitos"
      end
    end

    -- Determine category weight dynamically based on when it enters the project
    local base_weight = cat_earliest[category] or MAX_TIME
    
    -- Force specific categories to the bottom
    local is_vocal = category:find("vocal") ~= nil or category == "voz"
    if is_vocal then
      base_weight = base_weight + 40000000
    elseif category == "outro" then
      base_weight = base_weight + 30000000
    elseif category == "efeitos" then
      base_weight = base_weight + 20000000
    elseif category == "pastas" then
      base_weight = base_weight + 10000000
    end

    table.insert(list, {
      track = tr,
      guid = guid,
      weight = base_weight,
      track_earliest = track_sound_starts[tr],
      category = category,
      instrument = instrument:lower(),
      name = tr_name:lower(),
      orig_idx = i
    })
  end

  -- Sort the list
  table.sort(list, function(a, b)
    if a.weight ~= b.weight then
      return a.weight < b.weight
    end
    -- same category weight, sort by individual track start time
    if a.track_earliest ~= b.track_earliest then
      return a.track_earliest < b.track_earliest
    end
    -- within same category and start time, group core guitars/violões together
    if a.category == b.category and (a.category == "guitarra" or a.category == "violao" or a.category == "violão") then
      local a_is_core = (a.instrument:find("guitar", 1, true) or a.instrument:find("violao", 1, true) or a.instrument:find("violão", 1, true) or a.name:find("guitar", 1, true) or a.name:find("violao", 1, true) or a.name:find("violão", 1, true)) and 1 or 2
      local b_is_core = (b.instrument:find("guitar", 1, true) or b.instrument:find("violao", 1, true) or b.instrument:find("violão", 1, true) or b.name:find("guitar", 1, true) or b.name:find("violao", 1, true) or b.name:find("violão", 1, true)) and 1 or 2
      if a_is_core ~= b_is_core then
        return a_is_core < b_is_core
      end
    end
    -- same category and sub-group, sort by instrument name
    if a.instrument ~= b.instrument then
      return a.instrument < b.instrument
    end
    -- same instrument, sort by track name
    if a.name ~= b.name then
      return a.name < b.name
    end
    -- keep stable using original index
    return a.orig_idx < b.orig_idx
  end)

  -- Now apply the new order in Reaper
  -- First, store selection states
  local sel_states = {}
  for i = 0, total_tracks - 1 do
    local tr = reaper.GetTrack(0, i)
    sel_states[tr] = reaper.IsTrackSelected(tr)
  end

  -- Clear folder depths for sorted tracks to prevent nested folder brackets
  for i = 1, #list do
    reaper.SetMediaTrackInfo_Value(list[i].track, "I_FOLDERDEPTH", 0)
  end

  -- Reorder tracks
  for i = 1, #list do
    local tr = list[i].track
    -- Unselect all
    for j = 0, total_tracks - 1 do
      local t_j = reaper.GetTrack(0, j)
      reaper.SetTrackSelected(t_j, false)
    end
    -- Select current track to move
    reaper.SetTrackSelected(tr, true)
    -- Move it before index i - 1
    reaper.ReorderSelectedTracks(i - 1, 0)
  end

  -- Restore selections
  for j = 0, total_tracks - 1 do
    local t_j = reaper.GetTrack(0, j)
    reaper.SetTrackSelected(t_j, sel_states[t_j] or false)
  end
end

------------------------------------------------------------------
-- 1) opcoes do usuario
------------------------------------------------------------------

local config_colors, created_new = load_config(config_path)

local only_selected = false
local segment_seconds = 8
local workers = 5
local color_prompt = ""
local script_running = true
local inputs


local function start_analysis()
  track_info = {} -- Reseta o estado para evitar acúmulo de dados entre execuções
  if current_theme ~= "custom" then
    write_theme_to_ini(current_theme)
  end
  local config_colors_file = config_path
  if current_theme == "custom" and color_prompt ~= "" then
    config_colors_file = script_dir .. "colors_prompt.ini"
  end

  local track_count = reaper.CountTracks(0)
  local manifest_lines = {}
  local skipped_no_audio = 0

  for i = 0, track_count - 1 do
    local track = reaper.GetTrack(0, i)
    local include = (not only_selected) or reaper.IsTrackSelected(track)

    if include then
      local _, name = reaper.GetTrackName(track)
      local best = nil -- {filename, start, dur}
      local n_items = reaper.CountTrackMediaItems(track)

      for j = 0, n_items - 1 do
        local item = reaper.GetTrackMediaItem(track, j)
        local take = reaper.GetActiveTake(item)

        if take and not reaper.TakeIsMIDI(take) then
          local source = reaper.GetMediaItemTake_Source(take)
          local filename = reaper.GetMediaSourceFileName(source, "")

          if filename and filename ~= "" then
            local item_len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
            local playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE")
            if not playrate or playrate <= 0 then playrate = 1.0 end
            local used_dur = item_len * playrate

            if (not best) or used_dur > best.dur then
              local start_offs = reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")
              best = {filename = filename, start = start_offs, dur = used_dur}
            end
          end
        end
      end

      track_info[i] = {
        track = track,
        name = name,
        audio = best,
        status = best and "pending" or "skipped",
        color = {77, 77, 77},
        category = "",
        instrument = ""
      }

      if best then
        table.insert(manifest_lines,
          string.format("%d\t%s\t%.3f\t%.3f", i, best.filename, best.start, best.dur))
      else
        skipped_no_audio = skipped_no_audio + 1
      end
    end
  end

  local n_jobs = #manifest_lines

  if n_jobs == 0 then
    reaper.MB(t("msg_no_audio"), "AiNOMEATOR", 0)
    return
  end



  local venv_exists = false
  local python_exe
  if is_windows then
    python_exe = script_dir .. "venv\\Scripts\\python.exe"
    if file_exists(python_exe) then
      venv_exists = true
    else
      python_exe = "python"
    end
  else
    python_exe = script_dir .. "venv/bin/python"
    if file_exists(python_exe) then
      venv_exists = true
    else
      python_exe = "python3"
    end
  end

  local batch_script = script_dir .. "src" .. sep .. "batch_rename.py"

  local work_dir = reaper.GetResourcePath() .. sep .. "reaper-ai-namer_tmp"
  reaper.RecursiveCreateDirectory(work_dir, 0)

  local stamp = tostring(math.floor(reaper.time_precise() * 1000))
  local manifest_path = work_dir .. sep .. "manifest_" .. stamp .. ".tsv"
  local result_path   = work_dir .. sep .. "result_"   .. stamp .. ".tsv"
  local log_path      = work_dir .. sep .. "log_"      .. stamp .. ".txt"
  local done_path     = work_dir .. sep .. "done_"     .. stamp .. ".flag"
  cancel_path         = work_dir .. sep .. "cancel_"   .. stamp .. ".flag"

  local mf, err = io.open(manifest_path, "w")
  if not mf then
    log("[ERRO] Nao foi possivel criar o arquivo manifest: " .. tostring(err))
    reaper.MB("Erro ao criar manifest: " .. tostring(err), "AiNOMEATOR", 0)
    return
  end
  for _, line in ipairs(manifest_lines) do
    mf:write(line .. "\n")
  end
  mf:close()

  local analysis_start_time = reaper.time_precise()
  local function log_t(msg)
    local elapsed = reaper.time_precise() - analysis_start_time
    log(string.format("%s [%.2fs]", msg, elapsed))
  end

  if DEBUG then
    reaper.ShowConsoleMsg("")  -- garante que o console abre
  end
  log("╭──────────────────────────────────────────────────────────╮")
  log("│ ainomeator by jasko                                                        │")
  log("╰──────────────────────────────────────────────────────────╯")
  log("")
  log_t("[ setup ]")
  if not venv_exists then
    log(t("msg_venv_missing"))
    log(t("msg_venv_hint"))
    log("")
  end
  local profile_name = "colors.ini"
  if current_theme == "custom" and color_prompt ~= "" then
    profile_name = "colors_prompt.ini"
  end
  log_t("› profile  : " .. profile_name)
  local backend_name = backend
  if backend == "panns" then
    backend_name = "panns (local/cnn14)"
  elseif backend == "gemini" then
    backend_name = "gemini (cloud/api)"
  elseif backend == "hybrid_heuristic" then
    backend_name = "hybrid_heuristic (PANNs + Gemini)"
  elseif backend == "hybrid_chaining" then
    backend_name = "hybrid_chaining (PANNs + Gemini review)"
  end
  log_t("› backend  : " .. backend_name)
  local device_str = "cpu (checkpoint loaded)"
  if backend == "gemini" then
    device_str = "cloud api"
  end
  log_t("› device   : " .. device_str)
  local target_str = string.format("%d tracks | %d thread | %s mode", n_jobs, workers, (analysis_mode == "detailed" and "detailed" or "fast"))
  log_t("› target   : " .. target_str)

  local local_threads = tonumber(inputs[3].val) or 1
  
  -- Calcula o tempo gasto ate o momento do disparo do script Python
  local setup_offset = reaper.time_precise() - analysis_start_time
  local python_args = string.format(
    '-u "%s" "%s" "%s" --workers %d --segment-seconds %.2f --done-flag "%s" --config-path "%s" --panns-threads %d --start-offset %.4f',
    batch_script, manifest_path, result_path, workers, segment_seconds, done_path, config_colors_file, local_threads, setup_offset
  )

  if analysis_mode == "detailed" then
    python_args = python_args .. ' --quality alta'
  else
    python_args = python_args .. ' --quality normal'
  end

  python_args = python_args .. ' --output-language ' .. sanitize_shell_arg(lang)
  python_args = python_args .. ' --backend ' .. sanitize_shell_arg(backend)

  if current_theme == "custom" and color_prompt ~= "" then
    local escaped_prompt = sanitize_shell_arg(color_prompt)
    python_args = python_args .. string.format(' --color-prompt "%s"', escaped_prompt)
  end

  local launch_cmd
  if is_windows then
    launch_cmd = string.format(
      'start "" /B cmd /c ""%s" %s > "%s" 2>&1"',
      python_exe, python_args, log_path
    )
  else
    launch_cmd = string.format(
      '"%s" %s > "%s" 2>&1 &',
      python_exe, python_args, log_path
    )
  end

  if backend == "panns" or backend == "hybrid_heuristic" or backend == "hybrid_chaining" then
    if lang == "pt" then
      log_t("  [!] Inicializando modelo PANNs (~300MB)...")
      log_t("  [!] Pode demorar de 10 a 30 segundos na primeira execução...")
    else
      log_t("  [!] Initializing PANNs model (~300MB)...")
      log_t("  [!] This might take 10 to 30 seconds on the first run...")
    end
  end

  os.execute(launch_cmd)

  local log_read_pos = 0    -- byte offset ate onde ja lemos o log
  local poll_start   = reaper.time_precise()
  local TIMEOUT_SEC  = 300  -- 5 minutos: timeout de seguranca

  local apply_results
  local poll

  apply_results = function()
    local function utf8_len(s)
      if not s then return 0 end
      if utf8 and utf8.len then
        local len = utf8.len(s)
        if len then return len end
      end
      local _, count = s:gsub("[\xc2-\xf4][\x80-\xbf]*", "")
      return s:len() - count
    end

    local function format_ranges(indices)
      if #indices == 0 then return "" end
      table.sort(indices)
      local ranges = {}
      local start = indices[1]
      local prev = indices[1]
      
      local function add_range(s, e)
        if s == e then
          table.insert(ranges, string.format("track %02d", s))
        else
          table.insert(ranges, string.format("tracks %02d-%02d", s, e))
        end
      end
      
      for i = 2, #indices do
        if indices[i] == prev + 1 then
          prev = indices[i]
        else
          add_range(start, prev)
          start = indices[i]
          prev = indices[i]
        end
      end
      add_range(start, prev)
      return table.concat(ranges, ", ")
    end

    if not file_exists(result_path) then
      log("\n[ERROR] batch_rename.py did not generate the result file.")
      log("Possible causes:")
      log("  - setup.bat has not been run in this folder")
      log("  - GEMINI_API_KEY is not configured in .env")
      log("  - Python was not found in PATH")
      reaper.MB("Failed to run AI. See the Reaper console for details.", "AiNOMEATOR", 0)
      return
    end

    config_colors = load_config(config_colors_file)
    local icon_files = list_icon_files(sep)

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    local applied, failed = 0, 0
    local processed_tracks = {}
    local track_categories = {}
    local track_instruments = {}
    local color_groups = {}
    local color_rgb = {}
    local tracks_to_delete = {}

    for line in io.lines(result_path) do
      if line ~= "" then
        local f = split_tab(line)
        local idx        = tonumber(f[1])
        local status     = f[2]
        local category   = f[3]
        local instrument = f[4]
        local confidence = f[5]
        local errmsg     = f[6]

        local info = idx and track_info[idx]
        if info then
          if status == "silence" then
            info.status = "silence"
            processed_tracks[idx] = true
            if delete_silent then
              table.insert(tracks_to_delete, info.track)
            end
          elseif status == "ok" and instrument and instrument ~= "" then
            local newname = capitalize(instrument)
            reaper.GetSetMediaTrackInfo_String(info.track, "P_NAME", newname, true)

            -- Salvar categoria e instrumento para ordenação
            track_categories[info.track] = category
            track_instruments[info.track] = instrument

            local col_key = get_color_key(category, instrument, newname)
            local is_folder = reaper.GetMediaTrackInfo_Value(info.track, "I_FOLDERDEPTH") == 1
            if is_folder then
              col_key = "pastas"
            end

            local col = config_colors[col_key] or config_colors["outro"]
            reaper.SetTrackColor(info.track, reaper.ColorToNative(col[1], col[2], col[3]) | 0x1000000)

            local icon_path = find_icon(icon_files, category, instrument)
            if icon_path then
              reaper.GetSetMediaTrackInfo_String(info.track, "P_ICON", icon_path, true)
            end

            color_groups[col_key] = color_groups[col_key] or {}
            table.insert(color_groups[col_key], idx)
            color_rgb[col_key] = col

            processed_tracks[idx] = true
            applied = applied + 1
          else
            failed = failed + 1
          end
        end
      end
    end

    for idx, info in pairs(track_info) do
      if not processed_tracks[idx] then
        local track_name = info.name
        local is_folder = reaper.GetMediaTrackInfo_Value(info.track, "I_FOLDERDEPTH") == 1
        local col_key = "outro"
        local category = "outro"

        if is_folder then
          col_key = "pastas"
          category = "pastas"
        else
          local key_by_name = get_color_key("", "", track_name)
          if key_by_name == "efeitos" then
            col_key = "efeitos"
            category = "efeitos"
          end
        end

        -- Salvar para ordenação
        track_categories[info.track] = category
        track_instruments[info.track] = track_name

        local col = config_colors[col_key] or config_colors["outro"]
        reaper.SetTrackColor(info.track, reaper.ColorToNative(col[1], col[2], col[3]) | 0x1000000)

        color_groups[col_key] = color_groups[col_key] or {}
        table.insert(color_groups[col_key], idx)
        color_rgb[col_key] = col
      end
    end

    log("\n[ reaper integration ]")
    log("› applying metadata & rgb color profiles...")
    local cat_names = {
      vocal_principal = { en = "lead_vocals", pt = "vocal_principal" },
      backing_vocals  = { en = "backing_vocals", pt = "backing_vocals" },
      bateria         = { en = "drums", pt = "bateria" },
      percussao       = { en = "percussion", pt = "percussão" },
      baixo           = { en = "bass", pt = "baixo" },
      guitarra_eletrica = { en = "electric_guitar", pt = "guitarra" },
      violao          = { en = "acoustic_guitar", pt = "violão" },
      teclado         = { en = "keyboard", pt = "teclado" },
      synth           = { en = "synth", pt = "sintetizador" },
      cordas          = { en = "strings", pt = "cordas" },
      sopros          = { en = "brass_winds", pt = "sopros" },
      efeitos         = { en = "sfx", pt = "efeitos" },
      pastas          = { en = "folders", pt = "pastas" },
      outro           = { en = "other", pt = "outro" }
    }

    local keys_order = {
      "vocal_principal", "backing_vocals", "bateria", "percussao", "baixo",
      "guitarra_eletrica", "violao", "teclado", "synth", "cordas", "sopros",
      "efeitos", "pastas", "outro"
    }
    for _, col_key in ipairs(keys_order) do
      local indices = color_groups[col_key]
      if indices and #indices > 0 then
        local rgb = color_rgb[col_key]
        local trk_str = format_ranges(indices)
        local disp_name = cat_names[col_key] and cat_names[col_key][lang] or col_key
        log(string.format("  + %-18s [rgb: %03d,%03d,%03d] : %s", disp_name, rgb[1], rgb[2], rgb[3], trk_str))
      end
    end

    if sort_tracks then
      log("› reordering tracks by instrument family...")
      sort_project_tracks(track_categories, track_instruments)
    end

    if create_folders then
      log("› grouping tracks into instrument folders...")
      
      -- Conta quantas tracks existem em cada categoria
      local cat_counts = {}
      for tr, cat in pairs(track_categories) do
        if cat and cat ~= "" then
          cat_counts[cat] = (cat_counts[cat] or 0) + 1
        end
      end

      local i = 0
      local current_folder = ""
      local in_folder = false
      
      while i < reaper.CountTracks(0) do
        local tr = reaper.GetTrack(0, i)
        local cat = track_categories[tr]
        
        if cat and cat ~= "" and cat ~= current_folder and cat ~= "pastas" and cat ~= "efeitos" and cat ~= "outro" then
          
          -- Fecha a pasta anterior se estavamos dentro de uma
          if in_folder and i > 0 then
            local last_tr = reaper.GetTrack(0, i - 1)
            local current_depth = reaper.GetMediaTrackInfo_Value(last_tr, "I_FOLDERDEPTH")
            reaper.SetMediaTrackInfo_Value(last_tr, "I_FOLDERDEPTH", current_depth - 1)
            in_folder = false
          end
          
          current_folder = cat
          
          -- Cria nova pasta apenas se houver 2 ou mais tracks
          if cat_counts[cat] and cat_counts[cat] >= 2 then
            reaper.InsertTrackAtIndex(i, true)
            local folder_tr = reaper.GetTrack(0, i)
            local disp_name = cat_names[cat] and cat_names[cat][lang] or cat
            local folder_name = disp_name:upper()
            reaper.GetSetMediaTrackInfo_String(folder_tr, "P_NAME", folder_name, true)
            reaper.SetMediaTrackInfo_Value(folder_tr, "I_FOLDERDEPTH", 1)
            
            local col = config_colors["pastas"]
            if col then
              reaper.SetTrackColor(folder_tr, reaper.ColorToNative(col[1], col[2], col[3]) | 0x1000000)
            end
            
            in_folder = true
          end
        end
        i = i + 1
      end
      
      -- Fecha a ultima pasta se ainda estiver aberta no final do projeto
      if in_folder then
        local final_total = reaper.CountTracks(0)
        if final_total > 0 then
          local last_tr = reaper.GetTrack(0, final_total - 1)
          local current_depth = reaper.GetMediaTrackInfo_Value(last_tr, "I_FOLDERDEPTH")
          reaper.SetMediaTrackInfo_Value(last_tr, "I_FOLDERDEPTH", current_depth - 1)
        end
      end
    end

    if delete_silent and #tracks_to_delete > 0 then
      log(string.format("› deleting %d silent tracks...", #tracks_to_delete))
      for _, tr in ipairs(tracks_to_delete) do
        reaper.DeleteTrack(tr)
      end
    end

    reaper.PreventUIRefresh(-1)
    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.Undo_EndBlock("IA: renomear/colorir tracks (AiNOMEATOR)", -1)

    log("")
    log("╭──────────────────────────────────────────────────────────╮")
    local success_str = string.format("✔ success      : %d applied ", applied)
    local error_str = string.format("%d errors", failed)
    local left_pad = 28 - utf8_len(success_str)
    if left_pad > 0 then
      success_str = success_str .. string.rep(" ", left_pad)
    end
    local line1 = success_str .. "│ " .. error_str
    local line1_pad = 66 - utf8_len(line1)
    if line1_pad > 0 then
      line1 = line1 .. string.rep(" ", line1_pad)
    end
    log("│ " .. line1 .. " │")

    local disp_path = result_path:gsub("^[Cc]:[/\\]Users[/\\][^/\\]+[/\\]AppData[/\\]Roaming[/\\]", "~\\appdata\\roaming\\")
    disp_path = disp_path:gsub("/", "\\")
    local line2 = string.format("📄 export log  : %s", disp_path)
    local line2_pad = 66 - utf8_len(line2)
    if line2_pad > 0 then
      line2 = line2 .. string.rep(" ", line2_pad)
    else
      local label = "📄 export log  : "
      local path_space = 66 - utf8_len(label)
      line2 = label .. "..." .. string.sub(disp_path, -path_space + 3)
    end
    log("│ " .. line2 .. " │")
    log("╰──────────────────────────────────────────────────────────╯")


  end

  local _last_poll_time = 0
  poll = function()
    if not script_running then
      return
    end

    local now = reaper.time_precise()
    if now - _last_poll_time >= 0.25 then
      _last_poll_time = now

      local current_size = file_size(log_path)
      if current_size > log_read_pos then
        local lf = io.open(log_path, "rb")
        if lf then
          lf:seek("set", log_read_pos)
          local new_content = lf:read("*a")
          lf:close()
          log_read_pos = current_size
          if new_content and new_content ~= "" then
            new_content = new_content:gsub("\r\n", "\n"):gsub("\r", "\n")
            log(new_content)
          end
        end
      end

      if file_exists(done_path) then
        local lf = io.open(log_path, "rb")
        if lf then
          lf:seek("set", log_read_pos)
          local tail = lf:read("*a")
          lf:close()
          if tail and tail ~= "" then
            log(tail:gsub("\r\n", "\n"):gsub("\r", "\n"))
          end
        end
        apply_results()
        gui_state = "completed"
        return
      end

      if reaper.time_precise() - poll_start > TIMEOUT_SEC then
        local err_msg = string.format("\n[TIMEOUT] Processo nao concluiu em %ds. Verifique o log em:\n  %s",
          TIMEOUT_SEC, log_path)
        log(err_msg)
        gui_state = "error"
        reaper.MB(
          string.format("Timeout: o processo demorou mais de %d segundos.\n\nVerifique o log completo em:\n%s",
            TIMEOUT_SEC, log_path),
          "AiNOMEATOR", 0)
        return
      end
    end

    reaper.defer(poll)
  end

  reaper.defer(poll)
end

-- ==================================================================
-- INTERFACE GRAFICA PERSONALIZADA (GFX)
-- ==================================================================


local function in_rect(x, y, w, h)
  return gfx.mouse_x >= x and gfx.mouse_x <= x + w and gfx.mouse_y >= y and gfx.mouse_y <= y + h
end


local layout = {
  -- Idiomas
  lang_en = { x = 241, y = 12, w = 32, h = 16, radio_x = 246, radio_y = 20, radio_r = 3 },
  lang_pt = { x = 276, y = 12, w = 36, h = 16, radio_x = 282, radio_y = 20, radio_r = 3 },
  
  -- Toggle de Opções Avançadas (Substitui o título geral)
  advanced_toggle = { x = 30, y = 100, w = 260, h = 24 },
  
  -- Checkboxes (deslocados para baixo do título)
  sort_tracks = { x = 30, y = 160, w = 125, h = 20, cb_x = 30, cb_y = 165, cb_size = 18 },
  only_selected = { x = 165, y = 160, w = 125, h = 20, cb_x = 165, cb_y = 165, cb_size = 18 },
  
  create_folders = { x = 30, y = 185, w = 125, h = 20, cb_x = 30, cb_y = 190, cb_size = 18 },
  delete_silent = { x = 165, y = 185, w = 125, h = 20, cb_x = 165, cb_y = 190, cb_size = 18 },
  
  -- Rádios do modo de análise (lado a lado, deslocados)
  mode_detailed = { x = 30, y = 240, w = 125, h = 20, cb_x = 30, cb_y = 245, cb_size = 18 },
  mode_fast = { x = 165, y = 240, w = 125, h = 20, cb_x = 165, cb_y = 245, cb_size = 18 },
  
  -- Rádios do backend de análise (deslocados)
  backend_label_y = 295,
  backend_start_y = 317,
  backend_spacing_y = 25,
  backend_cb_x = 30,
  backend_cb_size = 18,
  
  -- Theme Selector (deslocado)
  theme_selector = { x = 30, y = 515, w = 260, h = 30 },
  
  -- Outros Botões (deslocados)
  toggle_view = { x = 160, y = 105, w = 55, h = 24 },
  copy_logs = { x = 220, y = 105, w = 70, h = 24 },
  analyze = { x = 30, y = 670, w = 260, h = 36 },
  close = { x = 30, y = 645, w = 260, h = 36 }
}

-- Layout do modo compacto (painel avançado fechado): logo -> botão Analisar -> toggle -> aviso -> créditos
local COMPACT_BTN_Y = 160
local COMPACT_BTN_H = 46
local COMPACT_TOGGLE_Y = COMPACT_BTN_Y + COMPACT_BTN_H + 16
local COMPACT_NOTE_Y1 = COMPACT_TOGGLE_Y + 24 + 12
local COMPACT_NOTE_Y2 = COMPACT_NOTE_Y1 + 11
local COMPACT_CREDITS_Y = COMPACT_NOTE_Y2 + 30
local COMPACT_WINDOW_H = 340

local backend_options = {
  { key = "panns",              label_key = "backend_panns" },
  { key = "hybrid_chaining",    label_key = "backend_hybrid_chaining" },
  { key = "gemini",             label_key = "backend_gemini" },
  { key = "hybrid_heuristic",   label_key = "backend_hybrid_heuristic" },
}

local theme_options = {
  { key = "default",            label_key = "theme_default" },
  { key = "green",              label_key = "theme_green" },
  { key = "purple",             label_key = "theme_purple" },
  { key = "blue",               label_key = "theme_blue" },
  { key = "red",                label_key = "theme_red" },
  { key = "orange",             label_key = "theme_orange" },
  { key = "yellow",             label_key = "theme_yellow" },
  { key = "vintage",            label_key = "theme_vintage" },
  { key = "custom",             label_key = "theme_custom" }
}

local function open_url(url)
  local os_name = reaper.GetOS()
  if is_windows then
    os.execute(string.format('start "" "%s"', url))
  elseif os_name:find("OSX") or os_name:find("mac") then
    os.execute(string.format('open "%s"', url))
  else
    os.execute(string.format('xdg-open "%s"', url))
  end
end

local logo_loaded = false
local logo_w, logo_h = 0, 0
local logo_buffer = 1

local logo_big_loaded = false
local logo_big_w, logo_big_h = 0, 0
local logo_big_buffer = 2

local function load_logo()
  local logo_path = script_dir .. "src" .. sep .. "ainomeator_logo.png"
  local res = gfx.loadimg(logo_buffer, logo_path)
  if res >= 0 then
    logo_w, logo_h = gfx.getimgdim(logo_buffer)
    logo_loaded = true
  end

  local logo_big_path = script_dir .. "src" .. sep .. "ainometor_biglogo.png"
  local res_big = gfx.loadimg(logo_big_buffer, logo_big_path)
  if res_big >= 0 then
    logo_big_w, logo_big_h = gfx.getimgdim(logo_big_buffer)
    logo_big_loaded = true
  end
end

sort_tracks = (saved_sort_tracks == "true")
analysis_mode = "detailed"
inputs = {
  { label = t("thread_label"), val = saved_panns_workers, placeholder = "1-20", is_numeric = true, limit = 2, max_val = 20, x = -1000, y = -1000, w = 1, h = 1 },
  { label = t("prompt_label"), val = "", placeholder = t("prompt_placeholder"), is_numeric = false, limit = 100, x = 30, y = 595, w = 260, h = 30 },
  { label = t("local_thread_label"), val = saved_panns_threads, placeholder = "1-16", is_numeric = true, limit = 2, max_val = 16, x = 30, y = 445, w = 260, h = 30 }
}

local function refresh_language_labels()
  inputs[1].label = t("thread_label")
  inputs[2].label = t("prompt_label")
  inputs[2].placeholder = t("prompt_placeholder")
  inputs[3].label = t("local_thread_label")
end

local function set_language(new_lang)
  if lang == new_lang then
    return false
  end

  lang = new_lang
  _update_T()
  reaper.SetExtState("AiNOMEATOR", "language", new_lang, true)
  refresh_language_labels()
  return true
end

refresh_language_labels()

local focused_input = nil
local last_mouse_cap = 0

local function draw_logs(x, y, w, h)
  local font_size = 11
  gfx.setfont(1, ui_font, font_size)
  gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
  
  local line_height = 16
  local max_lines = math.max(0, math.floor((h - 8) / line_height))
  
  -- Prepara as linhas formatadas (com quebra se passarem de w)
  local formatted_lines = {}
  for raw_line in iter_logs() do
    local line = raw_line:gsub("\t", "    ")
    local line_w, _ = gfx.measurestr(line)
    if line_w <= w then
      table.insert(formatted_lines, line)
    else
      -- Quebra a linha de forma inteligente para caber no box de log
      local current = ""
      for word in line:gmatch("%S+") do
        local test = current == "" and word or (current .. " " .. word)
        if gfx.measurestr(test) > w then
          if current ~= "" then
            table.insert(formatted_lines, current)
            current = word
          else
            table.insert(formatted_lines, word)
            current = ""
          end
        else
          current = test
        end
      end
      if current ~= "" then
        table.insert(formatted_lines, current)
      end
    end
  end
  
  local start_idx = math.max(1, #formatted_lines - max_lines + 1)
  local curr_y = y
  local bottom_limit = y + h - line_height - 2
  
  -- Desenha as linhas que cabem
  for i = start_idx, #formatted_lines do
    if curr_y > bottom_limit then
      break
    end
    gfx.x = x
    gfx.y = curr_y
    gfx.drawstr(formatted_lines[i])
    curr_y = curr_y + line_height
  end
end

local function draw_copy_button()
  local btn = layout.copy_logs
  local mouse_over_copy = in_rect(btn.x, btn.y, btn.w, btn.h)
  if mouse_over_copy then
    gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
  else
    gfx.r, gfx.g, gfx.b = 0.18, 0.18, 0.18
  end
  gfx.rect(btn.x, btn.y, btn.w, btn.h, 1)
  gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
  gfx.rect(btn.x, btn.y, btn.w, btn.h, 0)
  gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
  gfx.setfont(1, ui_font, 10)
  local c_text = t("btn_copy_logs")
  local c_tw, c_th = gfx.measurestr(c_text)
  gfx.x = btn.x + (btn.w - c_tw)/2
  gfx.y = btn.y + (btn.h - c_th)/2
  gfx.drawstr(c_text)
end

local function draw_toggle_view_button()
  local btn = layout.toggle_view
  local mouse_over = in_rect(btn.x, btn.y, btn.w, btn.h)
  if mouse_over then
    gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
  else
    gfx.r, gfx.g, gfx.b = 0.18, 0.18, 0.18
  end
  gfx.rect(btn.x, btn.y, btn.w, btn.h, 1)
  gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
  gfx.rect(btn.x, btn.y, btn.w, btn.h, 0)
  
  gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
  gfx.setfont(1, ui_font, 10)
  local text = show_text_log and "VISUAL" or "LOG"
  local tw, th = gfx.measurestr(text)
  gfx.x = btn.x + (btn.w - tw) / 2
  gfx.y = btn.y + (btn.h - th) / 2
  gfx.drawstr(text)
end

local last_summary_time = 0
local cached_jobs = 0
local cached_skipped = 0

local function update_analysis_summary_cached()
  local now = reaper.time_precise()
  if now - last_summary_time > 0.5 then -- Atualiza a cada 500ms para manter a UI veloz
    last_summary_time = now
    
    local track_count = reaper.CountTracks(0)
    local n_jobs = 0
    local skipped_no_audio = 0

    for i = 0, track_count - 1 do
      local track = reaper.GetTrack(0, i)
      local include = (not only_selected) or reaper.IsTrackSelected(track)

      if include then
        local best = false
        local n_items = reaper.CountTrackMediaItems(track)

        for j = 0, n_items - 1 do
          local item = reaper.GetTrackMediaItem(track, j)
          local take = reaper.GetActiveTake(item)

          if take and not reaper.TakeIsMIDI(take) then
            local source = reaper.GetMediaItemTake_Source(take)
            local filename = reaper.GetMediaSourceFileName(source, "")

            if filename and filename ~= "" then
              best = true
              break
            end
          end
        end

        if best then
          n_jobs = n_jobs + 1
        else
          skipped_no_audio = skipped_no_audio + 1
        end
      end
    end
    cached_jobs = n_jobs
    cached_skipped = skipped_no_audio
  end
  return cached_jobs, cached_skipped
end

parse_log_progress = function(text)
  for line in text:gmatch("[^\n]+") do
    if line:find("✔ trk", 1, true) then
      local trk_idx_str = line:match("✔ trk (%d+)")
      if trk_idx_str then
        local idx = tonumber(trk_idx_str)
        if idx and track_info[idx] then
          local content = line:match("│%s*(.-)%s*│")
          if content then
            local arrow_start, arrow_end = content:find("→", 1, true)
            if arrow_start then
              local category = content:sub(1, arrow_start - 1):gsub("^%s*", ""):gsub("%s*$", "")
              local instrument = content:sub(arrow_end + 1):gsub("^%s*", ""):gsub("%s*$", "")
              
              local col_key = get_color_key(category, instrument, instrument)
              local col = config_colors[col_key] or config_colors["outro"]
              
              track_info[idx].status = "ok"
              track_info[idx].color = col
              track_info[idx].instrument = instrument
              track_info[idx].category = category
            end
          end
        end
      end
    elseif line:find("✖ trk", 1, true) then
      local trk_idx_str = line:match("✖ trk (%d+)")
      if trk_idx_str then
        local idx = tonumber(trk_idx_str)
        if idx and track_info[idx] then
          track_info[idx].status = "error"
          track_info[idx].color = {220, 50, 50}
          track_info[idx].instrument = "error"
        end
      end
    end
  end
end

local function draw_visualizer(box_x, box_y, box_w, box_h)
  local display_tracks = {}
  local track_count = reaper.CountTracks(0)
  for idx = 0, track_count - 1 do
    local info = track_info[idx]
    if info and info.audio then
      table.insert(display_tracks, info)
    end
  end
  
  local num_tracks = #display_tracks
  if num_tracks == 0 then
    gfx.setfont(1, ui_font, 12)
    gfx.r, gfx.g, gfx.b = 0.5, 0.5, 0.5
    local text = t("msg_no_audio") or "Sem faixas para analisar"
    local tw, th = gfx.measurestr(text)
    gfx.x = box_x + (box_w - tw) / 2
    gfx.y = box_y + (box_h - th) / 2
    gfx.drawstr(text)
    return
  end
  
  local padding = 4
  local track_h = math.floor((box_h - (num_tracks + 1) * padding) / num_tracks)
  if track_h < 6 then
    track_h = 6
    padding = 1
  end
  if track_h > 36 then
    track_h = 36
  end
  
  local start_y = box_y + padding
  
  for i, info in ipairs(display_tracks) do
    local ty = start_y + (i - 1) * (track_h + padding)
    if ty + track_h > box_y + box_h then break end
    
    local rx = box_x + padding
    local rw = box_w - padding * 2
    
    -- TCP width (left panel)
    local tcp_w = math.floor(rw * 0.35)
    if tcp_w < 50 then tcp_w = 50 end
    if tcp_w > 90 then tcp_w = 90 end
    
    local item_w = rw - tcp_w - 2
    local item_x = rx + tcp_w + 2
    
    -- 1. Draw TCP (Track Control Panel)
    gfx.r, gfx.g, gfx.b = 0.16, 0.16, 0.16
    gfx.rect(rx, ty, tcp_w, track_h, 1)
    
    gfx.r, gfx.g, gfx.b = 0.22, 0.22, 0.22
    gfx.rect(rx, ty, tcp_w, track_h, 0)
    
    if track_h >= 14 then
      gfx.setfont(1, ui_font, 10)
      gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
      gfx.x = rx + 6
      gfx.y = ty + (track_h - 12) / 2
      local disp_name = info.name
      while gfx.measurestr(disp_name) > (tcp_w - 12) and #disp_name > 0 do
        disp_name = disp_name:sub(1, -2)
      end
      if #disp_name < #info.name then
        disp_name = disp_name .. ".."
      end
      gfx.drawstr(disp_name)
    end
    
    -- 2. Draw "Media Item"
    local r, g, b = 0.25, 0.25, 0.25
    local is_analyzing = false
    
    if info.status == "ok" then
      r, g, b = info.color[1] / 255, info.color[2] / 255, info.color[3] / 255
    elseif info.status == "error" then
      r, g, b = 0.8, 0.2, 0.2
    elseif info.status == "pending" then
      r, g, b = 0.28, 0.28, 0.28
      local is_active = true
      for prev_idx = 1, i - 1 do
        if display_tracks[prev_idx].status == "pending" then
          is_active = false
          break
        end
      end
      if is_active then
        is_analyzing = true
        local pulse = 0.35 + 0.15 * math.sin(reaper.time_precise() * 6)
        r, g, b = pulse, pulse, pulse
      end
    end
    
    gfx.r, gfx.g, gfx.b = r, g, b
    gfx.rect(item_x, ty, item_w, track_h, 1)
    
    if is_analyzing then
      gfx.r, gfx.g, gfx.b = 0.6, 0.6, 0.6
    else
      gfx.r, gfx.g, gfx.b = 0.18, 0.18, 0.18
    end
    gfx.rect(item_x, ty, item_w, track_h, 0)
    
    -- Waveform abstraction
    if track_h >= 10 then
      gfx.r, gfx.g, gfx.b = r * 1.3, g * 1.3, b * 1.3
      if gfx.r > 1.0 then gfx.r = 1.0 end
      if gfx.g > 1.0 then gfx.g = 1.0 end
      if gfx.b > 1.0 then gfx.b = 1.0 end
      
      local num_lines = math.floor(item_w / 6)
      for j = 1, num_lines do
        local lx = item_x + j * 6
        local line_h = math.floor((track_h - 4) * (0.3 + 0.7 * math.sin(j * 0.8 + i)))
        if line_h < 2 then line_h = 2 end
        local ly = ty + (track_h - line_h) / 2
        gfx.line(lx, ly, lx, ly + line_h)
      end
    end
    
    -- Instrument Name overlay
    if info.status == "ok" and track_h >= 14 then
      gfx.setfont(1, ui_font, 10)
      gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
      
      local inst_text = info.instrument or ""
      local inst_w, inst_h = gfx.measurestr(inst_text)
      if inst_w < (item_w - 12) then
        gfx.x = item_x + 8
        gfx.y = ty + (track_h - inst_h) / 2
        gfx.drawstr(inst_text)
      end
    elseif is_analyzing and track_h >= 14 then
      gfx.setfont(1, ui_font, 10)
      gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
      local scan_text = "scanning..."
      local sw, sh = gfx.measurestr(scan_text)
      if sw < (item_w - 12) then
        gfx.x = item_x + 8
        gfx.y = ty + (track_h - sh) / 2
        gfx.drawstr(scan_text)
      end
    end
  end
end

local last_hovered_text = nil
local hover_start_time = 0
local lock_tx, lock_ty = 0, 0

local function draw_gui()
  local tooltip_to_draw = nil
  -- Fundo escuro (#1E1E1E)
  gfx.r, gfx.g, gfx.b = 0.12, 0.12, 0.12
  gfx.rect(0, 0, gfx.w, gfx.h, 1)

  -- Modo compacto: tela "config" com painel avançado fechado
  local is_compact_view = (gui_state == "config" and not show_advanced)

  -- Titulo (Logo ou Fallback Text) — no compacto usa a logo em alta resolucao
  -- (ainometor_biglogo.png), no mesmo tamanho visivel de sempre (target_h),
  -- so que mais nitida por partir de uma imagem maior
  local use_big_logo = is_compact_view and logo_big_loaded
  local buf = use_big_logo and logo_big_buffer or logo_buffer
  local src_w = use_big_logo and logo_big_w or logo_w
  local src_h = use_big_logo and logo_big_h or logo_h

  if logo_loaded or logo_big_loaded then
    local target_h = is_compact_view and 130 or 80
    local target_w = (src_w / src_h) * target_h
    local target_x = (gfx.w - target_w) / 2
    gfx.blit(buf, 1, 0, 0, 0, src_w, src_h, target_x, 15, target_w, target_h)
  else
    gfx.setfont(1, ui_font, 18) -- Bold
    gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    gfx.x, gfx.y = 30, 30
    gfx.drawstr("AiNOMEATOR")
  end

  -- Linha divisoria (oculta no modo compacto — o logo maior já ocupa esse espaço)
  if not is_compact_view then
    gfx.setfont(1, ui_font, 12)
    gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
    gfx.line(30, 100, 290, 100)
  end

  if gui_state == "config" then
    -- Radio de idioma no topo
    local en = layout.lang_en
    local pt = layout.lang_pt
    if in_rect(en.x, en.y, en.w, en.h) then tooltip_to_draw = t("tip_lang_en") end
    if in_rect(pt.x, pt.y, pt.w, pt.h) then tooltip_to_draw = t("tip_lang_pt") end

    gfx.setfont(1, ui_font, 10)
    gfx.r, gfx.g, gfx.b = 0.78, 0.78, 0.78
    gfx.x, gfx.y = en.radio_x + 12, en.radio_y - 7
    gfx.drawstr("EN")
    gfx.x, gfx.y = pt.radio_x + 12, pt.radio_y - 7
    gfx.drawstr("PT")
    gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
    gfx.circle(en.radio_x, en.radio_y, en.radio_r, 0, 1)
    gfx.circle(pt.radio_x, pt.radio_y, pt.radio_r, 0, 1)
    if lang == "en" then
      gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
      gfx.circle(en.radio_x, en.radio_y, en.radio_r - 2, 1, 1)
    else
      gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
      gfx.circle(pt.radio_x, pt.radio_y, pt.radio_r - 2, 1, 1)
    end

    -- Botão de Opções Avançadas (Toggle) — no compacto fica abaixo do botão Analisar
    local toggle = {
      x = layout.advanced_toggle.x,
      y = show_advanced and layout.advanced_toggle.y or COMPACT_TOGGLE_Y,
      w = layout.advanced_toggle.w,
      h = layout.advanced_toggle.h,
    }
    local mouse_over_toggle = in_rect(toggle.x, toggle.y, toggle.w, toggle.h)
    if mouse_over_toggle then
      tooltip_to_draw = t("tip_btn_show_advanced")
      gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
    else
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
    end
    gfx.rect(toggle.x, toggle.y, toggle.w, toggle.h, 1) -- fill
    gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
    gfx.rect(toggle.x, toggle.y, toggle.w, toggle.h, 0) -- border
    
    local toggle_label = show_advanced and t("btn_hide_advanced") or t("btn_show_advanced")
    gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
    gfx.setfont(1, ui_font, 10) -- Bold
    local t_w, t_h = gfx.measurestr(toggle_label)
    gfx.x = toggle.x + (toggle.w - t_w)/2
    gfx.y = toggle.y + (toggle.h - t_h)/2
    gfx.drawstr(toggle_label)

    if show_advanced then
      -- Opções Gerais Label
      gfx.setfont(1, ui_font, 11) -- Bold
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
      gfx.x, gfx.y = 30, 140
      gfx.drawstr(t("general_label"))
      gfx.setfont(1, ui_font, 11)

      -- Checkbox "Apenas faixas selecionadas"
      local opt = layout.only_selected
      if in_rect(opt.x, opt.y, opt.w, opt.h) then tooltip_to_draw = t("tip_only_selected") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(opt.cb_x, opt.cb_y, opt.cb_size, opt.cb_size, 1) -- fill
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(opt.cb_x, opt.cb_y, opt.cb_size, opt.cb_size, 0) -- border
      
      if only_selected then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(opt.cb_x + 3, opt.cb_y + 3, opt.cb_size - 6, opt.cb_size - 6, 1)
      end

      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = opt.cb_x + 28, opt.cb_y + 1
      gfx.drawstr(t("only_selected"))

      -- Checkbox "Ordenar por instrumento"
      local opt_s = layout.sort_tracks
      if in_rect(opt_s.x, opt_s.y, opt_s.w, opt_s.h) then tooltip_to_draw = t("tip_sort_tracks") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(opt_s.cb_x, opt_s.cb_y, opt_s.cb_size, opt_s.cb_size, 1) -- fill
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(opt_s.cb_x, opt_s.cb_y, opt_s.cb_size, opt_s.cb_size, 0) -- border
      
      if sort_tracks then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(opt_s.cb_x + 3, opt_s.cb_y + 3, opt_s.cb_size - 6, opt_s.cb_size - 6, 1)
      end

      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = opt_s.cb_x + 28, opt_s.cb_y + 1
      gfx.drawstr(t("sort_tracks"))
      
      -- Checkbox "Criar pastas"
      local opt_f = layout.create_folders
      if in_rect(opt_f.x, opt_f.y, opt_f.w, opt_f.h) then tooltip_to_draw = t("tip_create_folders") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(opt_f.cb_x, opt_f.cb_y, opt_f.cb_size, opt_f.cb_size, 1) -- fill
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(opt_f.cb_x, opt_f.cb_y, opt_f.cb_size, opt_f.cb_size, 0) -- border
      if create_folders then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(opt_f.cb_x + 3, opt_f.cb_y + 3, opt_f.cb_size - 6, opt_f.cb_size - 6, 1)
      end
      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = opt_f.cb_x + 28, opt_f.cb_y + 1
      gfx.drawstr(t("create_folders"))

      -- Checkbox "Apagar silencio"
      local opt_d = layout.delete_silent
      if in_rect(opt_d.x, opt_d.y, opt_d.w, opt_d.h) then tooltip_to_draw = t("tip_delete_silent") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(opt_d.cb_x, opt_d.cb_y, opt_d.cb_size, opt_d.cb_size, 1) -- fill
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(opt_d.cb_x, opt_d.cb_y, opt_d.cb_size, opt_d.cb_size, 0) -- border
      if delete_silent then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(opt_d.cb_x + 3, opt_d.cb_y + 3, opt_d.cb_size - 6, opt_d.cb_size - 6, 1)
      end
      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = opt_d.cb_x + 28, opt_d.cb_y + 1
      gfx.drawstr(t("delete_silent"))
      
      -- Linha divisória
      gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
      gfx.line(30, 215, 290, 215)

      -- Modo de Análise Label
      gfx.setfont(1, ui_font, 11) -- Bold
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
      gfx.x, gfx.y = 30, 225
      gfx.drawstr(t("analysis_mode"))
      gfx.setfont(1, ui_font, 11)

      -- Radio 1: "Análise rápida de pequena amostra"
      local r1 = layout.mode_fast
      if in_rect(r1.x, r1.y, r1.w, r1.h) then tooltip_to_draw = t("tip_mode_fast") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(r1.cb_x, r1.cb_y, r1.cb_size, r1.cb_size, 1)
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(r1.cb_x, r1.cb_y, r1.cb_size, r1.cb_size, 0)
      if analysis_mode == "fast" then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(r1.cb_x + 3, r1.cb_y + 3, r1.cb_size - 6, r1.cb_size - 6, 1)
      end
      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = r1.cb_x + 28, r1.cb_y + 1
      gfx.drawstr(t("mode_fast"))

      -- Radio 2: "Análise detalhada"
      local r2 = layout.mode_detailed
      if in_rect(r2.x, r2.y, r2.w, r2.h) then tooltip_to_draw = t("tip_mode_detailed") end
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      gfx.rect(r2.cb_x, r2.cb_y, r2.cb_size, r2.cb_size, 1)
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(r2.cb_x, r2.cb_y, r2.cb_size, r2.cb_size, 0)
      if analysis_mode == "detailed" then
        gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        gfx.rect(r2.cb_x + 3, r2.cb_y + 3, r2.cb_size - 6, r2.cb_size - 6, 1)
      end
      gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
      gfx.x, gfx.y = r2.cb_x + 28, r2.cb_y + 1
      gfx.drawstr(t("mode_detailed"))

      -- Linha divisória
      gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
      gfx.line(30, 270, 290, 270)

      -- Backend de Análise Label
      gfx.setfont(1, ui_font, 11) -- Bold
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
      gfx.x, gfx.y = 30, layout.backend_label_y
      gfx.drawstr(t("backend_label"))
      gfx.setfont(1, ui_font, 11)

      local cb_x, cb_size = layout.backend_cb_x, layout.backend_cb_size
      for i, opt in ipairs(backend_options) do
        local opt_y = layout.backend_start_y + (i - 1) * layout.backend_spacing_y
        if in_rect(cb_x, opt_y, 250, cb_size) then
          tooltip_to_draw = t("tip_backend_" .. opt.key)
        end
        gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
        gfx.rect(cb_x, opt_y, cb_size, cb_size, 1)
        gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
        gfx.rect(cb_x, opt_y, cb_size, cb_size, 0)
        if backend == opt.key then
          gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
          gfx.rect(cb_x + 3, opt_y + 3, cb_size - 6, cb_size - 6, 1)
        end
        gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
        gfx.x, gfx.y = cb_x + 28, opt_y + 1
        gfx.drawstr(t(opt.label_key))
      end

      -- Linha divisória
      gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
      gfx.line(30, 420, 290, 420)

      -- Linha divisória após as threads
      gfx.line(30, 485, 290, 485)

      -- Theme Selector Label
      gfx.setfont(1, ui_font, 10)
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
      gfx.x, gfx.y = layout.theme_selector.x, layout.theme_selector.y - 20
      gfx.drawstr(t("theme_label"))

      -- Theme Selector Box
      local ts = layout.theme_selector
      local mouse_over_ts = in_rect(ts.x, ts.y, ts.w, ts.h)
      if mouse_over_ts then
        tooltip_to_draw = t("tip_theme")
        gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
      else
        gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
      end
      gfx.rect(ts.x, ts.y, ts.w, ts.h, 1) -- fill
      gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
      gfx.rect(ts.x, ts.y, ts.w, ts.h, 0) -- border

      -- Theme Selector Text
      gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
      gfx.setfont(1, ui_font, 10) -- Bold
      local theme_text = t("theme_" .. current_theme)
      local tstw, tsth = gfx.measurestr(theme_text)
      gfx.x = ts.x + (ts.w - tstw)/2
      gfx.y = ts.y + (ts.h - tsth)/2
      gfx.drawstr(theme_text)

      -- Linha divisória após o Theme
      gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
      gfx.line(30, 560, 290, 560)

      -- Campos de Texto
      for i, inp in ipairs(inputs) do
        if in_rect(inp.x, inp.y, inp.w, inp.h) then
          if i == 1 then
            tooltip_to_draw = t("tip_threads_cpu")
          elseif i == 2 then
            tooltip_to_draw = t("tip_colors_prompt")
          elseif i == 3 then
            tooltip_to_draw = t("tip_local_threads")
          end
        end

        -- Label
        gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
        gfx.x, gfx.y = inp.x, inp.y - 20
        gfx.drawstr(inp.label)

        -- Caixa de input (cinza se desabilitada)
        if i == 2 and current_theme ~= "custom" then
          gfx.r, gfx.g, gfx.b = 0.10, 0.10, 0.10
        else
          gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
        end
        gfx.rect(inp.x, inp.y, inp.w, inp.h, 1) -- preenchimento

        -- Borda (destaque Coral se focado)
        if focused_input == i then
          gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
        else
          gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
        end
        gfx.rect(inp.x, inp.y, inp.w, inp.h, 0)

        -- Valor ou Placeholder
        local max_w = inp.w - 16
        if i == 2 and current_theme ~= "custom" then
          gfx.r, gfx.g, gfx.b = 0.35, 0.35, 0.35 -- Muted text
          gfx.x, gfx.y = inp.x + 8, inp.y + 7
          gfx.drawstr(t("theme_prompt_disabled"))
        elseif inp.val == "" and focused_input ~= i then
          gfx.r, gfx.g, gfx.b = 0.4, 0.4, 0.4 -- Cinza apagado para placeholder
          gfx.x, gfx.y = inp.x + 8, inp.y + 7
          
          local disp_placeholder = inp.placeholder
          while gfx.measurestr(disp_placeholder) > max_w and #disp_placeholder > 0 do
            disp_placeholder = disp_placeholder:sub(1, -2)
          end
          gfx.drawstr(disp_placeholder)
        else
          gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
          gfx.x, gfx.y = inp.x + 8, inp.y + 7
          
          local display_text = inp.val
          if inp.is_password and not show_api_key then
            display_text = string.rep("•", #inp.val)
          end
          
          -- Efeito de scroll: se o texto for maior que a caixa, corta o comeco
          local start_idx = 1
          local actual_display = display_text
          while gfx.measurestr(actual_display) > max_w and start_idx <= #display_text do
            start_idx = start_idx + 1
            actual_display = display_text:sub(start_idx)
          end
          
          gfx.drawstr(actual_display)

          -- Cursor piscante
          if focused_input == i then
            local str_w, str_h = gfx.measurestr(actual_display)
            if math.floor(reaper.time_precise() * 2) % 2 == 0 then
              gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
              gfx.rect(inp.x + 8 + str_w, inp.y + 6, 2, 18, 1)
            end
          end
        end
      end
    else
      -- Linha divisória compacta
      gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
      gfx.line(30, 135, 290, 135)
    end

    -- Info de resumo econômico/faixas dinâmico
    local n_jobs, n_skipped = update_analysis_summary_cached()
    gfx.setfont(1, ui_font, 11)
    local summary_y1 = show_advanced and 635 or 150
    local summary_y2 = show_advanced and 647 or 162
    if n_jobs == 0 then
      gfx.r, gfx.g, gfx.b = 0.8, 0.6, 0.2 -- Amarelo/Dourado suave
      gfx.x, gfx.y = 30, summary_y1
      gfx.drawstr(t("msg_no_audio"))
    else
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65 -- Cinza suave
      gfx.x, gfx.y = 30, summary_y2
      local msg = string.format(t("msg_summary"), n_jobs, n_skipped)
      gfx.drawstr(msg)
    end

    -- Botao Analisar (Coral) - Centrado de largura total
    local btn_y = show_advanced and layout.analyze.y or COMPACT_BTN_Y
    local btn_w = layout.analyze.w
    local btn_h = show_advanced and layout.analyze.h or COMPACT_BTN_H
    local mouse_over_run = in_rect(30, btn_y, btn_w, btn_h)
    if mouse_over_run then
      tooltip_to_draw = t("tip_btn_analyze")
      gfx.r, gfx.g, gfx.b = 0.65, 0.1, 0.15
    else
      gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    end
    gfx.rect(30, btn_y, btn_w, btn_h, 1)
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.setfont(1, ui_font, 12) -- Bold
    local tw, th = gfx.measurestr(t("btn_analyze"))
    gfx.x = 30 + (btn_w - tw)/2
    gfx.y = btn_y + (btn_h - th)/2
    gfx.drawstr(t("btn_analyze"))

    -- Aviso experimental
    gfx.setfont(1, ui_font, 11)
    gfx.r, gfx.g, gfx.b = 0.33, 0.33, 0.33
    local note1 = t("experimental_notice_1")
    local note2 = t("experimental_notice_2")
    local note1_w = gfx.measurestr(note1)
    local note2_w = gfx.measurestr(note2)
    local note_y1 = show_advanced and 720 or COMPACT_NOTE_Y1
    local note_y2 = show_advanced and 731 or COMPACT_NOTE_Y2
    gfx.x = (gfx.w - note1_w) / 2
    gfx.y = note_y1
    gfx.drawstr(note1)
    gfx.x = (gfx.w - note2_w) / 2
    gfx.y = note_y2
    gfx.drawstr(note2)

    -- Creditos visiveis
    gfx.setfont(1, ui_font, 10)
    local credit_text = "by jasko"
    local cr_w, cr_h = gfx.measurestr(credit_text)
    local cr_x = (gfx.w - cr_w) / 2
    local cr_y = show_advanced and 760 or COMPACT_CREDITS_Y
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.x = cr_x
    gfx.y = cr_y
    gfx.drawstr(credit_text)

    -- Processa o delay e lock do tooltip
    if tooltip_to_draw then
      if tooltip_to_draw ~= last_hovered_text then
        last_hovered_text = tooltip_to_draw
        hover_start_time = reaper.time_precise()
        lock_tx = gfx.mouse_x + 15
        lock_ty = gfx.mouse_y + 15
      end
      if reaper.time_precise() - hover_start_time >= 0.5 then
        -- Mantém tooltip_to_draw ativo
      else
        tooltip_to_draw = nil
      end
    else
      last_hovered_text = nil
      hover_start_time = 0
    end

  elseif gui_state == "analyzing" then
    -- Subtitulo
    gfx.setfont(1, ui_font, 12)
    gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
    gfx.x, gfx.y = 30, 110
    gfx.drawstr(t("lbl_analyzing"))

    -- Animacao simples
    local dot_count = math.floor(reaper.time_precise() * 2) % 4
    local dots = string.rep(".", dot_count)
    gfx.drawstr(dots)

    -- Caixa de Logs
    local box_x, box_y, box_w, box_h = 30, 135, 260, 460
    gfx.r, gfx.g, gfx.b = 0.08, 0.08, 0.08
    gfx.rect(box_x, box_y, box_w, box_h, 1)
    gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
    gfx.rect(box_x, box_y, box_w, box_h, 0)

    if show_text_log then
      draw_logs(box_x + 10, box_y + 10, box_w - 20, box_h - 20)
    else
      draw_visualizer(box_x, box_y, box_w, box_h)
    end
    draw_copy_button()
    draw_toggle_view_button()

    -- Botao Cancelar
    local btn = layout.close
    local mouse_over_cancel = in_rect(btn.x, btn.y, btn.w, btn.h)
    if mouse_over_cancel then
      gfx.r, gfx.g, gfx.b = 0.30, 0.30, 0.30
    else
      gfx.r, gfx.g, gfx.b = 0.20, 0.20, 0.20
    end
    gfx.rect(btn.x, btn.y, btn.w, btn.h, 1)
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.setfont(1, ui_font, 12) -- Bold
    local tw, th = gfx.measurestr(t("btn_cancel"))
    gfx.x = btn.x + (btn.w - tw)/2
    gfx.y = btn.y + (btn.h - th)/2
    gfx.drawstr(t("btn_cancel"))

  elseif gui_state == "completed" or gui_state == "error" then
    -- Subtitulo de Sucesso ou Erro
    gfx.setfont(1, ui_font, 13) -- Bold
    gfx.x, gfx.y = 30, 110
    if gui_state == "completed" then
      gfx.r, gfx.g, gfx.b = 0.3, 0.8, 0.3 -- Verde
      gfx.drawstr(t("lbl_completed"))
    else
      gfx.r, gfx.g, gfx.b = 0.9, 0.3, 0.3 -- Vermelho
      gfx.drawstr(t("lbl_error"))
    end

    -- Caixa de Logs
    local box_x, box_y, box_w, box_h = 30, 135, 260, 460
    gfx.r, gfx.g, gfx.b = 0.08, 0.08, 0.08
    gfx.rect(box_x, box_y, box_w, box_h, 1)
    gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
    gfx.rect(box_x, box_y, box_w, box_h, 0)

    if show_text_log then
      draw_logs(box_x + 10, box_y + 10, box_w - 20, box_h - 20)
    else
      draw_visualizer(box_x, box_y, box_w, box_h)
    end
    draw_copy_button()
    draw_toggle_view_button()

    -- Botao Fechar
    local btn = layout.close
    local mouse_over_close = in_rect(btn.x, btn.y, btn.w, btn.h)
    if mouse_over_close then
      gfx.r, gfx.g, gfx.b = 0.65, 0.1, 0.15
    else
      gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    end
    gfx.rect(btn.x, btn.y, btn.w, btn.h, 1)
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.setfont(1, ui_font, 12) -- Bold
    local tw, th = gfx.measurestr(t("btn_close"))
    gfx.x = btn.x + (btn.w - tw)/2
    gfx.y = btn.y + (btn.h - th)/2
    gfx.drawstr(t("btn_close"))
  end

  if tooltip_to_draw then
    gfx.setfont(1, ui_font, 10)
    local t_w, t_h = gfx.measurestr(tooltip_to_draw)
    local tx = lock_tx
    local ty = lock_ty
    if tx + t_w + 10 > gfx.w then tx = gfx.w - t_w - 10 end
    if ty + t_h + 10 > gfx.h then ty = lock_ty - t_h - 30 end -- offsets slightly higher if going out of screen bottom
    gfx.r, gfx.g, gfx.b = 0.18, 0.18, 0.18
    gfx.rect(tx, ty, t_w + 10, t_h + 6, 1)
    gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    gfx.rect(tx, ty, t_w + 10, t_h + 6, 0)
    gfx.r, gfx.g, gfx.b = 0.9, 0.9, 0.9
    gfx.x = tx + 5
    gfx.y = ty + 3
    gfx.drawstr(tooltip_to_draw)
  end

  gfx.update()
end

local function resize_window(expected_h)
  local dock, x, y, w, h = gfx.dock(-1, 0, 0, 0, 0)
  gfx.quit()
  gfx.init("AiNOMEATOR", 320, expected_h, 0, x, y)
  load_logo()
end

local function update_gui()
  local mouse_pressed = (gfx.mouse_cap & 1 == 1) and (last_mouse_cap & 1 == 0)
  last_mouse_cap = gfx.mouse_cap

  if mouse_pressed then
    if gui_state == "config" then
      -- Clique no radio de idioma no topo
      local en = layout.lang_en
      local pt = layout.lang_pt
      if in_rect(en.x, en.y, en.w, en.h) then
        if set_language("en") then
          return "redraw"
        end
      elseif in_rect(pt.x, pt.y, pt.w, pt.h) then
        if set_language("pt") then
          return "redraw"
        end
      end

      -- Clique no toggle de Opções Avançadas
      local toggle = {
        x = layout.advanced_toggle.x,
        y = show_advanced and layout.advanced_toggle.y or COMPACT_TOGGLE_Y,
        w = layout.advanced_toggle.w,
        h = layout.advanced_toggle.h,
      }
      if in_rect(toggle.x, toggle.y, toggle.w, toggle.h) then
        show_advanced = not show_advanced
        reaper.SetExtState("AiNOMEATOR", "show_advanced", show_advanced and "true" or "false", true)
        
        -- Redimensionar a janela
        local expected_h = show_advanced and 795 or COMPACT_WINDOW_H
        resize_window(expected_h)
        return "redraw"
      end

      if show_advanced then
        -- Clique no checkbox "Apenas faixas selecionadas"
        local opt = layout.only_selected
        if in_rect(opt.x, opt.y, opt.w, opt.h) then
          only_selected = not only_selected
          return "redraw"
        end

        -- Clique no checkbox "Ordenar por instrumento"
        local opt_s = layout.sort_tracks
        if in_rect(opt_s.x, opt_s.y, opt_s.w, opt_s.h) then
          sort_tracks = not sort_tracks
          reaper.SetExtState("AiNOMEATOR", "sort_tracks", sort_tracks and "true" or "false", true)
          return "redraw"
        end

        local opt_f = layout.create_folders
        if in_rect(opt_f.x, opt_f.y, opt_f.w, opt_f.h) then
          create_folders = not create_folders
          reaper.SetExtState("AiNOMEATOR", "create_folders", create_folders and "true" or "false", true)
          return "redraw"
        end

        local opt_d = layout.delete_silent
        if in_rect(opt_d.x, opt_d.y, opt_d.w, opt_d.h) then
          delete_silent = not delete_silent
          reaper.SetExtState("AiNOMEATOR", "delete_silent", delete_silent and "true" or "false", true)
          return "redraw"
        end

        -- Clicou Radio 1 "Análise rápida"
        local r1 = layout.mode_fast
        if in_rect(r1.x, r1.y, r1.w, r1.h) then
          analysis_mode = "fast"
          return "redraw"
        end

        -- Clicou Radio 2 "Análise detalhada"
        local r2 = layout.mode_detailed
        if in_rect(r2.x, r2.y, r2.w, r2.h) then
          analysis_mode = "detailed"
          return "redraw"
        end

        -- Clique nos botões de Rádio do Backend
        local cb_x, cb_size = layout.backend_cb_x, layout.backend_cb_size
        for i, opt_backend in ipairs(backend_options) do
          local opt_y = layout.backend_start_y + (i - 1) * layout.backend_spacing_y
          if in_rect(cb_x, opt_y, cb_size, cb_size) then
            backend = opt_backend.key
            reaper.SetExtState("AiNOMEATOR", "backend", backend, true)
            return "redraw"
          end
        end

        -- Clique no Theme Selector
        local ts = layout.theme_selector
        if in_rect(ts.x, ts.y, ts.w, ts.h) then
          local current_idx = 1
          for idx, opt in ipairs(theme_options) do
            if opt.key == current_theme then
              current_idx = idx
              break
            end
          end
          current_idx = current_idx + 1
          if current_idx > #theme_options then
            current_idx = 1
          end
          current_theme = theme_options[current_idx].key
          reaper.SetExtState("AiNOMEATOR", "theme", current_theme, true)
          
          -- Escreve a paleta se for um tema fixo
          if current_theme ~= "custom" then
            write_theme_to_ini(current_theme)
            inputs[2].val = "" -- limpa o prompt
          end
          focused_input = nil
          return "redraw"
        end

        -- Clique nos campos de texto
        local clicked_input = false
        for i, inp in ipairs(inputs) do
          if in_rect(inp.x, inp.y, inp.w, inp.h) then
            if i == 2 and current_theme ~= "custom" then
              -- desabilitado
            else
              focused_input = i
              clicked_input = true
            end
            break
          end
        end
        if not clicked_input then
          focused_input = nil
        end
      end

      -- Clique no botao ANALISAR
      local btn_y = show_advanced and layout.analyze.y or COMPACT_BTN_Y
      local btn_w = layout.analyze.w
      local btn_h = show_advanced and layout.analyze.h or COMPACT_BTN_H
      if in_rect(30, btn_y, btn_w, btn_h) then
        return "run"
      end

      -- Clique nos creditos "by jasko"
      gfx.setfont(1, ui_font, 10)
      local cr_w, cr_h = gfx.measurestr("by jasko")
      local cr_x = (gfx.w - cr_w) / 2
      local cr_y = show_advanced and 745 or COMPACT_CREDITS_Y
      if in_rect(cr_x, cr_y, cr_w, cr_h) then
        open_url("https://jasko.dev")
        return "redraw"
      end

    elseif gui_state == "analyzing" or gui_state == "completed" or gui_state == "error" then
      -- Clique no botao de alternar visualizacao
      local btn_toggle = layout.toggle_view
      if in_rect(btn_toggle.x, btn_toggle.y, btn_toggle.w, btn_toggle.h) then
        show_text_log = not show_text_log
        return "redraw"
      end

      -- Clique no botao de copiar logs
      local btn = layout.copy_logs
      if in_rect(btn.x, btn.y, btn.w, btn.h) then
        local logs_arr = {}
        for l in iter_logs() do table.insert(logs_arr, l) end
        local logs_str = table.concat(logs_arr, "\n")
        if reaper.CF_SetClipboard then
          reaper.CF_SetClipboard(logs_str)
          reaper.MB(t("msg_copied"), "AiNOMEATOR", 0)
        else
          reaper.ShowConsoleMsg(logs_str .. "\n")
          reaper.MB(t("msg_sent_console"), "AiNOMEATOR", 0)
        end
      end

      -- Clique no botao CANCELAR (apenas se analyzing)
      if gui_state == "analyzing" then
        local cancel_btn = layout.close
        if in_rect(cancel_btn.x, cancel_btn.y, cancel_btn.w, cancel_btn.h) then
          if cancel_path and cancel_path ~= "" then
            local f = io.open(cancel_path, "w")
            if f then
              f:write("cancel")
              f:close()
            end
          end
          gui_state = "config"
          local expected_h = show_advanced and 795 or 300
          resize_window(expected_h)
          return "redraw"
        end
      end

      -- Clique no botao FECHAR (apenas se completed ou error)
      if gui_state == "completed" or gui_state == "error" then
        local close_btn = layout.close
        if in_rect(close_btn.x, close_btn.y, close_btn.w, close_btn.h) then
          return "close"
        end
      end
    end
  end

  -- Teclado
  local char = gfx.getchar()
  if char < 0 then
    return "close"
  end

  if char == 27 then -- escape
    return "close"
  end

  if gui_state == "config" and focused_input and char > 0 then
    local inp = inputs[focused_input]
    if char == 8 then -- backspace
      inp.val = inp.val:sub(1, -2)
    elseif char == 13 then -- enter
      return "run"
    elseif char == 9 then -- tab
      focused_input = focused_input + 1
      if focused_input > #inputs then
        focused_input = 1
      end
      if focused_input == 1 then
        focused_input = 2
      end
    elseif char == 22 then -- Ctrl + V (Colar se SWS disponivel)
      if reaper.CF_GetClipboard then
        local clip = reaper.CF_GetClipboard("")
        if clip and clip ~= "" then
          clip = clip:gsub("[%r%n\t]", "")
          if inp.is_numeric then
            clip = sanitize_numeric_input(clip, inp.max_val or 99)
          end
          inp.val = inp.val .. clip
          if inp.is_numeric then
            inp.val = sanitize_numeric_input(inp.val, inp.max_val or 99)
          elseif #inp.val > inp.limit then
            inp.val = inp.val:sub(1, inp.limit)
          end
        end
      else
        log("[WARNING] " .. t("msg_sws_warning"))
        reaper.MB(t("msg_sws_warning"), "AiNOMEATOR", 0)
      end
    elseif char >= 32 and char <= 126 then
      local new_char = string.char(char)
      if inp.is_numeric then
        if new_char:match("%d") and #inp.val < inp.limit then
          inp.val = inp.val .. new_char
          inp.val = sanitize_numeric_input(inp.val, inp.max_val or 99)
        end
      else
        if #inp.val < inp.limit then
          inp.val = inp.val .. new_char
        end
      end
    end
  end

  return "continue"
end

local function run_gui_loop()
  if not script_running then
    return
  end

  -- Ajustar layout de threads/workers baseado no backend
  if backend == "panns" then
    inputs[1].x = 30
    inputs[1].y = 445
    inputs[1].w = 125
    inputs[1].h = 30

    inputs[3].x = 165
    inputs[3].y = 445
    inputs[3].w = 125
    inputs[3].h = 30
  else
    inputs[1].x = -1000
    inputs[1].y = -1000
    inputs[1].w = 1
    inputs[1].h = 1
    inputs[1].val = "1"

    inputs[3].x = 30
    inputs[3].y = 445
    inputs[3].w = 260
    inputs[3].h = 30
  end

  -- Travar o resize
  local expected_h = 680
  if gui_state == "config" then
    expected_h = show_advanced and 795 or 300
  end
  if gfx.w ~= 320 or gfx.h ~= expected_h then
    resize_window(expected_h)
  end

  draw_gui()
  local status = update_gui()
  if status == "run" then
    
    only_selected = (only_selected == true)

    workers = tonumber(sanitize_numeric_input(inputs[1].val, 20)) or 1
    if workers < 1 then workers = 1
    elseif workers > 20 then workers = 20 end
    inputs[1].val = tostring(workers)
    if backend == "panns" then
      reaper.SetExtState("AiNOMEATOR", "panns_workers", inputs[1].val, true)
    end

    local local_threads = tonumber(sanitize_numeric_input(inputs[3].val, 16)) or 1
    if local_threads < 1 then local_threads = 1
    elseif local_threads > 16 then local_threads = 16 end
    inputs[3].val = tostring(local_threads)
    reaper.SetExtState("AiNOMEATOR", "panns_threads", inputs[3].val, true)

    color_prompt = trim(inputs[2].val)

    gui_state = "analyzing"
    start_analysis()
    reaper.defer(run_gui_loop)
  elseif status == "cancel" or status == "close" then
    script_running = false
    gfx.quit()
    return
  else
    reaper.defer(run_gui_loop)
  end
end

-- Inicializa a tela grafica customizada centralizada na tela
local win_w, win_h = 320, 740
local win_x, win_y = 150, 150 -- Fallback padrão se my_getViewport não estiver disponível

if reaper.my_getViewport then
  -- Retorna as coordenadas da tela principal (workarea=true respeita a barra de tarefas)
  local _, left, top, right, bottom = reaper.my_getViewport(0, 0, 0, 0, 0, 0, 0, 0, true)
  if left and top and right and bottom then
    local screen_w = right - left
    local screen_w_real = screen_w > 0 and screen_w or 1024
    local screen_h = bottom - top
    local screen_h_real = screen_h > 0 and screen_h or 768
    
    win_x = left + (screen_w_real - win_w) / 2
    win_y = top + (screen_h_real - win_h) / 2
  end
end

gfx.init("AiNOMEATOR", win_w, win_h, 0, win_x, win_y)
load_logo()
reaper.defer(run_gui_loop)
