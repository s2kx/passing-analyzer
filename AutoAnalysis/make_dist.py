"""車長変換ツールの配布版（git に入れない単独フォルダと zip）を dist/ に作る。

    portable/python/python.exe AutoAnalysis/make_dist.py
      → dist/車長変換ツール/      そのまま別PCへコピーして使えるフォルダ
      → dist/車長変換ツール.zip   上を zip にしたもの

含めるもの
  AutoAnalysis のコード一式（length_gui.py・length_pipeline.py・3段階のスクリプト・xlsx_cells.py）、
  web_ui_length/、requirements.txt と prepare_portable_python.ps1（初回セットアップ用。本体と同じもの）、
  起動用の 車長変換GUI.bat（配布版の配置に合わせたもの）、はじめに.txt。
含めないもの
  .env（APIキー）、length_gui_settings.json、解析データ、portable/python、yolov8n.pt
  （Python と YOLO モデルは配布先の初回起動時に bat が自動で準備する）。

リポジトリ内では AutoAnalysis/ 配下、配布版ではフォルダ直下にスクリプトが並ぶが、
各スクリプトは「隣に web_gui.py があるか」でルートを判定するのでどちらでも同じコードで動く。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "web_gui.py").is_file() else HERE
DIST_NAME = "車長変換ツール"

CODE_FILES = (
    "length_gui.py", "length_pipeline.py", "extract_car_images.py", "car_length_gpt.py",
    "convert_relative_speed.py", "xlsx_cells.py",
)
UI_FILES = ("index.html", "style.css", "app.js")
SETUP_FILES = ("requirements.txt", "prepare_portable_python.ps1")   # 本体のものをそのまま同梱する

# 配布版の起動 bat。本体の 車長変換GUI.bat と同じ流れ（portable Python と YOLO モデルを準備 →
# 自己検証 → 窓を開く）で、スクリプトがフォルダ直下にある配置に合わせてある。
# cmd.exe は ANSI コードページでバッチを読むので、この中身は ASCII だけにする。
LAUNCHER_BAT = r"""@echo off
setlocal EnableExtensions

rem ============================================================
rem  Car length conversion tool - launcher (standalone package)
rem
rem  Cuts vehicle photos from the event clips, estimates each
rem  car's length with ChatGPT, and converts the relative speed
rem  to (p3 + car length) * 3.6 / passing time.
rem
rem  First run: portable Python, the required libraries and the
rem  YOLO model are prepared automatically (internet required,
rem  several minutes). Keep this window open until the GUI shows.
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
echo === Car Length Conversion Tool ===

call :ensure_python || exit /b 1
call :ensure_yolo_model || exit /b 1

if not exist ".env" (
    echo [INFO] ChatGPT API key is not set yet. Paste it in the GUI and press Save.
)

"%PY%" length_gui.py --check
if errorlevel 1 (
    echo [ERROR] Startup check failed.
    pause
    exit /b 1
)

echo [start] Launching the GUI...
start "Car Length Conversion" "%PYW%" length_gui.py
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

if not exist "prepare_portable_python.ps1" (
    echo [ERROR] prepare_portable_python.ps1 was not found next to this file.
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
    echo [ERROR] yolov8n.pt was not created in this folder.
    pause
    exit /b 1
)
exit /b 0
"""

README_TXT = """車長込みの相対速度への変換ツール
================================

追い越し解析の結果（セッションごとの lidar_edit / final / clips を含むフォルダ）から
追い越し車の写真を切り出し、ChatGPT で車名とカタログ全長を推定し、次の式に直した
Excel を書き出します（解析ソフト本体の 2026-11-07 以降の式と同じ）。

    通過時間   = フェーズ4の1つ前の行の時刻 - フェーズ3の時刻
    相対速度   = 3.6 × (距離p3 + 車長) ÷ 通過時間
    TTC        = フェーズ2の distance ÷ (フェーズ2の speed ÷ 3.6)
    自動車速度 = 自転車速度 + 相対速度

元の解析結果は一切書き換えません。

使い方
------
1. 「車長変換GUI.bat」をダブルクリックする。
   初回は Python 本体・必要なライブラリ・YOLO モデルを自動で準備します
   （インターネット接続が必要。数分〜十数分かかります。黒い画面が出ている間は閉じないでください）。
   2回目からはすぐに画面が開きます。
2. 画面の「解析結果フォルダ」を選ぶ。保存先は自動で、その隣に「<フォルダ名>_車長変換」ができます。
3. ChatGPT の API キー（sk- で始まる文字列）を貼り付けて「保存」する。
   キーはこのフォルダの .env に保存され、この PC だけで使われます。
