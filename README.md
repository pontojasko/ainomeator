<p align="center">
  <img src="ainomeator_logo.png" alt="AiNOMEATOR logo" width="240" />
</p>

# AiNOMEATOR

Automatically identifies the primary instrument of each track in Reaper using AI (Gemini + Local Models), then applies names, colors, and icons — keeping your DAW fully responsive with background processing.

---

## Screenshots & Demos

<p align="center">
  <img src="screenshots/script-window.png" alt="AiNOMEATOR script window in Reaper" width="320" />
  <br />
  <em>Script GUI — configuration, color prompt, and model options</em>
</p>

<p align="center">
  <img src="screenshots/desktop.gif" alt="AiNOMEATOR running in Reaper" width="720" />
  <br />
  <em>Demonstration — names, colors, and icons applied automatically in Reaper</em>
</p>

---

## 🚀 Quick Start: Installation

### Option A — ReaPack Installation (Recommended)

1. Make sure [ReaPack](https://reapack.com/) is installed in Reaper.
2. Go to **Extensions > ReaPack > Manage repositories**.
3. Click **Import repositories** (or right-click the list > **Import a repository**).
4. Paste the following URL:
   ```
   https://raw.githubusercontent.com/pontojasko/ReaperAiNOMEATOR/main/index.xml
   ```
5. Click **OK** and then **Synchronize packages**.
6. Search for **AiNOMEATOR** in the ReaPack browser and install `AiNOMEATOR.lua` (and optionally `AiNOMEATOR_sws_sync.lua`).
7. **Configure the Python Backend**:
   - Open your Reaper resource directory (**Options > Show REAPER resource path**).
   - Go to `Scripts/AiNOMEATOR/`.
   - Run `setup.bat` (this creates a virtual environment `venv` and installs all dependencies).
   - Open the generated `.env` file and paste your Gemini API key:
     ```env
     GEMINI_API_KEY=your_api_key_here
     ```
     *(Alternatively, you can paste the API key directly into the script's GUI inside Reaper).*

### Option B — Manual Installation

1. Clone or download this repository to a local folder (e.g. `C:\reaper-ainomeator`).
2. Run `setup.bat` in the folder to initialize the virtual environment and install dependencies.
3. Add `AiNOMEATOR.lua` to your Reaper Actions list (**Actions > Show action list > New action > Load ReaScript**).
4. Configure your Gemini API key in the generated `.env` file or in the script's GUI.

---

## 💡 How to Use & Best Practices

To get the most accurate and fastest results, follow these instructions:

### 1. Choose the Best Backend
In the **Analysis Backend** setting, select the engine:
*   ⭐ **Hybrid Heuristic (Recommended)**: This is **by far the best and most accurate mode**. It runs the local CNN14 (PANNs) model and cloud Gemini Flash-Lite in parallel. It uses PANNs to capture the physical spectral characteristics of the sound (such as low frequencies and transients) and Gemini to interpret the semantic context, joining them with a smart Arbiter and DSP checks.
*   **Gemini (Cloud)**: Good for general purpose semantic descriptions, but might struggle with sub-basses or short transients.
*   **YamNet (Local)**: Completely local Google model. Fast, but less detailed.
*   **PANNs (Local)**: Wavelet/acoustic-based model. Excellent for standard acoustic instruments (drums, brass, strings).
*   **Essentia (Local)**: Music Technology Group model (requires WSL/Linux/Mac for python bindings).

### 2. Configure GUI Settings
*   **Only selected (Apenas sel.)**: If checked, only selected tracks will be processed. Perfect for large sessions where you only want to rename new stems.
*   **Sort tracks (Ordenar inst.)**: If checked, it will automatically group and sort your tracks by instrument family at the end of the analysis:
    1.  *Guitars & Acoustic Guitars (Violões)* (placed at the top, glued together).
    2.  *Keyboards & Pianos*.
    3.  *Synths*.
    4.  *Strings*.
    5.  *Brass & Woodwinds*.
    6.  *Bass*.
    7.  *Drums & Percussion*.
    8.  *Vocals*.
    9.  *Folders, Effects and Others* (placed at the bottom).
*   **Analysis Mode**:
    *   *Fast*: Downmixes to a very lightweight 128kbps MP3 consisting of 3 energy peak segments (total 12 seconds). Fast upload and low cost.
    *   *Detailed*: Sends a high-fidelity WAV segment. Recommended for complex arrangements.
*   **Parallel tracks / CPU**: Set the number of threads for parallel analysis. If running on a slow connection, set it to `1` (default) or `2` to avoid Gemini rate limits.

---

## 🛠️ Hybrid Architecture & DSP Sanity

The recommended **Hybrid Heuristic** backend relies on a triple-layer logic to prevent AI hallucinations:

```
[Audio Input] ──► [Mono & Peak Normalization] ──► [ThreadPoolExecutor]
                                                        │
                                    ┌───────────────────┴───────────────────┐
                                    ▼                                       ▼
                             [CNN14 (Local)]                         [Gemini (Cloud)]
                                    │                                       │
                                    └───────────────────┬───────────────────┘
                                                        ▼
                                             [Conflict Arbiter]
                                                        │
                                    ┌───────────────────┴───────────────────┐
                                    ▼                                       ▼
                             [Consensus Check]                      [Logical Rules]
                                                                  (Rhythmic & Sub-Bass)
                                                        │
                                                        ▼
                                             [DSP Sanity Filter]
                                            (FFT & Envelope checks)
                                                        │
                                                        ▼
                                               [Final Track Info]
```

1.  **Parallel Execution Layer**: Both CNN14 and Gemini run concurrently. You always get both spectral and semantic classification models in memory before making a decision.
2.  **Conflict Arbiter**:
    *   *Rhythmic Priority Rule*: If CNN14 detects "vocal" but Gemini detects "shaker" (or percussion), the Arbiter overrides it to percussion/shaker. (Gemini isn't fooled by high-frequency rhythmic sibilance/fricatives).
    *   *Bass Transient Rule*: If Gemini detects "piano" but CNN14 detects "bass" or "strings", it is named "Baixo Pizzicato" (CNN14 understands low-frequency body better).
    *   *Absolute Consensus*: If both return compatible families (e.g. keyboard and synth), the result is automatically accepted, choosing Gemini's descriptive nomenclature.
3.  **Sanity Checker (DSP simples)**:
    *   *Low Frequency Override*: If the main energy concentration is below 100Hz, vocal/piano tags are blocked and forced to bass/kick.
    *   *Percussive Override*: If the sound has abrupt decays and no sustain, it is forced to percussion (bateria).
    *   *Smart Drum Icons*: Assigns `drums.png` for acoustic drum tracks and `drumbox.png` for electronic/sampler drum machines.

---

## 📂 File Architecture

```text
reaper-ainomeator/
├── AiNOMEATOR.lua              # Reaper integration (GUI + result application)
├── AiNOMEATOR_sws_sync.lua     # ReaScript shortcut for SWS color sync
├── batch_rename.py             # Batch processing and parallel execution
├── classify_track.py           # Classification wrapper with Gemini
├── audio_utils.py              # Mono downmix, peak normalization, snippet extraction
├── sync_sws_colors.py          # Syncs palette with SWS Auto Color
├── setup.bat                   # Creates virtual env and .env file
├── test_single.bat             # Single-audio command-line test
├── test_batch.bat              # Batch command-line test
├── reaper_ai_track_namer_colors.ini  # Default color palette
└── ainomeator_logo.png         # Project logo
```

---

## ❓ Troubleshooting & Support

*   **TypeError / NoneType in confidence**: Fixed in recent updates. Make sure you pull/sync the latest commits.
*   **503 / 429 rate limits**: Gemini might return temporary errors under high concurrent thread counts. Keep the parallel workers setting low in Reaper (usually `1` or `2`).
*   **Reaper doesn't rename**:
    1.  Ensure you have run `setup.bat` successfully.
    2.  Check that your API key is correctly saved in the GUI or `.env`.
    3.  Make sure Python 3.9+ is added to your OS system PATH.

---
