#!/usr/bin/env python3
"""HTMLベースの追い越し解析デスクトップGUI。"""

from __future__ import annotations

import argparse
import csv
import inspect
import json
import mimetypes
import os
import re
import subprocess
import struct
import sys
import threading
import time
import tempfile
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from lidar_pcap import VelodynePcap
from edit_csv import write_edit_analysis_xlsx, write_edit_csv
from final_export import generate_final_workbook


APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "web_ui"
SETTINGS_PATH = APP_DIR / "gui_settings.json"
DEFAULT_OUT = APP_DIR / "output"
VEHICLE_TYPES = {"大型", "普通", "軽"}


def format_timecode(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:06.3f}"


def validate_lidar_csv_folder(folder: Path) -> tuple[int, list[str]]:
    """各CSVの9・10列目がPoints列かを簡易確認する。"""
    checked = 0
    problems: list[str] = []
    for path in sorted(folder.glob("*.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                header = next(csv.reader(stream), [])
        except (OSError, UnicodeError, csv.Error) as exc:
            problems.append(f"{path.name}: 読み込み失敗 ({exc})")
            continue
        checked += 1
        if len(header) < 10:
            problems.append(f"{path.name}: 列数が10未満です")
            continue
        col9 = header[8].lower()
        col10 = header[9].lower()
        if "point" not in col9 or "point" not in col10:
            problems.append(
                f"{path.name}: 9・10列目がPointsではありません "
                f"({header[8]!r}, {header[9]!r})"
            )
    return checked, problems


def normalize_vehicle_type(value: str) -> str:
    """Normalize detector class labels to the labels used by the review UI."""
    raw = (value or "").strip()
    if raw in VEHICLE_TYPES:
        return raw
    if raw.lower() in {"bus", "truck"}:
        return "大型"
    if raw.lower() in {"kei", "light"}:
        return "軽"
    return "普通"


def read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for key in ("video", "pcap"):
        if data.get(key) and not Path(str(data[key])).is_file():
            data[key] = ""
    for key in ("out",):
        if data.get(key) and not Path(str(data[key])).is_dir():
            data[key] = ""
    if isinstance(data.get("videos"), list):
        data["videos"] = [v for v in data["videos"] if v and Path(str(v)).is_file()]
    return data


def write_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def output_dir_from_payload(payload: dict) -> Path:
    value = str(payload.get("out") or "").strip()
    out_dir = Path(value).expanduser() if value else DEFAULT_OUT
    if not out_dir.is_absolute():
        out_dir = APP_DIR / out_dir
    return out_dir


def lidar_dir_from_payload(payload: dict) -> Path:
    """LiDAR出力は常に検出出力フォルダ配下 <out>/lidar に統一する。"""
    lidar_dir = output_dir_from_payload(payload) / "lidar"
    lidar_dir.mkdir(parents=True, exist_ok=True)
    return lidar_dir


def edit_dir_from_payload(payload: dict) -> Path:
    edit_dir = output_dir_from_payload(payload) / "lidar_edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    return edit_dir


def move_edit_csv_to_dir(path: Path, edit_dir: Path) -> Path:
    dest = edit_dir / path.name
    if path.resolve() != dest.resolve():
        path.replace(dest)
    return dest


def prepare_output_dir(payload: dict) -> Path:
    out_dir = output_dir_from_payload(payload)
    # 保存先（親フォルダ）が存在しない場所を指定していると、解析後にエラーとなり
    # 処理が初期化されてしまうため、ここで存在を確認する。
    parent = out_dir.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"出力フォルダの保存先が存在しません: {parent}")
    out_dir.mkdir(exist_ok=True)
    return out_dir


def detection_base_dir_from_payload(payload: dict) -> Path:
    base_dir = output_dir_from_payload(payload)
    if re.fullmatch(r"\d{6}_\d+", base_dir.name) and base_dir.parent.is_dir():
        return base_dir.parent
    return base_dir


def prepare_detection_output_dir(payload: dict) -> Path:
    base_dir = detection_base_dir_from_payload(payload)
    parent = base_dir.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"出力フォルダの保存先が存在しません: {parent}")
    base_dir.mkdir(exist_ok=True)

    prefix = time.strftime("%y%m%d")
    number = 1
    while True:
        out_dir = base_dir / f"{prefix}_{number}"
        if not out_dir.exists():
            out_dir.mkdir()
            return out_dir
        number += 1


def detection_python() -> Path:
    portable_python = APP_DIR / "portable" / "python" / "python.exe"
    if portable_python.is_file():
        return portable_python
    return Path(sys.executable)


def read_events_csv(csv_path: Path, media_registry: dict[str, Path], base_url: str) -> list[dict]:
    """1つのCSVファイルを読み込む。

    ``review_status`` 列を持つ（レビュー済み）CSVは採用・除外と車種をそのまま適用し、
    それ以外（通常の検出CSV）は全件「未確認」で読み込む。クリップは同階層の
    ``clips/`` から解決する。
    """
    if not csv_path.is_file():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        reviewed = "review_status" in (reader.fieldnames or [])
        rows = list(reader)
    clips_dir = csv_path.parent / "clips"
    for row in rows:
        raw_class = row.get("class", "")
        row.setdefault("detector_class", raw_class)
        row["class"] = normalize_vehicle_type(raw_class)
        if reviewed:
            row["class_reviewed"] = str(row.get("class_reviewed", "")).lower() in {
                "1", "true", "yes"
            }
            row["review_status"] = row.get("review_status") or "未確認"
        else:
            row["class_reviewed"] = False
            row["review_status"] = "未確認"
        row["danger_level"] = str(row.get("danger_level") or "0")
        clip = clips_dir / f"event_{row.get('event_id', '')}_{row.get('side', '')}.mp4"
        if clip.is_file():
            token = uuid.uuid4().hex
            media_registry[token] = clip.resolve()
            row["clip_url"] = f"{base_url}/media/{token}"
        else:
            row["clip_url"] = ""
    return rows


def save_reviewed_events(out_dir: Path, events: list[dict], offset: float,
                         allow_empty: bool = False) -> Path:
    if not events:
        raise ValueError("保存する追い越し候補がありません")
    # 「除外」にした候補はレビュー結果CSVに出力しない
    kept = [e for e in events if str(e.get("review_status", "")) != "除外"]
    if not kept and not allow_empty:
        raise ValueError("除外していない追い越し候補がありません")
    # allow_empty（全自動実行）では全件除外でも見出しだけのCSVを残す。これが無いと
    # 下流の final_export が検出CSVへフォールバックし、除外した候補まで最終Excelへ出る。
    fields_source, events = events[0], kept
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reviewed_overtaking_events.csv"
    ignored = {"clip_url", "veloview_display"}
    fields = [key for key in fields_source if key not in ignored]
    for key in ("review_status", "veloview_t_start", "veloview_peak_t", "veloview_t_end"):
        if key not in fields:
            fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for source in events:
            row = {key: value for key, value in source.items() if key not in ignored}
            row["veloview_t_start"] = format_timecode(float(row.get("t_start_s", 0)) + offset)
            row["veloview_peak_t"] = format_timecode(float(row.get("peak_t_s", 0)) + offset)
            row["veloview_t_end"] = format_timecode(float(row.get("t_end_s", 0)) + offset)
            writer.writerow(row)
    return path


# ── 点群の自動追跡（半自動モード） ───────────────────────────────────────────
# 追跡窓のマージン（秒）。run_pipeline.VIEW_TRACK_MARGINS と同じ値。run_pipeline が
# web_gui を import しているため循環を避けてここに複製し、値のずれは run_check で
# run_pipeline.py の記述と突き合わせて検出する。
AUTOTRACK_VIEW_MARGINS = {"front": (8.0, 3.0), "rear": (3.0, 8.0)}
# 撮影方向が未確定のときは、非対称マージンの向きを誤ると接近または離脱の区間が
# 窓から切れるため、両側とも広い方の値で対称に取る。
AUTOTRACK_FALLBACK_MARGIN = 8.0
# 進捗バーの目安に使う所要時間の見積り。track_overtaking は進捗を通知しないので、
# 「窓のフレーム数 × 1フレームあたりの実測秒」で残り時間の目安だけを出す。
# 実測（ID1の候補001〜005、120〜202フレームの窓）は 33〜47ms/フレームだった。
AUTOTRACK_SECONDS_PER_FRAME = 0.05
AUTOTRACK_MIN_ESTIMATE = 3.0


def autotrack_margins(view: str) -> tuple[float, float]:
    """撮影方向から追跡窓の (開始側, 終了側) マージン秒を返す。"""
    return AUTOTRACK_VIEW_MARGINS.get(
        str(view or "").strip().lower(),
        (AUTOTRACK_FALLBACK_MARGIN, AUTOTRACK_FALLBACK_MARGIN),
    )


def event_seconds(event: dict, key: str, fallback_key: str = "peak_t_s") -> float:
    """候補CSVの秒フィールドを取り出す（空欄なら fallback_key）。app.js と同じ規則。"""
    for name in (key, fallback_key):
        raw = event.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def autotrack_window(event: dict, offset: float, view: str) -> tuple[float, float, float]:
    """候補1件の (追跡窓の開始秒, 終了秒, 期待通過時刻) を pcap 相対秒で返す。

    ``offset`` は ``pcap時刻 = gopro時刻 + offset``（Step 3 のGPS同期で求まる値）。
    """
    start_margin, end_margin = autotrack_margins(view)
    start_s = event_seconds(event, "t_start_s") + offset - start_margin
    end_s = event_seconds(event, "t_end_s") + offset + end_margin
    if end_s < start_s:
        start_s, end_s = end_s, start_s
    expected = event_seconds(event, "peak_t_s", "t_start_s") + offset
    return start_s, end_s, expected


def load_autotracker():
    """``lidar_autotrack.track_overtaking`` を遅延importする。

    GUI起動時に numpy などを巻き込まないよう、また当モジュールが欠けていても
    手動フローは動かせるよう、import は使う直前に行い失敗は文字列で返す。
    """
    try:
        from lidar_autotrack import track_overtaking
    except Exception as exc:  # noqa: BLE001 - 壊れていてもGUIは動かし続ける
        return None, f"lidar_autotrack を読み込めません: {exc}"
    return track_overtaking, ""


def load_view_detector():
    """``detect_view.detect_view`` を遅延importする（cv2を起動時に巻き込まない）。"""
    try:
        from detect_view import DetectViewError, detect_view
    except Exception as exc:  # noqa: BLE001 - 判定が使えなくても手動選択で進める
        return None, None, f"detect_view を読み込めません: {exc}"
    return detect_view, DetectViewError, ""


def estimate_autotrack_seconds(first_frame: int, last_frame: int) -> float:
    """自動追跡の所要時間の目安（秒）。進捗バーの分母にだけ使う。"""
    frames = max(0, int(last_frame) - int(first_frame) + 1)
    return round(max(AUTOTRACK_MIN_ESTIMATE, frames * AUTOTRACK_SECONDS_PER_FRAME), 1)


class TaskCancelled(Exception):
    """利用者が中止した（協調中断できる処理の脱出用）。"""


