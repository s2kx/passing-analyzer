#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析結果を1枚のHTMLにまとめる閲覧用レポート生成ツール。

``run_pipeline.py`` の出力フォルダ（``reviewed_overtaking_events.csv`` /
``clips/`` / ``lidar/`` / ``lidar_edit/``）を読み、ダブルクリックで開ける
``report.html`` を作る。イベントごとに

  * 動画クリップ（``clips/event_<id>_<side>.mp4`` への**相対パス参照**。コピーはしない）
  * 切り抜いた点群のビューア（上面図 X-Y と側面図 Y-Z の2面。フレーム送り・再生）
  * 主要数値（離隔距離LC・通過時間・相対速度・TTC など）とレビュー結果

を見られる。

**file:// で開く前提**なので ``fetch`` / ``XMLHttpRequest`` は使えない。点群は
イベントごとに ``report_data/event_<id>.js``（``window.__EVT["001"]=...`` の
代入文）へ書き出し、カードを開いたときに ``<script>`` 要素を注入して遅延読み込み
する。外部CDNへは一切依存しない（CSS/JS はすべてインライン）。

使い方::

    python make_report.py --out output/ID1
    python make_report.py --out output/ID1 --max-points-per-frame 1500

``run_pipeline.py`` は段階7の直後（段階7b）に ``generate_report`` を呼ぶ。
``--no-report`` で無効化できる。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 埋め込みPython（portable/python）は実行したスクリプトの場所を import の起点にしないので、
# 隣のモジュール（src/）を読めるよう自分のフォルダを足す。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

from car_length import lookup_car_length
from edit_csv import analyze_edit_points, read_edit_points

# ── 点群の軽量化のしきい値 ───────────────────────────────────────────────────
# file:// の script 読み込みは1ファイルまるごと同期で走るので、1イベントの js が
# 数MBになるとカードを開いた瞬間に固まる。実データ(260701/ID1)の点群CSVは
# 1イベント3.5万〜13万行あるため、次の2段構えで間引いて 1〜2MB に収める。
#   1. 1フレームあたりの上限（描画してもドットが潰れるだけなので効果が無い）
#   2. 1イベントあたりの総点数の予算（フレーム数が多い長い追い越し向けの保険）
# 間引きは等間隔（CSVの並びはレーザーID×方位角なので、等間隔に取ると空間的にも
# 満遍なく残る）。座標は小数2桁に丸める（LiDARの距離分解能は2mm、表示は1cm単位で十分）。
MAX_POINTS_PER_FRAME = 2000
MAX_POINTS_TOTAL = 60000
# 総点数の予算をフレーム数で割ってもこれ以上は減らさない（車が数点しか映らなくなる）
MIN_POINTS_PER_FRAME = 150
COORD_DECIMALS = 2

REPORT_NAME = "report.html"
DATA_DIR_NAME = "report_data"

# レビュー結果の表示色（バッジ）
STATUS_ORDER = {"採用": 0, "未確認": 1, "除外": 2}


class ReportError(Exception):
    """レポートを生成できないときに送出する。"""


# ── 入出力の小道具 ───────────────────────────────────────────────────────────

def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _to_float(value) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _num(value: float, decimals: int = COORD_DECIMALS) -> str:
    """JSON へ埋める数値を短い文字列にする（``3.50`` → ``3.5``、``4.00`` → ``4``）。"""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "-0" if text in {"-0", "-"} else (text or "0")


def _safe_id(event_id: str) -> str:
    """イベントIDをファイル名に使える形にする。"""
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(event_id or "")) or "event"


def _sort_key(event_id: str) -> tuple[int, int, str]:
    """``001`` … → ``L001`` … の順に並べる（動画由来が先、LiDAR発が後ろ）。"""
    text = str(event_id or "")
    digits = re.sub(r"\D", "", text)
    return (1 if text.upper().startswith("L") else 0,
            int(digits) if digits else 10 ** 9, text)


# ── 出力フォルダの読み取り ───────────────────────────────────────────────────

def load_events(out_dir: Path) -> list[dict[str, str]]:
    """レビュー結果CSV（無ければ検出CSV）を読む。"""
    reviewed = out_dir / "reviewed_overtaking_events.csv"
    path = reviewed if reviewed.is_file() else out_dir / "overtaking_events.csv"
    if not path.is_file():
        raise ReportError(
            "reviewed_overtaking_events.csv または overtaking_events.csv が"
            f"見つかりません: {out_dir}"
        )
    rows = [row for row in _read_csv_dicts(path) if str(row.get("event_id", "")).strip()]
    rows.sort(key=lambda row: _sort_key(row.get("event_id", "")))
    return rows


def load_session_info(out_dir: Path) -> dict:
    """``pipeline_report.json`` からヘッダに出す情報を拾う（無くても動く）。"""
    info = {"session": out_dir.name, "started_at": "", "finished_at": "",
            "view": "", "subject_id": "", "collection_date": "", "offset": None}
    path = out_dir / "pipeline_report.json"
    if not path.is_file():
        return info
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    session = str(data.get("session") or "")
    info["session"] = Path(session).name if session else out_dir.name
    for key in ("started_at", "finished_at", "view", "subject_id", "collection_date"):
        info[key] = str(data.get(key) or "")
    offset = (data.get("info") or {}).get("時刻オフセット(秒)")
    info["offset"] = _to_float(offset)
    return info


