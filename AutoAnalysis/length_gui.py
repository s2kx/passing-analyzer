#!/usr/bin/env python3
"""車長込み相対速度への変換（AutoAnalysis）の専用GUI。

全自動実行GUI（auto_gui.py）と同じ作り（pywebview + ローカルHTTP配信）で、
AutoAnalysis の3段階を画面から操作する。

  実行        解析結果フォルダ（セッションごとの lidar_edit/ final/ clips/）と保存先フォルダを
              選び、length_pipeline.py（写真の切り出し → ChatGPT で車長推定 → 相対速度の変換）を
              ボタン1つで起動して進捗とログを見る。どの段階も未処理分だけを扱うので、
              何度押しても結果が増えるだけで壊れない。
  確認・修正  保存先の car_lengths.csv の車長を、切り出した写真と GPT の判断理由を見ながら
              1件ずつ確かめ、間違いは手入力で直す（source=manual。再推定でも上書きされない）。
              直した後にもう一度「実行」を押すと Excel に反映される。

PC ごとに違うものはコードの外に置く:
  - フォルダの指定        AutoAnalysis/length_gui_settings.json（.gitignore 済み）
  - ChatGPT の API キー   AutoAnalysis/.env（.gitignore 済み。画面から保存できる。値は表示しない）
  - 写真・車長一覧・変換結果  保存先フォルダ（既定: 解析結果フォルダの隣の <名前>_車長変換）

画面は web_ui_length/ の HTML/CSS/JS。本体（web_gui.py 等）には依存せず、画面の配信・写真の配信
（/image/）・POST /api/ をこのファイルだけで行う。リポジトリ内（AutoAnalysis/ 配下）でも、
make_dist.py で作る配布版（単独フォルダ）でも同じコードで動く。

    portable/python/python.exe AutoAnalysis/length_gui.py            # GUIを起動（車長変換GUI.bat と同じ）
    portable/python/python.exe AutoAnalysis/length_gui.py --check    # 画面なしで部品を検証する
    portable/python/python.exe AutoAnalysis/length_gui.py --serve-only  # ブラウザで開くURLを出す
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
# リポジトリ内（AutoAnalysis/ 配下）でも配布版（単独フォルダ）でも動くよう、本体の web_gui.py が隣に
# あればその階層を、無ければ自分の階層をルート（portable/python・yolov8n.pt・.ultralytics の置き場）にする。
ROOT = HERE.parent if (HERE.parent / "web_gui.py").is_file() else HERE
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from car_length_gpt import (  # noqa: E402
    DEFAULT_MODEL, ENV_PATH, IMAGE_TYPES, find_api_key, load_env, parse_answer, read_rows, write_rows,
)

UI_DIR = HERE / "web_ui_length"
SETTINGS_PATH = HERE / "length_gui_settings.json"
ENV_FILE = ENV_PATH                       # この PC だけの API キー置き場（car_length_gpt が読む）
RUNNER = HERE / "length_pipeline.py"
WORK_SUFFIX = "_車長変換"
NO_SRC = "解析結果フォルダを選んでください"

STAGES = ("extract", "gpt", "convert")
STAGE_TITLES = {"extract": "写真の切り出し", "gpt": "車長の推定", "convert": "相対速度の変換"}
TITLE_TO_STAGE = {title: name for name, title in STAGE_TITLES.items()}
LOG_LINES = 400
MIN_LENGTH_M, MAX_LENGTH_M = 1.0, 30.0   # 手入力の車長として受け付ける範囲

# length_pipeline.py の出力。段階の見出し「[1/3 写真の切り出し] script ...」、
# 終わり「[1/3 写真の切り出し] 完了（4秒）」「[...] 失敗（終了コード 1、3秒）。ここで止めます。」
STAGE_LINE_RE = re.compile(r"^\[(\d)/3 ([^\]]+)\]\s*(.*)$")
# extract_car_images.py: 「== w5o/ID1（front）: 12 イベント」「  001: OK 001.jpg」
EXTRACT_SESSION_RE = re.compile(r"^== (\S+)（\w+）: (\d+) イベント")
EXTRACT_EVENT_RE = re.compile(r"^  (\S+): (.+)$")
# car_length_gpt.py: 「推定する対象: 5 件（...）」「[2/5] w6b/obihiro 011: トヨタ ... 全長 4.260 m (確度 0.90)」
GPT_TOTAL_RE = re.compile(r"^推定する対象: (\d+) 件")
GPT_ITEM_RE = re.compile(r"^\[(\d+)/(\d+)\] (.+)$")


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def detection_python() -> Path:
    """解析に使う Python。portable/python（起動 bat が用意する）があればそれ、無ければ今の Python。"""
    portable = ROOT / "portable" / "python" / "python.exe"
    return portable if portable.is_file() else Path(sys.executable)


# ---------------------------------------------------------------- フォルダの指定

def default_work(src: Path) -> Path:
    """保存先フォルダの既定: 解析結果フォルダの隣の「<フォルダ名>_車長変換」（length_pipeline と同じ）。"""
    return src.parent / f"{src.name}{WORK_SUFFIX}"


def read_settings() -> dict:
    """前回のフォルダ指定。既定値は持たない（選ぶまで何も読み込まない）。
    保存先を自分で選んでいなければ解析結果フォルダから決める。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    src = str(data.get("src") or "").strip()
    custom = str(data.get("work") or "").strip() if data.get("work_custom") else ""
    work = custom or (str(default_work(Path(src))) if src else "")
    return {"src": src, "work": work, "work_custom": bool(custom)}


