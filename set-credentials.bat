@echo off
rem Saves your sportsbook login in Windows Credential Manager (not in any file).
cd /d "%~dp0"
set BOOK=%1
if "%BOOK%"=="" set /p BOOK=Which sportsbook? Type OZ or MyB and press Enter: 
if "%BOOK%"=="" set BOOK=OZ
.venv\Scripts\python -m betbot set-credentials %BOOK%
pause
