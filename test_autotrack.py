#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追い越し車両の自動追跡 (``lidar_autotrack``) の単体テスト。

PCAP を使わず、numpy で合成した点群（地面平面 + 静止障害物 + 通過車両）で
地面除去・クラスタリング・トラッキング・同定・矩形生成を検証する。

    .venv\\Scripts\\python.exe test_autotrack.py
"""

import numpy as np

from lidar_autotrack import (
    MIN_TRACK_FRAMES, PASS_ZONE, ROI_MAX_ABS_Y, Cluster, Track, Tracker, build_tracks,
    cluster_points,
    detect_clusters, estimate_forward_sign, estimate_ground_plane, estimate_ground_z,
    filter_points, ground_height, score_track, select_overtaking, summarize_track,
    track_key_matches, track_overtaking, track_to_frames, velocity_compatible,
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


# 車列の2台目。1台目より 24m 後ろ = 相対 8m/s なので約 3 秒あとに通過する。
FOLLOWER_START_Y = -44.0


def scene_frame(rng, frame: int, vehicle: bool = True, flip: bool = False,
                oncoming: bool = False, follower: float | None = None) -> np.ndarray:
    """1 フレーム分の合成点群を作る。

    - 地面平面（z = -1.30）
    - 静止障害物 3 つ（路駐車・壁・電柱）。自転車速度で後退して見える。
    - 通過車両（``vehicle=True``）。自転車の右 1.8m を相対 8m/s で追い越す。
    - 車列の2台目（``follower`` に開始 Y を指定）。同じ速度で後ろから続く。
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
    if follower is not None:
        clouds.append(box_points(rng, 2.1, follower + 8.0 * t, 4.2, 1.8, 1.5))
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


def _cluster(frame: int, x: float, y: float, length: float = 4.6, width: float = 1.8,
             count: int = 60) -> Cluster:
    """指定位置・寸法のクラスタを 1 つ作る（トラッキングの単体テスト用）。"""
    return Cluster(
        frame=frame, timestamp=frame * FRAME_INTERVAL, center_x=x, center_y=y,
        min_x=x - width / 2, max_x=x + width / 2,
        min_y=y - length / 2, max_y=y + length / 2, count=count,
    )


# 注目領域の遠端ですれ違う「遠ざかる静止物」と「近づいてくる追い越し車」。
# 実データ(260701/ID1)の 395.1s と同じ形: 静止物は 65 フレーム追われたあと遠端
# Y=-29m で見えなくなり、そこへ追い越し車が Y=-28m から入ってくる（相対 +6m/s で
# 57 フレーム）。距離だけで対応づけると静止物のトラックが車へ乗り移り、前進と後退が
# 混ざって Y 速度がほぼ 0 になるため、追い越し車として同定できなくなる。
CROSSING_HANDOVER_FRAME = 65
CAR_SPEED = 6.0


def crossing_frames(count: int = 122) -> list[tuple[int, float, list[Cluster]]]:
    """遠端で静止物と追い越し車がすれ違う場面を合成する。"""
    frames = []
    for frame in range(count):
        t = frame * FRAME_INTERVAL
        clusters = []
        for base_x, base_y, length, width in ((-5.5, 12.0, 6.0, 0.6),   # 壁
                                              (6.2, 20.0, 4.4, 1.7)):   # 路駐車
            y = base_y - BIKE_SPEED * t
            if abs(y) <= ROI_MAX_ABS_Y:          # 遠端を出た静止物は見えない
                clusters.append(_cluster(frame, base_x, y, length, width))
        if frame < CROSSING_HANDOVER_FRAME:      # 遠端で見えなくなる静止物
            clusters.append(_cluster(frame, 2.0, 3.0 - BIKE_SPEED * t))
        if frame >= CROSSING_HANDOVER_FRAME:     # 遠端から入ってくる追い越し車
            clusters.append(_cluster(
                frame, 2.2,
                -28.0 + CAR_SPEED * (t - CROSSING_HANDOVER_FRAME * FRAME_INTERVAL)))
        frames.append((frame, t, clusters))
    return frames