def write_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class Workspace:
    """解析結果フォルダ（入力）と保存先フォルダ（出力）の対。未選択なら両方 None。"""
    src: Path | None
    work: Path | None

    @property
    def chosen(self) -> bool:
        return self.src is not None and self.work is not None

    def _under_work(self, *parts: str) -> Path:
        assert self.work is not None, NO_SRC
        return self.work.joinpath(*parts)

    @property
    def lengths(self) -> Path:
        return self._under_work("car_lengths.csv")

    @property
    def images(self) -> Path:
        return self._under_work("car_images")

    @property
    def frames(self) -> Path:
        return self._under_work("car_frames")

    @property
    def raw(self) -> Path:
        return self._under_work("gpt_raw")

    @property
    def converted(self) -> Path:
        return self._under_work("converted")

    @property
    def summary(self) -> Path:
        return self._under_work("converted", "conversion_summary.csv")

    def problem(self) -> str:
        """実行できない理由。無ければ空文字。"""
        if self.src is None or self.work is None:
            return NO_SRC
        if not self.src.is_dir():
            return f"解析結果フォルダがありません: {self.src}"
        if self.work == self.src or self.src in self.work.parents:
            return "保存先フォルダは解析結果フォルダの中に置けません"
        if not any(p.is_dir() for name in ("lidar_edit", "clips") for p in self.src.rglob(name)):
            return "解析結果フォルダにセッション（lidar_edit / clips を含むフォルダ）がありません"
        return ""


def workspace() -> Workspace:
    settings = read_settings()
    if not settings["src"] or not settings["work"]:
        return Workspace(None, None)
    return Workspace(Path(settings["src"]).expanduser(), Path(settings["work"]).expanduser())


