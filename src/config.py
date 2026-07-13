"""
config.py — constantes e env loading centralizados.
Importe daqui em vez de repetir load_dotenv() em cada módulo.
"""
import os
from dotenv import load_dotenv

_env_loaded = False

def load_env():
    """Idempotente: carrega .env uma única vez por processo."""
    global _env_loaded
    if _env_loaded:
        return
    load_dotenv()
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_env = os.path.join(os.path.dirname(_script_dir), ".env")
    if os.path.exists(parent_env):
        load_dotenv(parent_env)
    _env_loaded = True

# Constantes
DEFAULT_SEGMENT_SECONDS = 8
DEFAULT_WORKERS = 5
POLL_INTERVAL_SECONDS = 0.25
DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]

def get_api_key():
    load_env()
    return os.environ.get("GEMINI_API_KEY")

def get_fallback_models():
    load_env()
    model_env = os.environ.get("GEMINI_MODELS")
    if model_env:
        return [m.strip() for m in model_env.split(",") if m.strip()]
    return DEFAULT_MODELS.copy()

def get_panns_threads():
    load_env()
    try:
        return int(os.environ.get("PANNS_THREADS", 0)) or None
    except (ValueError, TypeError):
        return None
