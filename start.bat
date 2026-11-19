@echo off
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)
python run.py --open-browser %*
