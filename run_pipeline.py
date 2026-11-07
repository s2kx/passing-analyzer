#!/usr/bin/env python3
"""撮影セッションフォルダを end-to-end で自動処理するパイプライン。

GUI(`web_gui.py`)で人が Step1〜6 を順に操作していた作業を、1コマンドで通す。

    python run_pipeline.py <セッションフォルダ> [--out <出力先>] [--view rear] ...

セッションフォルダの規約:

    <セッション>/
      GOPRO/*.MP4      … GoPro動画（ファイル名順で1本の連結タイムラインとして扱う）
      LiDAR/*.pcap     … Velodyne PCAP（1つ）
      metadata.json    … 任意。{"subject":..,"date":..,"view":..,"organize_id":..}

段階:

    0. 撮影方向    --view / metadata.json が無ければ detect_view で自動判定
    1. 検出        detect_overtaking.py をサブプロセス実行
    2. GPS抽出     各動画の GoPro テレメトリ → out/gps/<stem>/telemetry_gps.xlsx
    3. 時刻同期    GoPro GPS と PCAP からオフセットを算出 → out/sync.json
    4. 点群追跡    候補と車両を1対1に割り当てて追跡 → out/lidar/overtaking_*.csv
    5. edit CSV    最近接点軌跡と解析Excel → out/lidar_edit/
    6. 自動レビュー LiDARの結果で採否を判定 → out/reviewed_overtaking_events.csv
    7. 最終Excel   out/final/<出力フォルダ名>_final.xlsx
    7b. 閲覧用レポート クリップと点群を見る out/report.html（--no-report で省略）
    8. レポート    out/pipeline_report.json / out/pipeline_report.txt

自動レビューは点群追跡と edit CSV の結果を使って判定するため、追跡より後に置く。
動画だけでは隣車線の車や誤検知を切り分けられないが、LiDAR の離隔距離まで求まれば
「そもそも車が見つからない」「離隔距離が隣車線相当」を機械的に落とせる。

さらに段階2のGPS（無ければ段階3.5のLiDAR自車速度）から自車の運動を読み、
「自転車が停止中」「旋回中」「通過区間を抜けきれていない」候補を落とす。停止中と
旋回中は追い越しの前提（自転車が直進していて、静止物が自車速度で後退する）が
崩れており、追跡も下流の計算も成り立たない。``--no-ego-gate`` で無効化できる。

危険感は全自動実行では空欄にする（人が後から最終Excelへ入力する）。

LiDAR には追い越し車が居るのに動画側の候補が無い（車列の2台目など検出の
取りこぼし）場合は、``L001`` … の候補IDで点群CSVまで書き出し、レビューCSVへ
``review_status="未確認"`` の行として起票する。最終Excelは採用行だけなので、
人が確認して採用に変えるまでは出力に混ざらない。``--no-lidar-extra`` で無効化できる。

段階7b は結果の閲覧用に ``out/report.html`` を作る（``make_report.py``）。イベント
ごとに動画クリップ（``clips/`` への相対参照）と切り抜いた点群のビューア（上面図・
側面図のフレーム送り）が並ぶ1枚のHTMLで、ダブルクリックで開ける。既存の出力から
``python make_report.py --out <出力先>`` で作り直せる。``--no-report`` で省略。
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from car_length import (
    estimate_car_lengths,
    load_car_lengths,
    read_review_events,
    target_events,
)
from detect_view import (
    DEFAULT_SAMPLE_SECONDS as VIEW_SAMPLE_SECONDS,
    DEFAULT_SAMPLES as VIEW_SAMPLES,
    DetectViewError,
    detect_view,
)
from edit_csv import (
    analyze_edit_points,
    read_edit_points,
    write_edit_analysis_xlsx,
    write_edit_csv,
)
from final_export import generate_final_workbook
from lidar_pcap import VelodynePcap
from make_report import ReportError, generate_report
from web_gui import (
    detection_python,
    move_edit_csv_to_dir,
    read_events_csv,
    save_reviewed_events,
)


APP_DIR = Path(__file__).resolve().parent
VIDEO_SUFFIXES = {".mp4", ".mov"}
# 実データ検証(260701/ID1)より: 1.5秒では接近区間が窓に入らずフェーズ2が
# フォールバックになりやすい。5秒で追跡成功が大きく増える。--track-margin を
# 明示した場合の前後共通マージンで、既定では view 別の非対称マージンを使う。
DEFAULT_TRACK_MARGIN = 5.0
# 撮影方向は指定が無ければ段階0で自動判定する。判定不能のときだけこの値を仮定する
# （既定値を front / rear のどちらかに固定すると自動判定が走らなくなる点に注意）。
DEFAULT_VIEW = "front"

# 追跡窓は接近側へ長く取る。TTC は接近区間（フェーズ2）から求めるため、車が
# 後方 Y<0 から近づいてくる時間を窓へ入れないとフォールバックになり値が狂う。
# front 撮影では車が映る前から後方にいるので開始側を、rear 撮影では映ってから
# 抜けるまでが長いので終了側を伸ばす。(開始側, 終了側) の秒数。
VIEW_TRACK_MARGINS = {"front": (8.0, 3.0), "rear": (3.0, 8.0)}

# 自動レビューのしきい値。
# 日本の追い越しは隣車線でなければ 2.5m 程度まで（正解データの LC は全て 2.45m
# 以下）。これを超える通過は隣車線の車を掴んだものとみなして除外する。
MAX_LANE_CLEARANCE = 3.5
# 同一車線の追い越しと言い切れる離隔距離の上限(m)。これを超えて MAX_LANE_CLEARANCE
# 以下の候補は、隣車線の車を掴んでいる疑いが濃いので「未確認」にする（除外はしない。
# 実測LCが申告値より広く出ることがあり、機械的に捨てると本物を落とすため）。
# 260701/ID1 を正解12件と突き合わせた実測: 正しく追えた11件の実測LCは 0.93〜1.89m、
# 正解に対応しない（＝隣車線の車を掴んだ）7件は 3.10〜3.48m で、間は完全に空いている。
# 正解46件の申告値も最大 2.45m・2番目は 2.03m なので、2.6m はその谷にある。
REVIEW_LANE_CLEARANCE = 2.6
# 追跡の確信度がこれ未満なら「未確認」にして人手確認へ回す。
MIN_TRACK_CONFIDENCE = 0.5

# 自車運動による足切り（段階6の前段で GPS / LiDAR から求める）。
# 停止中は「静止物が自車速度で後退する」前提が崩れ、lidar_autotrack の
# estimate_forward_sign が前進方向を +Y と決め打ちする（EGO_SPEED_FOR_SIGN 未満）。
# 通過時間・相対速度・TTC はどれも成り立たないので、停止中の候補は除外する。
# 260701/ID1 の 009 がこの型で、GPS 0.33m/s・LiDAR推定 0.03m/s。採用した他の19件は
# 最低でも 4.35m/s あるので、1.0m/s で確実に分かれる。
EGO_STOP_SPEED = 1.0            # これ未満は停止中とみなして除外する (m/s)
EGO_SLOW_SPEED = 2.0            # これ未満は低速として「未確認」へ回す (m/s)
# 曲がり角ではセンサのヨーで静止物が横へ流れ、追い越し車と同じ動きに見える。
# 自車運動補正をしていない以上、旋回中の追跡結果は信用できないので人手確認へ回す。
# ID1 の実測では採用行の最大が 16.9度/秒（緩いカーブ）で、明確な旋回は 20度/秒超
# （曲がり角の 25〜42度/秒）。採用行を巻き込まない 20度/秒を既定にする。
EGO_TURN_YAW_RATE = 20.0        # これを超える旋回中の候補は「未確認」へ回す (度/秒)
EGO_WINDOW_MARGIN = 1.5         # 自車運動を見るイベント前後の余裕 (秒)
EGO_MIN_SPEED_FOR_HEADING = 1.0  # 方位の計算に使う最低速度 (m/s)
EGO_HEADING_BASELINE = 3.0      # 方位を1つ測るのに必要な移動距離 (m)
# フェーズ3→4の Y 移動量の下限 (m)。通過区間（|Y| < 3m）を抜けきれば 6m 前後になる。
# これを下回る解析は通過区間の途中で追跡が切れており、通過時間・距離p3・TTC が
# 成り立たない。ID1 では採用行が 5.27〜6.51m、切れた追跡は 2.51m 以下だった。
MIN_PASS_PROGRESS = 4.5

# 段階3.5 時刻同期の自己検証（tools/sync_check.py）。
# GPS速度とLiDAR点群から求めた自車速度の相互相関でオフセットを独立に推定し、
# 段階3の値と突き合わせる。差の扱いは 260701 の実測（第4波J）に合わせて3段階。
# 1秒以内は同期の精度内なので記録だけ、3秒以内は警告、3秒超は追い越し1件分
# （車が窓を通り抜ける時間）を超えるので割り当てが壊れる。
SYNC_CHECK_TOLERANCE = 1.0      # これ以下の差は正常（記録のみ）
SYNC_CHECK_MISMATCH = 3.0       # これを超える差はレポートに推定値を明示して人手項目にする
# 推定値を信用できる条件。tools/sync_check.py の判定と同じ値を使う。
SYNC_CHECK_MIN_PEAK = 0.50
SYNC_CHECK_MIN_PROMINENCE = 0.15
SYNC_CHECK_MAX_SPREAD = 2.0

# イベントと車両の割り当て。
# 期待通過時刻の基準にする動画側の時刻。front 撮影では追い越し車は「自転車を抜いた
# 直後」に画角へ入るので、検出トラックの開始時刻が横に並んだ時刻にいちばん近い。
# bbox 面積が最大になる peak_t_s も普通はその直後に来るが、検出が途中で別の車へ
# 乗り移ったり、遠方で一度小さくなってから膨らんだりすると開始の十数秒後になり、
# 期待通過時刻が数秒ずれて隣車線の車を掴む。開始からこの秒数より後の peak は信用せず
# 開始時刻側へ丸める（rear 撮影は「抜かれる直前」まで映るので終了時刻側が基準）。
# 実データ5セッションでは 1〜5 秒のどこに取っても成績が同じで、3秒はその中央。
PEAK_ANCHOR_LIMIT = 3.0
# 同一車線の追い越しとみなす離隔距離の上限(m)。正解46件の離隔距離は最大 2.45m なので、
# これを超える車は隣車線を通ったとみなす。採否ではなく割り当ての優先順位に使う値で、
# 「まず同一車線の車、居なければ隣車線の車」の順に候補を割り当てる。2.0〜3.0m の
# どこに取っても実データの成績は変わらない。
NEAR_LANE_CLEARANCE = 2.6
# 期待通過時刻からこれ以上離れた車は、そのイベントの車とみなさない（秒）。
PASS_MATCH_TOLERANCE = 6.0
# 通過時刻がこれ以内なら、別イベントの走査で見つかった同じ車とみなす（秒）。
# 車列の2台目とは 4〜9 秒空くので、それより十分小さく取る。
VEHICLE_MERGE_GAP = 1.0
# 同じ車とみなすには横位置も近いことを要る（m）。時刻だけで束ねると、隣の車線を
# 車が流れている最中の追い越しで、同一車線の車と隣車線の車が1台にまとめられる。
# 束ねられたグループは1つのイベントにしか割り当てられず、残りは L### としても
# 起票されないので、同一車線の車がどこにも出てこなくなる（260701/ID1 の 608.6s が
# これ。正解 LC 1.02m の車は f5503x+1.4 として score 1.000 で追跡できていたのに、
# 0.1〜0.8秒違いで通る隣車線の3台（X +3.0/+3.4/+4.2）と1台に束ねられていた）。
# 値は「同じ車の横位置のぶれ」（TRACK_KEY_LATERAL_TOLERANCE = 1.0m）より少し広く、
# 同一車線と隣車線の間隔（実測で 1.6m 以上）より狭い 1.5m にする。
VEHICLE_MERGE_LATERAL = 1.5
# 期待通過時刻の系統ずれを推定する際、これ以上離れた候補は外れ値として使わない（秒）。
BIAS_SAMPLE_LIMIT = 10.0
# どの候補にも割り当たらなかった車を「動画検出の取りこぼしかも」と挙げる評価値の下限。
ORPHAN_MIN_SCORE = 0.85
# 割り当て先の無い車を起票するときの候補IDの接頭辞。動画由来の候補（001, 002, …）と
# 衝突しないよう L001 から振る。行は必ず「未確認」で、最終Excel（採用のみ）には出ない。
LIDAR_EXTRA_PREFIX = "L"
LIDAR_EXTRA_STATUS = "未確認"
# 追跡側（lidar_autotrack.candidate_summary）が返す車体寸法のキー。
# 割り当てた車の寸法を段階6の車種判定へ運ぶために使う。
LIDAR_SIZE_KEYS = ("length", "width", "height", "near_length", "points")
# 車種の判定（段階6a）。動画側の判定は YOLO のクラス投票（大型）と黄色ナンバーの
# 検出（軽）で決めているが、実データでは
#   * ミニバン・ワゴンを YOLO が truck と呼ぶ（普通→大型の誤り）
#   * トラック後部の黄色い標示・反射板を黄色ナンバーと見る（大型→軽の誤り）
# で外れる。点群で測った寸法のほうが素直なので、車を割り当てられた行はこちらで
# 決め直す。しきい値は下の calibrate 節（docs/計算方法.md 8）の実測から決めた。
VEHICLE_LARGE_SIDE_AREA = 9.0     # 側面積(車体長×車高, m^2) これ以上は大型
VEHICLE_KEI_MAX_LENGTH = 3.50     # 軽自動車の全長上限 3.40m + 点群のふくらみ
VEHICLE_KEI_MAX_WIDTH = 1.80      # 軽自動車の全幅上限 1.48m + 点群のふくらみ
VEHICLE_MIN_LENGTH = 2.50         # これ未満は車の全体を測れていないとみなす(m)
VEHICLE_MIN_WIDTH = 1.20          # 同上(m)
# 出力直前の重複チェック。同じフレームで矩形の中心がこの距離以内なら同じ車とみなす（m）。
SAME_VEHICLE_DISTANCE = 2.0
SAME_VEHICLE_MIN_FRAMES = 3

STATUS_LABELS = {
    "ok": "完了",
    "skipped": "スキップ",
    "warning": "警告あり",
    "failed": "失敗",
}

VIEW_SOURCE_LABELS = {
    "cli": "--view で明示指定",
    "metadata": "metadata.json の指定",
    "auto": "自動判定",
    "fallback": "判定不能のため仮定",
}


class PipelineError(Exception):
    """パイプラインを続行できない致命的なエラー。"""


# ── 設定 ──────────────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    session_dir: Path
    out_dir: Path
    videos: list[Path]
    pcap_path: Path
    #: "front" / "rear"。空文字なら段階0で自動判定する。
    view: str = ""
    #: view の決まり方: "cli" / "metadata" / "auto"（自動判定）
    view_source: str = "auto"
    view_samples: int = VIEW_SAMPLES
    view_sample_seconds: float = VIEW_SAMPLE_SECONDS
    subject_id: str = ""
    collection_date: str = ""
    organize_id: str = ""
    #: 道路の車線数。全候補の lane_count 列（最終Excelの「車線数」）の既定値。
    #: 自動では求まらないため、指定が無ければ空欄のまま人が後から入力する。
    lane_count: str = ""
    skip_detect: bool = False
    redetect: bool = False
    skip_gps: bool = False
    offset_override: float | None = None
    # None なら view 別の非対称マージン（VIEW_TRACK_MARGINS）を使う。
    track_margin: float | None = None
    # 自動レビューで隣車線の通過とみなす離隔距離のしきい値(m)。
    max_lane_clearance: float = MAX_LANE_CLEARANCE
    # これを超える離隔距離の候補は「未確認」にする(m)。
    review_lane_clearance: float = REVIEW_LANE_CLEARANCE
    # 自車運動（停止・旋回）による足切りを行うか。
    ego_gate: bool = True
    # これ未満の自車速度は停止中とみなして除外する(m/s)。
    stop_speed: float = EGO_STOP_SPEED
    # これを超える旋回中の候補は「未確認」へ回す(度/秒)。
    turn_yaw_rate: float = EGO_TURN_YAW_RATE
    # 割り当て先の無い車を LiDAR 発の「未確認」行として起票するか。
    lidar_extra: bool = True
    # 自動レビューで「未確認」相当の条件（隣車線の疑い・追跡不完全・低速・旋回・
    # 低確信度・LiDAR発の行など）に当たった候補を「未確認」のまま残すか。
    # 既定は残さず、そのまま不採用（除外）にする（人手確認を挟まない運用）。
    keep_unconfirmed: bool = False
    # 段階3.5 の時刻同期の自己検証を行うか。
    sync_check: bool = True
    # 検証で「信頼できる推定値が現行値と3秒超ずれる」ときに推定値へ差し替えるか。
    # 段階3のGPMFベースの値はLiDAR収録PCの時計ずれを見られないため、既定で差し替える。
    auto_offset: bool = True
    # 段階7b で閲覧用HTMLレポート（report.html）を作るか。
    report_html: bool = True
    # 段階6.5 で動画の写真から ChatGPT で車長を推定するか（相対速度の式に使う）。
    car_length: bool = True

    def track_margins(self) -> tuple[float, float]:
        """追跡窓の (開始側, 終了側) マージン秒を返す。

        撮影方向が確定していない（自動判定が走らない・判定不能の ``fallback``）
        場合は、非対称マージンの向きを誤ると接近または離脱の区間が窓から切れる
        ため、両側とも広い方の値で対称に取る。
        """
        if self.track_margin is not None:
            return float(self.track_margin), float(self.track_margin)
        if self.view_source == "fallback":
            wide = max(m for pair in VIEW_TRACK_MARGINS.values() for m in pair)
            return wide, wide
        return VIEW_TRACK_MARGINS.get(
            self.view, (DEFAULT_TRACK_MARGIN, DEFAULT_TRACK_MARGIN)
        )


def _subdir(session_dir: Path, name: str) -> Path | None:
    """大文字小文字を問わずサブフォルダを探す。"""
    if not session_dir.is_dir():
        return None
    for child in sorted(session_dir.iterdir()):
        if child.is_dir() and child.name.lower() == name:
            return child
    return None


def discover_videos(session_dir: Path) -> list[Path]:
    """`<セッション>/GOPRO/*.MP4` を名前順で返す（無ければセッション直下も探す）。"""
    for root in (_subdir(session_dir, "gopro"), session_dir):
        if root is None or not root.is_dir():
            continue
        found = sorted(
            path for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
        if found:
            return found
    return []


def discover_pcap(session_dir: Path) -> list[Path]:
    """`<セッション>/LiDAR/*.pcap` を返す（無ければセッション直下も探す）。"""
    for root in (_subdir(session_dir, "lidar"), session_dir):
        if root is None or not root.is_dir():
            continue
        found = sorted(
            path for path in root.iterdir()
            if path.is_file() and path.suffix.lower() == ".pcap"
        )
        if found:
            return found
    return []


def read_metadata(session_dir: Path) -> dict:
    """セッションフォルダの metadata.json を読む（無ければ空辞書）。"""
    path = session_dir / "metadata.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"metadata.json を読み込めません: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError("metadata.json はオブジェクト（辞書）である必要があります")
    return data


def _pick(cli_value, metadata: dict, key: str, default=""):
    """CLI引数 > metadata.json > 既定値 の優先順で値を選ぶ。"""
    if cli_value not in (None, ""):
        return cli_value
    value = metadata.get(key)
    if value in (None, ""):
        return default
    return value


def build_config(args: argparse.Namespace) -> PipelineConfig:
    session_dir = Path(args.session).expanduser().resolve()
    if not session_dir.is_dir():
        raise PipelineError(f"セッションフォルダが見つかりません: {session_dir}")
    metadata = read_metadata(session_dir)

    if args.videos:
        videos = [Path(v).expanduser().resolve() for v in args.videos]
    else:
        videos = discover_videos(session_dir)
    if not videos:
        raise PipelineError(
            f"GoPro動画が見つかりません: {session_dir / 'GOPRO'}\n"
            "--videos で明示指定してください"
        )
    missing = [str(v) for v in videos if not v.is_file()]
    if missing:
        raise PipelineError("動画が見つかりません: " + ", ".join(missing))

    if args.pcap:
        pcap_path = Path(args.pcap).expanduser().resolve()
        if not pcap_path.is_file():
            raise PipelineError(f"PCAPが見つかりません: {pcap_path}")
    else:
        candidates = discover_pcap(session_dir)
        if not candidates:
            raise PipelineError(
                f"LiDAR PCAPが見つかりません: {session_dir / 'LiDAR'}\n"
                "--pcap で明示指定してください"
            )
        pcap_path = candidates[0]

    out_value = args.out or metadata.get("out")
    out_dir = Path(out_value).expanduser().resolve() if out_value else session_dir / "out_auto"

    # 撮影方向: CLI > metadata.json > 自動判定（空文字＝段階0で判定する）
    view = str(_pick(args.view, metadata, "view", ""))
    if view and view not in {"front", "rear"}:
        raise PipelineError(f"撮影方向は front / rear のどちらかです: {view}")
    if args.view:
        view_source = "cli"
    elif view:
        view_source = "metadata"
    else:
        view_source = "auto"

    return PipelineConfig(
        session_dir=session_dir,
        out_dir=out_dir,
        videos=videos,
        pcap_path=pcap_path,
        view=view,
        view_source=view_source,
        view_samples=int(args.view_samples),
        view_sample_seconds=float(args.view_sample_seconds),
        subject_id=str(_pick(args.subject, metadata, "subject", session_dir.name)),
        collection_date=str(_pick(args.date, metadata, "date", session_dir.parent.name)),
        organize_id=str(_pick(args.organize_id, metadata, "organize_id", "")),
        lane_count=str(_pick(args.lane_count, metadata, "lane_count", "")).strip(),
        skip_detect=bool(args.skip_detect),
        redetect=bool(args.redetect),
        skip_gps=bool(args.skip_gps),
        offset_override=args.offset,
        track_margin=None if args.track_margin is None else float(args.track_margin),
        max_lane_clearance=float(args.max_lane_clearance),
        review_lane_clearance=float(getattr(args, "review_lane_clearance",
                                             REVIEW_LANE_CLEARANCE)),
        ego_gate=bool(getattr(args, "ego_gate", True)),
        stop_speed=float(getattr(args, "stop_speed", EGO_STOP_SPEED)),
        turn_yaw_rate=float(getattr(args, "turn_yaw_rate", EGO_TURN_YAW_RATE)),
        lidar_extra=bool(getattr(args, "lidar_extra", True)),
        keep_unconfirmed=bool(getattr(args, "keep_unconfirmed", False)),
        sync_check=bool(getattr(args, "sync_check", True)),
        auto_offset=bool(getattr(args, "auto_offset", True)),
        report_html=bool(getattr(args, "report_html", True)),
        car_length=bool(getattr(args, "car_length", True)),
    )


# ── レポート ──────────────────────────────────────────────────────────────────

@dataclass
class PipelineReport:
    config: PipelineConfig
    started_at: float = field(default_factory=time.time)
    stages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manual_actions: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    def stage(self, name: str, title: str, status: str, message: str = "", **detail) -> dict:
        entry = {"name": name, "title": title, "status": status, "message": message, **detail}
        self.stages.append(entry)
        # 段階の完了を1行で流す。コンソールで進捗が追えるほか、web_gui の
        # 全自動実行画面がこの行を解析して進捗バーと段階一覧を更新する。
        print(f"[stage] {name} {title} [{status}] {message}", flush=True)
        return entry

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def manual(self, message: str) -> None:
        if message not in self.manual_actions:
            self.manual_actions.append(message)

    def to_dict(self) -> dict:
        finished = time.time()
        config = self.config
        return {
            "session": str(config.session_dir),
            "out": str(config.out_dir),
            "videos": [str(v) for v in config.videos],
            "pcap": str(config.pcap_path),
            "view": config.view or DEFAULT_VIEW,
            "view_source": config.view_source,
            "subject_id": config.subject_id,
            "collection_date": config.collection_date,
            "organize_id": config.organize_id,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "finished_at": datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
            "elapsed_seconds": round(finished - self.started_at, 1),
            "info": self.info,
            "stages": self.stages,
            "warnings": self.warnings,
            "manual_actions": self.manual_actions,
        }

    def to_text(self) -> str:
        data = self.to_dict()
        lines = [
            "=== 自転車追い越し解析 自動パイプライン レポート ===",
            f"セッション   : {data['session']}",
            f"出力先       : {data['out']}",
            f"動画         : {', '.join(Path(v).name for v in data['videos'])}",
            f"PCAP         : {Path(data['pcap']).name}",
            f"撮影方向     : {data['view']}（{VIEW_SOURCE_LABELS.get(data['view_source'], data['view_source'])}）",
            f"被験者       : {data['subject_id']}",
            f"データ収集日 : {data['collection_date']}",
            f"実行         : {data['started_at']} 〜 {data['finished_at']}"
            f" ({data['elapsed_seconds']}秒)",
            "",
        ]
        if data["info"]:
            lines.append("[基本情報]")
            for key, value in data["info"].items():
                lines.append(f"  {key}: {value}")
            lines.append("")

        lines.append("[段階]")
        for entry in data["stages"]:
            label = STATUS_LABELS.get(entry["status"], entry["status"])
            # 全角を含むため桁揃えはせず、状態を角括弧で区切って読ませる
            lines.append(f"  {entry['title']} [{label}] {entry['message']}")
        lines.append("")

        lines.append(f"[警告] {len(data['warnings'])}件")
        for message in data["warnings"]:
            lines.append(f"  - {message}")
        lines.append("")

        lines.append(f"[人手が必要な項目] {len(data['manual_actions'])}件")
        for message in data["manual_actions"]:
            lines.append(f"  - {message}")
        return "\n".join(lines) + "\n"

    def save(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "pipeline_report.json"
        text_path = out_dir / "pipeline_report.txt"
        json_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        text_path.write_text(self.to_text(), encoding="utf-8")
        return json_path, text_path


# ── 段階0: 撮影方向 ───────────────────────────────────────────────────────────

VIEW_MANUAL_ACTION = (
    "撮影方向を自動判定できませんでした。out/clips のクリップで前向き／後ろ向きを確認し、"
    f"--view front / --view rear を指定して再実行してください（暫定で {DEFAULT_VIEW} を使用）"
)


def resolve_view(config: PipelineConfig, report: PipelineReport, detector=None) -> str:
    """撮影方向を決める。明示指定が常に優先し、無い場合だけ自動判定する。

    `detector` は差し替え用（既定は ``detect_view.detect_view``）。判定不能でも
    パイプラインは止めず、``DEFAULT_VIEW`` を仮定して警告と人手項目を残す。
    """
    if config.view:
        report.stage(
            "view", "0.撮影方向", "ok",
            f"{config.view}（{VIEW_SOURCE_LABELS[config.view_source]}）",
            view=config.view, source=config.view_source,
        )
        return config.view

    if config.skip_detect:
        # 検出をやり直さないなら方向が要るのは点群追跡の窓だけ。自動判定は省略し、
        # 窓は track_margins() が fallback 扱いで前後対称の広い値に切り替える
        config.view = DEFAULT_VIEW
        config.view_source = "fallback"
        report.stage(
            "view", "0.撮影方向", "skipped",
            "検出を行わないため自動判定は省略します（追跡窓は前後対称の広めで対応）",
            view=config.view, source=config.view_source,
        )
        return config.view

    detector = detector or detect_view
    try:
        result = detector(
            config.videos, config.out_dir / "view_probe",
            samples=config.view_samples, sample_seconds=config.view_sample_seconds,
        )
    except DetectViewError as exc:
        config.view = DEFAULT_VIEW
        config.view_source = "fallback"
        report.stage("view", "0.撮影方向", "failed", f"自動判定に失敗しました: {exc}",
                     view=config.view, source=config.view_source)
        report.warn(f"撮影方向を自動判定できません: {exc}")
        report.manual(VIEW_MANUAL_ACTION)
        return config.view
    except Exception as exc:  # noqa: BLE001 - 自動判定の失敗でパイプラインを止めない
        config.view = DEFAULT_VIEW
        config.view_source = "fallback"
        report.stage("view", "0.撮影方向", "failed", f"自動判定で予期しないエラー: {exc}",
                     view=config.view, source=config.view_source)
        report.warn(f"撮影方向の自動判定で予期しないエラー: {exc}")
        report.manual(VIEW_MANUAL_ACTION)
        return config.view

    result = result or {}
    detail = dict(result.get("detail") or {})
    view = result.get("view")
    confidence = float(result.get("confidence") or 0.0)
    ratio = detail.get("radial_ratio")
    evidence = (
        f"背景フローの放射成分 {ratio:+.2f}" if isinstance(ratio, (int, float))
        else "背景フローを測定できず"
    ) + f" / 走行中 {detail.get('moving_pairs', '?')}フレーム対"
    if view in {"front", "rear"}:
        config.view = view
        config.view_source = "auto"
        report.stage(
            "view", "0.撮影方向", "ok",
            f"{view} と判定しました（確信度 {confidence:.2f}、{evidence}）",
            view=view, source="auto", confidence=confidence, detail=detail,
        )
        report.info["撮影方向の判定根拠"] = detail.get("reason", "")
        if confidence < 0.8:
            report.warn(
                f"撮影方向の自動判定の確信度が低めです（{confidence:.2f}）。"
                "検出結果が明らかに少ない場合は --view を明示して再実行してください"
            )
        return view

    config.view = DEFAULT_VIEW
    config.view_source = "fallback"
    report.stage(
        "view", "0.撮影方向", "warning",
        f"判定不能のため {DEFAULT_VIEW} を仮定します（{evidence}）",
        view=config.view, source="fallback", confidence=0.0, detail=detail,
    )
    report.info["撮影方向の判定根拠"] = detail.get("reason", "")
    report.warn(f"撮影方向を自動判定できませんでした: {detail.get('reason', '')}")
    report.manual(VIEW_MANUAL_ACTION)
    return config.view


# ── 段階1: 検出 ───────────────────────────────────────────────────────────────

def detection_command(config: PipelineConfig) -> list[str]:
    """GUIの start_detection と同じ検出コマンドを組み立てる。"""
    return [
        str(detection_python()), str(APP_DIR / "detect_overtaking.py"),
        *[str(video) for video in config.videos],
        "--view", config.view, "--out", str(config.out_dir),
        "--clip", "--imgsz", "640", "--batch", "32",
        "--flow-stride", "3", "--prefetch-batches", "2",
        "--decoder", "auto",
    ]


def run_detection(config: PipelineConfig, report: PipelineReport) -> Path:
    events_csv = config.out_dir / "overtaking_events.csv"
    if config.skip_detect or (events_csv.is_file() and not config.redetect):
        reason = "--skip-detect が指定されました" if config.skip_detect else \
            "既存の overtaking_events.csv を使用します"
        if config.skip_detect and not events_csv.is_file():
            raise PipelineError(
                f"--skip-detect が指定されましたが {events_csv} がありません"
            )
        report.stage("detect", "1.検出", "skipped", reason, events_csv=str(events_csv))
        return events_csv

    cmd = detection_command(config)
    env = os.environ.copy()
    yolo_config_dir = APP_DIR / ".ultralytics"
    yolo_config_dir.mkdir(exist_ok=True)
    env["YOLO_CONFIG_DIR"] = str(yolo_config_dir)
    print("$ " + subprocess.list2cmdline(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=APP_DIR, env=env)
    if completed.returncode != 0:
        report.stage("detect", "1.検出", "failed", f"終了コード {completed.returncode}")
        raise PipelineError(f"detect_overtaking.py が失敗しました（終了コード {completed.returncode}）")
    if not events_csv.is_file():
        report.stage("detect", "1.検出", "failed", "overtaking_events.csv が生成されませんでした")
        raise PipelineError("overtaking_events.csv が生成されませんでした")
    report.stage(
        "detect", "1.検出", "ok", f"{events_csv.name} を生成しました",
        events_csv=str(events_csv), command=cmd,
    )
    return events_csv


# ── 段階2: GPS抽出 ────────────────────────────────────────────────────────────

def run_gps_extraction(config: PipelineConfig, report: PipelineReport) -> None:
    """GUIの _gps_worker と同じ配置で out/gps/<動画stem>/telemetry_gps.xlsx を作る。"""
    if config.skip_gps:
        report.stage("gps", "2.GPS抽出", "skipped", "--skip-gps が指定されました")
        report.manual("自転車速度はGPSテレメトリが必要です。--skip-gps を外して再実行してください")
        return
    try:
        from extract_telemetry import extract_telemetry, save_gps_workbook
    except Exception as exc:  # noqa: BLE001 - 依存欠落も警告に留めて続行する
        report.stage("gps", "2.GPS抽出", "warning", f"初期化できません: {exc}")
        report.warn(f"GPS抽出を初期化できません: {exc}")
        return

    gps_root = config.out_dir / "gps"
    gps_root.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    failed: list[str] = []
    for video in config.videos:
        print(f"[gps] {video.name} のGoPro GPSを抽出中...", flush=True)
        try:
            streams = extract_telemetry(video)
            video_out = gps_root / video.stem
            video_out.mkdir(exist_ok=True)
            book = save_gps_workbook(streams, video_out)
            if book is None:
                failed.append(video.name)
                report.warn(f"{video.name}: GPS5データが見つかりませんでした")
            else:
                saved.append(str(book))
        except Exception as exc:  # noqa: BLE001 - 1本失敗しても続行する
            failed.append(video.name)
            report.warn(f"{video.name}: GPS抽出に失敗しました: {exc}")

    status = "ok" if saved and not failed else ("warning" if saved else "failed")
    if not saved:
        report.manual("GPSテレメトリが無いため、最終Excelの自転車速度・自動車速度は空欄になります")
    report.stage(
        "gps", "2.GPS抽出", status,
        f"{len(saved)}/{len(config.videos)} 本を保存しました",
        saved=saved, failed=failed,
    )


# ── 段階3: 時刻同期 ───────────────────────────────────────────────────────────

def run_sync(config: PipelineConfig, pcap: VelodynePcap, report: PipelineReport) -> float:
    """先頭動画とPCAPからオフセットを求める（pcap時刻 = gopro時刻 + offset）。"""
    sync: dict = {
        "pcap": str(config.pcap_path),
        "pcap_start_unix": pcap.start_unix_time,
        "video": str(config.videos[0]),
    }
    if config.offset_override is not None:
        offset = float(config.offset_override)
        sync.update({"offset": offset, "source": "manual"})
        report.stage("sync", "3.時刻同期", "ok", f"--offset の指定値 {offset} を使用します")
    else:
        from gpmf_sync import SyncError, compute_gps_offset
        try:
            result = compute_gps_offset(config.videos[0], pcap.start_unix_time)
            offset = float(result["offset"])
            sync.update({**result, "source": "gps"})
            report.stage(
                "sync", "3.時刻同期", "ok",
                f"オフセット {offset} 秒（GoPro {result['gopro_utc']} / PCAP {result['pcap_utc']}）",
                **result,
            )
        except SyncError as exc:
            offset = 0.0
            sync.update({"offset": offset, "source": "fallback", "error": str(exc)})
            report.stage("sync", "3.時刻同期", "failed", f"GPS自動同期に失敗しました: {exc}")
            report.warn(f"GPS自動同期に失敗しました: {exc}")
            report.manual(
                "時刻オフセットを自動算出できませんでした。GUIで手動同期し "
                "--offset で指定して再実行してください（暫定で 0 秒を使用）"
            )
        except Exception as exc:  # noqa: BLE001 - 想定外でも続行する
            offset = 0.0
            sync.update({"offset": offset, "source": "fallback", "error": str(exc)})
            report.stage("sync", "3.時刻同期", "failed", f"予期しないエラー: {exc}")
            report.warn(f"GPS自動同期で予期しないエラー: {exc}")
            report.manual("時刻オフセットを手動で確認し --offset で指定して再実行してください")

    config.out_dir.mkdir(parents=True, exist_ok=True)
    (config.out_dir / "sync.json").write_text(
        json.dumps(sync, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return offset


# ── 段階3.5: 時刻同期の自己検証 ───────────────────────────────────────────────

def load_sync_checker() -> tuple[object | None, str]:
    """``tools/sync_check.check_session`` を読み込む（無ければ ``None``）。

    診断用の任意依存なので、モジュールが無くてもパイプラインは止めない。
    """
    try:
        sys.path.insert(0, str(APP_DIR / "tools"))
        from sync_check import check_session
    except Exception as exc:  # noqa: BLE001 - 依存が無くても本処理は続ける
        return None, f"{type(exc).__name__}: {exc}"
    return check_session, ""


def sync_check_is_trusted(result: dict) -> bool:
    """自己検証の推定オフセットを信用してよいかを判定する。

    相関のピークと突出度が十分高く、区間ごとの推定がばらついていないことを見る
    （``tools/sync_check.py`` が ``status="low_confidence"`` を付ける条件と同じ）。
    """
    if result.get("status") in {"failed", "low_confidence", "no_reference"}:
        return False
    if result.get("estimated_offset") is None:
        return False
    if float(result.get("correlation_peak") or 0.0) < SYNC_CHECK_MIN_PEAK:
        return False
    if float(result.get("correlation_prominence") or 0.0) < SYNC_CHECK_MIN_PROMINENCE:
        return False
    spread = result.get("segment_spread_s")
    return not (spread is not None and float(spread) > SYNC_CHECK_MAX_SPREAD)


def run_sync_check(config: PipelineConfig, pcap: VelodynePcap, offset: float,
                   report: PipelineReport, checker=None) -> float:
    """段階3のオフセットを GPS 速度×LiDAR 自車速度の相互相関で検証する。

    段階3のオフセットは GoPro の GPMF 時刻と PCAP の記録時刻の引き算なので、
    LiDAR 収録PCの時計がずれていると丸ごとずれる（260701 では全5セッションで
    +4.2〜5.2 秒ずれていた）。ここでは点群から自車速度を作り直し、GPS 速度との
    相互相関でオフセットを独立に推定して突き合わせる。

    差の扱い:

    * ``SYNC_CHECK_TOLERANCE`` 以内 … 記録のみ（同期の精度内）
    * ``SYNC_CHECK_MISMATCH`` 以内 … 警告（割り当ての取り違えが増えうる）
    * それ超 … 推定値が信用できるなら**既定でその場で差し替えて**先へ進む。
      GPMF 側はPCの時計ずれを見られないので、独立に測った推定値のほうを採る。
      ``--no-auto-offset`` を付けた場合だけ差し替えず、警告と人手項目を残す

    戻り値は以降の段階で使うオフセット（差し替えなければ引数のまま）。
    """
    if not config.sync_check:
        report.stage("synccheck", "3.5.同期の自己検証", "skipped",
                     "--skip-sync-check が指定されました")
        return offset
    if config.offset_override is not None:
        report.stage("synccheck", "3.5.同期の自己検証", "skipped",
                     "--offset で手動指定されたオフセットは検証しません")
        return offset

    checker, load_error = (checker, "") if checker else load_sync_checker()
    if checker is None:
        report.stage("synccheck", "3.5.同期の自己検証", "skipped",
                     f"tools/sync_check を読み込めません（{load_error}）")
        return offset

    try:
        result = checker(
            config.out_dir, config.session_dir, pcap=pcap,
            series_path=config.out_dir / "sync_series.npz",
        ) or {}
    except Exception as exc:  # noqa: BLE001 - 検証の失敗で本処理は止めない
        report.stage("synccheck", "3.5.同期の自己検証", "failed",
                     f"自己検証でエラー: {exc}")
        report.warn(f"時刻同期の自己検証に失敗しました: {exc}")
        return offset

    try:
        (config.out_dir / "sync_check.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:
        report.warn(f"sync_check.json を書き出せません: {exc}")

    estimated = result.get("estimated_offset")
    trusted = sync_check_is_trusted(result)
    detail = {
        "estimated_offset": estimated,
        "current_offset": offset,
        "difference": result.get("difference"),
        "correlation_peak": result.get("correlation_peak"),
        "correlation_prominence": result.get("correlation_prominence"),
        "segment_spread_s": result.get("segment_spread_s"),
        "trusted": trusted,
    }
    if not trusted:
        report.stage("synccheck", "3.5.同期の自己検証", "warning",
                     "推定オフセットを信用できません（相関が弱いか区間でばらつきます）",
                     **detail)
        report.warn(
            "時刻同期の自己検証: 推定オフセットを信用できないため検証できませんでした"
            f"（相関ピーク {result.get('correlation_peak')} / "
            f"突出度 {result.get('correlation_prominence')} / "
            f"区間ばらつき {result.get('segment_spread_s')}秒）"
        )
        return offset

    difference = float(estimated) - float(offset)
    report.info["同期の自己検証(推定offset, 差)"] = (round(float(estimated), 3),
                                                    round(difference, 3))
    if abs(difference) <= SYNC_CHECK_TOLERANCE:
        report.stage("synccheck", "3.5.同期の自己検証", "ok",
                     f"推定 {estimated:+.3f} 秒 / 現行 {offset:+.3f} 秒（差 {difference:+.3f} 秒）",
                     **detail)
        return offset

    if abs(difference) <= SYNC_CHECK_MISMATCH:
        report.stage("synccheck", "3.5.同期の自己検証", "warning",
                     f"推定 {estimated:+.3f} 秒と現行 {offset:+.3f} 秒の差が "
                     f"{difference:+.3f} 秒あります", **detail)
        report.warn(
            f"時刻同期の自己検証: 推定オフセットとの差が {difference:+.2f} 秒あります"
            "（車両の割り当てがずれる可能性があります）"
        )
        return offset

    if config.auto_offset:
        report.stage("synccheck", "3.5.同期の自己検証", "warning",
                     f"自己検証の推定値 {estimated:+.3f} 秒へ差し替えました"
                     f"（現行 {offset:+.3f} 秒 / 差 {difference:+.3f} 秒）",
                     applied=True, **detail)
        report.warn(
            f"時刻同期を自己検証の推定値 {float(estimated):+.3f} 秒へ差し替えました"
            f"（段階3の値 {offset:+.3f} 秒との差 {difference:+.3f} 秒）"
        )
        _rewrite_sync_offset(config, float(estimated), report)
        return float(estimated)

    report.stage("synccheck", "3.5.同期の自己検証", "warning",
                 f"推定 {estimated:+.3f} 秒と現行 {offset:+.3f} 秒の差が "
                 f"{difference:+.3f} 秒あります（許容 {SYNC_CHECK_MISMATCH:g} 秒超）",
                 **detail)
    report.warn(
        f"時刻同期の自己検証: 推定オフセットとの差が {difference:+.2f} 秒で、"
        "追い越し1件分より大きいため車両の割り当てが壊れます"
    )
    report.manual(
        f"時刻オフセットを確認してください。自己検証の推定値は {float(estimated):+.3f} 秒です"
        f"（段階3の値 {offset:+.3f} 秒）。--no-auto-offset を外して再実行すれば推定値で"
        f"処理できます（推定値を直接指定するなら --offset {float(estimated):.3f}）"
    )
    return offset


def _rewrite_sync_offset(config: PipelineConfig, offset: float,
                         report: PipelineReport) -> None:
    """``sync.json`` のオフセットを差し替える（差し替え前の値も残す）。"""
    path = config.out_dir / "sync.json"
    try:
        sync = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.warn(f"sync.json を更新できません: {exc}")
        return
    sync["gpmf_offset"] = sync.get("offset")
    sync["offset"] = offset
    sync["source"] = "sync_check"
    try:
        path.write_text(json.dumps(sync, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        report.warn(f"sync.json を更新できません: {exc}")


# ── 段階4: 点群自動追跡とイベント↔車両の割り当て ─────────────────────────────

def load_autotracker() -> tuple[object | None, str]:
    """``lidar_autotrack.track_overtaking`` を読み込む（未実装なら None）。

    契約::

        track_overtaking(pcap: VelodynePcap, first_frame: int, last_frame: int,
                         side: str = "right",
                         expected_pass_time: float | None = None,
                         exclude_track_keys: list[str] | None = None) -> {
            "ok": bool,
            "frames": [{"index": int,
                        "bounds": {"min_x":.., "max_x":.., "min_y":.., "max_y":..}}],
            "confidence": float,
            "warnings": [str],
            "pass_time": float | None,     # 選択トラックが |Y|<3m を横断した時刻
            "track_key": str | None,       # 同一車両を指す識別子
            "candidates": [{"track_key", "pass_time", "score",
                            "median_x", "lc_estimate"}],
        }

    後ろ 3 つのキーと引数 2 つは任意。受け取らない古いプラグインでも動くよう、
    呼び出し側は署名を見て渡すかどうかを決める。
    """
    try:
        from lidar_autotrack import track_overtaking
    except ImportError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - 壊れたプラグインでも続行する
        return None, f"lidar_autotrack を読み込めません: {exc}"
    return track_overtaking, ""


def _event_seconds(event: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(event.get(key) or default)
    except (TypeError, ValueError):
        return default


def _tracker_accepts(tracker, name: str) -> bool:
    """プラグインが指定の任意引数を受け取れるか調べる（後方互換のため）。"""
    try:
        return name in inspect.signature(tracker).parameters
    except (TypeError, ValueError):
        return False


def event_window(config: PipelineConfig, event: dict, offset: float) -> tuple[float, float]:
    """1 候補の追跡窓（pcap 相対秒）を返す。"""
    start_margin, end_margin = config.track_margins()
    return (_event_seconds(event, "t_start_s") + offset - start_margin,
            _event_seconds(event, "t_end_s") + offset + end_margin)


def pass_anchor(event: dict, view: str = "", limit: float = PEAK_ANCHOR_LIMIT) -> float:
    """1 候補について、車が横に並んだ時刻の目安になる動画側の秒を返す。

    基本は最接近時刻 ``peak_t_s`` だが、これは bbox 面積が最大のフレームなので、
    検出が別の車へ乗り移るとトラック開始のはるか後ろへずれる。追い越し車が
    自転車の横に並ぶのは、front 撮影なら画角へ入る瞬間（``t_start_s``）、rear 撮影
    なら画角から消える瞬間（``t_end_s``）なので、そこから ``limit`` 秒より離れた
    peak は採らずに端の時刻へ丸める。撮影方向が分からない場合は従来どおり peak。
    """
    peak = _event_seconds(event, "peak_t_s")
    kind = str(view).lower()
    if kind == "front":
        return min(peak, _event_seconds(event, "t_start_s") + limit)
    if kind == "rear":
        return max(peak, _event_seconds(event, "t_end_s") - limit)
    return peak


def expected_pass_times(events: list[dict], offset: float, bias: float = 0.0,
                        view: str = "") -> dict[str, float]:
    """候補ごとの「車が横に並ぶはずの時刻」（pcap 相対秒）を返す。

    動画側の基準時刻（``pass_anchor``）を時刻オフセットで pcap 時刻へ移し、系統ずれ
    ``bias`` を足す。この時刻に最も近い車をそのイベントの車とみなす。
    """
    return {
        str(event.get("event_id", "")): pass_anchor(event, view) + offset + bias
        for event in events
    }


def estimate_pass_bias(expected: dict[str, float], candidate_sets: dict[str, list[dict]],
                       limit: float = BIAS_SAMPLE_LIMIT) -> float:
    """期待通過時刻の系統ずれ（秒）を推定する。

    期待通過時刻は「動画の最接近時刻 + 時刻オフセット」で作るが、GPS同期の誤差や
    カメラの見え方（前向きでは車が映る前から後方にいる）で LiDAR の実際の通過時刻
    と数秒ずれる。全候補について「最も近い車との差」を集めて中央値を取れば、
    個々の誤対応に引きずられずにこのずれだけを測れる。260701/ID1 では約 +2.7 秒。
    """
    deltas = []
    for event_id, candidates in candidate_sets.items():
        want = expected.get(event_id)
        times = [c["pass_time"] for c in candidates if c.get("pass_time") is not None]
        if want is None or not times:
            continue
        nearest = min(times, key=lambda value: abs(value - want))
        if abs(nearest - want) <= limit:
            deltas.append(nearest - want)
    return float(statistics.median(deltas)) if len(deltas) >= 3 else 0.0


def _same_vehicle_candidate(left: dict, right: dict, merge_gap: float,
                            lateral: float) -> bool:
    """2つの候補要約が同じ車かどうかを、通過時刻と横位置の近さで判定する。

    横位置（``median_x``）が分からない候補は、従来どおり通過時刻だけで判断する。
    """
    if abs(float(left["pass_time"]) - float(right["pass_time"])) > merge_gap:
        return False
    left_x, right_x = left.get("median_x"), right.get("median_x")
    if not isinstance(left_x, (int, float)) or not isinstance(right_x, (int, float)):
        return True
    return abs(float(left_x) - float(right_x)) <= lateral


def group_vehicles(candidate_sets: dict[str, list[dict]],
                   merge_gap: float = VEHICLE_MERGE_GAP,
                   lateral: float = VEHICLE_MERGE_LATERAL) -> list[list[tuple[str, dict]]]:
    """全候補の追跡結果を実車両ごとのグループにまとめる。

    イベントごとに追跡窓が違うため、同じ車でも走査のたびに ``track_key`` が僅かに
    ぶれうる。通過時刻が ``merge_gap`` 秒以内で、かつ横位置が ``lateral`` m 以内の
    候補を同じ車とみなして 1 つのグループにし、「1 台の車は 1 つのイベントにしか
    出力しない」を保証する単位にする。車列の 2 台目とは 4 秒以上空くので取り違えない。

    横位置も見るのは、隣の車線を車が流れている最中の追い越しで、同一車線の車と
    隣車線の車が同時刻に通るため（``VEHICLE_MERGE_LATERAL`` の説明を参照）。
    判定はグループの先頭（いちばん早い候補）と比べて行う。直前の1件と比べると、
    少しずつずれた候補が数珠つなぎになって ``merge_gap`` より広い範囲が1台に
    なるため、グループの広がりは必ず ``merge_gap`` 以内になるようにする。
    """
    items = [
        (event_id, candidate)
        for event_id, candidates in candidate_sets.items()
        for candidate in candidates
        if candidate.get("pass_time") is not None
    ]
    items.sort(key=lambda item: item[1]["pass_time"])
    groups: list[list[tuple[str, dict]]] = []
    for entry in items:
        target = None
        for group in reversed(groups):
            anchor = group[0][1]
            # items も groups も通過時刻順なので、先頭が届かなければ以前のグループも届かない
            if float(entry[1]["pass_time"]) - float(anchor["pass_time"]) > merge_gap:
                break
            if _same_vehicle_candidate(entry[1], anchor, merge_gap, lateral):
                target = group
                break
        if target is None:
            groups.append([entry])
        else:
            target.append(entry)
    return groups


def lane_rank(candidate: dict, near_clearance: float = NEAR_LANE_CLEARANCE) -> int:
    """候補の車線らしさを ``0``（同一車線）/ ``1``（隣車線相当）で返す。

    追跡側が出す離隔距離の目安 ``lc_estimate`` が ``near_clearance`` を超える車は
    隣車線を通ったとみなす。目安が無い（追跡が通過区間を捉えていない）場合は
    決めつけず同一車線側に置く。
    """
    clearance = candidate.get("lc_estimate")
    if isinstance(clearance, (int, float)) and float(clearance) > near_clearance:
        return 1
    return 0


def assign_events(expected: dict[str, float], candidate_sets: dict[str, list[dict]],
                  tolerance: float = PASS_MATCH_TOLERANCE,
                  merge_gap: float = VEHICLE_MERGE_GAP,
                  near_clearance: float = NEAR_LANE_CLEARANCE) -> dict[str, dict]:
    """イベントと車両を 1 対 1 に割り当てる。

    「同一車線を通った車で、期待通過時刻との差が小さい」ペアから順に確定していく
    グリーディ法。既に使ったイベントと車両は飛ばすため、同じ車が複数イベントへ
    出力される重複が起きず、同じ車を取り合った場合は期待通過時刻に近い方の
    イベントが勝つ。差が ``tolerance`` を超えるペアは組まない（＝そのイベントは
    車なしになる）。

    車線を時刻より優先するのは、追い越しの窓には隣車線を走る車も入るためで、
    時刻だけで選ぶと期待時刻が僅かにずれただけで隣車線の車を掴み、本来のその
    イベントの車が余る（＝どのイベントにも割り当たらない車として捨てられる）。
    隣車線の車も、同一車線の候補が無いイベントには従来どおり割り当てる。

    戻り値は ``event_id -> {"track_key", "pass_time", "lc_estimate", "score",
    "vehicle", "delta"}``。割り当たらなかったイベントは含まない。
    """
    pairs: list[tuple[int, float, str, int, dict]] = []
    for vehicle, members in enumerate(group_vehicles(candidate_sets, merge_gap)):
        # 同じ車について同じイベントが複数の候補を持つことはないが、念のため近い方を採る
        best: dict[str, tuple[tuple[int, float], dict]] = {}
        for event_id, candidate in members:
            want = expected.get(event_id)
            if want is None:
                continue
            delta = abs(candidate["pass_time"] - want)
            if delta > tolerance:
                continue
            key = (lane_rank(candidate, near_clearance), delta)
            if event_id not in best or key < best[event_id][0]:
                best[event_id] = (key, candidate)
        pairs.extend((rank, delta, event_id, vehicle, candidate)
                     for event_id, ((rank, delta), candidate) in best.items())

    pairs.sort(key=lambda item: (item[0], item[1], item[2]))
    used_events: set[str] = set()
    used_vehicles: set[int] = set()
    assignment: dict[str, dict] = {}
    for _rank, delta, event_id, vehicle, candidate in pairs:
        if event_id in used_events or vehicle in used_vehicles:
            continue
        used_events.add(event_id)
        used_vehicles.add(vehicle)
        assignment[event_id] = {
            "track_key": candidate["track_key"],
            "pass_time": candidate["pass_time"],
            "lc_estimate": candidate.get("lc_estimate"),
            "score": candidate.get("score"),
            "vehicle": vehicle,
            "delta": round(delta, 3),
            **{key: candidate.get(key) for key in LIDAR_SIZE_KEYS},
        }
    return assignment


def unassigned_vehicles(candidate_sets: dict[str, list[dict]], assignment: dict[str, dict],
                        max_clearance: float = MAX_LANE_CLEARANCE,
                        min_score: float = ORPHAN_MIN_SCORE,
                        merge_gap: float = VEHICLE_MERGE_GAP) -> list[dict]:
    """どの候補にも割り当てられなかった「追い越し車らしい車」を返す。

    動画検出が車列の2台目を取りこぼすと、LiDAR には車が居るのに割り当て先の候補が
    無い状態になる。実データ(260701/ID1)ではこれが未検出の追い越しの正体だった。
    採否は決められないので、レポートの人手項目として「動画検出の取りこぼしかも
    しれない車」を挙げる。隣車線相当（``max_clearance`` 超）と評価値の低い車は
    誤検出が多いので除く。

    返す辞書は ``track_overtaking`` の候補要約に ``source_event_id``（その車が
    見つかった追跡窓の候補ID）を足したもの。追跡し直して点群CSVを書き出すとき、
    どの窓を走査すればよいかがこれで分かる。
    """
    used = {value.get("vehicle") for value in assignment.values()}
    orphans = []
    for vehicle, members in enumerate(group_vehicles(candidate_sets, merge_gap)):
        if vehicle in used:
            continue
        event_id, best = max(members, key=lambda item: item[1].get("score") or 0.0)
        clearance = best.get("lc_estimate")
        if (best.get("score") or 0.0) < min_score:
            continue
        if isinstance(clearance, (int, float)) and clearance > max_clearance:
            continue
        orphans.append({**best, "source_event_id": event_id})
    return sorted(orphans, key=lambda candidate: candidate["pass_time"])


def dump_assignment_debug(path: Path, offset: float, bias: float,
                          events: list[dict], results: dict[str, dict],
                          raw_expected: dict[str, float], expected: dict[str, float],
                          candidate_sets: dict[str, list[dict]],
                          assignment: dict[str, dict]) -> None:
    """割り当ての入力と結果を JSON へ書き出す（診断用）。

    点群の走査は 1 セッション数分かかるため、割り当てだけを試行錯誤したいときに
    走査をやり直さずに済むよう、候補一覧と期待通過時刻をそのまま残す。
    パイプラインの動作には影響しない。
    """
    payload = {
        "offset": offset,
        "bias": bias,
        "events": [
            {
                "event_id": str(event.get("event_id", "")),
                "peak_t_s": _event_seconds(event, "peak_t_s"),
                "t_start_s": _event_seconds(event, "t_start_s"),
                "t_end_s": _event_seconds(event, "t_end_s"),
                "class": event.get("class", ""),
                "first_frame": (results.get(str(event.get("event_id", ""))) or {}).get("first_frame"),
                "last_frame": (results.get(str(event.get("event_id", ""))) or {}).get("last_frame"),
                "raw_expected": raw_expected.get(str(event.get("event_id", ""))),
                "expected": expected.get(str(event.get("event_id", ""))),
                "candidates": candidate_sets.get(str(event.get("event_id", "")), []),
                "assigned": assignment.get(str(event.get("event_id", ""))),
            }
            for event in events
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:  # 診断用なので書けなくても続行する
        pass


def same_vehicle_key(left: str, right: str) -> bool:
    """2 つの ``track_key`` が同じ車を指すかを、追跡側と同じ規則で判定する。

    追跡側（``lidar_autotrack.track_key_matches``）は追跡窓のぶれを吸収するため
    キーを完全一致ではなくフレーム・横位置の近さで照合する。除外キーを組み立てる
    側が完全一致で考えると、狙ったトラックの近くにある別キーを除外したつもりが
    狙ったトラックまで除外されてしまうため、判定を追跡側に合わせる。
    追跡モジュールが無い環境では完全一致に落とす。
    """
    if left == right:
        return True
    try:
        from lidar_autotrack import track_key_matches
    except Exception:  # noqa: BLE001 - 追跡モジュールが無くても割り当ては続ける
        return False
    return bool(track_key_matches(left, right))


def keys_other_than(candidates: list[dict], track_key: str) -> list[str]:
    """``candidates`` から ``track_key`` 以外のキーを返す（選び直しの除外キー）。

    ``exclude_track_keys`` へ渡すと、追跡側には狙ったトラックしか残らない。ただし
    狙ったトラック自身と同一視されるキー（通過フレームが数フレーム・横位置が1m以内
    しか違わない別候補）は除くこと。これを渡すと追跡側が狙ったトラックごと除外して
    しまい、「候補はすべて他のイベントへ割り当て済み」になって車を失う。

    採用順で狙ったトラックより**後ろ**にあるキーも除外する。``candidates`` の順は
    1回目の走査（補正前の期待時刻）での採用順だが、選び直しは系統ずれを補正した
    期待時刻で走らせるため、順番が入れ替わるからである。後ろのキーを残していると、
    補正で近くなった別の車（隣車線など）を掴んで割り当ての決定が覆る
    （実データ260701/ID5 の 753.1s が、ずれ0.17秒で隣車線の車に化けていた）。
    """
    keys = [candidate["track_key"] for candidate in candidates]
    if track_key not in keys:
        return []
    return [key for key in keys
            if key != track_key and not same_vehicle_key(key, track_key)]


def _scan_event(tracker, pcap: VelodynePcap, config: PipelineConfig, event: dict,
                offset: float, report: PipelineReport, expected: float | None,
                exclude: list[str] | None = None) -> dict | None:
    """1 候補分の追跡を実行する。窓外・例外は理由を記録して ``None`` を返す。"""
    event_id = str(event.get("event_id", ""))
    start_s, end_s = event_window(config, event, offset)
    if end_s < 0 or start_s > pcap.duration:
        report.warn(
            f"候補 {event_id}: 追い越し時刻 {start_s:.1f}〜{end_s:.1f}s がPCAPの"
            f"収録範囲(0〜{pcap.duration:.1f}s)から外れています"
        )
        report.manual(f"候補 {event_id}: 時刻オフセットを確認し、手動選択が必要です")
        return None
    first = pcap.nearest_frame(start_s)
    last = pcap.nearest_frame(end_s)
    options: dict = {}
    if expected is not None and _tracker_accepts(tracker, "expected_pass_time"):
        options["expected_pass_time"] = expected
    if exclude and _tracker_accepts(tracker, "exclude_track_keys"):
        options["exclude_track_keys"] = list(exclude)
    try:
        # 通過側は日本では常に右(+X)。detect_overtaking.py の side 列はカメラの
        # 見え方(front=left/rear=right)を表しており物理的な通過側ではないため、
        # トラッカーへは渡さない。
        result = tracker(pcap, first, last, side="right", **options) or {}
    except Exception as exc:  # noqa: BLE001 - 1候補の失敗で止めない
        report.warn(f"候補 {event_id}: 点群の自動追跡でエラー: {exc}")
        report.manual(f"候補 {event_id}: 点群の自動追跡に失敗したため手動選択が必要です")
        return None
    result["first_frame"], result["last_frame"] = first, last
    return result


# ── 段階4b: LiDAR発の「未確認」行 ────────────────────────────────────────────

def _frame_time(pcap: VelodynePcap, index, default: float = 0.0) -> float:
    """フレーム番号を pcap 相対秒に直す（範囲外なら ``default``）。"""
    try:
        return float(pcap.frames[int(index)].timestamp)
    except (AttributeError, IndexError, TypeError, ValueError):
        return default


def build_lidar_extra_event(event_id: str, orphan: dict, result: dict,
                            pcap: VelodynePcap, offset: float, source: dict) -> dict:
    """割り当て先の無い車 1 台分の「未確認」行を作る。

    列は動画由来の候補行（``source``）と同じ構成にそろえる。列がずれると
    ``save_reviewed_events`` の見出しと値が食い違い、GUI と最終Excelが壊れるため、
    値は入れ替えても**キーは足さない**（``lidar_extra`` は保存時に無視される内部印）。

    時刻は動画側の秒（``t_start_s`` など）で持つ規約なので、追跡できたフレームの
    pcap 相対秒を ``pcap時刻 = gopro時刻 + offset`` の定義で動画時刻へ戻す。
    車種は目視で決めるものなので空にする。
    """
    frames = list(result.get("frames") or [])
    start_s = _frame_time(pcap, frames[0]["index"]) - offset if frames else 0.0
    end_s = _frame_time(pcap, frames[-1]["index"]) - offset if frames else 0.0
    pass_time = result.get("pass_time")
    peak_s = (float(pass_time) - offset) if pass_time is not None else (start_s + end_s) / 2
    # 動画ファイル内の時刻は「見つかった窓の候補」と同じ動画・同じずれで換算できる
    local_delta = _event_seconds(source, "local_t_start_s") - _event_seconds(source, "t_start_s")
    clearance = orphan.get("lc_estimate")
    row = {key: "" for key in source}
    row.update({
        "event_id": event_id,
        "class": "",
        "detector_class": "",
        "side": source.get("side", ""),
        "t_start_s": round(start_s, 2),
        "peak_t_s": round(peak_s, 2),
        "t_end_s": round(end_s, 2),
        "duration_s": round(max(0.0, end_s - start_s), 2),
        "source_video": source.get("source_video", ""),
        "local_t_start_s": round(start_s + local_delta, 2),
        "local_t_end_s": round(end_s + local_delta, 2),
        "class_reviewed": False,
        "danger_level": "0",
        "review_status": LIDAR_EXTRA_STATUS,
        "review_reason": (
            f"動画側に対応する検出候補がありません（LiDAR {float(pass_time or 0.0):.1f}s / "
            f"離隔距離の目安 {clearance}m）。車列の2台目など動画検出の取りこぼしの"
            "可能性があります"
        ),
        # 自動レビューへ「これはLiDAR発の行」と伝える内部印。レビューCSVの列は
        # 候補行から作るため、このキーは書き出されない。
        "lidar_extra": True,
    })
    return row


def scan_lidar_extra(tracker, pcap: VelodynePcap, config: PipelineConfig,
                     orphans: list[dict], events_by_id: dict[str, dict],
                     offset: float, report: PipelineReport
                     ) -> tuple[list[dict], dict[str, dict]]:
    """割り当て先の無い車を追跡し直し、「未確認」行と追跡結果を作る。

    その車が見つかった追跡窓をもう一度走査し、期待通過時刻にその車の通過時刻を
    渡して同じトラックを選ばせる。狙いと違う車が返った場合（候補一覧を返さない
    旧プラグインなど）は起票しない。起票できた車には ``orphans`` の要素へ
    ``issued_as``（振った候補ID）を書き込み、レポート側が対応づけられるようにする。

    戻り値は ``(未確認行の一覧, 候補ID -> 追跡結果)``。
    """
    extra_events: list[dict] = []
    extra_results: dict[str, dict] = {}
    for number, orphan in enumerate(orphans, start=1):
        source = events_by_id.get(str(orphan.get("source_event_id", "")))
        pass_time = orphan.get("pass_time")
        if source is None or pass_time is None:
            continue
        event_id = f"{LIDAR_EXTRA_PREFIX}{number:03d}"
        result = _scan_event(tracker, pcap, config, source, offset, report, float(pass_time))
        got = (result or {}).get("pass_time")
        if not (result or {}).get("ok") or not (result or {}).get("frames") \
                or got is None or abs(float(got) - float(pass_time)) > VEHICLE_MERGE_GAP:
            report.warn(
                f"LiDARの {float(pass_time):.1f}s の車を追跡し直せなかったため、"
                f"{event_id} として起票できませんでした"
            )
            continue
        extra_results[event_id] = result
        extra_events.append(
            build_lidar_extra_event(event_id, orphan, result, pcap, offset, source)
        )
        orphan["issued_as"] = event_id
    return extra_events, extra_results


def run_autotrack(config: PipelineConfig, pcap: VelodynePcap, events: list[dict],
                  offset: float, report: PipelineReport, tracker=None
                  ) -> tuple[dict[str, dict], list[dict]]:
    """全候補の点群を追跡し、イベントと車両を1対1に割り当てて点群CSVを書き出す。

    処理は 2 段構え。まず候補ごとに窓を走査して「その窓に居た追い越し車らしい
    トラック」を全部集め（1回目）、集めた情報でイベントと車両の割り当てを決めてから、
    1回目の選択と違う車になったイベントだけ選び直す（2回目）。こうすると、車列で
    追跡窓が重なっても同じ車が複数イベントへ出力されず、2台目も拾える。

    割り当て先の無い車（動画検出の取りこぼし候補）は、``--no-lidar-extra`` を
    指定しない限り ``L001`` … として点群CSVまで書き出し、レビューCSVへ「未確認」の
    行として起票する（最終Excelは採用行だけなので、そこへは出ない）。

    戻り値は ``(tracking, LiDAR発の未確認行)``。``tracking`` は
    ``event_id -> {"ok", "confidence", "pass_time", "track_key", "lc_estimate",
    "path", "reason"}`` で、段階6の自動レビューがこれを使う。
    """
    if tracker is None:
        tracker, load_error = load_autotracker()
    else:
        load_error = ""

    if tracker is None:
        report.stage(
            "autotrack", "4.点群追跡", "skipped",
            f"lidar_autotrack が利用できません（{load_error}）", exported=[],
        )
        report.warn(f"点群の自動追跡モジュールがありません: {load_error}")
        for event in events:
            report.manual(
                f"候補 {event.get('event_id', '?')}: 点群の自動追跡ができないため"
                "GUIで手動選択が必要です"
            )
        return {}, []

    ordered = sorted(events, key=lambda event: _event_seconds(event, "peak_t_s"))
    # 撮影方向が確定していないときは、向き依存の丸め（pass_anchor）を効かせない
    anchor_view = "" if config.view_source == "fallback" else config.view
    raw_expected = expected_pass_times(ordered, offset, view=anchor_view)

    # 1回目: 窓ごとに走査して候補を集める
    results: dict[str, dict] = {}
    candidate_sets: dict[str, list[dict]] = {}
    for event in ordered:
        event_id = str(event.get("event_id", ""))
        result = _scan_event(tracker, pcap, config, event, offset, report,
                             raw_expected.get(event_id))
        if result is None:
            continue
        results[event_id] = result
        candidate_sets[event_id] = list(result.get("candidates") or [])

    if not any(candidate_sets.values()):
        # 候補一覧を返さない旧プラグイン。割り当ては組めないので走査結果をそのまま使う
        # （同じ車の重複は _export_tracks の track_key チェックだけで防ぐ）。
        assignment = {
            event_id: {"track_key": result.get("track_key"), "vehicle": index,
                       "pass_time": result.get("pass_time"), "lc_estimate": None,
                       "score": None, "delta": 0.0}
            for index, (event_id, result) in enumerate(results.items())
            if result.get("ok")
        }
        return _export_tracks(config, pcap, ordered, results, assignment,
                              raw_expected, report), []

    # 期待通過時刻の系統ずれを測り、それを補正した時刻で割り当てる
    bias = estimate_pass_bias(raw_expected, candidate_sets)
    expected = {event_id: want + bias for event_id, want in raw_expected.items()}
    assignment = assign_events(expected, candidate_sets)
    if bias:
        report.info["期待通過時刻の系統ずれ(秒)"] = round(bias, 2)
    dump_assignment_debug(config.out_dir / "lidar" / "autotrack_candidates.json",
                          offset, bias, ordered, results, raw_expected, expected,
                          candidate_sets, assignment)
    # 割り当て先の無い車（動画検出の取りこぼし候補）を洗い出し、必要なら起票する
    orphans = unassigned_vehicles(candidate_sets, assignment, config.max_lane_clearance)
    events_by_id = {str(event.get("event_id", "")): event for event in ordered}
    extra_events: list[dict] = []
    extra_results: dict[str, dict] = {}
    if orphans and config.lidar_extra:
        extra_events, extra_results = scan_lidar_extra(
            tracker, pcap, config, orphans, events_by_id, offset, report
        )

    # 2回目: 1回目と違う車になったイベントだけ、先に選ばれる車を除外して選び直す
    for event in ordered:
        event_id = str(event.get("event_id", ""))
        target = assignment.get(event_id)
        result = results.get(event_id)
        if result is None:
            continue
        if target is None:
            result["ok"] = False
            result.setdefault("warnings", []).append(
                "追跡窓の中に見つかった車はすべて他のイベントへ割り当てられました"
                if candidate_sets.get(event_id) else
                "追跡窓の中に追い越し車らしいトラックがありませんでした"
            )
            continue
        if result.get("track_key") == target["track_key"]:
            continue
        exclude = keys_other_than(candidate_sets[event_id], target["track_key"])
        retry = _scan_event(tracker, pcap, config, event, offset, report,
                            expected.get(event_id), exclude)
        if retry is not None:
            results[event_id] = retry

    tracking = _export_tracks(config, pcap, ordered, results, assignment, expected,
                              report, extra_events, extra_results)
    # 点群CSVまで書き出せた起票だけを「起票しました」と報告する
    issued = {str(row["event_id"]) for row in extra_events
              if (tracking.get(str(row["event_id"])) or {}).get("ok")}
    _report_unassigned_vehicles(orphans, issued, offset, report)
    return tracking, [row for row in extra_events if str(row["event_id"]) in issued]


def frame_centers(frames: list[dict]) -> dict[int, tuple[float, float]]:
    """矩形列を ``フレーム番号 -> 中心座標`` にする（同一車両の判定に使う）。"""
    centers: dict[int, tuple[float, float]] = {}
    for frame in frames:
        bounds = frame.get("bounds") or {}
        try:
            centers[int(frame["index"])] = (
                (float(bounds["min_x"]) + float(bounds["max_x"])) / 2,
                (float(bounds["min_y"]) + float(bounds["max_y"])) / 2,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return centers


def same_vehicle(left: dict[int, tuple[float, float]], right: dict[int, tuple[float, float]],
                 distance: float = SAME_VEHICLE_DISTANCE,
                 min_frames: int = SAME_VEHICLE_MIN_FRAMES) -> bool:
    """2 つの矩形列が同じ車を囲っているかを軌跡の重なりで判定する。

    ``track_key`` は追跡窓が違うとぶれうるため、最後の重複チェックは軌跡そのもので
    行う。同じフレームで矩形の中心が ``distance`` m 以内に居るフレームが過半数を
    占めれば同じ車とみなす。
    """
    shared = set(left) & set(right)
    if len(shared) < min_frames:
        return False
    near = sum(
        1 for index in shared
        if math.dist(left[index], right[index]) <= distance
    )
    return near >= len(shared) * 0.6


def _report_unassigned_vehicles(orphans: list[dict], issued_ids: set[str],
                                offset: float, report: PipelineReport) -> list[dict]:
    """割り当て先の無い追い越し車を、動画検出の取りこぼしとしてレポートへ挙げる。

    ``scan_lidar_extra`` が起票できた車には ``issued_as`` が入っているので、その
    候補IDを人手項目に添えて、どの行を確認すればよいかが分かるようにする。
    ``issued_ids`` は実際に点群CSVまで書き出せた候補IDの集合。
    """
    if not orphans:
        return []
    report.info["割り当て先の無い車(動画検出の取りこぼし候補)"] = len(orphans)
    for candidate in orphans:
        # pcap時刻 = gopro時刻 + offset なので、動画時刻は引き算で戻す
        video_time = candidate["pass_time"] - offset
        event_id = str(candidate.get("issued_as", ""))
        if event_id not in issued_ids:
            event_id = ""
        note = "車列の2台目など動画検出の取りこぼしの可能性があります"
        if event_id:
            note += (f"。{event_id} として「未確認」で起票したので、"
                     "クリップと点群を確認し、採用なら最終Excelへ追記してください")
        report.manual(
            f"LiDARの {candidate['pass_time']:.1f}s（動画 {video_time:.1f}s 付近）に"
            f"追い越し車がいますが、対応する検出候補がありません"
            f"（離隔距離の目安 {candidate.get('lc_estimate')}m）。" + note
        )
    return orphans


def _drop_stale_exports(config: PipelineConfig, tracking: dict[str, dict],
                        report: PipelineReport) -> None:
    """今回車両を割り当てられなかった候補の、前回実行の点群CSVを消す。

    同じ出力フォルダへ再実行したとき、古い ``overtaking_<id>.csv`` が残っていると
    段階5がそれを拾い、除外したはずの候補の解析結果が残ってしまう。LiDAR発の
    ``overtaking_L###.csv`` は起票数が実行ごとに変わる（``--no-lidar-extra`` では
    0件になる）ので、今回作らなかった番号も消す。
    """
    lidar_dir = config.out_dir / "lidar"
    edit_dir = config.out_dir / "lidar_edit"
    stale_ids = [event_id for event_id, entry in tracking.items() if not entry.get("ok")]
    if lidar_dir.is_dir():
        stale_ids += [
            _event_id_from_lidar_csv(path)
            for path in sorted(lidar_dir.glob(f"overtaking_{LIDAR_EXTRA_PREFIX}*.csv"))
            if not path.stem.endswith("_edit")
            and _event_id_from_lidar_csv(path) not in tracking
        ]
    for event_id in stale_ids:
        for stale in (lidar_dir / f"overtaking_{event_id}.csv",
                      edit_dir / f"overtaking_{event_id}_edit.csv",
                      edit_dir / f"overtaking_{event_id}_edit_analysis.xlsx"):
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:  # noqa: PERF203 - 消せなくても続行する
                report.warn(f"前回の出力を削除できません: {stale.name}: {exc}")


def _export_tracks(config: PipelineConfig, pcap: VelodynePcap, events: list[dict],
                   results: dict[str, dict], assignment: dict[str, dict],
                   expected: dict[str, float], report: PipelineReport,
                   extra_events: list[dict] | None = None,
                   extra_results: dict[str, dict] | None = None) -> dict[str, dict]:
    """割り当て済みの追跡結果を点群CSVへ書き出し、レビュー用の要約を返す。

    ``extra_events`` / ``extra_results`` は LiDAR 発の「未確認」行（``L###``）。
    重複チェックを候補行と共有したいので同じループで書き出すが、段階4の
    「候補◯件のうち◯件」の数には数えない。
    """
    lidar_dir = config.out_dir / "lidar"
    extra_events = list(extra_events or [])
    results = {**results, **(extra_results or {})}
    extra_ids = {str(event.get("event_id", "")) for event in extra_events}
    tracking: dict[str, dict] = {}
    exported: list[Path] = []
    extra_exported: list[Path] = []
    used_keys: dict[str, str] = {}
    used_paths: dict[str, dict[int, tuple[float, float]]] = {}

    for event in list(events) + extra_events:
        event_id = str(event.get("event_id", ""))
        result = results.get(event_id)
        entry = {"ok": False, "confidence": 0.0, "pass_time": None, "track_key": None,
                 "lc_estimate": None, "path": "", "reason": "点群を追跡できませんでした"}
        tracking[event_id] = entry
        if result is None:
            entry["reason"] = "追跡窓がPCAPの範囲外か、追跡でエラーが起きました"
            continue
        for message in result.get("warnings") or []:
            report.warn(f"候補 {event_id}: {message}")
        frames = list(result.get("frames") or [])
        if not result.get("ok") or not frames:
            entry["reason"] = "追跡窓の中に対応する追い越し車両が見つかりませんでした"
            continue

        track_key = result.get("track_key")
        # 万一同じ車が2回出てきたら後勝ちを許さず、ここで確実に止める。キーの一致だけ
        # では追跡窓のぶれを取りこぼすため、軌跡の重なりでも同一車両を検出する。
        centers = frame_centers(frames)
        duplicate = used_keys.get(track_key) if track_key else None
        if duplicate is None:
            duplicate = next(
                (other for other, other_centers in used_paths.items()
                 if same_vehicle(centers, other_centers)), None
            )
        if duplicate is not None:
            entry["reason"] = f"同じ車両を候補 {duplicate} へ割り当て済みです"
            continue
        try:
            path, frame_count, written, points = pcap.export_overtaking(
                lidar_dir, event_id, frames
            )
        except Exception as exc:  # noqa: BLE001 - 1候補の失敗で止めない
            report.warn(f"候補 {event_id}: 点群CSVを書き出せません: {exc}")
            entry["reason"] = f"点群CSVを書き出せませんでした: {exc}"
            continue

        if track_key:
            used_keys[track_key] = event_id
        used_paths[event_id] = centers
        (extra_exported if event_id in extra_ids else exported).append(path)
        target = assignment.get(event_id) or {}
        # LC の目安は実際に選ばれたトラックのものを使う（選び直しで割り当て時と
        # 別のトラックになった場合に備え、候補一覧から引き直す）
        chosen = next((candidate for candidate in result.get("candidates") or []
                       if candidate.get("track_key") == track_key), None)
        entry.update({
            "ok": True, "reason": "",
            "confidence": float(result.get("confidence") or 0.0),
            "pass_time": result.get("pass_time"),
            "track_key": track_key,
            "lc_estimate": (chosen or target).get("lc_estimate"),
            "path": str(path),
            **{key: (chosen or target).get(key) for key in LIDAR_SIZE_KEYS},
        })
        want = expected.get(event_id)
        pass_time = result.get("pass_time")
        gap = "" if want is None or pass_time is None else f" (期待との差 {pass_time - want:+.1f}s)"
        print(
            f"[track] 候補 {event_id}: フレーム {result['first_frame']}〜"
            f"{result['last_frame']} / {written}/{frame_count} フレーム, {points} 点"
            f"{gap}",
            flush=True,
        )
        if written < frame_count:
            report.warn(
                f"候補 {event_id}: {frame_count} フレーム中 {written} フレームしか点を選択できませんでした"
            )

    _drop_stale_exports(config, tracking, report)
    missing = len(events) - len(exported)
    status = "ok" if exported and not missing else ("warning" if exported else "failed")
    extra_note = (f"（ほかに割り当て先の無い車 {len(extra_exported)} 台を"
                  "「未確認」で起票）" if extra_exported else "")
    report.stage(
        "autotrack", "4.点群追跡", status,
        f"候補 {len(events)} 件のうち {len(exported)} 件に車両を割り当てました" + extra_note,
        exported=[str(p) for p in exported], failed=missing,
        lidar_extra=[str(p) for p in extra_exported],
        assigned={event_id: value["track_key"] for event_id, value in tracking.items()
                  if value["ok"]},
    )
    return tracking


# ── 段階5: edit CSV ───────────────────────────────────────────────────────────

def _event_id_from_lidar_csv(path: Path) -> str:
    """``overtaking_<id>.csv`` から候補IDを取り出す。"""
    return path.stem[len("overtaking_"):]


def run_edit_csv(config: PipelineConfig, report: PipelineReport) -> dict[str, dict]:
    """GUIの generate_all_edit_csv 相当。out/lidar_edit/ へ出力する。

    戻り値は ``event_id -> {"path", "analysis_path", "metrics", "error"}``。
    ``metrics`` は ``edit_csv.analyze_edit_points`` の結果で、段階6の自動レビューが
    離隔距離 LC の判定に使う。
    """
    lidar_dir = config.out_dir / "lidar"
    targets = [
        path for path in sorted(lidar_dir.glob("overtaking_*.csv"))
        if not path.stem.endswith("_edit")
    ] if lidar_dir.is_dir() else []
    if not targets:
        report.stage("edit", "5.edit CSV", "skipped", "対象の overtaking_*.csv がありません")
        report.manual("out/lidar/ に点群CSVがありません。GUIで点群を選択・出力してください")
        return {}

    edit_dir = config.out_dir / "lidar_edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    generated: dict[str, dict] = {}
    failed = 0
    for target in targets:
        event_id = _event_id_from_lidar_csv(target)
        try:
            path = move_edit_csv_to_dir(write_edit_csv(target), edit_dir)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で止めない
            failed += 1
            report.warn(f"{target.name}: edit.csvを生成できません: {exc}")
            report.manual(f"{target.name}: edit.csvを生成できないため手動確認が必要です")
            continue
        entry: dict = {"source": str(target), "path": str(path), "metrics": None}
        try:
            entry["metrics"] = analyze_edit_points(read_edit_points(path))
            entry["analysis_path"] = str(write_edit_analysis_xlsx(path))
        except Exception as exc:  # noqa: BLE001 - 解析だけの失敗は警告に留める
            entry["error"] = str(exc)
            report.warn(f"{path.name}: 解析Excelを生成できません: {exc}")
            report.manual(
                f"{path.name}: フェーズ判定ができませんでした。点群の選択範囲を手動で見直してください"
            )
        generated[event_id] = entry

    analysis_failed = sum(1 for entry in generated.values() if entry.get("error"))
    status = "ok" if generated and not failed and not analysis_failed else \
        ("warning" if generated else "failed")
    report.stage(
        "edit", "5.edit CSV", status,
        f"{len(generated)}/{len(targets)} 件のedit.csvを生成しました"
        f"（解析Excel失敗 {analysis_failed} 件）",
        generated=[{k: v for k, v in entry.items() if k != "metrics"}
                   for entry in generated.values()],
        failed=failed,
    )
    return generated


# ── 段階6a: 点群の寸法で車種を決め直す ──────────────────────────────────────

def lidar_vehicle_class(track: dict,
                        large_side_area: float = VEHICLE_LARGE_SIDE_AREA,
                        kei_length: float = VEHICLE_KEI_MAX_LENGTH,
                        kei_width: float = VEHICLE_KEI_MAX_WIDTH,
                        min_length: float = VEHICLE_MIN_LENGTH,
                        min_width: float = VEHICLE_MIN_WIDTH) -> str:
    """割り当てた車の点群寸法から車種を返す。寸法が無ければ空文字。

    ``track`` は ``_export_tracks`` が返す要約（``LIDAR_SIZE_KEYS`` を含む）で、
    ``length`` は追跡中のクラスタの Y 方向の広がりの 80 パーセンタイル（車体長）、
    ``width`` は X 方向の同値（車幅）、``height`` は地面からの上端高の同値（車高）。

    * **大型**: 側面積（車体長 × 車高）で見る。大型車は長さと高さの両方が大きい
      ので、片方だけのしきい値より素直に分かれる。
    * **軽**: 車体長・車幅が軽自動車の規格（3.40m × 1.48m）に収まる。
      規格より小さすぎる測定値は「車の全体を測れていない」ので軽とは呼ばない。
    * それ以外は **普通**。
    """
    values = [track.get(key) for key in ("length", "width", "height")]
    if not all(isinstance(value, (int, float)) for value in values):
        return ""
    length, width, height = (float(value) for value in values)
    if length * height >= large_side_area:
        return "大型"
    if min_length <= length <= kei_length and min_width <= width <= kei_width:
        return "軽"
    return "普通"


def refine_vehicle_classes(events: list[dict], tracking: dict[str, dict]) -> list[tuple[str, str, str]]:
    """動画由来の車種を、点群で測った寸法による判定へ差し替える。

    人がレビューで直した車種（``class_reviewed``）と、車種を空欄で起票する
    LiDAR 発の行はそのままにする。戻り値は ``(候補ID, 変更前, 変更後)`` の一覧。
    """
    changes: list[tuple[str, str, str]] = []
    for event in events:
        if event.get("lidar_extra"):
            continue
        if str(event.get("class_reviewed", "")).strip().lower() in {"true", "1"}:
            continue
        track = tracking.get(str(event.get("event_id", ""))) or {}
        if not track.get("ok"):
            continue
        kind = lidar_vehicle_class(track)
        before = str(event.get("class", ""))
        if kind and kind != before:
            event["class"] = kind
            changes.append((str(event.get("event_id", "")), before, kind))
    return changes


# ── 段階6の前段: 自車運動（停止・旋回）──────────────────────────────────────

def read_gps_track(path: Path) -> list[tuple[float, float, float, float]]:
    """``telemetry_gps.xlsx`` から ``(動画内秒, 緯度, 経度, 速度m/s)`` を読む。

    読み取り自体は書き出し側と同じ ``extract_telemetry.read_gps_workbook`` に任せ、
    ここでは測位できていない行（``fix`` が 2 未満）だけ落とす。自車運動の判定は
    位置から進行方位も出すので、測位状態の怪しい行は使わない。読めない場合は空を
    返し、呼び出し側は LiDAR 側の時系列へ回る。
    """
    try:
        from extract_telemetry import read_gps_workbook
    except Exception:  # noqa: BLE001 - 依存欠落でも自車運動の判定を飛ばすだけ
        return []
    return [(fix.seconds, fix.latitude, fix.longitude, fix.speed)
            for fix in read_gps_workbook(path) if fix.fix >= 2]


def read_lidar_speed(path: Path) -> list[tuple[float, float]]:
    """段階3.5が保存した自車速度の時系列を ``(PCAP相対秒, m/s)`` で読む。

    ``out/sync_series.npz`` は拡張子に反して ``tools/sync_check.save_series`` が
    書くCSV（``time_s,speed_ms,peak,prominence,ok``）。推定に失敗した行（ok=0）は
    捨てる。GPSが無いセッションでの停止判定の予備に使う。
    """
    if not path.is_file():
        return []
    series: list[tuple[float, float]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            if not int(parts[4]):
                continue
            series.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    series.sort()
    return series


def _bearing_deg(start: tuple, end: tuple) -> float:
    """2つの測位点 ``(秒, 緯度, 経度, 速度)`` を結ぶ進行方位（度）。"""
    lat1, lon1 = math.radians(start[1]), math.radians(start[2])
    lat2, lon2 = math.radians(end[1]), math.radians(end[2])
    delta = lon2 - lon1
    return math.degrees(math.atan2(
        math.sin(delta) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta),
    ))


def _distance_m(start: tuple, end: tuple) -> float:
    """2つの測位点の距離（m）。1セッションの範囲なので平面近似で足りる。"""
    lat_m = (end[1] - start[1]) * 111_320.0
    lon_m = (end[2] - start[2]) * 111_320.0 * math.cos(math.radians(start[1]))
    return math.hypot(lat_m, lon_m)


@dataclass
class EgoMotion:
    """イベント区間の自車速度と旋回の速さを引く（段階6が使う）。

    ``tracks`` は動画stemごとの GPS 測位列（動画内秒）、``lidar`` は点群から
    推定した自車速度（PCAP相対秒）。GPSを優先し、無ければ LiDAR 側を使う。
    旋回は緯度経度からしか求まらないので GPS のみ。
    """

    tracks: dict[str, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    lidar: list[tuple[float, float]] = field(default_factory=list)
    offset: float = 0.0
    sources: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return bool(self.tracks or self.lidar)

    def _gps_window(self, event: dict, margin: float) -> list[tuple]:
        """イベント区間（前後 ``margin`` 秒）に入る GPS 測位を返す。

        GPS の時刻は動画1本ごとの経過秒なので、``source_video`` と動画内時刻
        （``local_t_start_s``）の両方が揃っている候補にだけ使える。片方でも欠けて
        いる古いCSVでは空を返し、呼び出し側は LiDAR 側の時系列へ回す。0秒として
        扱うと、走り出す前の停止区間を見て「停止中」と誤判定してしまう。
        """
        stem = Path(str(event.get("source_video") or "")).stem
        rows = self.tracks.get(stem)
        if not rows:
            return []
        if not str(event.get("local_t_start_s") or "").strip():
            return []
        if not str(event.get("local_t_end_s") or "").strip():
            return []
        start = _event_seconds(event, "local_t_start_s") - margin
        end = _event_seconds(event, "local_t_end_s") + margin
        return [row for row in rows if start <= row[0] <= end]

    def speed(self, event: dict,
              margin: float = EGO_WINDOW_MARGIN) -> tuple[float | None, str]:
        """イベント区間の自車速度の中央値と、その出どころを返す。

        瞬間値ではなく中央値を使う。停止からの発進や停止直前が区間に混ざっても、
        「その区間ずっと止まっていた」ものだけが停止と判定される。
        """
        rows = self._gps_window(event, margin)
        if rows:
            return float(statistics.median(row[3] for row in rows)), "GPS"
        if self.lidar:
            start = _event_seconds(event, "t_start_s") + self.offset - margin
            end = _event_seconds(event, "t_end_s") + self.offset + margin
            speeds = [speed for seconds, speed in self.lidar if start <= seconds <= end]
            if speeds:
                return float(statistics.median(speeds)), "LiDAR自車速度"
        return None, ""

    def yaw_rate(self, event: dict, margin: float = EGO_WINDOW_MARGIN) -> float | None:
        """イベント区間で最も速い旋回（度/秒）。求められなければ ``None``。

        測位点をそのまま繋ぐと 0.5m 刻みの位置誤差で方位が荒れるので、
        ``EGO_HEADING_BASELINE`` 以上進むごとに方位を1つ測る。停止付近の方位は
        意味を持たないため ``EGO_MIN_SPEED_FOR_HEADING`` 未満の測位は使わない。
        """
        rows = [row for row in self._gps_window(event, margin)
                if row[3] >= EGO_MIN_SPEED_FOR_HEADING]
        bearings: list[tuple[float, float]] = []
        anchor = None
        for row in rows:
            if anchor is None:
                anchor = row
                continue
            if _distance_m(anchor, row) >= EGO_HEADING_BASELINE:
                bearings.append((row[0], _bearing_deg(anchor, row)))
                anchor = row
        if len(bearings) < 2:
            return None
        fastest = 0.0
        for (start, first), (end, second) in zip(bearings, bearings[1:]):
            span = end - start
            if span <= 0:
                continue
            turn = abs((second - first + 540.0) % 360.0 - 180.0)
            fastest = max(fastest, turn / span)
        return fastest


def load_ego_motion(config: PipelineConfig, offset: float) -> EgoMotion:
    """段階2のGPSと段階3.5の自車速度から ``EgoMotion`` を組み立てる。

    どちらも出力フォルダに残っている既存の成果物なので、追加の計算はしない。
    ``--skip-gps`` と ``--skip-sync-check`` を両方指定した場合は空になり、
    段階6の自車運動による足切りは自動で省かれる。
    """
    motion = EgoMotion(offset=float(offset))
    gps_root = config.out_dir / "gps"
    if gps_root.is_dir():
        for book in sorted(gps_root.glob("*/telemetry_gps.xlsx")):
            track = read_gps_track(book)
            if track:
                motion.tracks[book.parent.name] = track
    if motion.tracks:
        motion.sources.append("GPS")
    series = read_lidar_speed(config.out_dir / "sync_series.npz")
    if series:
        motion.lidar = series
        motion.sources.append("LiDAR自車速度")
    return motion


# ── 段階6: 自動レビュー ───────────────────────────────────────────────────────

def _analysis_metric(analysis: dict | None, key: str) -> float | None:
    """edit 解析から指標を1つ取り出す（解析できていなければ ``None``）。"""
    value = ((analysis or {}).get("metrics") or {}).get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _analysis_lc(analysis: dict | None) -> float | None:
    """edit 解析から離隔距離 LC を取り出す（解析できていなければ None）。"""
    return _analysis_metric(analysis, "lc")


def _analysis_fallbacks(analysis: dict | None) -> list[str]:
    """edit 解析の警告のうち、フェーズを代用したものだけを返す。

    ``edit_csv.classify_phases`` はフェーズ2・4が見つからないとき前後の有効点で
    代用する。代用が入った解析は距離p2（＝TTCの分子）や距離p3が実測ではなく、
    値をそのまま最終Excelへ載せられない。
    """
    warnings = ((analysis or {}).get("metrics") or {}).get("warnings") or []
    return [str(note) for note in warnings if "代用" in str(note)]


def auto_review_events(events: list[dict], config: PipelineConfig,
                       tracking: dict[str, dict] | None = None,
                       analyses: dict[str, dict] | None = None,
                       ego: "EgoMotion | None" = None) -> list[dict]:
    """LiDAR の追跡結果を突き合わせて候補の採用・除外を自動判定する。

    動画だけでは誤検知と本物を切り分けられないが、点群まで通せば次を機械的に
    落とせる。判定は ``review_status`` と、理由の ``review_reason`` に入れる。

    * LiDAR 発の行（``lidar_extra``、動画側に候補が無い車） → 常に「未確認」
    * LiDAR で対応する車両が見つからない → 「除外」（誤検知か同期ずれ）
    * 自転車が停止中（自車速度 < ``config.stop_speed``） → 「除外」
    * 離隔距離 LC が ``config.max_lane_clearance`` を超える → 「除外」（隣車線の通過）
    * 離隔距離 LC が ``config.review_lane_clearance`` を超える → 「未確認」
    * フェーズ3→4で車が前進していない → 「未確認」
    * 通過区間を抜けきれていない（距離p3 < ``MIN_PASS_PROGRESS``） → 「未確認」
    * フェーズ2・4を代用している → 「未確認」（距離p2・TTCが実測でない）
    * 自転車が低速（< ``EGO_SLOW_SPEED``）または旋回中
      （> ``config.turn_yaw_rate``） → 「未確認」
    * 追跡の確信度が ``MIN_TRACK_CONFIDENCE`` 未満 → 「未確認」（人手で確認する）
    * それ以外 → 「採用」

    自車運動（停止・低速・旋回）の判定には ``ego`` を渡す。``None`` の場合や
    ``--no-ego-gate`` の場合、GPSも自車速度も無い場合はその3つを飛ばす。

    上記の「未確認」は条件の分類であって、既定（``config.keep_unconfirmed`` が
    False）では最後にすべて「除外」へ落とす（人手確認を挟まない運用）。

    危険感(``danger_level``)は全自動実行では付けない（"0" のまま＝最終Excelでは空欄）。
    ``events`` は ``web_gui.read_events_csv`` の返す行の一覧で、戻り値は
    ``review_status`` を設定した同じ行の一覧。
    """
    tracking = tracking or {}
    analyses = analyses or {}
    use_ego = bool(config.ego_gate and ego is not None and ego.available())
    for event in events:
        event_id = str(event.get("event_id", ""))
        track = tracking.get(event_id) or {}
        event["danger_level"] = str(event.get("danger_level") or "0")
        # 車線数は --lane-count の指定値を既定にする（後からGUIで候補ごとに直せる）
        event["lane_count"] = str(event.get("lane_count") or "").strip() or config.lane_count
        if event.get("lidar_extra"):
            # 動画側に候補が無い車。採否は人が決めるので理由はそのまま残す
            event["review_status"] = LIDAR_EXTRA_STATUS
            event["review_reason"] = event.get("review_reason") or (
                "動画側に対応する検出候補がありません"
            )
            continue
        if not track.get("ok"):
            event["review_status"] = "除外"
            event["review_reason"] = track.get("reason") or "LiDARで対応する車両が見つかりません"
            continue
        # 自転車が停止している間の「追い越し」は、信号待ちの横を車が流れていく状況で
        # あって解析対象の追い越しではない。静止物が自車速度で後退する前提も崩れ、
        # 前進方向の符号すら決められない（lidar_autotrack.estimate_forward_sign）ので、
        # 追跡結果と下流の計算はどちらも成り立たない。LC より先に見るのは、停止中で
        # あることのほうが理由として具体的で人が確認しやすいため。
        ego_speed, ego_source = (None, "")
        if use_ego:
            ego_speed, ego_source = ego.speed(event)
        if ego_speed is not None and ego_speed < config.stop_speed:
            event["review_status"] = "除外"
            event["review_reason"] = (
                f"自転車が停止中です（自車速度 {ego_speed:.2f} m/s・{ego_source}）。"
                "静止物が後退する前提が崩れており、通過時間・相対速度・TTCが"
                "成り立ちません"
            )
            continue
        # 実測の LC（edit 解析）を優先し、無ければ追跡時の目安値で判定する
        lc = _analysis_lc(analyses.get(event_id))
        if lc is None:
            lc = track.get("lc_estimate")
        limit = config.max_lane_clearance
        if isinstance(lc, (int, float)) and lc > limit:
            event["review_status"] = "除外"
            event["review_reason"] = (
                f"離隔距離 {lc:.2f}m が {limit}m を超えます（隣車線の通過）"
            )
            continue
        # 隣車線と言い切れないが同一車線とも言い切れない離隔距離。実測LCは申告値より
        # 広く出ることがあるので除外はせず、人が点群を見て決められるよう残す。
        if isinstance(lc, (int, float)) and lc > config.review_lane_clearance:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"離隔距離 {lc:.2f}m が同一車線の目安 {config.review_lane_clearance}m を"
                "超えます。隣車線の車を掴んでいる可能性があるため目視確認が必要です"
            )
            continue
        # フェーズ3→4で車が前進していない解析は成り立たない。通過区間の途中で追跡が
        # 切れ、代用のフェーズ4が後方の点になった場合に起きる（距離p3 が負になる）。
        # 値を採用すると最終Excelへ成り立たない通過時間・相対速度が入るので、除外は
        # せず人手確認へ回す。相対速度は車長を足すので符号の判定に使えない。
        advance = _analysis_metric(analyses.get(event_id), "distance_p3")
        if advance is not None and advance <= 0:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"フェーズ3→4で車が前進していません（距離p3 {advance:.2f} m）。"
                "通過区間を抜けるまで追えておらず、通過時間・距離p3・TTCが"
                "成り立たないため目視確認が必要です"
            )
            continue
        # 通過区間（|Y| < 3m）を端から端まで抜ければ距離p3は 6m 前後になる。
        # 途中で追跡が切れると通過時間が短く出て、相対速度・TTCがまとめて狂う。
        progress = _analysis_metric(analyses.get(event_id), "distance_p3")
        if progress is not None and progress < MIN_PASS_PROGRESS:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"通過区間を抜けきれていません（距離p3 {progress:.2f}m < "
                f"{MIN_PASS_PROGRESS}m）。追跡が途中で切れており、通過時間・"
                "相対速度・TTCが短く出るため目視確認が必要です"
            )
            continue
        fallbacks = _analysis_fallbacks(analyses.get(event_id))
        if fallbacks:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"フェーズを代用しています（{fallbacks[0]}）。距離p2・TTCが"
                "実測ではないため目視確認が必要です"
            )
            continue
        if ego_speed is not None and ego_speed < EGO_SLOW_SPEED:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"自転車が低速です（自車速度 {ego_speed:.2f} m/s・{ego_source}）。"
                "発進直後や停止直前は追い越しの前提が崩れるため目視確認が必要です"
            )
            continue
        # 曲がり角ではセンサのヨーで静止物が横へ流れ、追い越し車と見分けがつかない。
        # 自車運動補正をしていないので、旋回中の追跡結果は採用せず人手へ回す。
        yaw_rate = ego.yaw_rate(event) if use_ego else None
        if yaw_rate is not None and yaw_rate > config.turn_yaw_rate:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"自転車が旋回中です（{yaw_rate:.0f} 度/秒 > {config.turn_yaw_rate:g} 度/秒）。"
                "静止物が横へ流れて追い越し車と紛れるため目視確認が必要です"
            )
            continue
        confidence = float(track.get("confidence") or 0.0)
        if confidence < MIN_TRACK_CONFIDENCE:
            event["review_status"] = "未確認"
            event["review_reason"] = f"追跡の確信度が低い（{confidence:.2f}）ため目視確認が必要です"
            continue
        event["review_status"] = "採用"
        event["review_reason"] = ""
    # 既定では「未確認」を人手確認へ残さず、そのまま不採用（除外）にする。
    # どの条件で落ちたかは review_reason とレポートの reasons で追える。
    # 従来どおり残して人が確認する場合は --keep-unconfirmed を付ける。
    if not config.keep_unconfirmed:
        for event in events:
            if event.get("review_status") == "未確認":
                event["review_status"] = "除外"
    return events


def run_review(config: PipelineConfig, events: list[dict], tracking: dict[str, dict],
               analyses: dict[str, dict], offset: float,
               report: PipelineReport) -> list[dict]:
    changes = refine_vehicle_classes(events, tracking)
    if changes:
        report.info["点群の寸法で直した車種"] = len(changes)
    ego = load_ego_motion(config, offset) if config.ego_gate else EgoMotion()
    if config.ego_gate:
        if ego.available():
            report.info["自車運動の判定に使った時系列"] = " / ".join(ego.sources)
        else:
            report.warn(
                "GPSも自車速度の時系列も無いため、停止中・旋回中の候補を落とせません"
            )
    reviewed = auto_review_events(events, config, tracking, analyses, ego)
    counts = {status: [e for e in reviewed if e.get("review_status") == status]
              for status in ("採用", "未確認", "除外")}
    path = save_reviewed_events(config.out_dir, reviewed, offset, allow_empty=True)
    report.stage(
        "review", "6.自動レビュー", "ok" if counts["採用"] else "warning",
        f"候補 {len(reviewed)} 件のうち 採用 {len(counts['採用'])} / "
        f"未確認 {len(counts['未確認'])} / 除外 {len(counts['除外'])} 件",
        path=str(path), total=len(reviewed),
        accepted=len(counts["採用"]), unreviewed=len(counts["未確認"]),
        rejected=len(counts["除外"]),
        reasons={str(e.get("event_id", "")): e.get("review_reason", "")
                 for e in reviewed if e.get("review_status") != "採用"},
    )
    for event in counts["未確認"]:
        # LiDAR発の行は段階4で「取りこぼし候補」として既に人手項目へ挙げている
        if event.get("lidar_extra"):
            continue
        report.manual(
            f"候補 {event.get('event_id', '?')}: {event.get('review_reason', '')}"
            "（クリップと点群を確認し、採用なら最終Excelへ追記してください）"
        )
    if not counts["採用"]:
        report.warn("採用された候補がありません。時刻オフセットと検出結果を確認してください")
    return counts["採用"]


# ── 段階6.5: 車長推定 ─────────────────────────────────────────────────────────

def run_car_length(config: PipelineConfig, report: PipelineReport) -> dict:
    """「除外」以外の候補の車長を、動画の写真1枚から ChatGPT で求める（car_length.py）。

    相対速度 = 3.6 × (距離p3 + 車長) / 通過時間 に使う。車長が得られなかった候補は
    相対速度・自動車速度が空欄になるだけで、他の指標（TTC を含む）はそのまま出る。
    推定後は解析xlsx を作り直して車長と相対速度を書き込む。
    """
    if not config.car_length:
        report.stage("car_length", "6.5.車長推定", "skipped",
                     "--no-car-length が指定されました（相対速度・自動車速度は車長があるものだけ）")
        return {}
    events = target_events(read_review_events(config.out_dir))
    if not events:
        report.stage("car_length", "6.5.車長推定", "skipped", "対象の候補がありません")
        return {}
    try:
        summary = estimate_car_lengths(
            config.out_dir, events, config.view or DEFAULT_VIEW,
            log=lambda line: print(line, flush=True))
    except Exception as exc:  # noqa: BLE001 - 車長が無くても最終Excelは作る
        report.stage("car_length", "6.5.車長推定", "failed", f"予期しないエラー: {exc}")
        report.warn(f"車長を推定できません: {exc}")
        return {}
    for message in summary["messages"]:
        report.warn(message)
    for edit in sorted((config.out_dir / "lidar_edit").glob("overtaking_*_edit.csv")):
        try:
            write_edit_analysis_xlsx(edit)
        except Exception:  # noqa: BLE001 - 解析できない候補は段階5で警告済み
            pass
    lengths = load_car_lengths(config.out_dir)
    missing = sum(1 for event in events if str(event.get("event_id", "")) not in lengths)
    status = "ok" if not missing else "warning"
    message = (f"対象 {len(events)} 件: 推定 {summary['estimated']} / 既存 {summary['kept']} / "
               f"失敗 {summary['failed']} 件")
    if summary["no_key"]:
        message += "（APIキーが無いため推定していません）"
    report.stage("car_length", "6.5.車長推定", status, message,
                 path=summary["path"], estimated=summary["estimated"],
                 kept=summary["kept"], failed=summary["failed"], missing=missing)
    if missing:
        report.manual(
            f"車長が無い候補が {missing} 件あります。相対速度・自動車速度が空欄です。"
            f"{Path(summary['path']).name} の car_length_m に入力（source は manual）し、"
            "最終Excelを再生成してください"
        )
    return summary


# ── 段階7: 最終Excel ──────────────────────────────────────────────────────────

def run_final_export(config: PipelineConfig, pcap: VelodynePcap, offset: float,
                     report: PipelineReport) -> dict:
    try:
        result = generate_final_workbook(
            config.out_dir,
            organize_id=config.organize_id,
            subject_id=config.subject_id,
            collection_date=config.collection_date,
            offset=offset,
            pcap_path=str(config.pcap_path),
            pcap_start_unix=pcap.start_unix_time,
        )
    except Exception as exc:  # noqa: BLE001 - レポートは必ず残す
        report.stage("final", "7.最終Excel", "failed", str(exc))
        report.warn(f"最終Excelを生成できません: {exc}")
        return {}
    for message in result.get("warnings") or []:
        report.warn(message)
    status = "warning" if result.get("warnings") else "ok"
    report.stage(
        "final", "7.最終Excel", status,
        f"{Path(result['path']).name} に {result['rows']} 行を出力しました",
        **result,
    )
    report.manual(
        f"危険感は全自動実行では空欄です。{Path(result['path']).name} の「危険感」列へ"
        "被験者の申告値を入力してください"
    )
    return result


# ── 段階7b: 閲覧用HTMLレポート ────────────────────────────────────────────────

def run_report_html(config: PipelineConfig, report: PipelineReport) -> dict:
    """``out/report.html``（動画クリップ + 点群ビューア）を作る。

    ここまでの成果物（レビューCSV・clips・lidar・lidar_edit）だけを読むので、
    後から ``python make_report.py --out <出力先>`` で作り直すこともできる。
    クリップが残っていない場合は動画欄が「クリップなし」になるだけで、
    点群ビューだけのレポートとして成立する。
    """
    if not config.report_html:
        report.stage("report_html", "7b.閲覧用レポート", "skipped",
                     "--no-report が指定されました")
        return {}
    # 段階8（pipeline_report.json の保存）はこの後なので、実行中の値を直接渡す。
    session_info = {
        "session": config.session_dir.name,
        "subject_id": config.subject_id,
        "collection_date": config.collection_date,
        "view": config.view or DEFAULT_VIEW,
        "started_at": datetime.fromtimestamp(report.started_at).isoformat(timespec="seconds"),
    }
    try:
        result = generate_report(config.out_dir, session_info=session_info)
    except ReportError as exc:
        report.stage("report_html", "7b.閲覧用レポート", "failed", str(exc))
        report.warn(f"閲覧用レポートを生成できません: {exc}")
        return {}
    except Exception as exc:  # noqa: BLE001 - レポート生成の失敗で実行結果を失わない
        report.stage("report_html", "7b.閲覧用レポート", "failed", f"予期しないエラー: {exc}")
        report.warn(f"閲覧用レポートを生成できません: {exc}")
        return {}
    status = "ok" if result["clips"] else "warning"
    message = (
        f"{Path(result['path']).name} に {result['events']} 件"
        f"（採用 {result['accepted']} / 未確認 {result['unreviewed']}）を出力しました。"
        f"クリップ {result['clips']} 件 / 点群 {result['data_files']} 件"
    )
    if not result["clips"]:
        message += "（clips/ が無いため動画は「クリップなし」表示）"
    report.stage("report_html", "7b.閲覧用レポート", status, message, **result)
    report.info["閲覧用レポート"] = result["path"]
    print(f"[report] {result['path']} をブラウザで開くとクリップと点群を確認できます",
          flush=True)
    return result


# ── パイプライン本体 ──────────────────────────────────────────────────────────

def run_pipeline(config: PipelineConfig, tracker=None, view_detector=None,
                 sync_checker=None) -> PipelineReport:
    report = PipelineReport(config)
    config.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        resolve_view(config, report, detector=view_detector)
        events_csv = run_detection(config, report)
        run_gps_extraction(config, report)

        print(f"[pcap] {config.pcap_path.name} を読み込んでいます...", flush=True)
        pcap = VelodynePcap(config.pcap_path)
        report.info.update({
            "LiDAR": f"{pcap.model_name} / {len(pcap.frames)}フレーム / {pcap.duration:.1f}秒",
            "PCAP開始Unix時刻": pcap.start_unix_time,
        })

        offset = run_sync(config, pcap, report)
        offset = run_sync_check(config, pcap, offset, report, checker=sync_checker)
        report.info["時刻オフセット(秒)"] = offset

        events = read_events_csv(events_csv, {}, "")
        if not events:
            report.stage("review", "6.自動レビュー", "failed",
                         f"{events_csv.name} に候補がありません")
            raise PipelineError(f"追い越し候補がありません: {events_csv}")
        # 自動レビューは点群の結果で判定するため、追跡・edit CSV の後に置く
        tracking, extra_events = run_autotrack(config, pcap, events, offset, report,
                                               tracker=tracker)
        analyses = run_edit_csv(config, report)
        # LiDAR発の「未確認」行は候補行の後ろへ足す（列は候補行から作るため順序が重要）
        run_review(config, events + extra_events, tracking, analyses, offset, report)
        run_car_length(config, report)
        run_final_export(config, pcap, offset, report)
        run_report_html(config, report)
    except PipelineError as exc:
        report.stage("abort", "中断", "failed", str(exc))
        report.warn(str(exc))
        raise
    finally:
        report.stage(
            "report", "8.レポート", "ok",
            "pipeline_report.json / pipeline_report.txt",
        )
        report.save(config.out_dir)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="撮影セッションフォルダから最終Excelまでを全自動で実行する",
    )
    parser.add_argument("session", help="撮影セッションフォルダ（GOPRO/ と LiDAR/ を含む）")
    parser.add_argument("--out", default="", help="出力先（既定: <セッション>/out_auto）")
    parser.add_argument("--view", default=None, choices=["front", "rear"],
                        help="撮影方向（省略時は metadata.json、それも無ければ自動判定）")
    parser.add_argument("--view-samples", type=int, default=VIEW_SAMPLES,
                        help=f"撮影方向を自動判定するサンプル箇所数（既定: {VIEW_SAMPLES}）")
    parser.add_argument("--view-sample-seconds", type=float, default=VIEW_SAMPLE_SECONDS,
                        help=f"自動判定サンプル1箇所の長さ秒（既定: {VIEW_SAMPLE_SECONDS:.0f}）")
    parser.add_argument("--subject", default="", help="被験者名またはID（既定: セッションフォルダ名）")
    parser.add_argument("--date", default="", help="データ収集日（既定: セッションの親フォルダ名 YYMMDD）")
    parser.add_argument("--organize-id", default="", help="整理ID")
    parser.add_argument("--lane-count", default="",
                        help="道路の車線数。全候補の「車線数」列（reviewed CSV と"
                             "最終Excel）の既定値として入れる。後からGUIで候補ごとに直せる")
    parser.add_argument("--videos", nargs="+", help="GoPro動画を明示指定する（自動発見の代わり）")
    parser.add_argument("--pcap", default="", help="PCAPを明示指定する（自動発見の代わり）")
    parser.add_argument("--skip-detect", action="store_true", help="段階1の検出を行わない")
    parser.add_argument("--redetect", action="store_true", help="既存CSVがあっても検出をやり直す")
    parser.add_argument("--skip-gps", action="store_true", help="段階2のGPS抽出を行わない")
    parser.add_argument("--offset", type=float, default=None,
                        help="時刻オフセット(秒)を手動指定する（GPS自動同期の代わり）")
    parser.add_argument("--track-margin", type=float, default=None,
                        help="点群追跡の前後マージン秒。省略時は撮影方向別の非対称マージン"
                             f"（front {VIEW_TRACK_MARGINS['front']} / "
                             f"rear {VIEW_TRACK_MARGINS['rear']} = 開始側/終了側）")
    parser.add_argument("--max-lane-clearance", type=float, default=MAX_LANE_CLEARANCE,
                        help="自動レビューで隣車線の通過として除外する離隔距離(m)"
                             f"（既定: {MAX_LANE_CLEARANCE}）")
    parser.add_argument("--review-lane-clearance", type=float, default=REVIEW_LANE_CLEARANCE,
                        help="これを超える離隔距離の候補を「未確認」にする(m)"
                             f"（既定: {REVIEW_LANE_CLEARANCE}。隣車線の車を掴んだ疑い）")
    parser.add_argument("--no-ego-gate", dest="ego_gate", action="store_false",
                        default=True,
                        help="自車運動（停止・旋回）による足切りを行わない"
                             "（既定は行う。GPSもLiDAR自車速度も無ければ自動で省略）")
    parser.add_argument("--stop-speed", type=float, default=EGO_STOP_SPEED,
                        help="これ未満の自車速度は停止中とみなして除外する(m/s)"
                             f"（既定: {EGO_STOP_SPEED}）")
    parser.add_argument("--turn-yaw-rate", type=float, default=EGO_TURN_YAW_RATE,
                        help="これを超える旋回中の候補を「未確認」へ回す(度/秒)"
                             f"（既定: {EGO_TURN_YAW_RATE:g}）")
    parser.add_argument("--skip-sync-check", dest="sync_check", action="store_false",
                        default=True,
                        help="段階3.5の時刻同期の自己検証を行わない"
                             "（1セッション約3分かかる。既定は検証する）")
    parser.add_argument("--auto-offset", dest="auto_offset", action="store_true",
                        default=True,
                        help="（既定で有効。以前からの指定をそのまま受け付ける）")
    parser.add_argument("--no-auto-offset", dest="auto_offset", action="store_false",
                        help="自己検証の推定オフセットが信頼でき、現行値と"
                             f"{SYNC_CHECK_MISMATCH:g}秒超ずれていても差し替えず、"
                             "警告と人手項目だけ残す")
    parser.add_argument("--keep-unconfirmed", dest="keep_unconfirmed",
                        action="store_true", default=False,
                        help="自動レビューの「未確認」を従来どおりレビューCSVへ残す。"
                             "既定は未確認の条件（隣車線の疑い・追跡不完全・低速・旋回・"
                             "低確信度・L###行など）をすべて「除外」にする")
    parser.add_argument("--no-lidar-extra", dest="lidar_extra", action="store_false",
                        default=True,
                        help="動画側に候補の無い車をレビューCSVへ「未確認」で"
                             "起票しない（既定は起票する）")
    parser.add_argument("--no-car-length", dest="car_length", action="store_false",
                        default=True,
                        help="段階6.5の車長推定（動画の写真1枚を ChatGPT へ送る）を行わない。"
                             "車長が無い候補の相対速度・自動車速度は空欄になる")
    parser.add_argument("--no-report", dest="report_html", action="store_false",
                        default=True,
                        help="段階7bの閲覧用HTMLレポート（report.html）を作らない"
                             "（既定は作る）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        config = build_config(parse_args(argv))
    except PipelineError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    try:
        report = run_pipeline(config)
    except PipelineError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        print(f"レポート: {config.out_dir / 'pipeline_report.txt'}", file=sys.stderr)
        return 1
    print()
    print(report.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