class BackgroundTask:
    """進捗表示と中止に対応した単発ジョブの状態。

    pywebview は js_api の呼び出しごとに別スレッドを起こすので、重い処理は
    呼び出しスレッドでそのまま実行し、GUIは別の呼び出しで ``snapshot()`` を
    ポーリングして経過秒を表示する。``cancel()`` は中断フラグを立てるだけで、
    協調中断の口が無い処理（``track_overtaking``）は最後まで走ってしまうが、
    ``finish()`` が False を返すので結果は破棄され、GUIは即座に解放できる。
    協調中断できる処理（``detect_view`` の progress コールバック）は
    ``raise_if_cancelled()`` でその場で抜ける。
    """

    def __init__(self, label: str = "") -> None:
        self._lock = threading.Lock()
        self._label = label
        self._token = 0
        self._running = False
        self._cancelled = False
        self._started = 0.0
        self._elapsed = 0.0
        self._message = ""
        self._detail: dict = {}

    def start(self, message: str = "", detail: dict | None = None) -> int:
        """ジョブを開始してトークンを返す。すでに実行中なら 0 を返す。"""
        with self._lock:
            if self._running:
                return 0
            self._token += 1
            self._running = True
            self._cancelled = False
            self._started = time.monotonic()
            self._elapsed = 0.0
            self._message = message
            self._detail = dict(detail or {})
            return self._token

    def note(self, message: str = "", **detail) -> None:
        """進捗メッセージ・付随情報を更新する。"""
        with self._lock:
            if message:
                self._message = str(message)
            self._detail.update(detail)

    def cancelled(self, token: int = 0) -> bool:
        with self._lock:
            if token and int(token) != self._token:
                return True
            return self._cancelled

    def raise_if_cancelled(self, token: int = 0) -> None:
        if self.cancelled(token):
            raise TaskCancelled()

    def cancel(self, token: int = 0) -> dict:
        with self._lock:
            if not self._running:
                return {"ok": False, "running": False, "cancelled": self._cancelled,
                        "token": self._token, "error": "実行中の処理はありません"}
            if token and int(token) != self._token:
                return {"ok": False, "running": True, "cancelled": self._cancelled,
                        "token": self._token, "error": "対象の処理はすでに終了しています"}
            self._cancelled = True
            return {"ok": True, "running": True, "cancelled": True, "token": self._token}

    def finish(self, token: int) -> bool:
        """終了処理。結果を採用してよければ True（中止済みなら False）。"""
        with self._lock:
            keep = self._token == int(token) and not self._cancelled
            if self._token == int(token) and self._running:
                self._running = False
                self._elapsed = time.monotonic() - self._started
            return keep

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (time.monotonic() - self._started) if self._running else self._elapsed
            return {"ok": True, "running": self._running, "cancelled": self._cancelled,
                    "token": self._token, "label": self._label,
                    "elapsed": round(max(0.0, elapsed), 1),
                    "message": self._message, **self._detail}


def _tracker_accepts(tracker, name: str) -> bool:
    """プラグインが指定の任意引数を受け取れるか調べる（後方互換のため）。"""
    try:
        return name in inspect.signature(tracker).parameters
    except (TypeError, ValueError):
        return False


def normalize_track_frames(raw) -> list[dict]:
    """``track_overtaking`` の frames をGUI／JSON安全な形へ正規化する。"""
    frames: list[dict] = []
    for item in raw or []:
        try:
            index = int(item["index"])
            bounds = item.get("bounds") or {}
            box = {key: float(bounds[key]) for key in ("min_x", "max_x", "min_y", "max_y")}
        except (KeyError, TypeError, ValueError):
            continue
        frames.append({"index": index, "bounds": box})
    frames.sort(key=lambda frame: frame["index"])
    return frames


def autotrack_candidate_frames(pcap, event: dict, offset: float, view: str = "",
                               tracker=None, exclude_track_keys=None, progress=None) -> dict:
    """1候補分の自動追跡を実行し、記録済みフレーム機構へ流し込める矩形列を返す。

    ``tracker`` を渡すとその関数を使う（テスト用の差し替え口）。失敗時は必ず
    ``{"ok": False, "error": ...}`` を返し、呼び出し側は従来の手動フローへ戻す。
    ``progress`` を渡すと、追跡を始める直前に窓の情報（フレーム範囲と所要時間の
    目安）を1度だけ通知する。``track_overtaking`` 自体は進捗を通知しないため、
    GUI側は以降を経過時間で表示する。
    """
    if pcap is None:
        return {"ok": False, "error": "先にPCAPを開いてください"}
    if tracker is None:
        tracker, load_error = load_autotracker()
        if tracker is None:
            return {"ok": False, "error": load_error}
    start_s, end_s, expected = autotrack_window(event, offset, view)
    duration = float(getattr(pcap, "duration", 0.0) or 0.0)
    if end_s < 0 or start_s > duration:
        return {"ok": False, "error": (
            f"追跡窓 {start_s:.1f}〜{end_s:.1f}秒 がPCAPの収録範囲"
            f"(0〜{duration:.1f}秒)から外れています。時刻オフセットを確認してください"
        )}
    first = int(pcap.nearest_frame(start_s))
    last = int(pcap.nearest_frame(end_s))
    window = {"start_s": start_s, "end_s": end_s, "expected_pass_time": expected,
              "first_frame": first, "last_frame": last}
    if last <= first:
        return {"ok": False, "error": "追跡窓に含まれるフレームが足りません", **window}
    if progress is not None:
        progress({"first_frame": first, "last_frame": last,
                  "expected_seconds": estimate_autotrack_seconds(first, last)})
    options: dict = {}
    if _tracker_accepts(tracker, "expected_pass_time"):
        options["expected_pass_time"] = expected
    if exclude_track_keys and _tracker_accepts(tracker, "exclude_track_keys"):
        options["exclude_track_keys"] = [str(key) for key in exclude_track_keys if str(key)]
    try:
        # 通過側は日本では常に右(+X)。検出CSVの side 列はカメラの見え方
        # (front=left / rear=right) であって物理的な通過側ではないため渡さない。
        result = tracker(pcap, first, last, side="right", **options) or {}
    except Exception as exc:  # noqa: BLE001 - 自動追跡の失敗でGUIを固めない
        return {"ok": False, "error": f"点群の自動追跡でエラー: {exc}", **window}
    warnings = [str(item) for item in (result.get("warnings") or [])]
    frames = normalize_track_frames(result.get("frames"))
    if not result.get("ok") or not frames:
        return {"ok": False, "warnings": warnings, **window, "error":
                "追い越し車両を自動で見つけられませんでした。手動で矩形を選択してください"}
    pass_time = result.get("pass_time")
    return {
        "ok": True, "frames": frames, "warnings": warnings, **window,
        "confidence": float(result.get("confidence") or 0.0),
        "pass_time": None if pass_time is None else float(pass_time),
        "track_key": None if result.get("track_key") is None else str(result["track_key"]),
    }