def find_clip(out_dir: Path, event: dict) -> str:
    """クリップの**相対パス**を返す（無ければ空文字）。動画はコピーしない。"""
    event_id = str(event.get("event_id", ""))
    side = str(event.get("side", "")).strip()
    clips = out_dir / "clips"
    if not clips.is_dir():
        return ""
    names = []
    if side:
        names.append(f"event_{event_id}_{side}.mp4")
    names.extend([f"event_{event_id}_left.mp4", f"event_{event_id}_right.mp4"])
    for name in names:
        if (clips / name).is_file():
            return f"clips/{name}"
    # side 列が想定外でも拾えるようにワイルドカードで探す
    for path in sorted(clips.glob(f"event_{event_id}_*.mp4")):
        return f"clips/{path.name}"
    return ""


def load_metrics(out_dir: Path, event_id: str) -> tuple[dict, list[str], str]:
    """edit CSV から主要数値を求める。戻り値は (指標, 警告, エラー文)。"""
    path = out_dir / "lidar_edit" / f"overtaking_{event_id}_edit.csv"
    if not path.is_file():
        return {}, [], ""
    try:
        points = read_edit_points(path)
        car_length = lookup_car_length(out_dir, event_id)
        metrics = analyze_edit_points(
            points, car_length=car_length.meters if car_length else None)
    except Exception as exc:  # noqa: BLE001 - 1件の失敗でレポート全体を止めない
        return {}, [], str(exc)
    warnings = [str(w) for w in (metrics.get("warnings") or [])]
    values = {key: _to_float(metrics.get(key))
              for key in ("lc", "passing_time", "distance_p2", "distance_p3",
                          "relative_speed", "ttc")}
    return {k: v for k, v in values.items() if v is not None}, warnings, ""


def load_trajectory(out_dir: Path, event_id: str) -> list[dict]:
    """edit CSV の軌跡（フレームごとの最近接点）とフェーズ名を返す。"""
    path = out_dir / "lidar_edit" / f"overtaking_{event_id}_edit.csv"
    if not path.is_file():
        return []
    try:
        points = read_edit_points(path)
        analyze_edit_points(points)      # 副作用で point.phase が付く
    except Exception:  # noqa: BLE001 - フェーズが付かなくても軌跡は出す
        try:
            points = read_edit_points(path)
        except Exception:  # noqa: BLE001
            return []
    return [{"t": point.unixtime, "x": point.x, "y": point.y,
             "z": _to_float(point.z_text) or 0.0, "ph": point.phase}
            for point in points]


# ── 点群の読み取りと軽量化 ───────────────────────────────────────────────────

def _frame_budget(frame_count: int, per_frame: int, total: int) -> int:
    """1フレームあたりに残す点数の上限を決める。"""
    if frame_count <= 0:
        return per_frame
    # 総点数の予算をフレーム数で割った値を使うが、MIN_POINTS_PER_FRAME を下回らない。
    # ただし明示された1フレーム上限（per_frame）を、下限側の都合で超えてはいけない。
    budget = total // frame_count
    return min(per_frame, max(MIN_POINTS_PER_FRAME, budget))


def _thin(rows: list, limit: int) -> list:
    """等間隔に間引いて ``limit`` 点以下にする。"""
    if limit <= 0 or len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[min(len(rows) - 1, int(i * step))] for i in range(limit)]


def load_point_frames(csv_path: Path, per_frame: int = MAX_POINTS_PER_FRAME,
                      total: int = MAX_POINTS_TOTAL) -> dict:
    """点群CSVを Frame 列でグループ化し、間引いた結果を返す。

    戻り値は ``{"frames": [{"f", "t", "p", "i"}], "bounds", "points", "raw_points"}``。
    ``p`` は ``[x, y, z, x, y, z, ...]`` の平坦な配列、``i`` は Intensity。
    """
    groups: dict[str, list[tuple[float, float, float, int]]] = {}
    order: list[str] = []
    times: dict[str, float] = {}
    raw_points = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            x = _to_float(row.get("Points:0"))
            y = _to_float(row.get("Points:1"))
            z = _to_float(row.get("Points:2"))
            if x is None or y is None or z is None:
                continue
            frame = str(row.get("Frame", "")).strip()
            if frame not in groups:
                groups[frame] = []
                order.append(frame)
                times[frame] = _to_float(row.get("Time")) or 0.0
            intensity = _to_float(row.get("Intensity")) or 0.0
            groups[frame].append((x, y, z, int(intensity)))
            raw_points += 1

    limit = _frame_budget(len(order), per_frame, total)
    frames = []
    kept = 0
    min_x = min_y = min_z = math.inf
    max_x = max_y = max_z = -math.inf
    for frame in order:
        points = _thin(groups[frame], limit)
        kept += len(points)
        for x, y, z, _ in points:
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            min_z, max_z = min(min_z, z), max(max_z, z)
        frames.append({"f": frame, "t": times[frame], "points": points})
    if not frames:
        return {"frames": [], "bounds": None, "points": 0, "raw_points": raw_points,
                "limit": limit}
    return {
        "frames": frames,
        "bounds": {"x": [min_x, max_x], "y": [min_y, max_y], "z": [min_z, max_z]},
        "points": kept,
        "raw_points": raw_points,
        "limit": limit,
    }


