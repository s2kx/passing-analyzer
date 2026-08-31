#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""時刻同期（GoPro↔PCAP オフセット）を点群だけから独立に検証するツール。

``run_pipeline.py`` の段階3は GoPro の GPMF に埋まった GPS UTC と PCAP の先頭
パケット時刻からオフセットを求めている（``pcap時刻 = gopro時刻 + offset``）。
この値が正しいかどうかは、これまで「最終結果が正解と合うかどうか」でしか
確かめられなかった。本ツールは **まったく別の経路** でオフセットを推定して
突き合わせる。

  * GPS 側 …… ``out/gps/<動画stem>/telemetry_gps.xlsx`` の 2D 速度（m/s）。
    チャプター動画は GPMF の UTC 差で 1 本の GoPro 経過時間軸につなぐ。
  * LiDAR 側 …… 点群から推定した自車（自転車）速度。LiDAR は自転車の上に
    載っているので、静止物・路側構造物は自転車速度で -Y 方向へ流れる。
    連続する 2 フレームの非地面点を XY 占有格子にして Y 方向にずらしながら
    相関を取り、最も一致するずれ量 dy から ``自車速度 = -dy / dt`` を得る。

2 本の速度時系列の相互相関のピーク位置がオフセットの推定値になる。
``sync.json`` の現行値との差・相関ピークの鋭さ（信頼度）・前半後半のばらつき
（ドリフト）を報告し、差が閾値を超えたら警告する。

使い方::

    python tools/sync_check.py --out output/ID1_v3
    python tools/sync_check.py --out output/ID3_w4 --session "E:/.../260701/ID3"
    python tools/sync_check.py --out output/ID1_v3 --json out_j/ID1_sync_check.json

``--out`` のフォルダに ``sync.json`` と ``gps/`` があれば、PCAP と動画の場所は
``sync.json`` に書かれた絶対パスから復元するので ``--session`` は省略できる。

実データを一切書き換えない読み取り専用のツール。1.4GB の PCAP でも
既定設定（1 秒おきに 2 フレームだけ復号）で 2 分程度で終わる。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from lidar_autotrack import estimate_ground_plane, filter_points  # noqa: E402
from lidar_pcap import VelodynePcap  # noqa: E402


# ── 既定パラメータ ───────────────────────────────────────────────────────────
# 自車速度を推定する間隔（秒）。GPS も LiDAR も速度の変化は数秒スケールなので
# 1 秒おきで相互相関には十分。フレーム復号が支配的なコストなのでここが速度を決める。
SAMPLE_SECONDS = 1.0
# 速度推定に使うフレーム間隔。VLP-16 は約 10Hz なので 3 で dt≒0.3 秒。
# 大きいほど変位が増えて速度分解能は上がるが、シーンの入れ替わりで相関が落ちる。
PAIR_STEP = 3

# 占有格子。X は粗く（横ずれ・ヨー回転に鈍感にする）、Y は細かく（速度分解能）。
GRID_X_BIN = 0.5
GRID_Y_BIN = 0.10
GRID_MAX_X = 10.0
GRID_MAX_Y = 30.0

MAX_EGO_SPEED = 22.0        # 探索する自車速度の上限 (m/s)
MIN_EGO_SPEED = -2.0        # 後退方向の許容（ノイズ分だけ負も探索する）
MIN_GRID_POINTS = 150       # 片方でもこれ未満のフレーム対は捨てる
MIN_SHIFT_PEAK = 0.10       # 占有格子の一致度がこれ未満なら信用しない
MIN_SHIFT_PROMINENCE = 0.02  # ピークの突出度（別位置の最大値との差）
SHIFT_EXCLUDE_M = 1.0       # 突出度を測るときにピーク周辺から除く幅 (m)

MAX_LAG = 30.0              # 現行オフセットの周り±何秒を探索するか
LAG_STEP = 0.05             # 相互相関の刻み (秒)
PEAK_DROP = 0.05            # ピーク幅を測る相関の落ち幅

# 加速度（速度の微分）での再推定。速度そのものの相関は山が広い（ID1で幅5秒）が、
# 微分すると山が鋭くなる（同0.95秒）ので、位置の詰めに使う。
ACCEL_GRID = 0.25           # 微分を取る一様格子の間隔 (秒)
ACCEL_SMOOTH = 2.0          # 微分前の移動平均の窓 (秒)
ACCEL_LAG = 5.0             # 速度で求めた位置の周り±何秒を詰めるか
MIN_ACCEL_PEAK = 0.15       # これ未満なら加速度の相関は使わない

OFFSET_TOLERANCE = 1.0      # これを超える差は警告（秒）
MIN_CORRELATION = 0.50      # ピーク相関係数の下限（下回れば信頼度低）
MIN_PROMINENCE = 0.15       # ピークが背景（探索範囲の中央値）より高い量の下限
MAX_SEGMENT_SPREAD = 2.0    # 区間ごとの推定のばらつき上限 (秒)
SEGMENTS = 3                # ドリフト確認のための分割数

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ── 1. GPS 速度時系列 ────────────────────────────────────────────────────────

