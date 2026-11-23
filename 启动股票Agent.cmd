@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
if not exist ".venv\ky-v001-ready" (
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 goto failed
    type nul > ".venv\ky-v001-ready"
)
".venv\Scripts\python.exe" -m cli.main --ticker TQQQ
if errorlevel 1 goto failed
exit /b 0
:failed
echo Check the message above. Python 3.11 or newer is required.
pause
exit /b 1
