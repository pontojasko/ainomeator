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

3. RESAMPLE: trocamos `np.interp` (interpolacao linear — funciona, mas gera
   aliasing e e mais lenta do que parece) por `soxr` (libsoxr, o mesmo
   resampler que o librosa usa por padrao hoje em dia). Medido neste
   projeto (8s @ 44100Hz -> 32000Hz): np.interp ~6.5ms vs soxr ~3.3ms por
   faixa — quase 2x mais rapido, e sem aliasing. Se `soxr` nao estiver
   instalado, cai pra `scipy.signal.resample_poly` com a razao up/down
   aproximada (ver `_resample` pra entender por que a razao EXATA seria
   mais lenta que o np.interp), e se nem scipy tiver, cai pro np.interp
   original — nunca quebra por falta de dependencia opcional.

4. TOP-K: trocamos `np.argsort` (O(n log n) nas 527 classes inteiras) por
   `np.argpartition` (O(n)) pra achar so as top-10, e só ordenamos essas 10.
   Ganho pequeno em termos absolutos (527 elementos e pouco), mas e "gratis"
   e some com qualquer razao de nao fazer.

5. GPU (NVIDIA / AMD):
   - NVIDIA CUDA: já funcionava (`torch.cuda.is_available()`).
   - AMD no Linux (ROCm): um PyTorch compilado com ROCm expõe a MESMA API
     `torch.cuda.*` (é a forma que a AMD escolheu para ter compat com o
     ecossistema CUDA). Ou seja: com o pacote certo instalado
     (ver README/requirements), o AMD já cai automaticamente no mesmo
     caminho "cuda" — nao tivemos que mudar nada na logica, so garantir que
     nao tem NADA hardcoded assumindo NVIDIA.
   - AMD/Intel no Windows (DirectML): ROCm nao roda no Windows. A unica
     rota de GPU nesse caso e `torch-directml`. A lib `panns_inference` so
     entende os STRINGS 'cuda' ou 'cpu' internamente (ela faz
     `.cuda()` hardcoded), entao nao da pra so passar device='dml'. Por
     isso, quando detectamos DirectML, construimos o modelo em CPU e
     movemos o `nn.Module` manualmente pro device DML, e escrevemos nosso
     proprio laco de inferencia (`_forward_on_device`) no lugar de
     `at.inference()`.
     CUIDADO: o torch-directml tem incompatibilidades conhecidas. por
     exemplo, o bug do `torch.inference_mode()` (microsoft/DirectML#602,
     "Cannot set version_counter"), que contornamos usando `torch.no_grad()`
     no `_forward_on_device`. alem disso, se qualquer outra operacao
     falhar em runtime, o codigo detecta, desliga DML PRA SEMPRE nesse
     processo (evita tentar nas proximas 300 faixas) e cai pra CPU
     automaticamente, sem derrubar o batch inteiro.

6. INFERENCIA EM LOTE (`classify_many_with_panns`): a funcao original
   processa um arquivo por chamada. Se voce esta processando N faixas de
   uma vez (ex: varios stems do mesmo projeto), rodar 1 forward pass com um
   batch de N e MUITO mais rapido que N forward passes separados — menos
   overhead de Python por chamada e, em GPU, paraleliza de verdade dentro
   do proprio device. Use essa funcao quando estiver classificando varias
   faixas em sequencia com o backend "panns" puro (sem hibrido/Gemini).
=====================================================================
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
    "A capella":                      ("vocal", "Vocal", "Vocal"),
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
    "Acoustic guitar":                ("guitarra", "Violão", "Acoustic guitar"),
    "Electric guitar":                ("guitarra", "Guitarra", "Electric guitar"),
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

    # FX / Ruido / Outros
    "Vehicle":                        ("outro", "FX (Veículo)", "FX (Vehicle)"),
    "Motor vehicle (road)":           ("outro", "FX (Veículo)", "FX (Vehicle)"),
    "Car":                            ("outro", "FX (Carro)", "FX (Car)"),
    "Engine":                         ("outro", "FX (Motor)", "FX (Engine)"),
    "White noise":                    ("outro", "FX (Ruído branco)", "FX (White noise)"),
    "Pink noise":                     ("outro", "FX (Ruído rosa)", "FX (Pink noise)"),
    "Noise":                          ("outro", "FX (Ruído)", "FX (Noise)"),
    "Wind":                           ("outro", "FX (Vento)", "FX (Wind)"),
    "Rain":                           ("outro", "FX (Chuva)", "FX (Rain)"),
    "Water":                          ("outro", "FX (Água)", "FX (Water)"),
    "Explosion":                      ("outro", "FX (Explosão)", "FX (Explosion)"),
    "Gunshot, gunfire":               ("outro", "FX (Tiro)", "FX (Gunshot)"),
    "Bird":                           ("outro", "FX (Pássaro)", "FX (Bird)"),
    "Animal":                         ("outro", "FX (Animal)", "FX (Animal)"),
}


