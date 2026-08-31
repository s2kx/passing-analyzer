#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect_view.py の軽量テスト。

実データもGPUも使わない。背景フローの測定をスタブへ差し替えて判定ロジックを検証し、
フロー計算そのものは合成動画(拡大／縮小するテクスチャ)で検証する。
`detect_overtaking.py` のサブプロセスもスタブ化する。

    .venv\\Scripts\\python.exe test_detect_view.py
"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import detect_view as dv
from detect_view import (
    DECISION_CONFIDENCE,
    MIN_MOVING_PAIRS,
    DetectViewError,
    SamplePlan,
    detect_view,
    detection_command_for_view,
    plan_samples,
    primary_fractions,
    read_event_rows,
    score_flow,
)


DUMMY_MP4 = struct.pack(">I", 32) + b"ftyp" + b"\x00" * 24


def make_videos(root: Path, names=("GX010731.MP4",)) -> list[Path]:
    paths = []
    for name in names:
        path = root / name
        path.write_bytes(DUMMY_MP4)
        paths.append(path)
    return paths


def flow_result(ratios: list[float], flow_px: float = 6.0, pairs: int = 600) -> dict:
    """measure_flow の戻り値を模した辞書。"""
    return {
        "pairs": pairs,
        "moving_pairs": len(ratios),
        "ratios": list(ratios),
        "flow_px": flow_px if ratios else 0.0,
        "flow_px_all": flow_px if ratios else 0.05,
    }


class StubFlow:
    """サンプル番号ごとに決め打ちのフロー測定結果を返すスタブ。"""

    def __init__(self, results=None, default=None, fail=()):
        self.results = results or {}
        self.default = default if default is not None else flow_result([])
        self.fail = set(fail)
        self.calls: list[tuple[str, float]] = []

    def measure_flow(self, video, start_s, seconds, max_moving_pairs=None):
        index = len(self.calls) + 1
        self.calls.append((Path(video).name, start_s))
        if index in self.fail:
            raise DetectViewError(f"サンプル{index}を測定できません")
        return self.results.get(index, self.default)


def run_with_stub(stub: StubFlow, videos: list[Path], work_dir: Path,
                  duration: float = 600.0, detection=None, **kwargs) -> dict:
    """スタブを差し込んで detect_view を実行し、必ず元へ戻す。"""
    saved = [
        ("probe_duration", dv.probe_duration),
        ("measure_flow", dv.measure_flow),
        ("detection_counts", dv.detection_counts),
    ]
    dv.probe_duration = lambda video: duration
    dv.measure_flow = stub.measure_flow
    if detection is not None:
        dv.detection_counts = detection
    try:
        return detect_view(videos, work_dir, progress=lambda message: None, **kwargs)
    finally:
        for name, value in saved:
            setattr(dv, name, value)


# ── 判定スコア ────────────────────────────────────────────────────────────────

def test_score_flow():
    """外向き成分の中央値の符号で方向を決め、確信度は「符号の確かさ×強さ」。"""
    # 実測値相当: 前向き +0.87 / 後ろ向き -0.81 が一貫して出る
    view, confidence = score_flow([0.87] * 120)
    assert view == "front" and confidence >= DECISION_CONFIDENCE, (view, confidence)
    view, confidence = score_flow([-0.81] * 120)
    assert view == "rear" and confidence >= DECISION_CONFIDENCE, (view, confidence)

    # カーブや段差で2割ほど逆向きが混ざっても、中央値が強ければ判定できる
    # (実測: 一致率0.80・中央値+0.74・319対の前向き動画)
    view, confidence = score_flow([0.74] * 255 + [-0.2] * 64)
    assert view == "front" and confidence >= DECISION_CONFIDENCE, (view, confidence)

    # 弱い信号・少ない対では確信度が上がらない
    assert score_flow([])[0] is None
    assert score_flow([0.2] * 5)[1] < DECISION_CONFIDENCE, score_flow([0.2] * 5)
    assert score_flow([0.3] * 70 + [-0.3] * 50)[1] < DECISION_CONFIDENCE
    print("  OK: 背景フローの外向き成分から方向と確信度を出す")


# ── サンプル位置 ──────────────────────────────────────────────────────────────

