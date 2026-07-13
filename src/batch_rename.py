"""
batch_rename.py

FASE 4: script chamado pelo ReaScript (Lua) de dentro do Reaper.

Le um "manifest" (lista de tracks + caminho do audio de cada uma) de um
arquivo de texto BEM leve (tab-separated, sem JSON/XML), classifica cada
track e escreve o resultado em outro arquivo de texto igualmente leve,
pro Lua ler de volta sem precisar de nenhuma lib de JSON dentro do ReaScript.

Formato do manifest (uma linha por track, SEM cabecalho):
    idx<TAB>caminho_do_audio<TAB>inicio_segundos<TAB>duracao_segundos

Formato do resultado (uma linha por track, SEM cabecalho):
    idx<TAB>status<TAB>categoria<TAB>instrumento<TAB>confianca<TAB>erro

    status e "ok" ou "erro". Em caso de erro, categoria/instrumento/
    confianca vem vazios e o campo erro tem a mensagem.

Uso:
    python batch_rename.py manifest.tsv resultado.tsv \\
        [--workers 5] [--segment-seconds 8] [--models m1,m2,m3]
"""

import sys
import os

# --- Verify dependencies ---
REQUIRED_PACKAGES = [
    ("dotenv", "python-dotenv"),
    ("google.genai", "google-genai"),
    ("numpy", "numpy"),
    ("soundfile", "soundfile"),
    ("panns_inference", "panns-inference"),
    ("torch", "torch"),
    ("soxr", "soxr"),
    ("scipy", "scipy"),
]

missing_packages = []
for module_name, pip_name in REQUIRED_PACKAGES:
    try:
        __import__(module_name)
    except ImportError:
        missing_packages.append(pip_name)

if missing_packages:
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    print("\n" + "="*60)
    print("[ERRO] Dependências do Python ausentes / Missing Python dependencies!")
    print("="*60)
    print("As seguintes bibliotecas necessárias não estão instaladas:")
    for pkg in missing_packages:
        print(f"  - {pkg}")
    print("\nPara corrigir, execute o arquivo 'setup.bat' na pasta do projeto.")
    print("Please run 'setup.bat' in the project directory to install dependencies.")
    print("="*60 + "\n")
    sys.exit(1)

import argparse
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Garante que cada print() vai pro arquivo de log IMEDIATAMENTE (line-buffered)
# e em UTF-8, independente do locale do Windows (CP1252 por padrao ao redirecionar).
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except AttributeError:
    pass

from dotenv import load_dotenv
from google import genai
from google.genai import types

from audio_utils import extract_best_segment, downmix_resample, extract_three_peaks, convert_to_mp3_128k
from classify_track import classify_audio_bytes, MODELOS_FALLBACK, build_chaining_prompt
from panns_classify import classify_with_panns, classify_many_with_panns

# Backends locais disponíveis (apenas PANNs — yamnet/essentia removidos)
LOCAL_BACKENDS = {
    "panns": classify_with_panns,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(text):
    if text is None:
        return ""
    return str(text).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def _prepare_audio_for_gemini(tmp_seg_path, quality):
    """
    Prepara os bytes de áudio para envio ao Gemini a partir de um segmento WAV já extraído.
    - quality=='alta': envia o WAV diretamente.
    - quality outro: tenta MP3 128k via ffmpeg; fallback para WAV mono 24kHz.

    Retorna (audio_bytes, mime_type). Cria e remove temp files internamente.
    """
    if quality == "alta":
        with open(tmp_seg_path, "rb") as f:
            return f.read(), "audio/wav"

    tmp_mp3_fd, tmp_mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="ai_namer_mp3_")
    os.close(tmp_mp3_fd)
    try:
        if convert_to_mp3_128k(tmp_seg_path, tmp_mp3_path):
            with open(tmp_mp3_path, "rb") as f:
                return f.read(), "audio/mp3"
    finally:
        if os.path.isfile(tmp_mp3_path):
            try:
                os.remove(tmp_mp3_path)
            except OSError:
                pass

    # Fallback: WAV mono 24kHz
    tmp_light_fd, tmp_light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
    os.close(tmp_light_fd)
    try:
        downmix_resample(tmp_seg_path, tmp_light_path)
        with open(tmp_light_path, "rb") as f:
            return f.read(), "audio/wav"
    finally:
        if os.path.isfile(tmp_light_path):
            try:
                os.remove(tmp_light_path)
            except OSError:
                pass


