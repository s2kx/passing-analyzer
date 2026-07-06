#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
追い越し判定ロジック (classify_overtaking) の単体テスト。
YOLO や動画を使わず、合成した track 軌跡で判定を検証する。

判定条件 (検出・追跡は前段):
  2) 5 フレーム以上追跡
  3) 追い越し側から現れ抜ける (front=右端から現れ中央へ / rear=中央から現れ左端へ)
  4) 追い越し方向へ移動 (front=中央へ左流れ / rear=左端へ左流れ)
  5) 逆向きに横切る車は除外 (front=左端で消える / rear=右端から現れる)

    .venv\\Scripts\\python.exe test_logic.py
"""
from argparse import Namespace

from detect_overtaking import (
    Track, TrackSample, classify_overtaking, classify_vehicle_type,
    yellow_plate_score,
)


def default_args(view="rear"):
    # 自車運動ゲートは合成データでは検証しないため無効化 (no_stop_filter=True)
    return Namespace(view=view, appear_edge=0.70, exit_edge=0.40,
                     no_stop_filter=True, stop_flow=0.8,
                     min_track_seconds=0.6,
                     no_trajectory_filter=True, no_area_filter=True,
                     road_roi=False)


def geometry_args(view="rear", road_roi=False):
    """新しい誤検知フィルタをすべて有効にした既定値。"""
    return Namespace(
        view=view, appear_edge=0.70, exit_edge=0.40,
        no_stop_filter=True, stop_flow=0.8, min_track_seconds=0.6,
        no_trajectory_filter=False, min_vertical_motion=0.025,
        max_lateral_vertical_ratio=8.0,
        no_area_filter=False, min_area_ratio=1.15, max_mid_peak_ratio=1.8,
        road_roi=road_roi, vanishing_x=0.5, vanishing_y=0.42,
        road_top_half_width=0.08, road_bottom_left=0.12,
        road_bottom_right=0.88, min_road_ratio=0.25,
    )


def make_track(tid, traj):
    """traj: list of (t, cx, cy, area)"""
    tr = Track(tid)
    for i, (t, cx, cy, area) in enumerate(traj):
        tr.samples.append(TrackSample(i, t, cx, cy, area, "car"))
    return tr


def traj(cx_start, cx_end, n=12):
    """cx を cx_start→cx_end へ単調移動させる軌跡。
    判定は横位置のみを使うため面積は一定のダミー値。"""
    return [
        (i * 0.1, cx_start + (cx_end - cx_start) * i / (n - 1), 0.5, 0.03)
        for i in range(n)
    ]


def motion_traj(cx_start, cx_end, cy_start, cy_end, area_start, area_end, n=12):
    """位置と面積が線形変化する、幾何フィルタ用の合成軌跡。"""
    return [
        (
            i * 0.1,
            cx_start + (cx_end - cx_start) * i / (n - 1),
            cy_start + (cy_end - cy_start) * i / (n - 1),
            area_start + (area_end - area_start) * i / (n - 1),
        )
        for i in range(n)
    ]


def test_front_overtake_detected():
    # 前向き: 右端(0.85)から現れ、中央(0.50)へ移動 = 追い越し
    ev = classify_overtaking(make_track(1, traj(0.85, 0.50)), default_args("front"))
    assert ev is not None and ev.side == "left", "前向きの追い越しが検出されるべき"
    print("  OK: front 追い越しを検出 side=left")


def test_rear_overtake_detected():
    # 後ろ向き: 中央(0.50)に現れ、左端(0.15)へ移動 = 追い越し
    ev = classify_overtaking(make_track(2, traj(0.50, 0.15)), default_args("rear"))
    assert ev is not None and ev.side == "right", "後ろ向きの追い越しが検出されるべき"
    print("  OK: rear 追い越しを検出 side=right")


def test_rear_oncoming_ignored():
    # 後ろ向き: 左端(0.15)から現れ中央(0.50)へ = 遠ざかる対向車 → 除外
    ev = classify_overtaking(make_track(2, traj(0.15, 0.50)), default_args("rear"))
    assert ev is None, "左端→中央へ動く対向車は除外されるべき"
    print("  OK: rear 対向車(左端→中央)を除外")


def test_front_exit_left_edge_ignored():
    # 条件5: 前向きで右端から現れ「左端(0.10)で消える」= 横切り → 除外
    ev = classify_overtaking(make_track(3, traj(0.85, 0.10)), default_args("front"))
    assert ev is None, "左端で消える車は除外されるべき"
    print("  OK: 左端で消える車を除外 (条件5)")


def test_rear_exit_right_edge_ignored():
    # 条件5: 後ろ向きで「右端(0.85)から現れ」左端(0.15)へ横切る車 → 除外
    ev = classify_overtaking(make_track(4, traj(0.85, 0.15)), default_args("rear"))
    assert ev is None, "右端から現れて横切る車は除外されるべき"
    print("  OK: 右端から現れ横切る車を除外 (条件5)")


def test_wrong_appear_edge_ignored():
    # 条件3: 前向きで「左側(0.30)」から現れる = 出現端が違う → 除外
    ev = classify_overtaking(make_track(5, traj(0.30, 0.50)), default_args("front"))
    assert ev is None, "出現端が違う車は除外されるべき"
    print("  OK: 出現端が違う車を除外 (条件3)")


def test_not_toward_center_ignored():
    # 条件4: 前向きで右端(0.75)から現れるが中央へ向かわない(0.75→0.95, 右へ) → 除外
    ev = classify_overtaking(make_track(6, traj(0.75, 0.95)), default_args("front"))
    assert ev is None, "中央へ向かわない車は除外されるべき"
    print("  OK: 中央へ向かわない車を除外 (条件4)")


def test_stopped_ego_gate_ignored():
    # 条件1: 自車運動ゲート。背景フローが小さい(停止中)に発生した追い越し形状は除外
    args = default_args("front")
    args.no_stop_filter = False
    tr = make_track(8, traj(0.85, 0.50))               # 形状は追い越し
    stopped = {x.frame: 0.1 for x in tr.samples}       # 背景ほぼ静止=停止中
    assert classify_overtaking(tr, args, stopped) is None, "停止中は除外されるべき"
    moving = {x.frame: 3.0 for x in tr.samples}        # 背景が流れる=走行中
    assert classify_overtaking(tr, args, moving) is not None, "走行中は検出されるべき"
    print("  OK: 停止中の追い越し形状を除外 / 走行中は検出 (条件1)")


def test_too_short_ignored():
    # 条件2: 5 フレーム未満は除外
    ev = classify_overtaking(make_track(7, traj(0.85, 0.50, n=3)), default_args("front"))
    assert ev is None, "サンプル数不足は除外されるべき"
    print("  OK: 短すぎる track を除外 (条件2)")


def test_geometry_rear_overtake_detected():
    # 後方: 中央から左端へ移動しながら、画面下へ接近して bbox が拡大する。
    tr = make_track(20, motion_traj(0.50, 0.15, 0.40, 0.65, 0.008, 0.035))
    ev = classify_overtaking(tr, geometry_args("rear"))
    assert ev is not None and ev.area_change_ratio > 1.15
    print("  OK: 2次元軌跡と面積変化が正しい rear 追い越しを検出")


def test_crossing_vehicle_ignored_by_trajectory():
    # 横切る車: cx は大きく動くが、道路奥行き方向(cy)にはほぼ動かない。
    tr = make_track(21, motion_traj(0.50, 0.15, 0.52, 0.53, 0.012, 0.030))
    assert classify_overtaking(tr, geometry_args("rear")) is None
    print("  OK: 横移動主体の交差車両を除外")


def test_bicycle_passing_vehicle_ignored_by_area():
    # 後方映像なのに車が縮小する=自転車側が車を追い抜いた可能性が高い。
    tr = make_track(22, motion_traj(0.50, 0.15, 0.40, 0.65, 0.035, 0.008))
    assert classify_overtaking(tr, geometry_args("rear")) is None
    print("  OK: 面積変化が逆の車両を除外")


def test_mid_peak_crossing_vehicle_ignored():
    # 交差車両に多い「中盤だけ大きく、両端で小さい」面積変化。
    values = []
    areas = [0.010, 0.011, 0.013, 0.020, 0.035, 0.045,
             0.040, 0.030, 0.020, 0.015, 0.012, 0.011]
    for i, area in enumerate(areas):
        r = i / (len(areas) - 1)
        values.append((i * 0.1, 0.50 - 0.35 * r, 0.40 + 0.25 * r, area))
    tr = make_track(23, values)
    assert classify_overtaking(tr, geometry_args("rear")) is None
    print("  OK: 中盤だけ拡大する交差車両を除外")


def test_close_front_pass_with_mid_peak_detected():
    # 前方カメラで、最接近時に画面右側で大きく映り、その後前方へ抜けて縮小する追い越し。
    args = geometry_args("front", road_roi=True)
    values = []
    cxs = [0.54, 0.56, 0.60, 0.66, 0.72, 0.76, 0.74, 0.70, 0.65, 0.61, 0.58, 0.56]
    areas = [0.006, 0.010, 0.025, 0.080, 0.180, 0.240, 0.220, 0.150, 0.090, 0.060, 0.045, 0.040]
    for i, (cx, area) in enumerate(zip(cxs, areas)):
        values.append((i * 0.1, cx, 0.85, area))
    ev = classify_overtaking(make_track(25, values), args)
    assert ev is not None and ev.area_change_ratio > 1.8
    print("  OK: 近距離で中盤ピークになる front 追い越しを検出")


def test_road_roi_rejects_off_road_track():
    # 狭い道路ROIを明示し、その外側だけを動く track を除外する。
    args = geometry_args("rear", road_roi=True)
    args.road_bottom_left = 0.45
    args.road_bottom_right = 0.55
    args.road_top_half_width = 0.02
    args.min_road_ratio = 0.8
    tr = make_track(24, motion_traj(0.40, 0.15, 0.40, 0.65, 0.008, 0.035))
    assert classify_overtaking(tr, args) is None
    print("  OK: 道路ROI外の車両を除外")


def vehicle_track(classes, plate_scores=None):
    """車種投票と黄色ナンバー判定用の小さなtrackを作る。"""
    plate_scores = plate_scores or [0.0] * len(classes)
    track = Track(90)
    for i, (vehicle_class, plate_score) in enumerate(zip(classes, plate_scores)):
        track.samples.append(TrackSample(
            i, i * 0.1, 0.5, 0.5, 0.01 + i * 0.002, vehicle_class,
            confidence=0.9, yellow_plate_score=plate_score,
        ))
    return track


def test_vehicle_type_large_from_detector_vote():
    vehicle_type, detector_class, _, _ = classify_vehicle_type(
        vehicle_track(["truck", "truck", "truck", "truck", "truck", "car"])
    )
    assert vehicle_type == "大型" and detector_class == "truck"
    print("  OK: truck/bus を大型へ分類")


def test_vehicle_type_ordinary_without_yellow_plate():
    vehicle_type, _, score, hits = classify_vehicle_type(
        vehicle_track(["car"] * 5, [0.0] * 5)
    )
    assert vehicle_type == "普通" and score == 0.0 and hits == 0
    print("  OK: 黄色ナンバーなしを普通へ分類")


def test_vehicle_type_kei_from_multiple_frames():
    vehicle_type, detector_class, score, hits = classify_vehicle_type(
        vehicle_track(["car"] * 5, [0.82, 0.78, 0.74, 0.0, 0.0])
    )
    assert vehicle_type == "軽" and detector_class == "car" and score >= 0.74 and hits == 3
    print("  OK: 複数フレームの黄色ナンバーから軽へ分類")


def test_single_yellow_frame_does_not_make_kei():
    vehicle_type, _, _, hits = classify_vehicle_type(
        vehicle_track(["car"] * 5, [0.9, 0.0, 0.0, 0.0, 0.0])
    )
    assert vehicle_type == "普通" and hits == 1
    print("  OK: 黄色1フレームだけでは軽にしない")


def test_yellow_plate_image_geometry():
    import numpy as np

    frame = np.zeros((400, 600, 3), dtype=np.uint8)
    bbox = (100, 80, 500, 360)
    frame[270:300, 250:320] = (0, 255, 255)  # BGRの黄色、ナンバー比率の矩形
    assert yellow_plate_score(frame, bbox) >= 0.45
    frame[270:300, 250:320] = (255, 255, 255)
    assert yellow_plate_score(frame, bbox) == 0.0
    print("  OK: 黄色いナンバー形状だけを色・形状で抽出")


def test_kei_plate_visible_after_largest_frames():
    """最接近時は側面、通過後に黄色ナンバーが見える実動画パターン。"""
    track = Track(91)
    for i in range(18):
        score = 0.84 if 12 <= i < 18 else 0.0
        track.samples.append(TrackSample(
            i, i * 0.04, 0.5, 0.5, 0.20 - i * 0.009, "car",
            confidence=0.9, yellow_plate_score=score,
        ))
    vehicle_type, _, score, hits = classify_vehicle_type(track)
    assert vehicle_type == "軽" and score >= 0.84 and hits == 6
    print("  OK: 最接近後に見える黄色ナンバーも軌跡全体から検出")


def test_edge_inflated_truck_vote_does_not_override_car_votes():
    """端で大きく映った1回のtruck誤認が、複数のcar票を逆転しない。"""
    track = Track(92)
    track.samples.append(TrackSample(
        0, 0.0, 0.96, 0.5, 0.30, "truck", confidence=0.95,
    ))
    for i in range(1, 7):
        track.samples.append(TrackSample(
            i, i * 0.1, 0.55, 0.5, 0.03, "car", confidence=0.70,
        ))
    vehicle_type, detector_class, _, _ = classify_vehicle_type(track)
    assert vehicle_type == "普通" and detector_class == "car"
    print("  OK: GoPro画面端で膨らんだtruck誤認を弱める")


def test_mixed_van_votes_default_to_ordinary():
    """car票も多いバンは、truck/busが明確でない限り普通にする。"""
    track = vehicle_track(["car"] * 6 + ["truck"] * 3 + ["bus"] * 2)
    vehicle_type, _, _, _ = classify_vehicle_type(track)
    assert vehicle_type == "普通"
    print("  OK: carとtruck/busが混在するバンを普通へ分類")


def test_yellow_plate_overrides_truck_misclassification():
    """軽SUVをYOLOがtruckと誤認しても、黄色ナンバーを優先する。"""
    track = vehicle_track(["truck"] * 6, [0.85] * 6)
    vehicle_type, detector_class, _, hits = classify_vehicle_type(track)
    assert vehicle_type == "軽" and detector_class == "truck" and hits == 6
    print("  OK: 黄色ナンバーはtruck誤認より優先して軽へ分類")


def test_weak_yellow_mark_does_not_override_large_vehicle():
    """大型車の黄色い注意表示は、黄色ナンバーとして大型判定を覆さない。"""
    track = vehicle_track(["truck"] * 8, [0.56] * 8)
    vehicle_type, detector_class, score, hits = classify_vehicle_type(track)
    assert vehicle_type == "大型" and detector_class == "truck"
    assert 0.55 <= score <= 0.57 and hits == 8
    print("  OK: 大型車の弱い黄色注意表示をナンバーと誤認しない")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for t in tests:
        t()
    print("[test] 全テスト成功")
