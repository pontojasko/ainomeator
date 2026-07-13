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
bateria, etc). Roda em CPU ou GPU (CUDA/ROCm/DirectML — ver `_detect_device`).

Requisitos: pip install panns_inference torch soundfile scipy

=====================================================================
NOTAS DE PERFORMANCE (leia antes de mexer neste arquivo)
=====================================================================

Este modulo e chamado, no fluxo normal do AiNOMEATOR, UMA VEZ POR TRACK
(potencialmente centenas de vezes numa sessao grande), entao qualquer coisa
que rode "por chamada" em vez de "uma vez por processo" e multiplicada pelo
numero de faixas. As otimizacoes abaixo atacam exatamente isso:

1. INIT UNICO (`_ensure_ready`): imports pesados (torch, panns_inference),
   leitura do .env e configuracao de threads agora rodam SO NA PRIMEIRA
   chamada do processo, protegidos por lock. Antes, `classify_with_panns()`
   fazia `load_dotenv()` (I/O de disco!) e reimportava torch/panns_inference
   TODA vez que era chamada — desperdicio puro em um loop de centenas de
   faixas.

2. GRADIENTES DESLIGADOS: alem do `torch.no_grad()` que o proprio
   panns_inference ja usa internamente, envolvemos toda inferencia em
   `torch.inference_mode()` (mais rapido que no_grad: pula o bookkeeping de
   version counter do autograd) e desligamos `torch.set_grad_enabled(False)`
   globalmente no processo, ja que este modulo nunca treina nada.

3. RESAMPLE: usamos `audio_utils.resample()` que prioriza soxr (libsoxr,
   o mesmo resampler que o librosa usa por padrao hoje em dia). Medido neste
   projeto (8s @ 44100Hz -> 32000Hz): np.interp ~6.5ms vs soxr ~3.3ms por
   faixa — quase 2x mais rapido, e sem aliasing. Fallback para
   scipy.signal.resample_poly e entao np.interp se soxr nao estiver instalado.

4. TOP-K via argpartition: O(n) para achar top-10 vs O(n log n) de argsort
   sobre todas as 527 classes. Ganho pequeno em absoluto, mas gratis.

5. GPU (NVIDIA / AMD):
   - NVIDIA CUDA: ja funcionava (`torch.cuda.is_available()`).
   - AMD no Linux (ROCm): um PyTorch compilado com ROCm expoe a MESMA API
     `torch.cuda.*`. Com o pacote certo instalado, cai automaticamente no
     mesmo caminho "cuda".
   - AMD/Intel no Windows (DirectML): ROCm nao roda no Windows. A unica
     rota de GPU nesse caso e `torch-directml`. A lib `panns_inference` so
     entende os STRINGS 'cuda' ou 'cpu' internamente (ela faz
     `.cuda()` hardcoded), entao nao da pra so passar device='dml'. Por
     isso, quando detectamos DirectML, construimos o modelo em CPU e
     movemos o `nn.Module` manualmente pro device DML, e escrevemos nosso
     proprio laco de inferencia (`_forward_on_device`) no lugar de
     `at.inference()`.
     CUIDADO: o torch-directml tem incompatibilidades conhecidas. Por
     exemplo, o bug do `torch.inference_mode()` (microsoft/DirectML#602,
     "Cannot set version_counter"), que contornamos usando `torch.no_grad()`
     no `_forward_on_device`. Alem disso, se qualquer outra operacao
     falhar em runtime, o codigo detecta, desliga DML PRA SEMPRE nesse
     processo e cai pra CPU automaticamente.

6. INFERENCIA EM LOTE (`classify_many_with_panns`): a funcao original
   processa um arquivo por chamada. Se voce esta processando N faixas de
   uma vez (ex: varios stems do mesmo projeto), rodar 1 forward pass com um
   batch de N e MUITO mais rapido que N forward passes separados — menos
   overhead de Python por chamada e, em GPU, paraleliza de verdade dentro
   do proprio device. Use essa funcao quando estiver classificando varias
   faixas em sequencia com o backend "panns" puro (sem hibrido/Gemini).

