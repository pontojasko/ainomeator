<p align="center">
  <img src="ainomeator_logo.png" alt="AiNOMEATOR logo" width="240" />
</p>

# AiNOMEATOR

Automatically identifies the primary instrument of each track in Reaper using AI (Gemini + Local Models), then applies names, colors, and icons. AiNOMEATOR keeps your DAW fully responsive by offloading the heavy lifting to background processing.

## Getting Started

Follow these steps to get AiNOMEATOR running in your Reaper environment.

### Prerequisites

- **Reaper** installed and configured.
- **Python 3.9+** installed and added to your system PATH.
- **Gemini API Key** from Google AI Studio.
- *(Optional)* SWS Extension for color synchronization.

### Installation

**Option A: ReaPack (Recommended)**
1. In Reaper, go to **Extensions > ReaPack > Manage repositories**.
2. Click **Import a repository** and paste the following URL:
   ```text
   https://raw.githubusercontent.com/pontojasko/ReaperAiNOMEATOR/main/index.xml
   ```
3. Click **OK**, then **Synchronize packages**.
4. Search for **AiNOMEATOR** in the ReaPack browser and install it.

**Option B: Manual Installation**
1. Clone or download this repository to a local folder.
2. Add `AiNOMEATOR.lua` to your Reaper Actions list (**Actions > Show action list > New action > Load ReaScript**).

### Configuration

You must configure your Python environment and API key before running the script.

1. Open your Reaper resource directory (**Options > Show REAPER resource path**).
2. Navigate to the `Scripts/AiNOMEATOR/` folder.
3. Run `setup.bat`. This will create a virtual environment (`venv`) and install all required dependencies.
4. Open the generated `.env` file and add your Gemini API key:

```env
GEMINI_API_KEY=your_api_key_here
```

> [!WARNING]
> The API key **must** be configured in the `.env` file. The option to input the key directly via the Reaper GUI has been removed to streamline the interface and improve security.

## Usage

Once installed and configured, run the **AiNOMEATOR** script from your Reaper Actions list.

### Best Practices

To get the most accurate and fastest results, we strongly recommend the following settings in the GUI:

- **Analysis Backend**: Select **Hybrid Heuristic**. This is by far the most accurate mode. It runs a local CNN14 (PANNs) model and cloud Gemini in parallel. It uses PANNs to capture physical spectral characteristics (like low frequencies and transients) and Gemini to interpret semantic context, merging them with a smart Arbiter and DSP checks.
- **Analysis Mode**: Use **Fast** for a lightweight 128kbps MP3 consisting of energy peak segments. Use **Detailed** (WAV) only for highly complex arrangements.
- **Sort Tracks**: Enable this to automatically group and sort your tracks by instrument family. Guitars and acoustic guitars are glued together at the top, followed by Keys, Synths, Strings, Brass, Bass, Drums, and Vocals.
- **Parallel Tracks**: Keep the thread count low (`1` or `2`) to avoid Gemini rate limits.

## Architecture & Features

The recommended **Hybrid Heuristic** backend relies on a triple-layer logic to prevent AI hallucinations and misclassifications:

1. **Parallel Execution Layer**: Both CNN14 and Gemini run concurrently, providing both spectral and semantic classification models in memory before making a decision.
2. **Conflict Arbiter**: 
   - *Rhythmic Priority*: If CNN14 detects a vocal but Gemini detects a shaker, the Arbiter overrides to shaker (Gemini excels at identifying high-frequency fricatives).
   - *Bass Transient*: If Gemini detects a piano but CNN14 detects bass or strings, it is classified as a bass (CNN14 recognizes low-frequency bodies better).
3. **DSP Sanity Filter**: Runs FFT and envelope checks locally.
   - Blocks vocal/piano tags if the main energy concentration is below 100Hz, forcing a bass/kick classification.
   - Forces a percussion classification if the sound has abrupt decays and no sustain.

### Audio Processing

Audio is locally converted to mono, peak-normalized, reduced to a higher-energy segment, and resampled to 24 kHz (or 16/32 kHz depending on the local model) before any AI processing occurs. This ensures low latency, reduced costs, and minimal context noise.

## Troubleshooting

> [!NOTE]
> If Reaper reports no results, ensure that `setup.bat` was run, the `.env` file exists with your key, and Python is accessible in your PATH.

- **503 / 429 Errors**: Gemini might return temporary rate limit errors. Reduce the parallel threads setting in the GUI.
- **Invalid Python Path**: Ensure you restart Reaper or your computer after adding Python to your system PATH.
