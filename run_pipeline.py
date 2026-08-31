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
    4. 自動レビュー 候補の採用判定 → out/reviewed_overtaking_events.csv
    5. 点群追跡    lidar_autotrack プラグインで車両を追跡 → out/lidar/overtaking_*.csv
    6. edit CSV    最近接点軌跡と解析Excel → out/lidar_edit/
    7. 最終Excel   out/final/<出力フォルダ名>_final.xlsx
    8. レポート    out/pipeline_report.json / out/pipeline_report.txt

危険感は全自動実行では空欄にする（人が後から最終Excelへ入力する）。
"""

from __future__ import annotations

import argparse
import json
import os
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
from edit_csv import write_edit_analysis_xlsx, write_edit_csv
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
# フォールバックになりやすい。5秒で追跡成功が大きく増える(5/66→15/66相当)。
DEFAULT_TRACK_MARGIN = 5.0
# 撮影方向は指定が無ければ段階0で自動判定する。判定不能のときだけこの値を仮定する
# （既定値を front / rear のどちらかに固定すると自動判定が走らなくなる点に注意）。
DEFAULT_VIEW = "front"

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
    track_margin: float = DEFAULT_TRACK_MARGIN


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
        track_margin=float(args.track_margin),
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
        # 検出をやり直さないなら方向は結果に影響しない。GPU時間を使わずに済ませる
        config.view = DEFAULT_VIEW
        config.view_source = "fallback"
        report.stage(
            "view", "0.撮影方向", "skipped",
            "検出を行わないため自動判定は不要です",
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


# ── 段階4: 自動レビュー ───────────────────────────────────────────────────────

def auto_review_events(events: list[dict], config: PipelineConfig) -> list[dict]:
    """候補の採用・除外を自動判定する。

    現段階は暫定で全候補を「採用」にする。危険感(``danger_level``)は全自動実行では
    付けない（"0" のまま＝最終Excelでは空欄）。クリップ内容を見て自動判定する実装は
    ここを差し替えることで組み込める。``events`` は ``web_gui.read_events_csv`` の
    返す行（辞書）の一覧で、戻り値は ``review_status`` を設定した同じ行の一覧。
    """
    for event in events:
        event["review_status"] = "採用"
        event["danger_level"] = str(event.get("danger_level") or "0")
    return events


def run_review(config: PipelineConfig, events_csv: Path, offset: float,
               report: PipelineReport) -> list[dict]:
    events = read_events_csv(events_csv, {}, "")
    if not events:
        report.stage("review", "4.自動レビュー", "failed", f"{events_csv.name} に候補がありません")
        raise PipelineError(f"追い越し候補がありません: {events_csv}")
    reviewed = auto_review_events(events, config)
    accepted = [e for e in reviewed if e.get("review_status") == "採用"]
    path = save_reviewed_events(config.out_dir, reviewed, offset)
    report.stage(
        "review", "4.自動レビュー", "ok",
        f"候補 {len(events)} 件のうち {len(accepted)} 件を採用しました",
        path=str(path), total=len(events), accepted=len(accepted),
    )
    report.manual(
        f"自動レビューは全候補({len(accepted)}件)を採用しています。"
        "誤検知が残っていないかクリップ(out/clips)で確認してください"
    )
    return accepted


# ── 段階5: 点群自動追跡 ───────────────────────────────────────────────────────

def load_autotracker() -> tuple[object | None, str]:
    """``lidar_autotrack.track_overtaking`` を読み込む（未実装なら None）。

    契約::

        track_overtaking(pcap: VelodynePcap, first_frame: int, last_frame: int,
                         side: str = "right") -> {
            "ok": bool,
            "frames": [{"index": int,
                        "bounds": {"min_x":.., "max_x":.., "min_y":.., "max_y":..}}],
            "confidence": float,
            "warnings": [str],
        }
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


