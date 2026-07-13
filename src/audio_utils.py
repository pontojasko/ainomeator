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
    Usa ffmpeg para fazer o downsample de 44.1kHz -> 24kHz. Isso e rapido
    e produz audio de qualidade adequada para classificacao de instrumento.

Funcao _resample() (resample em memoria, sem ffmpeg):
    Cadeia de prioridade: soxr -> scipy.signal.resample_poly -> np.interp.
    Compartilhada com panns_classify.py e yamnet_classify.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

import numpy as np
import soundfile as sf


# ---------------------------------------------------------------------------
# Resample em memoria (compartilhado com panns_classify / yamnet_classify)
# ---------------------------------------------------------------------------

def resample(data: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    """Reamostra `data` (mono, float32) de `sr` para `target_sr`.

    Cadeia de prioridade (a primeira disponivel e usada):
    1. soxr   — filtro polifasico C++, ~2x mais rapido que np.interp, sem aliasing.
    2. scipy.signal.resample_poly — fallback com razao limitada (limit_denominator=256)
       para evitar arrays intermediarios gigantes.
    3. np.interp — ultimo recurso, sem dependencias extras.
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
        return resample_poly(data, frac.numerator, frac.denominator).astype(np.float32)
    except ImportError:
        pass

    duration = len(data) / sr
    n_target = max(1, int(round(duration * target_sr)))
    x_old = np.linspace(0, duration, num=len(data), endpoint=False)
    x_new = np.linspace(0, duration, num=n_target, endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


# ---------------------------------------------------------------------------
# Leitura de audio
# ---------------------------------------------------------------------------

def _read_audio(path: str) -> tuple[np.ndarray, int]:
    """Le o audio com soundfile. Se falhar, tenta converter com ffmpeg.

    Aplica conversao para mono e peak normalization.
    Levanta ValueError("absolute_silence") se o audio for silencio total.
    """
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

    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)

    max_val = np.max(np.abs(data))
    if max_val < 1e-6:
        raise ValueError("absolute_silence")
    data = data / max_val
    return data, sr


# ---------------------------------------------------------------------------
# Helper interno: calcula janela de busca
# ---------------------------------------------------------------------------

def _compute_search_window(
    total_frames: int,
    samplerate: int,
    search_start_seconds: Optional[float],
    search_duration_seconds: Optional[float],
) -> tuple[int, int]:
    """Retorna (window_start_frame, window_frames) respeitando os limites do arquivo.

    Args:
        total_frames: numero total de frames do audio carregado.
        samplerate: taxa de amostragem.
        search_start_seconds: inicio da janela de busca em segundos (ou None).
        search_duration_seconds: duracao da janela de busca em segundos (ou None).

    Returns:
        Tupla (window_start_frame, window_frames).
    """
    window_start_frame = 0
    if search_start_seconds is not None:
        window_start_frame = min(max(0, int(search_start_seconds * samplerate)), total_frames)

    if search_duration_seconds is not None:
        window_frames = min(int(search_duration_seconds * samplerate), total_frames - window_start_frame)
    else:
        window_frames = total_frames - window_start_frame

    return window_start_frame, max(0, window_frames)


# ---------------------------------------------------------------------------
# _best_energy_start: localiza o trecho de maior energia via cumsum
# ---------------------------------------------------------------------------

def _best_energy_start(mono: np.ndarray, segment_frames: int, hop_frames: int) -> int:
    """Retorna o indice de inicio do segmento de maior energia RMS no array mono.

    Usa cumsum para O(n) em vez de recalcular a soma do zero em cada posicao.
    """
    squared = mono.astype(np.float64) ** 2
    cumsum = np.cumsum(np.insert(squared, 0, 0.0))
    n_frames = mono.shape[0]
    starts = np.arange(0, n_frames - segment_frames + 1, hop_frames)
    if len(starts) == 0:
        return 0
    energies = cumsum[starts + segment_frames] - cumsum[starts]
    return int(starts[np.argmax(energies)])


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def extract_best_segment(
    audio_path: str,
    out_path: str,
    segment_seconds: float = 8,
    hop_seconds: float = 0.5,
    search_start_seconds: Optional[float] = None,
    search_duration_seconds: Optional[float] = None,
) -> tuple[str, float, float]:
    """Le o arquivo de audio, acha a janela de `segment_seconds` com maior
    energia media (RMS), e salva so essa janela em `out_path`.

    Se `search_start_seconds`/`search_duration_seconds` forem informados,
    a busca fica restrita a esse trecho do arquivo.

    Returns:
        (out_path, start_seconds, duration_seconds) — start/duration relativos
        ao arquivo INTEIRO (nao a janela de busca), para debug/log.
    """
    data, samplerate = _read_audio(audio_path)
    total_frames = data.shape[0]

    window_start_frame, window_frames = _compute_search_window(
        total_frames, samplerate, search_start_seconds, search_duration_seconds
    )
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
            mono = windowed.mean(axis=1)
            hop_frames = max(1, int(hop_seconds * samplerate))
            best_start_in_window = _best_energy_start(mono, segment_frames, hop_frames)
            segment = windowed[best_start_in_window:best_start_in_window + segment_frames]

    sf.write(out_path, segment, samplerate)
    absolute_start = (window_start_frame + best_start_in_window) / samplerate
    return out_path, absolute_start, segment.shape[0] / samplerate


def downmix_resample(
    in_path: str,
    out_path: str,
    target_sr: int = 24000,
    keep_stereo: bool = False,
) -> str:
    """Converte para mono + reduz a taxa de amostragem via ffmpeg.

    Gera um WAV menor/mais leve para mandar para a API.
    """
    ac_channels = "2" if keep_stereo else "1"
    cmd = ["ffmpeg", "-y", "-i", in_path, "-ac", ac_channels]
    if target_sr is not None:
        cmd.extend(["-ar", str(target_sr)])
    cmd.extend(["-c:a", "pcm_s16le", out_path])
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return out_path


def extract_three_peaks(
    audio_path: str,
    out_path: str,
    search_start_seconds: Optional[float] = None,
    search_duration_seconds: Optional[float] = None,
    segment_seconds: float = 4,
) -> tuple[str, float, float]:
    """Le o audio, extrai 3 trechos de segment_seconds dos picos de energia
    (comeco, meio, fim) e concatena em um unico arquivo WAV.
    """
    data, samplerate = _read_audio(audio_path)
    total_frames = data.shape[0]

    window_start_frame, window_frames = _compute_search_window(
        total_frames, samplerate, search_start_seconds, search_duration_seconds
    )
    windowed = data[window_start_frame:window_start_frame + window_frames]
    total_duration = window_frames / samplerate

    if total_duration <= segment_seconds * 3 or window_frames == 0:
        segment = windowed if window_frames > 0 else data
        sf.write(out_path, segment, samplerate)
        return out_path, 0, segment.shape[0] / samplerate

    part_frames = window_frames // 3
    seg_frames = int(segment_seconds * samplerate)
    hop_frames = int(0.5 * samplerate)

    segments = []
    for i in range(3):
        part_start = i * part_frames
        part_data = windowed[part_start:part_start + part_frames]

        if part_data.shape[0] <= seg_frames:
            segments.append(part_data)
        else:
            mono = part_data.mean(axis=1)
            best_start = _best_energy_start(mono, seg_frames, hop_frames)
            segments.append(part_data[best_start:best_start + seg_frames])

    concatenated = np.concatenate(segments, axis=0)
    sf.write(out_path, concatenated, samplerate)
    return out_path, 0, concatenated.shape[0] / samplerate


def convert_to_mp3_128k(in_wav_path: str, out_mp3_path: str) -> bool:
    """Converte o arquivo WAV para MP3 128kbps usando ffmpeg.

    Tenta com libmp3lame explicitamente; se falhar, tenta sem especificar o codec
    (ffmpeg usa o encoder padrao disponivel). Retorna True em caso de sucesso.
    """
    base_cmd = ["ffmpeg", "-y", "-i", in_wav_path, "-b:a", "128k", out_mp3_path]
    lame_cmd = ["ffmpeg", "-y", "-i", in_wav_path, "-codec:a", "libmp3lame", "-b:a", "128k", out_mp3_path]

    for cmd in (lame_cmd, base_cmd):
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            return True
        except Exception:
            continue
    return False


def analyze_dsp_properties(audio_path: str) -> dict[str, float]:
    """Analisa propriedades de DSP basicas do audio:
    1. Concentracao de energia abaixo de 100Hz via FFT.
    2. Proporcao de frames com amplitude abaixo de 10% do pico (transientes curtos).

    Usado como verificador de sanidade nos modos hibridos.

    Returns:
        {"low_freq_ratio": float, "low_energy_ratio": float}
    """
    try:
        data, sr = sf.read(audio_path, always_2d=True)
        y = data.mean(axis=1) if data.shape[1] > 1 else data.flatten()

        if len(y) == 0:
            return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}

        # 1. Concentracao de energia abaixo de 100Hz
        fft_vals = np.fft.rfft(y)
        fft_freqs = np.fft.rfftfreq(len(y), d=1 / sr)
        energy = np.abs(fft_vals) ** 2
        total_energy = np.sum(energy)
        low_freq_ratio = float(np.sum(energy[fft_freqs < 100]) / total_energy) if total_energy > 0 else 0.0

        # 2. Decaimentos abruptos (transientes rapidos sem sustain)
        frame_size = int(0.050 * sr)
        low_energy_ratio = 0.0
        if frame_size > 0 and len(y) >= frame_size:
            num_frames = len(y) // frame_size
            frames = y[:num_frames * frame_size].reshape((num_frames, frame_size))
            frame_max = np.max(np.abs(frames), axis=1)
            global_max = np.max(frame_max)
            if global_max > 0:
                low_energy_ratio = float(np.sum(frame_max < (0.1 * global_max)) / num_frames)

        return {"low_freq_ratio": low_freq_ratio, "low_energy_ratio": low_energy_ratio}

    except Exception as e:
        print(f"[DSP ERROR] Falha ao analisar propriedades DSP: {e}")
        return {"low_freq_ratio": 0.0, "low_energy_ratio": 0.0}


if __name__ == "__main__":
    # teste rapido isolado: python audio_utils.py entrada.wav saida.wav [segundos]
    import sys
    if len(sys.argv) < 3:
        print("Uso: python audio_utils.py entrada.wav saida.wav [segundos]")
        sys.exit(1)
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8
    path, start, dur = extract_best_segment(sys.argv[1], sys.argv[2], segment_seconds=seconds)
    print(f"Trecho extraido: {path}")
    print(f"Comeca em {start:.1f}s, dura {dur:.1f}s")
