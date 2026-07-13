"""
classify_track.py

Envia um arquivo de audio para a API do Gemini e recebe de volta o
instrumento/fonte identificada, em JSON.

FASE 1 do projeto: isso roda sozinho, sem Reaper nenhum. O objetivo
e validar 100% a parte de IA (prompt, formato de resposta, precisao)
ANTES de qualquer integracao com ReaScript.

Uso:
    python classify_track.py "caminho/para/audio.wav"
    python classify_track.py "caminho/para/audio.wav" --json-out resultado.json

Requisitos:
    pip install -r requirements.txt
    Variavel de ambiente GEMINI_API_KEY definida (ou arquivo .env na mesma pasta)
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

import json
import argparse
import time
import tempfile

from dotenv import load_dotenv
from google import genai
from google.genai import types

from audio_utils import extract_best_segment, downmix_resample

# Categorias permitidas -> mantém o vocabulário fechado para não vir
# "guitar-like instrument with reverb" ou outras respostas fora do padrão
CATEGORIAS_VALIDAS = [
    "vocal", "guitarra", "baixo", "bateria",
    "teclado", "synth", "sopro", "cordas", "outro"
]

# Ordem de preferencia: tenta o primeiro, se estiver sobrecarregado (503)
# cai pro proximo. Cada modelo roda em cluster de capacidade separado no
# Google, entao um 503 num nao significa 503 no outro.
#
# gemini-3.5-flash em primeiro: tem percepcao mais aguçada pra nuances
# musicais (articulacoes, timbres hibridos, instrumentos polifonicos vs
# monofonicos). A diferenca de latencia pro flash-lite e pequena e compensa
# pela reducao de erros de classificacao em trechos ambiguos.
load_dotenv()
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_ENV = os.path.join(os.path.dirname(_SCRIPT_DIR), ".env")
if os.path.exists(_PARENT_ENV):
    load_dotenv(_PARENT_ENV)

# Tenta ler do .env os modelos preferidos pelo usuário, caso contrário usa a ordem padrão
model_env = os.environ.get("GEMINI_MODELS")
if model_env:
    MODELOS_FALLBACK = [m.strip() for m in model_env.split(",") if m.strip()]
else:
    MODELOS_FALLBACK = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]

DEFAULT_PROMPT_PT = """Voce e um assistente de organizacao de faixas de audio dentro de uma DAW (estacao de audio digital).

Ouca este trecho de audio, que e uma unica faixa isolada de uma sessao de gravacao multitrack (nao e a mixagem completa).

Identifique a fonte sonora principal e responda APENAS com um JSON valido, sem nenhum texto antes ou depois, exatamente neste formato:

{
    "instrument": "nome curto e especifico em portugues descrevendo o timbre e a funcao, ex: 'guitarra base distorcida', 'vocal principal', 'baixo acustico pizzicato', 'synth ritmico de acordes', 'flauta em acordes (sampler)'",
    "category": "uma destas opcoes exatas: vocal, guitarra, baixo, bateria, teclado, synth, sopro, cordas, outro",
    "confidence": numero de 0.0 a 1.0 indicando sua confianca na identificacao,
    "notes": "uma frase curta com qualquer observacao relevante (ex: 'audio com ruido de fundo', 'silencio quase total', 'multiplos instrumentos misturados')"
}

Atencao a armadilhas comuns de articulacao e timbre:
- Instrumentos de corda (baixo, violoncelo, contrabaixo, etc) tocados em pizzicato ou slap tem um ataque percussivo forte com decaimento rapido, mas EMITEM NOTAS AFINADAS. Nao os confunda com bateria. Se voce ouve notas musicais claras (com altura definida), NAO e bateria — e o instrumento de corda com articulacao percussiva.
- Bateria de verdade produz sons SEM altura musical definida (bumbo, caixa, hi-hat, pratos). Se ha notas musicais claras, mesmo com ataque percussivo, NAO classifique como bateria.
- Se um instrumento tipicamente monofonico (flauta, sax, trompete) estiver tocando acordes ou multiplas notas simultaneas, provavelmente e um sampler ou synth imitando o timbre desse instrumento. Use category "synth" ou "teclado" e descreva no campo instrument o timbre e a funcao ritmica/harmonica (ex: 'synth ritmico com timbre de flauta', 'sampler de sopro em acordes').
- Sempre priorize a PRESENCA OU AUSENCIA DE NOTAS MUSICAIS AFINADAS como criterio principal para distinguir bateria de outros instrumentos.
- NUNCA inclua palavras como "mono" ou "stereo" na descricao do instrumento. O nome deve ser limpo.
- Adicione "Esquerda" ou "Direita" apenas se o audio estiver claramente e inegavelmente isolado de um lado.