def write_api_key(key: str) -> None:
    """OPENAI_API_KEY を .env に書く。既存の行（KEY=値 や sk- だけの行）は置き換える。値はログに出さない。"""
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    kept = [line for line in lines
            if not re.match(r"^\s*(?:export\s+)?OPENAI_API_KEY\s*[=:]", line) and not line.strip().startswith("sk-")]
    kept.append(f"OPENAI_API_KEY={key}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 実行

def runner_command(ws: Workspace) -> list[str]:
    """3段階を既定の設定で全部実行する（採用イベントのみ・既存は飛ばす・車長が無い行は空欄）。"""
    assert ws.chosen, NO_SRC
    return [str(detection_python()), str(RUNNER), "--src", str(ws.src), "--work", str(ws.work)]


class RunProgress:
    """length_pipeline.py の出力行から進捗率(%)・表示メッセージ・段階の状態を読み取る。

    3段階で 100% を等分し、段階の中は 切り出し=処理したイベント数、GPT=推定した件数で
    進める。変換は一瞬で終わるので開始/完了だけ。
    """

    def __init__(self) -> None:
        self.index = -1
        self.extract_total = 0
        self.extract_done = 0
        self.gpt_total = 0

    def current(self) -> str | None:
        return STAGES[self.index] if 0 <= self.index < len(STAGES) else None

    def span(self) -> tuple[float, float]:
        index = max(self.index, 0)
        return 100.0 * index / len(STAGES), 100.0 * (index + 1) / len(STAGES)

    def feed(self, line: str) -> tuple[int | None, str, dict | None]:
        """1行を読み、(進捗率 or None, メッセージ, 段階の記録 or None) を返す。"""
        match = STAGE_LINE_RE.match(line)
        if match:
            title, rest = match.group(2), match.group(3).strip()
            name = TITLE_TO_STAGE.get(title, title)
            if rest.startswith("完了"):
                return round(self.span()[1]), f"{title} — {rest}", \
                    {"name": name, "title": title, "status": "ok", "message": rest}
            if rest.startswith("失敗"):
                return None, f"{title} — {rest}", \
                    {"name": name, "title": title, "status": "failed", "message": rest}
            self.index = STAGES.index(name) if name in STAGES else self.index + 1
            return round(self.span()[0]), f"{title} を開始しました", \
                {"name": name, "title": title, "status": "running", "message": ""}

        low, high = self.span()
        current = self.current()
        match = EXTRACT_SESSION_RE.match(line)
        if match and current == "extract":
            self.extract_total += int(match.group(2))
            return None, f"{STAGE_TITLES['extract']} — {match.group(1)}: {match.group(2)} イベント", None
        match = EXTRACT_EVENT_RE.match(line)
        if match and current == "extract":
            self.extract_done += 1
            ratio = min(self.extract_done / self.extract_total, 0.95) if self.extract_total else 0.0
            return round(low + (high - low) * ratio), f"切り出し {match.group(1)}: {match.group(2)}", None
        match = GPT_TOTAL_RE.match(line)
        if match:
            self.gpt_total = int(match.group(1))
            return None, f"{STAGE_TITLES['gpt']} — 対象 {self.gpt_total} 件", None
        match = GPT_ITEM_RE.match(line)
        if match and current == "gpt":
            done, total = int(match.group(1)), max(1, int(match.group(2)))
            return round(low + (high - low) * min(done / total, 1.0)), \
                f"{STAGE_TITLES['gpt']} {done}/{total} — {match.group(3)[:120]}", None
        if line.startswith(("結果:", "完了:", "最終Excel", "一覧:", "解析結果フォルダ:", "保存先フォルダ:")):
            return None, line[:160], None
        return None, "", None


# ---------------------------------------------------------------- 集計

def count_images(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for path in folder.iterdir() if path.suffix.lower() in IMAGE_TYPES)


def first_image(folder: Path, event_id: str) -> Path | None:
    """car_length_gpt.find_images と同じ規則（ファイル名の先頭がイベントID）で1枚目を返す。"""
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob(f"{event_id}*")):
        if path.suffix.lower() in IMAGE_TYPES and re.split(r"[_\-\s]", path.stem, maxsplit=1)[0] == event_id:
            return path
    return None


