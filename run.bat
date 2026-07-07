@echo off
chcp 65001 > nul
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  Passing Analyzer single launcher
rem
rem  Double-click       : update check, first-run setup, launch GUI
rem  Drag-and-drop      : run detection for the dropped video
rem  Command line       : run.bat input.mp4 [front|rear] [options...]
rem  Environment check  : run.bat --check
rem  Skip update check  : run.bat --no-update
rem ============================================================

cd /d "%~dp0"

set "APP_NAME=Passing Analyzer"
set "PY=portable\python\python.exe"
set "PYW=portable\python\pythonw.exe"
set "SKIP_UPDATE=0"
set "CHECK_ONLY=0"

:parse_options
if /i "%~1"=="--no-update" (
    set "SKIP_UPDATE=1"
    shift
    goto parse_options
)

if /i "%~1"=="--check" (
    set "CHECK_ONLY=1"
    shift
    goto parse_options
)

echo.
echo === %APP_NAME% ===

if "%SKIP_UPDATE%"=="0" call :auto_update
call :ensure_runtime || exit /b 1
call :ensure_yolo_model || exit /b 1
call :ensure_ffmpeg

if "%CHECK_ONLY%"=="1" goto check
if "%~1"=="" goto gui

rem If the first argument is a video file, keep the old drag-and-drop/CLI flow.
if exist "%~1" goto detect

if /i "%~1"=="--gui" goto gui

echo [ERROR] Unknown argument:
echo   %~1
echo.
echo Usage:
echo   run.bat
echo   run.bat --check
echo   run.bat --no-update
echo   run.bat input.mp4 [front^|rear] [options...]
echo.
pause
exit /b 1

:auto_update
where git > nul 2>&1
if errorlevel 1 (
    echo [update] Git was not found. Skipping update check.
    exit /b 0
)

if not exist ".git\HEAD" (
    echo [update] Git repository was not found. Skipping update check.
    exit /b 0
)

git remote get-url origin > nul 2>&1
if errorlevel 1 (
    echo [update] origin remote is not configured. Skipping update check.
    exit /b 0
)

git diff --quiet > nul 2>&1
if errorlevel 1 (
    echo [update] Local changes exist. Skipping automatic update.
    exit /b 0
)

git diff --cached --quiet > nul 2>&1
if errorlevel 1 (
    echo [update] Staged local changes exist. Skipping automatic update.
    exit /b 0
)

echo [update] Checking GitHub updates...
git fetch --quiet origin
if errorlevel 1 (
    echo [update] Could not fetch updates. Continuing with the current version.
    exit /b 0
)

set "BEHIND=0"
for /f "usebackq delims=" %%I in (`git rev-list --count HEAD..@{u} 2^>nul`) do set "BEHIND=%%I"
if "%BEHIND%"=="0" (
    echo [update] Already up to date.
    exit /b 0
)

echo [update] Found %BEHIND% new commit(s). Updating...
git pull --ff-only
if errorlevel 1 (
    echo [update] Automatic update failed. Continuing with the current version.
    exit /b 0
)

echo [update] Update completed.
exit /b 0

:ensure_runtime
if exist "%PY%" (
    exit /b 0
)

echo.
echo [setup] Portable Python runtime was not found.
echo [setup] This appears to be the first startup on this PC.
echo [setup] Installing Python and required libraries into portable\python.
echo [setup] This can take several minutes and requires internet access.
echo.

if not exist "prepare_portable_python.ps1" (
    echo [ERROR] prepare_portable_python.ps1 was not found.
    echo Re-clone the repository or restore the setup files.
    pause
    exit /b 1
)

if defined TORCH_INDEX_URL (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_portable_python.ps1" -TorchIndexUrl "%TORCH_INDEX_URL%"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_portable_python.ps1"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Portable Python setup failed.
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [ERROR] portable\python\python.exe was still not found after setup.
    pause
    exit /b 1
)

exit /b 0

:ensure_yolo_model
if exist "yolov8n.pt" (
    exit /b 0
)

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
if exist "tools\ffmpeg.exe" (
    exit /b 0
)

where ffmpeg > nul 2>&1
if not errorlevel 1 (
    echo [setup] System FFmpeg was found. Bundled FFmpeg is not required.
    exit /b 0
)

echo.
echo [setup] FFmpeg was not found.
echo [setup] Installing bundled FFmpeg into tools\.
if not exist "prepare_ffmpeg_tools.ps1" (
    echo [WARN] prepare_ffmpeg_tools.ps1 was not found. Clip extraction may be unavailable.
    exit /b 0
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\prepare_ffmpeg_tools.ps1"
if errorlevel 1 (
    echo [WARN] FFmpeg setup failed. The app can run, but clip extraction may be unavailable.
    exit /b 0
)

exit /b 0

:gui
set "PYTHONUTF8=1"
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"

"%PY%" web_gui.py --check
if errorlevel 1 (
    echo [ERROR] GUI startup check failed.
    pause
    exit /b 1
)

echo [start] Launching GUI...
start "Passing Analyzer" "%PYW%" web_gui.py
exit /b 0

:detect
set "VIDEO=%~1"

rem Camera direction can be the second argument or an interactive choice.
set "VIEW="
if /i "%~2"=="front" set "VIEW=front"
if /i "%~2"=="rear"  set "VIEW=rear"

if not defined VIEW (
    echo.
    echo Camera direction:
    echo   F = front-facing GoPro
    echo   R = rear-facing GoPro
    choice /C FR /N /M "Select [F/R]: "
    if errorlevel 2 (
        set "VIEW=rear"
    ) else (
        set "VIEW=front"
    )
)

shift
if /i "%~1"=="front" shift
if /i "%~1"=="rear" shift
set "EXTRA="
:collect
if "%~1"=="" goto run_detection
set "EXTRA=!EXTRA! "%~1""
shift
goto collect

:run_detection
set "PYTHONUTF8=1"
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"
echo.
echo === Overtaking detection start ===
echo   input : %VIDEO%
echo   view  : %VIEW%
echo   preset: GPU auto, imgsz 416, batch 32, clip enabled
echo   output: out\
echo.

"%PY%" detect_overtaking.py "%VIDEO%" --view "%VIEW%" --clip --imgsz 416 --batch 32 !EXTRA!
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" (
    echo [FAILED] Detection stopped with exit code %RESULT%.
) else (
    echo [DONE] Results:
    echo   out\overtaking_events.csv
    echo   out\clips\
)
echo.
pause
exit /b %RESULT%

:check
set "PYTHONUTF8=1"
set "YOLO_CONFIG_DIR=%CD%\.ultralytics"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%"
"%PY%" -c "import sys, cv2, numpy, torch, ultralytics, webview; assert (3, 11) <= sys.version_info[:2] <= (3, 13), sys.version; print('Python:', sys.version.split()[0]); print('OpenCV:', cv2.__version__); print('Ultralytics:', ultralytics.__version__); print('PyTorch:', torch.__version__); print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
if errorlevel 1 (
    echo [FAILED] Environment check failed.
    exit /b 1
)
echo [OK] run.bat is ready.
exit /b 0
