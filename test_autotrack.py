#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追い越し車両の自動追跡 (``lidar_autotrack``) の単体テスト。

PCAP を使わず、numpy で合成した点群（地面平面 + 静止障害物 + 通過車両）で
地面除去・クラスタリング・トラッキング・同定・矩形生成を検証する。

    .venv\\Scripts\\python.exe test_autotrack.py
"""

import numpy as np

from lidar_autotrack import (
    MIN_TRACK_FRAMES, Cluster, Tracker, build_tracks, cluster_points, detect_clusters,
    estimate_forward_sign, estimate_ground_plane, estimate_ground_z, filter_points,
    ground_height, score_track, select_overtaking, summarize_track,
    track_overtaking, track_to_frames,
)


SENSOR_HEIGHT = 1.30      # センサ取付高 (m)。地面は z = -1.30 に現れる。
GROUND_Z = -SENSOR_HEIGHT
BIKE_SPEED = 5.0          # 自転車速度 (m/s)。静止物はこの速度で後退して見える。
FRAME_INTERVAL = 0.1      # VLP-16 は約 10Hz


def _rng(seed: int = 20260701) -> np.random.Generator:
    return np.random.default_rng(seed)


def ground_points(rng, count: int = 6000, span_x: float = 9.0, span_y: float = 32.0) -> np.ndarray:
    """平坦な路面を模した点群を作る。"""
    x = rng.uniform(-span_x, span_x, count)
    y = rng.uniform(-span_y, span_y, count)
    z = GROUND_Z + rng.normal(0.0, 0.015, count)
    return np.column_stack([x, y, z])


def box_points(rng, center_x: float, center_y: float, length: float, width: float,
               height: float, count: int = 260) -> np.ndarray:
    """直方体の車両・障害物を模した点群を作る。"""
    x = rng.uniform(center_x - width / 2, center_x + width / 2, count)
    y = rng.uniform(center_y - length / 2, center_y + length / 2, count)
    z = rng.uniform(GROUND_Z + 0.3, GROUND_Z + height, count)
    return np.column_stack([x, y, z])


def scene_frame(rng, frame: int, vehicle: bool = True, flip: bool = False,
                oncoming: bool = False) -> np.ndarray:
    """1 フレーム分の合成点群を作る。

    - 地面平面（z = -1.30）
    - 静止障害物 3 つ（路駐車・壁・電柱）。自転車速度で後退して見える。
    - 通過車両（``vehicle=True``）。自転車の右 1.8m を相対 8m/s で追い越す。
    - ``oncoming=True`` では対向車（後退方向へ速く動く物体）を追加する。
    - ``flip=True`` では XY を 180 度回転し、センサが逆向きに取り付けられた場合を模す。
    """
    t = frame * FRAME_INTERVAL
    clouds = [ground_points(rng)]
    static_offset = -BIKE_SPEED * t
    for base_x, base_y, length, width, height in (
        (6.2, 22.0 + static_offset, 4.4, 1.7, 1.4),    # 路駐車
        (-5.5, 10.0 + static_offset, 6.0, 0.6, 2.4),   # 壁
        (5.8, -4.0 + static_offset, 0.4, 0.4, 3.0),    # 電柱
    ):
        clouds.append(box_points(rng, base_x, base_y, length, width, height))
    if vehicle:
        clouds.append(box_points(rng, 1.8, -20.0 + 8.0 * t, 4.6, 1.8, 1.5))
    if oncoming:
        clouds.append(box_points(rng, -2.5, 25.0 - 18.0 * t, 4.4, 1.8, 1.5))
    points = np.vstack(clouds)
    if flip:
        points = points * np.array([-1.0, -1.0, 1.0])
    return points


def scene_frames(count: int = 45, **options) -> list[tuple[int, float, list[Cluster]]]:
    """連続フレームを合成し、``(フレーム番号, 時刻, クラスタ列)`` を返す。"""
    rng = _rng()
    frames = []
    for frame in range(count):
        points = scene_frame(rng, frame, **options)
        timestamp = frame * FRAME_INTERVAL
        frames.append((frame, timestamp, detect_clusters(points, frame=frame, timestamp=timestamp)))
    return frames


class FakePcap:
    """``VelodynePcap`` の最小互換スタブ（合成点群を返す）。"""

    class _Frame:
        def __init__(self, timestamp: float):
            self.timestamp = timestamp

    def __init__(self, count: int = 45, **options):
        self.frames = [self._Frame(i * FRAME_INTERVAL) for i in range(count)]
        self._options = options
        self._rng = _rng()

    def decode_frame(self, index: int):
        points = scene_frame(self._rng, index, **self._options)
        # 実機と同じ (x, y, z, intensity, laser_id) のタプル列で返す
        return [(float(x), float(y), float(z), 12, index % 16) for x, y, z in points]


# --- 地面除去 -----------------------------------------------------------------
def test_ground_height_is_estimated():
    """近傍点の z 分布から取付高（地面高）を推定できる。"""
    points = scene_frame(_rng(), 0)
    ground = estimate_ground_z(points)
    assert ground is not None and abs(ground - GROUND_Z) < 0.05, ground
    print(f"  OK: 地面高を推定 ({ground:.3f}m / 真値 {GROUND_Z:.3f}m)")


def test_ground_points_are_removed():
    """地面点が消え、車両点が残る。"""
    points = scene_frame(_rng(), 0)
    kept = filter_points(points)
    assert len(kept) < len(points) * 0.35, (len(kept), len(points))
    assert kept[:, 2].min() > GROUND_Z + 0.15
    assert len(kept) > 500, len(kept)
    print(f"  OK: 地面除去で {len(points)}点 -> {len(kept)}点")


def test_ground_plane_follows_tilt():
    """自転車の傾き・路面のカントで傾いた地面を平面として推定する。"""
    rng = _rng()
    points = scene_frame(rng, 0)
    tilt = 0.06 * points[:, 0] - 0.02 * points[:, 1]
    points = points + np.column_stack([np.zeros(len(points))] * 2 + [tilt])
    plane = estimate_ground_plane(points)
    assert abs(plane[0] - GROUND_Z) < 0.08, plane
    assert abs(plane[1] - 0.06) < 0.02 and abs(plane[2] + 0.02) < 0.02, plane
    kept = filter_points(points, plane=plane)
    assert (kept[:, 2] - ground_height(plane, kept[:, 0], kept[:, 1])).min() > 0.15
    assert len(kept) > 500, len(kept)
    print(f"  OK: 傾いた地面を平面 z={plane[0]:+.2f}{plane[1]:+.3f}X{plane[2]:+.3f}Y として推定")


def test_horizontal_beams_do_not_fool_ground_estimate():
    """水平付近のビームが作る z≒0 の点の塊を地面と誤認しない。

    VLP-16 の -1 度・+1 度のビームは遠方の壁や車体に当たり、``z≒0`` に地面より
    多くの点を作ることがある。仰角で地面候補を絞るため、影響を受けない。
    """
    rng = _rng()
    radius = rng.uniform(5.0, 20.0, 8000)
    azimuth = rng.uniform(-np.pi, np.pi, 8000)
    plateau = np.column_stack([
        radius * np.sin(azimuth), radius * np.cos(azimuth), rng.normal(0.0, 0.12, 8000),
    ])
    points = np.vstack([scene_frame(rng, 0), plateau])
    ground = estimate_ground_z(points)
    assert abs(ground - GROUND_Z) < 0.10, ground
    print(f"  OK: z≒0 の点が地面点より多くても地面高 {ground:.2f}m を維持")


def test_roi_limits_range():
    """注目領域の外（遠方・高所）の点を捨てる。"""
    rng = _rng()
    far = np.column_stack([
        rng.uniform(20.0, 40.0, 300), rng.uniform(40.0, 80.0, 300),
        rng.uniform(GROUND_Z + 5.0, GROUND_Z + 9.0, 300),
    ])
    points = np.vstack([scene_frame(rng, 0), far])
    kept = filter_points(points)
    assert np.abs(kept[:, 0]).max() <= 8.0 and np.abs(kept[:, 1]).max() <= 30.0
    assert kept[:, 2].max() < GROUND_Z + 3.0
    print("  OK: |X|<=8m・|Y|<=30m・地面+3m以下へ限定")


def test_ground_estimate_without_points_is_none():
    """点が無い場合は地面高を推定せず None を返す。"""
    assert estimate_ground_z(np.zeros((0, 3))) is None
    assert len(filter_points([])) == 0
    print("  OK: 空の点群でも例外にならない")


# --- クラスタリング -----------------------------------------------------------
def test_clustering_separates_objects():
    """地面除去後の点群を物体ごとのクラスタに分ける。"""
    clusters = detect_clusters(scene_frame(_rng(), 0), frame=0, timestamp=0.0)
    assert len(clusters) == 4, [round(c.center_y, 1) for c in clusters]
    vehicle = min(clusters, key=lambda c: abs(c.center_y + 20.0))
    assert abs(vehicle.center_x - 1.8) < 0.2 and abs(vehicle.center_y + 20.0) < 0.3
    assert 4.0 < vehicle.length < 5.4 and 1.4 < vehicle.width < 2.4
    print(f"  OK: 4物体を分離し、車両の外接矩形は {vehicle.length:.1f}m x {vehicle.width:.1f}m")


def test_small_clusters_are_dropped():
    """点数の少ないかたまりはノイズとして捨てる。"""
    rng = _rng()
    noise = np.column_stack([
        rng.uniform(-0.1, 0.1, 3) + 7.0, rng.uniform(-0.1, 0.1, 3),
        np.full(3, GROUND_Z + 1.0),
    ])
    clusters = cluster_points(noise, min_points=6)
    assert clusters == []
    assert len(cluster_points(noise, min_points=2)) == 1
    print("  OK: 最小点数でノイズを除去")


def test_cluster_bounds_have_margin():
    """外接矩形にマージンを付けられる。"""
    cluster = Cluster(0, 0.0, 1.0, 2.0, 0.0, 2.0, 1.0, 3.0, 40)
    bounds = cluster.bounds(margin=0.3)
    assert bounds == {"min_x": -0.3, "max_x": 2.3, "min_y": 0.7, "max_y": 3.3}
    small = Cluster(0, 0.0, 0.0, 0.0, -0.1, 0.1, -0.1, 0.1, 40).bounds(margin=0.0, min_size=1.0)
    assert abs(small["max_x"] - small["min_x"] - 1.0) < 1e-6
    print("  OK: マージンと最小サイズを矩形へ反映")


# --- トラッキング -------------------------------------------------------------
def test_tracking_follows_vehicle():
    """通過車両を全フレームにわたり 1 本のトラックとして追える。"""
    frames = scene_frames(45)
    tracks = build_tracks(frames)
    long_tracks = [t for t in tracks if len(t.clusters) >= 40]
    assert len(long_tracks) == 4, [len(t.clusters) for t in tracks]
    vehicle = max(long_tracks, key=lambda t: summarize_track(t).speed_y)
    summary = summarize_track(vehicle)
    assert summary.frames == 45 and summary.span == 45
    assert abs(summary.speed_y - 8.0) < 0.3, summary.speed_y
    assert summary.monotonic > 0.95 and summary.passed_zone
    print(f"  OK: 45フレーム連続追跡、Y速度 {summary.speed_y:+.2f}m/s")


def test_tracker_bridges_short_loss():
    """数フレームのロストを跨いで同じトラックを維持する。"""
    frames = [(f, t, c) for f, t, c in scene_frames(30)]
    for index in (10, 11):
        frames[index] = (frames[index][0], frames[index][1],
                         [c for c in frames[index][2] if c.center_x < 1.0])
    tracks = build_tracks(frames)
    vehicle = max(tracks, key=lambda t: summarize_track(t).speed_y)
    summary = summarize_track(vehicle)
    assert summary.frames == 28 and summary.span == 30
    print("  OK: 2フレームのロストを跨いで追跡継続")


def test_static_objects_move_backward():
    """静止物は自転車速度で後退して見え、前進方向の符号を決められる。"""
    summaries = [summarize_track(t) for t in build_tracks(scene_frames(45))]
    forward, ego = estimate_forward_sign(summaries)
    assert forward == 1 and abs(ego + BIKE_SPEED) < 0.3, (forward, ego)
    print(f"  OK: 静止物の見かけ速度 {ego:+.2f}m/s から前進方向 +Y を推定")


def test_forward_sign_follows_flipped_axis():
    """センサが逆向きでも、静止物の動きから前進方向を反転して推定する。"""
    summaries = [summarize_track(t) for t in build_tracks(scene_frames(45, flip=True))]
    forward, ego = estimate_forward_sign(summaries)
    assert forward == -1 and abs(ego - BIKE_SPEED) < 0.3, (forward, ego)
    print(f"  OK: 反転座標系でも前進方向 -Y を推定 ({ego:+.2f}m/s)")


# --- 追い越し車両の同定 -------------------------------------------------------
def test_selects_passing_vehicle():
    """静止物ではなく通過車両を選ぶ。"""
    result = select_overtaking(build_tracks(scene_frames(45)), side="right")
    summary = result["summary"]
    assert summary is not None
    assert abs(summary.median_x - 1.8) < 0.3, summary.median_x
    assert summary.speed_y > 7.0 and summary.passed_zone
    assert result["score"] > 0.8, result["score"]
    assert len(result["candidates"]) == 1, len(result["candidates"])
    print(f"  OK: 通過車両を1本だけ選択 (confidence {result['score']:.3f})")


def test_selection_survives_flipped_axis():
    """センサ取付向きが逆でも同じ車両を選べる（符号は警告のみ）。"""
    result = select_overtaking(build_tracks(scene_frames(45, flip=True)), side="right")
    summary = result["summary"]
    assert summary is not None and summary.speed_y < -7.0
    assert result["score"] > 0.7, result["score"]
    assert any("X 符号" in w for w in result["warnings"])
    print(f"  OK: 反転座標系でも検出し、符号不一致を警告 (confidence {result['score']:.3f})")


def test_static_scene_has_no_candidate():
    """静止物だけの場面では候補を返さない。"""
    result = select_overtaking(build_tracks(scene_frames(45, vehicle=False)), side="right")
    assert result["summary"] is None and result["candidates"] == []
    print("  OK: 静止物だけの場面では候補なし")


def test_oncoming_vehicle_is_rejected():
    """対向車は Y の進行方向が逆になるため除外する。"""
    frames = scene_frames(45, vehicle=False, oncoming=True)
    tracks = build_tracks(frames)
    summaries = [summarize_track(t) for t in tracks]
    forward, ego = estimate_forward_sign(summaries)
    oncoming = min(summaries, key=lambda s: s.speed_y)
    score, reasons = score_track(oncoming, forward, ego, "left")
    assert score == 0.0 and any("相対速度" in r for r in reasons)
    assert select_overtaking(tracks, side="left")["summary"] is None
    print("  OK: 対向車を相対速度の向きで除外")


def test_parked_car_is_rejected():
    """路駐車は静止物基準の速度が出ないため除外する。"""
    summaries = [summarize_track(t) for t in build_tracks(scene_frames(45))]
    forward, ego = estimate_forward_sign(summaries)
    parked = min(summaries, key=lambda s: abs(s.speed_y + BIKE_SPEED))
    score, reasons = score_track(parked, forward, ego, "right")
    assert score == 0.0 and any("静止物" in r for r in reasons)
    print("  OK: 路駐車を静止物として除外")


def test_vehicle_before_passing_zone_is_rejected():
    """通過区間（|Y| < 3m）へ入る前に区間が終わるトラックは候補にしない。

    下流のフェーズ分類は ``|Y| < 3`` の点を必要とするため、区間へ入っていない
    トラックを返しても解析できない。
    """
    tracks = build_tracks(scene_frames(15))
    summaries = [summarize_track(t) for t in tracks]
    forward, ego = estimate_forward_sign(summaries)
    vehicle = max(summaries, key=lambda s: s.speed_y)
    assert not vehicle.entered_zone and vehicle.frames >= MIN_TRACK_FRAMES
    score, reasons = score_track(vehicle, forward, ego, "right")
    assert score == 0.0 and any("通過区間" in r for r in reasons)
    assert select_overtaking(tracks, side="right")["summary"] is None
    print("  OK: 通過区間へ届かないトラックを除外")


def test_incomplete_pass_is_warned():
    """通過区間を抜けきる前に途切れたトラックは、採用しつつ警告する。"""
    result = select_overtaking(build_tracks(scene_frames(25)), side="right")
    summary = result["summary"]
    assert summary is not None and summary.entered_zone and not summary.passed_zone
    assert any("抜けるまで追えていません" in w for w in result["warnings"])
    assert round(result["score"], 3) == 0.95, result["score"]   # 通過区間を抜けない分だけ下がる
    print(f"  OK: 通過途中で途切れたトラックを警告つきで採用 (confidence {result['score']:.3f})")


def test_short_track_is_rejected():
    """追跡フレーム数が足りないトラックは候補にしない。"""
    summaries = [summarize_track(t) for t in build_tracks(scene_frames(4))]
    forward, ego = estimate_forward_sign(summaries)
    for summary in summaries:
        score, reasons = score_track(summary, forward, ego, "right")
        assert score == 0.0 and reasons
    print("  OK: 4フレームだけの場面では候補にしない")


# --- 矩形生成 -----------------------------------------------------------------
def test_frames_are_built_with_interpolation():
    """ロストしたフレームの矩形を前後から内挿する。"""
    tracks = build_tracks(scene_frames(20))
    vehicle = max(tracks, key=lambda t: summarize_track(t).speed_y)
    del vehicle.clusters[7:9]
    frames = track_to_frames(vehicle, margin=0.3)
    indexes = [f["index"] for f in frames]
    assert indexes == list(range(20)), indexes
    centers = [(f["bounds"]["min_y"] + f["bounds"]["max_y"]) / 2 for f in frames]
    assert all(b - a > 0.5 for a, b in zip(centers, centers[1:]))
    widths = [f["bounds"]["max_x"] - f["bounds"]["min_x"] for f in frames]
    assert 2.0 < min(widths) and max(widths) < 3.2, (min(widths), max(widths))
    print("  OK: 欠測2フレームを内挿し、20フレーム分の矩形を生成")


def test_merged_frame_box_is_trimmed():
    """一時的に隣の物体と繋がったフレームの矩形を切り詰める。"""
    tracks = build_tracks(scene_frames(20))
    vehicle = max(tracks, key=lambda t: summarize_track(t).speed_y)
    merged = vehicle.clusters[10]
    merged.min_x -= 4.0          # 隣の路駐車と繋がった状態を模す
    merged.max_y += 6.0
    frames = track_to_frames(vehicle, margin=0.3)
    bounds = frames[10]["bounds"]
    assert bounds["max_x"] - bounds["min_x"] < 5.0, bounds
    assert bounds["max_y"] - bounds["min_y"] < 11.0, bounds
    assert bounds["min_x"] > merged.min_x, bounds
    print("  OK: 膨らんだ矩形を寸法中央値の2倍まで切り詰め")


def test_frames_are_clipped_to_window():
    """要求されたフレーム区間の外へは矩形を作らない。"""
    tracks = build_tracks(scene_frames(20))
    vehicle = max(tracks, key=lambda t: summarize_track(t).speed_y)
    frames = track_to_frames(vehicle, first_frame=5, last_frame=12)
    assert [f["index"] for f in frames] == list(range(5, 13))
    print("  OK: 矩形をフレーム区間内へ制限")


# --- 公開 API -----------------------------------------------------------------
def test_track_overtaking_contract():
    """``track_overtaking`` が契約どおりの辞書を返す。"""
    result = track_overtaking(FakePcap(40), 0, 39, "right")
    assert result["ok"] is True and 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["warnings"], list)
    assert len(result["frames"]) == 40
    for frame in result["frames"]:
        assert set(frame["bounds"]) == {"min_x", "max_x", "min_y", "max_y"}
        assert isinstance(frame["index"], int)
        assert frame["bounds"]["min_x"] < frame["bounds"]["max_x"]
        assert frame["bounds"]["min_y"] < frame["bounds"]["max_y"]
    first, last = result["frames"][0]["bounds"], result["frames"][-1]["bounds"]
    assert last["min_y"] - first["min_y"] > 20.0
    print(f"  OK: 40フレーム分の矩形を生成 (confidence {result['confidence']:.3f})")


def test_track_overtaking_without_vehicle():
    """追い越し車両がいない場合は ok=False・frames=[] を返す。"""
    result = track_overtaking(FakePcap(40, vehicle=False), 0, 39, "right")
    assert result["ok"] is False and result["frames"] == []
    assert result["confidence"] == 0.0 and result["warnings"]
    print("  OK: 車両なしの場面は ok=False")


def test_track_overtaking_invalid_range():
    """フレーム区間が不正な場合も例外にせず ok=False を返す。"""
    assert track_overtaking(FakePcap(10), 5, 5, "right")["ok"] is False
    empty = FakePcap(10)
    empty.frames = []
    assert track_overtaking(empty, 0, 5, "right")["ok"] is False
    print("  OK: 不正なフレーム区間を安全に処理")


def test_exported_boxes_contain_vehicle_points():
    """生成した矩形で実際に車両点を切り出せる。"""
    pcap = FakePcap(30)
    result = track_overtaking(pcap, 0, 29, "right")
    assert result["ok"]
    for frame in result["frames"][::7]:
        points = np.array([(p[0], p[1]) for p in pcap.decode_frame(frame["index"])])
        bounds = frame["bounds"]
        inside = points[
            (points[:, 0] >= bounds["min_x"]) & (points[:, 0] <= bounds["max_x"])
            & (points[:, 1] >= bounds["min_y"]) & (points[:, 1] <= bounds["max_y"])
        ]
        assert len(inside) > 100, (frame["index"], len(inside))
    print("  OK: 各矩形が車両点群を含む")


def test_tracker_handles_empty_frames():
    """点が無いフレームが混ざってもトラッカーが壊れない。"""
    tracker = Tracker()
    tracker.update([], 0.0)
    tracker.update(detect_clusters(scene_frame(_rng(), 0), frame=1, timestamp=0.1), 0.1)
    tracker.update([], 0.2)
    assert len(tracker.tracks()) == 4
    print("  OK: 空フレームを含んでも追跡を継続")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for t in tests:
        t()
    print("[test] 全テスト成功")