def test_velocity_compatible_rejects_direction_reversal():
    """進行方向が確定したトラックは、向きを反転させる観測を受け付けない。"""
    track = Track(track_id=0)
    for frame in range(4):
        track.append(_cluster(frame, 2.0, -20.0 - BIKE_SPEED * frame * FRAME_INTERVAL))
    assert track.velocity_y < -4.0, track.velocity_y
    # 遠ざかっているトラックへ、近づいてくる車の観測は結び付けない
    approaching = _cluster(4, 2.2, track.clusters[-1].center_y + 1.0)
    assert not velocity_compatible(track, approaching)
    # 同じ向きの観測と、重心の揺れ程度（数 m/s）の揺り戻しは通す。ここを狭めると
    # 同じ物体のトラックが細切れになり、断片が別の車に見えて誤検出が増える
    assert velocity_compatible(track, _cluster(4, 2.0, track.clusters[-1].center_y - 0.5))
    assert velocity_compatible(track, _cluster(4, 2.0, track.clusters[-1].center_y + 0.05))
    assert velocity_compatible(track, _cluster(4, 2.0, track.clusters[-1].center_y + 0.6))
    # 進行方向が定まっていない（観測数・速度が足りない）トラックには効かない
    slow = Track(track_id=1)
    for frame in range(4):
        slow.append(_cluster(frame, 2.0, -20.0 - 0.5 * frame * FRAME_INTERVAL))
    assert velocity_compatible(slow, _cluster(4, 2.0, slow.clusters[-1].center_y + 1.0))
    print("  OK: 進行方向を反転させる観測を対応づけから外す")


def test_receding_object_does_not_hijack_the_approaching_car():
    """遠端ですれ違っても、静止物のトラックが追い越し車へ乗り移らない。"""
    frames = crossing_frames()
    tracks = build_tracks(frames)
    lane = [t for t in tracks if 1.0 < t.clusters[0].center_x < 4.0]
    car = [t for t in lane if t.first_frame == CROSSING_HANDOVER_FRAME]
    assert len(car) == 1, [(t.first_frame, t.last_frame) for t in lane]
    summary = summarize_track(car[0])
    assert summary.frames == len(frames) - CROSSING_HANDOVER_FRAME, summary.frames
    assert abs(summary.speed_y - CAR_SPEED) < 0.3, summary.speed_y
    # 静止物側は乗り移らずに終わっている（車のフレームを1つも含まない）
    stale = [t for t in lane if t.first_frame == 0]
    assert len(stale) == 1 and stale[0].last_frame < CROSSING_HANDOVER_FRAME, stale
    print(f"  OK: 遠端のすれ違いで乗り移らない（車のY速度 {summary.speed_y:+.2f}m/s）")


def test_hijacked_track_would_hide_the_passing_vehicle():
    """速度整合を外すと乗り移りが起き、追い越し車を同定できなくなる（回帰の証拠）。"""
    frames = crossing_frames()
    merged = build_tracks(frames, max_reversal=float("inf"))
    hijacked = [t for t in merged if 1.0 < t.clusters[0].center_x < 4.0]
    assert len(hijacked) == 1 and hijacked[0].last_frame == len(frames) - 1, \
        "乗り移りが再現しない（テストの前提が崩れている）"
    # 前進と後退が混ざって Y 速度がほぼ 0 になり、追い越し車の条件を満たさなくなる
    assert abs(summarize_track(hijacked[0]).speed_y) < 1.0, summarize_track(hijacked[0]).speed_y
    assert select_overtaking(merged)["summary"] is None, "乗り移ったのに同定できている"
    # 速度整合ありなら同じ場面で追い越し車を同定できる
    chosen = select_overtaking(build_tracks(frames))["summary"]
    assert chosen is not None and chosen.median_x > 1.0, chosen
    assert chosen.pass_time is not None and chosen.passed_zone, chosen
    print("  OK: 速度整合なしでは同定できない場面を、ありなら同定できる")


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


# --- 車列（イベント↔車両の対応付け） -----------------------------------------
def convoy_pcap(count: int = 70) -> FakePcap:
    """2 台が続けて通過する場面。1台目は 1.9 秒、2台目は 4.9 秒に通過区間へ入る。"""
    return FakePcap(count, follower=FOLLOWER_START_Y)


