"""
essentia_classify.py

Backend alternativo de classificacao de audio usando Essentia (modelos
TensorFlow do "Essentia Model Zoo"). Nao precisa de API key nem internet
depois do primeiro uso — so a primeira execucao baixa os modelos (~90MB).

Usa dois modelos oficiais da MTG-UPF (https://essentia.upf.edu/models.html):
  1. discogs-effnet-bs64-1.pb        -> extrator de embeddings (EfficientNet
                                         treinado no catalogo do Discogs)
  2. mtg_jamendo_instrument-discogs-effnet-1.pb -> "cabeca" de classificacao
                                         treinada no MTG-Jamendo, 40 classes
                                         de instrumentos, a partir dos
                                         embeddings acima.

Mapeamos as 40 classes do MTG-Jamendo para as mesmas categorias fechadas que
o Gemini/YamNet/PANNs retornam (vocal, guitarra, baixo, bateria, etc).

IMPORTANTE (Windows): os bindings Python do Essentia (`essentia-tensorflow`)
NAO tem wheel oficial pra Windows nativo — so Linux e macOS. No Windows,
essa funcao retorna um erro explicando isso (rode via WSL, ou instale
Essentia numa maquina Linux/Mac se quiser testar esse backend). Veja:
https://essentia.upf.edu/installing.html
"""

import os
import sys
import json
import threading
import urllib.request

# --- Download/cache dos modelos TensorFlow do Essentia --------------------

_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_EMBEDDING_MODEL_PATH = os.path.join(_MODEL_DIR, "discogs-effnet-bs64-1.pb")
_HEAD_MODEL_PATH = os.path.join(_MODEL_DIR, "mtg_jamendo_instrument-discogs-effnet-1.pb")

_EMBEDDING_MODEL_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb"
_HEAD_MODEL_URL = "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_instrument/mtg_jamendo_instrument-discogs-effnet-1.pb"


def _ensure_models():
    """Baixa os dois arquivos .pb do Essentia se ainda nao existirem localmente."""
    os.makedirs(_MODEL_DIR, exist_ok=True)
    if not os.path.isfile(_EMBEDDING_MODEL_PATH):
        print("  [Essentia] Baixando modelo de embeddings (primeira execucao, ~18MB)...")
        urllib.request.urlretrieve(_EMBEDDING_MODEL_URL, _EMBEDDING_MODEL_PATH)
    if not os.path.isfile(_HEAD_MODEL_PATH):
        print("  [Essentia] Baixando modelo de classificacao de instrumentos (~few MB)...")
        urllib.request.urlretrieve(_HEAD_MODEL_URL, _HEAD_MODEL_PATH)
    return _EMBEDDING_MODEL_PATH, _HEAD_MODEL_PATH


# --- Mapeamento das 40 classes do MTG-Jamendo Instrument -> categorias -----
# Referencia das classes: https://essentia.upf.edu/models/classification-heads/mtg_jamendo_instrument/

_INSTRUMENT_TAGS = [
    "accordion", "acousticbassguitar", "acousticguitar", "bass", "beat", "bell",
    "bongo", "brass", "cello", "clarinet", "classicalguitar", "computer",
    "doublebass", "drummachine", "drums", "electricguitar", "electricpiano",
    "flute", "guitar", "harmonica", "harp", "horn", "keyboard", "oboe",
    "orchestra", "organ", "pad", "percussion", "piano", "pipeorgan", "rhodes",
    "sampler", "saxophone", "strings", "synthesizer", "trombone", "trumpet",
    "viola", "violin", "voice",
]