def build_overview(ws: Workspace) -> dict:
    """セッションごとの進み具合（写真・車長・変換）と、APIキーの有無。キーの値は返さない。

    フォルダが未選択なら何も読まず、空の一覧を返す。
    """
    env = load_env(ENV_FILE)
    common = {
        "api_key": bool(find_api_key(env)),
        "model": os.environ.get("OPENAI_MODEL") or env.get("OPENAI_MODEL") or DEFAULT_MODEL,
        "env_path": str(ENV_FILE),
        "problem": ws.problem(),
    }
    counted = ("images", "lengths", "with_length", "manual", "converted", "missing")
    if not ws.chosen:
        return {**common, "sessions": [], "totals": {key: 0 for key in counted},
                "src": "", "work": "", "converted_dir": "", "summary_updated": ""}
    rows_by: dict[str, list[dict]] = defaultdict(list)
    for row in read_rows(ws.lengths):
        rows_by[row.get("session", "")].append(row)
    conversion: dict[str, dict[str, int]] = defaultdict(lambda: {"converted": 0, "missing": 0})
    for row in read_csv(ws.summary):
        if row.get("row"):
            conversion[row.get("session", "")]["converted" if row.get("status") == "変換済み" else "missing"] += 1
    keys = set(rows_by) | set(conversion)
    if ws.src.is_dir():
        for name in ("lidar_edit", "clips"):
            keys |= {p.parent.relative_to(ws.src).as_posix() for p in ws.src.rglob(name) if p.is_dir()}
    keys.discard("")

    sessions = []
    for skey in sorted(keys):
        rows = rows_by.get(skey, [])
        sessions.append({
            "session": skey, "has_clips": (ws.src / skey / "clips").is_dir(),
            "images": count_images(ws.images / skey),
            "lengths": len(rows),
            "with_length": sum(1 for row in rows if row.get("car_length_m")),
            "manual": sum(1 for row in rows if row.get("source") == "manual"),
            **conversion.get(skey, {"converted": 0, "missing": 0}),
        })
    summary_updated = ""
    if ws.summary.is_file():
        summary_updated = datetime.fromtimestamp(ws.summary.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {
        **common,
        "sessions": sessions,
        "totals": {key: sum(s[key] for s in sessions) for key in counted},
        "src": str(ws.src), "work": str(ws.work),
        "converted_dir": str(ws.converted) if ws.converted.is_dir() else "",
        "summary_updated": summary_updated,
    }


# ---------------------------------------------------------------- API

class LengthApi:
    """web_ui_length の js_api。実行（run）と確認・修正（review）の両画面を支える。"""

    def __init__(self) -> None:
        self._window = None
        self._base_url = ""
        self._media_registry: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._cancelled = False
        self._state = {
            "running": False, "progress": 0, "log": "待機中", "message": "待機中",
            "exit_code": None, "stages": [],
        }

    def attach(self, window, base_url: str) -> None:
        self._window = window
        self._base_url = base_url

    # ── フォルダ・APIキー ────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return read_settings()

    def save_settings(self, data: dict) -> dict:
        """解析結果フォルダと保存先を保存する。保存先が空なら既定（隣の <名前>_車長変換）に戻す。"""
        data = dict(data or {})
        src = str(data.get("src") or "").strip().strip('"')
        work = str(data.get("work") or "").strip().strip('"')
        custom = bool(src) and bool(work) \
            and Path(work).expanduser().resolve() != default_work(Path(src).expanduser()).resolve()
        write_settings({"src": src, "work": work if custom else "", "work_custom": custom})
        return {"ok": True, **read_settings()}

    def choose_folder(self, kind: str) -> dict:
        """フォルダ選択（kind: src / work）。デスクトップ版だけ。ブラウザでは欄に直接貼り付ける。"""
        if self._window is None:
            return {"ok": False, "error": "フォルダ選択はデスクトップ版だけです。欄にパスを貼り付けてください"}
        try:
            import webview

            dialog = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            result = self._window.create_file_dialog(dialog, allow_multiple=False)
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (tuple, list)) else result
            return {"ok": True, "kind": kind, "path": str(path)}
        except Exception as exc:  # noqa: BLE001 - 失敗はGUIへ表示するだけ
            return {"ok": False, "error": str(exc)}

    def set_api_key(self, payload: dict) -> dict:
        """ChatGPT の API キーをこの PC の .env に保存する。値は返さない・表示しない。"""
        key = str((payload or {}).get("key") or "").strip().strip('"').strip("'")
        if not key:
            return {"ok": False, "error": "API キーが空です"}
        if any(ch.isspace() for ch in key) or len(key) < 20:
            return {"ok": False, "error": "API キーの形式が違います（sk- で始まる1行の文字列を貼り付けてください）"}
        try:
            write_api_key(key)
        except OSError as exc:
            return {"ok": False, "error": f"保存できません: {exc}"}
        return {"ok": True, "env_path": str(ENV_FILE)}

    def get_overview(self) -> dict:
        try:
            return {"ok": True, **build_overview(workspace())}
        except Exception as exc:  # noqa: BLE001 - 集計の失敗はGUIへ表示するだけ
            return {"ok": False, "error": f"状態を読めません: {exc}"}

    # ── 実行 ────────────────────────────────────────────────────────────────

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start_run(self) -> dict:
        ws = workspace()
        problem = ws.problem()
        if problem:
            return {"ok": False, "error": problem}
        cmd = runner_command(ws)
        with self._lock:
            if self._is_running():
                return {"ok": False, "error": "すでに実行中です"}
            self._cancelled = False
            self._state = {
                "running": True, "progress": 0, "log": "$ " + subprocess.list2cmdline(cmd),
                "message": "3段階の実行を開始しました", "exit_code": None, "stages": [],
            }
        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()
        return {"ok": True}

    def _worker(self, cmd: list[str]) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        yolo_config_dir = ROOT / ".ultralytics"
        yolo_config_dir.mkdir(exist_ok=True)
        env["YOLO_CONFIG_DIR"] = str(yolo_config_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"   # 子プロセスの print を行ごとに受け取る
        lines: deque[str] = deque(maxlen=LOG_LINES)
        lines.append("$ " + subprocess.list2cmdline(cmd))
        tracker = RunProgress()
        try:
            process = subprocess.Popen(
                cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags, env=env,
            )
            with self._lock:
                self._process = process
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                progress, message, entry = tracker.feed(line)
                with self._lock:
                    state = self._state
                    state["log"] = "\n".join(lines)
                    if message:
                        state["message"] = message
                    if progress is not None:
                        state["progress"] = max(state["progress"], min(99, progress))
                    if entry:
                        stages = [s for s in state["stages"] if s["name"] != entry["name"]]
                        state["stages"] = [*stages, entry]
            code = process.wait()
        except Exception as exc:  # noqa: BLE001 - 失敗はGUIへ表示して終わる
            lines.append(f"[GUI ERROR] {exc}")
            code = 1
        with self._lock:
            state = self._state
            state["running"] = False
            state["exit_code"] = code
            state["log"] = "\n".join(lines)
            if code == 0:
                state["progress"] = 100
                state["message"] = "すべての段階が完了しました"
            elif self._cancelled:
                state["message"] = "実行を中止しました"
            else:
                state["message"] = "実行中にエラーが発生しました（ログ参照）"
            self._process = None

    def get_run_state(self) -> dict:
        with self._lock:
            state = self._state
            return {**state, "stages": [dict(entry) for entry in state["stages"]]}

    def cancel_run(self) -> dict:
        """実行中の length_pipeline.py を子プロセス（YOLO・GPT）ごと止める。"""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return {"ok": False, "error": "実行中の処理はありません"}
            self._cancelled = True
            pid = process.pid
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                process.terminate()
        except OSError as exc:
            return {"ok": False, "error": f"中止に失敗しました: {exc}"}
        return {"ok": True}

    # ── 確認・修正 ──────────────────────────────────────────────────────────

    def _image_url(self, path: Path | None) -> str:
        if path is None or not path.is_file():
            return ""
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", str(path))
        self._media_registry[token] = path
        return f"{self._base_url}/image/{token}?v={int(path.stat().st_mtime)}"

    def list_lengths(self) -> dict:
        """car_lengths.csv の全行に、写真のURL・切り出し結果・変換結果を添えて返す。"""
        ws = workspace()
        if not ws.chosen:
            return {"ok": True, "rows": [], "sessions": []}
        try:
            rows = read_rows(ws.lengths)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"car_lengths.csv を読めません: {exc}"}
        extract_log = {(r.get("session", ""), r.get("event_id", "")): r.get("status", "")
                       for r in read_csv(ws.images / "extract_log.csv")}
        conversion = {(r.get("session", ""), r.get("event_id", "")): r.get("status", "")
                      for r in read_csv(ws.summary) if r.get("row") and r.get("event_id")}
        out = []
        for row in rows:
            key = (row.get("session", ""), row.get("event_id", ""))
            out.append({
                **row,
                "crop_url": self._image_url(first_image(ws.images / key[0], key[1])),
                "frame_url": self._image_url(ws.frames / key[0] / f"{key[1]}.jpg"),
                "extract_status": extract_log.get(key, ""),
                "convert_status": conversion.get(key, ""),
            })
        return {"ok": True, "rows": out, "sessions": sorted({r["session"] for r in out if r["session"]})}

    def _find_row(self, rows: list[dict], session: str, event_id: str) -> dict:
        for row in rows:
            if (row.get("session"), row.get("event_id")) == (session, event_id):
                return row
        row = {"session": session, "event_id": event_id}
        rows.append(row)
        return row

    def set_length(self, payload: dict) -> dict:
        """車長を手入力で保存する（source=manual）。空欄で保存すると車長と出典を消す。"""
        payload = dict(payload or {})
        session = str(payload.get("session") or "").strip()
        event_id = str(payload.get("event_id") or "").strip()
        if not session or not event_id:
            return {"ok": False, "error": "イベントが選ばれていません"}
        value = str(payload.get("car_length_m") or "").strip()
        note = payload.get("note")
        ws = workspace()
        if not ws.chosen:
            return {"ok": False, "error": NO_SRC}
        with self._lock:
            if self._is_running():
                return {"ok": False, "error": "実行中は車長を編集できません。完了か中止を待ってください"}
            rows = read_rows(ws.lengths)
            row = self._find_row(rows, session, event_id)
            if value:
                try:
                    meters = float(value)
                except ValueError:
                    return {"ok": False, "error": f"車長は数値（m）で入力してください: {value}"}
                if not MIN_LENGTH_M <= meters <= MAX_LENGTH_M:
                    return {"ok": False, "error": f"車長は {MIN_LENGTH_M}〜{MAX_LENGTH_M} m の範囲で入力してください"}
                row["car_length_m"] = f"{meters:.3f}"
                row["source"] = "manual"
            else:
                row["car_length_m"] = ""
                row["source"] = ""
            if note is not None:
                row["note"] = str(note)
            row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            ws.work.mkdir(parents=True, exist_ok=True)
            write_rows(ws.lengths, rows)
        return {"ok": True, "row": row}

    def restore_gpt(self, payload: dict) -> dict:
        """手入力を取り消す。保存してある GPT の生応答があればその車長へ戻し、無ければ空欄にする。"""
        payload = dict(payload or {})
        session = str(payload.get("session") or "").strip()
        event_id = str(payload.get("event_id") or "").strip()
        if not session or not event_id:
            return {"ok": False, "error": "イベントが選ばれていません"}
        ws = workspace()
        if not ws.chosen:
            return {"ok": False, "error": NO_SRC}
        answer, model = None, DEFAULT_MODEL
        raw = ws.raw / f"{session.replace('/', '_')}_{event_id}.json"
        if raw.is_file():
            try:
                response = json.loads(raw.read_text(encoding="utf-8"))
                answer = parse_answer(response)
                model = response.get("model") or model
            except Exception:  # noqa: BLE001 - 読めない生応答は無いものとして扱う
                answer = None
        with self._lock:
            if self._is_running():
                return {"ok": False, "error": "実行中は車長を編集できません。完了か中止を待ってください"}
            rows = read_rows(ws.lengths)
            row = self._find_row(rows, session, event_id)
            length = (answer or {}).get("length_m")
            if answer and answer.get("vehicle_visible") and length:
                row.update({
                    "car_length_m": f"{float(length):.3f}",
                    "source": f"gpt:{model}:{answer.get('length_basis', '')}",
                    "make": answer.get("make", ""), "model": answer.get("model", ""),
                    "body_type": answer.get("body_type", ""),
                    "confidence": f"{float(answer.get('confidence') or 0):.2f}",
                    "note": answer.get("notes", ""),
                })
            else:
                row["car_length_m"] = ""
                row["source"] = ""
            row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            ws.work.mkdir(parents=True, exist_ok=True)
            write_rows(ws.lengths, rows)
        return {"ok": True, "row": row, "restored": bool(answer and length)}

    def open_path(self, value: str) -> dict:
        path = Path(str(value or ""))
        if not path.exists():
            return {"ok": False, "error": f"パスが存在しません: {path}"}
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- HTTP

