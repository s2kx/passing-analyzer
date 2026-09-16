#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
追い越し判定ロジック (classify_overtaking) と、edit.csv 解析
(classify_phases / analyze_edit_points) の単体テスト。
YOLO や動画を使わず、合成した track 軌跡・合成した最近接点軌跡で検証する。

判定条件 (検出・追跡は前段):
  2) 5 フレーム以上追跡
  3) 追い越し側から現れ抜ける (front=右端から現れ中央へ / rear=中央から現れ左端へ)
  4) 追い越し方向へ移動 (front=中央へ左流れ / rear=左端へ左流れ)
  5) 逆向きに横切る車は除外 (front=左端で消える / rear=右端から現れる)

    portable\\python\\python.exe test_logic.py
"""
import math
import statistics
from argparse import Namespace

from detect_overtaking import (
    Track, TrackSample, classify_overtaking, classify_vehicle_type,
    yellow_plate_score,
)
from edit_csv import (
    PHASE2_MAX_ABS_Y, EditPoint, analyze_edit_points, classify_phases,
    build_edit_rows_from_file,
)


def default_args(view="rear"):
    # 自車運動ゲートは合成データでは検証しないため無効化 (no_stop_filter=True)
    return Namespace(view=view, appear_edge=0.70, exit_edge=0.40,
                     appear_window_seconds=0.5,
                     no_stop_filter=True, stop_flow=0.8,
                     min_track_seconds=0.6,
                     no_trajectory_filter=True, no_area_filter=True,
                     road_roi=False)


def geometry_args(view="rear", road_roi=False):
    """新しい誤検知フィルタをすべて有効にした既定値。"""
    return Namespace(
        view=view, appear_edge=0.70, exit_edge=0.40,
        appear_window_seconds=0.5, close_pass_min_pre_samples=0,
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


def test_fast_close_pass_with_long_tail_detected():
    # 至近を1秒未満で抜けたあと、前方に小さく映り続ける追い越し。出現端を「先頭1/4
    # 区間の中央値」だけで見ると、長い尾に引かれて中央寄りの値になり取りこぼす。
    # 出現直後 appear_window_seconds の間に端へ最も寄った位置も見て拾う (第6波A)。
    values = []
    pass_cx = [0.92, 0.88, 0.85, 0.82, 0.78, 0.74, 0.70, 0.66]
    pass_y = [0.79, 0.76, 0.73, 0.70, 0.68, 0.66, 0.64, 0.62]
    pass_area = [0.065, 0.090, 0.130, 0.180, 0.150, 0.100, 0.060, 0.040]
    for i, (cx, cy, area) in enumerate(zip(pass_cx, pass_y, pass_area)):
        values.append((i / 24.0, cx, cy, area))
    tail_n = 60                                    # 前方に映り続ける尾 (約6秒)
    for i in range(tail_n):
        r = i / (tail_n - 1)
        values.append((8 / 24.0 + i * 0.1, 0.65 - 0.13 * r,
                       0.60 - 0.06 * r, 0.030 - 0.0294 * r))
    tr = make_track(26, values)

    cxs = [x.cx for x in tr.samples]
    q = max(1, len(cxs) // 4)
    assert statistics.median(cxs[:q]) < 0.70, "中央値では出現端を満たさない軌跡であること"

    args = geometry_args("front")
    args.close_pass_min_road_ratio = 1.1           # 近接救済を止めて出現端の判定だけを見る
    ev = classify_overtaking(tr, args)
    assert ev is not None, "速く抜けた至近追い越しを検出すべき"
    print("  OK: 速く抜けて前方に映り続ける追い越しを検出 (出現端の窓)")


def test_close_pass_starting_at_peak_detected():
    # 最接近の瞬間から track が始まる至近追い越し。接近側が画角外・別trackだと
    # 面積ピークが先頭に来る。ピーク前のサンプルを要求すると救済できない (第6波A)。
    values = []
    n = 12
    for i in range(n):
        r = i / (n - 1)
        values.append((i * 0.1, 0.60 - 0.10 * r, 0.80, 0.19 - 0.189 * r))
    tr = make_track(27, values)

    args = geometry_args("front")
    ev = classify_overtaking(tr, args)
    assert ev is not None, "先頭が最接近の至近追い越しを救済すべき"

    args.close_pass_min_pre_samples = 3            # 旧仕様ではピーク前3点が必要だった
    assert classify_overtaking(tr, args) is None
    print("  OK: 先頭が最接近になる至近追い越しを救済 (ピーク前サンプルの要求なし)")


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


# ---------------------------------------------------------------------------
# edit.csv 解析 (フェーズ判定・離隔距離LC) のテスト
# ---------------------------------------------------------------------------

def edit_points(rows):
    """rows: list of (unixtime, X, Y, speed) から EditPoint の列を作る。

    distance(m) は edit_csv と同じく sqrt(X^2 + Y^2) で埋める。
    """
    points = []
    for row_number, (t, x, y, speed) in enumerate(rows, start=2):
        speed_text = "" if speed is None else str(speed)
        points.append(EditPoint(
            row_number=row_number, unixtime_text=str(t), unixtime=float(t),
            x=float(x), y=float(y), z_text="0", distance=math.hypot(x, y),
            speed=None if speed is None else float(speed),
            raw=[str(t), str(x), str(y), "0", str(math.hypot(x, y)), speed_text],
        ))
    return points


def test_phases_normal_case():
    """通常データ: フェーズ2は一桁速度の最後の点、フェーズ3/4は Y で決まる。"""
    points = edit_points([
        (0, -0.5, 8, None), (1, -1.0, 7, 12), (2, -1.5, 6, 8),
        (3, -2.0, 5, 7), (4, -2.5, 2.5, 14), (5, -1.0, 3.2, 16),
    ])
    metrics = analyze_edit_points(points)
    assert metrics["phase2"].row_number == 5, metrics
    assert metrics["phase3"].row_number == 6, metrics
    assert metrics["phase4"].row_number == 7, metrics
    assert metrics["passing_time"] == 1.0, metrics
    assert metrics["warnings"] == [], metrics
    print("  OK: 通常データのフェーズ判定は従来どおり")


def test_phase2_fallback_uses_point_before_phase3():
    """フェーズ2欠落(|Y|<3 の前に speed が無い) → フェーズ3直前の点で代用し警告。"""
    rows = [(0, -2.0, 8, None), (1, -2.5, 5, None), (2, -2.5, 2.5, 14), (3, -1.0, 3.2, 16)]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["phase2"].row_number == 3, metrics          # フェーズ3直前の有効点
    assert metrics["phase3"].row_number == 4, metrics
    assert metrics["distance_p2"] == 5.0, metrics
    assert any("フェーズ2" in w for w in metrics["warnings"]), metrics

    # strict=True (既定) の classify_phases は従来どおり例外で止まる。
    try:
        classify_phases(edit_points(rows))
    except ValueError as exc:
        assert "フェーズ2" in str(exc), exc
    else:
        raise AssertionError("strict=True ではフェーズ2欠落は例外になるべき")
    print("  OK: フェーズ2欠落をフェーズ3直前の点で代用し警告")


def test_phase4_fallback_uses_last_point():
    """フェーズ4欠落(|Y|>=3 に戻らない) → 最終有効点で代用し警告。"""
    rows = [
        (0, -0.5, 8, None), (1, -1.0, 7, 12), (2, -1.5, 6, 8),
        (3, -2.0, 5, 7), (4, -2.5, 2.5, 14), (5, -1.0, 1.0, 16),
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["phase3"].row_number == 6, metrics
    assert metrics["phase4"].row_number == 7, metrics          # 最終有効点
    assert metrics["passing_time"] == 1.0, metrics
    assert any("フェーズ4" in w for w in metrics["warnings"]), metrics

    try:
        classify_phases(edit_points(rows))
    except ValueError as exc:
        assert "フェーズ4" in str(exc), exc
    else:
        raise AssertionError("strict=True ではフェーズ4欠落は例外になるべき")
    print("  OK: フェーズ4欠落を最終有効点で代用し警告")


def test_phase3_missing_still_raises():
    """フェーズ3欠落(|Y|<3 が一度も無い)は計算不能なのでフォールバックしない。"""
    rows = [(0, -0.5, 8, None), (1, -1.0, 7, 12), (2, -1.5, 6, 8)]
    for strict in (True, False):
        try:
            classify_phases(edit_points(rows), strict=strict)
        except ValueError as exc:
            assert "選択範囲" in str(exc) and "同期" in str(exc), exc
        else:
            raise AssertionError("フェーズ3欠落は例外になるべき")
    print("  OK: フェーズ3欠落は選択範囲・同期ずれを示す例外で停止")


def test_lc_ignores_opposite_side_outlier():
    """通過区間に反対符号のX外れ値が1点混ざってもLCが乗っ取られない。"""
    rows = [
        (0, 2.0, 8, None), (1, 2.0, 7, 12), (2, 2.0, 5, 7),
        (3, 2.5, 2.5, 14),      # フェーズ3
        (4, -0.2, 1.0, 15),     # 反対側へ飛んだ外れ値
        (5, 1.5, 0.5, 15),
        (6, 2.0, 3.2, 16),      # フェーズ4
    ]
    metrics = analyze_edit_points(edit_points(rows))
    # 外れ値 -0.2 ではなく、多数派(X>0)側で最も近い 1.5 が採用される。
    assert metrics["lc"] == 1.5, metrics
    assert any("外れ値" in w for w in metrics["warnings"]), metrics
    print("  OK: 反対符号の外れ値を除いてLCを算出")


def test_lc_is_nearest_lateral_distance():
    """LCは通過側で |X| が最小の点（最も近い横距離）。"""
    rows = [
        (0, -0.5, 8, None), (1, -1.0, 7, 12), (2, -2.0, 5, 7),
        (3, -2.5, 2.5, 14), (4, -1.0, 3.2, 16),
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["lc"] == 1.0, metrics     # min(|-2.5|, |-1.0|)
    print("  OK: 通過側で最も近い横距離をLCにする")


def test_lc_skips_points_closer_than_one_meter():
    """distance(m) < 1.0 の点はLC対象から外す（既存仕様）。"""
    rows = [
        (0, 2.0, 8, None), (1, 2.0, 7, 12), (2, 2.0, 5, 7),
        (3, 2.5, 2.5, 14),      # フェーズ3
        (4, 0.3, 0.3, 15),      # distance=0.42 → LC対象外
        (5, 2.0, 3.2, 16),      # フェーズ4
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["lc"] == 2.0, metrics
    print("  OK: distance < 1m の点はLC対象から除外")


def test_phase2_speed_spike_is_reported():
    """最近接点の乗り移りによる速度スパイクは候補から外し、警告を残す。"""
    rows = [
        (0, -0.5, 8, None), (1, -1.0, 7, 480), (2, -1.5, 6, 8),
        (3, -2.0, 5, 320), (4, -2.5, 2.5, 14), (5, -1.0, 3.2, 16),
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["phase2"].row_number == 4, metrics   # 一桁速度(8)の点
    assert any("スパイク" in w for w in metrics["warnings"]), metrics
    print("  OK: フェーズ2候補の速度スパイクを除外し警告")


def test_phase2_ignores_far_candidates():
    """遠方の点はフェーズ2候補にしない（距離p2・TTCの系統過大の対策）。

    速度が一桁の点が無い場合の従来規則「速度絶対値が最小の点」は、追跡が長いほど
    遠い点を拾ってしまう。|Y| <= PHASE2_MAX_ABS_Y の点だけを候補にする。
    """
    rows = [
        (0, 2.0, 20, None),
        (1, 2.0, 18, 11),      # 速度最小だが遠すぎる → 候補外
        (2, 2.0, 12, 30),
        (3, 2.0, 5.0, 28),     # |Y| <= 5.5 の候補
        (4, 2.5, 2.5, 26),     # フェーズ3
        (5, 2.0, 3.2, 25),     # フェーズ4
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["phase2"].row_number == 5, metrics     # Y=5.0 の点
    assert metrics["distance_p2"] == 5.0, metrics
    assert metrics["warnings"] == [], metrics
    print(f"  OK: |Y| <= {PHASE2_MAX_ABS_Y:g} m の点だけをフェーズ2候補にする")


def test_phase2_far_only_falls_back_with_warning():
    """|Y| <= 上限 の候補が1点も無ければ制限を外し、警告を残す。"""
    rows = [
        (0, 2.0, 20, None), (1, 2.0, 18, 11), (2, 2.0, 12, 30),
        (3, 2.5, 2.5, 26),     # フェーズ3（直前に近い候補が無い）
        (4, 2.0, 3.2, 25),     # フェーズ4
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["distance_p2"] == 18.0, metrics        # 速度最小の点に戻る
    assert any("|Y|" in w for w in metrics["warnings"]), metrics
    print("  OK: 近い候補が無いときは制限を外して警告")


def test_negative_relative_speed_is_warned():
    """フェーズ4がフェーズ3より手前（相対速度が0以下）なら警告を出す。"""
    rows = [
        (0, 2.0, 8, None), (1, 2.0, 5.0, 20),
        (2, 2.0, 2.5, 20),      # フェーズ3
        (3, 2.0, 1.0, 20),
        (4, 2.0, -3.2, 20),     # フェーズ4（フェーズ3より手前）
    ]
    metrics = analyze_edit_points(edit_points(rows))
    assert metrics["relative_speed"] < 0, metrics
    assert any("相対速度" in w and "0以下" in w for w in metrics["warnings"]), metrics
    print("  OK: 相対速度が0以下のフェーズ崩壊を警告する")


def _write_frames_csv(path, frames):
    """``frames`` = [(time, [(X, Y, Z), ...]), ...] から VeloView 形式CSVを書く。"""
    header = ",".join((
        "Frame", "Time", "Laser ID", "Intensity", "Distance",
        "Azimuth", "Elevation", "Selected", "Points:0", "Points:1", "Points:2",
    ))
    lines = [header]
    for index, (t, points) in enumerate(frames):
        for x, y, z in points:
            lines.append(f"{index},{t:.6f},0,5,5,0,0,1,{x},{y},{z}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


# 自車構造物: センサ架台の環(0.48m)と、通過側と反対側にある搭乗者(1.05m)。
# どちらも実データ(ID3候補056・ID4候補011)でフェーズ判定を壊した張本人。
_EGO_POINTS = [(0.15, 0.46, -0.13), (-0.15, 0.46, -0.13), (-1.05, 0.05, -0.13)]


def test_trajectory_ignores_ego_structure():
    """自車構造物に乗り移らず追い越し車を追い続ける（ID3候補056・ID4候補011の再現）。

    車が横を通り過ぎると、車より近い自車構造物（架台の環 0.48m、搭乗者 1.05m）が
    最近接点になって軌跡を乗っ取り、|Y| が 3 に戻らないためフェーズ4が作れなくなる。
    その結果、通過時間が数秒に伸び TTC と相対速度が壊れる。
    """
    import tempfile
    from pathlib import Path

    frames = []
    for i in range(20):
        # 追い越し車: X=1.2 一定、Y は -6.0 から 0.7m/フレームで前へ抜けていく
        points = [(1.2, -6.0 + 0.7 * i, -0.2)]
        if i >= 6:
            points += _EGO_POINTS          # 車が近づくと矩形が原点をまたぐ
        frames.append((100.0 + 0.1 * i, points))

    with tempfile.TemporaryDirectory() as temp_value:
        path = Path(temp_value) / "overtaking_901.csv"
        _write_frames_csv(path, frames)
        rows = build_edit_rows_from_file(path)

    xs = [float(r[1]) for r in rows[1:] if r[1]]
    assert xs and all(abs(x - 1.2) < 1e-6 for x in xs), rows
    metrics = analyze_edit_points(edit_points_from_rows(rows))
    assert abs(metrics["passing_time"] - 0.8) < 1e-6, metrics
    assert abs(metrics["distance_p3"] - 5.6) < 1e-6, metrics
    assert metrics["relative_speed"] > 0 and metrics["ttc"] > 0, metrics
    print("  OK: 自車構造物に乗り移らず追い越し車を追い続ける")


def test_trajectory_stops_when_vehicle_is_lost():
    """車を見失った後に矩形へ入り込む別物体は軌跡に入れない。"""
    import tempfile
    from pathlib import Path

    frames = []
    for i in range(12):
        if i < 8:
            points = [(1.2, -4.0 + 0.6 * i, -0.2)]      # 追い越し車
        else:
            points = [(2.0, -8.0 + 0.2 * (i - 8), -0.2)]  # 遠くの別物体
        frames.append((200.0 + 0.1 * i, points))

    with tempfile.TemporaryDirectory() as temp_value:
        path = Path(temp_value) / "overtaking_902.csv"
        _write_frames_csv(path, frames)
        rows = build_edit_rows_from_file(path)

    assert all(r[1] for r in rows[1:9]), rows            # 車の 8 フレームは残る
    assert not any(r[1] for r in rows[9:]), rows         # 別物体のフレームは空
    print("  OK: 追跡が途切れた後の別物体を軌跡に入れない")


def edit_points_from_rows(rows):
    """``build_edit_rows_from_file`` の戻り値を EditPoint の列に変換する。"""
    return [
        EditPoint(
            row_number=index, unixtime_text=r[0], unixtime=float(r[0]),
            x=float(r[1]), y=float(r[2]), z_text=r[3], distance=float(r[4]),
            speed=float(r[5]) if r[5] else None, raw=list(r),
        )
        for index, r in enumerate(rows[1:], start=2) if r[1]
    ]


def test_nearest_point_skips_ground_points():
    """路面付近の点は最近接点にしない（矩形の余裕で入り込む路面点のLC過小対策）。"""
    import tempfile
    from pathlib import Path

    header = ",".join((
        "Frame", "Time", "Laser ID", "Intensity", "Distance",
        "Azimuth", "Elevation", "Selected", "Points:0", "Points:1", "Points:2",
    ))
    lines = [header]
    # 路面: Z=-1.0 に 30 点。うち 1 点は車体より内側(X=0.8)にあり、
    # 除去しないとこれが最近接点になってしまう。
    for i in range(30):
        x = 0.8 if i == 0 else 2.0 + i * 0.05
        lines.append(f"0,10.0,0,5,5,0,0,1,{x},0.0,-1.0")
    # 車体: Z=-0.2 に 10 点。最も近いのは X=1.5。
    for i in range(10):
        lines.append(f"0,10.0,0,5,5,0,0,1,{1.5 + i * 0.1},0.0,-0.2")

    with tempfile.TemporaryDirectory() as temp_value:
        path = Path(temp_value) / "overtaking_900.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        rows = build_edit_rows_from_file(path)

    assert len(rows) == 2, rows
    assert float(rows[1][1]) == 1.5, rows[1]      # 路面の 0.8 ではなく車体の 1.5
    print("  OK: 路面付近の点を除いてから最近接点を選ぶ")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for t in tests:
        t()
    print("[test] 全テスト成功")