def test_candidates_list_every_passing_vehicle():
    """車列では通過した車すべてを ``candidates`` に通過時刻つきで返す。"""
    result = track_overtaking(convoy_pcap(), 0, 69, "right")
    keys = {candidate["track_key"] for candidate in result["candidates"]}
    assert len(result["candidates"]) == 2, result["candidates"]
    assert len(keys) == 2, keys
    times = sorted(candidate["pass_time"] for candidate in result["candidates"])
    assert abs(times[0] - 1.9) < 0.3 and abs(times[1] - 4.9) < 0.3, times
    for candidate in result["candidates"]:
        assert set(candidate) == {
            "track_key", "pass_time", "score", "median_x", "lc_estimate",
            "length", "width", "height", "near_length", "points",
        }
        assert 0.5 < candidate["lc_estimate"] < 2.0, candidate
    print(f"  OK: 車列の2台を候補として列挙 (通過 {times[0]:.1f}s / {times[1]:.1f}s)")


def test_expected_pass_time_picks_the_matching_vehicle():
    """期待通過時刻に最も近いトラックを選ぶ（スコア最大ではなく）。

    2 台とも評価値は同じなので、スコアだけでは 2 台目を選べない。
    """
    plain = track_overtaking(convoy_pcap(), 0, 69, "right")
    leader = track_overtaking(convoy_pcap(), 0, 69, "right", expected_pass_time=2.0)
    follower = track_overtaking(convoy_pcap(), 0, 69, "right", expected_pass_time=5.0)
    assert leader["ok"] and follower["ok"]
    assert leader["track_key"] != follower["track_key"], leader["track_key"]
    assert abs(leader["pass_time"] - 1.9) < 0.3, leader["pass_time"]
    assert abs(follower["pass_time"] - 4.9) < 0.3, follower["pass_time"]
    # 期待時刻を渡さない従来の呼び方では 1 台目（スコア順の先頭）のままになる
    assert plain["track_key"] == leader["track_key"], plain["track_key"]
    assert plain["pass_time"] is not None and plain["candidates"]
    # 2台目の矩形は 1台目より後ろのフレームから始まる
    assert follower["frames"][0]["index"] > leader["frames"][0]["index"] - 5
    print("  OK: 期待通過時刻 2.0s/5.0s で1台目・2台目を撃ち分け")


def test_exclude_track_keys_falls_back_to_the_next_vehicle():
    """割り当て済みのトラックを除外すると次点（車列の2台目）を選ぶ。"""
    first = track_overtaking(convoy_pcap(), 0, 69, "right", expected_pass_time=2.0)
    second = track_overtaking(convoy_pcap(), 0, 69, "right", expected_pass_time=2.0,
                              exclude_track_keys=[first["track_key"]])
    assert second["ok"] and second["track_key"] != first["track_key"]
    assert abs(second["pass_time"] - 4.9) < 0.3, second["pass_time"]
    # 全部除外すれば候補なしとして ok=False（重複出力を作らない）
    empty = track_overtaking(convoy_pcap(), 0, 69, "right", expected_pass_time=2.0,
                             exclude_track_keys=[first["track_key"], second["track_key"]])
    assert empty["ok"] is False and empty["frames"] == []
    assert empty["track_key"] is None and len(empty["candidates"]) == 2
    assert any("割り当て済み" in w for w in empty["warnings"]), empty["warnings"]
    print("  OK: 割り当て済みを除外して2台目を選び、全除外なら ok=False")


def test_track_key_tolerates_window_jitter():
    """追跡窓が違って通過フレームが僅かにずれても同じ車と照合できる。"""
    assert track_key_matches("f19x+1.8", "f19x+1.8")
    assert track_key_matches("f19x+1.8", "f21x+2.0")      # 2フレーム・0.2m のぶれ
    assert not track_key_matches("f19x+1.8", "f49x+2.1")  # 車列の2台目は別の車
    assert not track_key_matches("f19x+1.8", "f19x+3.5")  # 隣車線の車は別の車
    assert not track_key_matches("f19x+1.8", "t19x+1.8")  # 種別が違えば別扱い
    print("  OK: track_key を前後3フレーム・1mの許容で照合")