def check_api_availability(client, models, output_language="pt"):
    if output_language == "pt":
        print("\n[batch_rename] Verificando disponibilidade da API Gemini...")
    else:
        print("\n[batch_rename] Checking Gemini API availability...")
    for i, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents=["Responda apenas 'OK'."],
                config=types.GenerateContentConfig(temperature=0.1)
            )
            if response.text:
                if output_language == "pt":
                    print(f"  [OK] API disponível (modelo: {model}).")
                else:
                    print(f"  [OK] API available (model: {model}).")
                return True, models[i:]
        except Exception as e:
            if output_language == "pt":
                print(f"  [ERRO] Falhou com {model} ({type(e).__name__}: {e}).")
            else:
                print(f"  [ERROR] Failed with {model} ({type(e).__name__}: {e}).")
    return False, models


# ---------------------------------------------------------------------------
# Track processing — local PANNs (single track, para hybrid)
# ---------------------------------------------------------------------------

def _process_one_local(idx, audio_path, start_sec, dur_sec, segment_seconds, quality, output_language, backend_name):
    """Processa UMA track usando PANNs local. Usado no modo de thread por track."""
    classify_fn = LOCAL_BACKENDS[backend_name]
    tmp_seg_path = None
    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        if quality == "alta":
            if search_start is None and search_dur is None:
                result = classify_fn(audio_path, output_language=output_language)
            else:
                tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
                os.close(tmp_seg_fd)
                extract_best_segment(
                    audio_path, tmp_seg_path, segment_seconds=search_dur,
                    search_start_seconds=search_start,
                    search_duration_seconds=search_dur,
                )
                result = classify_fn(tmp_seg_path, output_language=output_language)
        else:
            tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
            os.close(tmp_seg_fd)
            extract_best_segment(
                audio_path, tmp_seg_path, segment_seconds=segment_seconds,
                search_start_seconds=search_start,
                search_duration_seconds=search_dur,
            )
            result = classify_fn(tmp_seg_path, output_language=output_language)

        result["_model_usado"] = backend_name
        return idx, result
    except Exception as e:
        return idx, {"error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp_seg_path and os.path.isfile(tmp_seg_path):
            try:
                os.remove(tmp_seg_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# DSP sanity checker (usado apenas pelo modo hybrid)
# ---------------------------------------------------------------------------

def analyze_dsp_properties(audio_path):
    """
    Analisa propriedades de DSP básico para o verificador de sanidade do modo híbrido:
    1. Concentração de energia abaixo de 100Hz via FFT.
    2. Proporção de frames com decaimento abrupto (senso de percussão/staccato).
    """
    import numpy as np
    import soundfile as sf
    try:
        data, sr = sf.read(audio_path, always_2d=True)
        y = data.mean(axis=1) if data.shape[1] > 1 else data.flatten()

        if len(y) == 0:
            return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}

        fft_vals = np.fft.rfft(y)
        fft_freqs = np.fft.rfftfreq(len(y), d=1/sr)
        magnitudes = np.abs(fft_vals)
        energy = magnitudes ** 2
        total_energy = np.sum(energy)

        low_freq_ratio = 0.0
        if total_energy > 0:
            low_freq_ratio = np.sum(energy[fft_freqs < 100]) / total_energy

        frame_size = int(0.050 * sr)
        low_energy_ratio = 0.0
        if frame_size > 0 and len(y) >= frame_size:
            num_frames = len(y) // frame_size
            frames = y[:num_frames * frame_size].reshape((num_frames, frame_size))
            frame_max = np.max(np.abs(frames), axis=1)
            global_max = np.max(frame_max)
            if global_max > 0:
                low_energy_ratio = np.sum(frame_max < (0.1 * global_max)) / num_frames

        return {
            "low_freq_ratio": float(low_freq_ratio),
            "low_energy_ratio": float(low_energy_ratio)
        }
    except Exception as e:
        print(f"[DSP ERROR] Falha ao analisar propriedades DSP: {e}")
        return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}


# ---------------------------------------------------------------------------
# Hybrid processing (heuristic + chaining unified)
# ---------------------------------------------------------------------------

