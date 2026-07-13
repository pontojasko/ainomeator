"""
batch_rename.py

FASE 4: script chamado pelo ReaScript (Lua) de dentro do Reaper.

Le um "manifest" (lista de tracks + caminho do audio de cada uma) de um
arquivo de texto BEM leve (tab-separated, sem JSON/XML), classifica cada
track EM PARALELO usando threads (as chamadas de API sao I/O-bound - o
gargalo e rede, nao CPU - entao threads bastam, nao precisa multiprocessing
nem lidar com GIL) e escreve o resultado em outro arquivo de texto
igualmente leve, pro Lua ler de volta sem precisar de nenhuma lib de JSON
dentro do ReaScript.

Formato do manifest (uma linha por track, SEM cabecalho):
    idx<TAB>caminho_do_audio<TAB>inicio_segundos<TAB>duracao_segundos

    - idx: indice da track no Reaper (0-based), usado so pra casar o
      resultado de volta com a track certa.
    - inicio_segundos/duracao_segundos: opcionais. Se informados, restringem
      a analise a essa janela do arquivo-fonte (ex: onde o item realmente
      esta, se o arquivo for maior que o item ou compartilhado por varios
      items/tracks).

Formato do resultado (uma linha por track, SEM cabecalho):
    idx<TAB>status<TAB>categoria<TAB>instrumento<TAB>confianca<TAB>erro

    status e "ok" ou "erro". Em caso de erro, categoria/instrumento/
    confianca vem vazios e o campo erro tem a mensagem (tabs/quebras de
    linha sao trocados por espaco, pra nao quebrar o parser de uma linha
    so no lado do Lua).

Uso:
    python batch_rename.py manifest.tsv resultado.tsv \
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
# e em UTF-8, independente do locale do Windows (que usa CP1252 por padrao ao
# redirecionar stdout para arquivo com >). Sem isso, chars como acentos ou
# simbolos causam UnicodeEncodeError no redirect do CMD.
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except AttributeError:
    # Python < 3.7 nao tem reconfigure(); -u na linha de comando supre
    pass

from dotenv import load_dotenv
from google import genai
from google.genai import types

from audio_utils import extract_best_segment, downmix_resample, extract_three_peaks, convert_to_mp3_128k
from classify_track import classify_audio_bytes, MODELOS_FALLBACK, build_chaining_prompt
from yamnet_classify import classify_with_yamnet
from essentia_classify import classify_with_essentia
from panns_classify import classify_with_panns

# Backends locais (sem API/chave) disponiveis, por nome -> funcao de classificacao.
# Todos tem a mesma assinatura: classify_fn(audio_path, output_language) -> dict
LOCAL_BACKENDS = {
    "yamnet": classify_with_yamnet,
    "essentia": classify_with_essentia,
    "panns": classify_with_panns,
}


class SharedModelList:
    def __init__(self, initial_models, output_language="pt"):
        self.models = list(initial_models)
        self.output_language = output_language

    def get_models(self):
        return list(self.models)

    def remove_model(self, model):
        if model in self.models and len(self.models) > 1:
            if self.output_language == "pt":
                print(f"\n  [AVISO] Modelo '{model}' indisponível/falhou! Ativando fallback global de modelo para todas as demais faixas do lote...")
            else:
                print(f"\n  [WARNING] Model '{model}' unavailable/failed! Activating global model fallback for all remaining batch tracks...")
            self.models.remove(model)


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


def _sanitize(text):
    if text is None:
        return ""
    return str(text).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def _process_one_local(idx, audio_path, start_sec, dur_sec, segment_seconds, quality, output_language, backend_name):
    """Processa UMA track usando um backend local (YamNet/Essentia/PANNs), sem API do Gemini."""
    classify_fn = LOCAL_BACKENDS[backend_name]
    tmp_seg_path = None
    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        if quality == "alta":
            # Análise detalhada para local: analisa o arquivo/item inteiro em vez de apenas um trecho de 8s
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
            # Análise rápida: extrai o trecho de 8s de maior energia
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


def analyze_dsp_properties(audio_path):
    """
    Carrega o áudio e analisa suas propriedades via DSP básico:
    1. Concentração de energia abaixo de 100Hz usando FFT.
    2. Proporção de decaimentos abruptos e contínuos sem sustain (sense of staccato/percussion).
    """
    import numpy as np
    import soundfile as sf
    try:
        data, sr = sf.read(audio_path, always_2d=True)
        # Converter para mono se necessário
        if data.shape[1] > 1:
            y = data.mean(axis=1)
        else:
            y = data.flatten()
            
        if len(y) == 0:
            return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}

        # 1. Concentração de energia abaixo de 100Hz
        # Usamos rfft para sinais reais
        fft_vals = np.fft.rfft(y)
        fft_freqs = np.fft.rfftfreq(len(y), d=1/sr)
        magnitudes = np.abs(fft_vals)
        energy = magnitudes ** 2
        total_energy = np.sum(energy)
        
        low_freq_ratio = 0.0
        if total_energy > 0:
            low_freq_ratio = np.sum(energy[fft_freqs < 100]) / total_energy

        # 2. Decaimentos abruptos (transiente rápido sem sustain)
        # Dividimos em blocos de 50ms
        frame_size = int(0.050 * sr)
        low_energy_ratio = 0.0
        if frame_size > 0 and len(y) >= frame_size:
            num_frames = len(y) // frame_size
            frames = y[:num_frames * frame_size].reshape((num_frames, frame_size))
            frame_max = np.max(np.abs(frames), axis=1)
            global_max = np.max(frame_max)
            if global_max > 0:
                # Fração de frames onde a amplitude máxima é menor que 10% do pico absoluto
                low_energy_ratio = np.sum(frame_max < (0.1 * global_max)) / num_frames
                
        return {
            "low_freq_ratio": float(low_freq_ratio),
            "low_energy_ratio": float(low_energy_ratio)
        }
    except Exception as e:
        print(f"[DSP ERROR] Falha ao analisar propriedades DSP: {e}")
        return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}


def _process_one_hybrid(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language, backend_name):
    """Processa UMA track usando um algoritmo de analise hibrida que combina PANNs local e Gemini em nuvem."""
    import numpy as np
    tmp_seg_path = None
    tmp_light_path = None
    tmp_mp3_path = None
    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        # 1. Extração do segmento de áudio de maior energia
        tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
        os.close(tmp_seg_fd)
        
        extract_best_segment(
            audio_path, tmp_seg_path, segment_seconds=segment_seconds,
            search_start_seconds=search_start,
            search_duration_seconds=search_dur,
        )

        # 2. Analisar propriedades de DSP simples (Verificador de Sanidade)
        dsp_info = analyze_dsp_properties(tmp_seg_path)
        low_freq_ratio = dsp_info["low_freq_ratio"]
        low_energy_ratio = dsp_info["low_energy_ratio"]

        # 3. Preparar áudio bytes para o Gemini
        if not api_available or not client:
            # Sem API do Gemini disponível, roda apenas o local PANNs
            panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
            if panns_result and "error" not in panns_result:
                panns_result["_model_usado"] = "panns_only_no_api"
                return idx, panns_result
            return idx, {"error": "API do Gemini nao disponivel e PANNs falhou"}

        # Preparar áudio para a API do Gemini
        if quality == "alta":
            with open(tmp_seg_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            tmp_mp3_fd, tmp_mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="ai_namer_mp3_")
            os.close(tmp_mp3_fd)
            mp3_success = convert_to_mp3_128k(tmp_seg_path, tmp_mp3_path)
            
            if mp3_success:
                with open(tmp_mp3_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/mp3"
            else:
                tmp_light_fd, tmp_light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
                os.close(tmp_light_fd)
                downmix_resample(tmp_seg_path, tmp_light_path)
                with open(tmp_light_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/wav"

        current_models = shared_models.get_models()
        def on_model_failed(model_name):
            shared_models.remove_model(model_name)

        # 4. Camada de Execução Paralela
        # Rodar CNN14 (PANNs) e Gemini Flash ao mesmo tempo
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
            
            # Aguarda ambos concluírem
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
            # Ambas falharam
            err_msg = f"CNN14 err: {panns_result.get('error') if panns_result else 'None'}; Gemini err: {gemini_result.get('error') if gemini_result else 'None'}"
            return idx, {"error": f"Ambas as IAs falharam: {err_msg}"}

        # Se apenas uma IA funcionou, fallback direto para ela
        if not panns_ok:
            gemini_result["_model_usado"] = f"gemini_{gemini_result.get('_model_usado', 'hybrid')}_panns_failed"
            final_res = gemini_result
            rule_applied = "panns_failed"
        elif not gemini_ok:
            panns_result["_model_usado"] = "panns_gemini_failed"
            final_res = panns_result
            rule_applied = "gemini_failed"
        else:
            # 5. O Árbitro (Matriz de Decisão de Conflitos)
            p_cat = panns_result.get("category", "").lower()
            p_inst = panns_result.get("instrument", "").lower()
            g_cat = gemini_result.get("category", "").lower()
            g_inst = gemini_result.get("instrument", "").lower()

            # Sanitização robusta dos valores de confiança para evitar NoneType e TypeError
            try:
                g_conf = gemini_result.get("confidence")
                g_conf = float(g_conf) if g_conf is not None and g_conf != "" else 0.5
            except (ValueError, TypeError):
                g_conf = 0.5

            try:
                p_conf = panns_result.get("confidence")
                p_conf = float(p_conf) if p_conf is not None and p_conf != "" else 0.5
            except (ValueError, TypeError):
                p_conf = 0.5

            final_category = gemini_result.get("category")
            final_instrument = gemini_result.get("instrument")
            final_confidence = g_conf
            notes_parts = [f"CNN14={panns_result.get('instrument')}({p_conf})", f"Gemini={gemini_result.get('instrument')}({g_conf})"]
            rule_applied = "fallback"

            # Fricativas rítmicas: se PANNs achar "vocal" e Gemini achar "shaker" ou percussão
            shaker_keywords = ["shaker", "chocalho", "cabasa", "maraca", "percuss", "tambourine", "pandeiro", "claves", "castanholas", "caxixi"]
            is_gemini_shaker = g_cat == "bateria" or any(kw in g_inst for kw in shaker_keywords)
            
            # Regra 1: Prioridade Rítmica (vocal vs shaker)
            if p_cat == "vocal" and is_gemini_shaker:
                final_category = "bateria"
                final_instrument = gemini_result.get("instrument") if any(kw in g_inst for kw in ["shaker", "chocalho", "cabasa", "maraca", "pandeiro"]) else ("Shaker" if output_language == "pt" else "Shaker")
                final_confidence = max(g_conf, p_conf)
                rule_applied = "prioridade_ritmica"

            # Regra 2: Transiente Grave (Gemini piano vs PANNs baixo/cordas)
            elif "piano" in g_inst and (p_cat in ["baixo", "cordas"] or any(kw in p_inst for kw in ["baixo", "bass", "cello", "contrabaixo", "double bass"])):
                final_category = "baixo"
                final_instrument = "Baixo Pizzicato" if output_language == "pt" else "Pizzicato Bass"
                final_confidence = p_conf
                rule_applied = "transiente_grave"

            # Regra 3: Consenso Absoluto (compatibilidade de famílias de instrumentos)
            else:
                compatible = False
                if p_cat == g_cat:
                    compatible = True
                elif p_cat in ["cordas", "baixo"] and g_cat in ["cordas", "baixo"]:
                    compatible = True
                elif p_cat in ["teclado", "synth"] and g_cat in ["teclado", "synth"]:
                    compatible = True
                elif p_cat in ["baixo", "synth"] and g_cat in ["baixo", "synth"]:
                    compatible = True
                
                if compatible:
                    final_category = gemini_result.get("category")
                    final_instrument = gemini_result.get("instrument")
                    final_confidence = max(g_conf, p_conf)
                    rule_applied = "consenso_absoluto"
                else:
                    # Sem consenso e sem regra especial: prioriza o que tem confiança maior ou o Gemini por padrão
                    if p_conf > 0.75 and p_cat in ["bateria", "baixo", "sopro", "cordas"]:
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
                "notes": f"Arbítrio: {rule_applied} | " + " | ".join(notes_parts)
            }

        # Garantia final de que a confiança é um float válido e formatado
        try:
            final_res["confidence"] = round(float(final_res.get("confidence", 0.5)), 3)
        except (TypeError, ValueError):
            final_res["confidence"] = 0.5

        # 6. O Verificador de Sanidade (DSP simples)
        orig_category = final_res.get("category")
        orig_instrument = final_res.get("instrument")
        notes = final_res.get("notes", "")

        # Teste 1: Concentração de graves extrema (< 100Hz)
        if low_freq_ratio > 0.45:
            if orig_category not in ["baixo", "bateria"] or not any(kw in (orig_instrument or "").lower() for kw in ["bass", "baixo", "kick", "bumbo", "sub"]):
                final_res["category"] = "baixo"
                final_res["instrument"] = "Baixo/Bumbo (DSP Grave <100Hz)" if output_language == "pt" else "Bass/Kick (DSP Low-Freq <100Hz)"
                final_res["notes"] = notes + f" | [DSP Override: Grave (F={low_freq_ratio:.2f})]"
                print(f"  [DSP Override] Track {idx}: Grave extremo forçou categoria baixo (low_freq_ratio={low_freq_ratio:.2f})")

        # Teste 2: Decaimentos abruptos e contínuos sem sustain
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
        for p in (tmp_seg_path, tmp_light_path, tmp_mp3_path):
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

def _process_one_chaining(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language):
    """
    Processa UMA track usando a verdadeira arquitetura de Chaining:
    1. Roda PANNs localmente.
    2. Envia o áudio para o Gemini junto com o contexto/previsão do PANNs para review.
    """
    tmp_seg_path = None
    tmp_light_path = None
    tmp_mp3_path = None
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

        # Roda o PANNs localmente
        panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
        
        if not api_available or not client:
            if panns_result and "error" not in panns_result:
                panns_result["_model_usado"] = "panns_only_no_api"
                return idx, panns_result
            return idx, {"error": "API do Gemini nao disponivel e PANNs falhou"}

        # PANNs pode ter falhado
        if panns_result and "error" in panns_result:
            print(f"  [Chaining] PANNs falhou na track {idx}: {panns_result['error']}. Tentando apenas com Gemini.")
            panns_result = {"category": "desconhecida", "instrument": "falha na analise local", "confidence": 0.0}

        # Preparar áudio para a API do Gemini
        if quality == "alta":
            with open(tmp_seg_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            tmp_mp3_fd, tmp_mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="ai_namer_mp3_")
            os.close(tmp_mp3_fd)
            mp3_success = convert_to_mp3_128k(tmp_seg_path, tmp_mp3_path)
            
            if mp3_success:
                with open(tmp_mp3_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/mp3"
            else:
                tmp_light_fd, tmp_light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
                os.close(tmp_light_fd)
                downmix_resample(tmp_seg_path, tmp_light_path)
                with open(tmp_light_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/wav"

        current_models = shared_models.get_models()
        def on_model_failed(model_name):
            shared_models.remove_model(model_name)

        # Monta o prompt dinâmico usando o resultado do PANNs
        chaining_prompt = build_chaining_prompt(panns_result, output_language=output_language)

        # Envia pro Gemini com o prompt especializado
        gemini_result = classify_audio_bytes(
            client, audio_bytes, mime_type=mime_type,
            models=current_models, on_model_failed=on_model_failed,
            output_language=output_language,
            custom_prompt=chaining_prompt
        )

        if gemini_result and "error" not in gemini_result:
            gemini_result["_model_usado"] = "hybrid_chaining_review"
            return idx, gemini_result
        else:
            # Fallback para o PANNs se Gemini falhar
            panns_result["_model_usado"] = "panns_gemini_failed"
            return idx, panns_result

    except Exception as e:
        return idx, {"error": f"{type(e).__name__}: {e}"}
    finally:
        for p in (tmp_seg_path, tmp_light_path, tmp_mp3_path):
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def check_absolute_silence(audio_path, start_sec=None, dur_sec=None):
    """
    Retorna True se o áudio (ou o trecho especificado) for silêncio absoluto
    (pico máximo < -120dB / 1e-6).
    """
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
            
        max_val = np.max(np.abs(data))
        if max_val < 1e-6:  # -120dB
            return True
        return False
    except Exception:
        # Em caso de falha (ex: codec de MP3 não suportado), deixa o fluxo continuar
        return False


def process_one(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language, backend="gemini", cancel_flag=None):
    import os
    if cancel_flag and os.path.exists(cancel_flag):
        return idx, {"error": "cancelled"}
        
    if check_absolute_silence(audio_path, start_sec, dur_sec):
        return idx, {"error": "absolute_silence"}
        
    if backend in LOCAL_BACKENDS:
        return _process_one_local(idx, audio_path, start_sec, dur_sec, segment_seconds, quality, output_language, backend)
    if backend == "hybrid_heuristic":
        return _process_one_hybrid(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language, backend)
    if backend == "hybrid_chaining":
        return _process_one_chaining(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language)
    if not api_available:
        return idx, {"category": "outro", "instrument": "Audio", "confidence": 0.0, "_model_usado": "fallback_universal"}

    """Processa UMA track: corta o trecho correspondente, tenta comprimir para MP3 128kbps,
    caso falhe usa o fallback WAV adequado, e manda para o Gemini."""
    tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
    os.close(tmp_seg_fd)
    tmp_light_fd, tmp_light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
    os.close(tmp_light_fd)
    tmp_mp3_fd, tmp_mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="ai_namer_mp3_")
    os.close(tmp_mp3_fd)

    try:
        if not audio_path or not os.path.isfile(audio_path):
            return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

        search_start = start_sec if start_sec is not None and start_sec >= 0 else None
        search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

        if quality == "alta":
            # Análise detalhada: sem remover silêncios, mantendo a qualidade original (wav)
            extract_best_segment(
                audio_path, tmp_seg_path, segment_seconds=segment_seconds,
                search_start_seconds=search_start,
                search_duration_seconds=search_dur,
            )
            with open(tmp_seg_path, "rb") as f:
                audio_bytes = f.read()
            mime_type = "audio/wav"
        else:
            # Análise rápida: pega 3 trechos de pico de 4s cada (12s total)
            extract_three_peaks(
                audio_path, tmp_seg_path,
                search_start_seconds=search_start,
                search_duration_seconds=search_dur,
                segment_seconds=4
            )

            # Tenta converter para MP3 128kbps
            mp3_success = convert_to_mp3_128k(tmp_seg_path, tmp_mp3_path)
            
            if mp3_success:
                with open(tmp_mp3_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/mp3"
            else:
                # Fallback para WAV caso ffmpeg nao esteja disponivel
                # WAV leve 24kHz mono
                downmix_resample(tmp_seg_path, tmp_light_path)
                    
                with open(tmp_light_path, "rb") as f:
                    audio_bytes = f.read()
                mime_type = "audio/wav"

        current_models = shared_models.get_models()
        def on_model_failed(model_name):
            shared_models.remove_model(model_name)

        result = classify_audio_bytes(
            client, audio_bytes, mime_type=mime_type,
            models=current_models, on_model_failed=on_model_failed,
            output_language=output_language
        )
        return idx, result
    except Exception as e:
        return idx, {"error": f"{type(e).__name__}: {e}"}
    finally:
        for p in (tmp_seg_path, tmp_light_path, tmp_mp3_path):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass


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
        "- cordas (Seçao de cordas orquestrais, violino, cello)\n"
        "- sopros (Metais e sopros, sax, trompete, trombone)\n"
        "- efeitos (Canais auxiliares de efeitos, reverb, delay, FX)\n"
        "- pastas (Canais pai / folders / busses que agrupam tracks)\n"
        "- outro (Cor padrao / outros instrumentos nao identificados)\n\n"
        "Diretrizes:\n"
        "1. As cores devem ser profissionais e faceis de ler no Reaper (evite cores muito brilhantes de fundo ou que dificultem ver os nomes).\n"
        "2. Mantenha uma diferenca visual clara entre instrumentos parecidos (ex: vocal_principal deve se destacar mais que backing_vocals, violao diferente de guitarra, bateria diferente de percussao).\n"
        "3. Mesmo se o pedido for 'tudo em verde', use tons, saturacoes e brilhos diferentes de verde para cada instrumento, para que continuem distinguiveis.\n"
        "4. Responda APENAS com a estrutura do arquivo INI sob a secao [Cores]. Nao inclua blocos de codigo markdown, nao inclua nenhuma explicacao ou texto extra."
    )

    response = client.models.generate_content(
        model=model,
        contents=[system_prompt],
        config=types.GenerateContentConfig(
            temperature=0.3,
        ),
    )
    text = response.text.strip()
    
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            if "[Cores]" in part or "vocal_principal" in part:
                lines = part.strip().split("\n")
                if lines and (lines[0].strip() == "ini" or lines[0].strip() == "txt" or lines[0].strip() == "toml"):
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
                    raise ValueError("Resposta do modelo nao contem secao [Cores] ou chaves esperadas." if output_language == "pt" else "Model response does not contain [Cores] section or expected keys.")
                
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
        print(f"  [ERRO] Nao foi possivel gerar paleta de cores personalizada. Usando paleta existente.")
    else:
        print(f"  [ERROR] Could not generate custom color palette. Using existing palette.")


def main():
    parser = argparse.ArgumentParser(description="Classifica varias tracks em paralelo (chamado pelo ReaScript)")
    parser.add_argument("manifest_path")
    parser.add_argument("result_path")
    parser.add_argument("--workers", type=int, default=5,
                         help="threads em paralelo (padrao: 5). Cada uma faz uma chamada de API por vez.")
    parser.add_argument("--segment-seconds", type=float, default=8)
    parser.add_argument("--models", default=None,
                         help="lista de modelos separados por virgula, em ordem de preferencia")
    parser.add_argument("--done-flag", default=None,
                         help="caminho de um arquivo sentinela criado APOS o result.tsv ser gravado por completo. "
                              "O ReaScript faz polling nesse arquivo pra saber quando pode ler o resultado, "
                              "evitando race condition (leitura parcial do TSV).")
    parser.add_argument("--color-prompt", default=None,
                         help="Prompt para gerar paleta de cores personalizada")
    parser.add_argument("--config-path", default=None,
                         help="Caminho do arquivo de cores .ini")
    parser.add_argument("--quality", default="normal",
                         help="Qualidade de analise: 'normal' ou 'alta'")
    parser.add_argument("--output-language", choices=["pt", "en"], default="pt",
                         help="idioma do campo instrument: pt ou en (padrao: pt)")
    parser.add_argument("--backend", choices=["gemini", "yamnet", "essentia", "panns", "hybrid_heuristic", "hybrid_chaining"], default="gemini",
                         help="backend de classificacao: gemini (API, padrao), yamnet/essentia/panns (locais), ou hibridos (heuristic, chaining)")
    parser.add_argument("--panns-threads", type=int, default=None,
                         help="threads internas do PyTorch (PANNs) por worker")
    args = parser.parse_args()

    load_dotenv()
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT_ENV = os.path.join(os.path.dirname(_SCRIPT_DIR), ".env")
    if os.path.exists(_PARENT_ENV):
        load_dotenv(_PARENT_ENV)
    if args.panns_threads is not None and args.panns_threads > 0:
        os.environ["PANNS_THREADS"] = str(args.panns_threads)
    api_key = os.environ.get("GEMINI_API_KEY")

    use_local_backend = (args.backend in LOCAL_BACKENDS)

    if not use_local_backend:
        if not api_key:
            if args.output_language == "pt":
                print("ERRO: GEMINI_API_KEY nao encontrada (crie/edite o .env nesta pasta).")
                print("Dica: use --backend yamnet, --backend essentia ou --backend panns para classificacao local sem API key.")
            else:
                print("ERROR: GEMINI_API_KEY not found (create/edit .env in this folder).")
                print("Tip: use --backend yamnet, --backend essentia or --backend panns for local classification without API key.")
            sys.exit(1)
        client = genai.Client(api_key=api_key)
    else:
        client = None
        if args.output_language == "pt":
            print(f"[batch_rename] Backend: {args.backend} (local, sem API)")
        else:
            print(f"[batch_rename] Backend: {args.backend} (local, no API)")

    if not os.path.isfile(args.manifest_path):
        if args.output_language == "pt":
            print(f"ERRO: manifest nao encontrado: {args.manifest_path}")
        else:
            print(f"ERROR: manifest not found: {args.manifest_path}")
        sys.exit(1)

    if not use_local_backend:
        client = genai.Client(api_key=api_key)
        models = args.models.split(",") if args.models else None
        initial_models = models if models else MODELOS_FALLBACK

        api_available, working_models = check_api_availability(client, initial_models, output_language=args.output_language)
        if not api_available:
            if args.output_language == "pt":
                print("\n[AVISO] Verificacao inicial falhou. Ativando fallback universal (sem IA) para todos os passos.")
            else:
                print("\n[WARNING] Initial check failed. Activating universal fallback (no AI) for all steps.")
        initial_models = working_models
    else:
        api_available = True  # backends locais sao sempre "available" (rodam na maquina)
        initial_models = [args.backend]

        # Se o prompt de cores foi informado, gera a paleta antes do processamento das faixas
    if args.color_prompt and args.config_path:
        if api_available and not use_local_backend:
            handle_color_generation(client, initial_models, args.color_prompt, args.config_path, output_language=args.output_language)
        else:
            if args.output_language == "pt":
                print(f"\n[batch_rename] Pulando geracao de cores customizada (API indisponivel ou backend local). Usando paleta existente.")
            else:
                print(f"\n[batch_rename] Skipping custom color generation (API unavailable or local backend). Using existing palette.")

    entries = read_manifest(args.manifest_path)
    total = len(entries)

    if total == 0:
        print("No tracks with audio in manifest. Nothing to do.")
        from pathlib import Path
        Path(args.result_path).touch()
        return

    shared_models = SharedModelList(initial_models, output_language=args.output_language)

    print(f"\n[ analysis : {args.backend} {'local' if use_local_backend else 'cloud'} inference ]")

    if args.backend in ["panns", "hybrid_heuristic", "hybrid_chaining"]:
        if args.output_language == "pt":
            print("  [!] Pre-carregando modelo PANNs (isso pode demorar um pouco na primeira vez)...", flush=True)
        else:
            print("  [!] Pre-loading PANNs model (this may take a while on first run)...", flush=True)
        try:
            from panns_classify import _ensure_ready
            _ensure_ready()
        except Exception:
            pass

    results = {}
    t0 = time.time()
    done = 0

    cancel_flag = args.done_flag.replace("done_", "cancel_") if args.done_flag else None

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(process_one, client, idx, path, start, dur, shared_models, args.segment_seconds, args.quality, api_available, args.output_language, args.backend, cancel_flag): idx
            for idx, path, start, dur in entries
        }
        for future in as_completed(futures):
            if cancel_flag and os.path.exists(cancel_flag):
                print("Cancellation detected. Stopping.")
                break
            idx, result = future.result()
            results[idx] = result
            done += 1
            if "error" in result:
                print(f"✖ trk {idx:02d} │ error     → {result['error']}")
            else:
                category = result.get('category', 'other')
                if category == 'outro':
                    category = 'other'
                instrument = result.get('instrument', '')
                try:
                    conf = float(result.get('confidence', 0)) * 100
                except (ValueError, TypeError):
                    conf = 0.0
                print(f"✔ trk {idx:02d} │ {category:<9} → {instrument:<21} │ conf: {conf:04.1f}%")

    with open(args.result_path, "w", encoding="utf-8") as f:
        for idx, path, start, dur in entries:
            r = results.get(idx, {"error": "sem resultado (thread nao completou)" if args.output_language == "pt" else "no result (thread did not complete)"})
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

    elapsed = time.time() - t0
    print(f"› analysis completed in {elapsed:.1f}s")

    # Cria o arquivo sentinela APOS gravar o result.tsv por completo.
    # O Lua faz polling nesse arquivo pra saber que pode ler o resultado sem
    # risco de pegar uma escrita parcial.
    if args.done_flag:
        with open(args.done_flag, "w") as f:
            f.write("done")


if __name__ == "__main__":
    main()
