@echo off
rem BetBot launcher for Windows. First run sets everything up.
cd /d "%~dp0"

if not exist .venv (
  echo Setting up Python environment...
  py -3.12 -m venv .venv 2>/dev/null || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -e .
)
if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo Created config.yaml - set your account label, then run:  set-credentials.bat
  notepad config.yaml
  pause
  exit /b
)

start "" http://127.0.0.1:8765
.venv\Scripts\python -m betbot run %*
pause