Se o audio estiver em silencio, muito baixo, ou nao for possivel identificar, use category "outro" e confidence baixo, nao invente.
"""

DEFAULT_PROMPT_EN = """You are an assistant for organizing audio tracks inside a DAW.

Listen to this audio excerpt, which is a single isolated track from a multitrack recording session (not the full mix).

Identify the main sound source and respond ONLY with valid JSON, with no text before or after, exactly in this format:

{
    "instrument": "short and specific name in English describing the timbre and role, e.g. 'distorted rhythm guitar', 'lead vocal', 'pizzicato acoustic bass', 'rhythmic chord synth', 'flute-like sampler chords'",
    "category": "one of these exact values: vocal, guitarra, baixo, bateria, teclado, synth, sopro, cordas, outro",
    "confidence": a number from 0.0 to 1.0 indicating your confidence in the identification,
    "notes": "a short sentence with any relevant observation (e.g. 'background noise', 'almost complete silence', 'multiple instruments mixed')"
}

Watch out for common articulation and timbre traps:
- String instruments (bass, cello, double bass, etc.) played pizzicato or slap have a strong percussive attack with a fast decay, but they still PRODUCE PITCHED NOTES. Do not confuse them with drums. If you hear clear musical notes (defined pitch), it is NOT drums — it is a string instrument with percussive articulation.
- Real drums produce sounds WITHOUT defined musical pitch (kick, snare, hi-hat, cymbals). If clear musical notes are present, even with a percussive attack, do NOT classify it as drums.
- If a typically monophonic instrument (flute, sax, trumpet) is playing chords or multiple simultaneous notes, it is probably a sampler or synth imitating that timbre. Use category "synth" or "teclado" and describe the timbre and rhythmic/harmonic role in instrument (e.g. 'rhythmic flute-like synth', 'brass sampler chords').
- Always prioritize the PRESENCE OR ABSENCE OF PITCHED MUSICAL NOTES as the main criterion for distinguishing drums from other instruments.
- NEVER include words like "mono" or "stereo" in the instrument description. The name must be clean.
- Add "Left" or "Right" only if the audio is clearly and undeniably panned to one side.

If the audio is silent, too quiet, or cannot be identified, use category "outro" and low confidence; do not invent.
"""


def build_default_prompt(output_language="pt"):
    return DEFAULT_PROMPT_EN if output_language == "en" else DEFAULT_PROMPT_PT


def load_prompt(output_language="pt"):
    """Le o prompt de analise do arquivo analysis_prompt.txt se existir,
    caso contrario usa o prompt padrao hardcoded."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, "analysis_prompt.txt")
    if os.path.isfile(prompt_path):
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    if output_language == "en":
                        return content + "\n\nImportant: write the 'instrument' field in English. Keep the category values exactly as specified."
                    return content
        except Exception as e:
            print(f"[AVISO] Falha ao ler {prompt_path}: {e}. Usando prompt padrao.")
    return build_default_prompt(output_language)


