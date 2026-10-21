@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.11+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
rem Chromium is the fallback when Google Chrome is not installed
.venv\Scripts\python -m playwright install chromium
if not exist config\credentials.yaml copy config\credentials.example.yaml config\credentials.yaml >nul
echo.
echo Done. Start with run.bat
pause
