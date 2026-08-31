#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""時刻同期の自己検証 (``tools/sync_check``) の単体テスト。

実データを使わず、合成した点群と合成した速度プロファイルで
「自車速度の推定」→「相互相関でのオフセット推定」の全経路を検証する。

    .venv\\Scripts\\python.exe test_sync_check.py
"""

import math
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import sync_check as sc  # noqa: E402


GROUND_Z = -1.30          # センサ取付高 1.30m → 地面は z = -1.30
FRAME_INTERVAL = 0.1      # VLP-16 は約 10Hz


def _rng(seed: int = 20260901) -> np.random.Generator:
    return np.random.default_rng(seed)


# ── 合成の速度プロファイル（解析的に積分できる形） ──────────────────────────
W1 = 2 * math.pi / 40.0
W2 = 2 * math.pi / 17.0


def true_speed(t):
    """自転車の速度 (m/s)。数十秒周期でゆっくり上下する。"""
    return 6.0 + 2.0 * np.sin(W1 * t) + 1.2 * np.sin(W2 * t + 0.7)


def true_distance(t):
    """``true_speed`` の積分（t=0 で 0）。"""
    base = -2.0 * np.cos(W1 * t) / W1 - 1.2 * np.cos(W2 * t + 0.7) / W2
    zero = -2.0 * np.cos(0.0) / W1 - 1.2 * np.cos(0.7) / W2
    return 6.0 * t + base - zero


class FakePcap:
    """``VelodynePcap`` の最小の代役。静止物だけの世界を自転車が走る。

    世界座標に固定した柱を並べ、時刻 t の自車位置 ``true_distance(t)`` を引いて
    センサ座標に直す。これで「静止物は自車速度で -Y へ流れる」実データと同じ
    見え方になる。地面点は毎フレーム作り直す（実データ同様、地面には Y 方向の
    手掛かりが無いことを再現する）。
    """

    def __init__(self, duration: float = 240.0, seed: int = 20260901,
                 moving_car: bool = False):
        rng = _rng(seed)
        # 路側の物体を世界座標に固定して作る。1本の柱を真っ直ぐ立てると XY 平面
        # では 1 セルにしかならないので、実際の植込み・塀のように XY にも散らす。
        count, per_object = 2600, 10
        center_x = rng.uniform(2.0, 9.5, count) * rng.choice([-1.0, 1.0], count)
        center_y = np.sort(rng.uniform(-60.0, 2400.0, count))
        total = count * per_object
        self._world = np.column_stack([
            np.repeat(center_x, per_object) + rng.normal(0.0, 0.12, total),
            np.repeat(center_y, per_object) + rng.normal(0.0, 0.12, total),
            GROUND_Z + np.tile(np.linspace(0.45, 2.2, per_object), count),
        ])
        self._rng = rng
        self._moving_car = moving_car
        self.frames = [_Frame(i * FRAME_INTERVAL) for i in range(int(duration / FRAME_INTERVAL))]

    @property
    def duration(self) -> float:
        return self.frames[-1].timestamp if self.frames else 0.0

    @property
    def start_unix_time(self) -> float:
        return 1782868176.0

    def nearest_frame(self, seconds: float) -> int:
        return int(min(max(round(seconds / FRAME_INTERVAL), 0), len(self.frames) - 1))

    def decode_frame(self, index: int, max_points: int | None = None) -> np.ndarray:
        t = self.frames[index].timestamp
        position = true_distance(t)
        relative = self._world[:, 1] - position
        near = (relative > -32.0) & (relative < 32.0)
        visible = self._world[near].copy()
        visible[:, 1] = relative[near]
        # 測距の揺らぎ（実データでも同じ物体の点が毎フレーム少しずれる）
        visible[:, :2] += self._rng.normal(0.0, 0.02, (visible.shape[0], 2))
        points = [visible]
        # 地面点。半径 2〜20m に散らす（毎フレーム別の位置＝Y の手掛かりにならない）
        radius = self._rng.uniform(2.0, 20.0, 2600)
        angle = self._rng.uniform(0.0, 2 * math.pi, 2600)
        points.append(np.column_stack([
            radius * np.cos(angle), radius * np.sin(angle),
            np.full(2600, GROUND_Z) + self._rng.normal(0.0, 0.01, 2600),
        ]))
        if self._moving_car:
            # 自転車を追い越す車（相対 +8m/s）。静止物より点数が多くても
            # 自車速度の推定が引きずられないことを確かめるために入れる。
            car_y = (t * 8.0) % 60.0 - 30.0
            points.append(np.column_stack([
                self._rng.uniform(1.4, 3.4, 900),
                car_y + self._rng.uniform(-2.4, 2.4, 900),
                GROUND_Z + self._rng.uniform(0.5, 1.6, 900),
            ]))
        return np.vstack(points)


class _Frame:
    def __init__(self, timestamp: float):
        self.timestamp = timestamp


def shifted_scene(rng, shift: float, count: int = 900) -> tuple[np.ndarray, np.ndarray]:
    """静止物だけの点群と、それを Y 方向に ``shift`` だけずらした点群。"""
    points = np.column_stack([
        rng.uniform(-9.0, 9.0, count),
        rng.uniform(-28.0, 28.0, count),
        rng.uniform(-0.8, 1.6, count),
    ])
    moved = points.copy()
    moved[:, 1] += shift
    return points, moved


# ── 占有格子とピーク検出 ────────────────────────────────────────────────────

def test_occupancy_grid_shape_and_binary():
    """格子は指定の分解能で、同じセルに何点入っても 1 になる。"""
    grid = sc.occupancy_grid(np.array([[0.0, 0.0, 0.0], [0.01, 0.01, 0.0]]),
                             x_bin=0.5, y_bin=0.1, max_x=10.0, max_y=30.0)
    assert grid.shape == (40, 600)
    assert grid.sum() == 1.0
    assert grid[20, 300] == 1.0
    print("  OK: 占有格子は二値で分解能どおり")


def test_occupancy_grid_drops_outside():
    """注目領域の外の点は捨てる。空の点群でも落ちない。"""
    grid = sc.occupancy_grid(np.array([[40.0, 0.0, 0.0], [0.0, 99.0, 0.0]]))
    assert grid.sum() == 0.0
    assert sc.occupancy_grid(np.zeros((0, 3))).sum() == 0.0
    print("  OK: 範囲外の点と空の点群を無視")


def test_refine_peak_interpolates():
    """離散のピークを放物線で補間して小数位置を返す。"""
    values = np.array([0.1, 0.5, 0.9, 0.8, 0.2])
    position, peak, prominence = sc.refine_peak(values, exclude=1)
    assert peak == 0.9
    assert 2.0 < position < 2.5          # 右隣が高いので右へずれる
    assert prominence > 0.4              # 山の外（0.5 と 0.2）との差
    print(f"  OK: 放物線補間でピーク位置 {position:.3f}")


def test_peak_width_measures_sharpness():
    """ピーク幅は相関が peak-drop を保つ区間の長さ。"""
    lags = np.arange(-5.0, 5.0, 0.05)
    sharp = np.exp(-(lags ** 2) / (2 * 0.5 ** 2))
    broad = np.exp(-(lags ** 2) / (2 * 2.0 ** 2))
    width_sharp = sc.peak_width(sharp, 0.05, drop=0.05)
    width_broad = sc.peak_width(broad, 0.05, drop=0.05)
    assert width_sharp < width_broad
    assert 0.2 < width_sharp < 1.0
    print(f"  OK: ピーク幅 鋭い {width_sharp:.2f} < 広い {width_broad:.2f}")


# ── 自車速度の推定 ──────────────────────────────────────────────────────────

def test_estimate_ego_speed_recovers_shift():
    """既知のずれ量を入れると ``速度 = -dy/dt`` が返る。"""
    rng = _rng()
    for speed in (3.0, 5.5, 8.25, 12.0):
        dt = 0.3
        points, moved = shifted_scene(rng, -speed * dt)
        result = sc.estimate_ego_speed(points, moved, dt)
        assert result["ok"], speed
        assert abs(result["speed"] - speed) < 0.25, (speed, result["speed"])
    print("  OK: 3.0〜12.0 m/s のずれを 0.25 m/s 以内で復元")


def test_estimate_ego_speed_subbin_precision():
    """格子（0.1m ≒ 0.33m/s）より細かいずれも放物線補間で拾える。"""
    rng = _rng(7)
    dt = 0.3
    errors = []
    for speed in np.arange(4.0, 9.0, 0.37):
        points, moved = shifted_scene(rng, -speed * dt)
        errors.append(sc.estimate_ego_speed(points, moved, dt)["speed"] - speed)
    assert max(abs(e) for e in errors) < 0.20
    print(f"  OK: サブビン精度 最大誤差 {max(abs(e) for e in errors):.3f} m/s")


def test_estimate_ego_speed_rejects_sparse():
    """点が少なすぎるフレーム対は ok=False にして使わない。"""
    result = sc.estimate_ego_speed(np.zeros((0, 3)), np.zeros((0, 3)), 0.3)
    assert not result["ok"] and math.isnan(result["speed"])
    few = np.column_stack([np.zeros(5), np.arange(5.0), np.zeros(5)])
    assert not sc.estimate_ego_speed(few, few, 0.3)["ok"]
    print("  OK: 点の少ないフレーム対を除外")


def test_ego_speed_series_tracks_true_speed():
    """合成した世界で、推定した自車速度が真の速度に追従する。"""
    pcap = FakePcap(duration=120.0)
    series = sc.ego_speed_series(pcap, sample_seconds=2.0, pair_step=3)
    usable = series["ok"] & np.isfinite(series["speed"])
    assert usable.mean() > 0.9, usable.mean()
    error = series["speed"][usable] - true_speed(series["time"][usable])
    rmse = float(np.sqrt(np.mean(error ** 2)))
    assert rmse < 0.30, rmse
    print(f"  OK: 自車速度のRMSE {rmse:.3f} m/s（{int(usable.sum())} サンプル）")


def test_ego_speed_survives_moving_vehicle():
    """通過車の点が混じっても自車速度は静止物側のピークを選ぶ。"""
    pcap = FakePcap(duration=90.0, moving_car=True)
    series = sc.ego_speed_series(pcap, sample_seconds=2.0, pair_step=3)
    usable = series["ok"] & np.isfinite(series["speed"])
    error = series["speed"][usable] - true_speed(series["time"][usable])
    assert float(np.sqrt(np.mean(error ** 2))) < 0.40
    print("  OK: 追い越し車が混ざっても自車速度を維持")


# ── 相互相関 ────────────────────────────────────────────────────────────────

def _series_pair(offset: float, noise: float = 0.0, seed: int = 3):
    """``pcap時刻 = gopro時刻 + offset`` の関係にある 2 本の速度時系列を作る。"""
    rng = _rng(seed)
    gps_time = np.arange(0.0, 600.0, 0.1)
    gps_speed = true_speed(gps_time)
    lidar_time = np.arange(60.0, 520.0, 1.0)
    lidar_speed = true_speed(lidar_time - offset)
    if noise:
        lidar_speed = lidar_speed + rng.normal(0.0, noise, lidar_time.size)
    return lidar_time, lidar_speed, gps_time, gps_speed


def test_cross_correlation_recovers_offset():
    """ずらした時系列から元のオフセットを 0.1 秒以内で復元する。"""
    for offset in (-58.8, -25.4, 0.0, 6.3):
        lidar_time, lidar_speed, gps_time, gps_speed = _series_pair(offset)
        result = sc.cross_correlate_offset(lidar_time, lidar_speed, gps_time, gps_speed,
                                           center=offset - 7.0, max_lag=20.0)
        assert abs(result["offset"] - offset) < 0.1, (offset, result["offset"])
        assert result["peak"] > 0.95
        assert result["prominence"] > 0.3
    print("  OK: 4通りのオフセットを 0.1 秒以内で復元")


def test_cross_correlation_is_robust_to_noise():
    """速度に 0.8 m/s の雑音が乗っても推定はほとんど動かない。"""
    offset = -30.0
    lidar_time, lidar_speed, gps_time, gps_speed = _series_pair(offset, noise=0.8)
    result = sc.cross_correlate_offset(lidar_time, lidar_speed, gps_time, gps_speed,
                                       center=offset + 5.0, max_lag=20.0)
    assert abs(result["offset"] - offset) < 0.5, result["offset"]
    assert result["peak"] > 0.6
    print(f"  OK: 雑音入りでも {result['offset']:+.2f} 秒（真値 {offset}）")


def test_cross_correlation_flags_unrelated_series():
    """無関係な時系列ではピークが低く、突出度も出ない。"""
    rng = _rng(11)
    gps_time = np.arange(0.0, 600.0, 0.1)
    gps_speed = true_speed(gps_time)
    lidar_time = np.arange(60.0, 520.0, 1.0)
    lidar_speed = rng.normal(6.0, 1.5, lidar_time.size)
    result = sc.cross_correlate_offset(lidar_time, lidar_speed, gps_time, gps_speed,
                                       center=0.0, max_lag=20.0)
    assert result["peak"] < sc.MIN_CORRELATION
    print(f"  OK: 無関係な系列はピーク {result['peak']:.2f} で信頼度低と分かる")


def test_cross_correlation_needs_enough_samples():
    """サンプルが少なすぎるときは推定せず None を返す。"""
    result = sc.cross_correlate_offset(np.arange(3.0), np.ones(3),
                                       np.arange(3.0), np.ones(3))
    assert result["offset"] is None
    print("  OK: サンプル不足では推定しない")


def test_acceleration_series_differentiates():
    """一定加速度の系列を微分すると一定値になる。"""
    times = np.arange(0.0, 60.0, 0.5)
    values = 2.0 + 0.35 * times
    grid, accel = sc.acceleration_series(times, values)
    assert grid.size > 100
    assert abs(float(np.mean(accel)) - 0.35) < 0.02
    print("  OK: 加速度系列が微分として正しい")


def test_acceleration_correlation_sharpens_peak():
    """加速度の相関は速度の相関より山が鋭い（位置を詰められる）。"""
    offset = -20.0
    lidar_time, lidar_speed, gps_time, gps_speed = _series_pair(offset, noise=0.3)
    speed_result = sc.cross_correlate_offset(lidar_time, lidar_speed, gps_time, gps_speed,
                                             center=offset, max_lag=20.0)
    lidar_accel_t, lidar_accel = sc.acceleration_series(lidar_time, lidar_speed)
    gps_accel_t, gps_accel = sc.acceleration_series(gps_time, gps_speed)
    accel_result = sc.cross_correlate_offset(lidar_accel_t, lidar_accel,
                                             gps_accel_t, gps_accel,
                                             center=speed_result["offset"], max_lag=5.0)
    assert accel_result["width"] < speed_result["width"]
    assert abs(accel_result["offset"] - offset) < 0.5
    print(f"  OK: 幅 速度 {speed_result['width']:.2f}s → 加速度 {accel_result['width']:.2f}s")


# ── 全経路（点群 → 速度 → オフセット） ─────────────────────────────────────

def test_end_to_end_offset_from_synthetic_pointcloud():
    """合成点群から作った自車速度と GPS 速度で、既知のオフセットを取り戻す。"""
    offset = -58.827
    pcap = FakePcap(duration=300.0, seed=99)
    series = sc.ego_speed_series(pcap, sample_seconds=1.0, pair_step=3)
    usable = series["ok"] & np.isfinite(series["speed"])
    gps_time = np.arange(-100.0, 400.0, 0.1)
    gps_speed = true_speed(gps_time + offset)   # gopro時刻 g ↔ pcap時刻 g + offset
    result = sc.cross_correlate_offset(series["time"][usable], series["speed"][usable],
                                       gps_time, gps_speed, center=offset + 6.0, max_lag=15.0)
    assert abs(result["offset"] - offset) < 0.3, result["offset"]
    assert result["peak"] > 0.9
    print(f"  OK: 点群からの推定 {result['offset']:+.3f} 秒（真値 {offset}）")


# ── GPS の読み込み ──────────────────────────────────────────────────────────

def _write_gps_book(path: Path, rows: list[tuple[float, float]]) -> None:
    """実際の書き出し関数で ``telemetry_gps.xlsx`` を作る。"""
    from extract_telemetry import _write_simple_xlsx

    headers_1 = ["cts", "date", "GPS (Lat.) [deg]", "GPS (Long.) [deg]", "GPS (Alt.) [m]",
                 "GPS (2D speed) [k/s]", "GPS (3D speed) [m/s]", "fix", "precision",
                 "altitude system"]
    headers_2 = list(headers_1)
    headers_2[5] = "GPS (2D speed) [m/s]"
    sheet_1 = [headers_1] + [[t, "", 0.0, 0.0, 0.0, v / 1000.0, v, 3, 300, ""] for t, v in rows]
    sheet_2 = [headers_2] + [[t, "", 0.0, 0.0, 0.0, v, v, 3, 300, ""] for t, v in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_simple_xlsx(path, [("1", sheet_1), ("2", sheet_2)])


def test_read_gps_speed_rows():
    """``telemetry_gps.xlsx`` の 2 枚目（m/s）を読む。"""
    rows = [(round(0.1 * i, 3), 5.0 + 0.01 * i) for i in range(40)]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "telemetry_gps.xlsx"
        _write_gps_book(path, rows)
        loaded = sc.read_gps_speed_rows(path)
    assert len(loaded) == len(rows)
    assert abs(loaded[0][0] - rows[0][0]) < 1e-6
    assert abs(loaded[-1][1] - rows[-1][1]) < 1e-6
    assert sc.read_gps_speed_rows(Path("存在しない.xlsx")) == []
    print("  OK: GPS の xlsx を m/s で読める")


def test_gps_speed_series_concatenates_chapters():
    """チャプター動画の GPS が 1 本の GoPro 経過時間軸につながる。"""
    original = sc.chapter_offsets
    with tempfile.TemporaryDirectory() as folder:
        out_dir = Path(folder)
        _write_gps_book(out_dir / "gps" / "GX010731" / "telemetry_gps.xlsx",
                        [(round(0.1 * i, 3), 5.0) for i in range(50)])
        _write_gps_book(out_dir / "gps" / "GX020731" / "telemetry_gps.xlsx",
                        [(round(0.1 * i, 3), 7.0) for i in range(50)])
        sc.chapter_offsets = lambda videos: ({"gx010731": 0.0, "gx020731": 712.7}, [])
        try:
            times, speeds, warnings = sc.gps_speed_series(out_dir, [])
        finally:
            sc.chapter_offsets = original
    assert times.size == 100
    assert np.all(np.diff(times) >= 0)          # 時刻順に並ぶ
    assert abs(times[50] - 712.7) < 1e-6        # 2本目は章の先頭ぶんずれる
    assert abs(speeds[-1] - 7.0) < 1e-9
    assert warnings == []
    print("  OK: チャプターをつないで時刻順の GPS 系列にする")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for test in tests:
        test()
    print("[test] 全テスト成功")
