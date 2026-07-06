@echo off
chcp 65001 > nul
setlocal EnableExtensions

rem Build a Python-bundled portable distribution folder.
rem Prerequisite: portable\python\python.exe contains the bundled runtime.

cd /d "%~dp0"

set "DIST=dist\OvertakingToolPortable"
set "RUNTIME=portable\python"
set "PAUSE_ON_EXIT=1"

if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"

if not exist "%RUNTIME%\python.exe" (
    echo [ERROR] Bundled Python was not found: %RUNTIME%\python.exe
    echo Put the portable Python runtime under portable\python first.
    echo The runtime must already include packages from requirements.txt.
    if "%PAUSE_ON_EXIT%"=="1" pause
    exit /b 1
)

echo.
echo === Building portable distribution ===
echo Output: %DIST%
echo.

if exist "%DIST%" (
    rmdir /s /q "%DIST%"
    if errorlevel 1 (
        echo [ERROR] Failed to remove previous %DIST%
        if "%PAUSE_ON_EXIT%"=="1" pause
        exit /b 1
    )
)

mkdir "%DIST%" || exit /b 1
mkdir "%DIST%\portable" || exit /b 1
mkdir "%DIST%\tools" || exit /b 1
mkdir "%DIST%\output" || exit /b 1

copy /y run_gui.bat "%DIST%\" > nul
copy /y run.bat "%DIST%\" > nul
copy /y web_gui.py "%DIST%\" > nul
copy /y detect_overtaking.py "%DIST%\" > nul
copy /y lidar_pcap.py "%DIST%\" > nul
copy /y gpmf_sync.py "%DIST%\" > nul
copy /y edit_csv.py "%DIST%\" > nul
copy /y extract_telemetry.py "%DIST%\" > nul
copy /y final_export.py "%DIST%\" > nul
copy /y requirements.txt "%DIST%\" > nul
copy /y README.md "%DIST%\" > nul
copy /y PORTABLE_DISTRIBUTION.md "%DIST%\" > nul

if exist "yolov8n.pt" (
    copy /y yolov8n.pt "%DIST%\" > nul
) else (
    echo [WARN] yolov8n.pt was not found. Add a YOLO model before distribution.
)

xcopy /e /i /y web_ui "%DIST%\web_ui" > nul
xcopy /e /i /y "%RUNTIME%" "%DIST%\portable\python" > nul

if exist "tools\ffmpeg.exe" (
    copy /y tools\ffmpeg.exe "%DIST%\tools\" > nul
    if exist "tools\ffprobe.exe" copy /y tools\ffprobe.exe "%DIST%\tools\" > nul
) else (
    echo [WARN] tools\ffmpeg.exe was not found. Clip extraction will be skipped unless ffmpeg is on PATH.
)

echo.
echo [OK] Portable package created:
echo   %DIST%
echo.
echo Start with:
echo   %DIST%\run_gui.bat
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
endlocal
