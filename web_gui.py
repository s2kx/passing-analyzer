#!/usr/bin/env python3
"""HTMLベースの追い越し解析デスクトップGUI。"""

from __future__ import annotations

import argparse
import csv
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


def save_reviewed_events(out_dir: Path, events: list[dict], offset: float) -> Path:
    if not events:
        raise ValueError("保存する追い越し候補がありません")
    # 「除外」にした候補はレビュー結果CSVに出力しない
    events = [e for e in events if str(e.get("review_status", "")) != "除外"]
    if not events:
        raise ValueError("除外していない追い越し候補がありません")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reviewed_overtaking_events.csv"
    ignored = {"clip_url", "veloview_display"}
    fields = [key for key in events[0] if key not in ignored]
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


class AppApi:
    def __init__(self) -> None:
        self._window = None
        self._base_url = ""
        self._media_registry: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pcap: VelodynePcap | None = None
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
        assert metrics["lc"] == 2.5, metrics
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
        from final_export import _vehicle_speed_kmh

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
        assert _vehicle_speed_kmh(18.36, 10.35) == 28.71
        assert 'sqref="G2:G51"' in sheet_xml, sheet_xml
        assert 'sqref="M2:M51"' in sheet_xml, sheet_xml
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
                  document.dispatchEvent(new KeyboardEvent('keydown', {key:'x', bubbles:true}));
                  return {
                    before,
                    afterDecision,
                    afterShortcut: { selected: state.selected, secondStatus: state.events[1].review_status }
                  };
                })()
            """)
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
