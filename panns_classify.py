"""
panns_classify.py

Backend alternativo de classificacao de audio usando PANNs (Pretrained Audio
Neural Networks - modelo Cnn14, treinado no AudioSet). Nao precisa de API
key nem internet depois do primeiro uso — a lib `panns_inference` baixa o
checkpoint (Cnn14_mAP=0.431.pth, ~300MB) automaticamente na primeira vez que
roda, e guarda em ~/panns_data/.

PANNs e um classificador de eventos sonoros (nao especifico pra musica) com
527 classes do AudioSet, varias delas instrumentos. Mapeamos pro mesmo
vocabulario fechado que os outros backends usam (vocal, guitarra, baixo,
bateria, etc). Roda em CPU (mais lento que YamNet, mas geralmente mais
preciso — e um modelo bem maior).

Requisitos: pip install panns_inference torch soundfile
"""

import os
import sys
import json
import threading
import numpy as np

# --- Mapeamento de labels do AudioSet -> categorias do AiNOMEATOR ---------
# Mesma ideia do yamnet_classify.py: o PANNs usa a mesma ontologia AudioSet,
# entao os nomes de classe (quase) coincidem com os do YamNet.

_LABEL_MAP = {
    # Vocais
    "Speech":                         ("vocal", "Vocal", "Vocal"),
    "Female singing":                 ("vocal", "Vocal feminino", "Female vocal"),
    "Male singing":                   ("vocal", "Vocal masculino", "Male vocal"),
    "Choir":                          ("vocal", "Coral", "Choir"),
    "A capella":                      ("vocal", "A capella", "A capella"),
    "Yodeling":                       ("vocal", "Vocal (yodel)", "Vocal (yodel)"),
    "Child singing":                  ("vocal", "Vocal infantil", "Child vocal"),
    "Rapping":                        ("vocal", "Vocal (rap)", "Rap vocal"),

    # Bateria / Percussao
    "Snare drum":                     ("bateria", "Bateria (caixa)", "Drums (snare)"),
    "Bass drum":                      ("bateria", "Bateria (bumbo)", "Drums (kick)"),
    "Drum kit":                       ("bateria", "Bateria", "Drums"),
    "Drum":                           ("bateria", "Bateria", "Drums"),
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
    "Timpani":                        ("bateria", "Percussao (tímpano)", "Timpani"),

    # Baixo
    "Bass guitar":                    ("baixo", "Baixo", "Bass"),
    "Double bass":                    ("baixo", "Contrabaixo", "Double bass"),

    # Guitarra
    "Acoustic guitar":                ("guitarra", "Violao", "Acoustic guitar"),
    "Electric guitar":                ("guitarra", "Guitarra eletrica", "Electric guitar"),
    "Steel guitar, slide guitar":     ("guitarra", "Guitarra (steel/slide)", "Steel/slide guitar"),
    "Banjo":                          ("guitarra", "Banjo", "Banjo"),
    "Ukulele":                        ("guitarra", "Ukulele", "Ukulele"),

    # Teclado / Piano
    "Piano":                          ("teclado", "Piano", "Piano"),
    "Electric piano":                 ("teclado", "Piano eletrico", "Electric piano"),
    "Organ":                          ("teclado", "Orgao", "Organ"),
    "Electronic organ":               ("teclado", "Orgao eletronico", "Electronic organ"),
    "Keyboard (musical)":             ("teclado", "Teclado", "Keyboard"),
    "Harpsichord":                    ("teclado", "Cravo", "Harpsichord"),

    # Synth
    "Synthesizer":                    ("synth", "Synth", "Synth"),

    # Sopro
    "Flute":                          ("sopro", "Flauta", "Flute"),
    "Saxophone":                      ("sopro", "Saxofone", "Saxophone"),
    "Trumpet":                        ("sopro", "Trompete", "Trumpet"),
    "Trombone":                       ("sopro", "Trombone", "Trombone"),
    "Clarinet":                       ("sopro", "Clarinete", "Clarinet"),
    "Harmonica":                      ("sopro", "Gaita", "Harmonica"),
    "French horn":                    ("sopro", "Trompa", "French horn"),
    "Oboe":                           ("sopro", "Oboe", "Oboe"),
    "Brass instrument":               ("sopro", "Metais", "Brass instrument"),

    # Cordas
    "Violin, fiddle":                 ("cordas", "Violino", "Violin"),
    "Cello":                          ("cordas", "Violoncelo", "Cello"),
    "String section":                 ("cordas", "Cordas", "Strings"),
    "Harp":                           ("cordas", "Harpa", "Harp"),
    "Mandolin":                       ("cordas", "Bandolim", "Mandolin"),
}


