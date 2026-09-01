@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  Passing Analyzer - full automatic pipeline launcher
rem
rem  Double-click   : ask for the session folder, then run the pipeline
rem  Drag-and-drop  : drop a session folder onto this file to run it
rem  Command line   : this.bat <session folder> [run_pipeline.py options...]
rem
rem  Options handled by this launcher:
rem    --no-setup   skip the first-run setup / environment check
rem  Every other option is passed to run_pipeline.py as-is, e.g.
rem    this.bat E:\data\260701\ID1 --view rear --organize-id 1
rem ============================================================

cd /d "%~dp0"

set "APP_NAME=Passing Analyzer - Full Auto Pipeline"
set "PY=portable\python\python.exe"
set "VENV_PY=.venv\Scripts\python.exe"
set "SKIP_SETUP=0"
set "SESSION="
set "EXTRA="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--no-setup" (
    set "SKIP_SETUP=1"
    shift
    goto parse
)
if not defined SESSION if exist "%~1\" (
    set "SESSION=%~1"
    shift
    goto parse
)
set "EXTRA=!EXTRA! "%~1""
shift
goto parse

:parsed
echo.
echo === %APP_NAME% ===

if not defined SESSION call :ask_session

rem UTF-8 comes after the prompt on purpose: chcp breaks a later set /p.
chcp 65001 > nul
set "PYTHONUTF8=1"
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"

if not defined SESSION (
    echo [ERROR] No session folder was given.
    echo   Usage: this.bat ^<session folder^> [options...]
    echo.
    pause
    exit /b 1
)

if not exist "%SESSION%\" (
    echo [ERROR] Session folder was not found:
    echo   %SESSION%
    echo.
    pause
    exit /b 1
)

call :ensure_python || exit /b 1
if "%SKIP_SETUP%"=="0" (
    call :ensure_yolo_model || exit /b 1
    call :ensure_ffmpeg
)
goto run

:ask_session
echo.
echo Drop the session folder onto this window, or type its full path.
echo   The folder holds GOPRO\*.MP4 and LiDAR\*.pcap  (example: E:\data\260701\ID1)
echo.
set /p "SESSION=Session folder: "
if not defined SESSION exit /b 0
for /f "delims=" %%I in ("!SESSION!") do set "SESSION=%%~I"
exit /b 0

:ensure_python
if exist "%PY%" (
    call :verify_python "%PY%"
    if not errorlevel 1 exit /b 0
)

if exist "%VENV_PY%" (
    call :verify_python "%VENV_PY%"
    if not errorlevel 1 (
        set "PY=%VENV_PY%"
        exit /b 0
    )
)

if "%SKIP_SETUP%"=="1" (
    echo [ERROR] No usable Python runtime was found and --no-setup was given.
    echo   Run the GUI launcher once to create portable\python.
    echo.
    pause
    exit /b 1
)

echo.
echo [setup] A usable Python runtime was not found.
echo [setup] Installing Python and the required libraries into portable\python.
echo [setup] This can take several minutes and requires internet access.
echo.

if not exist "prepare_portable_python.ps1" (
    echo [ERROR] prepare_portable_python.ps1 was not found.
    echo Re-clone the repository or restore the setup files.
    pause
    exit /b 1
)

set "SETUP_FORCE="
if exist "portable\python\" set "SETUP_FORCE=-Force"

if defined TORCH_INDEX_URL (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_portable_python.ps1" %SETUP_FORCE% -TorchIndexUrl "%TORCH_INDEX_URL%"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_portable_python.ps1" %SETUP_FORCE%
)

if errorlevel 1 (
    echo.
    echo [ERROR] Portable Python setup failed.
    pause
    exit /b 1
)

set "PY=portable\python\python.exe"
call :verify_python "%PY%"
if errorlevel 1 (
    echo [ERROR] Setup finished but the required libraries are still unavailable.
    pause
    exit /b 1
)
exit /b 0

:verify_python
"%~1" -c "import sys, cv2, numpy, torch, ultralytics" > nul 2>&1
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

:ensure_ffmpeg
if exist "tools\ffmpeg.exe" exit /b 0

where ffmpeg > nul 2>&1
if not errorlevel 1 exit /b 0

echo.
echo [setup] System FFmpeg is not installed.
echo [setup] Installing bundled FFmpeg into tools\ for clip extraction.
if not exist "prepare_ffmpeg_tools.ps1" (
    echo [WARN] prepare_ffmpeg_tools.ps1 was not found. Clip extraction may be unavailable.
    exit /b 0
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_ffmpeg_tools.ps1"
if errorlevel 1 (
    echo [WARN] FFmpeg setup failed. Clip extraction may be unavailable.
)
exit /b 0

:run
set "OUT_DIR=%SESSION%\out_auto"
echo.
echo === Full auto pipeline start ===
echo   python : %PY%
echo   session: %SESSION%
if defined EXTRA echo   options:!EXTRA!
echo   output : %OUT_DIR%  (unless --out is given)
echo.
echo   Detection to final Excel takes a long time. Keep this window open.
echo.

"%PY%" run_pipeline.py "%SESSION%" !EXTRA!
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo [FAILED] Pipeline stopped with exit code %RESULT%.
    echo   Report: %OUT_DIR%\pipeline_report.txt
    echo.
    pause
    exit /b %RESULT%
)

echo [DONE] Results:
echo   %OUT_DIR%\final\        final Excel
echo   %OUT_DIR%\report.html   viewer report
echo   %OUT_DIR%\pipeline_report.txt
echo.

if exist "%OUT_DIR%\report.html" (
    choice /C YN /N /M "Open report.html now? [Y/N]: "
    if not errorlevel 2 start "" "%OUT_DIR%\report.html"
)

echo.
pause
exit /b 0