def test_sample_planning():
    """全長の20/50/80%を主サンプルにし、追加サンプルは別の時間帯にする。"""
    assert primary_fractions(3) == [0.2, 0.5, 0.8], primary_fractions(3)
    assert primary_fractions(1) == [0.5]

    videos = [Path("a.MP4"), Path("b.MP4")]
    plans = plan_samples(videos, [600.0, 600.0], samples=3, sample_seconds=60.0)
    primary = [p for p in plans if not p.extra]
    assert len(primary) == 3, plans
    # 20% = 240秒 → 1本目、50% = 600秒 → 2本目の先頭、80% = 960秒 → 2本目
    assert primary[0].video.name == "a.MP4" and abs(primary[0].start_s - 210.0) < 1.0
    assert primary[1].video.name == "b.MP4", primary[1]
    assert primary[2].video.name == "b.MP4" and primary[2].start_s > primary[1].start_s
    # どのサンプルも動画内に収まる
    for plan in plans:
        assert plan.start_s >= 0 and plan.start_s + plan.seconds <= 600.0 + 1e-6, plan
    assert [p for p in plans if p.extra], "追加サンプルが計画されていない"

    # 短い動画ではサンプル位置が重なるので1件に畳む
    short = plan_samples([Path("a.MP4")], [30.0], samples=3, sample_seconds=60.0)
    assert len(short) == 1 and short[0].seconds == 30.0, short
    print("  OK: サンプル位置の計画（連結タイムライン・短い動画）")


# ── 判定フロー ────────────────────────────────────────────────────────────────

def test_strong_flow_decides_after_first_sample():
    """外向きフローがはっきり出れば1サンプルで打ち切り、front を採用する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([0.87] * 120))
        result = run_with_stub(stub, videos, root / "work")

        assert result["view"] == "front", result
        assert result["confidence"] >= DECISION_CONFIDENCE, result
        detail = result["detail"]
        assert detail["samples_used"] == 1, detail
        assert detail["moving_pairs"] == 120, detail
        assert detail["radial_ratio"] == 0.87, detail
        assert len(stub.calls) == 1, stub.calls
        assert detail["method"] == "background_optical_flow", detail
        print("  OK: 外向きフローが強ければ1サンプルで front と確定する")


def test_inward_flow_is_detected_as_rear():
    """内向き(縮小)フローなら rear と判定する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([-0.81] * 120))
        result = run_with_stub(stub, videos, root / "work")
        assert result["view"] == "rear", result
        assert result["detail"]["radial_ratio"] == -0.81, result["detail"]
        print("  OK: 内向きフローを rear と判定する")


def test_stationary_samples_add_more_then_give_up():
    """自転車が停止していてフローが出ない場合は追加サンプル後に判定不能にする。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([]))
        result = run_with_stub(stub, videos, root / "work")

        assert result["view"] is None, result
        assert result["confidence"] == 0.0, result
        detail = result["detail"]
        # 主3サンプル + 追加サンプルをすべて試している
        assert detail["samples_used"] == detail["samples_planned"] > 3, detail
        assert detail["moving_pairs"] == 0, detail
        assert "停止" in detail["reason"], detail["reason"]
        assert detail["fallback_view"] == "front", detail
        assert all(s["radial_ratio"] is None for s in detail["samples"]), detail["samples"]
        print("  OK: 停止中ばかりなら追加サンプル後に判定不能を返す")


def test_later_sample_supplies_the_moving_section():
    """序盤が停止中でも、走行している時間帯が見つかれば判定できる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(results={3: flow_result([-0.8] * 120)}, default=flow_result([]))
        result = run_with_stub(stub, videos, root / "work")
        assert result["view"] == "rear", result
        assert result["detail"]["samples_used"] == 3, result["detail"]
        print("  OK: 走行している時間帯が見つかれば判定できる")


def test_unstable_flow_stays_undecided():
    """走行していても向きが安定しない場合は判定不能にする（誤判定より安全）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([0.3] * 7 + [-0.3] * 5))
        result = run_with_stub(stub, videos, root / "work")

        assert result["view"] is None, result
        detail = result["detail"]
        assert detail["moving_pairs"] >= MIN_MOVING_PAIRS, detail
        assert detail["leaning"] == "front", detail
        assert "安定しません" in detail["reason"], detail["reason"]
        print("  OK: フローの向きが安定しないときは判定不能")


def test_failed_sample_is_skipped_but_others_decide():
    """1サンプルの測定が失敗しても、残りのサンプルで判定を続ける。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([0.9] * 120), fail=(1,))
        result = run_with_stub(stub, videos, root / "work")

        assert result["view"] == "front", result
        detail = result["detail"]
        assert detail["samples_used"] == 1, detail          # 成功したのはサンプル2だけ
        assert len(detail["errors"]) == 1, detail["errors"]
        assert detail["samples"][0]["error"], detail["samples"]
        print("  OK: 1サンプルの失敗では止まらない")