def _cell_values(row: ET.Element, shared: list[str]) -> list[str]:
    """xlsx の 1 行を列順の文字列リストにする（列参照の飛びを埋める）。"""
    values: list[str] = []
    for cell in row.findall(f"{XLSX_NS}c"):
        ref = cell.get("r") or ""
        index = 0
        for ch in ref:
            if ch.isalpha():
                index = index * 26 + ord(ch.upper()) - 64
        index = max(index - 1, len(values))
        while len(values) < index:
            values.append("")
        kind = cell.get("t")
        if kind == "inlineStr":
            text = "".join(t.text or "" for t in cell.iter(f"{XLSX_NS}t"))
        elif kind == "s":
            node = cell.find(f"{XLSX_NS}v")
            position = int(node.text) if node is not None and node.text else -1
            text = shared[position] if 0 <= position < len(shared) else ""
        else:
            node = cell.find(f"{XLSX_NS}v")
            text = node.text or "" if node is not None else ""
        values.append(text)
    return values


def read_gps_speed_rows(path: Path) -> list[tuple[float, float]]:
    """``telemetry_gps.xlsx`` から ``(cts秒, 2D速度 m/s)`` を読む。

    ``extract_telemetry.save_gps_workbook`` が書く 2 シート構成を前提にする。
    シート2 は間引き無し・m/s、シート1 は毎秒 1 行・k/s（＝m/s の 1/1000）。
    """
    if not path.is_file():
        return []
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in si.iter(f"{XLSX_NS}t"))
                for si in root.findall(f"{XLSX_NS}si")
            ]
        sheet_name = "xl/worksheets/sheet2.xml"
        if sheet_name not in names:
            sheet_name = "xl/worksheets/sheet1.xml"
        sheet = ET.fromstring(book.read(sheet_name))
    rows = sheet.findall(f".//{XLSX_NS}sheetData/{XLSX_NS}row")
    if len(rows) < 2:
        return []
    headers = _cell_values(rows[0], shared)
    time_col = headers.index("cts") if "cts" in headers else 0
    speed_col = next(
        (i for i, header in enumerate(headers) if "GPS (2D speed)" in header), 5
    )
    scale = 1000.0 if "k/s" in (headers[speed_col] if speed_col < len(headers) else "") else 1.0
    result: list[tuple[float, float]] = []
    for row in rows[1:]:
        values = _cell_values(row, shared)
        if max(time_col, speed_col) >= len(values):
            continue
        try:
            seconds = float(values[time_col])
            speed = float(values[speed_col]) * scale
        except ValueError:
            continue
        result.append((seconds, speed))
    return result


def chapter_offsets(videos: list[Path]) -> tuple[dict[str, float], list[str]]:
    """チャプター動画の先頭が GoPro 経過時間軸のどこかを返す（先頭動画=0秒）。

    GPMF の GPS UTC の差を使う。``run_pipeline`` の連結タイムライン
    （``t_start_s - local_t_start_s``）と一致することを ID1 で確認済み。
    読めない動画があれば警告を返し、その動画は辞書に載せない。
    """
    from gpmf_sync import extract_gps_utc_at_start

    warnings: list[str] = []
    starts: dict[str, float] = {}
    base: float | None = None
    for video in videos:
        try:
            utc = extract_gps_utc_at_start(video).timestamp()
        except Exception as exc:  # noqa: BLE001 - 1本読めなくても続行する
            warnings.append(f"{video.name}: GPS UTCを読めません（{exc}）")
            continue
        if base is None:
            base = utc
        starts[video.stem.lower()] = utc - base
    return starts, warnings