# ===========================================================================
# Estado global do processo (tudo carregado/configurado UMA UNICA VEZ)
# ===========================================================================

_init_lock = threading.Lock()
_inference_lock = threading.Lock()
_ready = False

_audio_tagger = None
_panns_labels = None
_device_str = None          # 'cuda', 'cpu', ou um device DirectML
_device_backend = None      # 'cuda(nvidia)', 'rocm(amd)', 'directml', 'cpu'
_use_manual_forward = False  # True quando precisamos bypassar at.inference() (caso DirectML)
_dml_broken = False          # vira True se DML falhar em runtime -> cai pra CPU pro resto do processo


def _detect_device():
    """
    Escolhe o melhor device disponivel, nessa ordem de preferencia:

        1. CUDA  -> cobre NVIDIA (CUDA de verdade) E AMD no Linux (ROCm),
                    porque um torch compilado com ROCm reusa a MESMA API
                    torch.cuda.*. Nao ha necessidade de codigo especial pra
                    "AMD no Linux": se o usuario instalou o torch+ROCm certo,
                    isso ja cai aqui sozinho.
        2. DirectML -> unica rota de GPU pra AMD/Intel no Windows (sem
                    ROCm). Best-effort: pode nao suportar todos os operadores
                    que o Cnn14 usa.
        3. CPU   -> sempre funciona, fallback final.

    Pode ser forcado via variavel de ambiente PANNS_DEVICE=cuda|dml|cpu|auto.
    """
    import torch

    forced = os.environ.get("PANNS_DEVICE", "auto").strip().lower()

    if forced == "cpu":
        return "cpu", "cpu"

    if forced in ("auto", "cuda") and torch.cuda.is_available():
        # cudnn.benchmark: os segmentos analisados tem tamanho fixo (mesma
        # duracao em amostras na maioria dos casos), entao vale a pena deixar
        # o cuDNN testar/escolher o algoritmo de convolucao mais rapido pro
        # shape de entrada na primeira chamada e reusar depois.
        torch.backends.cudnn.benchmark = True
        try:
            # torch.version.hip so existe (e nao e None) em builds ROCm.
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


