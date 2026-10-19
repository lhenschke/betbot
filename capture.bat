@echo off
rem Opens Chrome and records one bet you place by hand, to confirm how the site works.
cd /d "%~dp0"
set BOOK=%1
if "%BOOK%"=="" set BOOK=OZ
.venv\Scripts\python -m betbot capture %BOOK%
pause