# tag -> (category, instrument_pt, instrument_en)
_TAG_MAP = {
    "accordion":          ("teclado", "Acordeao", "Accordion"),
    "acousticbassguitar":  ("baixo", "Baixo acustico", "Acoustic bass"),
    "acousticguitar":     ("guitarra", "Violão", "Acoustic guitar"),
    "bass":                ("baixo", "Baixo", "Bass"),
    "beat":                ("bateria", "Bateria (batida)", "Beat/drums"),
    "bell":                ("bateria", "Percussao (sino)", "Bell"),
    "bongo":               ("bateria", "Percussao (bongo)", "Bongo"),
    "brass":               ("sopro", "Metais", "Brass"),
    "cello":               ("cordas", "Violoncelo", "Cello"),
    "clarinet":            ("sopro", "Clarinete", "Clarinet"),
    "classicalguitar":     ("guitarra", "Violão clássico", "Classical guitar"),
    "computer":            ("synth", "Sintetizador/computador", "Computer/synth"),
    "doublebass":          ("baixo", "Contrabaixo", "Double bass"),
    "drummachine":         ("bateria", "Bateria eletronica", "Drum machine"),
    "drums":               ("bateria", "Bateria", "Drums"),
    "electricguitar":      ("guitarra", "Guitarra", "Electric guitar"),
    "electricpiano":       ("teclado", "Piano eletrico", "Electric piano"),
    "flute":               ("sopro", "Flauta", "Flute"),
    "guitar":              ("guitarra", "Guitarra/violão", "Guitar"),
    "harmonica":           ("sopro", "Gaita", "Harmonica"),
    "harp":                ("cordas", "Harpa", "Harp"),
    "horn":                ("sopro", "Trompa", "French horn"),
    "keyboard":            ("teclado", "Teclado", "Keyboard"),
    "oboe":                ("sopro", "Oboe", "Oboe"),
    "orchestra":           ("cordas", "Orquestra (cordas)", "Orchestra"),
    "organ":               ("teclado", "Orgao", "Organ"),
    "pad":                 ("synth", "Sintetizador (pad)", "Synth pad"),
    "percussion":          ("bateria", "Percussao", "Percussion"),
    "piano":               ("teclado", "Piano", "Piano"),
    "pipeorgan":           ("teclado", "Orgao de tubos", "Pipe organ"),
    "rhodes":              ("teclado", "Piano eletrico (Rhodes)", "Rhodes piano"),
    "sampler":             ("synth", "Sampler", "Sampler"),
    "saxophone":           ("sopro", "Saxofone", "Saxophone"),
    "strings":             ("cordas", "Cordas", "Strings"),
    "synthesizer":         ("synth", "Sintetizador", "Synthesizer"),
    "trombone":            ("sopro", "Trombone", "Trombone"),
    "trumpet":             ("sopro", "Trompete", "Trumpet"),
    "viola":               ("cordas", "Viola", "Viola"),
    "violin":              ("cordas", "Violino", "Violin"),
    "voice":               ("vocal", "Vocal", "Vocal"),
}

# --- Cache dos modelos carregados em memoria (thread-safe, carrega 1x) -----

_model_lock = threading.Lock()
_embedding_extractor = None
_classification_head = None


def _get_models():
    global _embedding_extractor, _classification_head
    with _model_lock:
        if _embedding_extractor is None or _classification_head is None:
            from essentia.standard import TensorflowPredictEffnetDiscogs, TensorflowPredict2D
            emb_path, head_path = _ensure_models()
            _embedding_extractor = TensorflowPredictEffnetDiscogs(graphFilename=emb_path, output="PartitionedCall:1")
            _classification_head = TensorflowPredict2D(graphFilename=head_path)
    return _embedding_extractor, _classification_head


def classify_with_essentia(audio_path, output_language="pt"):
    """
    Executa o pipeline Essentia (embeddings EffNet-Discogs + cabeca de
    instrumentos do MTG-Jamendo) sobre um arquivo de audio e retorna um
    dict no mesmo formato que os outros backends:

        {"category": ..., "instrument": ..., "confidence": ..., "notes": ...}
    """
    try:
        from essentia.standard import MonoLoader
    except ImportError as e:
        if sys.platform.startswith("win"):
            return {
                "error": (
                    "Essentia (bindings Python) nao tem wheel oficial para Windows nativo. "
                    "Rode este backend via WSL ou numa maquina Linux/Mac, ou use YamNet/PANNs "
                    "para um backend local no Windows. Detalhes: "
                    "https://essentia.upf.edu/installing.html"
                )
            }
        return {"error": f"essentia nao instalado: {e}. Rode: pip install essentia-tensorflow"}

    try:
        embedding_extractor, classification_head = _get_models()

        audio = MonoLoader(filename=audio_path, sampleRate=16000, resampleQuality=4)()
        
        # Peak normalization
        max_val = np.max(np.abs(audio))
        if max_val > 0:
            audio = audio / max_val

        embeddings = embedding_extractor(audio)
        predictions = classification_head(embeddings)

        import numpy as np
        mean_scores = np.mean(predictions, axis=0)
        best_idx = int(np.argmax(mean_scores))
        best_tag = _INSTRUMENT_TAGS[best_idx]
        best_confidence = float(mean_scores[best_idx])

        category, inst_pt, inst_en = _TAG_MAP.get(best_tag, ("outro", best_tag, best_tag))
        instrument = inst_en if output_language == "en" else inst_pt

        return {
            "category": category,
            "instrument": instrument,
            "confidence": round(best_confidence, 3),
            "notes": "Essentia (local inference, discogs-effnet + mtg-jamendo-instrument)",
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # Teste rapido via linha de comando:
    #   python essentia_classify.py caminho/para/audio.wav
    if len(sys.argv) < 2:
        print("Uso: python essentia_classify.py <audio.wav> [--output-language pt|en]")
        sys.exit(1)
    audio_file = sys.argv[1]
    lang_arg = "pt"
    if "--output-language" in sys.argv:
        idx = sys.argv.index("--output-language")
        if idx + 1 < len(sys.argv):
            lang_arg = sys.argv[idx + 1]
    result = classify_with_essentia(audio_file, output_language=lang_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
