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
    8. レポート    out/pipeline_report.json / out/pipeline_report.txt

自動レビューは点群追跡と edit CSV の結果を使って判定するため、追跡より後に置く。
動画だけでは隣車線の車や誤検知を切り分けられないが、LiDAR の離隔距離まで求まれば
「そもそも車が見つからない」「離隔距離が隣車線相当」を機械的に落とせる。

危険感は全自動実行では空欄にする（人が後から最終Excelへ入力する）。

LiDAR には追い越し車が居るのに動画側の候補が無い（車列の2台目など検出の
取りこぼし）場合は、``L001`` … の候補IDで点群CSVまで書き出し、レビューCSVへ
``review_status="未確認"`` の行として起票する。最終Excelは採用行だけなので、
人が確認して採用に変えるまでは出力に混ざらない。``--no-lidar-extra`` で無効化できる。
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
# 追跡の確信度がこれ未満なら「未確認」にして人手確認へ回す。
MIN_TRACK_CONFIDENCE = 0.5

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
# 期待通過時刻の系統ずれを推定する際、これ以上離れた候補は外れ値として使わない（秒）。
BIAS_SAMPLE_LIMIT = 10.0
# どの候補にも割り当たらなかった車を「動画検出の取りこぼしかも」と挙げる評価値の下限。
ORPHAN_MIN_SCORE = 0.85
# 割り当て先の無い車を起票するときの候補IDの接頭辞。動画由来の候補（001, 002, …）と
# 衝突しないよう L001 から振る。行は必ず「未確認」で、最終Excel（採用のみ）には出ない。
LIDAR_EXTRA_PREFIX = "L"
LIDAR_EXTRA_STATUS = "未確認"
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
    skip_detect: bool = False
    redetect: bool = False
    skip_gps: bool = False
    offset_override: float | None = None
    # None なら view 別の非対称マージン（VIEW_TRACK_MARGINS）を使う。
    track_margin: float | None = None
    # 自動レビューで隣車線の通過とみなす離隔距離のしきい値(m)。
    max_lane_clearance: float = MAX_LANE_CLEARANCE
    # 割り当て先の無い車を LiDAR 発の「未確認」行として起票するか。
    lidar_extra: bool = True
    # 段階3.5 の時刻同期の自己検証を行うか。
    sync_check: bool = True
    # 検証で「信頼できる推定値が現行値と3秒超ずれる」ときに推定値へ差し替えるか。
    auto_offset: bool = False

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
        skip_detect=bool(args.skip_detect),
        redetect=bool(args.redetect),
        skip_gps=bool(args.skip_gps),
        offset_override=args.offset,
        track_margin=None if args.track_margin is None else float(args.track_margin),
        max_lane_clearance=float(args.max_lane_clearance),
        lidar_extra=bool(getattr(args, "lidar_extra", True)),
        sync_check=bool(getattr(args, "sync_check", True)),
        auto_offset=bool(getattr(args, "auto_offset", False)),
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
    * それ超 … レポートへ推定値を明示し人手項目にする。``--auto-offset`` が
      指定されていて推定値が信用できるなら、その場で差し替えて先へ進む

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
                     f"--auto-offset により推定値 {estimated:+.3f} 秒へ差し替えました"
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
        f"（段階3の値 {offset:+.3f} 秒）。--offset {float(estimated):.3f} を指定するか、"
        "--auto-offset を付けて再実行すると推定値で処理できます"
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