def test_tracker_handles_empty_frames():
    """点が無いフレームが混ざってもトラッカーが壊れない。"""
    tracker = Tracker()
    tracker.update([], 0.0)
    tracker.update(detect_clusters(scene_frame(_rng(), 0), frame=1, timestamp=0.1), 0.1)
    tracker.update([], 0.2)
    assert len(tracker.tracks()) == 4
    print("  OK: 空フレームを含んでも追跡を継続")


def test_cluster_split_separates_a_vehicle_that_touches_the_rider():
    """車としてありえない幅に繋がったかたまりは、細い連結でほどき直す。"""
    rng = _rng()
    rider = box_points(rng, -1.0, 0.0, 2.0, 1.6, 1.0, count=900)
    vehicle = box_points(rng, 2.0, 0.0, 4.4, 1.8, 1.5, count=900)
    clusters = cluster_points(np.vstack([rider, vehicle]), frame=0)
    assert len(clusters) == 2, [(c.min_x, c.max_x) for c in clusters]
    left, right = sorted(clusters, key=lambda c: c.center_x)
    assert left.max_x < 0.0 < right.min_x, (left.max_x, right.min_x)
    # 1 台の車としてありえる大きさなら分けない（従来どおり）
    single = cluster_points(box_points(rng, 2.0, 0.0, 4.4, 1.8, 1.5, count=900), frame=0)
    assert len(single) == 1, single
    print("  OK: 幅4mを超えて繋がったかたまりを車と搭乗者へ分離")


def _zone_track(centers: list[tuple[float, float]]) -> Track:
    """``(X, Y)`` の並びから、幅 1.8m・長さ 4.0m の矩形が動くトラックを作る。"""
    clusters = [
        Cluster(frame=index, timestamp=index * FRAME_INTERVAL,
                center_x=x, center_y=y, min_x=x - 0.9, max_x=x + 0.9,
                min_y=y - 2.0, max_y=y + 2.0, count=200,
                top_z=GROUND_Z + 1.5, ground_z=GROUND_Z)
        for index, (x, y) in enumerate(centers)
    ]
    return Track(track_id=1, clusters=clusters)


def test_pass_time_uses_the_visit_that_draws_level():
    """通過区間へ2回入るトラックでは、真横に並んだほうの滞在を通過とみなす。

    路肩の静止物から追い越し車へ乗り移ったトラックがこの形になる。最初の滞在
    （かすめただけ）を採ると通過時刻が数秒早くなり、割り当てが外れる。
    """
    # 1回目: |Y| < 3 をかすめるだけで Y=0 を跨がない。2回目: 後方から真横を抜ける。
    graze = [(-5.0, -2.5), (-5.0, -2.8)]
    away = [(-3.0, -6.0 - 2.0 * i) for i in range(5)]
    approach = [(2.0, -4.0 + 1.5 * i) for i in range(6)]
    summary = summarize_track(_zone_track(graze + away + approach))
    assert summary.entered_zone
    # 2回目の滞在の先頭（Y=-4.0 の矩形が |Y|<3 へ入るフレーム）が通過時刻
    assert summary.pass_frame == len(graze) + len(away), summary.pass_frame
    assert abs(summary.lateral_min - 1.1) < 1e-6, summary.lateral_min
    print(f"  OK: 乗り移りトラックの通過時刻は真横の滞在 (frame {summary.pass_frame})")


def test_pass_time_unchanged_for_a_single_visit():
    """通過区間へ1回しか入らないふつうのトラックでは通過時刻は変わらない。"""
    centers = [(1.8, -12.0 + 1.5 * i) for i in range(14)]
    summary = summarize_track(_zone_track(centers))
    first = next(i for i, (_x, y) in enumerate(centers) if abs(y) - 2.0 < PASS_ZONE)
    assert summary.pass_frame == first, (summary.pass_frame, first)
    print("  OK: 1回しか区間へ入らないトラックの通過時刻は従来どおり")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for t in tests:
        t()
    print("[test] 全テスト成功")