def build_chaining_prompt(panns_result, output_language="pt"):
    """
    Constrói o prompt para o modo 'Híbrido com Review'.
    Injeta o resultado do PANNs (modelo local) para que o Gemini possa refinar e polir a resposta,
    priorizando a opinião do modelo local que possui ótima acurácia rítmica/tímbrica.
    """
    cat = panns_result.get("category", "desconhecida")
    inst = panns_result.get("instrument", "desconhecido")
    conf = panns_result.get("confidence", 0.0)

    # Base prompt (system context & task instruction)
    if output_language == "en":
        prompt = f"""You are an expert audio reviewer organizing tracks inside a DAW.

Listen to this single isolated audio track. You must classify its main sound source and output a valid JSON.
A highly accurate local machine learning model (CNN14/PANNs) has already analyzed this track. It is very good at identifying the instrument family and rhythmic features.

[LOCAL MODEL PREDICTION]
Category: {cat}
Instrument Details: {inst}
Confidence: {conf}

Your task is to LISTEN to the audio and REVIEW the local model's prediction.
- DO NOT ignore the local model. Use its prediction as your strong baseline.
- Help POLISH and refine the "instrument" description based on what you actually hear in the stem.
- If you agree with the local model, output a polished version of its category and instrument description.
- Only change the "category" if the local model is clearly and undeniably wrong (e.g., local model heard "vocal" but you clearly hear a "shaker/percussion").
- NEVER include words like "mono" or "stereo" in the instrument description. The name must be clean. Add "Left" or "Right" only if it is clearly panned to one side.

Respond ONLY with valid JSON exactly in this format:
{{
    "instrument": "short and specific name in English describing the timbre and role, incorporating the local model's insight but polished based on your hearing",
    "category": "one of these exact values: vocal, guitarra, baixo, bateria, teclado, synth, sopro, cordas, outro",
    "confidence": a number from 0.0 to 1.0 (you can increase it if you agree with the local model),
    "notes": "a short sentence confirming how you used the local model's insight or why you disagreed"
}}
"""
    else:
        prompt = f"""Voce e um especialista em revisao de audio organizando faixas dentro de uma DAW.

Ouca esta faixa de audio isolada. Voce deve classificar a fonte sonora principal e retornar APENAS um JSON valido.
Um modelo local de machine learning de alta precisao (CNN14/PANNs) ja analisou esta faixa. Ele e muito bom em identificar a familia do instrumento e caracteristicas ritmicas.

[PREVISAO DO MODELO LOCAL]
Categoria: {cat}
Detalhes do Instrumento: {inst}
Confianca: {conf}

Sua tarefa e OUVIR o audio e REVISAR a previsao do modelo local.
- NAO ignore o modelo local. Use a previsao dele como sua base principal (baseline).
- Ajude a POLIR e refinar a descricao do "instrument" (instrumento) baseado na sua propria analise da stem e na opiniao do modelo local.
- Se voce concorda com a analise local, retorne uma versao polida da categoria e instrumento dele.
- Mude a "category" (categoria) apenas se o modelo local estiver clara e inegavelmente errado (ex: o modelo local ouviu "vocal" mas voce ouve nitidamente uma "percussao/shaker").
- NUNCA inclua palavras como "mono" ou "stereo" na descricao do instrumento. O nome deve ser limpo. Adicione "Esquerda" ou "Direita" apenas se o audio estiver claramente isolado de um lado.

Responda APENAS com um JSON valido, sem markdown, exatamente neste formato:
{{
    "instrument": "nome curto e especifico descrevendo o timbre e a funcao, incorporando o insight do modelo local mas polido de acordo com o que voce ouviu",
    "category": "uma destas opcoes exatas: vocal, guitarra, baixo, bateria, teclado, synth, sopro, cordas, outro",
    "confidence": numero de 0.0 a 1.0 (voce pode aumentar a confianca se concordar com o modelo local),
    "notes": "uma frase curta confirmando como voce usou a base do modelo local ou justificando caso tenha discordado"
}}
"""
    return prompt


