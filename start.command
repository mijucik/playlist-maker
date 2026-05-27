#!/bin/bash
# Playlist Maker — macOS launcher
# Double-click this file in Finder to set everything up and open the app.

# Always run from the folder where this script lives
cd "$(dirname "$0")"

echo "======================================"
echo "  Playlist Maker"
echo "======================================"
echo ""

# ── 1. Check for Python 3 ─────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 is not installed."
    echo ""
    echo "  Install it from:  https://www.python.org/downloads/"
    echo "  Or with Homebrew: brew install python"
    echo ""
    read -rp "Press Enter to close..."
    exit 1
fi

echo "Python $(python3 --version | cut -d' ' -f2) found."

# ── 2. Create virtual environment if needed ────────────────────────────────
if [ ! -d "venv" ]; then
    echo ""
    echo "Setting up virtual environment (first run only)..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo ""
        echo "ERROR: Could not create virtual environment."
        read -rp "Press Enter to close..."
        exit 1
    fi
    echo "Virtual environment created."
fi

# ── 3. Activate the virtual environment ───────────────────────────────────
source venv/bin/activate

# ── 4. Upgrade pip quietly, then install dependencies ─────────────────────
echo ""
echo "Checking dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Could not install dependencies."
    echo "Check your internet connection and try again."
    read -rp "Press Enter to close..."
    exit 1
fi
echo "Dependencies ready."

# ── 5. Launch the web app ─────────────────────────────────────────────────
echo ""
echo "Starting Playlist Maker..."
echo ""
python3 web.py

# If web.py exits with an error, keep the window open so the user can read it
STATUS=$?
if [ $STATUS -ne 0 ]; then
    echo ""
    echo "The app stopped with an error (exit code $STATUS)."
    echo "Scroll up to read what went wrong."
    read -rp "Press Enter to close..."
fi
