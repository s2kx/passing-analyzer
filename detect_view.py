#!/usr/bin/env python3
"""GoPro撮影方向(front / rear)の自動判定。

`detect_overtaking.py` の `--view` を人が指定しなくても済むようにするモジュール。
方向を取り違えると結果が壊滅的に変わる(実測: 260701/ID1 の前向き撮影を rear で
解析すると誤検知66件・取りこぼし8件。front なら40候補で正解12件をほぼ網羅した)ため、
全自動パイプラインでは検出の前にここで方向を決める。

原理: 背景オプティカルフローの放射方向
--------------------------------------
自転車が前進しているとき、

- **前向きカメラ**: 世界は消失点(FOE)から**外向きに湧き出す**ように流れる(拡大)
- **後ろ向きカメラ**: 世界は画面中央へ**吸い込まれる**ように流れる(縮小)

これは撮影方向そのものが決める幾何であり、交通量・道路種別・時間帯に依存しない。
特徴点を LK 法で追跡し、変位ベクトルの「画面中心から外向き成分 ÷ 変位の大きさ」の
中央値を取る。実測値:

    260701/ID1 GX010731.MP4 (前向き)  … +0.87 / +0.84 (外向き一致率 1.00 / 0.97)
    1.海野 gopro_後 GX010714.MP4 (後向き) … -0.81 (外向き一致率 0.00)

自転車が**停止している間はフローがほぼ0**(実測 0.09px)で符号が定まらないため、
背景フローが十分大きいフレーム対だけを採用し、足りなければ別の時間帯を追加サンプルする。

なぜイベント数の比較を主判定に使わないか
----------------------------------------
「front / rear 両方で `detect_overtaking.py` を回し、イベントが多い方を採用する」案は
実データで**逆の答えを出した**。260701/ID1(前向き)の60秒クリップで front 1件 / rear 15件、
動画全体でも rear 66件 / front 40件となる。後方設定の軌跡条件(中央から左端へ流れる)は
前向き映像の路駐車両・対向車にも当てはまってしまうため、**誤った方向のほうが
イベントが増えうる**。件数比較は `--with-detection` で参考値として記録できるが、
判定には使わない。

使い方
------
    python detect_view.py <動画...> [--samples 3] [--sample-seconds 60]
    python detect_view.py <動画...> --with-detection   # 検出件数も参考表示する
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from detect_overtaking import ffmpeg_executable
from web_gui import detection_python


APP_DIR = Path(__file__).resolve().parent
VIEWS = ("front", "rear")

DEFAULT_SAMPLES = 3
DEFAULT_SAMPLE_SECONDS = 60.0
#: 判定を確定させる確信度。これ未満なら追加サンプル、最後まで届かなければ判定不能。
DECISION_CONFIDENCE = 0.65
#: 主サンプルで決まらなかったときに追加する時間帯の最大数。
MAX_EXTRA_SAMPLES = 3
#: 追加サンプルの位置(全長に対する割合)。主サンプルと重なるものは自動的に除く。
EXTRA_FRACTIONS = (0.35, 0.65, 0.10, 0.90)
#: 判定不能時にパイプラインが仮定する方向。
FALLBACK_VIEW = "front"
#: サンプル位置がこれ以上離れていなければ「同じ時間帯」とみなす(秒)。
MIN_SAMPLE_GAP_SECONDS = 15.0

# ── フローの測定条件 ──────────────────────────────────────────────────────────
#: フロー計算時の横幅(px)。しきい値はこの幅を基準にしている。
FLOW_WIDTH = 480
#: フレーム対の時間間隔(秒)。0.1秒で走行中の背景変位が数px出る。
FLOW_PAIR_INTERVAL = 0.1
#: 自車が「走行中」と見なす背景変位の中央値(px)。
#: 実測(4動画): 走行中 3.9〜7.6px / 停止中 0.03〜0.18px と大きく離れているので、
#: その谷にあたる 2.0px を採る。1.0px まで下げると「ほぼ停止」の対が混ざり、
#: 放射成分が薄まって判定不能になる動画があった。
MIN_FLOW_PX = 2.0
#: 1フレーム対で放射成分を計算するのに必要な追跡点数。
MIN_TRACKED_POINTS = 20
#: 画面中心からこの割合より内側の点は、放射方向が不安定なので使わない。
CENTER_EXCLUSION = 0.12
#: 判定に必要な「走行中フレーム対」の総数。
MIN_MOVING_PAIRS = 30
#: 1サンプルで集める走行中フレーム対の上限(これに達したら次の時間帯へ)。
MAX_MOVING_PAIRS_PER_SAMPLE = 120


class DetectViewError(Exception):
    """撮影方向を判定するための前提が整っていない。"""


@dataclass
class SamplePlan:
    """1サンプル分の切り出し計画。"""
    index: int
    video: Path
    start_s: float
    seconds: float
    fraction: float
    extra: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "video": self.video.name,
            "start_s": round(self.start_s, 2),
            "seconds": round(self.seconds, 2),
            "position": f"{self.fraction * 100:.0f}%",
            "extra": self.extra,
        }


# ── 動画長の取得 ──────────────────────────────────────────────────────────────

def ffprobe_executable() -> str | None:
    """同梱 ffprobe を優先し、無ければ PATH 上の ffprobe を返す。"""
    names = ["ffprobe.exe", "ffprobe"] if sys.platform == "win32" else ["ffprobe"]
    for folder in (APP_DIR / "tools", APP_DIR):
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffprobe")


def _no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def probe_duration(video: Path) -> float | None:
    """動画の長さ(秒)を返す。ffprobe → OpenCV の順に試し、どちらも駄目なら None。"""
    ffprobe = ffprobe_executable()
    if ffprobe:
        cmd = [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video),
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, creationflags=_no_window()
            )
            value = float((completed.stdout or "").strip())
            if value > 0:
                return value
        except (OSError, ValueError):
            pass
    cv2 = _load_cv2(required=False)
    if cv2 is None:
        return None
    capture = cv2.VideoCapture(str(video))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    finally:
        capture.release()
    if fps > 0 and frames > 0:
        return float(frames / fps)
    return None


# ── サンプル位置の決定 ────────────────────────────────────────────────────────

def primary_fractions(samples: int) -> list[float]:
    """全長に対するサンプル位置。3本なら 20% / 50% / 80%。"""
    samples = max(1, int(samples))
    if samples == 1:
        return [0.5]
    return [0.2 + 0.6 * i / (samples - 1) for i in range(samples)]


def _locate(fraction: float, videos: list[Path], durations: list[float],
            sample_seconds: float) -> tuple[Path, float, float]:
    """連結タイムライン上の割合位置を (動画, 動画内の開始秒, 長さ) へ変換する。

    サンプルが動画をまたがないよう、開始位置は必ずその動画の中へ収める。
    """
    total = sum(durations)
    target = max(0.0, min(total, fraction * total))
    for video, duration in zip(videos, durations):
        if target < duration or video is videos[-1]:
            seconds = min(sample_seconds, duration)
            start = min(max(0.0, target - seconds / 2.0), max(0.0, duration - seconds))
            return video, start, seconds
        target -= duration
    raise AssertionError("到達しない")  # pragma: no cover


def plan_samples(videos: list[Path], durations: list[float], samples: int,
                 sample_seconds: float, extras: int = MAX_EXTRA_SAMPLES) -> list[SamplePlan]:
    """主サンプル + 追加サンプルの計画を作る(位置が重なるものは除く)。

    戻り値の ``extra=False`` が主サンプル、``extra=True`` は「決着が付かなかった
    ときだけ使う」追加サンプル。動画が短くサンプルを分けられない場合は1件だけ返す。
    """
    if not videos or not durations or len(videos) != len(durations):
        raise DetectViewError("サンプル位置を決められません(動画長が不明です)")
    total = sum(durations)
    if total <= 0:
        raise DetectViewError("動画の長さが 0 秒です")

    plans: list[SamplePlan] = []
    used: list[tuple[Path, float]] = []

    def add(fraction: float, extra: bool) -> None:
        video, start, seconds = _locate(fraction, videos, durations, sample_seconds)
        for other_video, other_start in used:
            if other_video == video and abs(other_start - start) < MIN_SAMPLE_GAP_SECONDS:
                return
        used.append((video, start))
        plans.append(SamplePlan(len(plans) + 1, video, start, seconds, fraction, extra))

    for fraction in primary_fractions(samples):
        add(fraction, False)
    added = 0
    for fraction in EXTRA_FRACTIONS:
        if added >= max(0, int(extras)):
            break
        before = len(plans)
        add(fraction, True)
        added += len(plans) - before
    return plans


# ── 主判定: 背景オプティカルフローの放射方向 ──────────────────────────────────

def _load_cv2(required: bool = True):
    try:
        import cv2
    except ImportError as exc:
        if required:
            raise DetectViewError(
                "opencv-python が見つかりません。撮影方向を自動判定できません"
            ) from exc
        return None
    return cv2


def measure_flow(video: Path, start_s: float, seconds: float,
                 max_moving_pairs: int = MAX_MOVING_PAIRS_PER_SAMPLE) -> dict:
    """1サンプル区間の背景フローを測り、外向き成分の統計を返す。

    Returns:
        ``{"pairs": 全フレーム対数, "moving_pairs": 走行中の対数,
        "ratios": [外向き成分/変位量, ...], "flow_px": 走行中フレーム対の変位中央値,
        "flow_px_all": 全フレーム対の変位中央値}``
        ``ratios`` は +1 に近いほど外向き(前向きカメラ)、-1 に近いほど内向き(後ろ向き)。
    """
    cv2 = _load_cv2()
    import numpy as np

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise DetectViewError(f"動画を開けません: {video.name}")
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        if start_s > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
        step = max(1, int(round(fps * FLOW_PAIR_INTERVAL)))
        limit = int(seconds * fps)

        previous = None
        ratios: list[float] = []
        moving_magnitudes: list[float] = []
        magnitudes: list[float] = []
        pairs = 0
        read = 0
        while read < limit and len(ratios) < max_moving_pairs:
            ok, frame = capture.read()
            if not ok:
                break
            read += 1
            if (read - 1) % step:
                continue
            height, width = frame.shape[:2]
            small = cv2.resize(frame, (FLOW_WIDTH, max(1, round(height * FLOW_WIDTH / width))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if previous is not None:
                pairs += 1
                measurement = _pair_radial_ratio(cv2, np, previous, gray)
                if measurement is not None:
                    ratio, magnitude = measurement
                    magnitudes.append(magnitude)
                    if magnitude >= MIN_FLOW_PX:
                        ratios.append(ratio)
                        moving_magnitudes.append(magnitude)
            previous = gray
    finally:
        capture.release()

    return {
        "pairs": pairs,
        "moving_pairs": len(ratios),
        "ratios": ratios,
        "flow_px": round(statistics.median(moving_magnitudes), 2) if moving_magnitudes else 0.0,
        "flow_px_all": round(statistics.median(magnitudes), 2) if magnitudes else 0.0,
    }


def _pair_radial_ratio(cv2, np, previous, gray) -> tuple[float, float] | None:
    """フレーム対1組の「外向き成分 ÷ 変位量」と変位量の中央値を返す。"""
    points = cv2.goodFeaturesToTrack(
        previous, maxCorners=400, qualityLevel=0.01, minDistance=8, blockSize=7
    )
    if points is None or len(points) < MIN_TRACKED_POINTS:
        return None
    moved, status, _ = cv2.calcOpticalFlowPyrLK(
        previous, gray, points, None, winSize=(21, 21), maxLevel=3
    )
    if moved is None or status is None:
        return None
    keep = status.reshape(-1).astype(bool)
    start = points.reshape(-1, 2)[keep]
    end = moved.reshape(-1, 2)[keep]
    if len(start) < MIN_TRACKED_POINTS:
        return None

    center = np.array([gray.shape[1] / 2.0, gray.shape[0] / 2.0])
    offset = start - center
    distance = np.linalg.norm(offset, axis=1)
    # 画面中心付近は「外向き」の向きが定まらないので除く
    outer = distance > CENTER_EXCLUSION * gray.shape[1]
    start, end, offset, distance = start[outer], end[outer], offset[outer], distance[outer]
    if len(start) < MIN_TRACKED_POINTS:
        return None

    delta = end - start
    magnitude = float(np.median(np.linalg.norm(delta, axis=1)))
    if magnitude <= 0:
        return None
    radial = float(np.median(np.einsum("ij,ij->i", delta, offset / distance[:, None])))
    return radial / magnitude, magnitude


def score_flow(ratios: list[float]) -> tuple[str | None, float]:
    """外向き成分の一覧から (採用する方向, 確信度) を返す。

    確信度 = 「中央値の符号が確かである度合い」×「放射成分の強さ」。

    前者は符号の多数決を二項検定とみなした z 値 ``(p-0.5)*2*sqrt(n)`` を使う。
    カーブ・段差・停止直前などでは個々のフレーム対が逆向きに出ることがあるので、
    符号の一致率そのものを掛けると強い信号でも確信度が落ちてしまう。実測でも
    一致率0.80・中央値+0.74(319対)の前向き動画を取りこぼした。対数の多い測定では
    一致率0.8でも中央値の符号はまず動かない、という性質を z 値で表す。
    """
    if not ratios:
        return None, 0.0
    median = statistics.median(ratios)
    if median == 0:
        return None, 0.0
    view = "front" if median > 0 else "rear"
    agreement = sum(1 for r in ratios if (r > 0) == (median > 0)) / len(ratios)
    z_score = (agreement - 0.5) * 2 * math.sqrt(len(ratios))
    certainty = min(1.0, max(0.0, z_score) / 4.0)
    strength = min(1.0, abs(median) / 0.5)
    return view, round(min(0.99, certainty * strength), 3)


# ── 参考値: front / rear 両方で検出させた件数 ─────────────────────────────────

def _cut_clip(plan: SamplePlan, work_dir: Path) -> Path:
    """``-c copy`` で短いクリップを切り出す(再エンコードしない)。"""
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        raise DetectViewError(
            "ffmpeg が見つかりません。tools/ffmpeg.exe を用意してください"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"sample_{plan.index:02d}.mp4"
    cmd = [
        ffmpeg, "-y", "-ss", f"{plan.start_s:.2f}", "-i", str(plan.video),
        "-t", f"{plan.seconds:.2f}", "-map", "0:v:0", "-c", "copy",
        "-avoid_negative_ts", "make_zero", str(out),
    ]
    completed = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, errors="replace", creationflags=_no_window(),
    )
    if completed.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = (completed.stderr or "").strip().splitlines()[-3:]
        raise DetectViewError(
            f"クリップを切り出せません({plan.video.name} {plan.start_s:.0f}s): "
            + " / ".join(tail)
        )
    return out


def detection_command_for_view(clip: Path, view: str, out_dir: Path) -> list[str]:
    """参考値用の検出コマンド。クリップ・可視化は作らず、検出条件は本番と揃える。"""
    return [
        str(detection_python()), str(APP_DIR / "detect_overtaking.py"), str(clip),
        "--view", view, "--out", str(out_dir),
        "--imgsz", "640", "--batch", "32",
        "--flow-stride", "3", "--prefetch-batches", "2",
        "--decoder", "auto",
    ]


def read_event_rows(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _run_detection(clip: Path, view: str, out_dir: Path,
                   timeout: float | None = None) -> list[dict]:
    """`detect_overtaking.py` をサブプロセス実行し、検出イベントの行を返す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = detection_command_for_view(clip, view, out_dir)
    env = os.environ.copy()
    yolo_config_dir = APP_DIR / ".ultralytics"
    yolo_config_dir.mkdir(exist_ok=True)
    env["YOLO_CONFIG_DIR"] = str(yolo_config_dir)
    completed = subprocess.run(
        cmd, cwd=APP_DIR, env=env, capture_output=True,
        text=True, errors="replace", timeout=timeout,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
        raise DetectViewError(
            f"detect_overtaking.py({view}) が失敗しました"
            f"(終了コード {completed.returncode}): " + " / ".join(tail)
        )
    return read_event_rows(out_dir / "overtaking_events.csv")


def detection_counts(plan: SamplePlan, work_dir: Path,
                     timeout: float | None = None) -> dict:
    """1サンプルを front / rear 両方で検出させ、件数を返す(参考値)。"""
    clip = _cut_clip(plan, work_dir)
    try:
        counts = {}
        for view in VIEWS:
            rows = _run_detection(clip, view, work_dir / f"sample_{plan.index:02d}_{view}",
                                  timeout=timeout)
            counts[view] = len(rows)
        return counts
    finally:
        try:
            clip.unlink()
        except OSError:
            pass


# ── 本体 ──────────────────────────────────────────────────────────────────────

def _reason(view: str | None, confidence: float, ratio: float | None,
            moving_pairs: int, used: int) -> str:
    if view is not None:
        direction = "外向き(拡大)" if view == "front" else "内向き(縮小)"
        return (
            f"{used}サンプル・走行中{moving_pairs}フレーム対で、背景フローは"
            f"{direction}(放射成分 {ratio:+.2f})。{view} と判定しました"
            f"(確信度 {confidence:.2f})"
        )
    if moving_pairs < MIN_MOVING_PAIRS:
        return (
            f"{used}サンプルを調べましたが、自転車が走行しているフレームが"
            f"{moving_pairs}対しかありませんでした(必要 {MIN_MOVING_PAIRS}対)。"
            "停止中は背景が動かず撮影方向を判定できません"
        )
    return (
        f"{used}サンプル・走行中{moving_pairs}フレーム対を調べましたが、背景フローの"
        f"向きが安定しません(放射成分 {ratio:+.2f} / 確信度 {confidence:.2f})"
    )


def detect_view(videos: list[Path], work_dir: Path, *, samples: int = DEFAULT_SAMPLES,
                sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
                with_detection: bool = False, timeout: float | None = None,
                progress=None) -> dict:
    """GoPro動画の撮影方向を自動判定する。

    動画の複数箇所から背景オプティカルフローを測り、消失点から外向き(前向き)か
    内向き(後ろ向き)かで判定する。決着が付いた時点で打ち切り、自転車が停止していて
    測れない場合は別の時間帯を追加サンプルする。

    Args:
        videos: 解析対象の動画(複数本は1本の連結タイムラインとして扱う)
        work_dir: 途中結果を置く作業フォルダ(``with_detection`` のときだけ使う)
        samples: 主サンプル数
        sample_seconds: 1サンプルの長さ(秒)
        with_detection: front / rear 両方で `detect_overtaking.py` を回し、
            イベント件数を参考値として ``detail`` に記録する(判定には使わない)
        timeout: 参考値の検出1回あたりのタイムアウト秒
        progress: 進捗メッセージを受け取る callable(既定は print)

    Returns:
        ``{"view": "front"|"rear"|None, "confidence": float, "detail": {...}}``
        ``view`` が None のときは判定不能。呼び出し側は ``FALLBACK_VIEW`` を
        仮定しつつ人手確認を促すこと。
    """
    say = progress if progress is not None else (lambda message: print(message, flush=True))
    videos = [Path(v) for v in videos]
    if not videos:
        raise DetectViewError("動画が指定されていません")
    missing = [str(v) for v in videos if not v.is_file()]
    if missing:
        raise DetectViewError("動画が見つかりません: " + ", ".join(missing))

    started = time.time()
    durations: list[float] = []
    for video in videos:
        duration = probe_duration(video)
        if duration is None or duration <= 0:
            raise DetectViewError(f"動画の長さを取得できません: {video.name}")
        durations.append(duration)

    work_dir = Path(work_dir)
    plans = plan_samples(videos, durations, samples, sample_seconds)
    primary = [plan for plan in plans if not plan.extra]
    say(
        f"[view] 撮影方向を自動判定します(全長 {sum(durations):.0f}秒 / "
        f"サンプル {len(primary)}箇所 × {sample_seconds:.0f}秒 / 背景フロー方式)"
    )

    ratios: list[float] = []
    sample_details: list[dict] = []
    errors: list[str] = []
    view: str | None = None
    confidence = 0.0

    for plan in plans:
        if plan.extra and _decided(ratios):
            break
        entry = plan.to_dict()
        try:
            flow = measure_flow(plan.video, plan.start_s, plan.seconds)
        except DetectViewError as exc:
            entry["error"] = str(exc)
            errors.append(str(exc))
            sample_details.append(entry)
            continue

        entry.update({
            "pairs": flow["pairs"],
            "moving_pairs": flow["moving_pairs"],
            "flow_px": flow["flow_px"],
            "flow_px_all": flow["flow_px_all"],
        })
        if flow["ratios"]:
            entry["radial_ratio"] = round(statistics.median(flow["ratios"]), 3)
            ratios.extend(flow["ratios"])
        else:
            entry["radial_ratio"] = None
            entry["note"] = "自転車が停止していて背景が動きません"

        if with_detection:
            try:
                entry["detection_counts"] = detection_counts(plan, work_dir, timeout=timeout)
            except (DetectViewError, subprocess.TimeoutExpired) as exc:
                entry["detection_error"] = str(exc)

        sample_details.append(entry)
        say(
            f"[view] サンプル{plan.index} ({plan.video.name} "
            f"{plan.start_s:.0f}s+{plan.seconds:.0f}s): 走行中 {flow['moving_pairs']}対 / "
            f"背景変位 {flow['flow_px']}px / 放射成分 "
            + ("なし" if entry["radial_ratio"] is None else f"{entry['radial_ratio']:+.2f}")
        )

        if _decided(ratios):
            break

    used = len([entry for entry in sample_details if "error" not in entry])
    if used == 0:
        raise DetectViewError(
            "撮影方向を判定できるサンプルがありませんでした: "
            + (errors[-1] if errors else "原因不明")
        )
    view, confidence = score_flow(ratios)
    if not _decided(ratios):
        view = None

    median_ratio = round(statistics.median(ratios), 3) if ratios else None
    detail = {
        "method": "background_optical_flow",
        "radial_ratio": median_ratio,
        "moving_pairs": len(ratios),
        "min_moving_pairs": MIN_MOVING_PAIRS,
        "samples_used": used,
        "samples_planned": len(plans),
        "sample_seconds": sample_seconds,
        "decision_confidence": DECISION_CONFIDENCE,
        "samples": sample_details,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 1),
        "videos": [video.name for video in videos],
        "total_seconds": round(sum(durations), 1),
    }
    if with_detection:
        detail["detection_counts"] = {
            candidate: sum(
                entry.get("detection_counts", {}).get(candidate, 0)
                for entry in sample_details
            )
            for candidate in VIEWS
        }
        detail["detection_note"] = (
            "参考値。逆向き設定でもイベントが出る(むしろ増える)ことがあるため判定には使わない"
        )
    if view is None:
        detail["leaning"] = score_flow(ratios)[0]
        detail["fallback_view"] = FALLBACK_VIEW
    detail["reason"] = _reason(
        view, confidence, median_ratio if median_ratio is not None else 0.0,
        len(ratios), used,
    )
    say(f"[view] {detail['reason']}")
    return {"view": view, "confidence": confidence if view else 0.0, "detail": detail}


