@echo off
rem Opens Chrome and records what a sportsbook site does while you place one small bet by hand.
cd /d "%~dp0"
set BOOK=%1
if "%BOOK%"=="" set /p BOOK=Which sportsbook? Type OZ or MYB and press Enter: 
if "%BOOK%"=="" set BOOK=OZ
.venv\Scripts\python -m betbot capture %BOOK%
pause