def _ensure_ready():
    """
    Roda TUDO que so precisa acontecer uma vez por processo: .env, threads
    do torch, import de torch/panns_inference, deteccao de device e
    carregamento do checkpoint. Protegido por lock (varias threads podem
    chamar isso ao mesmo tempo no ThreadPoolExecutor do batch_rename.py).
    """
    global _ready, _audio_tagger, _panns_labels
    global _device_str, _device_backend, _use_manual_forward

    if _ready:
        return

    with _init_lock:
        if _ready:  # outra thread pode ter inicializado enquanto esperavamos o lock
            return

        # --- .env: le do disco UMA vez, nao a cada faixa ------------------
        try:
            from config import load_env
            load_env()
        except ImportError:
            pass

        import torch

        # Gradientes desligados globalmente: este processo so faz inferencia,
        # nunca treina nada. Evita qualquer alocacao de autograd por engano
        # em algum ponto do pipeline.
        torch.set_grad_enabled(False)

        panns_threads = os.environ.get("PANNS_THREADS")
        if panns_threads:
            try:
                torch.set_num_threads(int(panns_threads))
            except ValueError:
                pass
        # Sem PANNS_THREADS definido, mantemos o default do torch (usa todos
        # os cores). Se voce roda o batch com --workers > 1 (varias faixas
        # em paralelo), vale a pena setar PANNS_THREADS=<cores/workers> pra
        # evitar oversubscription (N threads de intra-op x M workers
        # concorrentes brigando pelos mesmos cores).

        from panns_inference import AudioTagging, labels

        device, backend = _detect_device()
        manual_forward = False

        import sys
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            if backend == "directml":
                # DirectML bug workaround: panns_inference AudioTagging falha ao dar
                # load() direto no device="directml". Entao inicializamos o AudioTagging
                # em CPU (rapido, so aloca os pesos) e movemos o nn.Module pra
                # GPU manualmente. A partir daqui, NAO usamos mais at.inference()
                # (ele faria .cuda() hardcoded e ignoraria nosso device DML) -
                # usamos _forward_on_device() no lugar.
                at = AudioTagging(checkpoint_path=None, device="cpu")
                try:
                    at.model.to(device)
                    manual_forward = True
                except Exception as e:
                    try:
                        err_msg = str(e)
                    except UnicodeDecodeError:
                        err_msg = repr(e)
                    sys.stdout = old_stdout
                    print(f"  [PANNs] Nao foi possivel mover o modelo pra DirectML "
                          f"({type(e).__name__}: {err_msg}). Usando CPU.", flush=True)
                    sys.stdout = io.StringIO()
                    device, backend = "cpu", "cpu"
            else:
                try:
                    at = AudioTagging(checkpoint_path=None, device=device)
                except Exception as e:
                    try:
                        err_msg = str(e)
                    except UnicodeDecodeError:
                        err_msg = repr(e)
                    sys.stdout = old_stdout
                    print(f"  [PANNs] Nao foi possivel inicializar no device {backend} "
                          f"({type(e).__name__}: {err_msg}). Caindo para CPU.", flush=True)
                    sys.stdout = io.StringIO()
                    device, backend = "cpu", "cpu"
                    at = AudioTagging(checkpoint_path=None, device="cpu")
        finally:
            sys.stdout = old_stdout

        print(f"  [PANNs] CNN14 carregado (device={backend}) (~300MB, so na primeira faixa)")

        _audio_tagger = at
        _panns_labels = labels
        _device_str = device
        _device_backend = backend
        _use_manual_forward = manual_forward
        _ready = True


def _get_model():
    _ensure_ready()
    return _audio_tagger, _panns_labels


# ===========================================================================
# Audio: leitura + resample otimizado
# ===========================================================================