def _decided(ratios: list[float]) -> bool:
    """今の測定量で判定を確定してよいか。"""
    if len(ratios) < MIN_MOVING_PAIRS:
        return False
    return score_flow(ratios)[1] >= DECISION_CONFIDENCE


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GoPro動画の撮影方向(front/rear)を自動判定する",
    )
    parser.add_argument("videos", nargs="+", help="GoPro動画(複数指定すると連結タイムラインとして扱う)")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"サンプルする時間帯の数(既定: {DEFAULT_SAMPLES})")
    parser.add_argument("--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS,
                        help=f"1サンプルの長さ(秒)(既定: {DEFAULT_SAMPLE_SECONDS:.0f})")
    parser.add_argument("--with-detection", action="store_true",
                        help="front/rear 両方で検出も行い、件数を参考表示する(GPUを使う)")
    parser.add_argument("--work-dir", default="", help="作業フォルダ(既定: 一時フォルダ)")
    parser.add_argument("--json", action="store_true", help="判定結果をJSONで出力する")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    videos = [Path(v).expanduser().resolve() for v in args.videos]

    temp_dir: tempfile.TemporaryDirectory | None = None
    if args.work_dir:
        work_dir = Path(args.work_dir).expanduser().resolve()
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="detect_view_")
        work_dir = Path(temp_dir.name)
    try:
        # --json では進捗を stderr へ逃がし、stdout をJSONだけにする
        progress = None
        if args.json:
            progress = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
        result = detect_view(
            videos, work_dir, samples=args.samples,
            sample_seconds=args.sample_seconds, with_detection=args.with_detection,
            progress=progress,
        )
    except DetectViewError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    detail = result["detail"]
    print()
    print("=== 撮影方向の自動判定 ===")
    print(f"動画        : {', '.join(detail['videos'])}({detail['total_seconds']}秒)")
    for entry in detail["samples"]:
        if "error" in entry:
            print(f"  サンプル{entry['index']} {entry['video']} {entry['position']}: "
                  f"失敗 {entry['error']}")
            continue
        ratio = entry["radial_ratio"]
        line = (f"  サンプル{entry['index']} {entry['video']} {entry['position']} "
                f"({entry['start_s']}s +{entry['seconds']}s): "
                f"走行中 {entry['moving_pairs']}対 / 背景変位 {entry['flow_px']}px / "
                + ("放射成分なし(停止中)" if ratio is None else f"放射成分 {ratio:+.2f}"))
        counts = entry.get("detection_counts")
        if counts:
            line += f" / 参考: 検出 front {counts['front']}件・rear {counts['rear']}件"
        print(line)
    ratio = detail["radial_ratio"]
    print(f"背景フロー  : 放射成分の中央値 "
          f"{'測定不能' if ratio is None else f'{ratio:+.3f}'}"
          f"(+が外向き=front / -が内向き=rear) / 走行中 {detail['moving_pairs']}対")
    if detail.get("detection_counts"):
        print(f"参考(検出)  : front {detail['detection_counts']['front']}件 / "
              f"rear {detail['detection_counts']['rear']}件 … {detail['detection_note']}")
    print(f"所要時間    : {detail['elapsed_seconds']}秒")
    if result["view"]:
        print(f"判定        : {result['view']}(確信度 {result['confidence']:.2f})")
        print(f"次のコマンド: --view {result['view']}")
    else:
        print("判定        : 判定不能")
        print(f"理由        : {detail['reason']}")
        print("対処        : 動画を目視で確認し --view front / --view rear を明示してください"
              f"(暫定では {FALLBACK_VIEW} を仮定します)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
