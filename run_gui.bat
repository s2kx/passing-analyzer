@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

rem --- Prefer bundled portable Python (USB deployment); fall back to .venv (dev PC). ---
set "PY=portable\python\python.exe"
set "PYW=portable\python\pythonw.exe"

if not exist "%PY%" (
    if exist ".venv\Scripts\python.exe" (
        set "PY=.venv\Scripts\python.exe"
        set "PYW=.venv\Scripts\pythonw.exe"
    ) else (
        echo [ERROR] Python was not found.
        echo Neither portable\python\python.exe nor .venv\Scripts\python.exe exists.
        echo Run setup.bat to create .venv, or copy the bundled 'portable' folder.
        pause
        exit /b 1
    )
)

"%PY%" web_gui.py --check
if errorlevel 1 (
    echo [ERROR] GUI startup check failed.
    pause
    exit /b 1
)

if /i "%~1"=="--check" exit /b 0

start "Overtaking workflow" "%PYW%" web_gui.py
endlocal
