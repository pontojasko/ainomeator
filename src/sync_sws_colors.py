import os
import re
import shutil
import sys

def log(msg):
    print(msg)

import subprocess

def is_reaper_running():
    # Verifica se o processo reaper está ativo no Windows ou Unix
    try:
        if sys.platform.startswith("win"):
            # Executa de forma segura sem abrir shell de comando visível
            res = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq reaper.exe'],
                capture_output=True, text=True, timeout=10, check=False
            )
            return "reaper.exe" in res.stdout.lower()
        else:
            res = subprocess.run(
                ['pgrep', '-x', 'REAPER'],
                capture_output=True, text=True, timeout=10, check=False
            )
            return res.returncode == 0
    except Exception:
        return False

def hex_to_bgr_int(hex_str):
    hex_str = hex_str.lstrip('#')
    if len(hex_str) == 3:
        hex_str = ''.join(c*2 for c in hex_str)
    if len(hex_str) != 6:
        return 9671574 # Cor padrão cinza se der erro
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return b * 65536 + g * 256 + r

def load_colors_ini(path):
    defaults = {
        "vocal_principal": "#E05A47",
        "backing_vocals": "#D38B80",
        "bateria": "#3B6E8C",
        "percussao": "#4A9F9B",
        "baixo": "#6D557A",
        "guitarra_eletrica": "#4A8F62",
        "violao": "#7F9C62",
        "teclado": "#E0923E",
        "synth": "#DCAE3B",
        "cordas": "#A3704C",
        "sopros": "#A64B75",
        "efeitos": "#708090",
        "pastas": "#3E3E3E",
        "outro": "#969696"
    }
    
    colors = {k: hex_to_bgr_int(v) for k, v in defaults.items()}
    
    if not os.path.exists(path):
        return colors

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Remove comentários
                line = re.sub(r'[#;].*', '', line).strip()
                if not line:
                    continue
                parts = line.split("=", 1)
                if len(parts) == 2:
                    key = parts[0].strip().lower()
                    val = parts[1].strip()
                    if val.startswith("#"):
                        colors[key] = hex_to_bgr_int(val)
                    else:
                        # R,G,B format
                        match = re.match(r'^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$', val)
                        if match:
                            r, g, b = map(int, match.groups())
                            colors[key] = b * 65536 + g * 256 + r
    except Exception as e:
        log(f"Aviso ao ler cores customizadas: {e}. Usando padrões.")
        
    return colors

