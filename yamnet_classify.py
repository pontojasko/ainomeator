"""
yamnet_classify.py

Backend alternativo de classificacao de audio usando YamNet (TensorFlow Lite).
Nao precisa de API key nem internet — roda 100% local.

YamNet e um modelo de classificacao de eventos sonoros treinado pelo Google
com 521 classes (AudioSet ontology). Muitas dessas classes correspondem a
instrumentos musicais, entao mapeamos as saidas do YamNet para as mesmas
categorias que o Gemini retorna (vocal, guitarra, baixo, bateria, etc).

Na primeira execucao, baixa o modelo TFLite automaticamente (~4MB).
"""

import os
import sys
import json
import urllib.request
import zipfile
import tempfile
import numpy as np

# --- Download/cache do modelo TFLite -------------------------------------------

_MODEL_URL = "https://tfhub.dev/google/lite-model/yamnet/tflite/1?lite-format=tflite"
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_MODEL_PATH = os.path.join(_MODEL_DIR, "yamnet.tflite")

def _ensure_model():
    """Baixa o modelo YamNet TFLite se ainda nao existir localmente."""
    if os.path.isfile(_MODEL_PATH):
        return _MODEL_PATH
    os.makedirs(_MODEL_DIR, exist_ok=True)
    print(f"  [YamNet] Baixando modelo (primeira execucao)...")
    req = urllib.request.Request(_MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response:
        with open(_MODEL_PATH, "wb") as f:
            f.write(response.read())
    print(f"  [YamNet] Modelo salvo em {_MODEL_PATH}")
    return _MODEL_PATH


# --- Mapeamento de labels AudioSet -> categorias do AiNOMEATOR -----------------
# As 521 classes do YamNet incluem varios instrumentos. Mapeamos as mais
# relevantes para as categorias fechadas que o projeto ja usa.
# Referencia: https://github.com/tensorflow/models/blob/master/research/audioset/yamnet/yamnet_class_map.csv

# Mapa de nomes de labels do YamNet para (category, instrument_pt, instrument_en)
_YAMNET_LABEL_MAP = {
    # Vocais
    "Speech":                         ("vocal", "Vocal", "Vocal"),
    "Female singing":                 ("vocal", "Vocal feminino", "Female vocal"),
    "Male singing":                   ("vocal", "Vocal masculino", "Male vocal"),
    "Choir":                          ("vocal", "Coral", "Choir"),
    "A capella":                      ("vocal", "A capella", "A capella"),
    "Yodeling":                       ("vocal", "Vocal (yodel)", "Vocal (yodel)"),
    "Child singing":                  ("vocal", "Vocal infantil", "Child vocal"),

    # Bateria / Percussao
    "Snare drum":                     ("bateria", "Bateria (caixa)", "Drums (snare)"),
    "Bass drum":                      ("bateria", "Bateria (bumbo)", "Drums (kick)"),
    "Drum kit":                       ("bateria", "Bateria", "Drums"),
    "Drum roll":                      ("bateria", "Bateria (roll)", "Drum roll"),
    "Cymbal":                         ("bateria", "Bateria (pratos)", "Cymbals"),
    "Hi-hat":                         ("bateria", "Bateria (hi-hat)", "Hi-hat"),
    "Tambourine":                     ("bateria", "Percussao (tamborim)", "Tambourine"),
    "Maracas":                        ("bateria", "Percussao (maracas)", "Maracas"),
    "Castanet":                       ("bateria", "Percussao (castanholas)", "Castanets"),
    "Clapping":                       ("bateria", "Palmas", "Clapping"),
    "Hand clap":                      ("bateria", "Palmas", "Hand clap"),
    "Cowbell":                        ("bateria", "Percussao (cowbell)", "Cowbell"),
    "Gong":                           ("bateria", "Percussao (gongo)", "Gong"),
    "Drum machine":                   ("bateria", "Bateria eletronica", "Drum machine"),

    # Baixo
    "Bass guitar":                    ("baixo", "Baixo", "Bass"),
    "Double bass":                    ("baixo", "Contrabaixo", "Double bass"),

    # Guitarra
    "Acoustic guitar":                ("guitarra", "Violao", "Acoustic guitar"),
    "Electric guitar":                ("guitarra", "Guitarra eletrica", "Electric guitar"),
    "Steel guitar":                   ("guitarra", "Guitarra (steel)", "Steel guitar"),
    "Slide guitar":                   ("guitarra", "Guitarra (slide)", "Slide guitar"),
    "Banjo":                          ("guitarra", "Banjo", "Banjo"),

    # Teclado / Piano
    "Piano":                          ("teclado", "Piano", "Piano"),
    "Electric piano":                 ("teclado", "Piano eletrico", "Electric piano"),
    "Organ":                          ("teclado", "Orgao", "Organ"),
    "Keyboard":                       ("teclado", "Teclado", "Keyboard"),
    "Harpsichord":                    ("teclado", "Cravo", "Harpsichord"),

    # Synth
    "Synthesizer":                    ("synth", "Synth", "Synth"),
    "Synth bass":                     ("synth", "Synth baixo", "Synth bass"),

    # Sopro
    "Flute":                          ("sopro", "Flauta", "Flute"),
    "Saxophone":                      ("sopro", "Saxofone", "Saxophone"),
    "Trumpet":                        ("sopro", "Trompete", "Trumpet"),
    "Trombone":                       ("sopro", "Trombone", "Trombone"),
    "Clarinet":                       ("sopro", "Clarinete", "Clarinet"),
    "Harmonica":                      ("sopro", "Gaita", "Harmonica"),
    "Horn":                           ("sopro", "Trompa", "French horn"),
    "Oboe":                           ("sopro", "Oboe", "Oboe"),

    # Cordas
    "Violin":                         ("cordas", "Violino", "Violin"),
    "Cello":                          ("cordas", "Violoncelo", "Cello"),
    "Strings":                        ("cordas", "Cordas", "Strings"),
    "Harp":                           ("cordas", "Harpa", "Harp"),
    "Mandolin":                       ("cordas", "Bandolim", "Mandolin"),
}


def _load_yamnet_labels():
    """Carrega a lista de 521 labels do arquivo yamnet_class_map.csv (baixa se nao existir)"""
    labels_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yamnet_class_map.csv")
    if not os.path.isfile(labels_path):
        try:
            print("  [YamNet] Baixando yamnet_class_map.csv...")
            url = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as response:
                with open(labels_path, "wb") as f:
                    f.write(response.read())
        except Exception as e:
            print(f"  [YamNet] Nao foi possivel baixar class map: {e}")
            return None

    if os.path.isfile(labels_path):
        import csv
        with open(labels_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)  # pula o cabecalho
            labels = []
            for row in reader:
                if len(row) >= 3:
                    labels.append(row[2].strip())
            return labels
    return None


def classify_with_yamnet(audio_path, output_language="pt"):
    """
    Executa o modelo YamNet sobre um arquivo de audio e retorna um dict
    no mesmo formato que o classify_track.py retorna do Gemini:

        {"category": ..., "instrument": ..., "confidence": ..., "notes": ...}
    """
    # Import tensorflow lazily — so pesa na primeira chamada
    try:
        import tensorflow as tf
    except ImportError:
        return {"error": "tensorflow nao instalado. Rode: pip install tensorflow"}

    try:
        model_path = _ensure_model()

        # Carregar labels
        all_labels = _load_yamnet_labels()

        # Carregar audio com soundfile (mesma lib que o projeto ja usa)
        import soundfile as sf
        data, sr = sf.read(audio_path)

        # Mono
        if data.ndim > 1:
            data = data.mean(axis=1)

        # Peak normalization
        max_val = np.max(np.abs(data))
        if max_val > 0:
            data = data / max_val

        # Resample para 16kHz (YamNet exige 16kHz)
        if sr != 16000:
            duration = len(data) / sr
            n_target = int(round(duration * 16000))
            x_old = np.linspace(0, duration, num=len(data), endpoint=False)
            x_new = np.linspace(0, duration, num=n_target, endpoint=False)
            data = np.interp(x_new, x_old, data).astype(np.float32)
            sr = 16000
        else:
            data = data.astype(np.float32)

        # YamNet espera pelo menos 15615 samples (0.975s a 16kHz)
        if len(data) < 15615:
            data = np.pad(data, (0, 15615 - len(data)))

        interpreter = tf.lite.Interpreter(model_path=model_path)
        input_details = interpreter.get_input_details()
        interpreter.resize_tensor_input(input_details[0]["index"], [len(data)])
        interpreter.allocate_tensors()
        output_details = interpreter.get_output_details()

        interpreter.set_tensor(input_details[0]["index"], data)
        interpreter.invoke()

        # A saida tem formato [num_frames, 521]
        scores = interpreter.get_tensor(output_details[0]["index"])
        if scores.ndim > 1:
            mean_scores = np.mean(scores, axis=0)
        else:
            mean_scores = scores

        # Top-5 labels
        top_indices = np.argsort(mean_scores)[::-1][:5]

        # Filtrar labels que temos mapeamento
        best_result = None
        best_confidence = 0.0
        for idx in top_indices:
            label_name = all_labels[idx] if all_labels and idx < len(all_labels) else f"class_{idx}"
            if label_name in _YAMNET_LABEL_MAP:
                category, inst_pt, inst_en = _YAMNET_LABEL_MAP[label_name]
                confidence = float(mean_scores[idx])
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_result = (category, inst_pt, inst_en)
                break  # ja achou o primeiro mapeado

        if best_result is None:
            # Nenhum label mapeado — pega o top-1 generico
            top_idx = top_indices[0]
            label_name = all_labels[top_idx] if all_labels and top_idx < len(all_labels) else f"class_{top_idx}"
            best_confidence = float(mean_scores[top_idx])
            best_result = ("outro", label_name, label_name)

        category, inst_pt, inst_en = best_result
        instrument = inst_en if output_language == "en" else inst_pt

        return {
            "category": category,
            "instrument": instrument,
            "confidence": round(best_confidence, 3),
            "notes": "YamNet (local inference)",
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # Teste rapido via linha de comando:
    #   python yamnet_classify.py caminho/para/audio.wav
    if len(sys.argv) < 2:
        print("Uso: python yamnet_classify.py <audio.wav> [--output-language pt|en]")
        sys.exit(1)
    audio_file = sys.argv[1]
    lang_arg = "pt"
    if "--output-language" in sys.argv:
        idx = sys.argv.index("--output-language")
        if idx + 1 < len(sys.argv):
            lang_arg = sys.argv[idx + 1]
    result = classify_with_yamnet(audio_file, output_language=lang_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