7. AGREGACAO DE SCORES POR CATEGORIA (`_pick_label_aggregated`): em vez de
   pegar apenas o top-1 label mapeado, acumula os scores de TODAS as 527
   classes por categoria. Isso resolve casos onde a energia esta distribuida
   entre multiplos labels da mesma categoria (ex: Drum 0.3 + Snare 0.25 +
   Hi-hat 0.2 = categoria "bateria" com score agregado 0.75, que vence
   "Speech" com 0.35 individual). O nome do instrumento e o do label
   individual de maior score dentro da categoria vencedora.
=====================================================================
"""

from __future__ import annotations

import os
import threading
from typing import Any

import numpy as np
import soundfile as sf

from audio_utils import resample as _resample_fn

# ---------------------------------------------------------------------------
# Mapeamento de labels do AudioSet -> categorias do AiNOMEATOR
# ---------------------------------------------------------------------------
# Expandido para cobrir ~90 labels musicais do AudioSet (527 classes totais).
# Formato: "Label AudioSet" -> (categoria, nome_pt, nome_en)
#
# NOTA: os strings devem bater EXATAMENTE com panns_inference.labels.
# Em _ensure_ready() validamos e descartamos silenciosamente os que nao baterem.

_LABEL_MAP_RAW: dict[str, tuple[str, str, str]] = {
    # ------------------------------------------------------------------
    # Vocais
    # ------------------------------------------------------------------
    "Speech":                           ("vocal", "Vocal",                    "Vocal"),
    "Singing":                          ("vocal", "Vocal (canto)",             "Singing"),
    "Vocal music":                      ("vocal", "Musica vocal",              "Vocal music"),
    "Female singing":                   ("vocal", "Vocal feminino",            "Female vocal"),
    "Male singing":                     ("vocal", "Vocal masculino",           "Male vocal"),
    "Choir":                            ("vocal", "Coral",                     "Choir"),
    "A capella":                        ("vocal", "Vocal",                     "A capella"),
    "Yodeling":                         ("vocal", "Vocal (yodel)",             "Vocal (yodel)"),
    "Child singing":                    ("vocal", "Vocal infantil",            "Child vocal"),
    "Rapping":                          ("vocal", "Vocal (rap)",               "Rap vocal"),
    "Humming":                          ("vocal", "Vocal (humming)",           "Humming"),
    "Beatboxing":                       ("vocal", "Vocal (beatbox)",           "Beatboxing"),
    "Whistling":                        ("vocal", "Vocal (assobio)",           "Whistling"),
    "Throat singing":                   ("vocal", "Vocal (gutural)",           "Throat singing"),

    # ------------------------------------------------------------------
    # Bateria / Percussao
    # ------------------------------------------------------------------
    "Snare drum":                       ("bateria", "Bateria (caixa)",         "Drums (snare)"),
    "Bass drum":                        ("bateria", "Bateria (bumbo)",         "Drums (kick)"),
    "Drum kit":                         ("bateria", "Bateria",                 "Drums"),
    "Drum":                             ("bateria", "Bateria",                 "Drums"),
    "Drum roll":                        ("bateria", "Bateria (roll)",          "Drum roll"),
    "Cymbal":                           ("bateria", "Bateria (pratos)",        "Cymbals"),
    "Hi-hat":                           ("bateria", "Bateria (hi-hat)",        "Hi-hat"),
    "Crash cymbal":                     ("bateria", "Bateria (crash)",         "Crash cymbal"),
    "Tambourine":                       ("bateria", "Percussao (tamborim)",    "Tambourine"),
    "Maracas":                          ("bateria", "Percussao (maracas)",     "Maracas"),
    "Castanet":                         ("bateria", "Percussao (castanholas)", "Castanets"),
    "Clapping":                         ("bateria", "Palmas",                  "Clapping"),
    "Hand clap":                        ("bateria", "Palmas",                  "Hand clap"),
    "Cowbell":                          ("bateria", "Percussao (cowbell)",     "Cowbell"),
    "Gong":                             ("bateria", "Percussao (gongo)",       "Gong"),
    "Drum machine":                     ("bateria", "Bateria eletronica",      "Drum machine"),
    "Timpani":                          ("bateria", "Percussao (timpano)",     "Timpani"),
    "Percussion":                       ("bateria", "Percussao",               "Percussion"),
    "Tabla":                            ("bateria", "Percussao (tabla)",       "Tabla"),
    "Djembe":                           ("bateria", "Percussao (djembe)",      "Djembe"),
    "Bongo":                            ("bateria", "Percussao (bongo)",       "Bongo"),
    "Conga":                            ("bateria", "Percussao (conga)",       "Conga"),
    "Wood block":                       ("bateria", "Percussao (wood block)",  "Wood block"),
    "Rimshot":                          ("bateria", "Bateria (rimshot)",       "Rimshot"),
    "Mallet percussion":                ("bateria", "Percussao (mallets)",     "Mallet percussion"),
    "Marimba, xylophone":               ("bateria", "Marimba/Xilofone",       "Marimba/Xylophone"),
    "Glockenspiel":                     ("bateria", "Glockenspiel",            "Glockenspiel"),
    "Vibraphone":                       ("bateria", "Vibrafone",               "Vibraphone"),
    "Steelpan":                         ("bateria", "Steel drum",              "Steelpan"),
    "Tubular bells":                    ("bateria", "Sinos tubulares",         "Tubular bells"),
    "Shaker":                           ("bateria", "Percussao (shaker)",      "Shaker"),

    # ------------------------------------------------------------------
    # Baixo
    # ------------------------------------------------------------------
    "Bass guitar":                      ("baixo", "Baixo",                    "Bass"),
    "Double bass":                      ("baixo", "Contrabaixo",              "Double bass"),

    # ------------------------------------------------------------------
    # Guitarra
    # ------------------------------------------------------------------
    "Guitar":                           ("guitarra", "Guitarra",              "Guitar"),
    "Acoustic guitar":                  ("guitarra", "Violao",                "Acoustic guitar"),
    "Electric guitar":                  ("guitarra", "Guitarra",              "Electric guitar"),
    "Steel guitar, slide guitar":       ("guitarra", "Guitarra (steel/slide)","Steel/slide guitar"),
    "Banjo":                            ("guitarra", "Banjo",                 "Banjo"),
    "Ukulele":                          ("guitarra", "Ukulele",               "Ukulele"),
    "Tapping (guitar technique)":       ("guitarra", "Guitarra (tapping)",    "Guitar tapping"),
    "Strum":                            ("guitarra", "Guitarra (dedilhado)",  "Strum"),
    "Sitar":                            ("guitarra", "Sitar",                 "Sitar"),
    "Plucked string instrument":        ("guitarra", "Cordas dedilhadas",     "Plucked strings"),

    # ------------------------------------------------------------------
    # Teclado / Piano
    # ------------------------------------------------------------------
    "Piano":                            ("teclado", "Piano",                  "Piano"),
    "Electric piano":                   ("teclado", "Piano eletrico",         "Electric piano"),
    "Organ":                            ("teclado", "Orgao",                  "Organ"),
    "Electronic organ":                 ("teclado", "Orgao eletronico",       "Electronic organ"),
    "Keyboard (musical)":               ("teclado", "Teclado",                "Keyboard"),
    "Harpsichord":                      ("teclado", "Cravo",                  "Harpsichord"),
    "Accordion":                        ("teclado", "Acordeao",               "Accordion"),
    "Concertina":                       ("teclado", "Concertina",             "Concertina"),

    # ------------------------------------------------------------------
    # Synth
    # ------------------------------------------------------------------
    "Synthesizer":                      ("synth", "Synth",                    "Synth"),

    # ------------------------------------------------------------------
    # Sopro
    # ------------------------------------------------------------------
    "Flute":                            ("sopro", "Flauta",                   "Flute"),
    "Saxophone":                        ("sopro", "Saxofone",                 "Saxophone"),
    "Trumpet":                          ("sopro", "Trompete",                 "Trumpet"),
    "Trombone":                         ("sopro", "Trombone",                 "Trombone"),
    "Clarinet":                         ("sopro", "Clarinete",                "Clarinet"),
    "Harmonica":                        ("sopro", "Gaita",                    "Harmonica"),
    "French horn":                      ("sopro", "Trompa",                   "French horn"),
    "Oboe":                             ("sopro", "Oboe",                     "Oboe"),
    "Brass instrument":                 ("sopro", "Metais",                   "Brass instrument"),
    "Wind instrument, woodwind instrument": ("sopro", "Sopro (madeiras)",     "Woodwind"),
    "Bassoon":                          ("sopro", "Fagote",                   "Bassoon"),
    "Bagpipes":                         ("sopro", "Gaita de foles",           "Bagpipes"),
    "Didgeridoo":                       ("sopro", "Didgeridoo",               "Didgeridoo"),

    # ------------------------------------------------------------------
    # Cordas
    # ------------------------------------------------------------------
    "Violin, fiddle":                   ("cordas", "Violino",                 "Violin"),
    "Cello":                            ("cordas", "Violoncelo",              "Cello"),
    "String section":                   ("cordas", "Cordas",                  "Strings"),
    "Harp":                             ("cordas", "Harpa",                   "Harp"),
    "Mandolin":                         ("cordas", "Bandolim",                "Mandolin"),
    "Bowed string instrument":          ("cordas", "Cordas (arco)",           "Bowed strings"),
    "Pizzicato":                        ("cordas", "Pizzicato",               "Pizzicato"),
    "Zither":                           ("cordas", "Citara",                  "Zither"),

    # ------------------------------------------------------------------
    # Generico musical (nao e instrumento especifico, mas e musica)
    # ------------------------------------------------------------------
    "Music":                            ("outro", "Musica",                   "Music"),
    "Musical instrument":               ("outro", "Instrumento musical",      "Musical instrument"),
    "Orchestra":                        ("outro", "Orquestra",                "Orchestra"),
}

# _LABEL_MAP e preenchido em _ensure_ready() apos validar contra panns_inference.labels
_LABEL_MAP: dict[str, tuple[str, str, str]] = {}


# ===========================================================================
# Estado global do processo (tudo carregado/configurado UMA UNICA VEZ)
# ===========================================================================

_init_lock = threading.Lock()
_inference_lock = threading.Lock()
_ready = False

_audio_tagger = None
_panns_labels: list[str] = []
_panns_label_set: frozenset[str] = frozenset()   # para lookup O(1) em _ensure_ready
_device_str: str = "cpu"
_device_backend: str = "cpu"
_use_manual_forward: bool = False
_dml_broken: bool = False


def _detect_device() -> tuple[Any, str]:
    """Escolhe o melhor device disponivel nessa ordem de preferencia:
    1. CUDA  -> NVIDIA (CUDA) e AMD no Linux (ROCm via mesma API torch.cuda.*).
    2. DirectML -> unica rota de GPU para AMD/Intel no Windows (sem ROCm).
    3. CPU   -> sempre funciona, fallback final.

    Pode ser forcado via variavel de ambiente PANNS_DEVICE=cuda|dml|cpu|auto.
    """
    import torch

    forced = os.environ.get("PANNS_DEVICE", "auto").strip().lower()

    if forced == "cpu":
        return "cpu", "cpu"

    if forced in ("auto", "cuda") and torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        try:
            is_rocm = bool(getattr(torch.version, "hip", None))
        except Exception:
            is_rocm = False
        return "cuda", ("rocm(amd)" if is_rocm else "cuda(nvidia)")

    if forced in ("auto", "dml"):
        try:
            import torch_directml
            dml_device = torch_directml.device()
            return dml_device, "directml"
        except Exception:
            if forced == "dml":
                print("  [PANNs] PANNS_DEVICE=dml pedido, mas torch-directml "
                      "nao esta instalado/disponivel. Caindo pra CPU. "
                      "(pip install torch-directml)")

    return "cpu", "cpu"


def _ensure_ready() -> None:
    """Roda TUDO que so precisa acontecer uma vez por processo: .env, threads
    do torch, import de torch/panns_inference, deteccao de device,
    carregamento do checkpoint e validacao do _LABEL_MAP.

    Protegido por lock (varias threads podem chamar isso ao mesmo tempo no
    ThreadPoolExecutor do batch_rename.py).
    """
    global _ready, _audio_tagger, _panns_labels, _panns_label_set
    global _device_str, _device_backend, _use_manual_forward, _LABEL_MAP

    if _ready:
        return

    with _init_lock:
        if _ready:
            return

        # .env: le do disco UMA vez, nao a cada faixa
        from _bootstrap import load_env
        load_env()

        import torch

        # Gradientes desligados globalmente: este processo so faz inferencia.
        torch.set_grad_enabled(False)

        panns_threads = os.environ.get("PANNS_THREADS")
        if panns_threads:
            try:
                torch.set_num_threads(int(panns_threads))
            except ValueError:
                pass

        from panns_inference import AudioTagging, labels as panns_labels_raw

        device, backend = _detect_device()
        manual_forward = False

        if backend == "directml":
            at = AudioTagging(checkpoint_path=None, device="cpu")
            try:
                at.model.to(device)
                manual_forward = True
            except Exception as e:
                err_msg = repr(e) if isinstance(e, UnicodeDecodeError) else str(e)
                print(f"  [PANNs] Nao foi possivel mover o modelo pra DirectML "
                      f"({type(e).__name__}: {err_msg}). Usando CPU.", flush=True)
                device, backend = "cpu", "cpu"
        else:
            try:
                at = AudioTagging(checkpoint_path=None, device=device)
            except Exception as e:
                err_msg = repr(e) if isinstance(e, UnicodeDecodeError) else str(e)
                print(f"  [PANNs] Nao foi possivel inicializar no device {backend} "
                      f"({type(e).__name__}: {err_msg}). Caindo para CPU.", flush=True)
                device, backend = "cpu", "cpu"
                at = AudioTagging(checkpoint_path=None, device="cpu")

        print(f"  [PANNs] CNN14 carregado (device={backend}) (~300MB, so na primeira faixa)")

        _audio_tagger = at
        _panns_labels = list(panns_labels_raw)
        _panns_label_set = frozenset(_panns_labels)
        _device_str = device
        _device_backend = backend
        _use_manual_forward = manual_forward

        # Valida _LABEL_MAP_RAW contra os labels reais do modelo.
        # Labels com nomes que nao existem no AudioSet 527 sao descartados
        # silenciosamente para nao causar KeyError em _pick_label*.
        valid_count = 0
        for label_str, mapping in _LABEL_MAP_RAW.items():
            if label_str in _panns_label_set:
                _LABEL_MAP[label_str] = mapping
                valid_count += 1
            # labels nao encontrados sao simplesmente ignorados

        print(f"  [PANNs] _LABEL_MAP validado: {valid_count}/{len(_LABEL_MAP_RAW)} labels ativos")

        _ready = True


def _get_model() -> tuple[Any, list[str]]:
    _ensure_ready()
    return _audio_tagger, _panns_labels


# ===========================================================================
# Audio: leitura + resample
# ===========================================================================

def _load_audio_32k_mono(audio_path: str) -> np.ndarray:
    """Le o audio com soundfile, converte para mono e reamostra para 32kHz
    (taxa que o PANNs/Cnn14 espera), sem depender de librosa/ffmpeg.
    """
    data, sr = sf.read(audio_path, always_2d=False, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1, dtype=np.float32)

    max_val = np.max(np.abs(data)) if data.size else 0.0
    if max_val > 0:
        data = data / max_val

    data = _resample_fn(data, sr, 32000)
    return np.ascontiguousarray(data, dtype=np.float32)


# ===========================================================================
# Inferencia
# ===========================================================================

def _pick_label(scores: np.ndarray, output_language: str) -> tuple[str, str, float]:
    """Pega o top-K labels, retorna o primeiro com mapeamento no _LABEL_MAP.

    Estrategia simples (top-1 mapeado). Para precisao maxima, prefira
    _pick_label_aggregated(), que soma scores por categoria antes de decidir.
    """
    K = 10
    if scores.shape[0] > K:
        unsorted_top = np.argpartition(scores, -K)[-K:]
        top_indices = unsorted_top[np.argsort(scores[unsorted_top])[::-1]]
    else:
        top_indices = np.argsort(scores)[::-1]

    for idx in top_indices:
        label_name = _panns_labels[idx]
        if label_name in _LABEL_MAP:
            category, inst_pt, inst_en = _LABEL_MAP[label_name]
            instrument = inst_en if output_language == "en" else inst_pt
            return category, instrument, round(float(scores[idx]), 3)

    top_idx = int(top_indices[0])
    return "outro", _panns_labels[top_idx], round(float(scores[top_idx]), 3)


def _pick_label_aggregated(scores: np.ndarray, output_language: str) -> tuple[str, str, float]:
    """Agrega scores de TODAS as 527 classes por categoria antes de decidir.

    Por que isso importa: se "Drum"=0.30, "Snare drum"=0.25 e "Hi-hat"=0.20,
    nenhum individualmente supera "Speech"=0.35, mas a categoria "bateria"
    tem score agregado 0.75 e deve vencer. Esta funcao captura esse caso.

    Fluxo:
    1. Itera todas as 527 classes (O(n), ~0.05ms — insignificante).
    2. Acumula score por categoria para todas as classes mapeadas.
    3. A categoria com maior score agregado vence.
    4. O nome do instrumento e o do label individual de maior score
       dentro da categoria vencedora.
    5. O confidence e o score agregado da categoria (capped em 1.0).

    Returns:
        (category, instrument, confidence)
    """
    # Acumuladores por categoria
    cat_scores: dict[str, float] = {}
    cat_best_label: dict[str, tuple[float, str, str]] = {}  # cat -> (score, pt, en)

    for i, label_name in enumerate(_panns_labels):
        if label_name not in _LABEL_MAP:
            continue
        category, inst_pt, inst_en = _LABEL_MAP[label_name]
        score = float(scores[i])
        cat_scores[category] = cat_scores.get(category, 0.0) + score
        prev_score, _, _ = cat_best_label.get(category, (0.0, "", ""))
        if score > prev_score:
            cat_best_label[category] = (score, inst_pt, inst_en)

    if not cat_scores:
        # Nenhum label mapeado — fallback para o top-1 generico
        top_idx = int(np.argmax(scores))
        return "outro", _panns_labels[top_idx], round(float(scores[top_idx]), 3)

    best_cat = max(cat_scores, key=lambda c: cat_scores[c])
    agg_confidence = min(1.0, cat_scores[best_cat])  # cap em 1.0
    _, inst_pt, inst_en = cat_best_label[best_cat]
    instrument = inst_en if output_language == "en" else inst_pt
    return best_cat, instrument, round(agg_confidence, 3)


def _forward_on_device(audio_batch: np.ndarray) -> np.ndarray:
    """Roda o forward pass manualmente no device configurado (usado no caminho
    DirectML, onde nao podemos usar at.inference() porque ela faz .cuda()
    hardcoded). audio_batch: numpy (batch, samples) float32.

    Retorna clipwise_output (batch, 527) como numpy array.
    """
    import torch

    tensor = torch.as_tensor(audio_batch, dtype=torch.float32, device=_device_str)
    # workaround para microsoft/DirectML#602: inference_mode quebra no DML
    with torch.no_grad():
        _audio_tagger.model.eval()
        output_dict = _audio_tagger.model(tensor, None)
    return output_dict["clipwise_output"].detach().to("cpu").numpy()


def _run_inference(audio_batch: np.ndarray) -> np.ndarray:
    """Ponto unico de entrada para rodar o modelo, batched. Decide entre o
    caminho normal (at.inference(), usado por CPU/CUDA/ROCm) e o caminho
    manual (DirectML), com fallback automatico e permanente para CPU se o
    DirectML falhar em qualquer momento.
    """
    global _use_manual_forward, _dml_broken, _device_str, _device_backend, _audio_tagger

    import torch

    with _inference_lock:
        if _use_manual_forward and not _dml_broken:
            try:
                return _forward_on_device(audio_batch)
            except Exception as e:
                err_msg = repr(e) if isinstance(e, UnicodeDecodeError) else str(e)
                print(f"  [PANNs] DirectML falhou em runtime ({type(e).__name__}: {err_msg}). "
                      f"Desligando GPU e usando CPU pro resto da sessao.", flush=True)
                _dml_broken = True
                try:
                    _audio_tagger.model.to("cpu")
                except Exception:
                    pass
                _device_str, _device_backend = "cpu", "cpu"
                _use_manual_forward = False

        with torch.inference_mode():
            clipwise_output, _embedding = _audio_tagger.inference(audio_batch)
        return clipwise_output


# ===========================================================================
# API publica
# ===========================================================================

def classify_with_panns(audio_path: str, output_language: str = "pt") -> dict[str, Any]:
    """Executa o modelo PANNs (Cnn14) sobre um arquivo de audio e retorna um
    dict no mesmo formato que os outros backends:

        {"category": ..., "instrument": ..., "confidence": ..., "notes": ...}

    Usa _pick_label_aggregated() para maxima precisao por agregacao de scores.
    """
    try:
        _ensure_ready()
    except ImportError:
        return {"error": "panns_inference/torch nao instalado. Rode: pip install panns_inference torch"}
    except Exception as e:
        return {"error": f"falha ao inicializar PANNs: {type(e).__name__}: {e}"}

    try:
        data = _load_audio_32k_mono(audio_path)
        audio = data[None, :]  # (batch_size=1, samples)

        clipwise_output = _run_inference(audio)
        scores = clipwise_output[0]

        category, instrument, confidence = _pick_label_aggregated(scores, output_language)

        return {
            "category": category,
            "instrument": instrument,
            "confidence": confidence,
            "notes": f"PANNs / Cnn14 (local inference, device={_device_backend})",
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def classify_many_with_panns(
    audio_paths: list[str],
    output_language: str = "pt",
) -> list[dict[str, Any]]:
    """Versao em lote de classify_with_panns(): classifica VARIAS faixas em UM
    UNICO forward pass, em vez de uma chamada por faixa.

    Por que isso importa para performance: cada chamada Python->PyTorch tem
    overhead fixo (lancar kernels, sincronizar, etc.), e em GPU um batch
    grande usa muito mais dos nucleos disponiveis de uma vez do que N
    chamadas sequenciais de batch=1. Para processar um projeto inteiro do
    Reaper (dezenas/centenas de stems), isso e a otimizacao de maior
    impacto depois de acertar o device.

    Retorna uma lista de dicts, na MESMA ORDEM de `audio_paths`, no mesmo
    formato de classify_with_panns(). Se uma faixa individual falhar ao
    carregar (arquivo corrompido etc.), o dict dela vem com "error" mas as
    outras faixas do lote nao sao afetadas.

    Usa _pick_label_aggregated() por precisao maxima.
    """
    try:
        _ensure_ready()
    except ImportError:
        return [{"error": "panns_inference/torch nao instalado. Rode: pip install panns_inference torch"}
                for _ in audio_paths]
    except Exception as e:
        return [{"error": f"falha ao inicializar PANNs: {type(e).__name__}: {e}"} for _ in audio_paths]

    if not audio_paths:
        return []

    arrays: list[np.ndarray | None] = []
    load_errors: dict[int, str] = {}
    for i, path in enumerate(audio_paths):
        try:
            arrays.append(_load_audio_32k_mono(path))
        except Exception as e:
            arrays.append(None)
            load_errors[i] = f"{type(e).__name__}: {e}"

    valid_lens = [a.shape[0] for a in arrays if a is not None]
    if not valid_lens:
        return [{"error": load_errors.get(i, "falha ao carregar audio")} for i in range(len(audio_paths))]

    max_len = max(valid_lens)
    batch = np.zeros((len(arrays), max_len), dtype=np.float32)
    valid_mask = [False] * len(arrays)
    for i, a in enumerate(arrays):
        if a is not None:
            batch[i, :a.shape[0]] = a
            valid_mask[i] = True

    try:
        clipwise_output = _run_inference(batch)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        return [{"error": load_errors.get(i, err)} for i in range(len(audio_paths))]

    results: list[dict[str, Any]] = []
    for i in range(len(audio_paths)):
        if not valid_mask[i]:
            results.append({"error": load_errors.get(i, "falha ao carregar audio")})
            continue
        category, instrument, confidence = _pick_label_aggregated(clipwise_output[i], output_language)
        results.append({
            "category": category,
            "instrument": instrument,
            "confidence": confidence,
            "notes": f"PANNs / Cnn14 (local inference, batch, device={_device_backend})",
        })
    return results


if __name__ == "__main__":
    # Teste rapido via linha de comando:
    #   python panns_classify.py caminho/para/audio.wav
    import sys
    import json

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