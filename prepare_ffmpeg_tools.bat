@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_ffmpeg_tools.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] FFmpeg preparation failed.
    pause
    exit /b 1
)

echo.
echo [OK] FFmpeg preparation completed.
pause
endlocal