def _resample(data, sr, target_sr):
    """
    Reamostra `data` (mono, float32) de `sr` pra `target_sr`. Sem comprimir,
    sem cortar, sem tocar no conteudo — so muda a taxa de amostragem.

    Cadeia de prioridade (a primeira que estiver disponivel/segura e usada):

    1. `soxr` (libsoxr): o resampler que o proprio librosa usa por padrao
       hoje em dia (`res_type="soxr_hq"`). Filtro polifasico em C++ bem
       otimizado, ~2x mais rapido que np.interp NO BENCHMARK DESTE PROJETO
       (8s @ 44100->32000: ~3.3ms vs ~6.5ms) e com qualidade bem superior
       (sem aliasing). Melhor opcao na quase totalidade dos casos.
       pip install soxr

    2. `scipy.signal.resample_poly` (filtro FIR polifasico, janela Kaiser),
       como fallback se soxr nao estiver instalado. CUIDADO: passar a razao
       EXATA up/down (ex: 44100->32000 = 320/441) faz o resample_poly
       upsamplar por 320x ANTES de decimar — pra um segmento de 8s isso
       gera um array intermediario de ~113 milhoes de amostras e fica mais
       LENTO que a interpolacao linear que estavamos tentando substituir
       (testado aqui: 0.29x, ou seja, PIOR). Por isso aproximamos a razao
       com `Fraction(...).limit_denominator(256)`: troca uma fracao de
       cent de afinacao (irrelevante pra classificar instrumento) por
       manter o filtro polifasico realmente rapido.

    3. `np.interp` (interpolacao linear) — ultimo recurso, sem dependencias
       extras, igual ao comportamento original.
    """
    if sr == target_sr:
        return data

    try:
        import soxr
        return soxr.resample(data, sr, target_sr).astype(np.float32)
    except ImportError:
        pass

    try:
        from scipy.signal import resample_poly
        from fractions import Fraction
        frac = Fraction(int(target_sr), int(sr)).limit_denominator(256)
        up, down = frac.numerator, frac.denominator
        return resample_poly(data, up, down).astype(np.float32)
    except ImportError:
        pass

    duration = len(data) / sr
    n_target = max(1, int(round(duration * target_sr)))
    x_old = np.linspace(0, duration, num=len(data), endpoint=False)
    x_new = np.linspace(0, duration, num=n_target, endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


def _load_audio_32k_mono(audio_path):
    """Le o audio com soundfile, converte pra mono e reamostra pra 32kHz
    (taxa que o PANNs/Cnn14 espera), sem depender de librosa/ffmpeg."""
    import soundfile as sf

    data, sr = sf.read(audio_path, always_2d=False, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1, dtype=np.float32)

    # Peak normalization
    max_val = np.max(np.abs(data)) if data.size else 0.0
    if max_val > 0:
        data = data / max_val

    data = _resample(data, sr, 32000)
    return np.ascontiguousarray(data, dtype=np.float32)


# ===========================================================================
# Inferencia
# ===========================================================================

def _pick_label(scores, output_language):
    """Recebe o vetor de scores (527,) de UMA faixa e devolve
    (category, instrument, confidence) usando o _LABEL_MAP."""
    K = 10
    if scores.shape[0] > K:
        # argpartition: O(n) pra achar as top-K, em vez de ordenar as 527
        # classes inteiras (O(n log n)) so pra usar as primeiras 10.
        unsorted_top = np.argpartition(scores, -K)[-K:]
        top_indices = unsorted_top[np.argsort(scores[unsorted_top])[::-1]]
    else:
        top_indices = np.argsort(scores)[::-1]

    for idx in top_indices:
        label_name = _panns_labels[idx]
        if label_name in _LABEL_MAP:
            category, inst_pt, inst_en = _LABEL_MAP[label_name]
            confidence = float(scores[idx])
            instrument = inst_en if output_language == "en" else inst_pt
            return category, instrument, round(confidence, 3)

    top_idx = int(top_indices[0])
    label_name = _panns_labels[top_idx]
    return "outro", label_name, round(float(scores[top_idx]), 3)


def _forward_on_device(audio_batch):
    """
    Roda o forward pass manualmente no device configurado (usado no caminho
    DirectML, onde nao podemos usar at.inference() porque ela faz .cuda()
    hardcoded). audio_batch: numpy (batch, samples) float32.

    Retorna clipwise_output (batch, 527) como numpy array.

    Se qualquer coisa falhar aqui (operador nao suportado no DirectML, por
    exemplo), o chamador (`_run_inference`) desliga DML pro resto do
    processo e refaz a chamada em CPU.
    """
    import torch

    tensor = torch.as_tensor(audio_batch, dtype=torch.float32, device=_device_str)
    
    # workaround para microsoft/DirectML#602: inference_mode quebra no DML
    with torch.no_grad():
        _audio_tagger.model.eval()
        output_dict = _audio_tagger.model(tensor, None)
    return output_dict["clipwise_output"].detach().to("cpu").numpy()


def _run_inference(audio_batch):
    """
    Ponto unico de entrada pra rodar o modelo, batched. Decide entre o
    caminho normal (at.inference(), usado por CPU/CUDA/ROCm) e o caminho
    manual (DirectML), com fallback automatico e permanente pra CPU se o
    DirectML falhar em qualquer momento.
    """
    global _use_manual_forward, _dml_broken, _device_str, _device_backend, _audio_tagger

    import torch

    with _inference_lock:
        if _use_manual_forward and not _dml_broken:
            try:
                return _forward_on_device(audio_batch)
            except Exception as e:
                try:
                    err_msg = str(e)
                except UnicodeDecodeError:
                    err_msg = repr(e)
                
                # If it's an Out Of Memory error, raise it so the caller can reduce batch size
                # Workaround for torch-directml in non-English Windows: the C++ driver raises 
                # a localized OOM exception (e.g. "Memória esgotada") containing non-UTF8 chars.
                # Pybind11 fails to decode this to Python and raises UnicodeDecodeError instead!
                err_lower = err_msg.lower()
                is_oom = ("memory" in err_lower or "allocate" in err_lower or "oom" in err_lower)
                is_pybind_encoding_bug = isinstance(e, UnicodeDecodeError)
                
                if is_oom or is_pybind_encoding_bug:
                    # Raise a clean RuntimeError so the caller knows it's an OOM and can reduce batch size
                    raise RuntimeError("DirectML OOM (Memory error or Pybind11 decoding bug caught)")
                
                print(f"  [PANNs] DirectML falhou em runtime ({type(e).__name__}: {err_msg}). "
                      f"Desligando GPU e usando CPU pro resto da sessao "
                      f"(provavelmente alguma operacao do modelo nao suportada pelo torch-directml).", flush=True)
                _dml_broken = True
                try:
                    _audio_tagger.model.to("cpu")
                except Exception:
                    pass
                _device_str, _device_backend = "cpu", "cpu"
                _use_manual_forward = False
                # cai pro caminho normal abaixo, agora em cpu

        with torch.inference_mode():
            clipwise_output, _embedding = _audio_tagger.inference(audio_batch)
        return clipwise_output


def classify_with_panns(audio_input, output_language="pt"):
    """
    Executa o modelo PANNs (Cnn14) sobre um arquivo de audio ou um array NumPy
    e retorna um dict no mesmo formato que os outros backends:

        {"category": ..., "instrument": ..., "confidence": ..., "notes": ...}

    audio_input: str (caminho para arquivo) OU np.ndarray (audio ja carregado
        em mono 32kHz float32 — evita releitura do disco quando o segmento
        ja foi extraido em memoria por outro modulo).
    """
    try:
        _ensure_ready()
    except ImportError:
        return {"error": "panns_inference/torch nao instalado. Rode: pip install panns_inference torch"}
    except Exception as e:
        return {"error": f"falha ao inicializar PANNs: {type(e).__name__}: {e}"}

    try:
        if isinstance(audio_input, np.ndarray):
            data = np.ascontiguousarray(audio_input, dtype=np.float32)
        else:
            data = _load_audio_32k_mono(audio_input)
        audio = data[None, :]  # (batch_size=1, samples)

        clipwise_output = _run_inference(audio)
        scores = clipwise_output[0]

        category, instrument, confidence = _pick_label(scores, output_language)

        return {
            "category": category,
            "instrument": instrument,
            "confidence": confidence,
            "notes": f"PANNs / Cnn14 (local inference, device={_device_backend})",
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def classify_many_with_panns(audio_inputs, output_language="pt"):
    """
    Versao em lote de classify_with_panns(): classifica VARIAS faixas em UM
    UNICO forward pass, em vez de uma chamada por faixa.

    Por que isso importa pra performance: cada chamada Python->PyTorch tem
    overhead fixo (lancar kernels, sincronizar, etc.), e em GPU um batch
    grande usa muito mais dos nucleos disponiveis de uma vez do que N
    chamadas sequenciais de batch=1. Pra processar um projeto inteiro do
    Reaper (dezenas/centenas de stems), isso e a otimizacao de maior
    impacto depois de acertar o device.

    audio_inputs: lista de str (caminhos) OU np.ndarray (arrays ja carregados
        em mono 32kHz float32). Pode misturar os dois tipos na mesma lista.

    Retorna uma lista de dicts, na MESMA ORDEM de `audio_inputs`, no mesmo
    formato de classify_with_panns(). Se uma faixa individual falhar ao
    carregar (arquivo corrompido etc.), o dict dela vem com "error" mas as
    outras faixas do lote nao sao afetadas.
    """
    try:
        _ensure_ready()
    except ImportError:
        return [{"error": "panns_inference/torch nao instalado. Rode: pip install panns_inference torch"}
                for _ in audio_inputs]
    except Exception as e:
        return [{"error": f"falha ao inicializar PANNs: {type(e).__name__}: {e}"} for _ in audio_inputs]

    if not audio_inputs:
        return []

    arrays = []
    load_errors = {}
    for i, item in enumerate(audio_inputs):
        try:
            if isinstance(item, np.ndarray):
                arrays.append(np.ascontiguousarray(item, dtype=np.float32))
            else:
                arrays.append(_load_audio_32k_mono(item))
        except Exception as e:
            arrays.append(None)
            load_errors[i] = f"{type(e).__name__}: {e}"

    valid_lens = [a.shape[0] for a in arrays if a is not None]
    if not valid_lens:
        return [{"error": load_errors.get(i, "falha ao carregar audio")} for i in range(len(audio_inputs))]

    MAX_SAMPLES = 32000 * 10  # 10 segundos por chunk
    flat_chunks = []
    chunk_to_track_idx = []
    valid_mask = [False] * len(arrays)

    for i, a in enumerate(arrays):
        if a is not None and a.shape[0] > 0:
            valid_mask[i] = True
            total_samples = a.shape[0]
            num_chunks = int(np.ceil(total_samples / MAX_SAMPLES))
            for c in range(num_chunks):
                start = c * MAX_SAMPLES
                end = min(total_samples, (c + 1) * MAX_SAMPLES)
                chunk_data = a[start:end]
                
                # Ignora recortes completamente silenciosos (abaixo de -46dBFS).
                # Como a faixa inteira ja sofreu peak normalization (max=1.0), 
                # qualquer coisa abaixo de 0.005 e ruido de chao ou silencio digital.
                # Se mandarmos zeros pro PANNs, ele tem um vies de classificar como "Speech/Vocal" com ~15% de confianca, 
                # o que polui a nota final se a faixa tiver 3 minutos de silencio e 10s de piano!
                if np.max(np.abs(chunk_data)) < 0.005:
                    continue
                
                padded = np.zeros(MAX_SAMPLES, dtype=np.float32)
                padded[:chunk_data.shape[0]] = chunk_data
                flat_chunks.append(padded)
                chunk_to_track_idx.append(i)

    if not flat_chunks:
        return [{"error": "falha ao carregar audio"} for _ in range(len(audio_inputs))]

    batch = np.array(flat_chunks, dtype=np.float32)

    clipwise_output_list = []
    # Cap chunk_size at 32 (320s of audio) to prevent Windows TDR (GPU timeout)
    # If we send 300 chunks at once, DirectML takes > 2 seconds and Windows kills the GPU driver!
    chunk_size = min(batch.shape[0], 32)
    i = 0
    while i < batch.shape[0]:
        chunk = batch[i : i + chunk_size]
        try:
            chunk_out = _run_inference(chunk)
            clipwise_output_list.append(chunk_out)
            i += chunk_size
        except RuntimeError as e:
            err_lower = str(e).lower()
            if ("memory" in err_lower or "allocate" in err_lower or "oom" in err_lower) and chunk_size > 1:
                chunk_size = chunk_size // 2
                print(f"  [PANNs] DirectML/GPU OOM. Reduzindo lote para {chunk_size} recortes simultaneos...", flush=True)
            elif ("memory" in err_lower or "allocate" in err_lower or "oom" in err_lower) and chunk_size == 1:
                print("  [PANNs] DirectML sem memoria ate para 1 recorte (ou GPU travou). Caindo para CPU pro resto da sessao...", flush=True)
                global _use_manual_forward, _dml_broken, _device_str, _device_backend, _audio_tagger
                _dml_broken = True
                try:
                    _audio_tagger.model.to("cpu")
                except Exception:
                    pass
                _device_str, _device_backend = "cpu", "cpu"
                _use_manual_forward = False
                # Nao incrementamos 'i', entao na proxima iteracao ele vai rodar o mesmo chunk, so que na CPU!
            else:
                err = f"{type(e).__name__}: {e}"
                return [{"error": load_errors.get(idx, err)} for idx in range(len(audio_inputs))]
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            return [{"error": load_errors.get(idx, err)} for idx in range(len(audio_inputs))]

    clipwise_output = np.concatenate(clipwise_output_list, axis=0) if clipwise_output_list else np.empty((0, 527))

    # Reagrupa os chunks calculados de volta para cada faixa (media dos scores)
    track_outputs = {idx: [] for idx in range(len(audio_inputs))}
    for c_idx, t_idx in enumerate(chunk_to_track_idx):
        track_outputs[t_idx].append(clipwise_output[c_idx])

    results = []
    for i in range(len(audio_inputs)):
        if not valid_mask[i] or len(track_outputs[i]) == 0:
            results.append({"error": load_errors.get(i, "falha ao carregar audio")})
            continue
        # Usa Media Generalizada (L3-norm) em vez de np.mean ou np.max!
        # O np.mean dilui o score de instrumentos esparsos (flauta toca 10s num arquivo de 3min).
        # O np.max e muito sensivel a picos falsos (ruido num chunk quieto classificado como Violao com 80%).
        # A norma L3 (p=3) penaliza valores baixos e recompensa valores altos, encontrando o balanco 
        # perfeito entre "pico forte" e "consistencia ao longo da faixa".
        chunk_scores_arr = np.array(track_outputs[i])
        l3_scores = np.mean(chunk_scores_arr ** 3, axis=0) ** (1/3)
        category, instrument, confidence = _pick_label(l3_scores, output_language)
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