def main():
    log("=========================================")
    log(" AiNOMEATOR - Sincronizar SWS ")
    log("=========================================\n")
    
    # 1. Verifica se o REAPER está aberto
    if is_reaper_running():
        log("❌ ERRO: O REAPER está aberto!")
        log("Por favor, FECHE O REAPER completamente antes de prosseguir.")
        log("Se o REAPER estiver aberto, ele irá sobrescrever a configuração ao fechar.")
        input("\nPressione Enter para fechar...")
        sys.exit(1)
        
    # 2. Localiza a pasta de recursos do REAPER (%APPDATA%\REAPER)
    appdata = os.getenv('APPDATA')
    if not appdata:
        log("❌ ERRO: Não foi possível obter o caminho %APPDATA% no seu sistema.")
        input("\nPressione Enter para fechar...")
        sys.exit(1)
        
    reaper_resource_dir = os.path.join(appdata, 'REAPER')
    if not os.path.exists(reaper_resource_dir):
        log(f"❌ ERRO: A pasta de recursos do REAPER não foi encontrada em: {reaper_resource_dir}")
        input("\nPressione Enter para fechar...")
        sys.exit(1)
        
    sws_ini_path = os.path.join(reaper_resource_dir, 'sws-autocoloricon.ini')
    sws_ini_backup = os.path.join(reaper_resource_dir, 'sws-autocoloricon_backup.ini')
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    colors_ini_path = os.path.join(parent_dir, 'colors.ini')
    if not os.path.exists(colors_ini_path):
        colors_ini_path = os.path.join(script_dir, 'colors.ini')
    colors = load_colors_ini(colors_ini_path)
    
    # 4. Define as regras da IA com prioridade (do mais específico ao mais geral)
    ia_rules = [
        # Vocais
        {"filter": "backing vocals", "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "backing vocal",  "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "back vocals",    "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "back vocal",     "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "backing",        "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "bgv",            "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "coral",          "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "coro",           "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "dobra",          "color_key": "backing_vocals",  "icon": "vocal"},
        {"filter": "vocal",          "color_key": "vocal_principal", "icon": "vocal"},
        {"filter": "vox",            "color_key": "vocal_principal", "icon": "vocal"},
        {"filter": "voz",            "color_key": "vocal_principal", "icon": "vocal"},

        # Baixo
        {"filter": "baixo",          "color_key": "baixo",           "icon": "bass"},
        {"filter": "bass",           "color_key": "baixo",           "icon": "bass"},
        {"filter": "contrabaixo",    "color_key": "baixo",           "icon": "bass"},

        # Guitarras e Violões
        {"filter": "violao acustico", "color_key": "violao",          "icon": "guitar"},
        {"filter": "violão acústico", "color_key": "violao",          "icon": "guitar"},
        {"filter": "violao",         "color_key": "violao",          "icon": "guitar"},
        {"filter": "violão",         "color_key": "violao",          "icon": "guitar"},
        {"filter": "acoustic",       "color_key": "violao",          "icon": "guitar"},
        {"filter": "guitarra",       "color_key": "guitarra_eletrica", "icon": "guitar"},
        {"filter": "guitar",         "color_key": "guitarra_eletrica", "icon": "guitar"},
        {"filter": "gtr",            "color_key": "guitarra_eletrica", "icon": "guitar"},

        # Bateria e Percussão
        {"filter": "bateria",        "color_key": "bateria",         "icon": "drum"},
        {"filter": "drums",          "color_key": "bateria",         "icon": "drum"},
        {"filter": "drum",           "color_key": "bateria",         "icon": "drum"},
        {"filter": "kick",           "color_key": "bateria",         "icon": "drum"},
        {"filter": "snare",          "color_key": "bateria",         "icon": "drum"},
        {"filter": "perc",           "color_key": "percussao",       "icon": "perc"},
        {"filter": "percussao",      "color_key": "percussao",       "icon": "perc"},
        {"filter": "percussão",      "color_key": "percussao",       "icon": "perc"},
        {"filter": "shaker",         "color_key": "percussao",       "icon": "perc"},
        {"filter": "pandeiro",       "color_key": "percussao",       "icon": "perc"},

        # Teclados, Pianos e Synths
        {"filter": "piano",          "color_key": "teclado",         "icon": "piano"},
        {"filter": "teclado",        "color_key": "teclado",         "icon": "piano"},
        {"filter": "keyboard",       "color_key": "teclado",         "icon": "piano"},
        {"filter": "keys",           "color_key": "teclado",         "icon": "piano"},
        {"filter": "synth",          "color_key": "synth",           "icon": "synth"},
        {"filter": "sintetizador",   "color_key": "synth",           "icon": "synth"},
        {"filter": "pad",            "color_key": "synth",           "icon": "synth"},

        # Cordas
        {"filter": "cordas",         "color_key": "cordas",          "icon": "string"},
        {"filter": "strings",        "color_key": "cordas",          "icon": "string"},
        {"filter": "violin",         "color_key": "cordas",          "icon": "string"},
        {"filter": "cello",          "color_key": "cordas",          "icon": "string"},

        # Sopros
        {"filter": "sopros",         "color_key": "sopros",          "icon": "wind"},
        {"filter": "sopro",          "color_key": "sopros",          "icon": "wind"},
        {"filter": "horns",          "color_key": "sopros",          "icon": "wind"},
        {"filter": "brass",          "color_key": "sopros",          "icon": "wind"},
        {"filter": "sax",            "color_key": "sopros",          "icon": "wind"},

        # Utilitários / Efeitos
        {"filter": "fx",             "color_key": "efeitos",         "icon": ""},
        {"filter": "reverb",         "color_key": "efeitos",         "icon": ""},
        {"filter": "delay",          "color_key": "efeitos",         "icon": ""},
        {"filter": "aux",            "color_key": "efeitos",         "icon": ""},
        {"filter": "bus",            "color_key": "pastas",          "icon": ""},
    ]
    
    # 5. Carrega regras antigas do SWS se o arquivo já existir
    existing_rules = []
    autocolor_enable = "1"
    autoicon_enable = "1"
    autolayout_enable = "0"
    automarker_enable = "0"
    autoregion_enable = "0"
    
    if os.path.exists(sws_ini_path):
        log(f"Encontrado arquivo SWS antigo. Fazendo backup para: {sws_ini_backup}")
        shutil.copy2(sws_ini_path, sws_ini_backup)
        
        try:
            with open(sws_ini_path, "r", encoding="utf-8", errors="ignore") as f_old:
                for line in f_old:
                    line_strip = line.strip()
                    if not line_strip:
                        continue
                    parts = line_strip.split("=", 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip()
                        if key == "AutoColorEnable": autocolor_enable = val
                        elif key == "AutoIconEnable": autoicon_enable = val
                        elif key == "AutoLayoutEnable": autolayout_enable = val
                        elif key == "AutoColorMarkerEnable": automarker_enable = val
                        elif key == "AutoColorRegionEnable": autoregion_enable = val
                        elif key.startswith("AutoColor "):
                            existing_rules.append(val)
        except Exception as e:
            log(f"Aviso ao ler regras SWS antigas: {e}. Prosseguindo sem importá-las.")

    # 6. Mescla as regras da IA (topo/prioridade máxima) com as regras antigas do usuário
    final_rules = []
    
    # Insere regras da IA
    for r in ia_rules:
        color_val = colors.get(r["color_key"], colors["outro"])
        # Formato da linha: Type Filter Color Icon Layout
        rule_str = f'0 "{r["filter"]}" {color_val} "{r["icon"]}" ""'
        final_rules.append(rule_str)
        
    # Insere regras antigas, pulando as duplicadas
    for ext_rule in existing_rules:
        # Extrai o filtro da regra antiga
        ext_filter_match = re.match(r'^\d+\s+"([^"]+)"', ext_rule)
        if not ext_filter_match:
            ext_filter_match = re.match(r'^\d+\s+([^\s]+)', ext_rule)
            
        ext_filter = ext_filter_match.group(1) if ext_filter_match else None
        
        is_duplicate = False
        if ext_filter:
            for r in ia_rules:
                if r["filter"].lower() == ext_filter.lower():
                    is_duplicate = True
                    break
                    
        if not is_duplicate:
            final_rules.append(ext_rule)
            
    # 7. Escreve de volta no sws-autocoloricon.ini real do REAPER
    try:
        with open(sws_ini_path, "w", encoding="utf-8") as f_out:
            f_out.write("[SWS]\n")
            f_out.write(f"AutoColorEnable={autocolor_enable}\n")
            f_out.write(f"AutoColorMarkerEnable={automarker_enable}\n")
            f_out.write(f"AutoColorRegionEnable={autoregion_enable}\n")
            f_out.write(f"AutoIconEnable={autoicon_enable}\n")
            f_out.write(f"AutoLayoutEnable={autolayout_enable}\n")
            f_out.write(f"AutoColorCount={len(final_rules)}\n")
            
            for idx, rule in enumerate(final_rules, start=1):
                f_out.write(f"AutoColor {idx}={rule}\n")
                
        log("\n✅ SUCESSO! Configurações do SWS Auto Color aplicadas diretamente!")
        log(f"Salvo em: {sws_ini_path}")
        log("\nAgora você já pode abrir o REAPER.")
        log("Sempre que você renomear uma track para 'vocal', 'baixo', 'violao', etc.,")
        log("as cores e os ícones serão alterados automaticamente em tempo real.")
    except Exception as e:
        log(f"\n❌ ERRO ao salvar as configurações: {e}")
        
    input("\nPressione Enter para fechar...")

if __name__ == "__main__":
    main()
