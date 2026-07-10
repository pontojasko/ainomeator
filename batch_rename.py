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

from audio_utils import extract_best_segment, downmix_resample, extract_three_peaks, convert_to_mp3_128k, remove_all_silence
from classify_track import classify_audio_bytes, MODELOS_FALLBACK


class SharedModelList:
    def __init__(self, initial_models):
        self.lock = threading.Lock()
        self.models = list(initial_models)

    def get_models(self):
        with self.lock:
            return list(self.models)

    def remove_model(self, model):
        with self.lock:
            if model in self.models and len(self.models) > 1:
                print(f"\n  [AVISO] Modelo '{model}' indisponível/falhou! Ativando fallback global de modelo para todas as demais faixas do lote...")
                self.models.remove(model)


def check_api_availability(client, models):
    print("\n[batch_rename] Checking Gemini API availability...")
    for i, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents=["Responda apenas 'OK'."],
                config=types.GenerateContentConfig(temperature=0.1)
            )
            if response.text:
                print(f"  [OK] API available (model: {model}).")
                return True, models[i:]
        except Exception as e:
            print(f"  [ERRO] Failed with {model} ({type(e).__name__}: {e}).")
    return False, models


def _sanitize(text):
    if text is None:
        return ""
    return str(text).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def process_one(client, idx, audio_path, start_sec, dur_sec, shared_models, segment_seconds, quality, api_available, output_language):
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
            # Análise detalhada: remove todos os silêncios, mantendo a qualidade original (wav)
            remove_all_silence(audio_path, tmp_seg_path)
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


def handle_color_generation(client, models, prompt_text, config_path):
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
                
                print(f"  [OK] New color palette successfully saved in: {config_path}")
                return
                
            except Exception as e:
                erro_str = f"{type(e).__name__}: {e}"
                eh_transitorio = any(marcador in erro_str for marcador in ERROS_TRANSITORIOS)
                
                if not eh_transitorio or tentativa == 2:
                    break
                
                time.sleep(2)
                
        if idx_modelo < len(models) - 1:
            print(f"  [{model} indisponivel para cores, tentando proximo modelo: {models[idx_modelo + 1]}...]")
            
    print(f"  [ERRO] Could not generate custom color palette. Using existing palette.")


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
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERRO: GEMINI_API_KEY nao encontrada (crie/edite o .env nesta pasta).")
        sys.exit(1)

    if not os.path.isfile(args.manifest_path):
        print(f"ERRO: manifest nao encontrado: {args.manifest_path}")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    models = args.models.split(",") if args.models else None
    initial_models = models if models else MODELOS_FALLBACK

    api_available, working_models = check_api_availability(client, initial_models)
    if not api_available:
        print("\n[AVISO] Initial check failed. Activating universal fallback (no AI) for all steps.")
    
    # Atualiza a lista de modelos para começar do que funcionou
    initial_models = working_models

    # Se o prompt de cores foi informado, gera a paleta antes do processamento das faixas
    if args.color_prompt and args.config_path:
        if api_available:
            handle_color_generation(client, initial_models, args.color_prompt, args.config_path)
        else:
            print(f"\n[batch_rename] Pulando geração de cores customizada (API indisponível). Usando paleta existente.")

    entries = read_manifest(args.manifest_path)
    total = len(entries)

    if total == 0:
        print("No tracks with audio in manifest. Nothing to do.")
        from pathlib import Path
        Path(args.result_path).touch()
        return

    shared_models = SharedModelList(initial_models)

    print(f"[batch_rename] {total} track(s) in queue, {args.workers} thread(s) in parallel "
          f"(modelo principal: {initial_models[0]})...")

    results = {}
    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(process_one, client, idx, path, start, dur, shared_models, args.segment_seconds, args.quality, api_available, args.output_language): idx
            for idx, path, start, dur in entries
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result
            done += 1
            if "error" in result:
                print(f"  [ERRO] [{done}/{total}] track {idx}: {result['error'][:100]}")
            else:
                modelo_usado = result.get('_model_usado', '?')
                print(f"  [OK]   [{done}/{total}] track {idx}: {result.get('category')} - "
                      f"{result.get('instrument')} (confianca {result.get('confidence')}) [{modelo_usado}]")

    with open(args.result_path, "w", encoding="utf-8") as f:
        for idx, path, start, dur in entries:
            r = results.get(idx, {"error": "sem resultado (thread nao completou)"})
            if "error" in r:
                f.write(f"{idx}\terro\t\t\t\t{_sanitize(r['error'])}\n")
            else:
                f.write(
                    f"{idx}\tok\t{_sanitize(r.get('category'))}\t"
                    f"{_sanitize(r.get('instrument'))}\t{r.get('confidence', '')}\t\n"
                )

    ok_count = sum(1 for r in results.values() if "error" not in r)
    elapsed = time.time() - t0
    print(f"\n[DONE] Completed in {elapsed:.1f}s  |  {ok_count}/{total} ok  |  resultado -> {args.result_path}")

    # Cria o arquivo sentinela APOS gravar o result.tsv por completo.
    # O Lua faz polling nesse arquivo pra saber que pode ler o resultado sem
    # risco de pegar uma escrita parcial.
    if args.done_flag:
        with open(args.done_flag, "w") as f:
            f.write("done")


if __name__ == "__main__":
    main()
