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

local lang = reaper.GetExtState("AiNOMEATOR", "language")
if lang == "" then lang = "en" end -- default to English

local saved_panns_threads = reaper.GetExtState("AiNOMEATOR", "panns_threads")
if saved_panns_threads == "" then saved_panns_threads = "1" end

local backend = reaper.GetExtState("AiNOMEATOR", "backend")
if backend == "" then backend = "gemini" end

local saved_sort_tracks = reaper.GetExtState("AiNOMEATOR", "sort_tracks")
if saved_sort_tracks == "" then saved_sort_tracks = "false" end


local strings = {
  en = {
    only_selected = "Only selected",
    sort_tracks = "Sort tracks",
    analysis_mode = "Analysis Mode:",
    mode_fast = "Fast (uses short 8-12s audio segments)",
    mode_detailed = "Detailed (analyzes full WAV / entire item duration)",
    thread_label = "Parallel tracks (1-20):",
    local_thread_label = "Local threads (1-16):",
    prompt_label = "Color prompt (empty = use colors.ini):",
    prompt_placeholder = "Ex: all in green, vintage autumn, cyberpunk neon",
    api_label = "Gemini API Key:",
    api_placeholder = "Paste your API key here (AIzaSy...)",
    btn_show = "Show",
    btn_hide = "Hide",
    btn_save = "Save",
    btn_analyze = "LETS NOMEATE!",
    experimental_notice_1 = "this is an experimental project.",
    experimental_notice_2 = "the AI will make mistakes.",
    btn_close = "CLOSE",
    btn_cancel = "CANCEL",
    lbl_analyzing = "Analyzing tracks with AI...",
    lbl_completed = "Analysis completed!",
    lbl_error = "Error during processing!",
    msg_no_audio = "No eligible audio tracks found.",
    msg_summary = "%d track(s) (%d s/ audio)",
    msg_done = "║  ✓ Done: %d applied  ✗ %d with error  ║",
    msg_copied = "Logs copied to clipboard.",
    msg_sent_console = "Logs sent to Reaper console.",
    msg_empty_api = "Error: Gemini API Key cannot be blank.",
    msg_sws_warning = "To paste with Ctrl+V, SWS Extension is required.",
    opt_quality_high = "alta",
    opt_quality_normal = "normal",
    logs_title = "Logs",
    btn_copy_logs = "Copy Logs",
    msg_color_created = "⚙  Color file created: ",
    msg_color_edit_hint = "   (You can edit this file to customize colors!)",
    msg_color_loaded = "⚙  Color settings loaded from: ",
    msg_venv_missing = "[WARNING] Virtual environment (venv) not detected. Using system global Python.",
    msg_venv_hint = "        Make sure you have run setup or have the dependencies installed.",
    msg_starting = "▶  Starting analysis of %d track(s) with %d thread(s)...",
    msg_waiting = "   Please wait — progress appears below in real time.",
    msg_applying = "◀  Analysis completed! Applying names and colors in Reaper...",
    msg_api_error = "[ERROR] batch_rename.py did not generate the result file.",
    msg_api_causes = "Possible causes:\n  - setup.bat has not been run in this folder\n  - GEMINI_API_KEY is not configured in .env\n  - Python was not found in PATH",
    msg_api_causes_mb = "Failed to run AI. Possible causes:\n- setup.bat has not been run in this folder (venv/.env missing)\n- GEMINI_API_KEY not configured in .env\n- Python not found\n\nSee the Reaper console for details.",
    backend_label = "Analysis Backend:",
    backend_gemini = "Gemini (cloud, API key required)",
    backend_yamnet = "YamNet (local, no API key)",
    backend_essentia = "Essentia (local, no API key)",
    backend_panns = "PANNs (local, no API key)",
    backend_hybrid_heuristic = "Hybrid Heuristic (PANNs + Gemini)",
    backend_hybrid_chaining = "Hybrid Chaining (PANNs + Gemini review)",
  },
  pt = {
    only_selected = "Apenas sel.",
    sort_tracks = "Ordenar inst.",
    analysis_mode = "Modo de Análise:",
    mode_fast = "Rápida (analisa trechos curtos de 8-12s)",
    mode_detailed = "Detalhada (analisa WAV original / faixa completa)",
    thread_label = "Faixas/CPU (1-20):",
    local_thread_label = "Threads locais (1-16):",
    prompt_label = "Prompt de cores (vazio = usar cores.ini):",
    prompt_placeholder = "Ex: tudo em verde, vintage outono, cyberpunk neon",
    api_label = "Chave API Gemini:",
    api_placeholder = "Cole sua chave da API aqui (AIzaSy...)",
    btn_show = "Mostrar",
    btn_hide = "Ocultar",
    btn_save = "Salvar",
    btn_analyze = "VAMOS NOMEAR!",
    experimental_notice_1 = "isso e um projeto experimental.",
    experimental_notice_2 = "a ia VAI cometer erros.",
    btn_close = "FECHAR",
    btn_cancel = "CANCELAR",
    lbl_analyzing = "Analisando faixas com IA...",
    lbl_completed = "Analise concluida!",
    lbl_error = "Erro no processamento!",
    msg_no_audio = "Nenhuma faixa de áudio elegível.",
    msg_summary = "%d faixas (%d s/ áudio)",
    msg_done = "║  ✓ Concluido: %d aplicada(s)  ✗ %d com erro  ║",
    msg_copied = "Os logs foram copiados para a area de transferencia.",
    msg_sent_console = "Os logs foram enviados para o console do Reaper.",
    msg_empty_api = "Erro: A Chave API do Gemini nao pode ficar em branco.",
    msg_sws_warning = "Para colar com Ctrl+V, é necessária a extensão SWS Extension.",
    opt_quality_high = "alta",
    opt_quality_normal = "normal",
    logs_title = "Logs",
    btn_copy_logs = "Copiar Logs",
    msg_color_created = "⚙  Arquivo de cores criado: ",
    msg_color_edit_hint = "   (Voce pode editar este arquivo para personalizar as cores!)",
    msg_color_loaded = "⚙  Configuracoes de cores carregadas de: ",
    msg_venv_missing = "[AVISO] Ambiente virtual (venv) nao detectado. Usando python global do sistema.",
    msg_venv_hint = "        Certifique-se de ter rodado o setup ou ter as dependencias instaladas.",
    msg_starting = "▶  Iniciando analise de %d faixa(s) com %d thread(s)...",
    msg_waiting = "   Aguarde — o progresso aparece abaixo em tempo real.",
    msg_applying = "◀  Analise concluida! Aplicando nomes e cores no Reaper...",
    msg_api_error = "[ERRO] batch_rename.py nao gerou o arquivo de resultado.",
    msg_api_causes = "Possiveis causas:\n  - setup.bat ainda nao foi rodado nesta pasta (venv/.env faltando)\n  - GEMINI_API_KEY nao configurada no .env\n  - Python nao encontrado no PATH",
    msg_api_causes_mb = "Falha ao rodar a IA. Possiveis causas:\n- setup.bat ainda nao foi rodado nesta pasta (venv/.env faltando)\n- GEMINI_API_KEY nao configurada no .env\n- Python nao encontrado\n\nVeja o console do Reaper para detalhes completos.",
    backend_label = "Backend de Analise:",
    backend_gemini = "Gemini (nuvem, exige chave de API)",
    backend_yamnet = "YamNet (local, sem chave de API)",
    backend_essentia = "Essentia (local, sem chave de API)",
    backend_panns = "PANNs (local, sem chave de API)",
    backend_hybrid_heuristic = "Híbrido Heurística (PANNs + Gemini)",
    backend_hybrid_chaining = "Híbrido Encadeado (Gemini avalia PANNs)",
  }
}

