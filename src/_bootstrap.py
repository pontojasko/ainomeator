"""
_bootstrap.py

Inicializacao centralizada do backend AiNOMEATOR.

Importar este modulo no inicio de qualquer script Python do projeto garante:
  - stdout configurado para UTF-8 + line-buffered (1x por processo)
  - variaveis de ambiente carregadas do .env local e do pai (1x por processo)
  - constantes compartilhadas disponiveis em um unico lugar

Uso:
    from _bootstrap import configure_stdout, load_env, CATEGORIAS_VALIDAS
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Constantes compartilhadas entre modulos
# ---------------------------------------------------------------------------

# Vocabulario fechado de categorias validas — deve coincidir com o que o Gemini
# e os backends locais retornam, e com o que o Lua espera no result.tsv.
CATEGORIAS_VALIDAS: tuple[str, ...] = (
    "vocal", "guitarra", "baixo", "bateria",
    "teclado", "synth", "sopro", "cordas", "outro",
)

# Marcadores de erros transitorios da API Gemini (503, 429, timeouts).
# Usados em classify_track.py e batch_rename.py para decidir se vale re-tentar.
ERROS_TRANSITORIOS: tuple[str, ...] = (
    "503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DeadlineExceeded", "timeout",
)

# ---------------------------------------------------------------------------
# stdout
# ---------------------------------------------------------------------------

_stdout_configured = False
_stdout_lock = threading.Lock()


def configure_stdout() -> None:
    """Reconfigura stdout para UTF-8 line-buffered (chamada segura 1x por processo).

    Necessario no Windows, onde o locale padrao (CP1252) causa UnicodeEncodeError
    ao redirecionar stdout com > em scripts chamados pelo Reaper via CMD.
    """
    global _stdout_configured
    if _stdout_configured:
        return
    with _stdout_lock:
        if _stdout_configured:
            return
        try:
            sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
        except AttributeError:
            # Python < 3.7 nao tem reconfigure(); -u na linha de comando supre.
            pass
        _stdout_configured = True


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

_env_loaded = False
_env_lock = threading.Lock()


def load_env() -> None:
    """Carrega variaveis de ambiente do .env local (src/) e do pai (raiz do projeto).

    Chamada segura multiplas vezes — o carregamento real acontece apenas uma vez.
    """
    global _env_loaded
    if _env_loaded:
        return
    with _env_lock:
        if _env_loaded:
            return
        try:
            from dotenv import load_dotenv
            script_dir = os.path.dirname(os.path.abspath(__file__))
            load_dotenv(os.path.join(script_dir, ".env"))
            parent_env = os.path.join(os.path.dirname(script_dir), ".env")
            if os.path.exists(parent_env):
                load_dotenv(parent_env)
        except ImportError:
            pass  # python-dotenv e opcional para quem usa variaveis de ambiente direto
        _env_loaded = True


# ---------------------------------------------------------------------------
# Verificacao de dependencias
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("dotenv",           "python-dotenv"),
    ("google.genai",     "google-genai"),
    ("numpy",            "numpy"),
    ("soundfile",        "soundfile"),
    ("panns_inference",  "panns-inference"),
    ("torch",            "torch"),
    ("torch_directml",   "torch-directml"),
    ("soxr",             "soxr"),
    ("scipy",            "scipy"),
)


def check_dependencies(packages: tuple[tuple[str, str], ...] | None = None) -> list[str]:
    """Verifica quais pacotes estao ausentes e retorna uma lista de nomes pip.

    Args:
        packages: sequencia de (module_name, pip_name). Se None, usa REQUIRED_PACKAGES.

    Returns:
        Lista de nomes pip que nao estao instalados. Lista vazia = tudo ok.
    """
    packages = packages or REQUIRED_PACKAGES
    missing: list[str] = []
    for module_name, pip_name in packages:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)
    return missing


def abort_if_missing(packages: tuple[tuple[str, str], ...] | None = None) -> None:
    """Checa dependencias e encerra o processo com mensagem clara se alguma faltar."""
    missing = check_dependencies(packages)
    if not missing:
        return
    configure_stdout()
    print("\n" + "=" * 60)
    print("[ERRO] Dependencias do Python ausentes / Missing Python dependencies!")
    print("=" * 60)
    print("As seguintes bibliotecas necessarias nao estao instaladas:")
    for pkg in missing:
        print(f"  - {pkg}")
    print("\nPara corrigir, execute o arquivo 'setup.bat' na pasta do projeto.")
    print("Please run 'setup.bat' in the project directory to install dependencies.")
    print("=" * 60 + "\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers de uso geral
# ---------------------------------------------------------------------------

def safe_float(value: Any, default: float = 0.5) -> float:
    """Converte value para float de forma segura, retornando default em caso de falha."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
