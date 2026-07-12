<div align="center">
  <img src="src/ainomeator_logo.png" alt="AiNOMEATOR" width="240"/>

  [![License](https://img.shields.io/github/license/pontojasko/ReaperAiNOMEATOR?style=flat-square)](LICENSE)
  [![Stars](https://img.shields.io/github/stars/pontojasko/ReaperAiNOMEATOR?style=flat-square)](https://github.com/pontojasko/ReaperAiNOMEATOR/stargazers)
  [![Issues](https://img.shields.io/github/issues/pontojasko/ReaperAiNOMEATOR?style=flat-square)](https://github.com/pontojasko/ReaperAiNOMEATOR/issues)

  **Have you ever had to export stems from your FL Studio or Ableton project into Reaper, only to find yourself dreading the tedious process of organizing and renaming dozens of messy tracks?**

  Automatically identify, rename, and colorize your Reaper tracks using AI

  [Getting Started](#getting-started) · [Architecture](#architecture--features) · [Report Bug](https://github.com/pontojasko/ReaperAiNOMEATOR/issues)

  <br />
  <img src="screenshots/desktop.gif" alt="AiNOMEATOR running in Reaper" width="720" />
  <br />
  <em>Demo — names, colors and icons applied automatically by AI</em>
</div>

---

## Overview

> [!WARNING]
> **Note:** This is an experimental project. The AI models under the hood can still make mistakes. We warmly welcome any suggestions, feedback, or pull requests to help improve the classification pipelines!

Exporting stems from modern DAWs or receiving poorly named tracks from clients usually means spending hours manually renaming, coloring, and organizing the session before you can even start mixing.

AI Nomeator offloads this heavy lifting to a background AI processor. By utilizing a hybrid model approach (combining local CNNs and cloud-based Gemini), it accurately identifies the instruments playing in each stem and automatically organizes your entire Reaper project.

A fully structured, color-coded, and properly named Reaper session ready for mixing in minutes, saving you hours of tedious administrative work.

---

## Getting Started
### Prerequisites

- **Python 3.9+**.
- **Free Gemini API Key**.
- *(Optional)* SWS Extension for color synchronization.

### Installation

1. Clone or download this repository to a local folder.
2. Add `AiNOMEATOR.lua` to your Reaper Actions list (**Actions > Show action list > New action > Load ReaScript**).
3. Run "setup.bat"

### Configuration

You must configure your API key if you want to run gemini or hybrid analysis.

```env
GEMINI_API_KEY=your_api_key_here
```
---

## Usage
### Quick Execution
When you launch `AiNOMEATOR.lua` in Reaper, you will be greeted by a simple, clean, and distraction-free interface:

<p align="center">
  <img src="screenshots/compactedmod.png" alt="AiNOMEATOR Compact UI" width="280" />
  <br />
  <em>Compact Mode</em>
</p>


- **LETS NOMEATE!**: Click the main button to instantly analyze, name, and color all tracks using your current baseline options.
- **EN / PT**: Toggle between English and Portuguese localization for the UI and generated names/colors.
- **Advanced Options [+]**: Click to expand the window and configure advanced features.

---

### Advanced Options

Expanding the settings panel allows you to customize the underlying AI models and performance options:

<p align="center">
  <img src="screenshots/advancedoptions.png" alt="AiNOMEATOR Advanced Options" width="280" valign="top" />
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="screenshots/analysis.png" alt="AiNOMEATOR Analysis Status" width="280" valign="top" />
  <br />
  <em>Left: Advanced Options Panel &nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;&nbsp; Right: Analysis Status</em>
</p>

- **Analysis Backend**: start with **PANNs** as your baseline. It is generally the most fast starting point. You can also test Gemini or a hybrid solution if you want to explore another/better results.
- **Analysis Mode**: use **Detailed**.
- **Parallel Tracks**: with Gemini, keep the thread count `1` to avoid rate limits .

---

## Troubleshooting

> [!NOTE]
> If Reaper reports no results, ensure that `setup.bat` was run, the `.env` file exists with your key, and Python is accessible in your PATH.

- **503 / 429 Errors**: Gemini might return temporary rate limit errors. Reduce the parallel threads setting in the GUI.
- **Invalid Python Path**: Ensure you restart Reaper or your computer after adding Python to your system PATH.