def classify_track(client, audio_path, models=None, segment_seconds=8, keep_temp=False,
                    retries_per_model=2, search_start_seconds=None, search_duration_seconds=None,
                    on_model_failed=None, output_language="pt"):
    """
    Fluxo completo: acha o trecho de maior energia no arquivo (local, sem IA),
    corta ele pra um wav temporario curto, gera uma versao leve (mono/24kHz)
    e SO ENTAO manda pro Gemini como bytes inline (sem upload/polling).

    Isso evita mandar o stem inteiro (que pode ter minutos de silencio ou
    trechos onde o instrumento nem toca) pra API, economizando tempo e custo.

    `search_start_seconds`/`search_duration_seconds` (opcionais) restringem
    a busca do trecho de maior energia a uma janela especifica do arquivo -
    usado na integracao com o Reaper (Fase 4), onde cada item so ocupa uma
    parte do arquivo-fonte.
    """
    if not os.path.isfile(audio_path):
        return {"error": f"arquivo nao encontrado: {audio_path}"}

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_segment_")
    os.close(tmp_fd)
    light_fd, light_path = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_light_")
    os.close(light_fd)

    try:
        _, start_sec, dur_sec = extract_best_segment(
            audio_path, tmp_path, segment_seconds=segment_seconds,
            search_start_seconds=search_start_seconds,
            search_duration_seconds=search_duration_seconds,
        )
        downmix_resample(tmp_path, light_path)
    except Exception as e:
        for p in (tmp_path, light_path):
            if os.path.isfile(p):
                os.remove(p)
        return {"error": f"falha ao extrair trecho de audio: {type(e).__name__}: {e}"}

    with open(light_path, "rb") as f:
        audio_bytes = f.read()

    result = classify_audio_bytes(
        client, audio_bytes, models=models,
        retries_per_model=retries_per_model, on_model_failed=on_model_failed,
        output_language=output_language
    )
    result["_segment_start_seconds"] = round(start_sec, 2)
    result["_segment_duration_seconds"] = round(dur_sec, 2)

    if keep_temp:
        result["_segment_file"] = tmp_path
        result["_segment_file_leve"] = light_path
    else:
        os.remove(tmp_path)
        os.remove(light_path)

    return result


def classify_audio(client, audio_path, models=None, retries_per_model=2, on_model_failed=None,
                   output_language="pt"):
    """Le um arquivo de audio do disco e manda pro Gemini (via bytes inline).

    Mantido por compatibilidade com test_batch.py / uso direto. Nao faz
    corte de trecho nem downsample - se o arquivo for grande, prefira
    `classify_track`, que ja cuida disso.
    """
    if not os.path.isfile(audio_path):
        return {"error": f"arquivo nao encontrado: {audio_path}"}

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    mime = "audio/wav" if audio_path.lower().endswith(".wav") else "audio/mpeg"
    return classify_audio_bytes(client, audio_bytes, mime_type=mime, models=models,
                                 retries_per_model=retries_per_model, on_model_failed=on_model_failed,
                                 output_language=output_language)


def classify_audio_bytes(client, audio_bytes, mime_type="audio/wav", models=None,
                         retries_per_model=2, on_model_failed=None, output_language="pt",
                         custom_prompt=None):
    """Manda os bytes do audio direto pro Gemini (sem passar por disco/upload)
    e retorna o dict já parseado (ou dict com 'error').

    Usa `types.Part.from_bytes` (dado inline no proprio request) em vez da
    API de upload de arquivos - pra trechos curtos (poucos segundos, mono,
    24kHz) isso e bem mais rapido, porque evita o ciclo de
    upload -> aguardar estado ACTIVE -> gerar conteudo -> (deletar arquivo).
    Importante quando varias tracks sao processadas em paralelo (Fase 4).

    Tenta uma lista de modelos em ordem (MODELOS_FALLBACK por padrao). Se um
    modelo der erro transitorio (503 sobrecarga, 429 rate limit) tenta de
    novo `retries_per_model` vezes nesse mesmo modelo com backoff, e se
    continuar falhando PULA pro proximo modelo da lista, em vez de ficar
    insistindo no mesmo cluster sobrecarregado.

    Erros permanentes (404 modelo nao existe, 401 chave invalida) nao
    tentam de novo em hipotese nenhuma, pulam direto pro proximo modelo.
    """
    if models is None:
        models = MODELOS_FALLBACK

    ERROS_TRANSITORIOS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DeadlineExceeded", "timeout")

    erros_por_modelo = {}

    prompt_to_use = custom_prompt if custom_prompt is not None else load_prompt(output_language)

    for idx_modelo, model in enumerate(models):
        for tentativa in range(1, retries_per_model + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[prompt_to_use, types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                        top_k=1,
                    ),
                )

                raw_text = response.text.strip()
                result = json.loads(raw_text)

                if result.get("category") not in CATEGORIAS_VALIDAS:
                    result["_warning"] = f"categoria fora do vocabulario esperado: {result.get('category')}"

                result["_model_usado"] = model
                return result

            except json.JSONDecodeError as e:
                # resposta veio mas nao era JSON valido -> problema do modelo, nao de rede
                # nao adianta re-tentar, mas vale tentar o proximo modelo da lista
                erros_por_modelo[model] = f"resposta nao era JSON valido: {e}"
                break

            except Exception as e:
                erro_str = f"{type(e).__name__}: {e}"
                erros_por_modelo[model] = erro_str[:150]

                eh_transitorio = any(marcador in erro_str for marcador in ERROS_TRANSITORIOS)
                if not eh_transitorio:
                    # erro permanente (chave invalida, modelo nao existe) -> pula direto pro proximo modelo e bane globalmente
                    if on_model_failed:
                        on_model_failed(model)
                    break

                if tentativa == retries_per_model:
                    # esgotou as tentativas nesse modelo -> remove do fallback global para as proximas faixas
                    if on_model_failed:
                        on_model_failed(model)
                    break

                espera = 2 ** tentativa  # 2s, 4s
                print(f"  [{model}: tentativa {tentativa}/{retries_per_model} falhou "
                      f"({erro_str[:80]}), tentando de novo em {espera}s...]")
                time.sleep(espera)

        if idx_modelo < len(models) - 1:
            print(f"  [{model} indisponivel, tentando proximo modelo: {models[idx_modelo + 1]}...]")

    return {"error": "todos os modelos falharam", "_erros_por_modelo": erros_por_modelo}