# --- Cache do modelo carregado em memoria (thread-safe, carrega 1x) -------

_model_lock = threading.Lock()
_audio_tagger = None
_panns_labels = None


def _get_model():
    global _audio_tagger, _panns_labels
    with _model_lock:
        if _audio_tagger is None:
            from panns_inference import AudioTagging, labels
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            print("  [PANNs] Loading CNN14 AI Model (device={device}) (~300MB)...")
            _audio_tagger = AudioTagging(checkpoint_path=None, device=device)
            _panns_labels = labels
    return _audio_tagger, _panns_labels


def _load_audio_32k_mono(audio_path):
    """Le o audio com soundfile, converte pra mono e reamostra pra 32kHz
    (taxa que o PANNs/Cnn14 espera), sem depender de librosa/ffmpeg."""
    import soundfile as sf

    data, sr = sf.read(audio_path, always_2d=False)
    data = np.asarray(data, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)

    # Peak normalization
    max_val = np.max(np.abs(data))
    if max_val > 0:
        data = data / max_val

    target_sr = 32000
    if sr != target_sr:
        duration = len(data) / sr
        n_target = max(1, int(round(duration * target_sr)))
        x_old = np.linspace(0, duration, num=len(data), endpoint=False)
        x_new = np.linspace(0, duration, num=n_target, endpoint=False)
        data = np.interp(x_new, x_old, data).astype(np.float32)

    return data


def classify_with_panns(audio_path, output_language="pt"):
    """
    Executa o modelo PANNs (Cnn14) sobre um arquivo de audio e retorna um
    dict no mesmo formato que os outros backends:

        {"category": ..., "instrument": ..., "confidence": ..., "notes": ...}
    """
    try:
        import torch  # noqa: F401
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        panns_threads = os.environ.get("PANNS_THREADS")
        if panns_threads:
            try:
                torch.set_num_threads(int(panns_threads))
            except ValueError:
                pass
        from panns_inference import AudioTagging  # noqa: F401
    except ImportError:
        return {"error": "panns_inference/torch nao instalado. Rode: pip install panns_inference torch"}

    try:
        at, panns_labels = _get_model()

        data = _load_audio_32k_mono(audio_path)
        audio = data[None, :]  # (batch_size=1, samples)

        clipwise_output, _embedding = at.inference(audio)
        scores = clipwise_output[0]  # (527,)

        top_indices = np.argsort(scores)[::-1][:10]

        best_result = None
        best_confidence = 0.0
        for idx in top_indices:
            label_name = panns_labels[idx]
            if label_name in _LABEL_MAP:
                category, inst_pt, inst_en = _LABEL_MAP[label_name]
                confidence = float(scores[idx])
                best_confidence = confidence
                best_result = (category, inst_pt, inst_en)
                break  # ja achou o top-1 mapeado (lista esta em ordem decrescente)

        if best_result is None:
            top_idx = int(top_indices[0])
            label_name = panns_labels[top_idx]
            best_confidence = float(scores[top_idx])
            best_result = ("outro", label_name, label_name)

        category, inst_pt, inst_en = best_result
        instrument = inst_en if output_language == "en" else inst_pt

        return {
            "category": category,
            "instrument": instrument,
            "confidence": round(best_confidence, 3),
            "notes": "PANNs / Cnn14 (local inference)",
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # Teste rapido via linha de comando:
    #   python panns_classify.py caminho/para/audio.wav
    if len(sys.argv) < 2:
        print("Uso: python panns_classify.py <audio.wav> [--output-language pt|en]")
        sys.exit(1)
    audio_file = sys.argv[1]
    lang_arg = "pt"
    if "--output-language" in sys.argv:
        idx = sys.argv.index("--output-language")
        if idx + 1 < len(sys.argv):
            lang_arg = sys.argv[idx + 1]
    result = classify_with_panns(audio_file, output_language=lang_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
