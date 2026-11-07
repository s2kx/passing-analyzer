#!/usr/bin/env python3
"""全自動実行（run_pipeline.py）の専用GUI。

従来のワークフローGUI（web_gui.py・起動.bat）とは独立したアプリとして起動する
（`全自動実行GUI.bat`）。実行画面でセッションフォルダを選んで一括実行し、完了すると
候補ごとの確認画面へ自動で切り替わる。確認画面では各候補のクリップを見ながら
採否・車種を手動修正し、危険感と道路の車線数を入力して reviewed CSV と
最終Excel（末尾の「車線数」列）へ反映する。

画面は web_ui_auto/ の HTML/CSS/JS。HTTP配信・メディア配信・candidate CSV の
読み書きは web_gui.py の部品（start_server / read_events_csv /
save_reviewed_events）を使い回す。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 埋め込みPython（portable/python）は実行したスクリプトの場所を import の起点にしないので、
# 隣のモジュール（src/）を読めるよう自分のフォルダを足す。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from web_gui import (
    APP_DIR,
    SRC_DIR,
    detection_python,
    read_events_csv,
    save_reviewed_events,
    start_server,
)
from final_export import generate_final_workbook
# 点群ビューのデータは report.html と同じ読み取り・間引きを使う
from make_report import _safe_id, load_metrics, load_point_frames, load_trajectory

AUTO_UI_DIR = SRC_DIR / "web_ui_auto"
SETTINGS_PATH = APP_DIR / "auto_gui_settings.json"

# run_pipeline は段階の完了ごとに「[stage] 名前 タイトル [状態] メッセージ」を
# 1行出す（PipelineReport.stage）。値は「その段階が終わった時点」の進捗率(%)。
PIPELINE_STAGE_PROGRESS = {
    "view": 5, "detect": 45, "gps": 50, "sync": 55, "synccheck": 70,
    "autotrack": 85, "edit": 88, "review": 92, "final": 95,
    "report_html": 98, "report": 100,
}
PIPELINE_STAGE_RE = re.compile(r"^\[stage\]\s+(\S+)\s+(\S+)\s+\[(\w+)\]\s*(.*)$")
# GUIへ渡すログは末尾だけ残す（YOLOのフレーム行で数MBに肥大化するため）
PIPELINE_LOG_LINES = 400


def read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for key in ("session", "out"):
        if data.get(key) and not Path(str(data[key])).is_dir():
            data[key] = ""
    return data


def write_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_pipeline_stage(line: str) -> dict | None:
    match = PIPELINE_STAGE_RE.match(line.strip())
    if not match:
        return None
    return {"name": match.group(1), "title": match.group(2),
            "status": match.group(3), "message": match.group(4).strip()}


def pipeline_out_dir(payload: dict) -> Path:
    session = Path(str(payload.get("session") or "").strip()).expanduser()
    value = str(payload.get("out") or "").strip()
    return Path(value).expanduser() if value else session / "out_auto"


def pipeline_command(payload: dict) -> tuple[list[str], Path]:
    """GUIの入力から run_pipeline.py のコマンドラインを組み立てる。"""
    session_value = str(payload.get("session") or "").strip()
    if not session_value:
        raise ValueError("セッションフォルダを選択してください")
    session = Path(session_value).expanduser()
    if not session.is_dir():
        raise FileNotFoundError(f"セッションフォルダが見つかりません: {session}")
    cmd = [str(detection_python()), str(SRC_DIR / "run_pipeline.py"), str(session)]
    out_value = str(payload.get("out") or "").strip()
    if out_value:
        cmd += ["--out", out_value]
    view = str(payload.get("view") or "").strip().lower()
    if view in ("front", "rear"):
        cmd += ["--view", view]
    for key, flag in (("subject", "--subject"), ("date", "--date"),
                      ("organize_id", "--organize-id")):
        value = str(payload.get(key) or "").strip()
        if value:
            cmd += [flag, value]
    lane = str(payload.get("lane_count") or "").strip()
    if lane:
        cmd += ["--lane-count", lane]
    mode = str(payload.get("detect_mode") or "").strip()
    if mode == "skip":
        cmd.append("--skip-detect")
    elif mode == "redetect":
        cmd.append("--redetect")
    offset_value = str(payload.get("offset") or "").strip()
    if offset_value:
        try:
            cmd += ["--offset", str(float(offset_value))]
        except ValueError:
            raise ValueError(f"時刻オフセットは数値で入力してください: {offset_value}")
    extra = str(payload.get("extra") or "").strip()
    if extra:
        # 空白区切りの追加オプション（--skip-sync-check など）。スペースを含む
        # パスは扱えないので、入力欄ではフラグ用と案内する。
        cmd += extra.split()
    return cmd, pipeline_out_dir(payload)


def read_pipeline_summary(out_dir: Path) -> dict | None:
    """pipeline_report.json からGUI表示用の要約を作る（無ければ None）。"""
    try:
        data = json.loads((out_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    review = next((s for s in data.get("stages", []) if s.get("name") == "review"), {})
    report_html = out_dir / "report.html"
    return {
        "elapsed_seconds": data.get("elapsed_seconds"),
        "view": data.get("view"),
        "view_source": data.get("view_source"),
        "subject_id": data.get("subject_id"),
        "collection_date": data.get("collection_date"),
        "organize_id": data.get("organize_id"),
        "offset": (data.get("info") or {}).get("時刻オフセット(秒)"),
        "total": review.get("total"),
        "accepted": review.get("accepted"),
        "unreviewed": review.get("unreviewed"),
        "rejected": review.get("rejected"),
        "warnings": list(data.get("warnings") or []),
        "manual_actions": list(data.get("manual_actions") or []),
        "stages": [{key: entry.get(key) for key in ("name", "title", "status", "message")}
                   for entry in data.get("stages", [])],
        "report_html": str(report_html) if report_html.is_file() else "",
    }


def resolve_offset(out_dir: Path, summary: dict | None) -> float:
    """確認画面で使う時刻オフセット。レポート → sync.json の順で探す。"""
    candidates = [(summary or {}).get("offset")]
    try:
        candidates.append(
            json.loads((out_dir / "sync.json").read_text(encoding="utf-8")).get("offset"))
    except (OSError, json.JSONDecodeError):
        pass
    for value in candidates:
        try:
            if value is not None and str(value).strip() != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


class AutoApi:
    """web_ui_auto の js_api。実行（pipeline）と確認（review）の両画面を支える。"""

    def __init__(self) -> None:
        self._window = None
        self._base_url = ""
        self._media_registry: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._pipeline_process: subprocess.Popen | None = None
        self._pipeline_cancelled = False
        self._pipeline_state = {
            "running": False, "progress": 0, "log": "待機中", "message": "待機中",
            "exit_code": None, "stages": [], "out": "", "summary": None,
        }

    def attach(self, window, base_url: str) -> None:
        self._window = window
        self._base_url = base_url

    # ── 設定・ファイル選択 ──────────────────────────────────────────────────

    def get_settings(self) -> dict:
        settings = read_settings()
        settings["base_url"] = self._base_url
        return settings

    def save_settings(self, data: dict) -> dict:
        data = dict(data or {})
        data.pop("base_url", None)
        write_settings(data)
        return {"ok": True}

    def choose_folder(self, kind: str) -> dict:
        """フォルダ選択（kind: session / out）。"""
        if self._window is None:
            return {"ok": False, "error": "デスクトップ版でのみフォルダ選択できます"}
        try:
            import webview

            if hasattr(webview, "FileDialog"):
                dialog = webview.FileDialog.FOLDER
            else:
                dialog = webview.FOLDER_DIALOG
            result = self._window.create_file_dialog(dialog, allow_multiple=False)
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (tuple, list)) else result
            return {"ok": True, "kind": kind, "path": str(path)}
        except Exception as exc:  # noqa: BLE001 - 失敗はGUIへ表示するだけ
            return {"ok": False, "error": str(exc)}

    # ── 全自動実行（run_pipeline.py） ───────────────────────────────────────

    def start_pipeline(self, payload: dict) -> dict:
        payload = dict(payload or {})
        with self._lock:
            if self._pipeline_process and self._pipeline_process.poll() is None:
                return {"ok": False, "error": "全自動実行はすでに実行中です"}
        try:
            cmd, out_dir = pipeline_command(payload)
        except (ValueError, FileNotFoundError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self._pipeline_cancelled = False
            self._pipeline_state = {
                "running": True, "progress": 0,
                "log": "$ " + subprocess.list2cmdline(cmd),
                "message": "全自動実行を開始しました", "exit_code": None,
                "stages": [], "out": str(out_dir), "summary": None,
            }
        threading.Thread(target=self._pipeline_worker, args=(cmd, out_dir),
                         daemon=True).start()
        return {"ok": True, "out": str(out_dir)}

    def _pipeline_worker(self, cmd: list[str], out_dir: Path) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        yolo_config_dir = APP_DIR / ".ultralytics"
        yolo_config_dir.mkdir(exist_ok=True)
        env["YOLO_CONFIG_DIR"] = str(yolo_config_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        lines: deque[str] = deque(maxlen=PIPELINE_LOG_LINES)
        lines.append("$ " + subprocess.list2cmdline(cmd))
        detect_done = False        # [stage] detect 以降は検出のフレーム進捗を使わない
        video_no, video_total = 1, 1
        detect_base = PIPELINE_STAGE_PROGRESS["view"]
        detect_span = PIPELINE_STAGE_PROGRESS["detect"] - detect_base
        try:
            process = subprocess.Popen(
                cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags, env=env,
            )
            with self._lock:
                self._pipeline_process = process
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                message = ""
                progress = None
                stage = parse_pipeline_stage(line)
                if stage:
                    if stage["name"] == "detect":
                        detect_done = True
                    target = PIPELINE_STAGE_PROGRESS.get(stage["name"])
                    if target is not None:
                        progress = target
                    message = (f"{stage['title']} — {stage['message']}"
                               if stage["message"] else stage["title"])
                elif re.match(r"^\[(view|gps|pcap|sync|track|report)\]", line):
                    message = line[:160]
                else:
                    video_match = re.search(r"=== 動画\s+(\d+)/(\d+)", line)
                    frame_match = re.search(r"processing frame\s+(\d+)/(\d+)", line)
                    if video_match:
                        video_no = int(video_match.group(1))
                        video_total = max(1, int(video_match.group(2)))
                        message = f"1.検出 — 動画 {video_no}/{video_total} を解析しています"
                    elif frame_match and not detect_done and int(frame_match.group(2)):
                        ratio = ((video_no - 1)
                                 + int(frame_match.group(1)) / int(frame_match.group(2))
                                 ) / video_total
                        progress = detect_base + round(min(1.0, ratio) * detect_span)
                with self._lock:
                    state = self._pipeline_state
                    state["log"] = "\n".join(lines)
                    if message:
                        state["message"] = message
                    if progress is not None:
                        state["progress"] = max(state["progress"], min(99, progress))
                    if stage:
                        state["stages"] = [*state["stages"], stage]
            code = process.wait()
        except Exception as exc:  # noqa: BLE001 - 失敗はGUIへ表示して終わる
            lines.append(f"[GUI ERROR] {exc}")
            code = 1
        summary = read_pipeline_summary(out_dir)
        with self._lock:
            cancelled = self._pipeline_cancelled
            state = self._pipeline_state
            state["running"] = False
            state["exit_code"] = code
            state["log"] = "\n".join(lines)
            state["summary"] = summary
            if code == 0:
                state["progress"] = 100
                state["message"] = "全自動実行が完了しました"
            elif cancelled:
                state["message"] = "全自動実行を中止しました"
            else:
                state["message"] = "全自動実行でエラーが発生しました"
            self._pipeline_process = None

    def get_pipeline_state(self) -> dict:
        with self._lock:
            state = self._pipeline_state
            return {**state, "stages": [dict(entry) for entry in state["stages"]]}

    def cancel_pipeline(self) -> dict:
        """実行中の run_pipeline.py を子プロセスごと止める。"""
        with self._lock:
            process = self._pipeline_process
            if process is None or process.poll() is not None:
                return {"ok": False, "error": "実行中の全自動実行はありません"}
            self._pipeline_cancelled = True
            pid = process.pid
        try:
            if os.name == "nt":
                # YOLO検出などの孫プロセスも止めるためプロセスツリーごと終了する
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
        except OSError as exc:
            return {"ok": False, "error": f"中止に失敗しました: {exc}"}
        return {"ok": True}

    # ── 確認画面（候補ごとの手動修正） ──────────────────────────────────────

    def load_results(self, out_value: str) -> dict:
        """出力フォルダの reviewed CSV と実行レポートを確認画面用に読み込む。"""
        out_dir = Path(str(out_value or "").strip()).expanduser()
        if not out_dir.is_dir():
            return {"ok": False, "error": f"出力フォルダが見つかりません: {out_dir}"}
        reviewed = out_dir / "reviewed_overtaking_events.csv"
        if not reviewed.is_file():
            return {"ok": False, "error": (
                f"レビュー結果がありません: {reviewed.name}\n"
                "全自動実行を完了させるか、出力フォルダを確認してください")}
        self._media_registry.clear()
        try:
            events = read_events_csv(reviewed, self._media_registry, self._base_url)
        except Exception as exc:  # noqa: BLE001 - 読み込み失敗はGUIへ表示
            return {"ok": False, "error": f"レビュー結果を読み込めません: {exc}"}
        for row in events:
            # 危険感は read_events_csv が "0"〜"3" に整えている。車線数は
            # 全自動実行のCSVには無い列なので、ここで必ずキーを作っておく
            # （保存時は先頭行のキー一覧が列になるため）。
            row["lane_count"] = str(row.get("lane_count") or "").strip()
        summary = read_pipeline_summary(out_dir)
        return {
            "ok": True, "events": events, "summary": summary,
            "out": str(out_dir), "offset": resolve_offset(out_dir, summary),
        }

    def get_event_points(self, payload: dict) -> dict:
        """候補1件の点群ビュー用データ（フレーム別点群・軌跡・LC）を返す。

        report.html の点群ビューアと同じ読み取り・間引き（make_report）を使うので、
        `--no-report` で実行した出力フォルダでも `out/lidar/overtaking_*.csv` が
        あれば表示できる。座標は2桁へ丸め、`p` は [x,y,z,x,y,z,...] の平坦配列。
        """
        payload = dict(payload or {})
        out_dir = Path(str(payload.get("out") or "").strip()).expanduser()
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            return {"ok": False, "error": "候補IDがありません"}
        csv_path = out_dir / "lidar" / f"overtaking_{_safe_id(event_id)}.csv"
        if not csv_path.is_file():
            return {"ok": False, "error": (
                f"点群CSVがありません: {csv_path.name}\n"
                "点群追跡（段階4）で車両を割り当てられなかった候補です")}
        try:
            cloud = load_point_frames(csv_path)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗でGUIを止めない
            return {"ok": False, "error": f"点群CSVを読み込めません: {exc}"}
        if not cloud["frames"]:
            return {"ok": False, "error": "点群CSVに点がありません"}
        metrics, _warnings, _error = load_metrics(out_dir, _safe_id(event_id))
        trajectory = load_trajectory(out_dir, _safe_id(event_id))
        frames = []
        for frame in cloud["frames"]:
            coords: list[float] = []
            intensities: list[int] = []
            for x, y, z, intensity in frame["points"]:
                coords.extend((round(x, 2), round(y, 2), round(z, 2)))
                intensities.append(int(intensity))
            frames.append({"f": frame["f"], "t": round(frame["t"], 3),
                           "p": coords, "i": intensities})
        bounds = cloud.get("bounds") or {"x": [-1, 1], "y": [-1, 1], "z": [-1, 1]}
        return {"ok": True, "data": {
            "id": event_id,
            "frames": frames,
            "bounds": {key: [round(v, 2) for v in value]
                       for key, value in bounds.items()},
            "points": cloud.get("points", 0),
            "rawPoints": cloud.get("raw_points", 0),
            "perFrame": cloud.get("limit", 0),
            "lc": metrics.get("lc"),
            "traj": [{"t": round(p["t"], 3), "x": round(p["x"], 2),
                      "y": round(p["y"], 2), "z": round(p["z"], 2), "ph": p["ph"]}
                     for p in trajectory],
        }}

    def save_reviews(self, payload: dict) -> dict:
        """確認画面の修正（採否・車種・危険感・車線数）を reviewed CSV へ保存する。"""
        payload = dict(payload or {})
        try:
            path = save_reviewed_events(
                Path(str(payload.get("out") or "").strip()),
                list(payload.get("events") or []),
                float(payload.get("offset") or 0),
                allow_empty=True,
            )
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def generate_final_excel(self, payload: dict) -> dict:
        """最終Excelを作り直す。整理ID・被験者・収集日・PCAPは実行レポートから引き継ぐ。"""
        payload = dict(payload or {})
        out_dir = Path(str(payload.get("out") or "").strip()).expanduser()
        if not out_dir.is_dir():
            return {"ok": False, "error": "出力フォルダが見つかりません"}
        try:
            report = json.loads(
                (out_dir / "pipeline_report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        info = report.get("info") or {}
        try:
            result = generate_final_workbook(
                out_dir,
                organize_id=str(report.get("organize_id") or ""),
                subject_id=str(report.get("subject_id") or ""),
                collection_date=str(report.get("collection_date") or ""),
                offset=float(payload.get("offset") or 0),
                pcap_path=str(report.get("pcap") or ""),
                pcap_start_unix=info.get("PCAP開始Unix時刻"),
            )
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def open_path(self, value: str) -> dict:
        path = Path(str(value or ""))
        if not path.exists():
            return {"ok": False, "error": f"パスが存在しません: {path}"}
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}


def run_check() -> None:
    import csv
    import tempfile

    for name in ("index.html", "style.css", "app.js"):
        assert (AUTO_UI_DIR / name).is_file(), name
    index_html = (AUTO_UI_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (AUTO_UI_DIR / "app.js").read_text(encoding="utf-8")

    # 実行画面のUI要素とJSの導線（車線数の事前入力を含む）
    for element_id in ("sessionPath", "outPath", "pipelineDetectMode", "pipelineLane",
                       "startPipeline", "cancelPipeline", "openExisting", "pipelineBar",
                       "pipelineStages", "pipelineLog"):
        assert f'id="{element_id}"' in index_html, element_id
    assert 'name="pipelineView"' in index_html
    assert "async function startPipeline()" in app_js
    assert "async function pollPipeline()" in app_js
    assert "api().start_pipeline(" in app_js and "api().get_pipeline_state(" in app_js
    assert "api().cancel_pipeline(" in app_js

    # 確認画面のUI要素: クリップ・採否・車種に加えて危険感と車線数を編集できる
    for element_id in ("eventRows", "clipPlayer", "vehicleSelect", "dangerSelect",
                       "laneSelect", "previewReason", "saveReviews", "generateFinal",
                       "openReport", "openOutDir", "summaryCounts", "manualList"):
        assert f'id="{element_id}"' in index_html, element_id
    assert 'data-status="採用"' in index_html and 'data-status="除外"' in index_html
    assert "async function loadResults(" in app_js
    assert "api().load_results(" in app_js and "api().save_reviews(" in app_js
    assert "api().generate_final_excel(" in app_js
    assert "lane_count" in app_js and "danger_level" in app_js
    # 完了したら確認画面へ自動で切り替える
    assert "goStep('confirm')" in app_js
    # 点群ビュー: クリップ/点群のタブと、report.html 相当のビューア
    for element_id in ("stageClip", "stageCloud", "cloudHost"):
        assert f'id="{element_id}"' in index_html, element_id
    assert "function buildCloudViewer(" in app_js
    assert "async function ensureCloud()" in app_js
    assert "api().get_event_points(" in app_js
    assert "function renderCloudView(" in app_js and "drawCloudPassBand" in app_js

    # 段階行の解析と進捗率。run_pipeline 側が [stage] 行を出すことも突き合わせる
    assert parse_pipeline_stage("[stage] detect 1.検出 [ok] 24 件の候補") == {
        "name": "detect", "title": "1.検出", "status": "ok", "message": "24 件の候補"}
    assert parse_pipeline_stage("[stage] report 8.レポート [ok] ") == {
        "name": "report", "title": "8.レポート", "status": "ok", "message": ""}
    assert parse_pipeline_stage("processing frame 3/100") is None
    assert PIPELINE_STAGE_PROGRESS["report"] == 100
    pipeline_source = (SRC_DIR / "run_pipeline.py").read_text(encoding="utf-8")
    assert 'print(f"[stage] {name} {title} [{status}] {message}"' in pipeline_source
    # 実行時の車線数は run_pipeline の --lane-count として渡す（reviewed CSV へ入る）
    assert '"--lane-count"' in pipeline_source

    # コマンドの組み立て
    with tempfile.TemporaryDirectory() as temp_value:
        session = Path(temp_value) / "ID1"
        session.mkdir()
        cmd, out_dir = pipeline_command({
            "session": str(session), "view": "front", "subject": "ID1",
            "date": "260701", "organize_id": "4", "detect_mode": "skip",
            "lane_count": "2", "offset": "4.5", "extra": "--skip-sync-check",
        })
        assert cmd[1].endswith("run_pipeline.py") and cmd[2] == str(session), cmd
        joined = " ".join(cmd)
        for expected in ("--view front", "--subject ID1", "--date 260701",
                         "--organize-id 4", "--lane-count 2", "--skip-detect",
                         "--offset 4.5", "--skip-sync-check"):
            assert expected in joined, (expected, cmd)
        no_lane, _ = pipeline_command({"session": str(session)})
        assert "--lane-count" not in no_lane, no_lane
        assert "--redetect" not in cmd and "--out" not in cmd, cmd
        assert out_dir == session / "out_auto", out_dir
        redo_cmd, redo_out = pipeline_command({
            "session": str(session), "detect_mode": "redetect",
            "out": str(session / "custom"),
        })
        assert "--redetect" in redo_cmd and "--skip-detect" not in redo_cmd, redo_cmd
        assert redo_out == session / "custom", redo_out
        for bad_payload, expected_error in (
            ({"session": ""}, ValueError),
            ({"session": str(session / "missing")}, FileNotFoundError),
            ({"session": str(session), "offset": "abc"}, ValueError),
        ):
            try:
                pipeline_command(bad_payload)
                raise AssertionError(f"エラーになりません: {bad_payload}")
            except expected_error:
                pass

    # 実行→確認の通し: 疑似コマンドでの実行、結果読み込み、修正保存、最終Excel
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out_auto"
        out_dir.mkdir()
        (out_dir / "pipeline_report.json").write_text(json.dumps({
            "elapsed_seconds": 12.3, "view": "front", "view_source": "auto",
            "subject_id": "ID1", "collection_date": "260701", "organize_id": "4",
            "pcap": "2026-07-01.pcap",
            "info": {"時刻オフセット(秒)": 4.242, "PCAP開始Unix時刻": 1000.0},
            "stages": [
                {"name": "detect", "title": "1.検出", "status": "ok", "message": "24 件"},
                {"name": "review", "title": "6.自動レビュー", "status": "ok",
                 "message": "", "total": 2, "accepted": 1, "unreviewed": 1,
                 "rejected": 0},
            ],
            "warnings": ["警告のサンプル"],
            "manual_actions": ["人手項目のサンプル"],
        }, ensure_ascii=False), encoding="utf-8")
        (out_dir / "reviewed_overtaking_events.csv").write_text(
            "event_id,class,source_video,local_t_start_s,t_start_s,peak_t_s,t_end_s,"
            "review_status,review_reason,danger_level,class_reviewed\n"
            "001,普通,GX010731.MP4,152.44,152.44,153.0,154.0,採用,,0,False\n"
            "002,大型,GX010731.MP4,160.0,160.0,161.0,162.0,未確認,追跡の確信度が低い,0,False\n",
            encoding="utf-8-sig",
        )
        (out_dir / "report.html").write_text("<!doctype html>", encoding="utf-8")

        summary = read_pipeline_summary(out_dir)
        assert summary and summary["accepted"] == 1 and summary["total"] == 2, summary
        assert summary["organize_id"] == "4" and summary["report_html"], summary
        assert read_pipeline_summary(Path(temp_value) / "missing") is None
        assert resolve_offset(out_dir, summary) == 4.242
        assert resolve_offset(out_dir, None) == 0.0  # sync.json も無ければ 0

        api = AutoApi()
        assert api.get_pipeline_state()["running"] is False
        assert api.cancel_pipeline()["ok"] is False
        assert api.start_pipeline({"session": ""})["ok"] is False

        # 疑似コマンドでワーカーを通し、段階と進捗が取り込まれること
        script = (
            "print('processing frame 50/100')\n"
            "print('[stage] detect 1.\\u691c\\u51fa [ok] 24 \\u4ef6')\n"
            "print('[stage] report 8.\\u30ec\\u30dd\\u30fc\\u30c8 [ok] done')\n"
        )
        api._pipeline_state.update({"running": True, "out": str(out_dir)})
        api._pipeline_worker([sys.executable, "-X", "utf8", "-c", script], out_dir)
        finished = api.get_pipeline_state()
        assert finished["running"] is False and finished["exit_code"] == 0, finished
        assert finished["progress"] == 100, finished
        assert [s["name"] for s in finished["stages"]] == ["detect", "report"], finished
        assert "processing frame 50/100" in finished["log"], finished
        assert finished["summary"]["accepted"] == 1, finished
        assert "完了" in finished["message"], finished

        fail_api = AutoApi()
        fail_api._pipeline_state.update({"running": True, "out": str(out_dir)})
        fail_api._pipeline_worker([sys.executable, "-c", "import sys; sys.exit(3)"],
                                  out_dir)
        failed = fail_api.get_pipeline_state()
        assert failed["exit_code"] == 3 and "エラー" in failed["message"], failed
        assert failed["summary"] is not None, failed

        # 結果読み込み: 全行に lane_count キーが付き、危険感は正規化される
        loaded = api.load_results(str(out_dir))
        assert loaded["ok"], loaded
        assert loaded["offset"] == 4.242 and loaded["summary"]["total"] == 2, loaded
        events = loaded["events"]
        assert len(events) == 2 and all("lane_count" in e for e in events), events
        assert events[0]["danger_level"] == "0", events[0]
        assert events[1]["review_reason"].startswith("追跡の確信度"), events[1]
        assert api.load_results(str(out_dir / "missing"))["ok"] is False
        empty_dir = Path(temp_value) / "empty"
        empty_dir.mkdir()
        assert api.load_results(str(empty_dir))["ok"] is False

        # 手動修正の保存: 危険感・車線数・採否・車種が reviewed CSV に残る
        events[0]["danger_level"] = "2"
        events[0]["lane_count"] = "2"
        events[1]["review_status"] = "採用"
        events[1]["class"] = "普通"
        events[1]["class_reviewed"] = True
        events[1]["danger_level"] = "1"
        events[1]["lane_count"] = "3"
        saved = api.save_reviews({"out": str(out_dir), "events": events,
                                  "offset": loaded["offset"]})
        assert saved["ok"], saved
        with Path(saved["path"]).open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows[0]["danger_level"] == "2" and rows[0]["lane_count"] == "2", rows[0]
        assert rows[1]["review_status"] == "採用" and rows[1]["lane_count"] == "3", rows[1]
        assert rows[1]["class"] == "普通", rows[1]

        # 再読み込みで修正が引き継がれる（クリップは無いので clip_url は空）
        reloaded = api.load_results(str(out_dir))
        assert reloaded["events"][0]["lane_count"] == "2", reloaded["events"][0]
        assert reloaded["events"][1]["review_status"] == "採用", reloaded["events"][1]

        # 点群ビュー: lidar/overtaking_*.csv と edit CSV から表示用データを組み立てる
        lidar_dir = out_dir / "lidar"
        lidar_dir.mkdir()
        roi_header = ",".join((
            "Frame", "Time", "Laser ID", "Intensity", "Distance",
            "Azimuth", "Elevation", "Selected", "Points:0", "Points:1", "Points:2"))
        (lidar_dir / "overtaking_001.csv").write_text(
            roi_header
            + "\n0,10.0,0,50,5,0,0,1,1.5,8.0,0.4\n0,10.0,0,80,5,0,0,1,1.6,7.5,0.5\n"
            + "1,10.1,0,120,5,0,0,1,1.5,6.0,0.4\n",
            encoding="utf-8-sig",
        )
        edit_dir = out_dir / "lidar_edit"
        edit_dir.mkdir()
        (edit_dir / "overtaking_001_edit.csv").write_text(
            "unixtime,X,Y,Z,distance(m),speed(km/h)\n"
            "10.0,-0.5,8,0,8.02,\n10.1,-1.0,7,0,7.10,12\n10.2,-1.5,5,0,5.22,8\n"
            "10.3,-2.0,2.5,0,3.20,7\n10.4,-2.5,1.0,0,2.69,14\n10.5,-1.0,3.2,0,3.35,16\n",
            encoding="utf-8-sig",
        )
        points = api.get_event_points({"out": str(out_dir), "event_id": "001"})
        assert points["ok"], points
        data = points["data"]
        assert len(data["frames"]) == 2, data["frames"]
        first = data["frames"][0]
        assert len(first["p"]) == 3 * len(first["i"]) == 6, first
        assert first["p"][0] == 1.5 and first["i"] == [50, 80], first
        assert data["bounds"]["y"] == [6.0, 8.0], data["bounds"]
        assert data["lc"] == 1.0, data          # edit CSV の実測LC（web_gui checkと同じ例）
        assert len(data["traj"]) == 6 and any(p["ph"] for p in data["traj"]), data["traj"]
        # 点群CSVが無い候補・IDなしはエラー文で返し、GUIは文言をそのまま表示する
        missing_points = api.get_event_points({"out": str(out_dir), "event_id": "002"})
        assert missing_points["ok"] is False and "点群CSV" in missing_points["error"]
        assert api.get_event_points({"out": str(out_dir)})["ok"] is False

        # 最終Excel: 実行レポートのID類を引き継ぎ、末尾の「車線数」列に値が入る
        from zipfile import ZipFile
        final = api.generate_final_excel({"out": str(out_dir),
                                          "offset": loaded["offset"]})
        assert final["ok"] and final["rows"] == 2, final
        with ZipFile(final["path"]) as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "車線数" in sheet_xml, sheet_xml
        # S列（19列目）= 車線数。1行目の候補001は 2、2行目の候補002は 3
        assert '<c r="S2"><v>2</v></c>' in sheet_xml, sheet_xml
        assert '<c r="S3"><v>3</v></c>' in sheet_xml, sheet_xml
        # 既存列の入力規則は動かない（G=危険感 / R=車種）
        assert 'sqref="G2:G51"' in sheet_xml and 'sqref="R2:R51"' in sheet_xml

    # 設定の読み書き: 消えたフォルダは復元しない
    with tempfile.TemporaryDirectory() as temp_value:
        keep_dir = Path(temp_value) / "keep"
        keep_dir.mkdir()
        original_path = globals()["SETTINGS_PATH"]
        globals()["SETTINGS_PATH"] = Path(temp_value) / "auto_gui_settings.json"
        try:
            write_settings({"session": str(keep_dir),
                            "out": str(Path(temp_value) / "gone"),
                            "subject": "ID1"})
            settings = read_settings()
            assert settings["session"] == str(keep_dir), settings
            assert settings["out"] == "" and settings["subject"] == "ID1", settings
        finally:
            globals()["SETTINGS_PATH"] = original_path

    print("Auto GUI check OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--serve-only", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.check:
        run_check()
        return
    api = AutoApi()
    server, url = start_server(api, args.port, ui_dir=AUTO_UI_DIR)
    if args.serve_only:
        print(url, flush=True)
        threading.Event().wait()
        return
    try:
        import webview
    except ImportError:
        server.shutdown()
        raise SystemExit("pywebviewがありません。pip install -r requirements.txt を実行してください。")
    window = webview.create_window(
        "全自動実行 - 自転車追い越し解析", url=url, js_api=api,
        width=1280, height=840, min_size=(980, 680),
    )
    api.attach(window, url)
    webview.start(debug=False)
    server.shutdown()


if __name__ == "__main__":
    main()
