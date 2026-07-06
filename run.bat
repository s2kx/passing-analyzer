@echo off
chcp 65001 > nul
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  Overtaking detector launcher (Windows / Python 3.13)
rem
rem  Double-click : choose a video in the file dialog
rem  Drag-and-drop: drop a video file onto run.bat
rem  Command line : run.bat input.mp4 [front^|rear] [options...]
rem  Environment  : run.bat --check
rem ============================================================

cd /d "%~dp0"
rem --- Prefer bundled portable Python (USB deployment); fall back to .venv (dev PC). ---
set "PY=portable\python\python.exe"

if not exist "%PY%" (
    if exist ".venv\Scripts\python.exe" (
        set "PY=.venv\Scripts\python.exe"
    ) else (
        echo [ERROR] Python was not found.
        echo Neither portable\python\python.exe nor .venv\Scripts\python.exe exists.
        echo Run setup.bat to create .venv, or copy the bundled 'portable' folder.
        echo.
        pause
        exit /b 1
    )
)

if /i "%~1"=="--check" goto check

rem --- Resolve input video. With no argument, show a Windows file picker. ---
set "VIDEO=%~1"
if not defined VIDEO (
    for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = 'Select GoPro video'; $d.Filter = 'Video files|*.mp4;*.mov;*.avi;*.mkv|All files|*.*'; if ($d.ShowDialog() -eq 'OK') { [Console]::Write($d.FileName) }"`) do set "VIDEO=%%I"
)

if not defined VIDEO (
    echo [CANCELLED] No video was selected.
    timeout /t 2 > nul
    exit /b 1
)
if not exist "%VIDEO%" (
    echo [ERROR] Video was not found:
    echo   %VIDEO%
    echo.
    pause
    exit /b 1
)

rem --- Camera direction can be the second argument or an interactive choice. ---
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

rem --- Preserve optional arguments after video and optional view. ---
shift
if /i "%~1"=="front" shift
if /i "%~1"=="rear" shift
set "EXTRA="
:collect
if "%~1"=="" goto run
set "EXTRA=!EXTRA! "%~1""
shift
goto collect

:run
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
"%PY%" -c "import sys, cv2, numpy, torch, ultralytics; assert (3, 11) <= sys.version_info[:2] <= (3, 13), sys.version; print('Python:', sys.version.split()[0]); print('OpenCV:', cv2.__version__); print('Ultralytics:', ultralytics.__version__); print('PyTorch:', torch.__version__); print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
if errorlevel 1 (
    echo [FAILED] Environment check failed.
    exit /b 1
)
echo [OK] run.bat is ready.
exit /b 0
