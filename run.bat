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
  rem First run: a few questions (books, what to bet, stake, logins) write config.yaml.
  .venv\Scripts\python -m betbot setup
  if not exist config.yaml (
    echo Setup didn't finish. Double-click run.bat to try again.
    pause
    exit /b
  )
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