def test_all_samples_failing_raises():
    """すべてのサンプルが失敗したらエラーにする（誤った既定値で進ませない）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(fail=(1, 2, 3, 4, 5, 6, 7))
        try:
            run_with_stub(stub, videos, root / "work")
        except DetectViewError as exc:
            assert "判定できるサンプルがありません" in str(exc), exc
        else:
            raise AssertionError("全サンプル失敗でも例外にならなかった")
        print("  OK: 全サンプル失敗は DetectViewError")


def test_missing_video_is_rejected():
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        try:
            detect_view([root / "none.MP4"], root / "work", progress=lambda m: None)
        except DetectViewError as exc:
            assert "見つかりません" in str(exc), exc
        else:
            raise AssertionError("存在しない動画でも例外にならなかった")
        print("  OK: 存在しない動画を弾く")


def test_samples_option_limits_work():
    """--samples 1 なら主サンプルは1箇所だけにする。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        stub = StubFlow(default=flow_result([]))
        result = run_with_stub(stub, videos, root / "work", samples=1)
        assert result["detail"]["samples_planned"] == 4, result["detail"]  # 主1 + 追加3
        print("  OK: samples オプションでサンプル数を絞れる")


# ── 参考値としての検出件数 ────────────────────────────────────────────────────

def test_detection_counts_are_recorded_but_never_decide():
    """検出件数は参考値。実データでは逆向き設定のほうが多くなることがある。

    260701/ID1(前向き)の60秒クリップ実測は front 1件 / rear 15件。件数で判定すると
    rear になってしまうため、件数は detail に残すだけで判定には使わない。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        videos = make_videos(root)
        calls: list = []

        def detection(plan, work_dir, timeout=None):
            calls.append(plan.index)
            return {"front": 1, "rear": 15}

        stub = StubFlow(default=flow_result([0.87] * 120))
        result = run_with_stub(stub, videos, root / "work", detection=detection,
                               with_detection=True)

        # フローは外向きなので、件数が rear に偏っていても front を採用する
        assert result["view"] == "front", result
        detail = result["detail"]
        assert detail["detection_counts"] == {"front": 1, "rear": 15}, detail
        assert calls == [1], calls
        assert "判定には使わない" in detail["detection_note"], detail
        print("  OK: 検出件数は参考値として残すだけで判定には使わない")


def test_detection_command():
    """参考値用の検出コマンドは本番と同じ条件で、クリップ・可視化は作らない。"""
    cmd = detection_command_for_view(Path("clip.mp4"), "rear", Path("O"))
    assert cmd[1].endswith("detect_overtaking.py"), cmd
    assert cmd[2] == "clip.mp4", cmd
    assert cmd[cmd.index("--view") + 1] == "rear", cmd
    assert cmd[cmd.index("--out") + 1] == "O", cmd
    assert cmd[cmd.index("--imgsz") + 1] == "640", cmd
    assert "--clip" not in cmd and "--overlay" not in cmd, cmd
    print("  OK: 参考値用の検出コマンドの組み立て")


def test_read_event_rows():
    with tempfile.TemporaryDirectory() as temp_value:
        path = Path(temp_value) / "overtaking_events.csv"
        assert read_event_rows(path) == []
        path.write_text(
            "event_id,class,side,t_start_s,t_end_s\n001,普通,right,1.0,3.0\n",
            encoding="utf-8-sig",
        )
        rows = read_event_rows(path)
        assert len(rows) == 1 and rows[0]["side"] == "right", rows
        print("  OK: overtaking_events.csv の件数読み取り")


# ── フロー計算そのもの（合成動画） ────────────────────────────────────────────

def write_zoom_video(path: Path, zoom_in: bool, frames: int = 150) -> bool:
    """拡大(前進する前向きカメラ)／縮小(後ろ向きカメラ)する合成動画を書く。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    rng = np.random.default_rng(0)
    texture = rng.integers(0, 255, (720, 1280), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (5, 5), 0)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (640, 360))
    if not writer.isOpened():
        return False
    height, width = texture.shape
    for index in range(frames):
        step = index if zoom_in else frames - 1 - index
        scale = 1.0 + 0.012 * step
        half_w, half_h = int(width / scale / 2), int(height / scale / 2)
        crop = texture[height // 2 - half_h:height // 2 + half_h,
                       width // 2 - half_w:width // 2 + half_w]
        frame = cv2.resize(crop, (640, 360))
        writer.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
    writer.release()
    return path.is_file() and path.stat().st_size > 0


def test_measure_flow_on_synthetic_video():
    """拡大する映像は外向き(front)、縮小する映像は内向き(rear)になる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        root = Path(temp_value)
        for zoom_in, expected in ((True, "front"), (False, "rear")):
            path = root / f"zoom_{'in' if zoom_in else 'out'}.avi"
            if not write_zoom_video(path, zoom_in):
                print("  SKIP: OpenCV の動画書き出しが使えないため合成動画テストを省略")
                return
            flow = dv.measure_flow(path, 0.0, 5.0)
            assert flow["moving_pairs"] >= 10, flow
            view, _ = score_flow(flow["ratios"])
            assert view == expected, (expected, view, flow["flow_px"],
                                      flow["ratios"][:5])
        print("  OK: 合成動画で拡大=front / 縮小=rear を確認")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for test in tests:
        test()
    print("[test] 全テスト成功")