def run_autotrack(config: PipelineConfig, pcap: VelodynePcap, accepted: list[dict],
                  offset: float, report: PipelineReport, tracker=None) -> list[Path]:
    """採用候補ごとに点群を自動追跡し、out/lidar/overtaking_<id>.csv を書き出す。"""
    if tracker is None:
        tracker, load_error = load_autotracker()
    else:
        load_error = ""
    lidar_dir = config.out_dir / "lidar"

    if tracker is None:
        report.stage(
            "autotrack", "5.点群追跡", "skipped",
            f"lidar_autotrack が利用できません（{load_error}）",
            exported=[],
        )
        report.warn(f"点群の自動追跡モジュールがありません: {load_error}")
        for event in accepted:
            report.manual(
                f"候補 {event.get('event_id', '?')}: 点群の自動追跡ができないため"
                "GUIで手動選択が必要です"
            )
        return []

    exported: list[Path] = []
    failures = 0
    for event in accepted:
        event_id = event.get("event_id", "")
        start_s = _event_seconds(event, "t_start_s") + offset - config.track_margin
        end_s = _event_seconds(event, "t_end_s") + offset + config.track_margin
        if end_s < 0 or start_s > pcap.duration:
            failures += 1
            report.warn(
                f"候補 {event_id}: 追い越し時刻 {start_s:.1f}〜{end_s:.1f}s がPCAPの"
                f"収録範囲(0〜{pcap.duration:.1f}s)から外れています"
            )
            report.manual(f"候補 {event_id}: 時刻オフセットを確認し、手動選択が必要です")
            continue
        first = pcap.nearest_frame(start_s)
        last = pcap.nearest_frame(end_s)
        try:
            result = tracker(pcap, first, last, side=str(event.get("side") or "right"))
        except Exception as exc:  # noqa: BLE001 - 1候補の失敗で止めない
            failures += 1
            report.warn(f"候補 {event_id}: 点群の自動追跡でエラー: {exc}")
            report.manual(f"候補 {event_id}: 点群の自動追跡に失敗したため手動選択が必要です")
            continue

        result = result or {}
        for message in result.get("warnings") or []:
            report.warn(f"候補 {event_id}: {message}")
        frames = list(result.get("frames") or [])
        if not result.get("ok") or not frames:
            failures += 1
            report.manual(
                f"候補 {event_id}: 点群を自動追跡できなかったため手動選択が必要です"
            )
            continue
        try:
            path, frame_count, written, points = pcap.export_overtaking(
                lidar_dir, event_id, frames
            )
        except Exception as exc:  # noqa: BLE001 - 1候補の失敗で止めない
            failures += 1
            report.warn(f"候補 {event_id}: 点群CSVを書き出せません: {exc}")
            report.manual(f"候補 {event_id}: 点群CSVの書き出しに失敗したため手動選択が必要です")
            continue
        exported.append(path)
        confidence = result.get("confidence")
        print(
            f"[track] 候補 {event_id}: フレーム {first}〜{last} / "
            f"{written}/{frame_count} フレーム, {points} 点",
            flush=True,
        )
        if written < frame_count:
            report.warn(
                f"候補 {event_id}: {frame_count} フレーム中 {written} フレームしか点を選択できませんでした"
            )
        if isinstance(confidence, (int, float)) and confidence < 0.5:
            report.manual(
                f"候補 {event_id}: 自動追跡の確信度が低いため（{confidence:.2f}）目視確認を推奨します"
            )

    status = "ok" if exported and not failures else ("warning" if exported else "failed")
    report.stage(
        "autotrack", "5.点群追跡", status,
        f"{len(exported)}/{len(accepted)} 件の点群CSVを書き出しました",
        exported=[str(p) for p in exported], failed=failures,
    )
    return exported


# ── 段階6: edit CSV ───────────────────────────────────────────────────────────

def run_edit_csv(config: PipelineConfig, report: PipelineReport) -> list[dict]:
    """GUIの generate_all_edit_csv 相当。out/lidar_edit/ へ出力する。"""
    lidar_dir = config.out_dir / "lidar"
    targets = [
        path for path in sorted(lidar_dir.glob("overtaking_*.csv"))
        if not path.stem.endswith("_edit")
    ] if lidar_dir.is_dir() else []
    if not targets:
        report.stage("edit", "6.edit CSV", "skipped", "対象の overtaking_*.csv がありません")
        report.manual("out/lidar/ に点群CSVがありません。GUIで点群を選択・出力してください")
        return []

    edit_dir = config.out_dir / "lidar_edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    generated: list[dict] = []
    failed = 0
    for target in targets:
        try:
            path = move_edit_csv_to_dir(write_edit_csv(target), edit_dir)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で止めない
            failed += 1
            report.warn(f"{target.name}: edit.csvを生成できません: {exc}")
            report.manual(f"{target.name}: edit.csvを生成できないため手動確認が必要です")
            continue
        entry = {"source": str(target), "path": str(path)}
        try:
            entry["analysis_path"] = str(write_edit_analysis_xlsx(path))
        except Exception as exc:  # noqa: BLE001 - 解析だけの失敗は警告に留める
            entry["analysis_error"] = str(exc)
            report.warn(f"{path.name}: 解析Excelを生成できません: {exc}")
            report.manual(
                f"{path.name}: フェーズ判定ができませんでした。点群の選択範囲を手動で見直してください"
            )
        generated.append(entry)

    analysis_failed = sum(1 for entry in generated if entry.get("analysis_error"))
    status = "ok" if generated and not failed and not analysis_failed else \
        ("warning" if generated else "failed")
    report.stage(
        "edit", "6.edit CSV", status,
        f"{len(generated)}/{len(targets)} 件のedit.csvを生成しました"
        f"（解析Excel失敗 {analysis_failed} 件）",
        generated=generated, failed=failed,
    )
    return generated


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

def run_pipeline(config: PipelineConfig, tracker=None, view_detector=None) -> PipelineReport:
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
        report.info["時刻オフセット(秒)"] = offset

        accepted = run_review(config, events_csv, offset, report)
        run_autotrack(config, pcap, accepted, offset, report, tracker=tracker)
        run_edit_csv(config, report)
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
    parser.add_argument("--track-margin", type=float, default=DEFAULT_TRACK_MARGIN,
                        help=f"点群追跡の前後マージン秒（既定: {DEFAULT_TRACK_MARGIN}）")
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