local function t(key)
  return strings[lang][key] or key
end
 -- "config", "analyzing", "completed", "error"
local gui_logs = {}
local DEBUG = false

local function log(msg)
  for line in string.gmatch(tostring(msg) .. "\n", "(.-)\n") do
    table.insert(gui_logs, line)
  end
  while #gui_logs > 500 do
    table.remove(gui_logs, 1)
  end
  if DEBUG then
    reaper.ShowConsoleMsg(tostring(msg) .. "\n")
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


local function read_env_api_key(env_path)
  local f = io.open(env_path, "r")
  if not f then return "" end
  local api_key = ""
  for line in f:lines() do
    local val = line:match("^%s*GEMINI_API_KEY%s*=%s*(.-)%s*$")
    if val then
      api_key = val
      -- remove quotes if any
      api_key = api_key:gsub('^"(.*)"$', "%1"):gsub("^'(.*)'$", "%1")
      break
    end
  end
  f:close()
  return trim(api_key)
end

local function write_env_api_key(env_path, api_key)
  local lines = {}
  local found = false
  local f = io.open(env_path, "r")
  if f then
    for line in f:lines() do
      if line:find("^%s*GEMINI_API_KEY%s*=") then
        table.insert(lines, "GEMINI_API_KEY=" .. api_key)
        found = true
      else
        table.insert(lines, line)
      end
    end
    f:close()
  end

  if not found then
    table.insert(lines, "GEMINI_API_KEY=" .. api_key)
  end

  f = io.open(env_path, "w")
  if f then
    for _, line in ipairs(lines) do
      f:write(line .. "\n")
    end
    f:close()
    return true
  end
  return false
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

