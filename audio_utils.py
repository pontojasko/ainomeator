"""
audio_utils.py

Encontra a regiao de maior energia (mais "cheia" de audio, menos silencio)
dentro de um arquivo, e extrai so esse trecho pra um wav temporario curto.

Isso e feito 100% local (numpy, sem IA), entao e rapido e nao gasta
chamada de API. So DEPOIS de cortar o trecho e que mandamos pro Gemini.

Motivo: stems costumam ter silencio no inicio/fim, ou trechos onde o
instrumento nao esta tocando (ex: guitarra que so entra no refrao).
Mandar o arquivo inteiro pro Gemini e lento e caro sem necessidade.

FASE 4 (integracao Reaper): alem do corte por energia, esse modulo tambem
sabe (1) restringir a busca a uma JANELA especifica do arquivo-fonte -
util quando o item na track e so um pedacinho de um arquivo maior, ou
quando varios items compartilham a mesma fonte - e (2) gerar uma versao
"leve" do trecho (mono, 24kHz) pra mandar pra API o mais rapido possivel,
ja que qualidade de audiofilo nao importa pra so identificar o instrumento.

NOTA sobre resample (downmix_resample):
    Usa np.interp (interpolacao linear) pra fazer o downsample de
    44.1kHz -> 24kHz. Isso e rapido e sem dependencias extras, mas
    introduz leve aliasing nos harmonicos acima de 12kHz. Para o
    proposito de classificacao de instrumento (nao masterizacao), o
    impacto pratico e minimo — a IA identifica corretamente guitarras,
    vocais e bateria mesmo com essa simplificacao. Se precisar de maior
    fidelidade espectral, substitua np.interp por scipy.signal.resample_poly.
"""

import os
import subprocess
import tempfile

import numpy as np
import soundfile as sf


def _read_audio(path):
    """Le o audio com soundfile. Se falhar (formato nao suportado pelo
    libsndfile instalado, ex: alguns mp3), tenta converter com ffmpeg
    (se estiver disponivel no PATH) pra um wav temporario e le de novo.
    Se nada funcionar, propaga o erro original.
    
    Aplica tambem conversao para mono e peak normalization."""
    try:
        data, sr = sf.read(path, always_2d=True)
    except Exception as e_original:
        tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="ai_namer_conv_")
        os.close(tmp_fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-ar", "44100", tmp_wav],
                check=True, capture_output=True, timeout=60,
            )
            data, sr = sf.read(tmp_wav, always_2d=True)
        except Exception:
            raise e_original
        finally:
            if os.path.isfile(tmp_wav):
                os.remove(tmp_wav)

    # Converter para mono (se tiver mais de um canal)
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)

    # Peak normalization
    max_val = np.max(np.abs(data))
    if max_val > 0:
        data = data / max_val

    return data, sr


def extract_best_segment(audio_path, out_path, segment_seconds=8, hop_seconds=0.5,
                          search_start_seconds=None, search_duration_seconds=None):
    """
    Le o arquivo de audio, acha a janela de `segment_seconds` com maior
    energia media (RMS), e salva so essa janela em `out_path`.

    Se `search_start_seconds`/`search_duration_seconds` forem informados,
    a busca fica restrita a esse trecho do arquivo (por exemplo, o range
    em que um item especifico do Reaper realmente usa a fonte), em vez de
    vasculhar o arquivo inteiro - importante quando varias tracks apontam
    pro mesmo arquivo-fonte mas usam pedacos diferentes dele.

    Retorna (out_path, start_seconds, duration_seconds) - start/duration
    sao relativos ao arquivo INTEIRO (nao a janela de busca), pra debug/log.
    """
    data, samplerate = _read_audio(audio_path)
    total_frames = data.shape[0]

    window_start_frame = 0
    if search_start_seconds is not None:
        window_start_frame = min(max(0, int(search_start_seconds * samplerate)), total_frames)

    if search_duration_seconds is not None:
        window_frames = min(int(search_duration_seconds * samplerate), total_frames - window_start_frame)
    else:
        window_frames = total_frames - window_start_frame
    window_frames = max(0, window_frames)

    windowed = data[window_start_frame:window_start_frame + window_frames]

    if segment_seconds is None or window_frames == 0:
        segment = windowed if window_frames > 0 else data
        best_start_in_window = 0
    else:
        segment_frames = int(segment_seconds * samplerate)
        if window_frames <= segment_frames:
            segment = windowed
            best_start_in_window = 0
        else:
            mono = windowed.mean(axis=1).astype(np.float64)
            hop_frames = max(1, int(hop_seconds * samplerate))

            # soma de energia (quadrado) via cumsum, pra achar a janela mais "cheia"
            # sem precisar recalcular a soma do zero pra cada posicao (rapido mesmo
            # em arquivos de varios minutos)
            squared = mono ** 2
            cumsum = np.cumsum(np.insert(squared, 0, 0.0))

            starts = np.arange(0, window_frames - segment_frames + 1, hop_frames)
            energies = cumsum[starts + segment_frames] - cumsum[starts]

            best_start_in_window = int(starts[np.argmax(energies)])
            segment = windowed[best_start_in_window:best_start_in_window + segment_frames]

    sf.write(out_path, segment, samplerate)
    absolute_start = (window_start_frame + best_start_in_window) / samplerate
    return out_path, absolute_start, segment.shape[0] / samplerate