4. 「実行」を押す。写真の切り出し → 車長の推定 → 相対速度の変換 の順に進みます。
   結果は保存先フォルダの converted に、元と同じファイル名の Excel として出ます。
   （済んでいる分は飛ばすので、何度押しても構いません）
5. 「車長の確認・修正」画面で、写真と GPT の判断理由を見て車長を確かめる。
   おかしい値は数値を直して「保存」すると「手入力」になり、以後の再推定でも上書きされません。
   直したらもう一度「実行」を押すと Excel に反映されます。

フォルダの中身
--------------
  車長変換GUI.bat             起動用（初回セットアップ込み）
  length_gui.py               画面（本体）
  length_pipeline.py          3段階をまとめて実行するコマンド版
  extract_car_images.py       1. 写真の切り出し（YOLO）
  car_length_gpt.py           2. ChatGPT で車長を推定
  convert_relative_speed.py   3. 相対速度の変換（xlsx_cells.py はその部品）
  web_ui_length/              画面の HTML / CSS / JS
  requirements.txt, prepare_portable_python.ps1   初回セットアップ用
  はじめに.txt                この説明

  初回起動後に増えるもの:
  portable/python/            Python 本体とライブラリ（数 GB）
  yolov8n.pt                  YOLO のモデル
  .env                        ChatGPT の API キー
  length_gui_settings.json    前回のフォルダ指定

保存先フォルダ（<名前>_車長変換）の中身
----------------------------------------
  car_lengths.csv     車長の一覧（GPT の推定・確度・判断理由と、手入力）
  car_images/         ChatGPT に送った切り抜き写真
  car_frames/         確認用（赤枠を描いた全体フレームと、セッションごとの一覧画像）
  gpt_raw/            ChatGPT の生の応答
  converted/          変換した Excel と、全イベントの新旧比較 conversion_summary.csv

注意
----
- このフォルダは好きな場所に置けます（USB メモリ・外付け SSD でも可）。移動しても設定の書き換えは要りません。
- .env には API キーが入ります。フォルダごと他の人に渡すときは .env を消してください。
- 車長が無いイベント（clips が無いセッションなど）は、Excel の相対速度・自動車速度が空欄になり、
  「車長の出典」列に「車長未取得」と書かれます（通過時間・TTC は車長に依らないので新しい式で入ります）。
- フェーズ2の speed が空欄のイベント（フェーズ2を代用したもの）は TTC が空欄になります。
- NVIDIA GPU が無い PC でも動きます（写真の切り出しが遅くなるだけです）。

コマンドで細かく指定したいとき
------------------------------
  portable\\python\\python.exe length_pipeline.py --src <解析結果フォルダ>
    --fallback lidar   車長が無いイベントは点群の実測長で代用する（--fallback class なら車種の代表値）
    --skip extract     写真の切り出しを飛ばす（--skip gpt / --skip convert も可）
    --force            写真と GPT の推定を作り直す（手入力の車長は保つ）
    --session w6b/ID1  切り出しをこのセッションだけにする
"""


def build(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in CODE_FILES:
        shutil.copy2(HERE / name, dest / name)
    (dest / "web_ui_length").mkdir()
    for name in UI_FILES:
        shutil.copy2(HERE / "web_ui_length" / name, dest / "web_ui_length" / name)
    for name in SETUP_FILES:
        source = ROOT / name
        if not source.is_file():
            sys.exit(f"同梱するファイルがありません: {source}")
        shutil.copy2(source, dest / name)
    lines = LAUNCHER_BAT.strip("\n").splitlines()
    assert all(ord(ch) < 128 for line in lines for ch in line), "bat に非ASCII文字が混ざっています"
    (dest / "車長変換GUI.bat").write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
    (dest / "はじめに.txt").write_text(README_TXT, encoding="utf-8-sig", newline="\r\n")
    return dest


def make_zip(folder: Path, zip_path: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{folder.name}/{path.relative_to(folder).as_posix()}")
    return zip_path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="車長変換ツールの配布版（フォルダと zip）を作る")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="出力先（既定: dist）")
    parser.add_argument("--no-zip", action="store_true", help="フォルダだけ作って zip は作らない")
    args = parser.parse_args()

    folder = build(args.out / DIST_NAME)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    print(f"配布フォルダ: {folder}（{len(files)} ファイル）")
    for path in files:
        print(f"  {path.relative_to(folder).as_posix()}")
    if not args.no_zip:
        zip_path = make_zip(folder, args.out / f"{DIST_NAME}.zip")
        print(f"zip: {zip_path}（{zip_path.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