class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, api: "LengthApi", ui_dir: Path) -> None:
        super().__init__(address, UiHandler)
        self.api = api
        self.ui_dir = Path(ui_dir)


class UiHandler(BaseHTTPRequestHandler):
    """画面ファイル（web_ui_length/）、/image/<token>（切り出した写真）、POST /api/<メソッド> を配る。

    /api/ は pywebview の js_api と同じメソッドを HTTP で呼ぶためのもの。--serve-only で
    普通のブラウザから画面を使う（デバッグ・pywebview が動かない環境）ときに app.js が使う。
    """

    server: UiServer

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path.startswith("/image/"):
            self._serve_image(path.removeprefix("/image/"))
            return
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        ui_dir = self.server.ui_dir.resolve()
        target = (ui_dir / relative).resolve()
        if ui_dir not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        name = path.removeprefix("/api/")
        method = getattr(self.server.api, name, None) if path.startswith("/api/") else None
        if name.startswith("_") or not callable(method):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            args = body.get("args") or [] if isinstance(body, dict) else []
            result = method(*args)
        except Exception as exc:  # noqa: BLE001 - 失敗はJSONで返して画面に出す
            result = {"ok": False, "error": f"{name}: {exc}"}
        self._send(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _serve_image(self, token: str) -> None:
        target = self.server.api._media_registry.get(token)
        if target is None or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "image/jpeg")


