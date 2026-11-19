#!/usr/bin/env bash
set -euo pipefail

echo "=== DeepSeek-V4-Pro / Qwen3.5 Local Studio Setup (Linux / macOS) ==="

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[Error] python3 not found. Please install Python 3.10+ first."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "[Setup] Creating virtual environment (.venv)..."
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip

if [ "${1:-}" = "--minimal" ]; then
  echo "[Setup] Installing core dependencies only (requirements.txt)..."
  pip install -r requirements.txt
else
  echo "[Setup] Installing full dependencies (requirements-full.txt)..."
  pip install -r requirements-full.txt
  echo "[Setup] Installing Playwright Chromium browser..."
  python -m playwright install chromium || echo "[Warning] Playwright browser install skipped."
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[Setup] Created .env from .env.example"
fi

echo ""
echo "=== Setup Complete ==="
echo "  Gemini-only mode (no model download): ./start.sh --provider gemini --gemini-key YOUR_KEY"
echo "  Local GGUF mode (downloads once):     ./start.sh --provider local"
echo ""
