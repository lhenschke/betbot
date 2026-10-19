@echo off
rem BetBot launcher for Windows. First run sets everything up.
cd /d "%~dp0"

if not exist .venv (
  echo Setting up Python environment...
  python -m venv .venv || py -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
)
rem Installs anything new or missing (quick when nothing changed).
.venv\Scripts\python -m pip install -q -e .
if errorlevel 1 (
  echo Installing packages failed - see the messages above.
  pause
  exit /b
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