def main():
    parser = argparse.ArgumentParser(description="Classifica instrumento de uma faixa de audio via Gemini")
    parser.add_argument("audio_path", help="caminho do arquivo de audio (wav, mp3, m4a, etc)")
    parser.add_argument("--json-out", help="se definido, salva o resultado nesse arquivo JSON", default=None)
    parser.add_argument("--models", default=None,
                         help=f"lista de modelos separados por virgula, em ordem de preferencia "
                              f"(padrao: {','.join(MODELOS_FALLBACK)})")
    parser.add_argument("--segment-seconds", type=float, default=8,
                         help="duracao do trecho analisado (padrao: 8s). Usa o trecho de maior energia do arquivo.")
    parser.add_argument("--full", action="store_true",
                         help="ignora o corte e manda o arquivo inteiro (mais lento/caro, use so pra comparar)")
    parser.add_argument("--keep-segment", action="store_true",
                         help="nao apaga o wav temporario do trecho cortado (util pra conferir o que foi analisado)")
    parser.add_argument("--output-language", choices=["pt", "en"], default="pt",
                         help="idioma do campo instrument: pt ou en (padrao: pt)")
    args = parser.parse_args()

    models = args.models.split(",") if args.models else None

    load_dotenv()
    if os.path.exists(_PARENT_ENV):
        load_dotenv(_PARENT_ENV)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERRO: variavel GEMINI_API_KEY nao encontrada.")
        print("Crie um arquivo .env nesta pasta com a linha:")
        print("GEMINI_API_KEY=sua_chave_aqui")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    if args.full:
        print(f"Analisando (arquivo inteiro): {args.audio_path}")
        result = classify_audio(client, args.audio_path, models=models, output_language=args.output_language)
    else:
        print(f"Analisando (trecho de {args.segment_seconds:.0f}s de maior energia): {args.audio_path}")
        result = classify_track(
            client, args.audio_path, models=models,
            segment_seconds=args.segment_seconds, keep_temp=args.keep_segment,
            output_language=args.output_language
        )
        if "_segment_start_seconds" in result:
            print(f"  (trecho analisado: {result['_segment_start_seconds']}s "
                  f"até {result['_segment_start_seconds'] + result['_segment_duration_seconds']:.1f}s)")

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nSalvo em: {args.json_out}")

    # exit code != 0 se deu erro, util pra scripts de teste em lote
    if "error" in result:
        sys.exit(2)


if __name__ == "__main__":
    main()
