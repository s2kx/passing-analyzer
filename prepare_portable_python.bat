@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_portable_python.ps1" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Portable Python preparation failed.
    pause
    exit /b 1
)

echo.
echo [OK] Portable Python preparation completed.
pause
endlocal