def gps_speed_series(out_dir: Path, videos: list[Path]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """全チャプターの GPS 速度を GoPro 経過時間軸の 1 本の時系列にまとめる。"""
    starts, warnings = chapter_offsets(videos)
    gps_dir = out_dir / "gps"
    samples: list[tuple[float, float]] = []
    if not gps_dir.is_dir():
        warnings.append(f"GPS抽出結果がありません: {gps_dir}")
        return np.zeros(0), np.zeros(0), warnings
    for book in sorted(gps_dir.glob("*/telemetry_gps.xlsx")):
        stem = book.parent.name.lower()
        if stem not in starts:
            warnings.append(f"{book.parent.name}: 対応する動画が無いのでGPSを使いません")
            continue
        rows = read_gps_speed_rows(book)
        if not rows:
            warnings.append(f"{book.parent.name}: GPS速度を読めませんでした")
            continue
        base = starts[stem]
        samples.extend((base + seconds, speed) for seconds, speed in rows)
    if not samples:
        return np.zeros(0), np.zeros(0), warnings
    samples.sort()
    times = np.array([t for t, _ in samples], dtype=float)
    speeds = np.array([v for _, v in samples], dtype=float)
    return times, speeds, warnings


# ── 2. 点群からの自車速度 ────────────────────────────────────────────────────

def occupancy_grid(points, x_bin: float = GRID_X_BIN, y_bin: float = GRID_Y_BIN,
                   max_x: float = GRID_MAX_X, max_y: float = GRID_MAX_Y) -> np.ndarray:
    """XY 平面の占有格子（0/1）を作る。点数の多寡に引きずられないよう二値化する。"""
    array = np.asarray(points, dtype=float)
    nx = int(round(2 * max_x / x_bin))
    ny = int(round(2 * max_y / y_bin))
    grid = np.zeros((nx, ny), dtype=np.float32)
    if array.size == 0:
        return grid
    if array.ndim != 2 or array.shape[1] < 2:
        raise ValueError("点群は (N, 2) 以上の配列である必要があります")
    xi = np.floor((array[:, 0] + max_x) / x_bin).astype(int)
    yi = np.floor((array[:, 1] + max_y) / y_bin).astype(int)
    inside = (xi >= 0) & (xi < nx) & (yi >= 0) & (yi < ny)
    grid[xi[inside], yi[inside]] = 1.0
    return grid


def shift_profile(grid_a: np.ndarray, grid_b: np.ndarray,
                  min_shift: int, max_shift: int) -> np.ndarray:
    """``grid_b`` を Y 方向に s ビンずらしたときの一致度を s ごとに返す。

    一致度は重なり領域での二値コサイン類似度
    ``|A∩B| / sqrt(|A|*|B|)``。重なりが狭くなる大きな s でも公平に比べられる。
    """
    values = []
    for shift in range(min_shift, max_shift + 1):
        if shift < 0:
            left, right = grid_a[:, -shift:], grid_b[:, :shift]
        elif shift > 0:
            left, right = grid_a[:, :-shift], grid_b[:, shift:]
        else:
            left, right = grid_a, grid_b
        if left.size == 0:
            values.append(0.0)
            continue
        denominator = float(left.sum()) * float(right.sum())
        values.append(float((left * right).sum()) / (denominator ** 0.5) if denominator > 0 else 0.0)
    return np.array(values, dtype=float)


def refine_peak(values: np.ndarray, exclude: int) -> tuple[float, float, float]:
    """離散のピークを放物線補間する。``(位置(小数index), 山の高さ, 突出度)``。"""
    if values.size == 0:
        return 0.0, 0.0, 0.0
    index = int(np.argmax(values))
    peak = float(values[index])
    position = float(index)
    if 0 < index < values.size - 1:
        curve = values[index - 1] - 2 * values[index] + values[index + 1]
        if abs(curve) > 1e-12:
            delta = 0.5 * (values[index - 1] - values[index + 1]) / curve
            position += float(np.clip(delta, -1.0, 1.0))
    outside = np.delete(values, slice(max(0, index - exclude), index + exclude + 1))
    prominence = peak - float(outside.max()) if outside.size else peak
    return position, peak, prominence


def estimate_ego_speed(points_a, points_b, dt: float,
                       x_bin: float = GRID_X_BIN, y_bin: float = GRID_Y_BIN,
                       max_x: float = GRID_MAX_X, max_y: float = GRID_MAX_Y,
                       max_speed: float = MAX_EGO_SPEED,
                       min_speed: float = MIN_EGO_SPEED) -> dict:
    """非地面点 2 フレームから自車の前進速度 (m/s) を推定する。

    ``points_a`` / ``points_b`` は地面除去済みの ``(N, 3)`` 配列。静止物は
    ``dy = -speed * dt`` だけ Y 方向にずれるので、一致度が最大になるずれ量から
    速度を逆算する。戻り値には信頼判定用の一致度・突出度・点数も入れる。
    """
    grid_a = occupancy_grid(points_a, x_bin, y_bin, max_x, max_y)
    grid_b = occupancy_grid(points_b, x_bin, y_bin, max_x, max_y)
    count_a, count_b = float(grid_a.sum()), float(grid_b.sum())
    result = {
        "speed": float("nan"), "peak": 0.0, "prominence": 0.0,
        "cells": min(count_a, count_b), "ok": False,
    }
    if dt <= 0 or count_a < MIN_GRID_POINTS or count_b < MIN_GRID_POINTS:
        return result
    min_shift = int(np.floor(-max_speed * dt / y_bin))
    max_shift = int(np.ceil(-min_speed * dt / y_bin))
    values = shift_profile(grid_a, grid_b, min_shift, max_shift)
    exclude = max(1, int(round(SHIFT_EXCLUDE_M / y_bin)))
    position, peak, prominence = refine_peak(values, exclude)
    shift_m = (min_shift + position) * y_bin
    speed = -shift_m / dt
    result.update({
        "speed": float(speed), "peak": peak, "prominence": prominence,
        "ok": bool(peak >= MIN_SHIFT_PEAK and prominence >= MIN_SHIFT_PROMINENCE),
    })
    return result


def ego_speed_series(pcap: VelodynePcap, sample_seconds: float = SAMPLE_SECONDS,
                     pair_step: int = PAIR_STEP, start: float = 0.0,
                     stop: float | None = None, progress: bool = False) -> dict:
    """PCAP 全体を間引きながら走査し、自車速度の時系列を作る。

    1 サンプルにつき 2 フレームだけ復号する。既定（1 秒おき）なら 1.4GB・25 分の
    PCAP でもフレーム復号は約 3000 回で済む。
    """
    duration = pcap.duration if stop is None else min(stop, pcap.duration)
    times: list[float] = []
    speeds: list[float] = []
    peaks: list[float] = []
    prominences: list[float] = []
    flags: list[bool] = []
    planned = np.arange(start, max(duration - pair_step * 0.11, start + 1e-9), sample_seconds)
    started = time.time()
    last_index = -1
    for step, target in enumerate(planned):
        index = pcap.nearest_frame(float(target))
        if index <= last_index or index + pair_step >= len(pcap.frames):
            continue
        last_index = index
        dt = pcap.frames[index + pair_step].timestamp - pcap.frames[index].timestamp
        points_a = pcap.decode_frame(index)
        points_b = pcap.decode_frame(index + pair_step)
        filtered_a = filter_points(points_a, estimate_ground_plane(points_a),
                                   max_abs_x=GRID_MAX_X, max_abs_y=GRID_MAX_Y)
        filtered_b = filter_points(points_b, estimate_ground_plane(points_b),
                                   max_abs_x=GRID_MAX_X, max_abs_y=GRID_MAX_Y)
        estimate = estimate_ego_speed(filtered_a, filtered_b, dt)
        times.append(pcap.frames[index].timestamp + dt / 2.0)
        speeds.append(estimate["speed"])
        peaks.append(estimate["peak"])
        prominences.append(estimate["prominence"])
        flags.append(bool(estimate["ok"]))
        if progress and step % 100 == 0:
            done = step + 1
            elapsed = time.time() - started
            print(f"  [lidar] {done}/{len(planned)} サンプル "
                  f"({elapsed:.0f}秒経過)", flush=True)
    return {
        "time": np.array(times, dtype=float),
        "speed": np.array(speeds, dtype=float),
        "peak": np.array(peaks, dtype=float),
        "prominence": np.array(prominences, dtype=float),
        "ok": np.array(flags, dtype=bool),
    }


# ── 3. 相互相関でオフセットを推定 ────────────────────────────────────────────

def cross_correlate_offset(lidar_time: np.ndarray, lidar_speed: np.ndarray,
                           gps_time: np.ndarray, gps_speed: np.ndarray,
                           center: float = 0.0, max_lag: float = MAX_LAG,
                           lag_step: float = LAG_STEP) -> dict:
    """``pcap時刻 = gopro時刻 + offset`` の offset を相互相関で推定する。

    候補 offset ``o`` に対し、LiDAR サンプル時刻 ``t``（PCAP 相対秒）を GoPro 経過
    時間 ``t - o`` に写して GPS 速度を線形補間し、LiDAR 自車速度との相関係数を取る。
    どの ``o`` でも同じサンプル集合で比べられるよう、探索範囲の両端で GPS 区間から
    はみ出すサンプルはあらかじめ除く。
    """
    lags = np.arange(center - max_lag, center + max_lag + lag_step * 0.5, lag_step)
    result = {
        "offset": None, "peak": 0.0, "prominence": 0.0, "width": None,
        "samples": 0, "lags": lags, "correlation": np.zeros(lags.size),
    }
    if lidar_time.size < 8 or gps_time.size < 8:
        return result
    inside = (lidar_time - lags[-1] >= gps_time[0]) & (lidar_time - lags[0] <= gps_time[-1])
    times, speeds = lidar_time[inside], lidar_speed[inside]
    if times.size < 8 or not np.isfinite(speeds).all():
        return result
    centered = speeds - speeds.mean()
    norm = float(np.sqrt((centered ** 2).sum()))
    if norm <= 0:
        return result
    correlation = np.zeros(lags.size)
    for i, lag in enumerate(lags):
        sampled = np.interp(times - lag, gps_time, gps_speed)
        other = sampled - sampled.mean()
        denominator = norm * float(np.sqrt((other ** 2).sum()))
        correlation[i] = float((centered * other).sum()) / denominator if denominator > 0 else 0.0
    position, peak, _ = refine_peak(correlation, 1)
    offset = float(lags[0] + position * lag_step)
    result.update({
        "offset": offset, "peak": peak,
        # 突出度は「ピーク − 探索範囲全体の中央値」。山が広くても、まぐれの
        # 山（背景と同じ高さ）と本物の山を区別できる。
        "prominence": peak - float(np.median(correlation)),
        "width": peak_width(correlation, lag_step),
        "samples": int(times.size), "correlation": correlation,
    })
    return result


def peak_width(correlation: np.ndarray, lag_step: float, drop: float = PEAK_DROP) -> float | None:
    """ピークの鋭さ。相関が ``peak - drop`` を保つ連続区間の幅（秒）。"""
    if correlation.size == 0:
        return None
    index = int(np.argmax(correlation))
    threshold = correlation[index] - drop
    low = index
    while low > 0 and correlation[low - 1] >= threshold:
        low -= 1
    high = index
    while high < correlation.size - 1 and correlation[high + 1] >= threshold:
        high += 1
    return float((high - low + 1) * lag_step)


def acceleration_series(times: np.ndarray, values: np.ndarray,
                        grid: float = ACCEL_GRID, smooth: float = ACCEL_SMOOTH
                        ) -> tuple[np.ndarray, np.ndarray]:
    """一様格子に載せ直して移動平均をかけ、時間微分（加速度）にする。"""
    if times.size < 4 or times[-1] - times[0] <= grid * 4:
        return np.zeros(0), np.zeros(0)
    uniform = np.arange(times[0], times[-1], grid)
    sampled = np.interp(uniform, times, values)
    window = max(3, int(round(smooth / grid)))
    kernel = np.ones(window) / window
    smoothed = np.convolve(sampled, kernel, mode="same")
    margin = window // 2 + 1
    if uniform.size <= 2 * margin + 4:
        return np.zeros(0), np.zeros(0)
    return uniform[margin:-margin], np.gradient(smoothed, grid)[margin:-margin]


# ── 4. セッション単位の検証 ──────────────────────────────────────────────────

def _subdir(root: Path, name: str) -> Path | None:
    if not root.is_dir():
        return None
    for child in root.iterdir():
        if child.is_dir() and child.name.lower() == name:
            return child
    return None


def discover_inputs(out_dir: Path, session_dir: Path | None,
                    pcap_override: Path | None) -> tuple[Path, list[Path], dict, list[str]]:
    """PCAP と動画一覧を、``sync.json`` かセッションフォルダから見つける。"""
    warnings: list[str] = []
    sync: dict = {}
    sync_path = out_dir / "sync.json"
    if sync_path.is_file():
        try:
            sync = json.loads(sync_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sync.json を読めません: {exc}")
    pcap_path: Path | None = pcap_override
    if pcap_path is None and sync.get("pcap"):
        candidate = Path(str(sync["pcap"]))
        if candidate.is_file():
            pcap_path = candidate
    if pcap_path is None and session_dir is not None:
        root = _subdir(session_dir, "lidar") or session_dir
        found = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".pcap")
        if found:
            pcap_path = found[0]
    if pcap_path is None:
        raise FileNotFoundError(
            "PCAPが見つかりません。--session か --pcap を指定してください"
        )

    video_root: Path | None = None
    if session_dir is not None:
        video_root = _subdir(session_dir, "gopro") or session_dir
    elif sync.get("video"):
        video_root = Path(str(sync["video"])).parent
    videos: list[Path] = []
    if video_root is not None and video_root.is_dir():
        videos = sorted(
            p for p in video_root.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mov"}
        )
    if not videos:
        warnings.append("GoPro動画が見つかりません（チャプター接続に GPMF の UTC が必要です）")
    return pcap_path, videos, sync, warnings


def save_series(path: Path, series: dict) -> None:
    """自車速度の時系列を CSV に保存する（再計算せず相関だけ試すため）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("time_s,speed_ms,peak,prominence,ok\n")
        for index in range(series["time"].size):
            stream.write(
                f"{series['time'][index]:.4f},{series['speed'][index]:.4f},"
                f"{series['peak'][index]:.4f},{series['prominence'][index]:.4f},"
                f"{int(series['ok'][index])}\n"
            )


def load_series(path: Path) -> dict:
    """``save_series`` が書いた CSV を読み戻す。"""
    rows = [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()[1:] if line]
    return {
        "time": np.array([float(r[0]) for r in rows]),
        "speed": np.array([float(r[1]) for r in rows]),
        "peak": np.array([float(r[2]) for r in rows]),
        "prominence": np.array([float(r[3]) for r in rows]),
        "ok": np.array([bool(int(r[4])) for r in rows]),
    }


def check_session(out_dir: Path, session_dir: Path | None = None,
                  pcap_override: Path | None = None,
                  sample_seconds: float = SAMPLE_SECONDS,
                  pair_step: int = PAIR_STEP, max_lag: float = MAX_LAG,
                  tolerance: float = OFFSET_TOLERANCE,
                  segments: int = SEGMENTS, limit_seconds: float | None = None,
                  progress: bool = False, series_path: Path | None = None) -> dict:
    """1 セッションのオフセットを検証して報告用の辞書を返す。"""
    started = time.time()
    pcap_path, videos, sync, warnings = discover_inputs(out_dir, session_dir, pcap_override)
    current = sync.get("offset")
    current_offset = float(current) if isinstance(current, (int, float)) else None

    gps_time, gps_speed, gps_warnings = gps_speed_series(out_dir, videos)
    warnings.extend(gps_warnings)

    if series_path is not None and series_path.is_file():
        if progress:
            print(f"[lidar] 保存済みの自車速度を再利用します: {series_path}", flush=True)
        series = load_series(series_path)
        pcap_duration, frame_count = float(series["time"][-1]) if series["time"].size else 0.0, 0
        pcap_start_unix = float(sync.get("pcap_start_unix") or 0.0)
    else:
        if progress:
            print(f"[pcap] {pcap_path.name} を索引中...", flush=True)
        pcap = VelodynePcap(pcap_path)
        pcap_duration, frame_count = pcap.duration, len(pcap.frames)
        pcap_start_unix = pcap.start_unix_time
        if progress:
            print(f"  フレーム {frame_count} 枚 / {pcap_duration:.0f} 秒", flush=True)
        series = ego_speed_series(pcap, sample_seconds, pair_step,
                                  stop=limit_seconds, progress=progress)
        if series_path is not None:
            save_series(series_path, series)

    usable = series["ok"] & np.isfinite(series["speed"])
    lidar_time, lidar_speed = series["time"][usable], series["speed"][usable]
    report: dict = {
        "out_dir": str(out_dir),
        "pcap": str(pcap_path),
        "pcap_start_unix": pcap_start_unix,
        "pcap_duration_s": round(pcap_duration, 3),
        "videos": [v.name for v in videos],
        "current_offset": current_offset,
        "sync_source": sync.get("source"),
        "lidar_samples": int(series["time"].size),
        "lidar_usable": int(lidar_time.size),
        "gps_samples": int(gps_time.size),
        "gps_span_s": round(float(gps_time[-1] - gps_time[0]), 3) if gps_time.size else 0.0,
        "sample_seconds": sample_seconds,
        "pair_step": pair_step,
        "warnings": warnings,
        "status": "ok",
    }
    if lidar_time.size < 30 or gps_time.size < 30:
        report["status"] = "failed"
        warnings.append("有効なサンプルが少なすぎて相互相関を取れません")
        report["elapsed_s"] = round(time.time() - started, 1)
        return report

    center = current_offset if current_offset is not None else 0.0
    overall = cross_correlate_offset(lidar_time, lidar_speed, gps_time, gps_speed,
                                     center=center, max_lag=max_lag)
    report.update({
        "estimated_offset": None if overall["offset"] is None else round(overall["offset"], 3),
        "correlation_peak": round(overall["peak"], 4),
        "correlation_prominence": round(overall["prominence"], 4),
        "peak_width_s": None if overall["width"] is None else round(overall["width"], 3),
        "correlated_samples": overall["samples"],
    })
    if overall["offset"] is None:
        report["status"] = "failed"
        warnings.append("相互相関のピークを求められませんでした")
        report["elapsed_s"] = round(time.time() - started, 1)
        return report

    if current_offset is not None:
        report["difference"] = round(overall["offset"] - current_offset, 3)

    # 加速度（速度の微分）で位置を詰める。速度の相関は山が広いので、鋭い山を持つ
    # 加速度で同じ位置が出れば推定が裏付けられる。
    lidar_accel_t, lidar_accel = acceleration_series(lidar_time, lidar_speed)
    gps_accel_t, gps_accel = acceleration_series(gps_time, gps_speed)
    refined = cross_correlate_offset(lidar_accel_t, lidar_accel, gps_accel_t, gps_accel,
                                     center=overall["offset"], max_lag=ACCEL_LAG)
    if refined["offset"] is not None and refined["peak"] >= MIN_ACCEL_PEAK:
        report.update({
            "refined_offset": round(refined["offset"], 3),
            "refined_peak": round(refined["peak"], 4),
            "refined_width_s": None if refined["width"] is None else round(refined["width"], 3),
            "refined_gap_s": round(refined["offset"] - overall["offset"], 3),
        })
    else:
        report["refined_offset"] = None
        warnings.append("加速度の相互相関では裏付けが取れませんでした（山が低い）")

    # 前半・後半で推定値が動かないか（クロックドリフトや章の継ぎ目の検出）
    parts: list[dict] = []
    if segments > 1 and lidar_time.size >= segments * 30:
        edges = np.array_split(np.arange(lidar_time.size), segments)
        for number, indices in enumerate(edges, 1):
            piece = cross_correlate_offset(
                lidar_time[indices], lidar_speed[indices], gps_time, gps_speed,
                center=center, max_lag=max_lag,
            )
            parts.append({
                "segment": number,
                "start_s": round(float(lidar_time[indices[0]]), 1),
                "end_s": round(float(lidar_time[indices[-1]]), 1),
                "offset": None if piece["offset"] is None else round(piece["offset"], 3),
                "peak": round(piece["peak"], 4),
                "samples": piece["samples"],
            })
    report["segments"] = parts
    values = [p["offset"] for p in parts if p["offset"] is not None]
    report["segment_spread_s"] = round(max(values) - min(values), 3) if len(values) > 1 else None

    # 参考値: 速度そのものの一致度（現行オフセットで揃えたときの残差）
    if current_offset is not None:
        aligned = np.interp(lidar_time - current_offset, gps_time, gps_speed)
        report["speed_bias_ms"] = round(float(np.mean(lidar_speed - aligned)), 3)
        report["speed_rmse_ms"] = round(float(np.sqrt(np.mean((lidar_speed - aligned) ** 2))), 3)

    # 推定の不確かさ: 区間ごとのばらつきと加速度推定とのずれの大きい方。
    uncertainty = report.get("segment_spread_s") or 0.0
    if report.get("refined_offset") is not None:
        uncertainty = max(uncertainty, abs(report["refined_gap_s"]))
    report["uncertainty_s"] = round(float(uncertainty), 3)

    if overall["peak"] < MIN_CORRELATION or overall["prominence"] < MIN_PROMINENCE:
        report["status"] = "low_confidence"
        warnings.append(
            f"相関が弱いので推定値を信用できません（ピーク {overall['peak']:.2f} / "
            f"突出度 {overall['prominence']:.2f}）"
        )
    elif report.get("segment_spread_s") is not None and report["segment_spread_s"] > MAX_SEGMENT_SPREAD:
        report["status"] = "low_confidence"
        warnings.append(
            f"区間ごとの推定が {report['segment_spread_s']:.2f} 秒ばらついています"
            "（クロックのドリフトか相関の不安定）"
        )
    if current_offset is None:
        report["status"] = "no_reference"
        warnings.append("sync.json に offset がありません")
    elif abs(report["difference"]) > tolerance and report["status"] == "ok":
        report["status"] = "mismatch"
        warnings.append(
            f"現行オフセットとの差が {report['difference']:+.2f} 秒で許容 {tolerance} 秒を超えます"
        )
    report["elapsed_s"] = round(time.time() - started, 1)
    return report


STATUS_LABEL = {
    "ok": "一致",
    "mismatch": "不一致（要確認）",
    "low_confidence": "信頼度低",
    "no_reference": "比較対象なし",
    "failed": "検証不能",
}


def format_report(report: dict) -> str:
    """人が読む 1 セッション分の要約。"""
    lines = [
        f"[{Path(report['out_dir']).name}] {STATUS_LABEL.get(report['status'], report['status'])}",
        f"  PCAP: {Path(report['pcap']).name}  {report['pcap_duration_s']:.0f}秒",
        f"  LiDAR自車速度: {report['lidar_usable']}/{report['lidar_samples']} サンプル有効"
        f"（{report['sample_seconds']}秒おき・{report['pair_step']}フレーム対）",
        f"  GPS: {report['gps_samples']} 行 / {report['gps_span_s']:.0f}秒",
    ]
    current = report.get("current_offset")
    estimated = report.get("estimated_offset")
    if estimated is not None:
        lines.append(
            f"  推定オフセット {estimated:+.3f} 秒 / 現行 "
            f"{'なし' if current is None else f'{current:+.3f} 秒'}"
            + (f" / 差 {report['difference']:+.3f} 秒" if "difference" in report else "")
        )
        lines.append(
            f"  相関ピーク {report['correlation_peak']:.3f} / 突出度 "
            f"{report['correlation_prominence']:.3f} / ピーク幅 "
            f"{report.get('peak_width_s')} 秒 / 使用 {report['correlated_samples']} サンプル"
        )
        if report.get("refined_offset") is not None:
            lines.append(
                f"  加速度での裏付け {report['refined_offset']:+.3f} 秒"
                f"（速度推定との差 {report['refined_gap_s']:+.3f} 秒 / ピーク "
                f"{report['refined_peak']:.3f} / 幅 {report['refined_width_s']} 秒）"
            )
        if report.get("uncertainty_s") is not None:
            lines.append(f"  推定の不確かさ ±{report['uncertainty_s']:.2f} 秒")
    for part in report.get("segments") or []:
        value = "—" if part["offset"] is None else f"{part['offset']:+.3f} 秒"
        lines.append(
            f"    区間{part['segment']} {part['start_s']:.0f}〜{part['end_s']:.0f}秒: "
            f"{value}（相関 {part['peak']:.3f} / {part['samples']} サンプル）"
        )
    if report.get("segment_spread_s") is not None:
        lines.append(f"  区間ごとのばらつき {report['segment_spread_s']:.3f} 秒")
    if "speed_rmse_ms" in report:
        lines.append(
            f"  現行オフセットでの速度残差 RMSE {report['speed_rmse_ms']:.2f} m/s "
            f"(偏り {report['speed_bias_ms']:+.2f} m/s)"
        )
    for warning in report.get("warnings", []):
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GPS速度とLiDAR自車運動の相互相関で時刻オフセットを検証する",
    )
    parser.add_argument("--out", required=True, action="append",
                        help="検証する出力フォルダ（sync.json と gps/ がある場所）。複数指定可")
    parser.add_argument("--session", help="セッションフォルダ（省略時は sync.json のパスを使う）")
    parser.add_argument("--pcap", help="PCAPを明示指定する")
    parser.add_argument("--sample", type=float, default=SAMPLE_SECONDS,
                        help=f"自車速度を推定する間隔（秒、既定 {SAMPLE_SECONDS}）")
    parser.add_argument("--pair-step", type=int, default=PAIR_STEP,
                        help=f"速度推定に使うフレーム間隔（既定 {PAIR_STEP}）")
    parser.add_argument("--max-lag", type=float, default=MAX_LAG,
                        help=f"現行オフセットの周り±何秒を探索するか（既定 {MAX_LAG}）")
    parser.add_argument("--tolerance", type=float, default=OFFSET_TOLERANCE,
                        help=f"警告する差の閾値（秒、既定 {OFFSET_TOLERANCE}）")
    parser.add_argument("--segments", type=int, default=SEGMENTS,
                        help=f"ドリフト確認の分割数（既定 {SEGMENTS}、1で無効）")
    parser.add_argument("--limit-seconds", type=float,
                        help="PCAPの先頭から何秒までを使うか（動作確認用）")
    parser.add_argument("--json", help="全結果をJSONで書き出す先")
    parser.add_argument("--series-dir",
                        help="推定した自車速度時系列を保存/再利用するフォルダ"
                             "（<出力フォルダ名>_ego.csv。あれば PCAP を読み直さない）")
    parser.add_argument("--quiet", action="store_true", help="進捗を表示しない")
    args = parser.parse_args(argv)

    session = Path(args.session).expanduser().resolve() if args.session else None
    pcap_override = Path(args.pcap).expanduser().resolve() if args.pcap else None
    reports: list[dict] = []
    for target in args.out:
        out_dir = Path(target).expanduser().resolve()
        if not out_dir.is_dir():
            print(f"[error] 出力フォルダがありません: {out_dir}")
            return 2
        series_path = (
            Path(args.series_dir).expanduser().resolve() / f"{out_dir.name}_ego.csv"
            if args.series_dir else None
        )
        try:
            report = check_session(
                out_dir, session, pcap_override, sample_seconds=args.sample,
                pair_step=args.pair_step, max_lag=args.max_lag,
                tolerance=args.tolerance, segments=args.segments,
                limit_seconds=args.limit_seconds, progress=not args.quiet,
                series_path=series_path,
            )
        except Exception as exc:  # noqa: BLE001 - 1件失敗しても残りを続ける
            report = {"out_dir": str(out_dir), "status": "failed", "warnings": [str(exc)],
                      "pcap": "", "pcap_duration_s": 0.0, "lidar_usable": 0,
                      "lidar_samples": 0, "gps_samples": 0, "gps_span_s": 0.0,
                      "sample_seconds": args.sample, "pair_step": args.pair_step}
            print(f"[error] {out_dir.name}: {exc}")
        reports.append(report)
        print(format_report(report))
        print()

    if len(reports) > 1:
        print("=== まとめ ===")
        print(f"{'出力':<12} {'推定':>10} {'現行':>10} {'差':>8} {'相関':>7} {'突出':>7} {'判定'}")
        for report in reports:
            name = Path(report["out_dir"]).name
            estimated = report.get("estimated_offset")
            current = report.get("current_offset")
            difference = report.get("difference")
            print(
                f"{name:<12} "
                f"{'—' if estimated is None else f'{estimated:+10.3f}'} "
                f"{'—' if current is None else f'{current:+10.3f}'} "
                f"{'—' if difference is None else f'{difference:+8.3f}'} "
                f"{report.get('correlation_peak', 0):7.3f} "
                f"{report.get('correlation_prominence', 0):7.3f} "
                f"{STATUS_LABEL.get(report['status'], report['status'])}"
            )

    if args.json:
        path = Path(args.json).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[json] {path}")
    return 0 if all(r["status"] in {"ok", "no_reference"} for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