def start_server(api: LengthApi, port: int = 0) -> tuple[UiServer, str]:
    server = UiServer(("127.0.0.1", port), api, UI_DIR)
    url = f"http://127.0.0.1:{server.server_port}"
    api.attach(None, url)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, url


# ---------------------------------------------------------------- 検証

def run_check() -> None:
    """画面なしで部品を検証する（フォルダ指定・コマンド・進捗の解釈・手入力の保存・APIキーの保存）。"""
    import tempfile

    for name in ("index.html", "style.css", "app.js"):
        assert (UI_DIR / name).is_file(), f"画面ファイルがありません: {UI_DIR / name}"
    assert RUNNER.is_file(), RUNNER

    tracker = RunProgress()
    progress, _, entry = tracker.feed("[1/3 写真の切り出し] extract_car_images.py")
    assert progress == 0 and entry["status"] == "running" and entry["name"] == "extract", (progress, entry)
    tracker.feed("== w5o/ID1（front）: 4 イベント")
    progress, message, _ = tracker.feed("  001: OK 001.jpg")
    assert progress == 8 and "001" in message, (progress, message)
    progress, _, entry = tracker.feed("[1/3 写真の切り出し] 完了（4秒）")
    assert progress == 33 and entry["status"] == "ok", (progress, entry)
    progress, _, entry = tracker.feed("[2/3 車長の推定] car_length_gpt.py")
    assert progress == 33 and entry["name"] == "gpt", (progress, entry)
    tracker.feed("推定する対象: 4 件（画像のあるイベント 68 件）")
    progress, message, _ = tracker.feed("[2/4] w6b/obihiro 011: トヨタ シエンタ 全長 4.260 m (確度 0.90)")
    assert progress == 50 and "2/4" in message, (progress, message)
    _, _, entry = tracker.feed("[3/3 相対速度の変換] 失敗（終了コード 1、3秒）。ここで止めます。")
    assert entry["status"] == "failed" and entry["name"] == "convert", entry

    global SETTINGS_PATH, ENV_FILE
    keep_settings, keep_env = SETTINGS_PATH, ENV_FILE
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        SETTINGS_PATH = root / "settings.json"
        ENV_FILE = root / ".env"
        try:
            api = LengthApi()
            # 未選択のうちは何も読まない: 一覧は空、実行は断る
            assert read_settings() == {"src": "", "work": "", "work_custom": False}, read_settings()
            empty = build_overview(workspace())
            assert empty["sessions"] == [] and empty["src"] == "" and empty["problem"] == NO_SRC, empty
            assert api.list_lengths() == {"ok": True, "rows": [], "sessions": []}
            assert api.start_run() == {"ok": False, "error": NO_SRC}
            assert api.set_length({"session": "a", "event_id": "1", "car_length_m": "4"})["error"] == NO_SRC
            # フォルダ指定: 保存先の既定は隣の <名前>_車長変換。セッションが無ければ実行を断る
            src = root / "data" / "output"
            (src / "w1" / "ID1" / "lidar_edit").mkdir(parents=True)
            settings = api.save_settings({"src": str(src), "work": ""})
            assert settings["work"] == str(src.parent / f"output{WORK_SUFFIX}") and not settings["work_custom"], settings
            ws = workspace()
            assert ws.problem() == "", ws.problem()
            assert runner_command(ws)[2:] == ["--src", str(src), "--work", str(ws.work)], runner_command(ws)
            assert Workspace(src, src / "x").problem(), "保存先が中なのに通った"
            assert Workspace(root / "none", root / "w").problem(), "無いフォルダなのに通った"
            assert Workspace(None, None).problem() == NO_SRC
            custom = api.save_settings({"src": str(src), "work": str(root / "elsewhere")})
            assert custom["work_custom"] and custom["work"] == str(root / "elsewhere"), custom
            api.save_settings({"src": str(src), "work": ""})
            # 手入力の保存と取り消し
            result = api.set_length({"session": "w1/ID1", "event_id": "001", "car_length_m": "4.1", "note": "確認済み"})
            assert result["ok"] and result["row"]["source"] == "manual" and result["row"]["car_length_m"] == "4.100", result
            assert not api.set_length({"session": "w1/ID1", "event_id": "001", "car_length_m": "abc"})["ok"]
            assert not api.set_length({"session": "w1/ID1", "event_id": "001", "car_length_m": "99"})["ok"]
            rows = read_rows(workspace().lengths)
            assert len(rows) == 1 and rows[0]["note"] == "確認済み", rows
            result = api.restore_gpt({"session": "w1/ID1", "event_id": "001"})  # 生応答なし → 空欄
            assert result["ok"] and not result["restored"] and result["row"]["car_length_m"] == "", result
            listed = api.list_lengths()
            assert listed["ok"] and len(listed["rows"]) == 1 and listed["rows"][0]["crop_url"] == "", listed
            # APIキーの保存（値は .env にだけ書く）
            assert not api.set_api_key({"key": "short"})["ok"]
            ENV_FILE.write_text("OPENAI_MODEL=gpt-5.5\nsk-oldkey-xxxxxxxxxxxxxxxxxxxx\n", encoding="utf-8")
            assert api.set_api_key({"key": "sk-check-" + "x" * 24})["ok"]
            env = load_env(ENV_FILE)
            assert env.get("OPENAI_MODEL") == "gpt-5.5" and find_api_key(env).startswith("sk-check-") \
                and "_BARE_KEY" not in env, env.keys()
            overview = build_overview(workspace())
            assert overview["api_key"] and overview["sessions"][0]["session"] == "w1/ID1", overview
        finally:
            SETTINGS_PATH, ENV_FILE = keep_settings, keep_env

    settings = read_settings()
    print(f"[OK] 車長変換GUIの検証に合格しました（この PC の解析結果フォルダ: {settings['src'] or '未選択'}）")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="車長込み相対速度への変換の専用GUI")
    parser.add_argument("--check", action="store_true", help="画面を出さずに部品を検証する")
    parser.add_argument("--serve-only", action="store_true", help="ブラウザで開くためのURLを出して配信だけ続ける")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.check:
        run_check()
        return
    api = LengthApi()
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
    window = webview.create_window(
        "車長変換 - 自転車追い越し解析", url=url, js_api=api,
        width=1240, height=880, min_size=(960, 700),
    )
    api.attach(window, url)
    webview.start(debug=False)
    server.shutdown()


if __name__ == "__main__":
    main()
