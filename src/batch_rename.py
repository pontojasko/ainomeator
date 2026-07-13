"""
batch_rename.py

Script principal de batch processing do AiNOMEATOR.
Le o manifest.tsv gerado pelo Reaper, orquestra a classificacao de todas
as faixas (local, nuvem ou hibrida), e escreve o result.tsv para o Reaper ler.

Executa chamadas a API do Gemini ou modelos locais em paralelo via
ThreadPoolExecutor, otimizando o tempo total.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import soundfile as sf
from google import genai

from _bootstrap import CATEGORIAS_VALIDAS, ERROS_TRANSITORIOS, abort_if_missing, configure_stdout, load_env, safe_float
from audio_utils import (
    analyze_dsp_properties,
    convert_to_mp3_128k,
    downmix_resample,
    extract_best_segment,
    extract_three_peaks,
)
from classify_track import build_chaining_prompt, classify_audio_bytes

# Encerra imediatamente se faltarem pacotes obrigatorios
abort_if_missing()

LOCAL_BACKENDS = ["yamnet", "essentia", "panns"]
MODELOS_FALLBACK = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]


def _sanitize(text: Any) -> str:
    """Remove tabs e quebras de linha que quebrariam o formato TSV."""
    if not text:
        return ""
    return str(text).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


class SharedModelList:
    """Lista thread-safe de modelos para a cascata de fallback do Gemini.
    Se uma thread detecta que o flash-lite ta fora do ar, remove da lista pra que
    as outras threads ja pulem direto pro proximo modelo sem gastar retries atoa.
    """
    def __init__(self, initial_models: List[str], output_language: str = "pt"):
        self.models = list(initial_models)
        self.output_language = output_language
        self._lock = threading.Lock()

    def get_models(self) -> List[str]:
        with self._lock:
            return list(self.models)

    def remove_model(self, model_name: str) -> None:
        with self._lock:
            if model_name in self.models:
                self.models.remove(model_name)
                msg = f"  [{model_name} fora do ar globalmente, removido da lista]"
                if self.output_language == "en":
                    msg = f"  [{model_name} offline globally, removed from list]"
                print(msg)


def check_api_availability(client: Any, models: List[str], output_language: str = "pt") -> Tuple[bool, List[str]]:
    """Testa a API do Gemini com um prompt minusculo pra validar a chave e os modelos."""
    if output_language == "pt":
        print("[!] Verificando conexao com a API do Gemini...", end=" ", flush=True)
    else:
        print("[!] Checking connection to Gemini API...", end=" ", flush=True)

    working_models = []
    for model in models:
        try:
            client.models.generate_content(
                model=model,
                contents="responda 'ok' e nada mais",
                config={"temperature": 0.0}
            )
            working_models.append(model)
            if output_language == "pt":
                print(f"[{model} OK]", end=" ", flush=True)
            else:
                print(f"[{model} OK]", end=" ", flush=True)
        except Exception as e:
            erro_str = str(e)
            if any(marcador in erro_str for marcador in ERROS_TRANSITORIOS):
                if output_language == "pt":
                    print(f"[{model} OCUPADO]", end=" ", flush=True)
                else:
                    print(f"[{model} BUSY]", end=" ", flush=True)
                working_models.append(model)
            else:
                if output_language == "pt":
                    print(f"[{model} ERRO PERMANENTE]", end=" ", flush=True)
                else:
                    print(f"[{model} FATAL ERROR]", end=" ", flush=True)

    print()

    if not working_models:
        if output_language == "pt":
            print("\n[ERRO CRITICO] Nenhum modelo do Gemini respondeu.")
        else:
            print("\n[CRITICAL ERROR] No Gemini models responded.")
        return False, []

    return True, working_models


def read_manifest(manifest_path: str) -> List[Tuple[int, str, Optional[float], Optional[float]]]:
    """Le o manifest gerado pelo Lua (TSV: idx, path, start_sec, dur_sec)."""
    entries = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                idx = int(parts[0])
                audio_path = parts[1]
                start_sec = None
                dur_sec = None
                if len(parts) >= 4 and parts[2] != "" and parts[3] != "":
                    try:
                        start_sec = float(parts[2])
                        dur_sec = float(parts[3])
                    except ValueError:
                        pass
                entries.append((idx, audio_path, start_sec, dur_sec))
    return entries


def handle_color_generation(client: Any, models: List[str], color_prompt: str, config_path: str, output_language: str = "pt") -> None:
    """Gera uma paleta de cores customizada via Gemini se solicitado."""
    if output_language == "pt":
        print(f"\n[batch_rename] Gerando cores customizadas ('{color_prompt[:30]}...')...")
    else:
        print(f"\n[batch_rename] Generating custom colors ('{color_prompt[:30]}...')...")

    prompt = (
        f"Gere um arquivo INI contendo uma paleta de cores para os seguintes 14 instrumentos musicais/categorias, "
        f"alem de 'pastas' e 'outro'. A paleta deve ser esteticamente coesa, moderna e harmoniosa, inspirada no seguinte prompt do usuario: '{color_prompt}'. "
        f"Use cores hexadecimais (ex: #FF0000). Use EXATAMENTE os nomes de chaves abaixo, sem adicionar ou remover nenhum.\n\n"
        f"O formato da saida DEVE SER EXATAMENTE como este, e NADA mais (nao adicione blocos de markdown, textos explicativos, nem notas. Apenas o conteudo INI):\n\n"
        f"[Cores]\n"
        f"vocal_principal = #...\n"
        f"backing_vocals = #...\n"
        f"bateria = #...\n"
        f"percussao = #...\n"
        f"baixo = #...\n"
        f"guitarra_eletrica = #...\n"
        f"violao = #...\n"
        f"teclado = #...\n"
        f"synth = #...\n"
        f"cordas = #...\n"
        f"sopros = #...\n"
        f"efeitos = #...\n"
        f"pastas = #...\n"
        f"outro = #...\n"
    )

    for idx_modelo, model in enumerate(models):
        for tentativa in range(1, 3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"temperature": 0.7}
                )
                ini_content = response.text.strip()
                if ini_content.startswith("```ini"):
                    ini_content = ini_content[6:]
                elif ini_content.startswith("```"):
                    ini_content = ini_content[3:]
                if ini_content.endswith("```"):
                    ini_content = ini_content[:-3]
                ini_content = ini_content.strip()

                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(f"# Paleta gerada por IA: {color_prompt}\n")
                    f.write(ini_content + "\n")

                if output_language == "pt":
                    print(f"  [OK] Paleta gerada e salva em {config_path}")
                else:
                    print(f"  [OK] Palette generated and saved to {config_path}")
                return

            except Exception as e:
                erro_str = str(e)
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


def check_absolute_silence(audio_path: str, start_sec: Optional[float], dur_sec: Optional[float]) -> bool:
    """Retorna True se o arquivo tiver 0 frames ou se a leitura revelar pico de energia proximo a zero."""
    if not os.path.exists(audio_path):
        return True
    try:
        info = sf.info(audio_path)
        if info.frames == 0:
            return True
        import numpy as np
        data, _ = sf.read(audio_path, start=0, frames=10000, always_2d=True)
        if data.shape[0] > 0:
            max_val = np.max(np.abs(data))
            if max_val < 1e-6:
                return True
    except Exception:
        pass
    return False


class _TempAudioFiles:
    """Context manager para garantir a exclusao de arquivos temporarios."""
    def __init__(self):
        self.files: List[str] = []

    def add(self, path: Optional[str]) -> None:
        if path:
            self.files.append(path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for path in self.files:
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _prepare_audio_segment(
    audio_path: str,
    start_sec: Optional[float],
    dur_sec: Optional[float],
    segment_seconds: float,
    quality: str,
    tmp_files: _TempAudioFiles
) -> Tuple[str, bytes, str]:
    """Corta o audio e prepara os bytes (wav ou mp3) para a API."""
    search_start = start_sec if start_sec is not None and start_sec >= 0 else None
    search_dur = dur_sec if dur_sec is not None and dur_sec > 0 else None

    tmp_seg_fd, tmp_seg_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_seg_")
    os.close(tmp_seg_fd)
    tmp_files.add(tmp_seg_path)

    if quality == "alta":
        extract_best_segment(
            audio_path, tmp_seg_path, segment_seconds=segment_seconds,
            search_start_seconds=search_start, search_duration_seconds=search_dur,
        )
        with open(tmp_seg_path, "rb") as f:
            audio_bytes = f.read()
        return tmp_seg_path, audio_bytes, "audio/wav"
    else:
        extract_three_peaks(
            audio_path, tmp_seg_path, search_start_seconds=search_start,
            search_duration_seconds=search_dur, segment_seconds=4
        )
        tmp_mp3_fd, tmp_mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="ai_namer_mp3_")
        os.close(tmp_mp3_fd)
        tmp_files.add(tmp_mp3_path)

        if convert_to_mp3_128k(tmp_seg_path, tmp_mp3_path):
            with open(tmp_mp3_path, "rb") as f:
                audio_bytes = f.read()
            return tmp_seg_path, audio_bytes, "audio/mp3"
        else:
            tmp_light_fd, tmp_light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
            os.close(tmp_light_fd)
            tmp_files.add(tmp_light_path)
            downmix_resample(tmp_seg_path, tmp_light_path)
            with open(tmp_light_path, "rb") as f:
                audio_bytes = f.read()
            return tmp_seg_path, audio_bytes, "audio/wav"


def _process_one_local(
    idx: int, audio_path: str, start_sec: Optional[float], dur_sec: Optional[float],
    segment_seconds: float, quality: str, output_language: str, backend_name: str
) -> Tuple[int, Dict[str, Any]]:
    """Processa UMA track com backends puramente locais (yamnet, essentia, panns).
    Nota: no fluxo batch, panns usa classify_many_with_panns (otimizado) fora desta funcao.
    Esta funcao so cobre os outros locais ou chamadas individuais.
    """
    with _TempAudioFiles() as tmp:
        try:
            if not audio_path or not os.path.isfile(audio_path):
                return idx, {"error": f"arquivo nao encontrado: {audio_path}"}
            
            # Corta o segmento exato
            tmp_seg_path, _, _ = _prepare_audio_segment(audio_path, start_sec, dur_sec, segment_seconds, "alta", tmp)
            
            if backend_name == "yamnet":
                from yamnet_classify import classify_with_yamnet
                return idx, classify_with_yamnet(tmp_seg_path, output_language=output_language)
            elif backend_name == "essentia":
                from essentia_classify import classify_with_essentia
                return idx, classify_with_essentia(tmp_seg_path, output_language=output_language)
            elif backend_name == "panns":
                from panns_classify import classify_with_panns
                return idx, classify_with_panns(tmp_seg_path, output_language=output_language)
            else:
                return idx, {"error": f"backend local desconhecido: {backend_name}"}

        except Exception as e:
            return idx, {"error": f"{type(e).__name__}: {e}"}


def _arbitrate(panns_result: Dict[str, Any], gemini_result: Dict[str, Any], output_language: str) -> Tuple[str, str, float, str]:
    """Arbitro (Matriz de Decisao) para resolver conflitos entre PANNs e Gemini."""
    p_cat = panns_result.get("category", "").lower()
    p_inst = panns_result.get("instrument", "").lower()
    g_cat = gemini_result.get("category", "").lower()
    g_inst = gemini_result.get("instrument", "").lower()

    g_conf = safe_float(gemini_result.get("confidence"), 0.5)
    p_conf = safe_float(panns_result.get("confidence"), 0.5)

    final_category = gemini_result.get("category", "outro")
    final_instrument = gemini_result.get("instrument", "")
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
            final_category = gemini_result.get("category", "outro")
            final_instrument = gemini_result.get("instrument", "")
            final_confidence = max(g_conf, p_conf)
            rule_applied = "consenso_absoluto"
        else:
            if p_conf > 0.75 and p_cat in ["bateria", "baixo", "sopro", "cordas"]:
                final_category = panns_result.get("category", "outro")
                final_instrument = panns_result.get("instrument", "")
                final_confidence = p_conf
                rule_applied = "prioridade_cnn14_confiante"
            else:
                final_category = gemini_result.get("category", "outro")
                final_instrument = gemini_result.get("instrument", "")
                final_confidence = g_conf
                rule_applied = "prioridade_gemini_default"

    return final_category, final_instrument, round(final_confidence, 3), rule_applied


def _process_one_hybrid(
    client: Any, idx: int, audio_path: str, start_sec: Optional[float], dur_sec: Optional[float],
    shared_models: SharedModelList, segment_seconds: float, quality: str, api_available: bool,
    output_language: str, backend_name: str
) -> Tuple[int, Dict[str, Any]]:
    """Processa UMA track combinando PANNs local e Gemini em nuvem."""
    with _TempAudioFiles() as tmp:
        try:
            if not audio_path or not os.path.isfile(audio_path):
                return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

            tmp_seg_path, audio_bytes, mime_type = _prepare_audio_segment(
                audio_path, start_sec, dur_sec, segment_seconds, quality, tmp
            )
            dsp_info = analyze_dsp_properties(tmp_seg_path)
            low_freq_ratio = dsp_info["low_freq_ratio"]
            low_energy_ratio = dsp_info["low_energy_ratio"]

            from panns_classify import classify_with_panns

            if not api_available or not client:
                panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
                if panns_result and "error" not in panns_result:
                    panns_result["_model_usado"] = "panns_only_no_api"
                    return idx, panns_result
                return idx, {"error": "API do Gemini nao disponivel e PANNs falhou"}

            current_models = shared_models.get_models()

            panns_result = None
            gemini_result = None

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_panns = executor.submit(classify_with_panns, tmp_seg_path, output_language=output_language)
                future_gemini = executor.submit(
                    classify_audio_bytes,
                    client, audio_bytes, mime_type=mime_type,
                    models=current_models, on_model_failed=shared_models.remove_model,
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
                err_msg = f"CNN14 err: {panns_result.get('error') if panns_result else 'None'}; Gemini err: {gemini_result.get('error') if gemini_result else 'None'}"
                return idx, {"error": f"Ambas as IAs falharam: {err_msg}"}

            if not panns_ok:
                gemini_result["_model_usado"] = f"gemini_{gemini_result.get('_model_usado', 'hybrid')}_panns_failed"
                final_res = gemini_result
                rule_applied = "panns_failed"
            elif not gemini_ok:
                panns_result["_model_usado"] = "panns_gemini_failed"
                final_res = panns_result
                rule_applied = "gemini_failed"
            else:
                f_cat, f_inst, f_conf, rule_applied = _arbitrate(panns_result, gemini_result, output_language)
                notes_parts = [f"CNN14={panns_result.get('instrument')}", f"Gemini={gemini_result.get('instrument')}"]
                final_res = {
                    "category": f_cat,
                    "instrument": f_inst,
                    "confidence": f_conf,
                    "notes": f"Arbítrio: {rule_applied} | " + " | ".join(notes_parts)
                }

            final_res["confidence"] = safe_float(final_res.get("confidence"), 0.5)

            # Verificador de Sanidade (DSP)
            orig_category = final_res.get("category")
            orig_instrument = final_res.get("instrument")
            notes = final_res.get("notes", "")

            if low_freq_ratio > 0.45:
                if orig_category not in ["baixo", "bateria"] or not any(kw in (orig_instrument or "").lower() for kw in ["bass", "baixo", "kick", "bumbo", "sub"]):
                    final_res["category"] = "baixo"
                    final_res["instrument"] = "Baixo/Bumbo (DSP Grave <100Hz)" if output_language == "pt" else "Bass/Kick (DSP Low-Freq <100Hz)"
                    final_res["notes"] = notes + f" | [DSP Override: Grave (F={low_freq_ratio:.2f})]"
            elif low_energy_ratio > 0.75:
                if orig_category != "bateria" and not any(kw in (orig_instrument or "").lower() for kw in ["perc", "shaker", "drum", "hat", "hit"]):
                    final_res["category"] = "bateria"
                    final_res["instrument"] = "Percussão (DSP Transiente Curto)" if output_language == "pt" else "Percussion (DSP Short Transient)"
                    final_res["notes"] = notes + f" | [DSP Override: Percussivo (S={low_energy_ratio:.2f})]"

            final_res["_model_usado"] = f"hybrid_{rule_applied}"
            return idx, final_res

        except Exception as e:
            return idx, {"error": f"{type(e).__name__}: {e}"}


def _process_one_chaining(
    client: Any, idx: int, audio_path: str, start_sec: Optional[float], dur_sec: Optional[float],
    shared_models: SharedModelList, segment_seconds: float, quality: str, api_available: bool,
    output_language: str
) -> Tuple[int, Dict[str, Any]]:
    """Processa UMA track com a arquitetura de Chaining (PANNs + DSP -> Gemini)."""
    with _TempAudioFiles() as tmp:
        try:
            if not audio_path or not os.path.isfile(audio_path):
                return idx, {"error": f"arquivo nao encontrado: {audio_path}"}

            tmp_seg_path, audio_bytes, mime_type = _prepare_audio_segment(
                audio_path, start_sec, dur_sec, segment_seconds, quality, tmp
            )
            
            # 1. Roda PANNs
            from panns_classify import classify_with_panns
            panns_result = classify_with_panns(tmp_seg_path, output_language=output_language)
            
            if not api_available or not client:
                if panns_result and "error" not in panns_result:
                    panns_result["_model_usado"] = "panns_only_no_api"
                    return idx, panns_result
                return idx, {"error": "API do Gemini nao disponivel e PANNs falhou"}

            if panns_result and "error" in panns_result:
                print(f"  [Chaining] PANNs falhou na track {idx}: {panns_result['error']}. Tentando apenas com Gemini.")
                panns_result = {"category": "desconhecida", "instrument": "falha na analise local", "confidence": 0.0}

            # 2. Analise DSP
            dsp_info = analyze_dsp_properties(tmp_seg_path)
            
            # 3. Monta o prompt dinamico injetando resultados do PANNs e DSP
            chaining_prompt = build_chaining_prompt(panns_result, output_language=output_language)
            
            dsp_context = (
                f"\n\n[ANÁLISE DSP ESPECTRAL]\n"
                f"Concentração de Graves (<100Hz): {dsp_info['low_freq_ratio']:.2f} (Valores >0.4 indicam bumbo, baixo, 808)\n"
                f"Transientes Curtos (Decaimento Abrupto): {dsp_info['low_energy_ratio']:.2f} (Valores >0.7 indicam percussão pura sem ressonância)\n"
            )
            if output_language == "en":
                dsp_context = (
                    f"\n\n[SPECTRAL DSP ANALYSIS]\n"
                    f"Low Frequency Concentration (<100Hz): {dsp_info['low_freq_ratio']:.2f} (Values >0.4 indicate kick drum, bass, 808)\n"
                    f"Short Transients (Abrupt Decay): {dsp_info['low_energy_ratio']:.2f} (Values >0.7 indicate pure percussion without resonance)\n"
                )
            
            chaining_prompt += dsp_context

            current_models = shared_models.get_models()
            gemini_result = classify_audio_bytes(
                client, audio_bytes, mime_type=mime_type,
                models=current_models, on_model_failed=shared_models.remove_model,
                output_language=output_language,
                custom_prompt=chaining_prompt
            )

            if gemini_result and "error" not in gemini_result:
                gemini_result["_model_usado"] = "hybrid_chaining_review"
                return idx, gemini_result
            else:
                panns_result["_model_usado"] = "panns_gemini_failed"
                return idx, panns_result

        except Exception as e:
            return idx, {"error": f"{type(e).__name__}: {e}"}


def _process_one_gemini(
    client: Any, idx: int, audio_path: str, start_sec: Optional[float], dur_sec: Optional[float],
    shared_models: SharedModelList, segment_seconds: float, quality: str, output_language: str
) -> Tuple[int, Dict[str, Any]]:
    """Processa UMA track exclusivamente com a API do Gemini."""
    with _TempAudioFiles() as tmp:
        try:
            if not audio_path or not os.path.isfile(audio_path):
                return idx, {"error": f"arquivo nao encontrado: {audio_path}"}
            
            _, audio_bytes, mime_type = _prepare_audio_segment(
                audio_path, start_sec, dur_sec, segment_seconds, quality, tmp
            )
            
            result = classify_audio_bytes(
                client, audio_bytes, mime_type=mime_type,
                models=shared_models.get_models(), on_model_failed=shared_models.remove_model,
                output_language=output_language
            )
            return idx, result
        except Exception as e:
            return idx, {"error": f"{type(e).__name__}: {e}"}


def process_one(
    client: Any, idx: int, audio_path: str, start_sec: Optional[float], dur_sec: Optional[float],
    shared_models: SharedModelList, segment_seconds: float, quality: str, api_available: bool,
    output_language: str, backend: str = "gemini", cancel_flag: Optional[str] = None
) -> Tuple[int, Dict[str, Any]]:
    if cancel_flag and os.path.exists(cancel_flag):
        return idx, {"error": "cancelled"}
        
    if check_absolute_silence(audio_path, start_sec, dur_sec):
        return idx, {"error": "absolute_silence"}
        
    if backend in LOCAL_BACKENDS:
        return _process_one_local(idx, audio_path, start_sec, dur_sec, segment_seconds, quality, output_language, backend)
    elif backend == "hybrid_heuristic":
        return _process_one_hybrid(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language, backend)
    elif backend == "hybrid_chaining":
        return _process_one_chaining(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language)
    else:
        if not api_available:
            return idx, {"category": "outro", "instrument": "Audio", "confidence": 0.0, "_model_usado": "fallback_universal"}
        return _process_one_gemini(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, output_language)


def main():
    configure_stdout()
    load_env()
    
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

    if args.panns_threads is not None and args.panns_threads > 0:
        os.environ["PANNS_THREADS"] = str(args.panns_threads)
    
    api_key = os.environ.get("GEMINI_API_KEY")
    use_local_backend = (args.backend in LOCAL_BACKENDS)
    client = None

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
        api_available = True
        initial_models = [args.backend]

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

    # OTIMIZAÇÃO MASSIVA PARA PANNS PURO:
    # Em vez de inferir track a track em threads, lemos tudo em paralelo e rodamos 1 forward pass
    if args.backend == "panns":
        print(f"  [PANNs] Preparando {total} faixas para batch inference (processamento muito mais rapido)...")
        prepared_paths = {}
        tmp_manager = _TempAudioFiles()
        
        # 1. Extrair os trechos em paralelo (I/O)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {}
            for idx, path, start, dur in entries:
                if cancel_flag and os.path.exists(cancel_flag):
                    break
                if check_absolute_silence(path, start, dur):
                    results[idx] = {"error": "absolute_silence"}
                    continue
                # Agenda o corte de wav (qualidade alta para PANNs)
                fut = pool.submit(_prepare_audio_segment, path, start, dur, args.segment_seconds, "alta", tmp_manager)
                futures[fut] = idx
            
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    seg_path, _, _ = fut.result()
                    prepared_paths[idx] = seg_path
                except Exception as e:
                    results[idx] = {"error": f"{type(e).__name__}: {e}"}

        # 2. Roda a Inferencia em Batch (1 chamada para GPU)
        valid_indices = list(prepared_paths.keys())
        valid_paths = [prepared_paths[i] for i in valid_indices]
        
        if valid_paths and not (cancel_flag and os.path.exists(cancel_flag)):
            from panns_classify import classify_many_with_panns
            panns_res = classify_many_with_panns(valid_paths, output_language=args.output_language)
            for i, res in enumerate(panns_res):
                results[valid_indices[i]] = res
        
        # Limpa temporarios
        tmp_manager.__exit__(None, None, None)
        
        for idx in [e[0] for e in entries]:
            if idx not in results:
                results[idx] = {"error": "cancelado ou pulado"}

    # FLUXO NORMAL (Thread Pool com API ou outros locais)
    else:
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
                    conf = safe_float(result.get('confidence'), 0.0) * 100
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

    if args.done_flag:
        with open(args.done_flag, "w") as f:
            f.write("done")


if __name__ == "__main__":
    main()
