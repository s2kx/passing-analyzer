@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  セットアップスクリプト
rem  別のPCで初めて使う場合はこちらを先に実行してください。
rem  Python 3.11?3.13 がインストール済みであれば動作します。
rem ============================================================

cd /d "%~dp0"

echo.
echo === 追い越し検出ツール セットアップ ===
echo.

rem --- Python を探す (py ランチャー優先) ---
set "PY="
set "PY_VER="

for %%v in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%v --version > nul 2>&1
        if not errorlevel 1 (
            set "PY=py -%%v"
            set "PY_VER=%%v"
        )
    )
)

if not defined PY (
    python --version > nul 2>&1
    if not errorlevel 1 (
        set "PY=python"
        for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    )
)

if not defined PY (
    echo [ERROR] Python が見つかりません。
    echo.
    echo Python 3.11?3.13 をインストールしてから再実行してください。
    echo   https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo 使用する Python: %PY% (バージョン %PY_VER%)
echo.

rem --- 既存の .venv を確認 ---
if exist ".venv\Scripts\python.exe" (
    echo [INFO] .venv が既に存在します。
    echo 再作成しますか？ ^(既存の環境は削除されます^)
    choice /C YN /N /M "再作成する [Y/N]: "
    if errorlevel 2 (
        echo キャンセルしました。既存の環境をそのまま使います。
        goto :done
    )
    echo 既存の .venv を削除しています...
    rmdir /s /q ".venv"
    if errorlevel 1 (
        echo [ERROR] .venv の削除に失敗しました。
        echo 手動で .venv フォルダを削除してから再実行してください。
        pause
        exit /b 1
    )
)

rem --- 仮想環境を作成 ---
echo .venv を作成しています...
%PY% -m venv .venv
if errorlevel 1 (
    echo [ERROR] 仮想環境の作成に失敗しました。
    pause
    exit /b 1
)
echo.

rem --- pip を最新化 ---
echo pip を更新しています...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
echo.

rem --- 依存パッケージをインストール ---
echo requirements.txt からパッケージをインストールしています...
echo (PyTorch / Ultralytics など大きいパッケージが含まれます。時間がかかる場合があります)
echo.
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] パッケージのインストールに失敗しました。
    echo ネットワーク接続を確認し、再試行してください。
    pause
    exit /b 1
)

echo.
rem --- ffmpeg を確認・インストール ---
echo ffmpeg を確認しています...
where ffmpeg > nul 2>&1
if not errorlevel 1 (
    echo [INFO] ffmpeg: インストール済み
) else (
    echo [INFO] ffmpeg が見つかりません。winget でインストールを試みます...
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo [WARN] winget でのインストールに失敗しました。
        echo 手動でインストールしてください: winget install ffmpeg
        echo   https://ffmpeg.org/download.html
        echo ffmpeg がなくてもクリップ生成以外の機能は動作します。
        echo.
    ) else (
        echo [INFO] ffmpeg のインストールが完了しました。
        echo ターミナルを再起動すると ffmpeg が使用可能になります。
        echo.
    )
):done
echo.
echo === セットアップ完了 ===
echo.
echo 起動方法:
echo   run.bat     ... 動画を解析する (ダブルクリックまたはドラッグ&ドロップ)
echo   run_gui.bat ... GUIワークフローを起動する
echo.
echo 環境確認: run.bat --check
echo.
pause
endlocal