class AppApi:
    def __init__(self) -> None:
        self._window = None
        self._base_url = ""
        self._media_registry: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pcap: VelodynePcap | None = None
        # 進捗表示と中止に対応する重い処理（Step4の自動追跡・Step1の撮影方向判定）
        self._autotrack_task = BackgroundTask("自動追跡")
        self._view_task = BackgroundTask("撮影方向の自動判定")
        self._state = {
            "running": False, "progress": 0, "log": "", "message": "準備完了", "exit_code": None
        }

    def attach(self, window, base_url: str) -> None:
        self._window = window
        self._base_url = base_url

    def get_settings(self) -> dict:
        settings = read_settings()
        settings.setdefault("view", "rear")
        settings.setdefault("out", str(DEFAULT_OUT))
        settings.setdefault("overlay", False)
        settings.setdefault("road_roi", False)
        settings["base_url"] = self._base_url
        return settings

    def save_settings(self, data: dict) -> dict:
        write_settings(data)
        return {"ok": True}

    def choose_path(self, kind: str) -> dict:
        if self._window is None:
            return {"ok": False, "error": "デスクトップ版でのみファイル選択できます"}
        try:
            import webview

            file_types = None
            if kind == "video":
                file_types = ("Video files (*.mp4;*.mov;*.avi;*.mkv)",)
            elif kind == "pcap":
                file_types = ("PCAP files (*.pcap)",)
            elif kind in ("edit_source", "events_csv"):
                file_types = ("CSV files (*.csv)",)
            folder_kinds = {"out"}
            if hasattr(webview, "FileDialog"):
                dialog = webview.FileDialog.FOLDER if kind in folder_kinds else webview.FileDialog.OPEN
            else:
                dialog = webview.FOLDER_DIALOG if kind in folder_kinds else webview.OPEN_DIALOG
            result = self._window.create_file_dialog(dialog, allow_multiple=False, file_types=file_types or ())
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (tuple, list)) else result
            # 結果CSVはファイルで選ばせる。読み込みには選択ファイル、その他処理には親フォルダを使う
            if kind == "events_csv":
                return {"ok": True, "path": str(path), "folder": str(Path(str(path)).parent)}
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_videos(self) -> dict:
        """Pick one or more video files; return them sorted by filename."""
        if self._window is None:
            return {"ok": False, "error": "デスクトップ版でのみファイル選択できます"}
        try:
            import webview

            file_types = ("Video files (*.mp4;*.mov;*.avi;*.mkv)",)
            if hasattr(webview, "FileDialog"):
                dialog = webview.FileDialog.OPEN
            else:
                dialog = webview.OPEN_DIALOG
            result = self._window.create_file_dialog(
                dialog, allow_multiple=True, file_types=file_types
            )
            if not result:
                return {"ok": False, "cancelled": True}
            paths = list(result) if isinstance(result, (tuple, list)) else [result]
            paths.sort(key=lambda p: Path(p).name.lower())
            return {"ok": True, "paths": [str(p) for p in paths]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _payload_videos(self, payload: dict) -> list[Path]:
        raw = payload.get("videos")
        if not raw:
            single = payload.get("video")
            raw = [single] if single else []
        return [Path(str(v)).expanduser() for v in raw if str(v).strip()]

    def start_detection(self, payload: dict) -> dict:
        videos = self._payload_videos(payload)
        if not videos:
            return {"ok": False, "error": "GoPro動画を選択してください"}
        for video in videos:
            if not video.is_file():
                return {"ok": False, "error": f"動画が見つかりません: {video}"}
        with self._lock:
            if self._process and self._process.poll() is None:
                return {"ok": False, "error": "検出処理はすでに実行中です"}
        try:
            out_dir = prepare_detection_output_dir(payload)
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"出力フォルダを準備できません: {exc}"}
        cmd = [
            str(detection_python()), str(APP_DIR / "detect_overtaking.py"),
            *[str(video) for video in videos],
            "--view", str(payload.get("view", "rear")), "--out", str(out_dir),
            "--clip", "--imgsz", "640", "--batch", "32",
            "--flow-stride", "3", "--prefetch-batches", "2",
            "--decoder", "auto",
        ]
        if payload.get("overlay"):
            cmd.append("--overlay")
        if payload.get("road_roi"):
            cmd.append("--road-roi")
        with self._lock:
            self._state = {
                "running": True, "progress": 0, "log": "$ " + subprocess.list2cmdline(cmd) + "\n",
                "message": "動画を解析しています", "exit_code": None,
            }
        saved_settings = dict(payload)
        saved_settings["out"] = str(out_dir)
        write_settings(saved_settings)
        threading.Thread(target=self._detection_worker, args=(cmd,), daemon=True).start()
        threading.Thread(target=self._gps_worker, args=(videos, out_dir), daemon=True).start()
        return {"ok": True, "out": str(out_dir)}

    def _gps_worker(self, videos: list[Path], out_dir: Path) -> None:
        try:
            from extract_telemetry import extract_telemetry, save_gps_workbook
        except Exception as exc:
            with self._lock:
                self._state["log"] += f"[gps] GPS抽出を初期化できません: {exc}\n"
            return

        gps_root = out_dir / "gps"
        gps_root.mkdir(exist_ok=True)
        for video in videos:
            with self._lock:
                self._state["log"] += f"[gps] {video.name} のGoPro GPSを抽出中...\n"
            try:
                streams = extract_telemetry(video)
                video_out = gps_root / video.stem
                video_out.mkdir(exist_ok=True)
                gps_book = save_gps_workbook(streams, video_out)
                if gps_book:
                    message = f"[gps] {video.name}: {gps_book.relative_to(out_dir)} を保存しました\n"
                else:
                    message = f"[gps] {video.name}: GPS5データが見つかりませんでした\n"
            except Exception as exc:
                message = f"[gps] {video.name}: GPS抽出に失敗しました: {exc}\n"
            with self._lock:
                self._state["log"] += message

    def _detection_worker(self, cmd: list[str]) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        yolo_config_dir = APP_DIR / ".ultralytics"
        yolo_config_dir.mkdir(exist_ok=True)
        env["YOLO_CONFIG_DIR"] = str(yolo_config_dir)
        try:
            self._process = subprocess.Popen(
                cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=creationflags,
                env=env,
            )
            assert self._process.stdout is not None
            # 進捗率は動画ごと（0〜99%）に表示し、メッセージで何本目を解析中かを示す。
            for line in self._process.stdout:
                with self._lock:
                    self._state["log"] += line
                video_match = re.search(r"=== 動画\s+(\d+)/(\d+)", line)
                if video_match:
                    with self._lock:
                        self._state["message"] = (
                            f"動画 {video_match.group(1)}/{video_match.group(2)} を解析しています"
                        )
                        # 次の動画に切り替わったら進捗を0%からやり直す
                        self._state["progress"] = 0
                    continue
                match = re.search(r"processing frame\s+(\d+)/(\d+)", line)
                if match and int(match.group(2)):
                    with self._lock:
                        self._state["progress"] = min(99, round(int(match.group(1)) / int(match.group(2)) * 100))
            code = self._process.wait()
        except Exception as exc:
            with self._lock:
                self._state["log"] += f"[GUI ERROR] {exc}\n"
            code = 1
        with self._lock:
            self._state["running"] = False
            self._state["exit_code"] = code
            self._state["progress"] = 100 if code == 0 else self._state["progress"]
            self._state["message"] = "検出が完了しました" if code == 0 else "検出中にエラーが発生しました"

    def get_detection_state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def load_events(self, out_value: str) -> dict:
        """フォルダ内の overtaking_events.csv を通常CSVとして読み込む（検出直後用）。"""
        out_dir = Path(out_value or DEFAULT_OUT)
        return self._load_events(out_dir / "overtaking_events.csv")

    def load_events_file(self, file_value: str) -> dict:
        """選択されたCSVを読み込む。reviewed_*なら採用・除外を適用、通常CSVはそのまま。"""
        return self._load_events(Path(str(file_value or "")))

    def _load_events(self, csv_path: Path) -> dict:
        self._media_registry.clear()
        try:
            events = read_events_csv(csv_path, self._media_registry, self._base_url)
            vehicle_reanalysis_required = False
            if csv_path.is_file():
                with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                    fields = set(csv.DictReader(stream).fieldnames or [])
                # 通常の検出CSV（review_status列なし）が旧形式なら再解析を促す
                if "review_status" not in fields:
                    vehicle_reanalysis_required = not {
                        "detector_class", "kei_plate_score", "kei_plate_hits"
                    }.issubset(fields)
            return {
                "ok": True,
                "events": events,
                "vehicle_reanalysis_required": vehicle_reanalysis_required,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "events": []}

    def save_reviews(self, payload: dict) -> dict:
        try:
            path = save_reviewed_events(
                Path(str(payload.get("out") or DEFAULT_OUT)),
                list(payload.get("events") or []), float(payload.get("offset") or 0),
            )
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def validate_lidar(self, out_value: str) -> dict:
        folder = output_dir_from_payload({"out": out_value}) / "lidar"
        if not folder.is_dir():
            return {"ok": False, "checked": 0, "problems": [],
                    "error": "まだLiDAR出力がありません（出力フォルダ内 lidar/ が未作成です）"}
        checked, problems = validate_lidar_csv_folder(folder)
        return {"ok": not problems and checked > 0, "checked": checked, "problems": problems}

    def open_pcap(self, value: str) -> dict:
        path = Path(value).expanduser()
        if not path.is_file():
            return {"ok": False, "error": "LiDAR PCAPを選択してください"}
        try:
            self._pcap = VelodynePcap(path)
            return {
                "ok": True, "model": self._pcap.model_name,
                "frame_count": len(self._pcap.frames), "duration": self._pcap.duration,
                "laser_count": self._pcap.laser_count,
                "frame_times": [frame.timestamp for frame in self._pcap.frames],
            }
        except Exception as exc:
            self._pcap = None
            return {"ok": False, "error": str(exc)}

    def get_lidar_frame(self, index: int, max_points: int = 45000) -> dict:
        if self._pcap is None:
            return {"ok": False, "error": "先にPCAPを開いてください"}
        try:
            point_limit = max(2000, min(45000, int(max_points)))
            return {"ok": True, **self._pcap.frame_data(int(index), max_points=point_limit)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def seek_lidar_time(self, seconds: float) -> dict:
        if self._pcap is None:
            return {"ok": False, "error": "先にPCAPを開いてください"}
        return {"ok": True, "index": self._pcap.nearest_frame(float(seconds))}

    def auto_track_candidate(self, payload: dict) -> dict:
        """開いているPCAPで候補の車両を自動追跡し、フレーム別の矩形を返す（半自動モード）。

        GUIは返った ``frames`` を「記録済みフレーム」の初期値として取り込み、人が
        点群ビュー上で微修正してから ``export_overtaking`` で出力する。
        ``ok=False`` のときはGUI側が従来の手動フローへ戻す。

        payload::

            {"event": {"event_id", "t_start_s", "peak_t_s", "t_end_s"},
             "offset": float,           # pcap時刻 = gopro時刻 + offset
             "view": "front" | "rear",  # 未指定なら前後対称の広い窓
             "exclude_track_keys": [str]}  # 他候補へ割り当て済みの車両
        """
        payload = dict(payload or {})
        if self._pcap is None:
            return {"ok": False, "error": "先にPCAPを開いてください"}
        event = dict(payload.get("event") or {})
        if not event:
            event = {key: payload.get(key)
                     for key in ("event_id", "t_start_s", "peak_t_s", "t_end_s")}
        try:
            offset = float(payload.get("offset") or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        event_id = str(event.get("event_id", "") or "")
        token = self._autotrack_task.start(
            "点群を読み込んで車両を探しています",
            {"event_id": event_id, "expected_seconds": 0.0},
        )
        if not token:
            snapshot = self._autotrack_task.snapshot()
            return {"ok": False, "busy": True, "event_id": event_id, "error": (
                "自動追跡がまだ実行中です（"
                f"{snapshot['elapsed']:.0f}秒経過）。完了か中止をお待ちください"
            )}
        try:
            result = autotrack_candidate_frames(
                self._pcap, event, offset, str(payload.get("view") or ""),
                exclude_track_keys=payload.get("exclude_track_keys"),
                progress=lambda info: self._autotrack_task.note(
                    "車両を追跡しています", **info),
            )
        except Exception as exc:  # noqa: BLE001 - ここで固まらせない（保険）
            result = {"ok": False, "error": f"点群の自動追跡でエラー: {exc}"}
        elapsed = round(self._autotrack_task.snapshot()["elapsed"], 1)
        if not self._autotrack_task.finish(token):
            # 中止済み。計算結果は捨てて、GUIは手動フローへ戻す
            return {"ok": False, "cancelled": True, "token": token, "elapsed": elapsed,
                    "event_id": event_id, "error": "自動追跡を中止しました"}
        result["event_id"] = event_id
        result["token"] = token
        result["elapsed"] = elapsed
        return result

    def get_auto_track_progress(self) -> dict:
        """実行中の自動追跡の経過秒・目安時間を返す（GUIがポーリングする）。"""
        return self._autotrack_task.snapshot()

    def cancel_auto_track(self, token: int = 0) -> dict:
        """自動追跡を中止する。

        ``track_overtaking`` には中断の口が無いので計算自体は最後まで走るが、
        結果は破棄されるためGUIは即座に手動フローへ戻れる。
        """
        try:
            token = int(token or 0)
        except (TypeError, ValueError):
            token = 0
        return self._autotrack_task.cancel(token)

    def detect_view_auto(self, payload: dict | None = None) -> dict:
        """開いているGoPro動画から撮影方向(front/rear)を自動判定する（Step1）。

        背景オプティカルフローの放射方向で判定する ``detect_view`` を遅延importで
        呼ぶ。判定不能・失敗のときは ``ok=False`` を返すだけで、GUIはラジオを
        触らず手動選択のままにする。
        """
        payload = dict(payload or {})
        videos = self._payload_videos(payload)
        if not videos:
            return {"ok": False, "error": "先にGoPro動画を追加してください"}
        missing = [video.name for video in videos if not video.is_file()]
        if missing:
            return {"ok": False, "error": "動画が見つかりません: " + ", ".join(missing)}
        detector, detect_error, load_error = load_view_detector()
        if detector is None:
            return {"ok": False, "error": load_error}
        token = self._view_task.start(f"{len(videos)} 本の動画から背景の動きを測っています")
        if not token:
            return {"ok": False, "busy": True, "error": "撮影方向の判定がすでに実行中です"}

        def progress(message) -> None:
            self._view_task.raise_if_cancelled(token)
            self._view_task.note(str(message).replace("[view] ", ""))

        options: dict = {}
        for key, cast in (("samples", int), ("sample_seconds", float)):
            if payload.get(key) not in (None, ""):
                try:
                    options[key] = cast(payload[key])
                except (TypeError, ValueError):
                    pass
        try:
            with tempfile.TemporaryDirectory() as work_value:
                result = detector(videos, Path(work_value), progress=progress, **options) or {}
        except TaskCancelled:
            self._view_task.finish(token)
            return {"ok": False, "cancelled": True, "token": token,
                    "error": "撮影方向の判定を中止しました"}
        except detect_error as exc:
            self._view_task.finish(token)
            return {"ok": False, "token": token, "error": f"撮影方向を判定できません: {exc}"}
        except Exception as exc:  # noqa: BLE001 - 判定の失敗でGUIを止めない
            self._view_task.finish(token)
            return {"ok": False, "token": token,
                    "error": f"撮影方向の判定で予期しないエラー: {exc}"}
        elapsed = round(self._view_task.snapshot()["elapsed"], 1)
        if not self._view_task.finish(token):
            return {"ok": False, "cancelled": True, "token": token, "elapsed": elapsed,
                    "error": "撮影方向の判定を中止しました"}
        detail = dict(result.get("detail") or {})
        view = result.get("view")
        common = {"token": token, "elapsed": elapsed, "reason": str(detail.get("reason") or ""),
                  "radial_ratio": detail.get("radial_ratio"),
                  "moving_pairs": detail.get("moving_pairs"),
                  "samples_used": detail.get("samples_used")}
        if view not in ("front", "rear"):
            return {"ok": False, "undecided": True, "view": None,
                    "leaning": detail.get("leaning"), **common, "error": (
                        "撮影方向を判定できませんでした（背景の動きが足りません）。"
                        "手動で前方／後方を選んでください")}
        return {"ok": True, "view": str(view),
                "confidence": float(result.get("confidence") or 0.0), **common}

    def get_detect_view_progress(self) -> dict:
        return self._view_task.snapshot()

    def cancel_detect_view(self, token: int = 0) -> dict:
        try:
            token = int(token or 0)
        except (TypeError, ValueError):
            token = 0
        return self._view_task.cancel(token)

    def export_lidar_roi(self, payload: dict) -> dict:
        if self._pcap is None:
            return {"ok": False, "error": "先にPCAPを開いてください"}
        folder = lidar_dir_from_payload(payload)
        try:
            laser_value = payload.get("lasers")
            lasers = {int(v) for v in laser_value} if laser_value is not None else None
            frames, points = self._pcap.export_roi(
                folder, int(payload.get("first", 0)), int(payload.get("last", 0)),
                dict(payload.get("bounds") or {}), lasers,
            )
            return {"ok": True, "frames": frames, "points": points, "folder": str(folder)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_overtaking(self, payload: dict) -> dict:
        if self._pcap is None:
            return {"ok": False, "error": "先にPCAPを開いてください"}
        frames = list(payload.get("frames") or [])
        if not frames:
            return {"ok": False, "error": "記録されたフレームがありません"}
        folder = lidar_dir_from_payload(payload)
        try:
            path, frame_count, written, points = self._pcap.export_overtaking(
                folder, payload.get("event_id"), frames,
            )
            return {
                "ok": True, "path": str(path), "frames": frame_count,
                "written_frames": written, "points": points,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def generate_edit_csv(self, source_value: str, out_value: str = "") -> dict:
        """最近接点の軌跡 ``<名前>_edit.csv`` を生成する。

        ``source_value`` は追い越し記録CSV（``overtaking_<id>.csv``）か、フレーム別CSVの
        入ったフォルダ。旧 VolodyneConverter（モード2）の処理に相当する。
        """
        source = Path(str(source_value or "").strip()).expanduser()
        if not source.exists():
            return {"ok": False, "error": "追い越し記録CSVまたはフォルダを選択してください"}
        try:
            path = move_edit_csv_to_dir(
                write_edit_csv(source),
                edit_dir_from_payload({"out": out_value}),
            )
            analysis = {}
            try:
                analysis["analysis_path"] = str(write_edit_analysis_xlsx(path))
            except Exception as exc:
                analysis["analysis_error"] = str(exc)
            rows = max(0, sum(1 for _ in path.open("r", encoding="utf-8-sig")) - 1)
            return {"ok": True, "path": str(path), "rows": rows, **analysis}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def generate_all_edit_csv(self, out_value: str) -> dict:
        lidar_dir = output_dir_from_payload({"out": out_value}) / "lidar"
        if not lidar_dir.is_dir():
            return {"ok": False, "error": "LiDARフォルダが見つかりません（出力フォルダ内 lidar/ を確認してください）"}

        targets = [
            path for path in sorted(lidar_dir.glob("overtaking_*.csv"))
            if not path.stem.endswith("_edit")
        ]

        if not targets:
            return {"ok": False, "error": "edit.csv生成対象の追い越しCSV（overtaking_*.csv）が見つかりません"}

        edit_dir = edit_dir_from_payload({"out": out_value})
        generated = []
        failed = []
        for target in targets:
            try:
                path = move_edit_csv_to_dir(write_edit_csv(target), edit_dir)
                analysis = {}
                try:
                    analysis["analysis_path"] = str(write_edit_analysis_xlsx(path))
                except Exception as exc:
                    analysis["analysis_error"] = str(exc)
                rows = max(0, sum(1 for _ in path.open("r", encoding="utf-8-sig")) - 1)
                generated.append({"source": str(target), "path": str(path), "rows": rows, **analysis})
            except Exception as exc:
                failed.append({"source": str(target), "error": str(exc)})

        analysis_failed_count = sum(1 for item in generated if item.get("analysis_error"))

        return {
            "ok": bool(generated),
            "generated": generated,
            "failed": failed,
            "count": len(generated),
            "failed_count": len(failed),
            "analysis_failed_count": analysis_failed_count,
            "error": "" if generated else "edit.csvを生成できませんでした",
        }

    def generate_final_excel(self, payload: dict) -> dict:
        try:
            out_dir = output_dir_from_payload(payload)
            pcap_start = self._pcap.start_unix_time if self._pcap is not None else None
            result = generate_final_workbook(
                out_dir,
                organize_id=str(payload.get("final_id") or "").strip(),
                subject_id=str(payload.get("final_subject") or "").strip(),
                collection_date=str(payload.get("final_date") or "").strip(),
                offset=float(payload.get("offset") or 0),
                pcap_path=str(payload.get("pcap") or ""),
                pcap_start_unix=pcap_start,
            )
            return {"ok": True, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def auto_sync_gps(self, video: str) -> dict:
        from gpmf_sync import compute_gps_offset, SyncError
        if self._pcap is None:
            return {"ok": False, "error": "先にStep 3でPCAPを読み込んでください"}
        video_path = Path(str(video).strip())
        if not video_path.is_file():
            return {"ok": False, "error": "GoPro動画が見つかりません（Step 1で指定してください）"}
        try:
            result = compute_gps_offset(video_path, self._pcap.start_unix_time)
            return {"ok": True, **result}
        except SyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"予期しないエラー: {exc}"}

    def open_path(self, value: str) -> dict:
        path = Path(value)
        if not path.exists():
            return {"ok": False, "error": f"パスが存在しません: {path}"}
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}



class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, api: AppApi):
        super().__init__(address, UiHandler)
        self.api = api


class UiHandler(BaseHTTPRequestHandler):
    server: UiServer

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/media/"):
            self._serve_media(path.removeprefix("/media/"))
            return
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (UI_DIR / relative).resolve()
        try:
            target.relative_to(UI_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_media(self, token: str) -> None:
        target = self.server.api._media_registry.get(token)
        if target is None or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = target.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
        length = max(0, end - start + 1)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def start_server(api: AppApi, port: int = 0) -> tuple[UiServer, str]:
    server = UiServer(("127.0.0.1", port), api)
    base_url = f"http://127.0.0.1:{server.server_port}"
    api._base_url = base_url
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, base_url


def check_autotrack_real_pcap() -> None:
    """実PCAPに対する ``auto_track_candidate`` のスモークテスト（任意実行）。

    実データはリポジトリに置けないので、環境変数で指示されたときだけ走らせる::

        WEBGUI_AUTOTRACK_PCAP    … 対象PCAP（必須。未設定ならこの検査は省略）
        WEBGUI_AUTOTRACK_EVENTS  … overtaking_events.csv（候補の時刻をここから取る）
        WEBGUI_AUTOTRACK_EVENT_ID… 使う候補のID（既定: CSVの先頭）
        WEBGUI_AUTOTRACK_OFFSET  … 時刻オフセット秒（pcap時刻 = gopro時刻 + offset）
        WEBGUI_AUTOTRACK_VIEW    … front / rear（既定 front）
        WEBGUI_AUTOTRACK_START / _PEAK / _END … CSVを使わず時刻を直接指定する場合
    """
    pcap_value = os.environ.get("WEBGUI_AUTOTRACK_PCAP", "").strip()
    if not pcap_value:
        return
    pcap_path = Path(pcap_value).expanduser()
    assert pcap_path.is_file(), f"PCAPが見つかりません: {pcap_path}"
    offset = float(os.environ.get("WEBGUI_AUTOTRACK_OFFSET", "0") or 0)
    view = os.environ.get("WEBGUI_AUTOTRACK_VIEW", "front").strip()
    events_value = os.environ.get("WEBGUI_AUTOTRACK_EVENTS", "").strip()
    if events_value:
        events = read_events_csv(Path(events_value).expanduser(), {}, "")
        assert events, f"候補がありません: {events_value}"
        wanted = os.environ.get("WEBGUI_AUTOTRACK_EVENT_ID", "").strip()
        event = next((e for e in events if str(e.get("event_id")) == wanted), events[0]) if wanted else events[0]
    else:
        event = {
            "event_id": os.environ.get("WEBGUI_AUTOTRACK_EVENT_ID", "smoke"),
            "t_start_s": os.environ.get("WEBGUI_AUTOTRACK_START", "0"),
            "peak_t_s": os.environ.get("WEBGUI_AUTOTRACK_PEAK", "0"),
            "t_end_s": os.environ.get("WEBGUI_AUTOTRACK_END", "0"),
        }
    api = AppApi()
    opened = api.open_pcap(str(pcap_path))
    assert opened["ok"], opened
    started = time.perf_counter()
    result = api.auto_track_candidate({"event": event, "offset": offset, "view": view})
    elapsed = time.perf_counter() - started
    assert result["ok"], result
    frames = result["frames"]
    assert frames and all(
        set(frame["bounds"]) == {"min_x", "max_x", "min_y", "max_y"} for frame in frames
    ), result
    assert result["first_frame"] <= frames[0]["index"] <= frames[-1]["index"] <= result["last_frame"], result
    # 返った矩形がそのまま記録済みフレーム機構（export_overtaking）へ渡せることを確認する。
    with tempfile.TemporaryDirectory() as temp_value:
        path, count, written, points = api._pcap.export_overtaking(
            Path(temp_value), event.get("event_id", "smoke"), frames,
        )
        assert path.is_file() and count == len(frames) and written > 0 and points > 0, (
            path, count, written, points)
    print(
        f"実PCAP自動追跡スモークOK: 候補 {result['event_id']} / "
        f"フレーム {result['first_frame']}〜{result['last_frame']} / "
        f"矩形 {len(frames)} 枚 / 確度 {result['confidence']} / "
        f"通過時刻 {result['pass_time']} / {elapsed:.1f}秒"
    )

    # 進捗表示と中止（実データ）。中止は即座に返り、走り切った結果は破棄される。
    box: dict = {}
    worker = threading.Thread(
        target=lambda: box.update(result=api.auto_track_candidate(
            {"event": event, "offset": offset, "view": view})),
        daemon=True,
    )
    worker.start()
    progress: dict = {}
    for _ in range(100):
        progress = api.get_auto_track_progress()
        if progress["running"] and progress.get("expected_seconds"):
            break
        time.sleep(0.05)
    assert progress.get("running") and progress.get("expected_seconds"), progress
    assert progress["message"] and progress["event_id"] == result["event_id"], progress
    busy = api.auto_track_candidate({"event": event, "offset": offset, "view": view})
    assert busy["ok"] is False and busy.get("busy") is True, busy
    time.sleep(0.5)
    later = api.get_auto_track_progress()
    assert later["running"] and later["elapsed"] > progress["elapsed"], (progress, later)
    cancel_started = time.perf_counter()
    cancelled = api.cancel_auto_track(progress["token"])
    cancel_elapsed = time.perf_counter() - cancel_started
    assert cancelled["ok"] and cancel_elapsed < 0.2, (cancelled, cancel_elapsed)
    worker.join(120)
    assert not worker.is_alive(), "自動追跡スレッドが終了しません"
    assert box["result"]["ok"] is False and box["result"]["cancelled"] is True, box
    assert api.get_auto_track_progress()["running"] is False
    print(
        f"実PCAP中止スモークOK: 進捗 {later['elapsed']:.1f}秒経過 / "
        f"目安 {later['expected_seconds']:.1f}秒 / "
        f"中止応答 {cancel_elapsed * 1000:.0f}ms（結果は破棄）"
    )


def check_detect_view_real_video() -> None:
    """実動画に対する ``detect_view_auto`` のスモークテスト（任意実行）。

        WEBGUI_DETECT_VIEW_VIDEO  … 対象動画（必須。未設定ならこの検査は省略）。
                                    複数本は ``os.pathsep``（Windowsは ;）で区切る
        WEBGUI_DETECT_VIEW_EXPECT … 期待する方向 front / rear（指定時のみ照合）
    """
    raw = os.environ.get("WEBGUI_DETECT_VIEW_VIDEO", "").strip()
    if not raw:
        return
    videos = [Path(value).expanduser() for value in raw.split(os.pathsep) if value.strip()]
    for video in videos:
        assert video.is_file(), f"動画が見つかりません: {video}"
    api = AppApi()
    started = time.perf_counter()
    result = api.detect_view_auto({"videos": [str(video) for video in videos]})
    elapsed = time.perf_counter() - started
    assert result["ok"], result
    assert result["view"] in ("front", "rear") and result["confidence"] > 0, result
    expected = os.environ.get("WEBGUI_DETECT_VIEW_EXPECT", "").strip().lower()
    if expected:
        assert result["view"] == expected, (result, expected)
    assert api.get_detect_view_progress()["running"] is False
    print(
        f"実動画の撮影方向スモークOK: {result['view']} / 確信度 {result['confidence']:.2f} / "
        f"{elapsed:.1f}秒 / {result['reason']}"
    )


def run_check() -> None:
    from gpmf_sync import offset_from_unix_starts

    for name in ("index.html", "style.css", "app.js"):
        assert (UI_DIR / name).is_file(), name
    index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert index_html.index('id="pcapPath"') < index_html.index('id="autoSyncGps"')
    assert index_html.index('id="autoSyncGps"') < index_html.index('id="panel-lidar"')
    assert "function jumpToCandidate()" in app_js
    assert "candidatePcapTime(event)" in app_js
    assert "eventSeconds(event, 't_start_s')" in app_js
    assert offset_from_unix_starts(100.0, 250.0) == -150.0
    sample = [{"event_id": "001", "t_start_s": "1", "peak_t_s": "2", "t_end_s": "3", "review_status": "採用"}]
    assert format_timecode(float(sample[0]["t_start_s"]) + 5) == "00:00:06.000"
    assert normalize_vehicle_type("truck") == "大型"
    assert normalize_vehicle_type("car") == "普通"
    assert normalize_vehicle_type("軽") == "軽"
    with tempfile.TemporaryDirectory() as temp_value:
        chosen = Path(temp_value) / "chosen-output"
        assert output_dir_from_payload({"out": str(chosen)}) == chosen
        assert prepare_output_dir({"out": str(chosen)}) == chosen
        assert chosen.is_dir()
        assert output_dir_from_payload({"out": ""}) == DEFAULT_OUT
        # LiDAR出力は検出出力フォルダ配下 <out>/lidar に統一される。
        assert lidar_dir_from_payload({"out": str(chosen)}) == chosen / "lidar"
        assert (chosen / "lidar").is_dir()

    # 通常CSVはそのまま（全件未確認）で読み込む。reviewed CSVは採用・除外を適用する。
    with tempfile.TemporaryDirectory() as temp_value:
        temp_dir = Path(temp_value)
        detection_csv = temp_dir / "overtaking_events.csv"
        detection_csv.write_text(
            "event_id,class,detector_class,side,t_start_s,peak_t_s,t_end_s,kei_plate_score,kei_plate_hits\n"
            "001,軽,car,left,0,1,2,0.921,11\n",
            encoding="utf-8-sig",
        )
        loaded = read_events_csv(detection_csv, {}, "http://127.0.0.1")
        assert loaded[0]["class"] == "軽"
        assert loaded[0]["review_status"] == "未確認"
        assert loaded[0]["class_reviewed"] is False

        # reviewed CSVは採用・除外と、明示的に直した車種をそのまま適用して読み込む。
        reviewed_csv = temp_dir / "reviewed_overtaking_events.csv"
        reviewed_csv.write_text(
            "event_id,class,class_reviewed,review_status,side\n001,普通,True,採用,left\n",
            encoding="utf-8-sig",
        )
        reviewed = read_events_csv(reviewed_csv, {}, "http://127.0.0.1")
        assert reviewed[0]["class"] == "普通"
        assert reviewed[0]["review_status"] == "採用"
        assert reviewed[0]["class_reviewed"] is True

    # 最小のVLP-16 PCAPで、索引・点群変換・既存互換CSV列を確認する。
    with tempfile.TemporaryDirectory() as temp_value:
        temp_dir = Path(temp_value)
        pcap_path = temp_dir / "sample.pcap"
        packets = []
        for packet_no, azimuth in enumerate((35000, 100, 300)):
            payload = bytearray(1206)
            for block in range(12):
                struct.pack_into("<HH", payload, block * 100, 0xEEFF, (azimuth + block * 10) % 36000)
                for laser in range(32):
                    struct.pack_into("<H", payload, block * 100 + 4 + laser * 3, 5000)
                    payload[block * 100 + 6 + laser * 3] = 80
            struct.pack_into("<I", payload, 1200, packet_no * 1000)
            payload[1204], payload[1205] = 0x37, 0x22
            packets.append(bytes(payload))
        with pcap_path.open("wb") as stream:
            stream.write(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1))
            for packet_no, payload in enumerate(packets):
                stream.write(struct.pack("<IIII", 100, packet_no * 100000, len(payload), len(payload)))
                stream.write(payload)
        pcap = VelodynePcap(pcap_path)
        assert pcap.model_name == "VLP-16"
        assert len(pcap.frames) == 2
        assert len(pcap.decode_frame(1)) > 0
        frames, points = pcap.export_roi(temp_dir / "csv", 1, 1, {"min_x": -100, "max_x": 100, "min_y": -100, "max_y": 100})
        assert frames == 1 and points > 0
        with next((temp_dir / "csv").glob("*.csv")).open("r", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream))
        assert header[8:10] == ["Points:0", "Points:1"]

    # フレームCSVフォルダ → edit.csv（最近接点軌跡）生成を検証する。
    with tempfile.TemporaryDirectory() as temp_value:
        frames_dir = Path(temp_value) / "100"
        frames_dir.mkdir()
        header_line = ",".join((
            "intensity", "laser_id", "azimuth", "distance_m", "adjustedtime",
            "timestamp", "vertical_angle", "ids", "Points:0", "Points:1", "Points:2",
        ))
        # フレーム1: 最近接点は (3,4) 距離5。 フレーム2: 最近接点は (6,8) 距離10。
        (frames_dir / "10.0.csv").write_text(
            header_line + "\n0,0,0,0,0,0,0,0,3,4,0.1\n0,0,0,0,0,0,0,0,30,40,0.2\n",
            encoding="utf-8-sig",
        )
        (frames_dir / "11.0.csv").write_text(
            header_line + "\n0,0,0,0,0,0,0,0,6,8,0.3\n",
            encoding="utf-8-sig",
        )
        api = AppApi()
        result = api.generate_edit_csv(str(frames_dir), str(Path(temp_value) / "out"))
        assert result["ok"], result
        assert result["rows"] == 2, result
        assert Path(result["path"]).parent.name == "lidar_edit", result
        with Path(result["path"]).open("r", encoding="utf-8-sig", newline="") as stream:
            edit = list(csv.reader(stream))
        assert edit[0] == ["unixtime", "X", "Y", "Z", "distance(m)", "speed(km/h)"], edit
        assert edit[1] == ["10.0", "3", "4", "0.1", "5", ""], edit[1]
        # 速度 = √((6-3)²+(8-4)²)/(11-10)*3600/1000 = 5/1*3.6 = 18 km/h
        assert edit[2][:5] == ["11.0", "6", "8", "0.3", "10"], edit[2]
        assert edit[2][5] == "18", edit[2]

    # 単一の追い越し記録CSV（overtaking_<id>.csv）→ edit.csv 生成を検証する。
    with tempfile.TemporaryDirectory() as temp_value:
        roi_header = ",".join((
            "Frame", "Time", "Laser ID", "Intensity", "Distance",
            "Azimuth", "Elevation", "Selected", "Points:0", "Points:1", "Points:2",
        ))
        overtaking = Path(temp_value) / "overtaking_001.csv"
        # フレーム0(t=10): 最近接点(3,4)距離5。 フレーム1(t=11): 最近接点(6,8)距離10。
        overtaking.write_text(
            roi_header
            + "\n0,10.0,0,5,5,0,0,1,3,4,0.1\n0,10.0,0,5,50,0,0,1,30,40,0.2\n1,11.0,0,5,10,0,0,1,6,8,0.3\n",
            encoding="utf-8-sig",
        )
        api = AppApi()
        result = api.generate_edit_csv(str(overtaking), str(Path(temp_value) / "out"))
        assert result["ok"], result
        assert result["rows"] == 2, result
        assert Path(result["path"]).name == "overtaking_001_edit.csv", result
        assert Path(result["path"]).parent.name == "lidar_edit", result
        with Path(result["path"]).open("r", encoding="utf-8-sig", newline="") as stream:
            edit = list(csv.reader(stream))
        assert edit[1] == ["10.0", "3", "4", "0.1", "5", ""], edit[1]
        assert edit[2][:5] == ["11.0", "6", "8", "0.3", "10"], edit[2]
        assert edit[2][5] == "18", edit[2]  # 5m / 1s * 3.6 = 18 km/h

    # フェーズ判定: abs(Y) < 3 になる前の範囲からフェーズ2を選び、
    # フェーズ3/4も distance ではなく Y 値で判定する。
    with tempfile.TemporaryDirectory() as temp_value:
        from zipfile import ZipFile
        from edit_csv import analyze_edit_points, read_edit_points, write_edit_analysis_xlsx

        edit_path = Path(temp_value) / "sample_edit.csv"
        edit_path.write_text(
            "unixtime,X,Y,Z,distance(m),speed(km/h)\n"
            "0,-0.5,8,0,8.02,\n"
            "1,-1.0,7,0,7.10,12\n"
            "2,-1.5,6,0,6.20,8\n"
            "3,-2.0,5,0,5.40,7\n"
            "4,-2.5,2.5,0,3.54,14\n"
            "5,-1.0,3.2,0,3.35,16\n",
            encoding="utf-8-sig",
        )
        points = read_edit_points(edit_path)
        metrics = analyze_edit_points(points)
        assert metrics["phase2"].row_number == 5, metrics
        assert metrics["phase3"].row_number == 6, metrics
        assert metrics["phase4"].row_number == 7, metrics
        # LC は通過区間(フェーズ3〜4, X=-2.5 と -1.0)の通過側で |X| が最小の点。
        # 旧実装の abs(min(X)) は X が負側だと「最も遠い横距離」(2.5) を返していた。
        assert metrics["lc"] == 1.0, metrics
        assert metrics["passing_time"] == 1.0, metrics
        analysis_path = write_edit_analysis_xlsx(edit_path)
        assert analysis_path.is_file(), analysis_path
        with ZipFile(analysis_path) as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "フェーズ2" in sheet_xml, sheet_xml
        assert "計算値（数式）" in sheet_xml, sheet_xml

    # Step6: 採用候補とedit.csv分析から共有用Excelを生成する。
    with tempfile.TemporaryDirectory() as temp_value:
        from zipfile import ZipFile
        from final_export import DANGER_BY_LEVEL, _vehicle_speed_kmh

        # 危険感は未設定("0")なら空欄。全自動実行では人が後から入力する。
        assert DANGER_BY_LEVEL["0"] == "", DANGER_BY_LEVEL
        assert DANGER_BY_LEVEL["2"] == "危険", DANGER_BY_LEVEL

        out_dir = Path(temp_value) / "260701_4"
        edit_dir = out_dir / "lidar_edit"
        edit_dir.mkdir(parents=True)
        (out_dir / "reviewed_overtaking_events.csv").write_text(
            "event_id,class,source_video,local_t_start_s,t_start_s,review_status,danger_level\n"
            "001,普通,GX010731.MP4,152.44,152.44,採用,2\n"
            "002,軽,GX010731.MP4,160.00,160.00,除外,0\n",
            encoding="utf-8-sig",
        )
        (edit_dir / "overtaking_001_edit.csv").write_text(
            "unixtime,X,Y,Z,distance(m),speed(km/h)\n"
            "97.0,-0.5,8,0,8.0,\n"
            "98.0,-1.0,7,0,7.1,12\n"
            "99.0,-1.5,6,0,6.2,8\n"
            "100.0,-2.0,5,0,5.4,7\n"
            "101.0,-2.5,2.5,0,2.8,14\n"
            "102.0,-1.0,3.2,0,3.35,16\n",
            encoding="utf-8-sig",
        )
        api = AppApi()
        result = api.generate_final_excel({
            "out": str(out_dir), "final_id": "4", "final_subject": "ID1",
            "final_date": "2026-07-01", "offset": "0",
            "pcap": "2026-07-01-10-09-23_Velodyne-VLP-16-Data.pcap",
        })
        assert result["ok"], result
        assert result["rows"] == 1, result
        final_path = Path(result["path"])
        assert final_path.parent.name == "final", result
        assert final_path.is_file(), result
        with ZipFile(final_path) as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "整理ID" in sheet_xml, sheet_xml
        assert "通過時間" in sheet_xml, sheet_xml
        assert "相対速度" in sheet_xml, sheet_xml
        assert _vehicle_speed_kmh(18.36, 10.35) == 28.71
        assert 'sqref="G2:G51"' in sheet_xml, sheet_xml
        assert 'sqref="R2:R51"' in sheet_xml, sheet_xml
    # Step4 半自動モード: 追跡窓の組み立てと track_overtaking プラグインの扱い。
    assert autotrack_margins("front") == (8.0, 3.0)
    assert autotrack_margins("rear") == (3.0, 8.0)
    # 撮影方向が未確定なら前後対称の広い窓（run_pipeline の fallback と同じ考え方）
    assert autotrack_margins("") == (8.0, 8.0)
    assert autotrack_margins("unknown") == (8.0, 8.0)
    # run_pipeline 側の値と食い違ったら気付けるようにソースを突き合わせる
    pipeline_source = (APP_DIR / "run_pipeline.py").read_text(encoding="utf-8")
    margins_line = re.search(r"^VIEW_TRACK_MARGINS = (.+)$", pipeline_source, re.MULTILINE)
    assert margins_line and eval(margins_line.group(1)) == AUTOTRACK_VIEW_MARGINS, margins_line

    sample_event = {"event_id": "007", "t_start_s": "100", "peak_t_s": "102", "t_end_s": "104"}
    assert autotrack_window(sample_event, -58.827, "front") == (33.173, 48.173, 43.173)
    assert autotrack_window(sample_event, 0.0, "rear") == (97.0, 112.0, 102.0)
    assert autotrack_window(sample_event, 0.0, "") == (92.0, 112.0, 102.0)
    # 空欄の時刻は peak_t_s で埋める（app.js の eventSeconds と同じ規則）
    assert autotrack_window({"t_start_s": "", "peak_t_s": "50", "t_end_s": ""}, 0.0, "front") == (42.0, 53.0, 50.0)
    assert event_seconds({"t_end_s": "x", "peak_t_s": "3"}, "t_end_s") == 3.0
    assert normalize_track_frames([
        {"index": "2", "bounds": {"min_x": "1", "max_x": 2, "min_y": 3, "max_y": 4}},
        {"index": 1, "bounds": {"min_x": 0, "max_x": 1, "min_y": 2, "max_y": 3}},
        {"index": 3, "bounds": {"min_x": 0}},   # 壊れた矩形は捨てる
    ]) == [{"index": 1, "bounds": {"min_x": 0.0, "max_x": 1.0, "min_y": 2.0, "max_y": 3.0}},
           {"index": 2, "bounds": {"min_x": 1.0, "max_x": 2.0, "min_y": 3.0, "max_y": 4.0}}]

    class _FakePcap:
        duration = 200.0

        def nearest_frame(self, seconds: float) -> int:
            return max(0, min(1999, int(round(float(seconds) * 10))))

    calls: list[dict] = []

    def fake_tracker(pcap, first, last, side="right", expected_pass_time=None,
                     exclude_track_keys=None):
        calls.append({"first": first, "last": last, "side": side,
                      "expected": expected_pass_time, "exclude": exclude_track_keys})
        return {"ok": True, "frames": [{"index": first + 1, "bounds": {
            "min_x": 1.0, "max_x": 2.0, "min_y": 3.0, "max_y": 4.0}}],
            "confidence": 0.83, "warnings": ["注意"], "pass_time": 43.2,
            "track_key": "t7", "candidates": []}

    tracked = autotrack_candidate_frames(
        _FakePcap(), sample_event, -58.827, "front", tracker=fake_tracker,
        exclude_track_keys=["t1", ""],
    )
    assert tracked["ok"] and tracked["track_key"] == "t7", tracked
    assert tracked["frames"] == [{"index": 333, "bounds": {
        "min_x": 1.0, "max_x": 2.0, "min_y": 3.0, "max_y": 4.0}}], tracked
    assert (tracked["first_frame"], tracked["last_frame"]) == (332, 482), tracked
    assert tracked["warnings"] == ["注意"] and tracked["confidence"] == 0.83, tracked
    # side は常に right（検出CSVの side 列はカメラの見え方で物理の通過側ではない）
    assert calls[0]["side"] == "right", calls
    assert abs(calls[0]["expected"] - 43.173) < 1e-9, calls
    assert calls[0]["exclude"] == ["t1"], calls

    # 見つからない・窓が範囲外・例外は、いずれも ok=False で手動フローへ返す
    def empty_tracker(pcap, first, last, side="right", **_kwargs):
        return {"ok": False, "frames": [], "warnings": ["見つかりません"]}

    missed = autotrack_candidate_frames(_FakePcap(), sample_event, 0.0, "front", tracker=empty_tracker)
    assert missed["ok"] is False and missed["warnings"] == ["見つかりません"], missed
    assert "手動" in missed["error"], missed
    outside = autotrack_candidate_frames(_FakePcap(), sample_event, -500.0, "front", tracker=fake_tracker)
    assert outside["ok"] is False and "収録範囲" in outside["error"], outside

    def broken_tracker(pcap, first, last, side="right", **_kwargs):
        raise RuntimeError("追跡失敗")

    broken = autotrack_candidate_frames(_FakePcap(), sample_event, 0.0, "front", tracker=broken_tracker)
    assert broken["ok"] is False and "追跡失敗" in broken["error"], broken
    assert autotrack_candidate_frames(None, sample_event, 0.0, "front")["ok"] is False

    # 古いプラグイン（任意引数なし）でも呼べる
    def legacy_tracker(pcap, first, last, side="right"):
        return {"ok": True, "frames": [{"index": first, "bounds": {
            "min_x": 0.0, "max_x": 1.0, "min_y": 0.0, "max_y": 1.0}}], "confidence": 0.5}

    legacy = autotrack_candidate_frames(_FakePcap(), sample_event, 0.0, "front",
                                        tracker=legacy_tracker, exclude_track_keys=["t1"])
    assert legacy["ok"] and legacy["track_key"] is None, legacy

    # PCAP未読込ならAPIはエラーを返すだけ（GUIは手動フローのまま）
    assert AppApi().auto_track_candidate({"event": sample_event})["ok"] is False
    # 遅延importが実際に解決できること（GUI起動時には読み込まない）
    tracker, load_error = load_autotracker()
    assert callable(tracker), load_error

    # Step4 のUIに自動追跡の導線があること
    assert 'id="recordAutoTrack"' in index_html
    assert 'id="autoTrackStatus"' in index_html
    assert index_html.index('id="recordAutoTrack"') < index_html.index('id="recordFinish"')
    assert "async function autoTrackCandidate()" in app_js
    assert "api().auto_track_candidate(" in app_js
    assert "function applyRecordedBounds(index)" in app_js
    # 自動追跡の矩形は記録済みフレーム機構へ入り、フレーム移動で選択枠として復元される
    assert "state.record={ active:true, eventId:event.event_id, eventIndex:index, frames, auto:true };" in app_js
    assert "if(!applyRecordedBounds(index)&&l.tracking)trackSelection(l.points);" in app_js
    assert "$('recordAutoTrack').addEventListener('click',autoTrackCandidate);" in app_js
    assert "async auto_track_candidate()" in app_js  # デモ(ブラウザ)用スタブ

    # ── 自動追跡の進捗表示と中止 ───────────────────────────────────────────
    assert estimate_autotrack_seconds(100, 100) == AUTOTRACK_MIN_ESTIMATE
    assert estimate_autotrack_seconds(0, 199) == round(200 * AUTOTRACK_SECONDS_PER_FRAME, 1)
    # 進捗フックは追跡開始の直前に窓の情報を1度だけ通知する
    notes: list[dict] = []
    autotrack_candidate_frames(_FakePcap(), sample_event, -58.827, "front",
                               tracker=fake_tracker, progress=notes.append)
    assert notes == [{"first_frame": 332, "last_frame": 482,
                      "expected_seconds": estimate_autotrack_seconds(332, 482)}], notes
    # 窓が範囲外なら追跡を始めないので進捗の通知も出ない
    notes.clear()
    autotrack_candidate_frames(_FakePcap(), sample_event, -500.0, "front",
                               tracker=fake_tracker, progress=notes.append)
    assert notes == [], notes

    task = BackgroundTask("試験")
    assert task.snapshot()["running"] is False
    token = task.start("開始しました", {"event_id": "007"})
    assert token == 1 and task.start("二重起動") == 0
    task.note("追跡しています", expected_seconds=12.5)
    running = task.snapshot()
    assert running["running"] and running["message"] == "追跡しています", running
    assert running["expected_seconds"] == 12.5 and running["event_id"] == "007", running
    assert running["elapsed"] >= 0.0 and running["cancelled"] is False, running
    assert task.cancelled(token) is False
    assert task.cancel(token + 5)["ok"] is False       # 別ジョブの中止は効かない
    assert task.cancel(token)["ok"] is True and task.cancelled(token) is True
    assert task.finish(token) is False                 # 中止済みなので結果は捨てる
    assert task.snapshot()["running"] is False
    assert task.cancel(0)["ok"] is False               # 実行中でなければ何もしない
    token2 = task.start("次のジョブ")
    assert token2 == token + 1 and task.finish(token2) is True
    try:
        token3 = task.start("中断できる処理")
        task.cancel(token3)
        task.raise_if_cancelled(token3)
        raise AssertionError("TaskCancelled が送出されません")
    except TaskCancelled:
        task.finish(token3)

    # 実行中の自動追跡は進捗を返し、中止すると結果を捨てて即座にGUIを解放する
    idle = AppApi()
    assert idle.get_auto_track_progress()["running"] is False
    assert idle.cancel_auto_track()["ok"] is False
    assert idle.get_detect_view_progress()["running"] is False
    assert idle.cancel_detect_view()["ok"] is False

    api_check = AppApi()
    api_check._pcap = _FakePcap()
    entered = threading.Event()

    def slow_tracker(pcap, first, last, side="right", **_kwargs):
        entered.set()
        time.sleep(0.5)
        return {"ok": True, "frames": [{"index": first + 1, "bounds": {
            "min_x": 1.0, "max_x": 2.0, "min_y": 3.0, "max_y": 4.0}}],
            "confidence": 0.7, "track_key": "slow"}

    original_loader = globals()["load_autotracker"]
    globals()["load_autotracker"] = lambda: (slow_tracker, "")
    try:
        box: dict = {}
        worker = threading.Thread(target=lambda: box.update(result=api_check.auto_track_candidate(
            {"event": sample_event, "offset": -58.827, "view": "front"})), daemon=True)
        worker.start()
        assert entered.wait(5.0), "自動追跡が始まりません"
        snapshot = api_check.get_auto_track_progress()
        assert snapshot["running"] and snapshot["event_id"] == "007", snapshot
        assert snapshot["expected_seconds"] == estimate_autotrack_seconds(332, 482), snapshot
        # 実行中の二重起動は弾く（中止か完了を待つ）
        busy = api_check.auto_track_candidate({"event": sample_event})
        assert busy["ok"] is False and busy.get("busy") is True, busy
        cancel_started = time.perf_counter()
        cancelled = api_check.cancel_auto_track(snapshot["token"])
        assert cancelled["ok"] and time.perf_counter() - cancel_started < 0.2, cancelled
        worker.join(10)
        assert box["result"]["ok"] is False and box["result"]["cancelled"] is True, box
        assert api_check.get_auto_track_progress()["running"] is False
        # 中止後もそのまま次の候補を自動追跡できる
        retried = api_check.auto_track_candidate(
            {"event": sample_event, "offset": -58.827, "view": "front"})
        assert retried["ok"] and retried["token"] == snapshot["token"] + 1, retried
        assert retried["elapsed"] >= 0.0 and retried["event_id"] == "007", retried
    finally:
        globals()["load_autotracker"] = original_loader

    # ── Step1 撮影方向の自動判定 ──────────────────────────────────────────
    assert 'id="detectView"' in index_html and 'id="detectViewStatus"' in index_html
    assert 'id="detectViewCancel"' in index_html
    assert index_html.index('name="view"') < index_html.index('id="detectView"')
    assert index_html.index('id="detectView"') < index_html.index('id="startDetection"')
    assert 'id="autoTrackCancel"' in index_html and 'id="autoTrackProgress"' in index_html
    assert index_html.index('id="recordAutoTrack"') < index_html.index('id="autoTrackCancel"')
    assert "async function autoDetectView()" in app_js
    assert "api().detect_view_auto(" in app_js
    assert "async function pollAutoTrack(run)" in app_js
    assert "api().get_auto_track_progress?.()" in app_js
    assert "api().cancel_auto_track?.(" in app_js
    assert "api().get_detect_view_progress?.()" in app_js
    assert "api().cancel_detect_view?.(" in app_js
    assert "$('autoTrackCancel').addEventListener('click',cancelAutoTrack);" in app_js
    assert "$('detectView').addEventListener('click', autoDetectView);" in app_js
    assert "async detect_view_auto()" in app_js          # デモ(ブラウザ)用スタブ
    assert "async get_auto_track_progress()" in app_js
    # 判定成功時だけラジオへ反映する（失敗時は手動選択のまま）
    assert 'const radio = document.querySelector(`[name=view][value="${result.view}"]`);' in app_js

    detector, detect_error, load_error = load_view_detector()
    assert callable(detector) and issubclass(detect_error, Exception), load_error

    view_api = AppApi()
    assert view_api.detect_view_auto({})["ok"] is False           # 動画未選択
    assert "動画" in view_api.detect_view_auto({})["error"]
    missing = view_api.detect_view_auto({"videos": [str(APP_DIR / "no-such-movie.mp4")]})
    assert missing["ok"] is False and "見つかりません" in missing["error"], missing

    with tempfile.TemporaryDirectory() as temp_value:
        movie = Path(temp_value) / "GX010001.MP4"
        movie.write_bytes(b"dummy")
        seen: list[str] = []

        def fake_detector(videos, work_dir, progress=None, **kwargs):
            assert [Path(v).name for v in videos] == ["GX010001.MP4"], videos
            if progress:
                progress("[view] サンプル1: 放射成分 +0.62")
            seen.append("done")
            return {"view": "front", "confidence": 0.97,
                    "detail": {"reason": "背景フローは外向き", "radial_ratio": 0.62,
                               "moving_pairs": 120, "samples_used": 1}}

        def undecided_detector(videos, work_dir, progress=None, **kwargs):
            return {"view": None, "confidence": 0.0,
                    "detail": {"reason": "判定材料不足", "leaning": "front"}}

        def broken_detector(videos, work_dir, progress=None, **kwargs):
            raise RuntimeError("cv2が壊れています")

        def slow_detector(videos, work_dir, progress=None, **kwargs):
            for _ in range(200):
                if progress:
                    progress("[view] 測定中")
                time.sleep(0.02)
            return {"view": "rear", "confidence": 0.9, "detail": {"reason": "測り切った"}}

        original_view_loader = globals()["load_view_detector"]
        try:
            payload = {"videos": [str(movie)]}
            globals()["load_view_detector"] = lambda: (fake_detector, RuntimeError, "")
            decided = view_api.detect_view_auto(payload)
            assert decided["ok"] and decided["view"] == "front", decided
            assert decided["confidence"] == 0.97 and seen == ["done"], decided
            assert decided["reason"] == "背景フローは外向き" and decided["elapsed"] >= 0.0, decided
            assert view_api.get_detect_view_progress()["running"] is False

            globals()["load_view_detector"] = lambda: (undecided_detector, RuntimeError, "")
            undecided = view_api.detect_view_auto(payload)
            assert undecided["ok"] is False and undecided["undecided"] is True, undecided
            assert undecided["view"] is None and undecided["leaning"] == "front", undecided

            globals()["load_view_detector"] = lambda: (broken_detector, ValueError, "")
            broken_view = view_api.detect_view_auto(payload)
            assert broken_view["ok"] is False and "cv2が壊れています" in broken_view["error"], broken_view

            globals()["load_view_detector"] = lambda: (None, None, "detect_view を読み込めません: 試験")
            unavailable = view_api.detect_view_auto(payload)
            assert unavailable["ok"] is False and "読み込めません" in unavailable["error"], unavailable

            # 中止は progress コールバックで協調的に効く（測定を打ち切る）
            globals()["load_view_detector"] = lambda: (slow_detector, RuntimeError, "")
            view_box: dict = {}
            view_worker = threading.Thread(
                target=lambda: view_box.update(result=view_api.detect_view_auto(payload)),
                daemon=True)
            view_worker.start()
            # 開始直後は start() が入れた「背景の動きを測っています」が見えるので、
            # 検出器が progress で出す「測定中」に変わるまで待つ（メッセージが
            # 空でないだけで抜けると、どちらを掴むかが実行ごとに変わる）。
            for _ in range(100):
                view_progress = view_api.get_detect_view_progress()
                if view_progress["running"] and "測定中" in view_progress["message"]:
                    break
                time.sleep(0.02)
            assert view_progress["running"] and "測定中" in view_progress["message"], view_progress
            assert view_api.cancel_detect_view(view_progress["token"])["ok"] is True
            view_worker.join(10)
            assert view_box["result"]["ok"] is False, view_box
            assert view_box["result"]["cancelled"] is True, view_box
            assert view_api.get_detect_view_progress()["running"] is False
        finally:
            globals()["load_view_detector"] = original_view_loader

    check_autotrack_real_pcap()
    check_detect_view_real_video()
    print("Web GUI check OK")


def run_ui_smoke(api: AppApi, server: UiServer, url: str) -> None:
    """WebView2上で候補確認画面の主要動作を検証する。"""
    import webview

    result_box: dict = {}
    window = webview.create_window(
        "UI smoke test", url=url, js_api=api, width=1280, height=840,
        min_size=(980, 680), hidden=True,
    )
    assert window is not None
    api.attach(window, url)

    def inspect_ui() -> None:
        try:
            time.sleep(2.0)
            result_box["result"] = window.evaluate_js("""
                (() => {
                  goStep('review');
                  state.events = [
                    {event_id:'001', class:'普通', t_start_s:'1.0', peak_t_s:'2.0', t_end_s:'3.0', review_status:'未確認', clip_url:'data:video/mp4;base64,AAAA'},
                    {event_id:'002', class:'大型', t_start_s:'4.0', peak_t_s:'5.0', t_end_s:'6.0', review_status:'未確認', clip_url:'data:video/mp4;base64,AAAA'},
                    {event_id:'003', class:'普通', t_start_s:'7.0', peak_t_s:'8.0', t_end_s:'9.0', review_status:'未確認', clip_url:'data:video/mp4;base64,AAAA'}
                  ];
                  state.selected = 0;
                  renderEvents();
                  selectEvent(0, false);
                  const preview = document.querySelector('.preview-card').getBoundingClientRect();
                  const table = document.querySelector('.table-card').getBoundingClientRect();
                  const before = {
                    placeholderHidden: document.getElementById('videoPlaceholder').hidden,
                    playerHidden: document.getElementById('clipPlayer').hidden,
                    previewLeft: preview.left, tableLeft: table.left,
                    previewWidth: preview.width, tableWidth: table.width
                  };
                  changeVehicleType(0, '軽');
                  decide('採用');
                  const afterDecision = { selected: state.selected, firstStatus: state.events[0].review_status, firstClass: state.events[0].class };
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: state.shortcuts.reject, bubbles:true}));
                  return {
                    before,
                    afterDecision,
                    afterShortcut: { selected: state.selected, secondStatus: state.events[1].review_status }
                  };
                })()
            """)
            # Step4 半自動モード: 自動追跡の結果が記録済みフレームへ入るか確かめる。
            # PCAPを開かずに済ませるため、window.pywebview.api を一時的に差し替える。
            window.evaluate_js("""
                (() => {
                  goStep('lidar');
                  window.__realApi = window.pywebview.api;
                  window.__progressCalls = 0;
                  window.pywebview.api = {
                    async auto_track_candidate(payload) {
                      window.__autoTrackPayload = payload;
                      if (window.__slowAutoTrack) await new Promise(r => setTimeout(r, 900));
                      return { ok:true, event_id:'001', first_frame:4, last_frame:9,
                        confidence:0.91, warnings:[], track_key:'trk-1', pass_time:43.2, elapsed:1.4,
                        frames:[{index:5, bounds:{min_x:1, max_x:2, min_y:3, max_y:4}},
                                {index:6, bounds:{min_x:1.1, max_x:2.1, min_y:3.1, max_y:4.1}}] };
                    },
                    async get_auto_track_progress() {
                      window.__progressCalls += 1;
                      return { ok:true, running:true, token:7, elapsed:1.5, expected_seconds:13.5,
                        message:'車両を追跡しています', event_id:'001' };
                    },
                    async cancel_auto_track(token) {
                      window.__cancelToken = token;
                      return { ok:true, running:true, cancelled:true, token };
                    },
                    async get_lidar_frame(index) { return { ok:true, timestamp:index*0.1, points:[] }; },
                    async seek_lidar_time(seconds) { return { ok:true, index:5 }; }
                  };
                  state.events = [{event_id:'001', class:'普通', t_start_s:'100', peak_t_s:'102', t_end_s:'104', review_status:'未確認'}];
                  state.offset = -58.827;
                  renderMapped();
                  document.getElementById('recordCandidate').value = '0';
                  Object.assign(state.lidar, {loaded:true, frameCount:100, frameTimes:[], index:0,
                    laserCount:0, lasers:new Set(), selection:null});
                  window.__autoTrack = 'pending';
                  autoTrackCandidate().then(() => {
                    window.__autoTrack = { active:state.record.active, auto:state.record.auto,
                      eventId:state.record.eventId, frames:state.record.frames.size,
                      index:state.lidar.index, selection:state.lidar.selection,
                      trackKey:state.autoTrackKeys.get('001'),
                      status:document.getElementById('autoTrackStatus').textContent,
                      payload:window.__autoTrackPayload };
                  }).catch(err => { window.__autoTrack = { error:String(err) }; });
                  return 'started';
                })()
            """)
            for _ in range(40):
                time.sleep(0.1)
                auto = window.evaluate_js("window.__autoTrack")
                if auto != "pending":
                    break
            result_box["auto"] = auto

            # 中止: 走り切った結果を捨てて手動フローへ戻し、ボタンを解放する
            window.evaluate_js("""
                (() => {
                  recordCancel();
                  window.__slowAutoTrack = true;
                  state.events = [{event_id:'002', class:'普通', t_start_s:'100', peak_t_s:'102', t_end_s:'104', review_status:'未確認'}];
                  renderMapped();
                  document.getElementById('recordCandidate').value = '0';
                  window.__cancelCase = 'pending';
                  autoTrackCandidate().then(() => {
                    window.__cancelCase = { active:state.record.active, frames:state.record.frames.size,
                      status:document.getElementById('autoTrackStatus').textContent,
                      buttonDisabled:document.getElementById('recordAutoTrack').disabled,
                      cancelHidden:document.getElementById('autoTrackCancel').hidden,
                      progressBarHidden:document.getElementById('autoTrackProgress').hidden,
                      progressCalls:window.__progressCalls, cancelToken:window.__cancelToken,
                      trackKey:state.autoTrackKeys.get('002') || '' };
                  }).catch(err => { window.__cancelCase = { error:String(err) }; });
                  setTimeout(() => {
                    window.__whileRunning = { status:document.getElementById('autoTrackStatus').textContent,
                      buttonDisabled:document.getElementById('recordAutoTrack').disabled,
                      cancelHidden:document.getElementById('autoTrackCancel').hidden };
                    document.getElementById('autoTrackCancel').click();
                  }, 400);
                  return 'started';
                })()
            """)
            cancel_case = "pending"
            for _ in range(60):
                time.sleep(0.1)
                cancel_case = window.evaluate_js("window.__cancelCase")
                if cancel_case != "pending":
                    break
            result_box["cancel"] = cancel_case
            result_box["while_running"] = window.evaluate_js("window.__whileRunning")

            # Step1 撮影方向の自動判定
            window.evaluate_js("""
                (() => {
                  goStep('detect');
                  window.pywebview.api = Object.assign({}, window.pywebview.api, {
                    async detect_view_auto(payload) {
                      window.__viewPayload = payload;
                      return { ok:true, view:'front', confidence:0.99, elapsed:1.3,
                        reason:'背景フローは外向き', radial_ratio:0.62 };
                    },
                    async get_detect_view_progress() {
                      return { ok:true, running:true, token:3, elapsed:0.5, message:'背景の動きを測っています' };
                    },
                    async save_settings() { return { ok:true }; }
                  });
                  state.videos = ['C:/dummy/GX010001.MP4'];
                  document.querySelector('[name=view][value="rear"]').checked = true;
                  window.__viewCase = 'pending';
                  autoDetectView().then(() => {
                    const button = document.getElementById('detectView').getBoundingClientRect();
                    const field = document.getElementById('detectView').closest('fieldset').getBoundingClientRect();
                    window.__viewCase = { checked:document.querySelector('[name=view]:checked').value,
                      layout:{ width:button.width, height:button.height,
                        inside: button.right <= field.right + 0.5 && button.bottom <= field.bottom + 0.5 },
                      status:document.getElementById('detectViewStatus').textContent,
                      buttonDisabled:document.getElementById('detectView').disabled,
                      cancelHidden:document.getElementById('detectViewCancel').hidden,
                      payload:window.__viewPayload };
                  }).catch(err => { window.__viewCase = { error:String(err) }; });
                  return 'started';
                })()
            """)
            view_case = "pending"
            for _ in range(60):
                time.sleep(0.1)
                view_case = window.evaluate_js("window.__viewCase")
                if view_case != "pending":
                    break
            result_box["view"] = view_case

            # 差し替えを戻し、新しいAPIが本物のjs_apiブリッジ越しに呼べることを見る
            window.evaluate_js("""
                (() => {
                  window.pywebview.api = window.__realApi;
                  window.__bridge = 'pending';
                  Promise.all([
                    api().get_auto_track_progress(), api().cancel_auto_track(0),
                    api().get_detect_view_progress(), api().cancel_detect_view(0),
                    api().detect_view_auto({videos: []})
                  ]).then(values => { window.__bridge = values; })
                    .catch(err => { window.__bridge = { error:String(err) }; });
                  return 'started';
                })()
            """)
            bridge = "pending"
            for _ in range(60):
                time.sleep(0.1)
                bridge = window.evaluate_js("window.__bridge")
                if bridge != "pending":
                    break
            result_box["bridge"] = bridge
        except Exception as exc:
            result_box["error"] = repr(exc)
        finally:
            window.destroy()

    webview.start(inspect_ui, debug=False)
    server.shutdown()
    if "error" in result_box:
        raise RuntimeError(result_box["error"])
    result = result_box.get("result") or {}
    before = result.get("before") or {}
    assert before.get("placeholderHidden") is True, result
    assert before.get("playerHidden") is False, result
    assert before.get("previewLeft", 9999) < before.get("tableLeft", 0), result
    assert before.get("previewWidth", 0) > before.get("tableWidth", 9999), result
    assert result.get("afterDecision") == {"selected": 1, "firstStatus": "採用", "firstClass": "軽"}, result
    assert result.get("afterShortcut") == {"selected": 2, "secondStatus": "除外"}, result
    auto = result_box.get("auto") or {}
    assert auto.get("active") is True and auto.get("auto") is True, auto
    assert auto.get("eventId") == "001" and auto.get("frames") == 2, auto
    assert auto.get("trackKey") == "trk-1", auto
    # 先頭フレームへ移動し、その矩形が選択枠として復元されている
    assert auto.get("index") == 5, auto
    assert auto.get("selection") == {"min_x": 1, "max_x": 2, "min_y": 3, "max_y": 4}, auto
    payload = auto.get("payload") or {}
    assert payload.get("offset") == -58.827 and payload.get("view") in ("front", "rear"), payload
    assert (payload.get("event") or {}).get("event_id") == "001", payload

    # 実行中は経過秒を表示し、中止ボタンを出してボタンを二重押しさせない
    while_running = result_box.get("while_running") or {}
    assert while_running.get("buttonDisabled") is True, while_running
    assert while_running.get("cancelHidden") is False, while_running
    assert "秒経過" in (while_running.get("status") or ""), while_running
    # 中止したら、あとから返ってきた結果は捨てて手動フローのまま残す
    cancel_case = result_box.get("cancel") or {}
    assert cancel_case.get("active") is False and cancel_case.get("frames") == 0, cancel_case
    assert cancel_case.get("trackKey") == "", cancel_case
    assert "中止" in (cancel_case.get("status") or ""), cancel_case
    assert cancel_case.get("buttonDisabled") is False, cancel_case
    assert cancel_case.get("cancelHidden") is True, cancel_case
    assert cancel_case.get("progressBarHidden") is True, cancel_case
    assert cancel_case.get("progressCalls", 0) >= 1, cancel_case
    assert cancel_case.get("cancelToken") == 7, cancel_case
    # Step1 撮影方向の自動判定はラジオへ反映される
    view_case = result_box.get("view") or {}
    assert view_case.get("checked") == "front", view_case
    assert "前方" in (view_case.get("status") or "") and "0.99" in (view_case.get("status") or ""), view_case
    assert view_case.get("buttonDisabled") is False and view_case.get("cancelHidden") is True, view_case
    assert (view_case.get("payload") or {}).get("videos") == ["C:/dummy/GX010001.MP4"], view_case
    layout = view_case.get("layout") or {}
    assert layout.get("width", 0) > 0 and layout.get("height", 0) > 0, layout
    assert layout.get("inside") is True, layout      # カメラ方向の枠から食み出さない
    # 本物の js_api ブリッジ越しでも新しいAPIが呼べる（引数の受け渡しを含む）
    bridge = result_box.get("bridge")
    assert isinstance(bridge, list) and len(bridge) == 5, bridge
    assert bridge[0]["running"] is False and bridge[1]["ok"] is False, bridge
    assert bridge[2]["running"] is False and bridge[3]["ok"] is False, bridge
    assert bridge[4]["ok"] is False and "動画" in bridge[4]["error"], bridge
    print("WebView review UI smoke test OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--ui-smoke", action="store_true")
    parser.add_argument("--serve-only", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.check:
        run_check()
        return
    api = AppApi()
    server, url = start_server(api, args.port)
    if args.serve_only:
        print(url, flush=True)
        threading.Event().wait()
        return
    try:
        import webview
    except ImportError:
        server.shutdown()
        raise SystemExit("pywebviewがありません。pip install -r requirements.txt を実行してください。")
    if args.ui_smoke:
        run_ui_smoke(api, server, url)
        return
    window = webview.create_window(
        "自転車追い越し解析ワークフロー", url=url, js_api=api,
        width=1280, height=840, min_size=(980, 680),
    )
    api.attach(window, url)
    webview.start(debug=False)
    server.shutdown()


if __name__ == "__main__":
    main()