def downmix_resample(in_path, out_path, target_sr=24000, keep_stereo=False):
    """
    Converte pra mono + reduz a taxa de amostragem, gerando um wav bem
    menor/mais leve pra mandar pra API. A analise so precisa reconhecer
    o instrumento, nao precisa de qualidade de audiofilo - isso deixa o
    envio bem mais rapido, principalmente com varias tracks em paralelo.

    Usa 24kHz em vez de 16kHz pra preservar mais harmonicos superiores
    que ajudam a IA a distinguir articulacoes (pizzicato vs. bateria,
    "ar" de flauta vs. synth, etc). Um trecho de 8s mono 24kHz 16-bit
    da uns 384KB - aumento aceitavel pro ganho de acuracia.

    Nota: o downsample usa interpolacao linear (np.interp), que e rapido
    e sem dependencias, mas introduce leve aliasing acima de 12kHz.
    Para classificacao de instrumento isso e aceitavel. Veja o docstring
    do modulo para detalhes e alternativas.
    """
    data, sr = sf.read(in_path, always_2d=True)
    
    if keep_stereo:
        out_data = data
    else:
        out_data = data.mean(axis=1, keepdims=True)

    if target_sr is not None and sr != target_sr and out_data.shape[0] > 1:
        duration = out_data.shape[0] / sr
        n_target = max(1, int(round(duration * target_sr)))
        x_old = np.linspace(0, duration, num=out_data.shape[0], endpoint=False)
        x_new = np.linspace(0, duration, num=n_target, endpoint=False)
        
        resampled_channels = []
        for ch in range(out_data.shape[1]):
            resampled_channels.append(np.interp(x_new, x_old, out_data[:, ch]))
        
        out_data = np.column_stack(resampled_channels)
    else:
        target_sr = sr  # Mantem o samplerate original caso target_sr seja None

    sf.write(out_path, out_data.astype(np.float32), target_sr, subtype="PCM_16")
    return out_path


def extract_three_peaks(audio_path, out_path, search_start_seconds=None, search_duration_seconds=None, segment_seconds=4):
    """
    Le o audio, extrai 3 trechos de segment_seconds dos picos de energia (comeco, meio, fim)
    e concatena em um unico arquivo WAV.
    """
    data, samplerate = _read_audio(audio_path)
    total_frames = data.shape[0]

    window_start_frame = 0
    if search_start_seconds is not None:
        window_start_frame = min(max(0, int(search_start_seconds * samplerate)), total_frames)

    if search_duration_seconds is not None:
        window_frames = min(int(search_duration_seconds * samplerate), total_frames - window_start_frame)
    else:
        window_frames = total_frames - window_start_frame
    window_frames = max(0, window_frames)

    windowed = data[window_start_frame:window_start_frame + window_frames]
    total_duration = window_frames / samplerate

    # Se for curto demais para dividir em 3 (ex: menor que 12s), exporta o bloco inteiro
    if total_duration <= segment_seconds * 3 or window_frames == 0:
        segment = windowed if window_frames > 0 else data
        sf.write(out_path, segment, samplerate)
        return out_path, 0, segment.shape[0] / samplerate

    # Divide em 3 partes equivalentes
    part_frames = window_frames // 3
    seg_frames = int(segment_seconds * samplerate)
    hop_frames = int(0.5 * samplerate)

    segments = []
    for i in range(3):
        part_start = i * part_frames
        part_end = part_start + part_frames
        part_data = windowed[part_start:part_end]

        if part_data.shape[0] <= seg_frames:
            segments.append(part_data)
        else:
            mono = part_data.mean(axis=1).astype(np.float64)
            squared = mono ** 2
            cumsum = np.cumsum(np.insert(squared, 0, 0.0))

            starts = np.arange(0, part_data.shape[0] - seg_frames + 1, hop_frames)
            if len(starts) == 0:
                segments.append(part_data[:seg_frames])
            else:
                energies = cumsum[starts + seg_frames] - cumsum[starts]
                best_start = int(starts[np.argmax(energies)])
                segments.append(part_data[best_start:best_start + seg_frames])

    concatenated = np.concatenate(segments, axis=0)
    sf.write(out_path, concatenated, samplerate)
    return out_path, 0, concatenated.shape[0] / samplerate