def event_js_text(event_id: str, cloud: dict, trajectory: list[dict],
                  metrics: dict) -> str:
    """``window.__EVT["001"]=...`` の代入文を組み立てる（file:// 用）。"""
    chunks = []
    for frame in cloud["frames"]:
        coords = []
        intensities = []
        for x, y, z, intensity in frame["points"]:
            coords.append(_num(x))
            coords.append(_num(y))
            coords.append(_num(z))
            intensities.append(str(int(intensity)))
        chunks.append(
            '{"f":' + json.dumps(frame["f"]) + ',"t":' + _num(frame["t"], 3)
            + ',"p":[' + ",".join(coords) + '],"i":[' + ",".join(intensities) + "]}"
        )
    bounds = cloud.get("bounds") or {"x": [-1, 1], "y": [-1, 1], "z": [-1, 1]}
    head = {
        "id": event_id,
        "bounds": {key: [round(v, COORD_DECIMALS) for v in value]
                   for key, value in bounds.items()},
        "points": cloud.get("points", 0),
        "rawPoints": cloud.get("raw_points", 0),
        "perFrame": cloud.get("limit", 0),
        "lc": metrics.get("lc"),
        "traj": [{"t": round(p["t"], 3), "x": round(p["x"], COORD_DECIMALS),
                  "y": round(p["y"], COORD_DECIMALS), "z": round(p["z"], COORD_DECIMALS),
                  "ph": p["ph"]} for p in trajectory],
    }
    # .js は script 要素で読むため、文字コードの取り違えが起きないよう ASCII だけにする
    head_text = json.dumps(head, ensure_ascii=True)
    # frames だけは桁を詰めた手組みの配列を差し込む
    body = head_text[:-1] + ',"frames":[' + ",".join(chunks) + "]}"
    return ("window.__EVT=window.__EVT||{};window.__EVT["
            + json.dumps(str(event_id)) + "]=" + body + ";\n")


# ── HTML ─────────────────────────────────────────────────────────────────────

def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _json_for_script(data) -> str:
    """``</script>`` でHTMLが切れないようにしてから埋め込む。"""
    return (json.dumps(data, ensure_ascii=False)
            .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


STYLE = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --line:#d9dde3; --text:#1c2128; --muted:#5b6775;
  --accent:#1f6feb; --ok:#137a3d; --warn:#a35a00; --view:#0d1117;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Yu Gothic UI","Meiryo","Hiragino Kaku Gothic ProN",sans-serif;
  font-size:14px;line-height:1.6}
