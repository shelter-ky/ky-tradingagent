@echo off
setlocal
cd /d "%~dp0"
python -X utf8 serve.py
if errorlevel 1 pause
