@echo off
rem BetBot launcher for Windows. First run sets everything up; every start installs updates.
cd /d "%~dp0"

if not exist .venv (
  echo Setting up Python environment...
  python -m venv .venv || py -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
)
if not exist config.yaml (
  .venv\Scripts\python -m pip install -q -e .
  copy config.example.yaml config.yaml >nul
  echo Created config.yaml - set your account label, then run:  set-credentials.bat
  notepad config.yaml
  pause
  exit /b
)
start "" http://127.0.0.1:8765

:start
if exist .git (
  echo Checking GitHub for updates...
  git pull --ff-only || echo Could not update - running the current version.
)
rem Installs anything new or missing (quick when nothing changed).
.venv\Scripts\python -m pip install -q -e .
if errorlevel 1 (
  echo Installing packages failed - see the messages above.
  pause
  exit /b
)
.venv\Scripts\python -m betbot run %*
rem Exit code 3 means "a new version is available": update and start again.
if %errorlevel%==3 goto start
pause