header{background:var(--card);border-bottom:1px solid var(--line);padding:18px 24px}
h1{margin:0 0 6px;font-size:20px}
.meta{color:var(--muted);font-size:13px}
.meta span{margin-right:18px;white-space:nowrap}
.counts{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.count{border:1px solid var(--line);border-radius:14px;padding:2px 12px;background:#fff;font-size:13px}
main{max-width:1100px;margin:0 auto;padding:18px 16px 60px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.toolbar button{border:1px solid var(--line);background:#fff;border-radius:6px;
  padding:5px 14px;cursor:pointer;font:inherit;color:inherit}
.toolbar button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
  margin-bottom:10px;overflow:hidden}
.card.hide{display:none}
.head{display:flex;gap:12px;align-items:center;padding:10px 14px;cursor:pointer;
  user-select:none;flex-wrap:wrap}
.head:hover{background:#eef2f7}
.arrow{color:var(--muted);width:14px;flex:none}
.eid{font-weight:700;font-size:15px;min-width:56px}
.badge{border-radius:11px;padding:1px 10px;font-size:12px;border:1px solid;white-space:nowrap}
.b-ok{color:var(--ok);border-color:#9fd6b4;background:#eaf7ef}
.b-un{color:var(--warn);border-color:#e5c48a;background:#fdf5e6}
.b-no{color:var(--muted);border-color:var(--line);background:#f1f2f4}
.klass{color:var(--muted)}
.quick{margin-left:auto;color:var(--muted);font-size:13px;white-space:nowrap}
.body{display:none;border-top:1px solid var(--line);padding:14px}
.card.open .body{display:block}
.grid{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.col-video{flex:1 1 380px;min-width:300px}
.col-info{flex:1 1 300px;min-width:260px}
video{width:100%;background:#000;border-radius:6px;display:block}
.novideo{background:#f1f2f4;border:1px dashed var(--line);border-radius:6px;
  padding:26px 12px;text-align:center;color:var(--muted)}
table.kv{border-collapse:collapse;width:100%;font-size:13px}
table.kv th,table.kv td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}
table.kv th{color:var(--muted);font-weight:400;width:40%;white-space:nowrap}
table.kv td{font-variant-numeric:tabular-nums}
.reason{margin-top:8px;padding:8px 10px;border-radius:6px;background:#fdf5e6;
  border:1px solid #e5c48a;font-size:13px}
.warns{margin-top:8px;font-size:12px;color:var(--warn)}
.warns div{margin-top:2px}
h3{margin:18px 0 8px;font-size:14px}
.views{display:flex;gap:12px;flex-wrap:wrap}
.view{flex:1 1 420px;min-width:300px}
.view .cap{font-size:12px;color:var(--muted);margin-bottom:4px}
canvas{width:100%;height:auto;background:var(--view);border-radius:6px;display:block;
  border:1px solid #22272e}
.player{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
.player button{border:1px solid var(--line);background:#fff;border-radius:6px;
  padding:4px 14px;cursor:pointer;font:inherit;min-width:74px}
.player input[type=range]{flex:1 1 220px;min-width:160px}
.player .pos{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;
  min-width:210px;white-space:nowrap}
.player label{font-size:12px;color:var(--muted)}
.loading{color:var(--muted);padding:10px 0}
.legend{margin-top:6px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:50%;
  margin:0 4px 0 12px;vertical-align:-1px}
footer{color:var(--muted);font-size:12px;padding:0 16px 30px;max-width:1100px;margin:0 auto}
"""


SCRIPT = r"""
(function(){
"use strict";
var DATA = window.__REPORT;
var STATE = {};

// ── 点の色 ────────────────────────────────────────────────────────────────
// Intensity は 0〜255、Z は世界座標(m)。どちらも青→緑→黄→赤の連続色に写す。
function ramp(t){
  t = t < 0 ? 0 : (t > 1 ? 1 : t);
  var r, g, b;
  if (t < 0.34){ var u = t / 0.34; r = 30; g = 80 + 150 * u; b = 230 - 60 * u; }
  else if (t < 0.67){ var v = (t - 0.34) / 0.33; r = 30 + 210 * v; g = 230; b = 170 - 150 * v; }
  else { var w = (t - 0.67) / 0.33; r = 240; g = 230 - 170 * w; b = 20; }
  return "rgb(" + (r|0) + "," + (g|0) + "," + (b|0) + ")";
}

function niceStep(span){
  var raw = span / 6, pow = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
  var n = raw / pow;
  var m = n >= 5 ? 5 : (n >= 2 ? 2 : 1);
  return m * pow;
}

// 世界座標 → 画面座標。両軸とも同じ m/px（等スケール）で、指定範囲が収まるように取る。
function projection(canvas, hmin, hmax, vmin, vmax, pad){
  var w = canvas.width, h = canvas.height;
  var hs = Math.max(hmax - hmin, 0.5), vs = Math.max(vmax - vmin, 0.5);
  var scale = Math.min((w - 2 * pad) / hs, (h - 2 * pad) / vs);
  var hc = (hmin + hmax) / 2, vc = (vmin + vmax) / 2;
  return {
    scale: scale,
    sx: function(hv){ return w / 2 + (hv - hc) * scale; },
    sy: function(vv){ return h / 2 - (vv - vc) * scale; },
    hmin: hc - (w / 2 - 0) / scale, hmax: hc + (w / 2) / scale,
    vmin: vc - (h / 2) / scale, vmax: vc + (h / 2) / scale,
    w: w, h: h
  };
}

function drawGrid(ctx, pr, hlabel, vlabel){
  var step = niceStep(Math.max(pr.hmax - pr.hmin, pr.vmax - pr.vmin));
  ctx.lineWidth = 1;
  ctx.font = "11px monospace";
  ctx.textBaseline = "top";
  var v;
  for (v = Math.ceil(pr.hmin / step) * step; v <= pr.hmax; v += step){
    var x = pr.sx(v);
    ctx.strokeStyle = Math.abs(v) < 1e-9 ? "#4a5568" : "#232a33";
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, pr.h); ctx.stroke();
    ctx.fillStyle = "#6b7684";
    ctx.fillText(v.toFixed(step < 1 ? 1 : 0), x + 3, pr.h - 15);
  }
  for (v = Math.ceil(pr.vmin / step) * step; v <= pr.vmax; v += step){
    var y = pr.sy(v);
    ctx.strokeStyle = Math.abs(v) < 1e-9 ? "#4a5568" : "#232a33";
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(pr.w, y); ctx.stroke();
    ctx.fillStyle = "#6b7684";
    ctx.fillText(v.toFixed(step < 1 ? 1 : 0), 4, y + 2);
  }
  ctx.fillStyle = "#8b97a6";
  ctx.font = "12px sans-serif";
  ctx.fillText(hlabel, pr.w - 150, 6);
  ctx.fillText(vlabel, 6, 6);
  // スケールバー（1目盛 = step m）
  var bx = pr.w - 20 - step * pr.scale, by = pr.h - 26;
  ctx.strokeStyle = "#8b97a6"; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + step * pr.scale, by); ctx.stroke();
  ctx.fillStyle = "#8b97a6"; ctx.font = "11px monospace";
  ctx.fillText(step + " m", bx, by - 15);
}

// 通過区間 |Y| < 3m の帯。横軸はどちらのビューも Y なので同じ描き方でよい。
function drawPassBand(ctx, pr){
  var x1 = pr.sx(-3), x2 = pr.sx(3);
  ctx.fillStyle = "rgba(80,140,255,0.10)";
  ctx.fillRect(Math.min(x1, x2), 0, Math.abs(x2 - x1), pr.h);
  ctx.strokeStyle = "rgba(120,170,255,0.55)";
  ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x1, 0); ctx.lineTo(x1, pr.h); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x2, 0); ctx.lineTo(x2, pr.h); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(150,190,255,0.9)"; ctx.font = "11px sans-serif";
  ctx.fillText("通過区間 |Y|<3m", Math.min(x1, x2) + 4, 22);
}

function drawBike(ctx, pr, hv, vv){
  var x = pr.sx(hv), y = pr.sy(vv);
  ctx.strokeStyle = "#ff5f8d"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
  ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11); ctx.stroke();
  ctx.fillStyle = "#ff8fb0"; ctx.font = "11px sans-serif";
  ctx.fillText("自転車(原点)", x + 12, y - 16);
}

function render(st, key){
  var canvas = st.canvas[key], ctx = canvas.getContext("2d");
  var d = st.data, fr = d.frames[st.frame];
  var top = (key === "top");
  var b = d.bounds;
  // 横軸は両ビューとも Y（前方+）。縦軸は上面図が X（右側+）、側面図が Z（上+）。
  var hmin = Math.min(b.y[0], -4) - 1, hmax = Math.max(b.y[1], 4) + 1;
  var vmin, vmax;
  if (top){ vmin = Math.min(b.x[0], -1) - 1; vmax = Math.max(b.x[1], 1) + 1; }
  else { vmin = Math.min(b.z[0], -2) - 0.5; vmax = Math.max(b.z[1], 1) + 0.5; }
  var pr = projection(canvas, hmin, hmax, vmin, vmax, 8);
  ctx.fillStyle = "#0d1117"; ctx.fillRect(0, 0, pr.w, pr.h);
  drawGrid(ctx, pr, top ? "→ Y 前方 [m]" : "→ Y 前方 [m]",
           top ? "↑ X 右側 [m]" : "↑ Z 高さ [m]");
  drawPassBand(ctx, pr);

  // 離隔距離 LC の補助線（上面図のみ。X = LC の高さ）
  if (top && typeof d.lc === "number" && isFinite(d.lc)){
    var ly = pr.sy(d.lc);
    ctx.strokeStyle = "#ffd45e"; ctx.setLineDash([7, 5]); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(pr.w, ly); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ffd45e"; ctx.font = "11px sans-serif";
    ctx.fillText("LC " + d.lc.toFixed(2) + " m", 8, ly - 14);
  }

  // 軌跡（edit CSV の最近接点）
  if (d.traj && d.traj.length){
    ctx.strokeStyle = "rgba(255,255,255,0.30)"; ctx.lineWidth = 1;
    ctx.beginPath();
    for (var t = 0; t < d.traj.length; t++){
      var p = d.traj[t], px = pr.sx(p.y), py = pr.sy(top ? p.x : p.z);
      if (t === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
    for (var q = 0; q < d.traj.length; q++){
      if (!d.traj[q].ph) continue;
      var pp = d.traj[q], qx = pr.sx(pp.y), qy = pr.sy(top ? pp.x : pp.z);
      ctx.fillStyle = "#ffffff";
      ctx.beginPath(); ctx.arc(qx, qy, 3, 0, Math.PI * 2); ctx.fill();
      ctx.font = "10px sans-serif";
      ctx.fillText(pp.ph.replace(/フェーズ/g, "P"), qx + 5, qy - 12);
    }
  }

  // 点群
  if (fr){
    var p3 = fr.p, inten = fr.i, n = inten.length;
    var byZ = (st.color === "z");
    var lo, hi;
    if (byZ){ lo = vmin; hi = vmax; }
    else {
      // Intensity の実効範囲はフレームごとに違う（実データで 1〜196）。
      // 固定範囲だと大半が同じ色になるのでフレーム内の最大値で正規化する。
      lo = 0; hi = 1;
      for (var m = 0; m < n; m++){ if (inten[m] > hi) hi = inten[m]; }
    }
    var size = pr.scale > 26 ? 2.4 : (pr.scale > 13 ? 1.8 : 1.3);
    for (var i = 0; i < n; i++){
      var wx = p3[i * 3], wy = p3[i * 3 + 1], wz = p3[i * 3 + 2];
      var sxp = pr.sx(wy), syp = pr.sy(top ? wx : wz);
      if (sxp < -4 || sxp > pr.w + 4 || syp < -4 || syp > pr.h + 4) continue;
      var val = byZ ? wz : inten[i];
      ctx.fillStyle = ramp((val - lo) / (hi - lo || 1));
      ctx.fillRect(sxp - size / 2, syp - size / 2, size, size);
    }
  }
  drawBike(ctx, pr, 0, 0);
}

function renderBoth(st){ render(st, "top"); render(st, "side"); }

function setFrame(st, index){
  var n = st.data.frames.length;
  if (!n) return;
  st.frame = ((index % n) + n) % n;
  st.range.value = String(st.frame);
  var fr = st.data.frames[st.frame];
  st.pos.textContent = "フレーム " + fr.f + "  (" + (st.frame + 1) + "/" + n
    + ")　t=" + fr.t.toFixed(2) + "s　" + fr.i.length + "点";
  renderBoth(st);
}

function stop(st){
  st.playing = false;
  if (st.timer){ clearInterval(st.timer); st.timer = null; }
  st.play.textContent = "▶ 再生";
}

function toggle(st){
  if (st.playing){ stop(st); return; }
  st.playing = true;
  st.play.textContent = "■ 停止";
  // LiDAR は 10Hz。等速で送ると実時間相当になる。
  st.timer = setInterval(function(){ setFrame(st, st.frame + 1); }, 100);
}

function buildViewer(host, data){
  host.innerHTML = "";
  if (!data.frames || !data.frames.length){
    host.innerHTML = '<div class="loading">このイベントには点群データがありません。</div>';
    return;
  }
  var st = {data: data, frame: 0, playing: false, timer: null, color: "i", canvas: {}};
  var views = document.createElement("div");
  views.className = "views";
  // 表示幅に合わせて解像度を決める（CSS で 100% に伸ばすので、低いとぼやける）
  var wide = Math.max(600, Math.min(1400, (host.clientWidth || 900)));
  [["top", "上面図（X-Y）　横=Y 前方 / 縦=X 右側"],
   ["side", "側面図（Y-Z）　横=Y 前方 / 縦=Z 高さ"]].forEach(function(pair){
    var box = document.createElement("div");
    box.className = "view";
    var cap = document.createElement("div");
    cap.className = "cap"; cap.textContent = pair[1];
    var cv = document.createElement("canvas");
    cv.width = wide; cv.height = Math.round(wide * 0.45);
    box.appendChild(cap); box.appendChild(cv); views.appendChild(box);
    st.canvas[pair[0]] = cv;
  });
  host.appendChild(views);

  var bar = document.createElement("div");
  bar.className = "player";
  var play = document.createElement("button");
  play.type = "button"; play.textContent = "▶ 再生";
  var range = document.createElement("input");
  range.type = "range"; range.min = "0"; range.value = "0";
  range.max = String(data.frames.length - 1); range.step = "1";
  var pos = document.createElement("span");
  pos.className = "pos";
  var label = document.createElement("label");
  var sel = document.createElement("select");
  sel.innerHTML = '<option value="i">Intensity</option><option value="z">高さZ</option>';
  label.appendChild(document.createTextNode("色: "));
  label.appendChild(sel);
  bar.appendChild(play); bar.appendChild(range); bar.appendChild(pos); bar.appendChild(label);
  host.appendChild(bar);

  var legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML = "点 " + data.points.toLocaleString() + " / 元 "
    + data.rawPoints.toLocaleString() + "（1フレーム上限 " + data.perFrame + "点に間引き）"
    + '<i style="background:#ff5f8d"></i>自転車=原点'
    + '<i style="background:#ffd45e"></i>離隔距離LC'
    + '<i style="background:rgba(120,170,255,.8)"></i>通過区間|Y|&lt;3m'
    + '<i style="background:#fff"></i>フェーズ点';
  host.appendChild(legend);

  st.play = play; st.range = range; st.pos = pos;
  play.addEventListener("click", function(){ toggle(st); });
  range.addEventListener("input", function(){ stop(st); setFrame(st, parseInt(range.value, 10)); });
  sel.addEventListener("change", function(){ st.color = sel.value; renderBoth(st); });
  setFrame(st, 0);
}

// ── 遅延読み込み（file:// では fetch が使えないので script 要素を注入する）──
function loadEvent(card){
  var id = card.getAttribute("data-id");
  var file = card.getAttribute("data-js");
  var host = card.querySelector(".cloud");
  if (!host || card.getAttribute("data-loaded")) return;
  card.setAttribute("data-loaded", "1");
  if (!file){
    host.innerHTML = '<div class="loading">このイベントには点群CSVがありません。</div>';
    return;
  }
  host.innerHTML = '<div class="loading">点群を読み込んでいます…</div>';
  var tag = document.createElement("script");
  tag.charset = "utf-8";
  tag.src = file;
  tag.onload = function(){
    var data = (window.__EVT || {})[id];
    if (data) buildViewer(host, data);
    else host.innerHTML = '<div class="loading">点群データを読み込めませんでした。</div>';
  };
  tag.onerror = function(){
    host.innerHTML = '<div class="loading">' + file
      + ' を読み込めませんでした（report_data フォルダを report.html と同じ場所に置いてください）。</div>';
  };
  document.head.appendChild(tag);
}

function init(){
  var cards = document.querySelectorAll(".card");
  Array.prototype.forEach.call(cards, function(card){
    card.querySelector(".head").addEventListener("click", function(){
      var open = card.classList.toggle("open");
      card.querySelector(".arrow").textContent = open ? "▼" : "▶";
      if (open) loadEvent(card);
    });
  });
  var buttons = document.querySelectorAll(".toolbar button[data-filter]");
  Array.prototype.forEach.call(buttons, function(btn){
    btn.addEventListener("click", function(){
      Array.prototype.forEach.call(buttons, function(b){ b.classList.remove("on"); });
      btn.classList.add("on");
      var want = btn.getAttribute("data-filter");
      Array.prototype.forEach.call(cards, function(card){
        var ok = (want === "all") || (card.getAttribute("data-status") === want);
        card.classList.toggle("hide", !ok);
      });
    });
  });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
})();
"""


def _badge_class(status: str) -> str:
    return {"採用": "b-ok", "未確認": "b-un"}.get(status, "b-no")


def _fmt(value, unit: str = "", decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}{unit}"


def _event_card_html(entry: dict) -> str:
    event = entry["event"]
    event_id = str(event.get("event_id", ""))
    status = str(event.get("review_status") or "未確認")
    metrics = entry["metrics"]
    # class_reviewed は「人が直したか」の真偽ではなく直した車種が入ることがあるので、
    # 真偽値らしい文字列でなければそれを優先して表示する。
    reviewed_class = str(event.get("class_reviewed", "")).strip()
    klass = reviewed_class if reviewed_class not in {"", "False", "false", "True", "true"} \
        else str(event.get("class") or "")
    span = "—"
    start, end = _to_float(event.get("t_start_s")), _to_float(event.get("t_end_s"))
    if start is not None and end is not None:
        span = f"{start:.2f} 〜 {end:.2f} 秒"
    rows = [
        ("離隔距離 LC", _fmt(metrics.get("lc"), " m")),
        ("通過時間", _fmt(metrics.get("passing_time"), " 秒")),
        ("相対速度", _fmt(metrics.get("relative_speed"), " km/h")),
        ("TTC", _fmt(metrics.get("ttc"), " 秒")),
        ("距離p2", _fmt(metrics.get("distance_p2"), " m")),
        ("距離p3", _fmt(metrics.get("distance_p3"), " m")),
        ("動画内の時刻", span),
        ("PCAP上の時刻", str(event.get("veloview_t_start", "") or "—")),
        ("元動画", str(event.get("source_video", "") or "—")),
    ]
    kv = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)

    quick_bits = []
    if metrics.get("lc") is not None:
        quick_bits.append(f"LC {metrics['lc']:.2f}m")
    if metrics.get("passing_time") is not None:
        quick_bits.append(f"通過 {metrics['passing_time']:.2f}s")
    if metrics.get("ttc") is not None:
        quick_bits.append(f"TTC {metrics['ttc']:.2f}s")
    quick = "　".join(quick_bits) or "点群の解析値なし"

    if entry["clip"]:
        video = ('<video controls preload="none" src="' + _esc(entry["clip"]) + '"></video>'
                 '<div class="legend">' + _esc(entry["clip"]) + "</div>")
    else:
        video = ('<div class="novideo">クリップなし'
                 "<div>（動画の切り抜きがこの出力フォルダにありません）</div></div>")

    reason = str(event.get("review_reason") or "").strip()
    reason_html = f'<div class="reason">{_esc(reason)}</div>' if reason else ""
    warn_html = ""
    warnings = entry["warnings"]
    if entry["metrics_error"]:
        warnings = warnings + [f"点群の解析に失敗しました: {entry['metrics_error']}"]
    if warnings:
        warn_html = ('<div class="warns">' + "".join(
            f"<div>⚠ {_esc(w)}</div>" for w in warnings) + "</div>")

    data_js = f'{DATA_DIR_NAME}/{entry["js_name"]}' if entry.get("js_name") else ""
    return (
        f'<section class="card" data-id="{_esc(event_id)}" data-status="{_esc(status)}"'
        f' data-js="{_esc(data_js)}">'
        '<div class="head">'
        '<span class="arrow">▶</span>'
        f'<span class="eid">{_esc(event_id)}</span>'
        f'<span class="badge {_badge_class(status)}">{_esc(status)}</span>'
        f'<span class="klass">{_esc(klass or "車種不明")}</span>'
        f'<span class="quick">{_esc(quick)}</span>'
        "</div>"
        '<div class="body">'
        '<div class="grid">'
        f'<div class="col-video">{video}</div>'
        f'<div class="col-info"><table class="kv">{kv}</table>{reason_html}{warn_html}</div>'
        "</div>"
        "<h3>点群ビュー</h3>"
        '<div class="cloud"><div class="loading">カードを開くと読み込みます。</div></div>'
        "</div></section>"
    )


def render_html(info: dict, entries: list[dict], generated_at: str) -> str:
    counts = {"採用": 0, "未確認": 0, "除外": 0}
    for entry in entries:
        status = str(entry["event"].get("review_status") or "未確認")
        counts[status] = counts.get(status, 0) + 1
    meta_bits = [("セッション", info.get("session") or "—")]
    if info.get("subject_id"):
        meta_bits.append(("被験者", info["subject_id"]))
    if info.get("collection_date"):
        meta_bits.append(("収集日", info["collection_date"]))
    if info.get("view"):
        meta_bits.append(("撮影方向", info["view"]))
    if info.get("started_at"):
        meta_bits.append(("解析実行", info["started_at"]))
    meta_bits.append(("レポート作成", generated_at))
    meta = "".join(f"<span>{_esc(k)}: {_esc(v)}</span>" for k, v in meta_bits)

    count_html = "".join(
        f'<span class="count">{_esc(name)} {counts.get(name, 0)} 件</span>'
        for name in ("採用", "未確認") if counts.get(name)
    ) or '<span class="count">イベント 0 件</span>'
    count_html = f'<span class="count">全 {len(entries)} 件</span>' + count_html

    cards = "".join(_event_card_html(entry) for entry in entries) or (
        '<div class="loading">表示できるイベントがありません。</div>')

    payload = _json_for_script({"count": len(entries)})
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>追い越し解析レポート {_esc(info.get('session') or '')}</title>"
        f"<style>{STYLE}</style></head><body>"
        "<header>"
        "<h1>自転車追い越し解析レポート</h1>"
        f'<div class="meta">{meta}</div>'
        f'<div class="counts">{count_html}</div>'
        "</header><main>"
        '<div class="toolbar">'
        '<button type="button" data-filter="all" class="on">すべて</button>'
        '<button type="button" data-filter="採用">採用</button>'
        '<button type="button" data-filter="未確認">未確認</button>'
        "</div>"
        f"{cards}"
        "</main>"
        "<footer>動画はこのフォルダの clips/ を相対パスで参照しています"
        "（report.html だけを別の場所へ移すと再生できません）。"
        "点群は report_data/ の js を、カードを開いたときに読み込みます。</footer>"
        f'<script>window.__REPORT={payload};</script>'
        f"<script>{SCRIPT}</script>"
        "</body></html>\n"
    )


# ── 本体 ─────────────────────────────────────────────────────────────────────

def generate_report(out_dir: Path,
                    max_points_per_frame: int = MAX_POINTS_PER_FRAME,
                    max_points_total: int = MAX_POINTS_TOTAL,
                    generated_at: str | None = None,
                    session_info: dict | None = None) -> dict:
    """``out_dir`` に ``report.html`` と ``report_data/`` を作る。

    ``session_info`` はヘッダに出す情報（``session`` / ``subject_id`` /
    ``collection_date`` / ``view`` / ``started_at``）の上書き。``run_pipeline`` の
    段階7bは ``pipeline_report.json`` を書く前に呼ばれる（＝前回実行分しか無い）ので、
    実行中の値をここで渡す。単体CLIでは ``pipeline_report.json`` から読む。

    戻り値は ``{"path", "data_dir", "events", "accepted", "unreviewed",
    "clips", "data_files", "bytes"}``。
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise ReportError(f"出力フォルダがありません: {out_dir}")
    events = load_events(out_dir)
    info = load_session_info(out_dir)
    for key, value in (session_info or {}).items():
        if str(value or "").strip():
            info[key] = value
    data_dir = out_dir / DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    written: list[Path] = []
    data_bytes = 0
    for event in events:
        event_id = str(event.get("event_id", ""))
        metrics, warnings, metrics_error = load_metrics(out_dir, event_id)
        entry = {
            "event": event,
            "metrics": metrics,
            "warnings": warnings,
            "metrics_error": metrics_error,
            "clip": find_clip(out_dir, event),
            "js_name": "",
        }
        lidar_csv = out_dir / "lidar" / f"overtaking_{event_id}.csv"
        if lidar_csv.is_file():
            cloud = load_point_frames(lidar_csv, max_points_per_frame, max_points_total)
            if cloud["frames"]:
                name = f"event_{_safe_id(event_id)}.js"
                text = event_js_text(event_id, cloud,
                                     load_trajectory(out_dir, event_id), metrics)
                path = data_dir / name
                path.write_text(text, encoding="utf-8")
                written.append(path)
                data_bytes += path.stat().st_size
                entry["js_name"] = name
                entry["points"] = cloud["points"]
        entries.append(entry)

    # 前回の実行で作った、今回は対象外になった js を消す（古い点群が残らないように）
    keep = {path.name for path in written}
    for stale in data_dir.glob("event_*.js"):
        if stale.name not in keep:
            stale.unlink()

    stamp = generated_at or datetime.now().isoformat(timespec="seconds")
    html = render_html(info, entries, stamp)
    report_path = out_dir / REPORT_NAME
    report_path.write_text(html, encoding="utf-8")

    statuses = [str(e["event"].get("review_status") or "未確認") for e in entries]
    return {
        "path": str(report_path),
        "data_dir": str(data_dir),
        "events": len(entries),
        "accepted": statuses.count("採用"),
        "unreviewed": statuses.count("未確認"),
        "clips": sum(1 for e in entries if e["clip"]),
        "data_files": len(written),
        "bytes": data_bytes + report_path.stat().st_size,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="解析結果の閲覧用HTMLレポート（report.html）を作る",
    )
    parser.add_argument("--out", required=True,
                        help="run_pipeline.py の出力フォルダ（reviewed_overtaking_events.csv がある場所）")
    parser.add_argument("--max-points-per-frame", type=int, default=MAX_POINTS_PER_FRAME,
                        help=f"1フレームあたりに残す点数の上限（既定: {MAX_POINTS_PER_FRAME}）")
    parser.add_argument("--max-points-total", type=int, default=MAX_POINTS_TOTAL,
                        help=f"1イベントあたりに残す点数の予算（既定: {MAX_POINTS_TOTAL}）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        result = generate_report(Path(args.out).expanduser().resolve(),
                                 max_points_per_frame=args.max_points_per_frame,
                                 max_points_total=args.max_points_total)
    except ReportError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(f"レポート: {result['path']}")
    print(f"  イベント {result['events']} 件"
          f"（採用 {result['accepted']} / 未確認 {result['unreviewed']}）"
          f" / クリップ {result['clips']} 件 / 点群 {result['data_files']} 件")
    print(f"  合計サイズ: {result['bytes'] / 1_048_576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
