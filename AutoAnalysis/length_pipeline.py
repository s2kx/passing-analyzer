"""車長込みの相対速度への変換を、3段階まとめて実行する。

  準備  convert_relative_speed.py --init-lengths  car_lengths.csv に無いイベントの行を足す
  1/3   extract_car_images.py     clips/ の動画から追い越し車の写真を切り出す（既存はスキップ）
  2/3   car_length_gpt.py         写真から ChatGPT で車長を推定し car_lengths.csv を更新（推定済みはスキップ）
  3/3   convert_relative_speed.py car_lengths.csv を使って解析結果の xlsx を converted/ へ変換

入力は解析結果フォルダ（--src。セッションごとの lidar_edit/ final/ clips/ を含む。
既定はリポジトリの output/）。出力はすべて保存先フォルダ（--work。既定は解析結果フォルダの
隣の「<フォルダ名>_車長変換」）に置き、元の解析結果は書き換えない:

    <work>/car_lengths.csv   車長の一覧（GPT の推定と手入力。source=manual は上書きされない）
    <work>/car_images/       ChatGPT へ送った切り抜き
    <work>/car_frames/       確認用（枠を描いた全体フレームと一覧画像）
    <work>/gpt_raw/          ChatGPT の生応答
    <work>/converted/        変換した xlsx と conversion_summary.csv

どの段階も「やり直しが要るものだけ」を処理するので、何度実行しても結果は増えるだけで壊れない。
途中の段階が失敗したらそこで止める（後の段階へ中途半端な入力を渡さないため）。
（リポジトリ直下の run_pipeline.py は全自動解析の方。こちらは車長変換専用）

    portable/python/python.exe AutoAnalysis/length_pipeline.py                         # output/ を既定の設定で
    portable/python/python.exe AutoAnalysis/length_pipeline.py --src E:/data/output     # 解析結果フォルダを指定
    portable/python/python.exe AutoAnalysis/length_pipeline.py --fallback lidar        # 車長が無い行は点群の実測長で代用
    portable/python/python.exe AutoAnalysis/length_pipeline.py --skip extract          # 画像の切り出しを飛ばす
    portable/python/python.exe AutoAnalysis/length_pipeline.py --session w6b/obihiro --force
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# リポジトリ内（AutoAnalysis/ 配下）でも配布版（単独フォルダ）でも動くよう、本体の web_gui.py が隣に
# あればその階層を、無ければ自分の階層をルート（output・yolov8n.pt・portable/python の置き場）にする。
ROOT = HERE.parent if (HERE.parent / "web_gui.py").is_file() else HERE
DEFAULT_SRC = ROOT / "output"
WORK_SUFFIX = "_車長変換"
STAGES = ("extract", "gpt", "convert")


def default_work(src: Path) -> Path:
    """保存先フォルダの既定: 解析結果フォルダの隣の「<フォルダ名>_車長変換」。"""
    return src.parent / f"{src.name}{WORK_SUFFIX}"


def run_stage(name: str, script: str, args: list[str]) -> None:
    print(f"\n{'=' * 70}\n[{name}] {script} {' '.join(args)}\n{'=' * 70}", flush=True)
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(HERE / script), *args], cwd=ROOT)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        sys.exit(f"\n[{name}] 失敗（終了コード {result.returncode}、{elapsed:.0f}秒）。ここで止めます。")
    print(f"[{name}] 完了（{elapsed:.0f}秒）", flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="写真の切り出し → 車長推定 → 相対速度の変換 を順に実行する")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="解析結果フォルダ（既定: output）")
    parser.add_argument("--work", type=Path, default=None,
                        help="保存先フォルダ（既定: 解析結果フォルダの隣の <名前>_車長変換）")
    parser.add_argument("--skip", choices=STAGES, action="append", default=[],
                        help="飛ばす段階（複数可: --skip extract --skip gpt）")
    parser.add_argument("--session", default="", help="画像の切り出しをこのセッションだけにする（例: w6b/obihiro）")
    parser.add_argument("--all", action="store_true", help="切り出しで未確認・除外のイベントも対象にする")
    parser.add_argument("--force", action="store_true", help="画像・GPT推定を作り直す（manual の車長は保つ）")
    parser.add_argument("--limit", type=int, default=0, help="GPT推定をこの件数で止める（試し打ち用）")
    parser.add_argument("--fallback", choices=["none", "lidar", "class"], default="none",
                        help="車長が無いイベントの代用（既定: 代用せず空欄）")
    args = parser.parse_args()

    src = args.src.resolve()
    if not src.is_dir():
        sys.exit(f"解析結果フォルダがありません: {src}")
    work = (args.work or default_work(src)).resolve()
    if work == src or src in work.parents:
        sys.exit(f"保存先フォルダは解析結果フォルダの中に置けません: {work}")
    work.mkdir(parents=True, exist_ok=True)
    lengths = work / "car_lengths.csv"
    print(f"解析結果フォルダ: {src}\n保存先フォルダ: {work}", flush=True)

    run_stage("準備 車長一覧の雛形", "convert_relative_speed.py",
              ["--init-lengths", "--src", str(src), "--lengths", str(lengths)])
    if "extract" not in args.skip:
        extract_args = ["--src", str(src), "--images", str(work / "car_images"), "--frames", str(work / "car_frames")] \
            + (["--session", args.session] if args.session else []) \
            + (["--all"] if args.all else []) + (["--force"] if args.force else [])
        run_stage("1/3 写真の切り出し", "extract_car_images.py", extract_args)
    if "gpt" not in args.skip:
        gpt_args = ["--images", str(work / "car_images"), "--lengths", str(lengths), "--raw", str(work / "gpt_raw")] \
            + (["--force"] if args.force else []) + (["--limit", str(args.limit)] if args.limit else [])
        run_stage("2/3 車長の推定", "car_length_gpt.py", gpt_args)
    if "convert" not in args.skip:
        run_stage("3/3 相対速度の変換", "convert_relative_speed.py",
                  ["--src", str(src), "--dest", str(work / "converted"), "--lengths", str(lengths),
                   "--fallback", args.fallback])
    print(f"\nすべて完了: {work}")


if __name__ == "__main__":
    main()