def remove_all_silence(in_path, out_path, threshold_db=-55.0, block_duration_s=0.05, min_sound_duration_s=0.1, min_silence_duration_s=0.3):
    """
    Remove todos os silêncios (início, meio e fim) do arquivo de áudio
    e salva apenas as partes ativas concatenadas no out_path.
    """
    data, samplerate = _read_audio(in_path)
    if data.shape[0] == 0:
        sf.write(out_path, data, samplerate)
        return out_path

    # Converte para mono para cálculo da energia
    if len(data.shape) > 1 and data.shape[1] > 1:
        mono = np.max(np.abs(data), axis=1)
    else:
        mono = np.abs(data.flatten())

    block_len = int(block_duration_s * samplerate)
    if block_len <= 0:
        block_len = 1

    num_blocks = data.shape[0] // block_len
    if num_blocks == 0:
        sf.write(out_path, data, samplerate)
        return out_path

    threshold = 10 ** (threshold_db / 20.0)

    # Reshape mono para blocos
    trimmed_mono = mono[:num_blocks * block_len]
    blocks = trimmed_mono.reshape((num_blocks, block_len))
    block_max = np.max(blocks, axis=1)

    is_sound = block_max > threshold

    # Parâmetros de suavização
    min_silence_blocks = int(round(min_silence_duration_s / block_duration_s))
    min_sound_blocks = int(round(min_sound_duration_s / block_duration_s))

    # Preenche silêncios muito curtos (ex: pausas entre palavras)
    silence_count = 0
    for i in range(num_blocks):
        if not is_sound[i]:
            silence_count += 1
        else:
            if silence_count > 0 and silence_count < min_silence_blocks:
                is_sound[i - silence_count : i] = True
            silence_count = 0
    if silence_count > 0 and silence_count < min_silence_blocks:
        is_sound[num_blocks - silence_count : num_blocks] = True

    # Descarta ruídos de som muito curtos (ex: cliques de transientes)
    sound_count = 0
    for i in range(num_blocks):
        if is_sound[i]:
            sound_count += 1
        else:
            if sound_count > 0 and sound_count < min_sound_blocks:
                is_sound[i - sound_count : i] = False
            sound_count = 0
    if sound_count > 0 and sound_count < min_sound_blocks:
        is_sound[num_blocks - sound_count : num_blocks] = False

    # Junta os índices de samples a serem mantidos
    keep_indices = []
    for i in range(num_blocks):
        if is_sound[i]:
            keep_indices.extend(range(i * block_len, (i + 1) * block_len))

    # Adiciona o restante se o último bloco tinha som
    if is_sound[-1] and data.shape[0] > num_blocks * block_len:
        keep_indices.extend(range(num_blocks * block_len, data.shape[0]))

    if len(keep_indices) == 0:
        # Se tudo for silêncio, exporta apenas 1 segundo do início para não dar erro de arquivo vazio na API
        fallback_len = min(data.shape[0], int(1.0 * samplerate))
        sf.write(out_path, data[:fallback_len], samplerate)
    else:
        sf.write(out_path, data[np.array(keep_indices)], samplerate)

    return out_path


def convert_to_mp3_128k(in_wav_path, out_mp3_path):
    """
    Tenta converter o arquivo WAV para MP3 128kbps usando ffmpeg.
    Retorna True se der certo, False se falhar.
    """
    try:
        # Usa subprocess.run para chamar ffmpeg
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_wav_path, "-codec:a", "libmp3lame", "-b:a", "128k", out_mp3_path],
            check=True, capture_output=True, timeout=60
        )
        return True
    except Exception:
        # Tenta sem especificar libmp3lame caso use outra versao do ffmpeg
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", in_wav_path, "-b:a", "128k", out_mp3_path],
                check=True, capture_output=True, timeout=60
            )
            return True
        except Exception:
            return False


if __name__ == "__main__":
    # teste rapido isolado: python audio_utils.py entrada.wav saida.wav
    import sys
    if len(sys.argv) < 3:
        print("Uso: python audio_utils.py entrada.wav saida.wav [segundos]")
        sys.exit(1)
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8
    path, start, dur = extract_best_segment(sys.argv[1], sys.argv[2], segment_seconds=seconds)
    print(f"Trecho extraido: {path}")
    print(f"Começa em {start:.1f}s, dura {dur:.1f}s")
