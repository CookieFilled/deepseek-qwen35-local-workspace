@echo off
setlocal enabledelayedexpansion

echo === DeepSeek-V4-Pro / Qwen3.5 Local Studio Setup (Windows) ===

where python >nul 2>nul
if errorlevel 1 (
    echo [Error] Python 3.10+ not found on PATH. Install Python first.
    exit /b 1
)

if not exist ".venv" (
    echo [Setup] Creating virtual environment (.venv)...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip

if "%~1"=="--minimal" (
    echo [Setup] Installing core dependencies only ^(requirements.txt^)...
    pip install -r requirements.txt
) else (
    echo [Setup] Installing full dependencies ^(requirements-full.txt^)...
    pip install -r requirements-full.txt
    echo [Setup] Installing Playwright Chromium browser...
    python -m playwright install chromium
)

if not exist ".env" (
    copy .env.example .env >nul
    echo [Setup] Created .env from .env.example
)

echo.
echo === Setup Complete ===
echo   Gemini-only mode ^(no model download^): start.bat --provider gemini --gemini-key YOUR_KEY
echo   Local GGUF mode ^(downloads once^):     start.bat --provider local
echo.
endlocal