local function find_icon(icon_files, category)
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

local function sort_project_tracks(track_categories, track_instruments)
  local total_tracks = reaper.CountTracks(0)
  if total_tracks <= 1 then return end

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

    -- Determine category weight
    local weight = 100
    if category == "guitarra" then
      weight = 10
    elseif category == "teclado" then
      weight = 20
    elseif category == "synth" then
      weight = 30
    elseif category == "cordas" then
      weight = 40
    elseif category == "sopro" then
      weight = 50
    elseif category == "baixo" then
      weight = 60
    elseif category == "bateria" then
      weight = 70
    elseif category == "vocal" then
      weight = 80
    elseif category == "pastas" then
      weight = 90
    elseif category == "efeitos" then
      weight = 95
    else
      weight = 100
    end

    table.insert(list, {
      track = tr,
      guid = guid,
      weight = weight,
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
    -- same category, sort by instrument name
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
local _, script_path = reaper.get_action_context()
local script_dir = script_path:match("^(.*[/\\])")

local os_name = reaper.GetOS()
local is_windows = os_name:find("Win") ~= nil
local sep = is_windows and "\\" or "/"

local env_path = script_dir .. ".env"
local config_path = script_dir .. "reaper_ai_track_namer_colors.ini"

local saved_api_key = read_env_api_key(env_path)
local config_colors, created_new = load_config(config_path)

local only_selected = false
local segment_seconds = 8
local workers = 5
local color_prompt = ""
local entered_key = ""
local script_running = true
local inputs

local function sync_api_key_to_env(api_key)
  local trimmed_key = trim(api_key)
  if trimmed_key == "" then
    return false
  end

  if write_env_api_key(env_path, trimmed_key) then
    saved_api_key = trimmed_key
    return true
  end

  return false
end

local function start_analysis()
  track_info = {} -- Reseta o estado para evitar acúmulo de dados entre execuções
  local config_colors_file = config_path
  if color_prompt ~= "" then
    config_colors_file = script_dir .. "reaper_ai_track_namer_colors_prompt.ini"
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

      track_info[i] = {track = track, name = name, audio = best}

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

  local batch_script = script_dir .. "batch_rename.py"

  local work_dir = reaper.GetResourcePath() .. sep .. "reaper-ai-namer_tmp"
  reaper.RecursiveCreateDirectory(work_dir, 0)

  local stamp = tostring(math.floor(reaper.time_precise() * 1000))
  local manifest_path = work_dir .. sep .. "manifest_" .. stamp .. ".tsv"
  local result_path   = work_dir .. sep .. "result_"   .. stamp .. ".tsv"
  local log_path      = work_dir .. sep .. "log_"      .. stamp .. ".txt"
  local done_path     = work_dir .. sep .. "done_"     .. stamp .. ".flag"

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

  if DEBUG then
    reaper.ShowConsoleMsg("")  -- garante que o console abre
  end
  log("==============================================")
  log("AiNOMEATOR | Gemini Console")
  log("----------------------------------------------")
  log(string.format("Scan    : %d track(s)", n_jobs))
  log(string.format("Threads : %d", workers))
  log(string.format("Mode    : %s", analysis_mode == "detailed" and "Detailed" or "Fast"))
  log("Output  : track names, colors, icons")
  log("----------------------------------------------")
  if created_new then
    log("Color file created: " .. config_path)
    log("You can edit this file to customize colors.")
  else
    log("Color settings loaded from: " .. config_path)
  end
  if not venv_exists then
    log("[WARNING] Virtual environment not detected. Using system Python.")
    log("Run setup if you want an isolated runtime.")
  end
  log(string.format("Starting analysis: %d track(s), %d thread(s)...", n_jobs, workers))
  log("Progress will appear below in real time.\n")

  local local_threads = tonumber(inputs[4].val) or 1
  local python_args = string.format(
    '-u "%s" "%s" "%s" --workers %d --segment-seconds %.2f --done-flag "%s" --config-path "%s" --panns-threads %d',
    batch_script, manifest_path, result_path, workers, segment_seconds, done_path, config_colors_file, local_threads
  )

  if analysis_mode == "detailed" then
    python_args = python_args .. ' --quality ' .. t('opt_quality_high')
  else
    python_args = python_args .. ' --quality ' .. t('opt_quality_normal')
  end

  python_args = python_args .. ' --output-language ' .. sanitize_shell_arg(lang)
  python_args = python_args .. ' --backend ' .. sanitize_shell_arg(backend)

  if color_prompt ~= "" then
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

  os.execute(launch_cmd)

  local log_read_pos = 0    -- byte offset ate onde ja lemos o log
  local poll_start   = reaper.time_precise()
  local TIMEOUT_SEC  = 300  -- 5 minutos: timeout de seguranca

  local apply_results
  local poll

  apply_results = function()
    if not file_exists(result_path) then
      log("\n[ERROR] batch_rename.py did not generate the result file.")
      log("Possible causes:")
      log("  - setup.bat has not been run in this folder")
      log("  - GEMINI_API_KEY is not configured in .env")
      log("  - Python was not found in PATH")
      reaper.MB("Failed to run AI. See the Reaper console for details.", "AiNOMEATOR", 0)
      return
    end

    log("\n[OK] Analysis completed. Applying names and colors in Reaper...")
    config_colors = load_config(config_colors_file)

    local icon_files = list_icon_files(sep)

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    local applied, failed = 0, 0
    local processed_tracks = {}
    local track_categories = {}
    local track_instruments = {}

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
          if status == "ok" and instrument and instrument ~= "" then
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

            local icon_path = find_icon(icon_files, category)
            if icon_path then
              reaper.GetSetMediaTrackInfo_String(info.track, "P_ICON", icon_path, true)
            end

            log(string.format("   [✓] Faixa %d: %s -> Cor: %s (RGB: %d,%d,%d)", idx, newname, col_key, col[1], col[2], col[3]))
            processed_tracks[idx] = true
            applied = applied + 1
          else
            failed = failed + 1
            log(string.format("  [✗] track %d '%s': %s", idx, info.name, errmsg or "falha desconhecida"))
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
        log(string.format("   [-] Faixa %d: %s -> Cor: %s (RGB: %d,%d,%d) (Utilidade/Ignorada)", idx, track_name, col_key, col[1], col[2], col[3]))
      end
    end

    if sort_tracks then
      log("\n[OK] Reordenando as tracks por familia de instrumentos...")
      sort_project_tracks(track_categories, track_instruments)
    end

    reaper.PreventUIRefresh(-1)
    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.Undo_EndBlock("IA: renomear/colorir tracks (AiNOMEATOR)", -1)

    log("")
    log(string.format("╔══════════════════════════════════════════════╗"))
    log(string.format(t("msg_done"), applied, failed))
    log(string.format("╚══════════════════════════════════════════════╝"))


  end

  poll = function()
    if not script_running then
      return
    end

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
  
  -- Checkboxes
  only_selected = { x = 30, y = 110, w = 125, h = 20, cb_x = 30, cb_y = 115, cb_size = 18 },
  sort_tracks = { x = 165, y = 110, w = 125, h = 20, cb_x = 165, cb_y = 115, cb_size = 18 },
  
  -- Rádios do modo de análise
  mode_fast = { x = 30, y = 165, w = 260, h = 20, cb_x = 30, cb_y = 170, cb_size = 18 },
  mode_detailed = { x = 30, y = 190, w = 260, h = 20, cb_x = 30, cb_y = 195, cb_size = 18 },
  
  -- Rádios do backend de análise
  backend_label_y = 230,
  backend_start_y = 252,
  backend_spacing_y = 25,
  backend_cb_x = 30,
  backend_cb_size = 18,
  
  -- Botões de API (deslocados +50px)
  api_eye = { x = 210, y = 520, w = 40, h = 30 },
  api_save = { x = 250, y = 520, w = 40, h = 30 },
  
  -- Outros Botões (deslocados +50px)
  copy_logs = { x = 190, y = 105, w = 100, h = 24 },
  analyze = { x = 30, y = 585, w = 260, h = 36 },
  close = { x = 30, y = 655, w = 260, h = 36 }
}

local backend_options = {
  { key = "gemini",             label_key = "backend_gemini" },
  { key = "panns",              label_key = "backend_panns" },
  { key = "hybrid_heuristic",   label_key = "backend_hybrid_heuristic" },
  { key = "hybrid_chaining",    label_key = "backend_hybrid_chaining" },
}

local function open_url(url)
  local os_name = reaper.GetOS()
  if os_name:find("Win") then
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

local function load_logo()
  local logo_path = script_dir .. "ainomeator_logo.png"
  local res = gfx.loadimg(logo_buffer, logo_path)
  if res >= 0 then
    logo_w, logo_h = gfx.getimgdim(logo_buffer)
    logo_loaded = true
  end
end

only_selected = false
sort_tracks = (saved_sort_tracks == "true")
analysis_mode = "detailed"
local show_api_key = false

inputs = {
  { label = t("thread_label"), val = "1", placeholder = "1-20", is_numeric = true, limit = 2, x = 30, y = 390, w = 110, h = 30 },
  { label = t("prompt_label"), val = "", placeholder = t("prompt_placeholder"), is_numeric = false, limit = 100, x = 30, y = 455, w = 260, h = 30 },
  { label = t("api_label"), val = saved_api_key, placeholder = t("api_placeholder"), is_numeric = false, is_password = true, limit = 200, x = 30, y = 520, w = 170, h = 30 },
  { label = t("local_thread_label"), val = saved_panns_threads, placeholder = "1-16", is_numeric = true, limit = 2, x = 180, y = 390, w = 110, h = 30 }
}

local function refresh_language_labels()
  inputs[1].label = t("thread_label")
  inputs[2].label = t("prompt_label")
  inputs[2].placeholder = t("prompt_placeholder")
  inputs[3].label = t("api_label")
  inputs[3].placeholder = t("api_placeholder")
  inputs[4].label = t("local_thread_label")
end

local function set_language(new_lang)
  if lang == new_lang then
    return false
  end

  lang = new_lang
  reaper.SetExtState("AiNOMEATOR", "language", new_lang, true)
  refresh_language_labels()
  return true
end

refresh_language_labels()

local function sanitize_thread_input(value)
  local digits = tostring(value or ""):gsub("%D", "")
  if digits == "" then
    return ""
  end

  local threads = tonumber(digits) or 1
  if threads < 1 then threads = 1 end
  if threads > 20 then threads = 20 end
  return tostring(threads)
end

local function sanitize_local_thread_input(value)
  local digits = tostring(value or ""):gsub("%D", "")
  if digits == "" then
    return ""
  end

  local threads = tonumber(digits) or 1
  if threads < 1 then threads = 1 end
  if threads > 16 then threads = 16 end
  return tostring(threads)
end

local focused_input = nil
local last_mouse_cap = 0

local function draw_logs(x, y, w, h)
  gfx.setfont(1, "Segoe UI", 11)
  gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
  
  local line_height = 16
  local max_lines = math.max(0, math.floor((h - 8) / line_height))
  
  -- Prepara as linhas formatadas (com quebra se passarem de w)
  local formatted_lines = {}
  for _, raw_line in ipairs(gui_logs) do
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
  gfx.setfont(1, "Segoe UI", 10)
  local c_text = t("btn_copy_logs")
  local c_tw, c_th = gfx.measurestr(c_text)
  gfx.x = btn.x + (btn.w - c_tw)/2
  gfx.y = btn.y + (btn.h - c_th)/2
  gfx.drawstr(c_text)
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

local function draw_gui()
  -- Fundo escuro (#1E1E1E)
  gfx.r, gfx.g, gfx.b = 0.12, 0.12, 0.12
  gfx.rect(0, 0, gfx.w, gfx.h, 1)

  -- Titulo (Logo ou Fallback Text)
  if logo_loaded then
    local target_h = 80
    local target_w = (logo_w / logo_h) * target_h
    local target_x = (gfx.w - target_w) / 2
    gfx.blit(logo_buffer, 1, 0, 0, 0, logo_w, logo_h, target_x, 10, target_w, target_h)
  else
    gfx.setfont(1, "Segoe UI", 18, 98) -- Bold
    gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    gfx.x, gfx.y = 30, 30
    gfx.drawstr("AiNOMEATOR")
  end

  -- Linha divisoria
  gfx.setfont(1, "Segoe UI", 12)
  gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
  gfx.line(30, 100, 290, 100)

  if gui_state == "config" then
    -- Radio de idioma no topo
    local en = layout.lang_en
    local pt = layout.lang_pt
    gfx.setfont(1, "Segoe UI", 10)
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

    -- Checkbox "Apenas faixas selecionadas"
    local opt = layout.only_selected
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
    
    -- Linha divisória
    gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
    gfx.line(30, 142, 290, 142)

    -- Modo de Análise Label
    gfx.setfont(1, "Segoe UI", 11, 98) -- Bold
    gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
    gfx.x, gfx.y = 30, 150
    gfx.drawstr(t("analysis_mode"))
    gfx.setfont(1, "Segoe UI", 11)

    -- Radio 1: "Análise rápida de pequena amostra"
    local r1 = layout.mode_fast
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
    gfx.line(30, 222, 290, 222)

    -- Backend de Análise Label
    gfx.setfont(1, "Segoe UI", 11, 98) -- Bold
    gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
    gfx.x, gfx.y = 30, layout.backend_label_y
    gfx.drawstr(t("backend_label"))
    gfx.setfont(1, "Segoe UI", 11)

    local cb_x, cb_size = layout.backend_cb_x, layout.backend_cb_size
    for i, opt in ipairs(backend_options) do
      local opt_y = layout.backend_start_y + (i - 1) * layout.backend_spacing_y
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
    gfx.line(30, 370, 290, 370)

    -- Campos de Texto
    for i, inp in ipairs(inputs) do
      -- Label
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65
      gfx.x, gfx.y = inp.x, inp.y - 20
      gfx.drawstr(inp.label)

      -- Caixa de input
      gfx.r, gfx.g, gfx.b = 0.17, 0.17, 0.17
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
      if inp.val == "" and focused_input ~= i then
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

    -- Botao Mostrar/Ocultar para a API Key
    local eye = layout.api_eye
    local mouse_over_eye = in_rect(eye.x, eye.y, eye.w, eye.h)
    if mouse_over_eye then
      gfx.r, gfx.g, gfx.b = 0.25, 0.25, 0.25
    else
      gfx.r, gfx.g, gfx.b = 0.18, 0.18, 0.18
    end
    gfx.rect(eye.x, eye.y, eye.w, eye.h, 1)
    gfx.r, gfx.g, gfx.b = 0.27, 0.27, 0.27
    gfx.rect(eye.x, eye.y, eye.w, eye.h, 0)
    gfx.r, gfx.g, gfx.b = 0.8, 0.8, 0.8
    local eye_text = show_api_key and t("btn_hide") or t("btn_show")
    local etw, eth = gfx.measurestr(eye_text)
    gfx.x = eye.x + (eye.w - etw)/2
    gfx.y = eye.y + (eye.h - eth)/2
    gfx.drawstr(eye_text)

    -- Botao Salvar para a API Key
    local save = layout.api_save
    local mouse_over_save = in_rect(save.x, save.y, save.w, save.h)
    if mouse_over_save then
      gfx.r, gfx.g, gfx.b = 0.42, 0.24, 0.24
    else
      gfx.r, gfx.g, gfx.b = 0.34, 0.18, 0.18
    end
    gfx.rect(save.x, save.y, save.w, save.h, 1)
    gfx.r, gfx.g, gfx.b = 0.46, 0.28, 0.28
    gfx.rect(save.x, save.y, save.w, save.h, 0)
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.setfont(1, "Segoe UI", 8, 98)
    local stw, sth = gfx.measurestr(t("btn_save"))
    gfx.x = save.x + (save.w - stw)/2
    gfx.y = save.y + (save.h - sth)/2
    gfx.drawstr(t("btn_save"))

    -- Info de resumo econômico/faixas dinâmico
    local n_jobs, n_skipped = update_analysis_summary_cached()
    gfx.setfont(1, "Segoe UI", 11)
    if n_jobs == 0 then
      gfx.r, gfx.g, gfx.b = 0.8, 0.6, 0.2 -- Amarelo/Dourado suave
      gfx.x, gfx.y = 30, 558
      gfx.drawstr(t("msg_no_audio"))
    else
      gfx.r, gfx.g, gfx.b = 0.65, 0.65, 0.65 -- Cinza suave
      gfx.x, gfx.y = 30, 562
      local msg = string.format(t("msg_summary"), n_jobs, n_skipped)
      gfx.drawstr(msg)
    end

    -- Botao Analisar (Coral) - Centrado de largura total
    local btn = layout.analyze
    local mouse_over_run = in_rect(btn.x, btn.y, btn.w, btn.h)
    if mouse_over_run then
      gfx.r, gfx.g, gfx.b = 0.65, 0.1, 0.15
    else
      gfx.r, gfx.g, gfx.b = 0.53, 0.0, 0.08
    end
    gfx.rect(btn.x, btn.y, btn.w, btn.h, 1)
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.setfont(1, "Segoe UI", 12, 98) -- Bold
    local tw, th = gfx.measurestr(t("btn_analyze"))
    gfx.x = btn.x + (btn.w - tw)/2
    gfx.y = btn.y + (btn.h - th)/2
    gfx.drawstr(t("btn_analyze"))

    -- Aviso experimental
    gfx.setfont(1, "Segoe UI", 12)
    gfx.r, gfx.g, gfx.b = 0.33, 0.33, 0.33
    local note1 = t("experimental_notice_1")
    local note2 = t("experimental_notice_2")
    local note1_w = gfx.measurestr(note1)
    local note2_w = gfx.measurestr(note2)
    gfx.x = (gfx.w - note1_w) / 2
    gfx.y = 635
    gfx.drawstr(note1)
    gfx.x = (gfx.w - note2_w) / 2
    gfx.y = 646
    gfx.drawstr(note2)

    -- Creditos visiveis
    gfx.setfont(1, "Segoe UI", 10)
    local credit_text = "by jasko"
    local cr_w, cr_h = gfx.measurestr(credit_text)
    local cr_x = (gfx.w - cr_w) / 2
    local cr_y = 666
    gfx.r, gfx.g, gfx.b = 1.0, 1.0, 1.0
    gfx.x = cr_x
    gfx.y = cr_y
    gfx.drawstr(credit_text)

  elseif gui_state == "analyzing" then
    -- Subtitulo
    gfx.setfont(1, "Segoe UI", 12)
    gfx.r, gfx.g, gfx.b = 0.85, 0.85, 0.85
    gfx.x, gfx.y = 30, 110
    gfx.drawstr(t("lbl_analyzing"))

    -- Animacao simples
    local dot_count = math.floor(reaper.time_precise() * 2) % 4
    local dots = string.rep(".", dot_count)
    gfx.drawstr(dots)

    -- Caixa de Logs
    local box_x, box_y, box_w, box_h = 30, 135, 260, 455
    gfx.r, gfx.g, gfx.b = 0.08, 0.08, 0.08
    gfx.rect(box_x, box_y, box_w, box_h, 1)
    gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
    gfx.rect(box_x, box_y, box_w, box_h, 0)

    draw_logs(box_x + 10, box_y + 10, box_w - 20, box_h - 20)
    draw_copy_button()

  elseif gui_state == "completed" or gui_state == "error" then
    -- Subtitulo de Sucesso ou Erro
    gfx.setfont(1, "Segoe UI", 13, 98) -- Bold
    gfx.x, gfx.y = 30, 110
    if gui_state == "completed" then
      gfx.r, gfx.g, gfx.b = 0.3, 0.8, 0.3 -- Verde
      gfx.drawstr(t("lbl_completed"))
    else
      gfx.r, gfx.g, gfx.b = 0.9, 0.3, 0.3 -- Vermelho
      gfx.drawstr(t("lbl_error"))
    end

    -- Caixa de Logs
    local box_x, box_y, box_w, box_h = 30, 135, 260, 455
    gfx.r, gfx.g, gfx.b = 0.08, 0.08, 0.08
    gfx.rect(box_x, box_y, box_w, box_h, 1)
    gfx.r, gfx.g, gfx.b = 0.2, 0.2, 0.2
    gfx.rect(box_x, box_y, box_w, box_h, 0)

    draw_logs(box_x + 10, box_y + 10, box_w - 20, box_h - 20)
    draw_copy_button()

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
    gfx.setfont(1, "Segoe UI", 12, 98) -- Bold
    local tw, th = gfx.measurestr(t("btn_close"))
    gfx.x = btn.x + (btn.w - tw)/2
    gfx.y = btn.y + (btn.h - th)/2
    gfx.drawstr(t("btn_close"))
  end

  gfx.update()
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

      -- Clique nos campos de texto
      local clicked_input = false
      for i, inp in ipairs(inputs) do
        if in_rect(inp.x, inp.y, inp.w, inp.h) then
          focused_input = i
          clicked_input = true
          break
        end
      end
      if not clicked_input then
        focused_input = nil
      end

      -- Clique no botao Mostrar/Ocultar
      local eye = layout.api_eye
      if in_rect(eye.x, eye.y, eye.w, eye.h) then
        show_api_key = not show_api_key
      end

      -- Clique no botao Salvar API Key
      local save = layout.api_save
      if in_rect(save.x, save.y, save.w, save.h) then
        if not sync_api_key_to_env(inputs[3].val) then
          reaper.MB("Failed to save the Gemini API key to .env.", "AiNOMEATOR", 0)
        end
        return "redraw"
      end

      -- Clique no botao ANALISAR
      local btn = layout.analyze
      if in_rect(btn.x, btn.y, btn.w, btn.h) then
        return "run"
      end

      -- Clique nos creditos "by jasko"
      gfx.setfont(1, "Segoe UI", 10)
      local cr_w, cr_h = gfx.measurestr("by jasko")
      local cr_x = (gfx.w - cr_w) / 2
      local cr_y = 616
      if in_rect(cr_x, cr_y, cr_w, cr_h) then
        open_url("https://jasko.dev")
        return "redraw"
      end

    elseif gui_state == "analyzing" or gui_state == "completed" or gui_state == "error" then
      -- Clique no botao de copiar logs
      local btn = layout.copy_logs
      if in_rect(btn.x, btn.y, btn.w, btn.h) then
        if reaper.CF_SetClipboard then
          reaper.CF_SetClipboard(table.concat(gui_logs, "\n"))
          reaper.MB(t("msg_copied"), "AiNOMEATOR", 0)
        else
          reaper.ShowConsoleMsg(table.concat(gui_logs, "\n") .. "\n")
          reaper.MB(t("msg_sent_console"), "AiNOMEATOR", 0)
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
    elseif char == 22 then -- Ctrl + V (Colar se SWS disponivel)
      if reaper.CF_GetClipboard then
        local clip = reaper.CF_GetClipboard("")
        if clip and clip ~= "" then
          clip = clip:gsub("[%r%n\t]", "")
          if inp.is_numeric then
            clip = sanitize_thread_input(clip)
          end
          inp.val = inp.val .. clip
          if inp.is_numeric then
            inp.val = sanitize_thread_input(inp.val)
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
          inp.val = sanitize_thread_input(inp.val)
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

  -- Travar o resize
  if gfx.w ~= 320 or gfx.h ~= 710 then
    gfx.init("AiNOMEATOR", 320, 710, 0, gfx.x, gfx.y)
  end

  draw_gui()
  local status = update_gui()
  if status == "run" then
    
    only_selected = (only_selected == true)

    workers = tonumber(sanitize_thread_input(inputs[1].val)) or 1
    if workers < 1 then workers = 1
    elseif workers > 20 then workers = 20 end
    inputs[1].val = tostring(workers)

    local local_threads = tonumber(sanitize_local_thread_input(inputs[4].val)) or 1
    if local_threads < 1 then local_threads = 1
    elseif local_threads > 16 then local_threads = 16 end
    inputs[4].val = tostring(local_threads)
    reaper.SetExtState("AiNOMEATOR", "panns_threads", inputs[4].val, true)

    color_prompt = trim(inputs[2].val)
    entered_key = trim(inputs[3].val)

    if entered_key == "" then
      reaper.MB(t("msg_empty_api"), "AiNOMEATOR", 0)
      reaper.defer(run_gui_loop)
      return
    end

    if not sync_api_key_to_env(entered_key) then
      reaper.MB("Failed to save the Gemini API key to .env.", "AiNOMEATOR", 0)
      reaper.defer(run_gui_loop)
      return
    end

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
local win_w, win_h = 320, 710
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