def group_vehicles(candidate_sets: dict[str, list[dict]],
                   merge_gap: float = VEHICLE_MERGE_GAP) -> list[list[tuple[str, dict]]]:
    """全候補の追跡結果を通過時刻でまとめ、実車両ごとのグループにする。

    イベントごとに追跡窓が違うため、同じ車でも走査のたびに ``track_key`` が僅かに
    ぶれうる。通過時刻が ``merge_gap`` 秒以内の候補は同じ車とみなして 1 つの
    グループにし、「1 台の車は 1 つのイベントにしか出力しない」を保証する単位に
    する。車列の 2 台目とは 4 秒以上空くので取り違えない。
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
        if groups and entry[1]["pass_time"] - groups[-1][-1][1]["pass_time"] <= merge_gap:
            groups[-1].append(entry)
        else:
            groups.append([entry])
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


def keys_before(candidates: list[dict], track_key: str) -> list[str]:
    """``candidates`` の採用順で ``track_key`` より前にあるキーを返す。

    ``track_overtaking`` は ``candidates`` を採用順に返すので、ここで返したキーを
    ``exclude_track_keys`` に渡せば、狙ったトラックが選ばれる。ただし狙った
    トラック自身と同一視されるキー（通過フレームが数フレーム・横位置が1m以内しか
    違わない別候補）は除くこと。これを渡すと追跡側が狙ったトラックごと除外して
    しまい、「候補はすべて他のイベントへ割り当て済み」になって車を失う。
    """
    keys = [candidate["track_key"] for candidate in candidates]
    if track_key not in keys:
        return []
    return [key for key in keys[:keys.index(track_key)]
            if not same_vehicle_key(key, track_key)]


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
        exclude = keys_before(candidate_sets[event_id], target["track_key"])
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


# ── 段階6: 自動レビュー ───────────────────────────────────────────────────────

def _analysis_metric(analysis: dict | None, key: str) -> float | None:
    """edit 解析から指標を1つ取り出す（解析できていなければ ``None``）。"""
    value = ((analysis or {}).get("metrics") or {}).get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _analysis_lc(analysis: dict | None) -> float | None:
    """edit 解析から離隔距離 LC を取り出す（解析できていなければ None）。"""
    return _analysis_metric(analysis, "lc")


def auto_review_events(events: list[dict], config: PipelineConfig,
                       tracking: dict[str, dict] | None = None,
                       analyses: dict[str, dict] | None = None) -> list[dict]:
    """LiDAR の追跡結果を突き合わせて候補の採用・除外を自動判定する。

    動画だけでは誤検知と本物を切り分けられないが、点群まで通せば次を機械的に
    落とせる。判定は ``review_status`` と、理由の ``review_reason`` に入れる。

    * LiDAR 発の行（``lidar_extra``、動画側に候補が無い車） → 常に「未確認」
    * LiDAR で対応する車両が見つからない → 「除外」（誤検知か同期ずれ）
    * 離隔距離 LC が ``config.max_lane_clearance`` を超える → 「除外」（隣車線の通過）
    * 追跡の確信度が ``MIN_TRACK_CONFIDENCE`` 未満 → 「未確認」（人手で確認する）
    * それ以外 → 「採用」

    危険感(``danger_level``)は全自動実行では付けない（"0" のまま＝最終Excelでは空欄）。
    ``events`` は ``web_gui.read_events_csv`` の返す行の一覧で、戻り値は
    ``review_status`` を設定した同じ行の一覧。
    """
    tracking = tracking or {}
    analyses = analyses or {}
    for event in events:
        event_id = str(event.get("event_id", ""))
        track = tracking.get(event_id) or {}
        event["danger_level"] = str(event.get("danger_level") or "0")
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
        # フェーズ3→4で車が前進していない解析は成り立たない。通過区間の途中で追跡が
        # 切れ、代用のフェーズ4が後方の点になった場合に起きる（距離p3・相対速度・TTC
        # がすべて負になる）。値を採用すると最終Excelへ TTC が負の行が入るので、
        # 除外はせず人手確認へ回す。
        speed = _analysis_metric(analyses.get(event_id), "relative_speed")
        if speed is not None and speed <= 0:
            event["review_status"] = "未確認"
            event["review_reason"] = (
                f"フェーズ3→4で車が前進していません（相対速度 {speed:.1f} km/h）。"
                "通過区間を抜けるまで追えておらず、通過時間・距離p3・TTCが"
                "成り立たないため目視確認が必要です"
            )
            continue
        confidence = float(track.get("confidence") or 0.0)
        if confidence < MIN_TRACK_CONFIDENCE:
            event["review_status"] = "未確認"
            event["review_reason"] = f"追跡の確信度が低い（{confidence:.2f}）ため目視確認が必要です"
            continue
        event["review_status"] = "採用"
        event["review_reason"] = ""
    return events


def run_review(config: PipelineConfig, events: list[dict], tracking: dict[str, dict],
               analyses: dict[str, dict], offset: float,
               report: PipelineReport) -> list[dict]:
    reviewed = auto_review_events(events, config, tracking, analyses)
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
        run_final_export(config, pcap, offset, report)
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
    parser.add_argument("--skip-sync-check", dest="sync_check", action="store_false",
                        default=True,
                        help="段階3.5の時刻同期の自己検証を行わない"
                             "（1セッション約3分かかる。既定は検証する）")
    parser.add_argument("--auto-offset", action="store_true",
                        help="自己検証の推定オフセットが信頼でき、現行値と"
                             f"{SYNC_CHECK_MISMATCH:g}秒超ずれている場合に推定値へ差し替える")
    parser.add_argument("--no-lidar-extra", dest="lidar_extra", action="store_false",
                        default=True,
                        help="動画側に候補の無い車をレビューCSVへ「未確認」で"
                             "起票しない（既定は起票する）")
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
