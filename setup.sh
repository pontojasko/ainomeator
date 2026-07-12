#!/bin/bash

echo "=========================================================="
echo "  AiNOMEATOR CLI - Environment Setup"
echo "=========================================================="
echo ""

# 1. Verify Python installation
echo "[*] Checking Python installation..."
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    # Make sure it's Python 3
    PY_VERSION=$(python -c 'import sys; print(sys.version_info[0])' 2>/dev/null)
    if [ "$PY_VERSION" = "3" ]; then
        PYTHON_CMD="python"
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] Python 3 was not found in your system PATH."
    echo "        Please install Python 3.9+ and ensure it is accessible."
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

# 2. Create Virtual Environment
if [ -d "venv" ]; then
    echo "[*] Virtual environment (venv) already exists. Skipping creation."
else
    echo "[*] Creating Python virtual environment (venv)..."
    $PYTHON_CMD -m venv venv
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to create virtual environment."
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo "[SUCCESS] Virtual environment created successfully."
fi
echo ""

# 3. Activate Virtual Environment & Install Dependencies
echo "[*] Activating virtual environment..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "[ERROR] Failed to locate activation script (venv/bin/activate)."
    read -p "Press Enter to exit..."
    exit 1
fi

echo "[*] Installing dependencies from src/requirements.txt..."
echo "    This may take a few minutes (installing PyTorch, PANNs, and Gemini)..."
python -m pip install --upgrade pip >/dev/null 2>&1
pip install -r src/requirements.txt
if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to install dependencies."
    read -p "Press Enter to exit..."
    exit 1
fi
echo "[SUCCESS] Dependencies installed successfully."
echo ""

# 4. Setup environment variables file (.env)
if [ -f ".env" ]; then
    echo "[*] Configuration file (.env) already exists. Keeping current setup."
else
    echo "[*] Creating configuration file (.env)..."
    echo "GEMINI_API_KEY=your_gemini_api_key_here" > .env
    echo "[SUCCESS] File .env created."
    echo ""
    echo "[IMPORTANT] Please open the \".env\" file in your project root and replace"
    echo "            \"your_gemini_api_key_here\" with your actual Gemini API Key."
    echo "            Get a free key here: https://aistudio.google.com/apikey"
fi
echo ""

echo "=========================================================="
echo "  Setup Completed Successfully!"
echo "=========================================================="
echo "  Next Steps:"
echo "  1. Configure your API key in the \".env\" file."
echo "  2. Open Reaper and run the \"AiNOMEATOR.lua\" script."
echo "=========================================================="
echo ""