def _process_one_hybrid(client, idx, audio_path, start_sec, dur_sec, shared_models,
                         segment_seconds, quality, api_available, output_language, strategy):
    """
    Processa UMA track com análise híbrida (PANNs local + Gemini nuvem).

    strategy='heuristic': PANNs e Gemini em paralelo, árbitro decide o resultado.
    strategy='chaining':  PANNs primeiro, resultado injeta no prompt do Gemini para refinamento.
    """
    tmp_seg_path = None
    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
        os.close(tmp_seg_fd)
        extract_best_segment(
            audio_path, tmp_seg_path, segment_seconds=segment_seconds,
            search_start_seconds=search_start,
            search_duration_seconds=search_dur,
        )

        # Fallback sem API: só PANNs
        if not api_available or not client:
            panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
            if panns_result and "error" not in panns_result:
                panns_result["_model_usado"] = "panns_only_no_api"
                return idx, panns_result
            return idx, {"error": "API do Gemini nao disponivel e PANNs falhou"}

        audio_bytes, mime_type = _prepare_audio_for_gemini(tmp_seg_path, quality)
        current_models = list(shared_models)
        models_lock = threading.Lock()

        def on_model_failed(model_name):
            with models_lock:
                if model_name in shared_models:
                    shared_models.remove(model_name)

        if strategy == "chaining":
            # PANNs primeiro, depois Gemini com contexto
            panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
            if panns_result and "error" in panns_result:
                print(f"  [Chaining] PANNs falhou na track {idx}: {panns_result['error']}. Tentando apenas com Gemini.")
                panns_result = {"category": "desconhecida", "instrument": "falha na analise local", "confidence": 0.0}

            chaining_prompt = build_chaining_prompt(panns_result, output_language=output_language)
            gemini_result = classify_audio_bytes(
                client, audio_bytes, mime_type=mime_type,
                models=current_models, on_model_failed=on_model_failed,
                output_language=output_language, custom_prompt=chaining_prompt
            )
            if gemini_result and "error" not in gemini_result:
                gemini_result["_model_usado"] = "hybrid_chaining_review"
                return idx, gemini_result
            panns_result["_model_usado"] = "panns_gemini_failed"
            return idx, panns_result

        # strategy == "heuristic": PANNs + Gemini em paralelo, árbitro decide
        dsp_info = analyze_dsp_properties(tmp_seg_path)
        low_freq_ratio = dsp_info["low_freq_ratio"]
        low_energy_ratio = dsp_info["low_energy_ratio"]

        panns_result = None
        gemini_result = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_panns = executor.submit(classify_with_panns, tmp_seg_path, output_language=output_language)
            future_gemini = executor.submit(
                classify_audio_bytes,
                client, audio_bytes, mime_type=mime_type,
                models=current_models, on_model_failed=on_model_failed,
                output_language=output_language
            )
            try:
                panns_result = future_panns.result()
            except Exception as e_panns:
                panns_result = {"error": str(e_panns)}
            try:
                gemini_result = future_gemini.result()
            except Exception as e_gem:
                gemini_result = {"error": str(e_gem)}

        panns_ok = panns_result and "error" not in panns_result
        gemini_ok = gemini_result and "error" not in gemini_result

        if not panns_ok and not gemini_ok:
            err_msg = (f"CNN14 err: {panns_result.get('error') if panns_result else 'None'}; "
                       f"Gemini err: {gemini_result.get('error') if gemini_result else 'None'}")
            return idx, {"error": f"Ambas as IAs falharam: {err_msg}"}

        if not panns_ok:
            gemini_result["_model_usado"] = f"gemini_{gemini_result.get('_model_usado', 'hybrid')}_panns_failed"
            return idx, gemini_result
        if not gemini_ok:
            panns_result["_model_usado"] = "panns_gemini_failed"
            return idx, panns_result

        # --- Árbitro (Matriz de Decisão de Conflitos) ---
        p_cat = panns_result.get("category", "").lower()
        p_inst = panns_result.get("instrument", "").lower()
        g_cat = gemini_result.get("category", "").lower()
        g_inst = gemini_result.get("instrument", "").lower()

        def _safe_conf(result):
            try:
                v = result.get("confidence")
                return float(v) if v is not None and v != "" else 0.5
            except (ValueError, TypeError):
                return 0.5

        g_conf = _safe_conf(gemini_result)
        p_conf = _safe_conf(panns_result)

        final_category = gemini_result.get("category")
        final_instrument = gemini_result.get("instrument")
        final_confidence = g_conf
        notes_parts = [f"CNN14={panns_result.get('instrument')}({p_conf})", f"Gemini={gemini_result.get('instrument')}({g_conf})"]
        rule_applied = "fallback"

        shaker_keywords = ["shaker", "chocalho", "cabasa", "maraca", "percuss", "tambourine", "pandeiro", "claves", "castanholas", "caxixi"]
        is_gemini_shaker = g_cat == "bateria" or any(kw in g_inst for kw in shaker_keywords)

        if p_cat == "vocal" and is_gemini_shaker:
            final_category = "bateria"
            final_instrument = gemini_result.get("instrument") if any(kw in g_inst for kw in ["shaker", "chocalho", "cabasa", "maraca", "pandeiro"]) else ("Shaker" if output_language == "pt" else "Shaker")
            final_confidence = max(g_conf, p_conf)
            rule_applied = "prioridade_ritmica"
        elif "piano" in g_inst and (p_cat in ["baixo", "cordas"] or any(kw in p_inst for kw in ["baixo", "bass", "cello", "contrabaixo", "double bass"])):
            final_category = "baixo"
            final_instrument = "Baixo Pizzicato" if output_language == "pt" else "Pizzicato Bass"
            final_confidence = p_conf
            rule_applied = "transiente_grave"
        else:
            compatible = (p_cat == g_cat
                          or (p_cat in ["cordas", "baixo"] and g_cat in ["cordas", "baixo"])
                          or (p_cat in ["teclado", "synth"] and g_cat in ["teclado", "synth"])
                          or (p_cat in ["baixo", "synth"] and g_cat in ["baixo", "synth"]))

            if compatible:
                final_category = gemini_result.get("category")
                final_instrument = gemini_result.get("instrument")
                final_confidence = max(g_conf, p_conf)
                rule_applied = "consenso_absoluto"
            elif p_conf > 0.75 and p_cat in ["bateria", "baixo", "sopro", "cordas"]:
                final_category = panns_result.get("category")
                final_instrument = panns_result.get("instrument")
                final_confidence = p_conf
                rule_applied = "prioridade_cnn14_confiante"
            else:
                final_category = gemini_result.get("category")
                final_instrument = gemini_result.get("instrument")
                final_confidence = g_conf
                rule_applied = "prioridade_gemini_default"

        final_res = {
            "category": final_category,
            "instrument": final_instrument,
            "confidence": round(float(final_confidence), 3),
            "notes": f"Árbitro: {rule_applied} | " + " | ".join(notes_parts)
        }

        try:
            final_res["confidence"] = round(float(final_res.get("confidence", 0.5)), 3)
        except (TypeError, ValueError):
            final_res["confidence"] = 0.5

        # Verificador de Sanidade (DSP)
        orig_category = final_res.get("category")
        orig_instrument = final_res.get("instrument")
        notes = final_res.get("notes", "")
        if low_freq_ratio > 0.45:
            if orig_category not in ["baixo", "bateria"] or not any(kw in (orig_instrument or "").lower() for kw in ["bass", "baixo", "kick", "bumbo", "sub"]):
                final_res["category"] = "baixo"
                final_res["instrument"] = "Baixo/Bumbo (DSP Grave <100Hz)" if output_language == "pt" else "Bass/Kick (DSP Low-Freq <100Hz)"
                final_res["notes"] = notes + f" | [DSP Override: Grave (F={low_freq_ratio:.2f})]"
                print(f"  [DSP Override] Track {idx}: Grave extremo forçou categoria baixo (low_freq_ratio={low_freq_ratio:.2f})")
        elif low_energy_ratio > 0.75:
            if orig_category != "bateria" and not any(kw in (orig_instrument or "").lower() for kw in ["perc", "shaker", "drum", "hat", "hit"]):
                final_res["category"] = "bateria"
                final_res["instrument"] = "Percussão (DSP Transiente Curto)" if output_language == "pt" else "Percussion (DSP Short Transient)"
                final_res["notes"] = notes + f" | [DSP Override: Percussivo (S={low_energy_ratio:.2f})]"
                print(f"  [DSP Override] Track {idx}: Decaimento abrupto forçou percussão (low_energy_ratio={low_energy_ratio:.2f})")

        final_res["_model_usado"] = f"hybrid_{rule_applied}"
        return idx, final_res

    except Exception as e:
        return idx, {"error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp_seg_path and os.path.isfile(tmp_seg_path):
            try:
                os.remove(tmp_seg_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def check_absolute_silence(audio_path, start_sec=None, dur_sec=None):
    """Retorna True se o áudio (ou o trecho especificado) for silêncio absoluto (pico < -120dB)."""
    try:
        import soundfile as sf
        import numpy as np

        info = sf.info(audio_path)
        if info.frames == 0:
            return True

        start_frame = 0
        if start_sec is not None and start_sec >= 0:
            start_frame = int(start_sec * info.samplerate)

        frames_to_read = -1
        if dur_sec is not None and dur_sec > 0:
            frames_to_read = int(dur_sec * info.samplerate)

        if start_frame >= info.frames:
            return True

        data, sr = sf.read(audio_path, start=start_frame, frames=frames_to_read, always_2d=True)
        if len(data) == 0:
            return True

        return np.max(np.abs(data)) < 1e-6
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main dispatch per track (used by ThreadPoolExecutor for Gemini/hybrid)
# ---------------------------------------------------------------------------

def process_one(client, idx, audio_path, start_sec, dur_sec, shared_models,
                segment_seconds, quality, api_available, output_language, backend="gemini", cancel_flag=None):
    if cancel_flag and os.path.exists(cancel_flag):
        return idx, {"error": "cancelled"}

    if check_absolute_silence(audio_path, start_sec, dur_sec):
        return idx, {"error": "absolute_silence"}

    if backend == "panns":
        # PANNs puro: processado via batch no main(), não via thread individual
        return _process_one_local(idx, audio_path, start_sec, dur_sec, segment_seconds, quality, output_language, backend)
    if backend in ("hybrid_heuristic", "hybrid_chaining"):
        strategy = "chaining" if backend == "hybrid_chaining" else "heuristic"
        return _process_one_hybrid(client, idx, audio_path, start_sec, dur_sec, shared_models,
                                   segment_seconds, quality, api_available, output_language, strategy)
    if not api_available:
        return idx, {"category": "outro", "instrument": "Audio", "confidence": 0.0, "_model_usado": "fallback_universal"}

    # Backend Gemini puro
    tmp_seg_path = None
    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
        os.close(tmp_seg_fd)

        if quality == "alta":
            extract_best_segment(
                audio_path, tmp_seg_path, segment_seconds=segment_seconds,
                search_start_seconds=search_start, search_duration_seconds=search_dur,
            )
        else:
            extract_three_peaks(
                audio_path, tmp_seg_path,
                search_start_seconds=search_start,
                search_duration_seconds=search_dur,
                segment_seconds=4
            )

        audio_bytes, mime_type = _prepare_audio_for_gemini(tmp_seg_path, quality)

        current_models = list(shared_models)
        models_lock = threading.Lock()

        def on_model_failed(model_name):
            with models_lock:
                if model_name in shared_models:
                    shared_models.remove(model_name)

        result = classify_audio_bytes(
            client, audio_bytes, mime_type=mime_type,
            models=current_models, on_model_failed=on_model_failed,
            output_language=output_language
        )
        return idx, result
    except Exception as e:
        return idx, {"error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp_seg_path and os.path.isfile(tmp_seg_path):
            try:
                os.remove(tmp_seg_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Manifest reader
# ---------------------------------------------------------------------------

def read_manifest(path):
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            idx = int(parts[0])
            audio_path = parts[1]
            start_sec = float(parts[2]) if len(parts) > 2 and parts[2] != "" else None
            dur_sec = float(parts[3]) if len(parts) > 3 and parts[3] != "" else None
            entries.append((idx, audio_path, start_sec, dur_sec))
    return entries


# ---------------------------------------------------------------------------
# Color palette generation (Gemini-powered)
# ---------------------------------------------------------------------------

def generate_colors_ini(client, model, prompt_text):
    system_prompt = (
        "Voce e um designer de cores profissional para Estacoes de Audio Digital (DAWs).\n"
        "O usuario deseja personalizar a paleta de cores das faixas do Reaper com base no seguinte pedido:\n"
        f"\"{prompt_text}\"\n\n"
        "Crie uma paleta de cores coerente, elegante e profissional em formato INI.\n"
        "Gere exatamente as 14 chaves abaixo sob a secao [Cores] com valores HEX (ex: #E05A47):\n"
        "- vocal_principal (Vocal principal / lead)\n"
        "- backing_vocals (Backing vocals, dobras, coro)\n"
        "- bateria (Bateria principal / pecas)\n"
        "- percussao (Percussao secundaria, pandeiro, shaker)\n"
        "- baixo (Baixo eletrico ou acustico)\n"
        "- guitarra_eletrica (Guitarras eletricas bases/solos)\n"
        "- violao (Violao acustico, nylon, aco, ukulele, banjo)\n"
        "- teclado (Teclado, piano acustico, rhodes, wurlitzer)\n"
        "- synth (Sintetizadores, pads, arpeggios)\n"
        "- cordas (Secao de cordas orquestrais, violino, cello)\n"
        "- sopros (Metais e sopros, sax, trompete, trombone)\n"
        "- efeitos (Canais auxiliares de efeitos, reverb, delay, FX)\n"
        "- pastas (Canais pai / folders / busses que agrupam tracks)\n"
        "- outro (Cor padrao / outros instrumentos nao identificados)\n\n"
        "Diretrizes:\n"
        "1. As cores devem ser profissionais e faceis de ler no Reaper.\n"
        "2. Mantenha diferenca visual clara entre instrumentos parecidos.\n"
        "3. Mesmo 'tudo em verde': use tons, saturacoes e brilhos diferentes.\n"
        "4. Responda APENAS com a estrutura INI sob [Cores]. Sem markdown, sem texto extra."
    )

    response = client.models.generate_content(
        model=model,
        contents=[system_prompt],
        config=types.GenerateContentConfig(temperature=0.3),
    )
    text = response.text.strip()

    if "```" in text:
        parts = text.split("```")
        for part in parts:
            if "[Cores]" in part or "vocal_principal" in part:
                lines = part.strip().split("\n")
                if lines and lines[0].strip() in ("ini", "txt", "toml"):
                    lines.pop(0)
                text = "\n".join(lines).strip()
                break

    return text


def handle_color_generation(client, models, prompt_text, config_path, output_language="pt"):
    if output_language == "pt":
        print(f"\n[batch_rename] Gerando paleta de cores personalizada com o prompt: \"{prompt_text}\"...")
    else:
        print(f"\n[batch_rename] Generating custom color palette with prompt: \"{prompt_text}\"...")
    ERROS_TRANSITORIOS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DeadlineExceeded", "timeout")

    for idx_modelo, model in enumerate(models):
        for tentativa in range(1, 3):
            try:
                ini_content = generate_colors_ini(client, model, prompt_text)

                if "[Cores]" not in ini_content and "vocal_principal" not in ini_content:
                    raise ValueError("Resposta do modelo nao contem secao [Cores] ou chaves esperadas.")

                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(ini_content)

                if output_language == "pt":
                    print(f"  [OK] Nova paleta de cores salva com sucesso em: {config_path}")
                else:
                    print(f"  [OK] New color palette successfully saved in: {config_path}")
                return

            except Exception as e:
                erro_str = f"{type(e).__name__}: {e}"
                eh_transitorio = any(marcador in erro_str for marcador in ERROS_TRANSITORIOS)
                if not eh_transitorio or tentativa == 2:
                    break
                time.sleep(2)

        if idx_modelo < len(models) - 1:
            if output_language == "pt":
                print(f"  [{model} indisponivel para cores, tentando proximo modelo: {models[idx_modelo + 1]}...]")
            else:
                print(f"  [{model} unavailable for colors, trying next model: {models[idx_modelo + 1]}...]")

    if output_language == "pt":
        print("  [ERRO] Nao foi possivel gerar paleta de cores personalizada. Usando paleta existente.")
    else:
        print("  [ERROR] Could not generate custom color palette. Using existing palette.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Classifica varias tracks em paralelo (chamado pelo ReaScript)")
    parser.add_argument("manifest_path")
    parser.add_argument("result_path")
    parser.add_argument("--workers", type=int, default=5,
                        help="threads em paralelo para Gemini/hybrid (padrao: 5)")
    parser.add_argument("--segment-seconds", type=float, default=8)
    parser.add_argument("--models", default=None,
                        help="lista de modelos separados por virgula, em ordem de preferencia")
    parser.add_argument("--done-flag", default=None,
                        help="arquivo sentinela criado APOS o result.tsv ser gravado por completo")
    parser.add_argument("--color-prompt", default=None,
                        help="Prompt para gerar paleta de cores personalizada")
    parser.add_argument("--config-path", default=None,
                        help="Caminho do arquivo de cores .ini")
    parser.add_argument("--quality", default="normal",
                        help="Qualidade de analise: 'normal' ou 'alta'")
    parser.add_argument("--output-language", choices=["pt", "en"], default="pt",
                        help="idioma do campo instrument: pt ou en (padrao: pt)")
    parser.add_argument("--backend",
                        choices=["gemini", "panns", "hybrid_heuristic", "hybrid_chaining"],
                        default="gemini",
                        help="backend de classificacao")
    parser.add_argument("--panns-threads", type=int, default=None,
                        help="threads internas do PyTorch (PANNs) por worker")
    parser.add_argument("--start-offset", type=float, default=0.0,
                        help="tempo decorrido no ReaScript (Lua) antes do Python inicializar")
    args = parser.parse_args()

    # --- Env / config ---
    global t_start_global
    t_start_global = time.time() - args.start_offset
    load_dotenv()
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT_ENV = os.path.join(os.path.dirname(_SCRIPT_DIR), ".env")
    if os.path.exists(_PARENT_ENV):
        load_dotenv(_PARENT_ENV)

    print(f"  [+] Env / dependencias carregadas [{time.time() - t_start_global:.2f}s]", flush=True)

    if args.panns_threads is not None and args.panns_threads > 0:
        os.environ["PANNS_THREADS"] = str(args.panns_threads)

    api_key = os.environ.get("GEMINI_API_KEY")
    use_local_backend = (args.backend == "panns")

    # --- Gemini client (instanciado apenas uma vez) ---
    client = None
    if not use_local_backend:
        if not api_key:
            if args.output_language == "pt":
                print("ERRO: GEMINI_API_KEY nao encontrada (crie/edite o .env nesta pasta).")
                print("Dica: use --backend panns para classificacao local sem API key.")
            else:
                print("ERROR: GEMINI_API_KEY not found (create/edit .env in this folder).")
                print("Tip: use --backend panns for local classification without API key.")
            sys.exit(1)
        client = genai.Client(api_key=api_key)
    else:
        if args.output_language == "pt":
            print(f"[batch_rename] Backend: {args.backend} (local, sem API) [{time.time() - t_start_global:.2f}s]")
        else:
            print(f"[batch_rename] Backend: {args.backend} (local, no API) [{time.time() - t_start_global:.2f}s]")

    if not os.path.isfile(args.manifest_path):
        if args.output_language == "pt":
            print(f"ERRO: manifest nao encontrado: {args.manifest_path}")
        else:
            print(f"ERROR: manifest not found: {args.manifest_path}")
        sys.exit(1)

    # --- Model list (shared mutable list, protegida por lock nos workers) ---
    if not use_local_backend:
        models = args.models.split(",") if args.models else None
        initial_models = models if models else MODELOS_FALLBACK

        api_available, working_models = check_api_availability(client, initial_models, output_language=args.output_language)
        if not api_available:
            if args.output_language == "pt":
                print(f"\n[AVISO] Verificacao inicial falhou. Ativando fallback universal (sem IA) para todos os passos. [{time.time() - t_start_global:.2f}s]")
            else:
                print(f"\n[WARNING] Initial check failed. Activating universal fallback (no AI) for all steps. [{time.time() - t_start_global:.2f}s]")
        shared_models = working_models  # plain list
    else:
        api_available = True
        shared_models = [args.backend]  # plain list

    # --- Color palette generation ---
    if args.color_prompt and args.config_path:
        if api_available and not use_local_backend:
            handle_color_generation(client, shared_models, args.color_prompt, args.config_path, output_language=args.output_language)
        else:
            if args.output_language == "pt":
                print(f"\n[batch_rename] Pulando geracao de cores customizada (API indisponivel ou backend local). Usando paleta existente. [{time.time() - t_start_global:.2f}s]")
            else:
                print(f"\n[batch_rename] Skipping custom color generation (API unavailable or local backend). Using existing palette. [{time.time() - t_start_global:.2f}s]")

    entries = read_manifest(args.manifest_path)
    total = len(entries)

    if total == 0:
        print("No tracks with audio in manifest. Nothing to do.")
        from pathlib import Path
        Path(args.result_path).touch()
        return

    print(f"\n[ analysis : {args.backend} {'local' if use_local_backend else 'cloud'} inference | {total} track(s) ] [{time.time() - t_start_global:.2f}s]")

    # --- Pre-load PANNs model ---
    if args.backend in ["panns", "hybrid_heuristic", "hybrid_chaining"]:
        if args.output_language == "pt":
            print(f"  [!] Pre-carregando modelo PANNs (isso pode demorar um pouco na primeira vez)... [{time.time() - t_start_global:.2f}s]", flush=True)
        else:
            print(f"  [!] Pre-loading PANNs model (this may take a while on first run)... [{time.time() - t_start_global:.2f}s]", flush=True)
        try:
            from panns_classify import _ensure_ready
            _ensure_ready()
            print(f"  [+] Modelo PANNs carregado com sucesso [{time.time() - t_start_global:.2f}s]", flush=True)
        except Exception as e:
            print(f"  [-] Falha no pre-carregamento do PANNs: {e}", flush=True)

    cancel_flag = args.done_flag.replace("done_", "cancel_") if args.done_flag else None
    results = {}
    t0 = time.time()

    # -----------------------------------------------------------------------
    # PANNs PURO: batch inference — 1 forward pass para todas as tracks
    # -----------------------------------------------------------------------
    if args.backend == "panns":
        # 1. Verificar silêncio e extrair segmentos (I/O em paralelo com threads)
        valid_entries = []
        for idx, audio_path, start_sec, dur_sec in entries:
            if cancel_flag and os.path.exists(cancel_flag):
                print("Cancellation detected. Stopping.")
                break
            if check_absolute_silence(audio_path, start_sec, dur_sec):
                results[idx] = {"error": "absolute_silence"}
                print(f"✖ trk {idx:02d} │ silence [{time.time() - t_start_global:.1f}s]")
                continue
            valid_entries.append((idx, audio_path, start_sec, dur_sec))

        if valid_entries:
            # Extrair segmentos em paralelo (I/O-bound)
            print(f"  [!] Extraindo trechos de áudio das tracks [{time.time() - t_start_global:.2f}s]...", flush=True)
            t_extract = time.time()
            seg_paths = {}

            def _extract_seg(entry):
                idx, audio_path, start_sec, dur_sec = entry
                search_start = start_sec if start_sec is not None and start_sec >= 0 else None
                search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None
                try:
                    if args.quality == "alta" and search_start is None and search_dur is None:
                        return idx, audio_path, None  # usa arquivo direto, sem temp
                    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
                    os.close(tmp_fd)
                    extract_best_segment(
                        audio_path, tmp_path,
                        segment_seconds=search_dur if args.quality == "alta" else args.segment_seconds,
                        search_start_seconds=search_start,
                        search_duration_seconds=search_dur,
                    )
                    return idx, tmp_path, tmp_path  # (idx, path_para_panns, path_temp_para_remover)
                except Exception as e:
                    return idx, None, None

            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                seg_futures = {pool.submit(_extract_seg, e): e[0] for e in valid_entries}
                for future in as_completed(seg_futures):
                    seg_idx, seg_path, temp_path = future.result()
                    if seg_path is None:
                        results[seg_idx] = {"error": "falha ao extrair segmento"}
                    else:
                        seg_paths[seg_idx] = (seg_path, temp_path)

            print(f"  [+] Extração de áudio concluída [{time.time() - t_start_global:.2f}s] (durou {time.time() - t_extract:.2f}s)", flush=True)

            # Batch inference: 1 forward pass
            batch_idxs = [idx for idx in [e[0] for e in valid_entries] if idx in seg_paths]
            batch_paths = [seg_paths[idx][0] for idx in batch_idxs]

            if batch_paths:
                print(f"  [!] Rodando modelo PANNs nas faixas extraídas [{time.time() - t_start_global:.2f}s]...", flush=True)
                t_inf = time.time()
                batch_results = classify_many_with_panns(batch_paths, output_language=args.output_language)
                print(f"  [+] Classificação/inferência concluída [{time.time() - t_start_global:.2f}s] (durou {time.time() - t_inf:.2f}s)", flush=True)
                for i, idx in enumerate(batch_idxs):
                    r = batch_results[i]
                    if "error" not in r:
                        r["_model_usado"] = "panns_batch"
                    results[idx] = r

            # Limpar temp files
            for idx, (seg_path, temp_path) in seg_paths.items():
                if temp_path and os.path.isfile(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

            # Log resultados
            for idx in [e[0] for e in valid_entries]:
                r = results.get(idx, {"error": "sem resultado"})
                elapsed_global = time.time() - t_start_global
                if "error" in r:
                    print(f"✖ trk {idx:02d} │ error     → {r['error']} [{elapsed_global:.1f}s]")
                else:
                    category = r.get("category", "other")
                    if category == "outro":
                        category = "other"
                    instrument = r.get("instrument", "")
                    try:
                        conf = float(r.get("confidence", 0)) * 100
                    except (ValueError, TypeError):
                        conf = 0.0
                    print(f"✔ trk {idx:02d} │ {category:<9} → {instrument:<21} │ conf: {conf:04.1f}% [{elapsed_global:.1f}s]")

    # -----------------------------------------------------------------------
    # Gemini / Hybrid: ThreadPoolExecutor por track (I/O-bound)
    # -----------------------------------------------------------------------
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {}
            for idx, path, start, dur in entries:
                f = pool.submit(process_one, client, idx, path, start, dur, shared_models,
                                args.segment_seconds, args.quality, api_available,
                                args.output_language, args.backend, cancel_flag)
                futures[f] = idx
            for future in as_completed(futures):
                if cancel_flag and os.path.exists(cancel_flag):
                    print("Cancellation detected. Stopping.")
                    break
                idx = futures[future]
                elapsed_global = time.time() - t_start_global
                _, result = future.result()
                results[idx] = result
                done += 1
                if "error" in result:
                    print(f"✖ trk {idx:02d} │ error     → {result['error']} [{elapsed_global:.1f}s]")
                else:
                    category = result.get("category", "other")
                    if category == "outro":
                        category = "other"
                    instrument = result.get("instrument", "")
                    try:
                        conf = float(result.get("confidence", 0)) * 100
                    except (ValueError, TypeError):
                        conf = 0.0
                    print(f"✔ trk {idx:02d} │ {category:<9} → {instrument:<21} │ conf: {conf:04.1f}% [{elapsed_global:.1f}s]")

    # --- Write result TSV ---
    with open(args.result_path, "w", encoding="utf-8") as f:
        for idx, path, start, dur in entries:
            r = results.get(idx, {
                "error": "sem resultado (thread nao completou)" if args.output_language == "pt" else "no result (thread did not complete)"
            })
            if "error" in r:
                if "absolute_silence" in str(r["error"]):
                    f.write(f"{idx}\tsilence\t\t\t\tabsolute_silence\n")
                else:
                    f.write(f"{idx}\terro\t\t\t\t{_sanitize(r['error'])}\n")
            else:
                f.write(
                    f"{idx}\tok\t{_sanitize(r.get('category'))}\t"
                    f"{_sanitize(r.get('instrument'))}\t{r.get('confidence', '')}\t\n"
                )

    elapsed = time.time() - t_start_global
    print(f"› analysis completed in {elapsed:.1f}s")

    # Cria o arquivo sentinela APOS gravar o result.tsv por completo.
    if args.done_flag:
        with open(args.done_flag, "w") as f:
            f.write("done")


if __name__ == "__main__":
    main()
