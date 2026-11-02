@echo off
rem Answer the setup questions again (books, what to bet, stake, logins). Replaces config.yaml.
cd /d "%~dp0"
.venv\Scripts\python -m betbot setup
pause
