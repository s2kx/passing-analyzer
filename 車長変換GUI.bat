@echo off
setlocal EnableExtensions

rem ============================================================
rem  Passing Analyzer - car length conversion GUI launcher
rem
rem  Launches AutoAnalysis\length_gui.py: cut vehicle photos from
rem  the event clips, estimate each car's length with ChatGPT, and
rem  convert the relative speed to (p3 + car length) * 3.6 / time.
rem  The GUI also lets you check and correct the lengths by hand.
rem
rem  First-run setup mirrors the other launchers: portable Python
rem  and the YOLO model are prepared when missing.
rem  NOTE: keep this file pure ASCII. cmd.exe parses batch files
rem  with the ANSI codepage and multibyte text breaks the parser.
rem ============================================================

cd /d "%~dp0"
chcp 65001 > nul

set "PY=portable\python\python.exe"
set "PYW=portable\python\pythonw.exe"
set "PYTHONUTF8=1"
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"

echo.
echo === Passing Analyzer - Car Length Conversion GUI ===

call :ensure_python || exit /b 1
call :ensure_yolo_model || exit /b 1

if not exist "AutoAnalysis\.env" (
    echo [WARN] AutoAnalysis\.env was not found.
    echo        Stage 2 ^(ChatGPT^) needs OPENAI_API_KEY=sk-... in that file.
)

"%PY%" AutoAnalysis\length_gui.py --check
if errorlevel 1 (
    echo [ERROR] Car length GUI startup check failed.
    pause
    exit /b 1
)

echo [start] Launching the car length conversion GUI...
start "Passing Analyzer Car Length" "%PYW%" AutoAnalysis\length_gui.py
exit /b 0

:ensure_python
if exist "%PY%" (
    call :verify_python
    if not errorlevel 1 exit /b 0
    echo.
    echo [setup] Existing portable Python is incomplete or broken.
    echo [setup] Recreating portable\python.
)

echo.
echo [setup] Installing Python and the required libraries into portable\python.
echo [setup] This can take several minutes and requires internet access.
echo.

if not exist "setup\prepare_portable_python.ps1" (
    echo [ERROR] setup\prepare_portable_python.ps1 was not found.
    echo Re-clone the repository or restore the setup files.
    pause
    exit /b 1
)

set "SETUP_FORCE="
if exist "portable\python\" set "SETUP_FORCE=-Force"

if defined TORCH_INDEX_URL (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\setup\prepare_portable_python.ps1" %SETUP_FORCE% -TorchIndexUrl "%TORCH_INDEX_URL%"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\setup\prepare_portable_python.ps1" %SETUP_FORCE%
)

if errorlevel 1 (
    echo.
    echo [ERROR] Portable Python setup failed.
    pause
    exit /b 1
)

call :verify_python
if errorlevel 1 (
    echo [ERROR] Setup finished but the required libraries are still unavailable.
    pause
    exit /b 1
)
exit /b 0

:verify_python
"%PY%" -c "import sys, cv2, numpy, torch, ultralytics, webview" > nul 2>&1
exit /b %ERRORLEVEL%

:ensure_yolo_model
if exist "yolov8n.pt" exit /b 0

echo.
echo [setup] YOLO model yolov8n.pt was not found.
echo [setup] Downloading it through Ultralytics...
"%PY%" -c "from ultralytics import YOLO; YOLO('yolov8n.pt'); print('YOLO model is ready')"
if errorlevel 1 (
    echo [ERROR] Failed to prepare yolov8n.pt.
    pause
    exit /b 1
)

if not exist "yolov8n.pt" (
    echo [ERROR] yolov8n.pt was not created in the project folder.
    pause
    exit /b 1
)
exit /b 0
