"""LiDAR 点群から追い越し車両を自動追跡する。

GUI では追い越しイベント付近の LiDAR フレームを 1 枚ずつ表示し、対象車両を矩形で
囲って追跡している。本モジュールはその矩形列を自動生成する。出力は
``VelodynePcap.export_overtaking`` がそのまま受け取れる形式で、下流の処理
（``edit_csv.py`` の最近接点・フェーズ算出）は一切変更しない。

処理は次の順で行う。

1. 地面除去 …… 近傍の下向きビームから地面平面を当てはめ、地面付近と上空を捨てる
2. 注目領域の限定 …… ``|X| <= 8m``、``|Y| <= 30m`` の範囲だけを扱う
3. クラスタリング …… XY 平面のグリッド占有セルを連結成分に分ける
4. トラッキング …… クラスタ重心を等速度予測つき最近傍で対応づける
5. 追い越し車両の同定 …… 進行方向・横位置・寸法から追い越し車らしいトラックを選ぶ
6. 矩形生成 …… 各フレームの外接矩形にマージンを付け、欠測フレームは内挿する

座標系は ``docs/計算方法.md`` の 6 節と同じセンサ座標系（X=横方向、Y=進行方向）。
LiDAR は自転車上で動いているため点群は相対座標であり、静止物は自転車速度で
後退して見える。自車運動補正は行わない（下流の算出がすべて相対運動前提のため）。

コマンドラインからも実行できる。

    python lidar_autotrack.py <pcap> --first <frame> --last <frame> [--side right] [--dump-csv out.csv]

``--list-tracks`` で全トラックの軌跡と却下理由を、``--ascii <frame>`` でそのフレームの
上面図を表示できる（いずれも動作確認用）。

追い越しイベントが連続する車列では、1 つの追跡窓に複数の車が入る。呼び出し側が
``expected_pass_time``（そのイベントで車が横に並ぶはずの時刻）と
``exclude_track_keys``（他のイベントへ割り当て済みの車）を渡すと、スコア最大では
なく「期待時刻に最も近い、まだ使われていない車」を選ぶ。戻り値の ``candidates``
には必須条件を満たした全トラックの要約が入るので、呼び出し側で 1 対 1 の割り当てを
組める。
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


# --- 既定パラメータ -----------------------------------------------------------
# 注目領域（センサ座標系、m）。追い越し検知に必要な範囲だけを残して高速化する。
ROI_MAX_ABS_X = 8.0
ROI_MAX_ABS_Y = 30.0

# 地面除去。地面 + GROUND_MARGIN 以下を地面、地面 + HEIGHT_LIMIT 以上を上空とみなす。
GROUND_MARGIN = 0.20
HEIGHT_LIMIT = 3.0
GROUND_SEARCH_RADIUS = 20.0
GROUND_MIN_RADIUS = 2.0
# 地面候補にする最大仰角。VLP-16 の水平付近のビーム（-1度・+1度）は遠くの壁や
# 車体に当たって z≒0 に大量の点を作るため、下向きビームだけを地面候補にする。
GROUND_MAX_ELEVATION = -4.0
GROUND_INLIER = 0.35        # 平面当てはめの許容残差 (m)
GROUND_MAX_SLOPE = 0.20     # これを超える傾き（約11度）は平面当てはめ失敗とみなす
GROUND_Z_RANGE = (-4.0, 0.0)

# クラスタリング。セル 0.4m、1 セル分の隙間を跨いで連結する。
CELL_SIZE = 0.4
LINK_CELLS = 2
MIN_CLUSTER_POINTS = 6
# 連結しすぎたかたまりを細い連結でほどく条件（XY の外接矩形、m）。
# 追い越し車が真横に並ぶと、車体と自転車・搭乗者の隙間が 0.8m まで詰まり、
# 連結幅（0.4m × LINK_CELLS）を跨いで 1 つのかたまりになる。かたまりは大きい方
# （＝自車側）のトラックに取られるので車のトラックがそこで切れ、通過区間を抜ける
# 前に追跡が終わってフェーズ4が代用点になる（距離p3・相対速度・TTC が壊れる。
# 260701/ID3 の 545.4s・634.2s・842.8s・870.6s がこれ）。1 台の車としてありえない
# 大きさのかたまりに限り、連結を 1 セル狭めてほどき直す。値は score_track が
# 「車両らしくない」と判定する寸法（WIDTH_RANGE・LENGTH_RANGE の上限）に合わせる。
SPLIT_MAX_WIDTH = 4.0
SPLIT_MAX_LENGTH = 20.0

# トラッキング。VLP-16 は約 10Hz なので 1 フレーム約 0.1 秒。
GATE_DISTANCE = 3.0
MAX_MISSED_FRAMES = 3

# 対応づけの速度整合。等速度予測の最近傍だけで対応づけると、注目領域の遠端
# （|Y| ≒ 30m）で「遠ざかる静止物」と「近づいてくる追い越し車」がすれ違うときに
# 静止物のトラックが車へ乗り移る。遠端では点がまばらでクラスタが小さく、両者の
# 見かけの位置が 3m 以内まで近づくためで、乗り移ったトラックは前進と後退が
# 混ざって Y 速度がほぼ 0 になり、追い越し車として同定できなくなる
# （260701/ID1 の 395.1s がこれで取りこぼされていた）。
# 進行方向がはっきりしているトラックについては、進行方向を反転させる観測を
# 別の物体とみなして対応づけない。
# しきい値 12m/s の根拠(260701/ID1 の実測): クラスタ重心は見えている面が変わるだけで
# 揺れるため、同じ物体でも 1 フレームで 5〜6m/s の見かけの揺り戻しが出る。一方で
# 乗り移りは -4.6m/s のトラックへ +9.6m/s の観測が入る（差 14m/s）形で起きる。
# 6m/s まで下げると同じ物体のトラックまで細切れになり、断片が別の車に見えて誤検出が
# 増えたので、揺り戻しの実測幅より十分大きく、乗り移りより小さい 12m/s を採る。
VELOCITY_LOCK_FRAMES = 3      # 進行方向が確定したとみなす最小観測数
VELOCITY_LOCK_SPEED = 2.0     # 進行方向が確定したとみなす Y 速度 (m/s)
MAX_SPEED_REVERSAL = 12.0     # 反転を許す Y 速度差の上限 (m/s)

# 追い越し車両の同定条件
MIN_TRACK_FRAMES = 5
MIN_RELATIVE_SPEED = 1.0      # 自転車に対する前進速度 (m/s)
MIN_GROUND_SPEED = 3.0        # 静止物基準の車両速度 (m/s)
MIN_Y_PROGRESS = 4.0          # Y 方向の総移動量 (m)
# 通過区間のしきい値。docs/計算方法.md のフェーズ3・4と同じ |Y| = 3m。下流の
# フェーズ分類はこの区間を通る点を必要とするため、同定でも必須条件にする。
PASS_ZONE = 3.0
LATERAL_RANGE = (0.4, 6.0)    # 許容する |X| 中央値 (m)
LATERAL_IDEAL = (0.5, 5.0)    # 追い越し車らしい |X| 中央値 (m)
LENGTH_RANGE = (1.2, 20.0)    # 許容する車体長 (m)
LENGTH_IDEAL = (2.0, 18.0)
WIDTH_RANGE = (0.4, 4.0)      # 許容する車幅 (m)
WIDTH_IDEAL = (1.0, 3.0)

# 矩形生成
BOX_MARGIN = 0.3
MIN_BOX_SIZE = 1.0
# 一時的に隣の物体と繋がったフレームで矩形が膨らむのを防ぐ倍率。トラック全体の
# 寸法中央値のこの倍を超えた矩形は、重心を中心に切り詰める。
MAX_BOX_GROWTH = 2.0

# 静止物の見かけ速度がこの値を超えたら、その向きから前進方向の符号を決める。
EGO_SPEED_FOR_SIGN = 0.7

# track_key の照合許容。追跡窓が違うと同じ車でも通過フレーム・横位置が僅かにぶれる。
# VLP-16 は約 10Hz なので 3 フレーム = 約 0.3 秒。
TRACK_KEY_FRAME_TOLERANCE = 3
TRACK_KEY_LATERAL_TOLERANCE = 1.0


def _as_xyz(points) -> np.ndarray:
    """点列を ``(N, 3)`` の float 配列にそろえる。

    ``VelodynePcap.decode_frame`` が返す ``(x, y, z, intensity, laser_id)`` の
    リストと、``(N, 3)`` 以上の numpy 配列の両方を受け取れる。
    """
    array = np.asarray(points, dtype=float)
    if array.size == 0:
        return np.zeros((0, 3), dtype=float)
    if array.ndim != 2 or array.shape[1] < 3:
        raise ValueError("点群は (N, 3) 以上の配列か (x, y, z, ...) の列である必要があります")
    return array[:, :3]


# --- 1. 地面除去 --------------------------------------------------------------
def estimate_ground_plane(points, max_radius: float = GROUND_SEARCH_RADIUS,
                          min_radius: float = GROUND_MIN_RADIUS,
                          max_elevation: float = GROUND_MAX_ELEVATION,
                          inlier: float = GROUND_INLIER,
                          z_range: tuple[float, float] = GROUND_Z_RANGE
                          ) -> tuple[float, float, float] | None:
    """近傍の下向きビームから地面平面 ``z = a + b*X + c*Y`` を推定する。

    センサ取付高は未知なので、地面高はデータから求める。ここで水平付近のビーム
    （VLP-16 の -1 度・+1 度）を使うと、遠方の壁や車体に当たった点が ``z≒0`` に
    集中して地面と誤認されるため、仰角が ``max_elevation`` 度より下を向く点だけを
    地面候補にする。候補の下側分位を初期値として、残差 ``inlier`` m 以内の点で
    平面を 3 回当てはめ直す。自転車の傾きや路面のカントで地面は水平にならないため、
    定数ではなく平面で扱う。傾きが大きすぎる場合は当てはめ失敗とみなして定数に戻す。

    推定できない場合は ``None`` を返す。戻り値は ``(a, b, c)`` で、``a`` はセンサ
    直下の地面高。
    """
    xyz = _as_xyz(points)
    if len(xyz) == 0:
        return None
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    radius = np.hypot(x, y)
    near = (radius >= min_radius) & (radius <= max_radius) & (z >= z_range[0]) & (z <= z_range[1])
    downward = near & (np.degrees(np.arctan2(z, np.maximum(radius, 1e-6))) <= max_elevation)
    candidate = xyz[downward]
    if len(candidate) < 50:
        fallback = z[near]
        return (float(np.percentile(fallback, 10.0)), 0.0, 0.0) if fallback.size >= 20 else None

    cx, cy, cz = candidate[:, 0], candidate[:, 1], candidate[:, 2]
    # 初期値は半径帯ごとの下側 20% 分位の中央値。低い側だけを見て物体点を避ける。
    bands = []
    band_radius = np.hypot(cx, cy)
    for low in np.arange(min_radius, max_radius, 4.0):
        inside = (band_radius >= low) & (band_radius < low + 4.0)
        if inside.sum() >= 30:
            bands.append(float(np.percentile(cz[inside], 20.0)))
    plane = (float(np.median(bands)) if bands else float(np.percentile(cz, 20.0)), 0.0, 0.0)
    for _step in range(3):
        residual = cz - (plane[0] + plane[1] * cx + plane[2] * cy)
        inside = np.abs(residual) <= inlier
        if inside.sum() < 30:
            break
        design = np.column_stack([np.ones(inside.sum()), cx[inside], cy[inside]])
        solution, *_ = np.linalg.lstsq(design, cz[inside], rcond=None)
        if abs(solution[1]) > GROUND_MAX_SLOPE or abs(solution[2]) > GROUND_MAX_SLOPE:
            plane = (float(np.median(cz[inside])), 0.0, 0.0)
            break
        plane = (float(solution[0]), float(solution[1]), float(solution[2]))
    return plane


def estimate_ground_z(points, **options) -> float | None:
    """センサ直下の地面高（m）を返す。地面を推定できない場合は ``None``。"""
    plane = estimate_ground_plane(points, **options)
    return None if plane is None else plane[0]


def ground_height(plane: tuple[float, float, float], x, y):
    """地面平面の高さを座標 ``(x, y)`` で評価する。"""
    return plane[0] + plane[1] * x + plane[2] * y


def filter_points(points, plane: "tuple[float, float, float] | float | None" = None,
                  ground_margin: float = GROUND_MARGIN,
                  height_limit: float = HEIGHT_LIMIT,
                  max_abs_x: float = ROI_MAX_ABS_X,
                  max_abs_y: float = ROI_MAX_ABS_Y) -> np.ndarray:
    """注目領域の限定と地面除去を行い、残った点を ``(N, 3)`` で返す。

    ``plane`` は ``estimate_ground_plane`` の戻り値。数値を渡した場合は水平な地面
    高として扱う。省略した場合はこのフレームから推定する。地面を推定できない
    ときは高さフィルタを行わず、注目領域の限定だけを適用する。
    """
    xyz = _as_xyz(points)
    if len(xyz) == 0:
        return xyz
    keep = (np.abs(xyz[:, 0]) <= max_abs_x) & (np.abs(xyz[:, 1]) <= max_abs_y)
    xyz = xyz[keep]
    if len(xyz) == 0:
        return xyz
    if plane is None:
        plane = estimate_ground_plane(points)
    elif isinstance(plane, (int, float)):
        plane = (float(plane), 0.0, 0.0)
    if plane is None:
        return xyz
    ground = ground_height(plane, xyz[:, 0], xyz[:, 1])
    height = xyz[:, 2] - ground
    return xyz[(height > ground_margin) & (height < height_limit)]


# --- 2. クラスタリング --------------------------------------------------------
@dataclass
class Cluster:
    """1 フレーム内の 1 かたまりの点群。"""

    frame: int
    timestamp: float
    center_x: float
    center_y: float
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    count: int
    top_z: float = 0.0
    # このかたまりの足元の地面高（``detect_clusters`` が平面から埋める）。
    # 車高を「地面からの高さ」で測るために持つ。平面が無いフレームでは 0。
    ground_z: float = 0.0

    @property
    def top_height(self) -> float:
        """地面からの車体上端高（車高の目安、m）。

        ``filter_points`` が地面 + ``HEIGHT_LIMIT`` より上を捨てているため、この値は
        ``HEIGHT_LIMIT`` で頭打ちになる。乗用車と大型車を分けるには十分だが、
        大型車どうしの高さの差は測れない。
        """
        return self.top_z - self.ground_z

    @property
    def length(self) -> float:
        """Y 方向の広がり（車体長の目安、m）。"""
        return self.max_y - self.min_y

    @property
    def width(self) -> float:
        """X 方向の広がり（車幅の目安、m）。"""
        return self.max_x - self.min_x

    def bounds(self, margin: float = 0.0, min_size: float = 0.0) -> dict[str, float]:
        """外接矩形をマージン付きで返す。"""
        return _box(self.min_x, self.max_x, self.min_y, self.max_y, margin, min_size)


def _box(min_x: float, max_x: float, min_y: float, max_y: float,
         margin: float, min_size: float) -> dict[str, float]:
    """外接矩形にマージンと最小サイズを適用する。"""
    min_x, max_x = min_x - margin, max_x + margin
    min_y, max_y = min_y - margin, max_y + margin
    if max_x - min_x < min_size:
        mid = (min_x + max_x) * 0.5
        min_x, max_x = mid - min_size / 2, mid + min_size / 2
    if max_y - min_y < min_size:
        mid = (min_y + max_y) * 0.5
        min_y, max_y = mid - min_size / 2, mid + min_size / 2
    return {"min_x": round(float(min_x), 4), "max_x": round(float(max_x), 4),
            "min_y": round(float(min_y), 4), "max_y": round(float(max_y), 4)}


def _label_grid(cell_ids: np.ndarray, shape: tuple[int, int], link_cells: int) -> dict:
    """占有セルを連結成分に分け、``セル -> ラベル`` の辞書を返す。

    ``link_cells`` セル以内の隣接を同じ物体とみなす。2 にすると 1 セル分の
    隙間を跨いで連結でき、遠方でまばらになった車両点群がばらけにくい。
    """
    occupied = {int(cell): idx for idx, cell in enumerate(cell_ids)}
    labels: dict[int, int] = {}
    nx, ny = shape
    offsets = [(dx, dy)
               for dx in range(-link_cells, link_cells + 1)
               for dy in range(-link_cells, link_cells + 1)
               if dx or dy]
    label = 0
    for cell in occupied:
        if cell in labels:
            continue
        labels[cell] = label
        stack = [cell]
        while stack:
            current = stack.pop()
            cx, cy = divmod(current, ny)
            for dx, dy in offsets:
                ax, ay = cx + dx, cy + dy
                if not (0 <= ax < nx and 0 <= ay < ny):
                    continue
                neighbor = ax * ny + ay
                if neighbor in occupied and neighbor not in labels:
                    labels[neighbor] = label
                    stack.append(neighbor)
        label += 1
    return labels


def cluster_points(points, frame: int = 0, timestamp: float = 0.0,
                   cell_size: float = CELL_SIZE, link_cells: int = LINK_CELLS,
                   min_points: int = MIN_CLUSTER_POINTS,
                   max_width: float = SPLIT_MAX_WIDTH,
                   max_length: float = SPLIT_MAX_LENGTH) -> list[Cluster]:
    """XY 平面のグリッド連結成分で点群をクラスタに分ける。

    セル 0.3〜0.5m 程度の占有グリッドを作り、隣接セルを同じ物体としてまとめる。
    点数が ``min_points`` 未満のかたまりはノイズとして捨てる。

    1 台の車としてありえない大きさ（``max_width`` × ``max_length`` の外接矩形を
    超える）になったかたまりは、連結を 1 セル狭めてほどき直す。2 つ以上に分かれた
    ときだけ採用するので、本当に大きな 1 物体はそのまま残る。
    """
    xyz = _as_xyz(points)
    if len(xyz) == 0:
        return []
    x, y = xyz[:, 0], xyz[:, 1]
    ix = np.floor((x - x.min()) / cell_size).astype(np.int64)
    iy = np.floor((y - y.min()) / cell_size).astype(np.int64)
    nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
    cells = ix * ny + iy
    unique_cells, inverse = np.unique(cells, return_inverse=True)
    labels = _label_grid(unique_cells, (nx, ny), link_cells)
    cell_label = np.array([labels[int(cell)] for cell in unique_cells], dtype=np.int64)
    point_label = cell_label[inverse]

    order = np.argsort(point_label, kind="stable")
    sorted_labels = point_label[order]
    starts = np.flatnonzero(np.r_[True, sorted_labels[1:] != sorted_labels[:-1]])
    groups = np.split(order, starts[1:])

    clusters: list[Cluster] = []
    for group in groups:
        if len(group) < min_points:
            continue
        member = xyz[group]
        mx, my = member[:, 0], member[:, 1]
        span_x = float(mx.max() - mx.min())
        span_y = float(my.max() - my.min())
        if link_cells > 1 and (span_x > max_width or span_y > max_length):
            parts = cluster_points(member, frame=frame, timestamp=timestamp,
                                   cell_size=cell_size, link_cells=link_cells - 1,
                                   min_points=min_points, max_width=max_width,
                                   max_length=max_length)
            if len(parts) > 1:
                clusters.extend(parts)
                continue
        clusters.append(Cluster(
            frame=frame, timestamp=timestamp,
            center_x=float(mx.mean()), center_y=float(my.mean()),
            min_x=float(mx.min()), max_x=float(mx.max()),
            min_y=float(my.min()), max_y=float(my.max()),
            count=int(len(member)), top_z=float(member[:, 2].max()),
        ))
    return clusters


def detect_clusters(points, frame: int = 0, timestamp: float = 0.0, **options) -> list[Cluster]:
    """1 フレーム分の生点群から、地面除去とクラスタリングまでをまとめて行う。"""
    plane = options.pop("plane", None)
    filtered = filter_points(
        points,
        plane=plane,
        ground_margin=options.pop("ground_margin", GROUND_MARGIN),
        height_limit=options.pop("height_limit", HEIGHT_LIMIT),
        max_abs_x=options.pop("max_abs_x", ROI_MAX_ABS_X),
        max_abs_y=options.pop("max_abs_y", ROI_MAX_ABS_Y),
    )
    clusters = cluster_points(filtered, frame=frame, timestamp=timestamp, **options)
    if plane is not None:
        # 車高は「地面からの高さ」で測る。地面は路面のカントや自転車の傾きで
        # 場所ごとに違うので、かたまりの重心位置の平面高を足元とみなす。
        for cluster in clusters:
            cluster.ground_z = float(ground_height(plane, cluster.center_x, cluster.center_y))
    return clusters


# --- 3. トラッキング ----------------------------------------------------------
@dataclass
class Track:
    """フレームをまたいで対応づけたクラスタ列。"""

    track_id: int
    clusters: list[Cluster] = field(default_factory=list)
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    missed: int = 0

    @property
    def first_frame(self) -> int:
        return self.clusters[0].frame

    @property
    def last_frame(self) -> int:
        return self.clusters[-1].frame

    @property
    def frame_span(self) -> int:
        return self.last_frame - self.first_frame + 1

    def predict(self, timestamp: float) -> tuple[float, float]:
        """等速度モデルで次フレームの重心位置を予測する。"""
        last = self.clusters[-1]
        dt = max(0.0, timestamp - last.timestamp)
        return last.center_x + self.velocity_x * dt, last.center_y + self.velocity_y * dt

    def append(self, cluster: Cluster) -> None:
        """観測を追加し、速度を平滑更新する。"""
        if self.clusters:
            last = self.clusters[-1]
            dt = cluster.timestamp - last.timestamp
            if dt > 1e-6:
                vx = (cluster.center_x - last.center_x) / dt
                vy = (cluster.center_y - last.center_y) / dt
                if len(self.clusters) == 1:
                    self.velocity_x, self.velocity_y = vx, vy
                else:
                    self.velocity_x = 0.5 * self.velocity_x + 0.5 * vx
                    self.velocity_y = 0.5 * self.velocity_y + 0.5 * vy
        self.clusters.append(cluster)
        self.missed = 0


def velocity_compatible(track: Track, cluster: Cluster,
                        max_reversal: float = MAX_SPEED_REVERSAL,
                        lock_frames: int = VELOCITY_LOCK_FRAMES,
                        lock_speed: float = VELOCITY_LOCK_SPEED) -> bool:
    """観測がトラックの進行方向と両立するかを判定する。

    進行方向が確定したトラック（``lock_frames`` 回以上観測され、Y 速度の大きさが
    ``lock_speed`` 以上）に対して、Y 速度の符号を反転させ、かつ速度差が
    ``max_reversal`` を超える観測は別の物体とみなして ``False`` を返す。

    注目領域の遠端では点がまばらで、遠ざかる静止物と近づいてくる追い越し車の
    クラスタが数メートルまで近づく。距離だけで対応づけると静止物のトラックが
    車へ乗り移り、前進と後退が混ざって追い越し車として同定できなくなる。
    実際の車両は 0.1 秒で進行方向を反転できないので、この判定で切り分けられる。
    クラスタ重心は見えている面が変わるだけで揺れるため、同じ物体でも数 m/s の
    揺り戻しは出る。それで細切れにしないよう ``max_reversal`` は広めに取る。
    """
    if len(track.clusters) < lock_frames or abs(track.velocity_y) < lock_speed:
        return True
    last = track.clusters[-1]
    dt = cluster.timestamp - last.timestamp
    if dt <= 1e-6:
        return True
    speed_y = (cluster.center_y - last.center_y) / dt
    if speed_y * track.velocity_y >= 0:
        return True
    return abs(speed_y - track.velocity_y) <= max_reversal


class Tracker:
    """クラスタ重心を等速度予測つき最近傍で対応づける簡易トラッカー。"""

    def __init__(self, gate: float = GATE_DISTANCE, max_missed: int = MAX_MISSED_FRAMES,
                 max_reversal: float = MAX_SPEED_REVERSAL):
        self.gate = gate
        self.max_missed = max_missed
        self.max_reversal = max_reversal
        self.active: list[Track] = []
        self.finished: list[Track] = []
        self._next_id = 0

    def update(self, clusters: list[Cluster], timestamp: float | None = None) -> None:
        """1 フレーム分のクラスタでトラックを更新する。"""
        if timestamp is None:
            timestamp = clusters[0].timestamp if clusters else 0.0
        pairs = []
        for ti, track in enumerate(self.active):
            px, py = track.predict(timestamp)
            for ci, cluster in enumerate(clusters):
                distance = math.hypot(cluster.center_x - px, cluster.center_y - py)
                if distance <= self.gate and velocity_compatible(
                        track, cluster, self.max_reversal):
                    pairs.append((distance, ti, ci))
        pairs.sort()
        used_tracks: set[int] = set()
        used_clusters: set[int] = set()
        for _distance, ti, ci in pairs:
            if ti in used_tracks or ci in used_clusters:
                continue
            used_tracks.add(ti)
            used_clusters.add(ci)
            self.active[ti].append(clusters[ci])
        for ci, cluster in enumerate(clusters):
            if ci in used_clusters:
                continue
            track = Track(track_id=self._next_id)
            self._next_id += 1
            track.append(cluster)
            self.active.append(track)
        still_active: list[Track] = []
        for ti, track in enumerate(self.active):
            if ti in used_tracks or track.clusters[-1].timestamp == timestamp:
                still_active.append(track)
                continue
            track.missed += 1
            (still_active if track.missed <= self.max_missed else self.finished).append(track)
        self.active = still_active

    def tracks(self) -> list[Track]:
        """未終了のものも含めた全トラックを返す。"""
        return sorted(self.finished + self.active, key=lambda t: t.track_id)


def build_tracks(frames: list[tuple[int, float, list[Cluster]]],
                 gate: float = GATE_DISTANCE, max_missed: int = MAX_MISSED_FRAMES,
                 max_reversal: float = MAX_SPEED_REVERSAL) -> list[Track]:
    """``(フレーム番号, 時刻, クラスタ列)`` の並びからトラックを構築する。"""
    tracker = Tracker(gate=gate, max_missed=max_missed, max_reversal=max_reversal)
    for _index, timestamp, clusters in frames:
        tracker.update(clusters, timestamp)
    return tracker.tracks()


# --- 4. 追い越し車両の同定 ----------------------------------------------------
@dataclass
class TrackSummary:
    """トラック 1 本の軌跡特徴。同定と confidence の算出に使う。"""

    track: Track
    frames: int
    span: int
    speed_x: float
    speed_y: float
    y_start: float
    y_end: float
    y_progress: float
    min_abs_y: float
    median_abs_x: float
    median_x: float
    length: float
    width: float
    monotonic: float
    continuity: float
    entered_zone: bool
    passed_zone: bool
    points: int
    # 通過区間（|Y| < 3m）へ入った時刻とフレーム番号。下流のフェーズ3と同じ定義なので、
    # 動画イベントの期待通過時刻との照合にそのまま使える。区間へ入っていなければ None。
    pass_time: float | None = None
    pass_frame: int | None = None
    # 通過区間にいる間の最小 |X|（離隔距離 LC の目安、m）。隣車線の車を弾くのに使う。
    lateral_min: float = float("inf")
    # 車高の目安（地面からの上端高の 80 パーセンタイル、m）。車種の推定に使う。
    height: float = 0.0
    # 真横（|Y| <= PASS_ZONE）を通っている間の車体長の最大値（m）。遠方では車体の
    # 一部しか見えないため、車種の推定には真横の値のほうが素直に効く。
    near_length: float = 0.0

    @property
    def track_key(self) -> str:
        """同一車両を指す識別子。

        イベントごとに追跡窓が違うため、窓で切られる開始フレームでは同じ車が別の
        キーになってしまう。窓に依存しない「通過区間へ入った絶対フレーム番号」と
        横位置から決めることで、窓が違っても同じ車は同じキーになる。窓の端で
        僅かにぶれても同じ車と分かるよう、照合は ``track_key_matches`` で行う。
        """
        if self.pass_frame is None:
            return f"t{self.track.first_frame}x{self.median_x:+.1f}"
        return f"f{self.pass_frame}x{self.median_x:+.1f}"


def _fit_speed(times: np.ndarray, values: np.ndarray) -> float:
    """時刻に対する 1 次回帰の傾き（速度、m/s）を返す。"""
    if times.size < 2 or float(times.max() - times.min()) < 1e-6:
        return 0.0
    return float(np.polyfit(times, values, 1)[0])


def _passing_visit(inside: np.ndarray, gap: np.ndarray) -> tuple[int, int] | None:
    """通過区間（``|Y| < PASS_ZONE``）への滞在のうち、追い越しにあたる区間を返す。

    戻り値は ``[start, stop)`` の添字。区間へ一度も入っていなければ ``None``。

    ふつうトラックは通過区間へ 1 回しか入らないので、そのときの戻り値は
    「最初に入った滞在」＝従来と同じで、値は 1 つも変わらない。2 回以上入るのは
    トラックが別の物体へ乗り移ったときで（``MAX_SPEED_REVERSAL`` は速度差が
    大きい乗り移りしか止められない。路肩の静止物からゆっくり近づく車へ移る場合の
    速度差は、同じ物体でも起きる見かけの揺り戻しと同じ 5〜6m/s 程度しかない）、
    このとき最初の滞在を採ると通過時刻が数秒早くなり、イベントとの割り当てが
    外れる（260701/ID5 の 574.3s がこれで取りこぼされていた。路肩の物体が
    ``|Y|=2m`` をかすめた 567.4s が通過時刻になっていた）。

    どちらが追い越しかは「矩形が ``Y=0`` を跨いだか」＝自転車の真横に並んだかで
    決まる。かすめただけの滞在は跨がないので、跨いだ滞在を優先して選ぶ。
    """
    if not inside.any():
        return None
    edges = np.flatnonzero(np.diff(inside.astype(np.int8)))
    starts = [0] if inside[0] else []
    stops: list[int] = []
    for edge in edges:
        (starts if inside[edge + 1] else stops).append(int(edge) + 1)
    if len(stops) < len(starts):
        stops.append(len(inside))
    visits = list(zip(starts, stops))
    beside = [visit for visit in visits if np.any(gap[visit[0]:visit[1]] <= 0.0)]
    return beside[0] if beside else visits[0]


def summarize_track(track: Track) -> TrackSummary:
    """トラックの軌跡特徴をまとめる。"""
    times = np.array([c.timestamp for c in track.clusters], dtype=float)
    xs = np.array([c.center_x for c in track.clusters], dtype=float)
    ys = np.array([c.center_y for c in track.clusters], dtype=float)
    steps = np.diff(ys)
    if steps.size:
        forward = float(np.sum(steps > 0)), float(np.sum(steps < 0))
        monotonic = max(forward) / steps.size
    else:
        monotonic = 0.0
    lengths = np.array([c.length for c in track.clusters], dtype=float)
    widths = np.array([c.width for c in track.clusters], dtype=float)
    # 通過区間までの距離は重心ではなく矩形の Y 範囲で測る。下流のフェーズ分類は
    # 矩形内の点の Y を見るため、重心が 3m の外にあっても点は区間へ入りうる。
    low = np.array([c.min_y for c in track.clusters], dtype=float)
    high = np.array([c.max_y for c in track.clusters], dtype=float)
    gap = np.where((low <= 0) & (high >= 0), 0.0, np.minimum(np.abs(low), np.abs(high)))
    inside = gap < PASS_ZONE
    entered = bool(inside.any())
    # 通過時刻は「|Y| < 3m へ入った」時刻。edit_csv のフェーズ3と同じ定義。
    # 1本のトラックが区間へ2回以上入ることがある（乗り移り）ので、どの滞在を
    # 通過とみなすかは _passing_visit が決める。
    visit = _passing_visit(inside, gap)
    heights = np.array([c.top_height for c in track.clusters], dtype=float)
    pass_time = pass_frame = None
    lateral_min = float("inf")
    near_length = 0.0
    passed = False
    if visit is not None:
        start, stop = visit
        passed = bool(np.any(gap[start + 1:] >= PASS_ZONE))
        pass_time = float(times[start])
        pass_frame = int(track.clusters[start].frame)
        near_length = float(np.max(lengths[start:stop]))
        # 通過区間にいる間の最小 |X|。矩形が原点を跨ぐフレームは 0 とみなす。
        lateral_min = min(
            0.0 if c.min_x <= 0 <= c.max_x else min(abs(c.min_x), abs(c.max_x))
            for c in track.clusters[start:stop]
        )
    return TrackSummary(
        track=track, frames=len(track.clusters), span=track.frame_span,
        speed_x=_fit_speed(times, xs), speed_y=_fit_speed(times, ys),
        y_start=float(ys[0]), y_end=float(ys[-1]),
        y_progress=float(ys[-1] - ys[0]), min_abs_y=float(gap.min()),
        median_abs_x=float(np.median(np.abs(xs))), median_x=float(np.median(xs)),
        length=float(np.percentile(lengths, 80)), width=float(np.percentile(widths, 80)),
        monotonic=monotonic, continuity=len(track.clusters) / max(1, track.frame_span),
        entered_zone=entered, passed_zone=passed,
        points=int(sum(c.count for c in track.clusters)),
        pass_time=pass_time, pass_frame=pass_frame, lateral_min=lateral_min,
        height=float(np.percentile(heights, 80)) if heights.size else 0.0,
        near_length=near_length,
    )


def estimate_forward_sign(summaries: list[TrackSummary]) -> tuple[int, float]:
    """静止物の見かけ速度から、Y 軸の前進方向の符号を推定する。

    自転車が前進している間、静止物（路駐車・壁・電柱）は相対的に自転車速度で
    後退して見える。トラックの Y 速度の中央値を静止物の見かけ速度とみなし、その
    逆向きを前進方向とする。自転車が停止していて符号を決められない場合は、
    ``docs/計算方法.md`` の座標定義どおり +Y を前進方向とする。

    戻り値は ``(前進方向の符号, 静止物の見かけ Y 速度)``。
    """
    speeds = [s.speed_y for s in summaries if s.frames >= 3]
    if not speeds:
        return 1, 0.0
    ego = float(np.median(speeds))
    if abs(ego) < EGO_SPEED_FOR_SIGN:
        return 1, ego
    return (-1 if ego > 0 else 1), ego


def _side_sign(side: str) -> float:
    """``side`` に対応する X の符号を返す（Velodyne 標準向きでは右が +X）。

    センサ取付向きによっては符号が反転しうるため、この値は confidence と警告に
    使うだけで、採否の判定には使わない。
    """
    return -1.0 if str(side).lower() == "left" else 1.0


def _taper(value: float, low: float, high: float, ideal_low: float, ideal_high: float) -> float:
    """``ideal`` 範囲内なら 1、``low``〜``high`` の外なら 0 になる評価値を返す。"""
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        return max(0.0, (value - low) / max(1e-6, ideal_low - low))
    return max(0.0, (high - value) / max(1e-6, high - ideal_high))


def score_track(summary: TrackSummary, forward: int, ego_speed_y: float,
                side: str = "right") -> tuple[float, list[str]]:
    """トラックが追い越し車両らしいかを 0..1 で評価する。

    追い越し車は次を満たす。

    - 自転車より速く前進する（相対 Y 速度が前進方向へ十分大きい）
    - 通過区間（``|Y| < 3m`` 付近）を後方から前方へ横切る
    - 自転車の片側におり、``|X|`` がおおむね 0.5〜5m
    - クラスタ寸法が車両らしい（長さ 2〜18m、幅 1〜3m 程度）

    条件を満たさない場合は ``0.0`` と却下理由を返す。X の符号と ``side`` の対応は
    センサ取付向きに依存して確定できないため、符号は confidence に反映するだけで
    採否には使わない。
    """
    reasons: list[str] = []
    relative = summary.speed_y * forward           # 自転車に対する前進速度
    ground = (summary.speed_y - ego_speed_y) * forward  # 静止物基準の車両速度
    progress = summary.y_progress * forward

    if summary.frames < MIN_TRACK_FRAMES:
        reasons.append("追跡フレーム数が不足")
    if relative < MIN_RELATIVE_SPEED:
        reasons.append("前進方向への相対速度が不足")
    if ground < MIN_GROUND_SPEED:
        reasons.append("静止物基準の速度が不足（静止物の可能性）")
    if progress < MIN_Y_PROGRESS:
        reasons.append("Y 方向の移動量が不足")
    if not summary.entered_zone:
        reasons.append("通過区間（|Y| < 3m）へ入っていない")
    if not (LATERAL_RANGE[0] <= summary.median_abs_x <= LATERAL_RANGE[1]):
        reasons.append("横位置 |X| が範囲外")
    if not (LENGTH_RANGE[0] <= summary.length <= LENGTH_RANGE[1]):
        reasons.append("クラスタ長が車両らしくない")
    if not (WIDTH_RANGE[0] <= summary.width <= WIDTH_RANGE[1]):
        reasons.append("クラスタ幅が車両らしくない")
    if reasons:
        return 0.0, reasons

    monotonic = summary.monotonic
    continuity = min(1.0, summary.continuity)
    size = 0.5 * _taper(summary.length, *LENGTH_RANGE, *LENGTH_IDEAL) \
        + 0.5 * _taper(summary.width, *WIDTH_RANGE, *WIDTH_IDEAL)
    lateral = _taper(summary.median_abs_x, *LATERAL_RANGE, *LATERAL_IDEAL)
    crossing = 1.0 if summary.passed_zone else 0.5
    speed = _taper(ground, 1.0, 45.0, 3.0, 30.0)
    side_match = 1.0 if summary.median_x * _side_sign(side) > 0 else 0.0

    score = (0.24 * continuity + 0.24 * monotonic + 0.18 * size
             + 0.14 * lateral + 0.10 * crossing + 0.05 * speed + 0.05 * side_match)
    return float(min(1.0, max(0.0, score))), []


def candidate_summary(summary: TrackSummary, score: float) -> dict:
    """呼び出し側の割り当て・診断用に、トラック 1 本の要約を辞書で返す。

    ``length`` / ``width`` / ``height`` / ``near_length`` は点群から測った車体寸法で、
    呼び出し側が車種を推定するのに使う（キーを足すだけなので既存の利用側は無改造）。
    """
    return {
        "track_key": summary.track_key,
        "pass_time": summary.pass_time,
        "score": round(float(score), 3),
        "median_x": round(float(summary.median_x), 3),
        "lc_estimate": (None if summary.lateral_min == float("inf")
                        else round(float(summary.lateral_min), 3)),
        "length": round(float(summary.length), 3),
        "width": round(float(summary.width), 3),
        "height": round(float(summary.height), 3),
        "near_length": round(float(summary.near_length), 3),
        "points": int(summary.points),
    }


def _parse_track_key(key: str) -> tuple[str, int, float] | None:
    """``track_key`` を ``(種別, フレーム番号, 横位置)`` に分解する。"""
    text = str(key)
    if not text or text[0] not in "ft" or "x" not in text:
        return None
    head, _, tail = text[1:].partition("x")
    try:
        return text[0], int(head), float(tail)
    except ValueError:
        return None


def track_key_matches(key: str, other: str,
                      frame_tolerance: int = TRACK_KEY_FRAME_TOLERANCE,
                      lateral_tolerance: float = TRACK_KEY_LATERAL_TOLERANCE) -> bool:
    """2 つの ``track_key`` が同じ車を指すかを判定する。

    追跡窓が違うと、同じ車でも通過区間へ入ったと判定されるフレームや横位置の
    中央値が僅かにぶれる。完全一致だけで比べると同じ車を別物と扱ってしまうため、
    フレーム・横位置の近さで照合する。
    """
    if key == other:
        return True
    left, right = _parse_track_key(key), _parse_track_key(other)
    if left is None or right is None or left[0] != right[0]:
        return False
    return (abs(left[1] - right[1]) <= frame_tolerance
            and abs(left[2] - right[2]) <= lateral_tolerance)


def rank_candidates(scored: list[tuple[float, TrackSummary]],
                    expected_pass_time: float | None = None) -> list[tuple[float, TrackSummary]]:
    """採用順にトラックを並べ替える。

    ``expected_pass_time`` を指定しない場合はスコアの大きい順。指定した場合は
    「通過時刻が期待時刻に近い順」で、同じ近さならスコアの大きい順にする。車列で
    複数の車が 1 つの窓に入るとき、スコア最大の車が必ずしもそのイベントの車とは
    限らないため、期待時刻を優先する。
    """
    if expected_pass_time is None:
        return sorted(scored, key=lambda item: -item[0])
    return sorted(scored, key=lambda item: (
        abs((item[1].pass_time if item[1].pass_time is not None else 0.0) - expected_pass_time),
        -item[0],
    ))


def select_overtaking(tracks: list[Track], side: str = "right",
                      expected_pass_time: float | None = None,
                      exclude_track_keys=None) -> dict:
    """トラック群から追い越し車両を 1 本選ぶ。

    ``expected_pass_time``（pcap 相対秒）を渡すと、必須条件を満たすトラックのうち
    通過時刻がその時刻に最も近いものを選ぶ。``exclude_track_keys`` に他イベントへ
    割り当て済みのキーを渡すと、それを除いた次点を選ぶ（車列の2台目を拾うため）。

    戻り値は ``{"summary", "score", "forward", "ego_speed_y", "candidates", "warnings"}``。
    ``candidates`` は必須条件を満たした全トラックの要約（除外したものも含む）。
    候補が無ければ ``summary`` は ``None``。
    """
    summaries = [summarize_track(t) for t in tracks if t.clusters]
    forward, ego = estimate_forward_sign(summaries)
    warnings: list[str] = []
    if abs(ego) < EGO_SPEED_FOR_SIGN:
        warnings.append("静止物の見かけ速度が小さく、自転車が停止中の可能性があります（+Yを前進方向と仮定）")
    scored = []
    for summary in summaries:
        score, _reasons = score_track(summary, forward, ego, side)
        if score > 0.0:
            scored.append((score, summary))
    ranked = rank_candidates(scored, expected_pass_time)
    candidates = [candidate_summary(summary, score) for score, summary in ranked]
    empty = {"summary": None, "score": 0.0, "forward": forward, "ego_speed_y": ego,
             "candidates": candidates, "warnings": warnings}
    if not ranked:
        return empty

    excluded = [str(key) for key in (exclude_track_keys or ())]
    available = [item for item in ranked
                 if not any(track_key_matches(item[1].track_key, key) for key in excluded)]
    if not available:
        warnings.append(
            f"追い越し車らしい候補{len(ranked)}件はすべて他のイベントへ割り当て済みです"
        )
        return empty

    if expected_pass_time is None:
        if len(available) > 1 and available[0][0] - available[1][0] < 0.05:
            warnings.append(f"追い越し車らしい候補が{len(available)}件あり、評価値が接近しています")
    else:
        rival = [item for item in available[1:]
                 if abs((item[1].pass_time or 0.0) - expected_pass_time)
                 - abs((available[0][1].pass_time or 0.0) - expected_pass_time) < 1.0]
        if rival:
            warnings.append(
                f"期待通過時刻 {expected_pass_time:.1f}s の近くに候補が{len(rival) + 1}件あります"
            )
    best_score, best = available[0]
    if best.median_x * _side_sign(side) <= 0:
        warnings.append("選択した車両の X 符号が side 指定と一致しません（座標系の向きに注意）")
    if not best.passed_zone:
        warnings.append("通過区間（|Y| < 3m）を抜けるまで追えていません（フェーズ4が求まらない可能性）")
    return {"summary": best, "score": best_score, "forward": forward, "ego_speed_y": ego,
            "candidates": candidates, "warnings": warnings}


# --- 5. 矩形生成 --------------------------------------------------------------
def _limited_bounds(cluster: Cluster, limit_x: float, limit_y: float,
                    margin: float, min_size: float) -> dict[str, float]:
    """隣の物体と繋がって膨らんだ矩形を、重心を中心に上限寸法まで切り詰める。

    離隔距離は矩形内の最近接点から求めるため、他の物体を巻き込んだ矩形をそのまま
    使うと距離が過小評価されうる。トラック全体の寸法から外れた矩形だけを抑える。
    """
    min_x, max_x, min_y, max_y = cluster.min_x, cluster.max_x, cluster.min_y, cluster.max_y
    if max_x - min_x > limit_x:
        min_x = max(min_x, cluster.center_x - limit_x / 2)
        max_x = min(max_x, cluster.center_x + limit_x / 2)
    if max_y - min_y > limit_y:
        min_y = max(min_y, cluster.center_y - limit_y / 2)
        max_y = min(max_y, cluster.center_y + limit_y / 2)
    return _box(min_x, max_x, min_y, max_y, margin, min_size)


def track_to_frames(track: Track, margin: float = BOX_MARGIN,
                    min_size: float = MIN_BOX_SIZE,
                    first_frame: int | None = None,
                    last_frame: int | None = None,
                    max_growth: float = MAX_BOX_GROWTH) -> list[dict]:
    """トラックから ``export_overtaking`` 用のフレーム別矩形列を作る。

    観測が無いフレーム（一時的なロスト）は前後の矩形から線形内挿する。一時的に
    隣の物体と繋がって膨らんだフレームの矩形は、トラック全体の寸法中央値の
    ``max_growth`` 倍まで切り詰める。
    """
    observed = {c.frame: c for c in track.clusters}
    order = sorted(observed)
    start = order[0] if first_frame is None else max(order[0], first_frame)
    end = order[-1] if last_frame is None else min(order[-1], last_frame)
    keys = ("min_x", "max_x", "min_y", "max_y")
    limit_x = max(min_size, float(np.median([c.width for c in track.clusters])) * max_growth)
    limit_y = max(min_size, float(np.median([c.length for c in track.clusters])) * max_growth)
    frames: list[dict] = []
    for index in range(start, end + 1):
        cluster = observed.get(index)
        if cluster is not None:
            bounds = _limited_bounds(cluster, limit_x, limit_y, margin, min_size)
        else:
            before = max((f for f in order if f < index), default=None)
            after = min((f for f in order if f > index), default=None)
            if before is None or after is None:
                continue
            lo = _limited_bounds(observed[before], limit_x, limit_y, margin, min_size)
            hi = _limited_bounds(observed[after], limit_x, limit_y, margin, min_size)
            ratio = (index - before) / (after - before)
            bounds = {k: round(lo[k] + (hi[k] - lo[k]) * ratio, 4) for k in keys}
        frames.append({"index": int(index), "bounds": bounds})
    return frames


def scan_frames(pcap, first: int, last: int) -> dict:
    """PCAP のフレーム区間を走査し、トラックと診断値を返す。

    戻り値は ``{"tracks", "warnings", "ground_z", "frames", "elapsed"}``。
    地面平面はフレームごとに推定し、推定できなかったフレームでは直近 10 フレームの
    中央値で代用する。
    """
    tracker = Tracker()
    warnings: list[str] = []
    planes: list[tuple[float, float, float]] = []
    started = time.perf_counter()
    count = 0
    for index in range(first, last + 1):
        try:
            points = pcap.decode_frame(index)
        except Exception as exc:  # 壊れたフレームは飛ばして続行する
            warnings.append(f"フレーム{index}の読み込みに失敗しました: {exc}")
            continue
        timestamp = pcap.frames[index].timestamp
        plane = estimate_ground_plane(points)
        if plane is not None:
            planes.append(plane)
        elif planes:
            plane = tuple(np.median(np.array(planes[-10:]), axis=0))
        clusters = detect_clusters(points, frame=index, timestamp=timestamp, plane=plane)
        tracker.update(clusters, timestamp)
        count += 1
    if not planes:
        warnings.append("地面を推定できませんでした（地面除去なしで処理しました）")
    return {"tracks": tracker.tracks(), "warnings": warnings, "frames": count,
            "ground_z": float(np.median([p[0] for p in planes])) if planes else None,
            "elapsed": time.perf_counter() - started}


# --- 6. 公開 API --------------------------------------------------------------
def _failed(warnings: list[str], candidates: list[dict] | None = None) -> dict:
    """追跡できなかったときの戻り値。キーの構成は成功時と同じにそろえる。"""
    return {"ok": False, "frames": [], "confidence": 0.0, "warnings": warnings,
            "pass_time": None, "track_key": None, "candidates": candidates or []}


def track_overtaking(pcap, first_frame: int, last_frame: int, side: str = "right",
                     expected_pass_time: float | None = None,
                     exclude_track_keys=None) -> dict:
    """PCAP のフレーム区間から追い越し車両を追跡し、フレーム別矩形を返す。

    ``pcap`` は ``VelodynePcap`` インスタンス。戻り値は次の形式で、``frames`` は
    ``VelodynePcap.export_overtaking`` にそのまま渡せる。

        {"ok": bool, "frames": [{"index": int, "bounds": {...}}],
         "confidence": float, "warnings": [str],
         "pass_time": float | None, "track_key": str | None,
         "candidates": [{"track_key", "pass_time", "score", "median_x", "lc_estimate",
                         "length", "width", "height", "near_length", "points"}]}

    ``expected_pass_time``（pcap 相対秒）を渡すと、必須条件を満たすトラックのうち
    通過時刻（``|Y| < 3m`` を横断する時刻）がその時刻に最も近いものを選ぶ。省略時は
    従来どおりスコア最大のトラックを選ぶ。``exclude_track_keys`` に他イベントへ
    割り当て済みの ``track_key`` を渡すと、それを除いた次点を選ぶ。

    ``candidates`` には必須条件を満たした全トラックの要約が採用順に入る（除外した
    ものも含む）。呼び出し側はこれを使ってイベントと車両の 1 対 1 割り当てを組める。

    追い越し車両が見つからない場合は ``ok=False``、``frames=[]`` を返す。
    """
    warnings: list[str] = []
    total = len(getattr(pcap, "frames", []) or [])
    if total == 0:
        return _failed(["LiDARフレームがありません"])
    first = max(0, min(int(first_frame), int(last_frame)))
    last = min(total - 1, max(int(first_frame), int(last_frame)))
    if last <= first:
        return _failed(["フレーム区間が不正です"])

    scan = scan_frames(pcap, first, last)
    warnings.extend(scan["warnings"])
    result = select_overtaking(scan["tracks"], side=side,
                               expected_pass_time=expected_pass_time,
                               exclude_track_keys=exclude_track_keys)
    warnings.extend(result["warnings"])
    candidates = result["candidates"]
    summary = result["summary"]
    if summary is None:
        warnings.append("追い越し車両らしいトラックが見つかりませんでした")
        return _failed(warnings, candidates)
    frames = track_to_frames(summary.track, first_frame=first, last_frame=last)
    if not frames:
        warnings.append("矩形を生成できませんでした")
        return _failed(warnings, candidates)
    if len(frames) < MIN_TRACK_FRAMES:
        warnings.append("追跡できたフレーム数が少なく、結果が不安定な可能性があります")
    return {"ok": True, "frames": frames, "confidence": round(float(result["score"]), 3),
            "warnings": warnings, "pass_time": summary.pass_time,
            "track_key": summary.track_key, "candidates": candidates}


# --- コマンドライン -----------------------------------------------------------
def _dump_csv(path: Path, frames: list[dict]) -> None:
    """生成した矩形列をCSVへ書き出す（目視確認用）。"""
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("index", "min_x", "max_x", "min_y", "max_y", "center_x", "center_y"))
        for frame in frames:
            b = frame["bounds"]
            writer.writerow((frame["index"], b["min_x"], b["max_x"], b["min_y"], b["max_y"],
                             round((b["min_x"] + b["max_x"]) / 2, 3),
                             round((b["min_y"] + b["max_y"]) / 2, 3)))


def _ascii_view(pcap, index: int, bounds: dict[str, float] | None = None,
                width: int = 33, height: int = 41) -> str:
    """1 フレームの上面図を ASCII で描く（地面除去の確認用）。

    ``#`` は地面除去後に残った点、``*`` は選ばれた矩形の中の点、``+`` は自転車
    （原点）。左が -X、上が +Y。
    """
    points = filter_points(pcap.decode_frame(index))
    grid = [["." for _ in range(width)] for _ in range(height)]
    for x, y, _z in points:
        column = int((x + ROI_MAX_ABS_X) / (2 * ROI_MAX_ABS_X) * (width - 1))
        row = int((ROI_MAX_ABS_Y - y) / (2 * ROI_MAX_ABS_Y) * (height - 1))
        if not (0 <= column < width and 0 <= row < height):
            continue
        inside = bounds is not None and (
            bounds["min_x"] <= x <= bounds["max_x"] and bounds["min_y"] <= y <= bounds["max_y"])
        if inside or grid[row][column] == ".":
            grid[row][column] = "*" if inside else "#"
    grid[(height - 1) // 2][(width - 1) // 2] = "+"
    header = f"フレーム{index}: 横 -8m〜+8m / 縦 +30m〜-30m  (#=点 *=選択矩形 +=自転車)"
    return header + "\n" + "\n".join("".join(row) for row in grid)


def _describe(summary: TrackSummary, forward: int, ego: float, score: float) -> str:
    """トラック 1 本の軌跡概要を 1 行にまとめる。"""
    return (f"track {summary.track.track_id:3d} "
            f"frames {summary.track.first_frame}-{summary.track.last_frame} ({summary.frames}点) "
            f"Y {summary.y_start:+6.1f}->{summary.y_end:+6.1f} "
            f"vY {summary.speed_y:+6.2f}m/s (対地 {(summary.speed_y - ego) * forward:+5.1f}) "
            f"X中央 {summary.median_x:+5.2f} 長{summary.length:4.1f}m 幅{summary.width:4.1f}m "
            f"score {score:.3f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LiDAR点群から追い越し車両を自動追跡する")
    parser.add_argument("pcap", help="Velodyne PCAP ファイル")
    parser.add_argument("--first", type=int, required=True, help="開始フレーム番号")
    parser.add_argument("--last", type=int, required=True, help="終了フレーム番号")
    parser.add_argument("--side", default="right", choices=("right", "left"), help="追い越し側")
    parser.add_argument("--dump-csv", default="", help="生成した矩形列のCSV出力先")
    parser.add_argument("--list-tracks", action="store_true", help="全トラックの軌跡概要を表示する")
    parser.add_argument("--expected-pass-time", type=float, default=None, metavar="SEC",
                        help="期待通過時刻(pcap相対秒)。最も近い通過時刻のトラックを選ぶ")
    parser.add_argument("--exclude-track", action="append", default=[], metavar="KEY",
                        help="割り当て済みの track_key を除外する（複数指定可）")
    parser.add_argument("--ascii", type=int, default=None, metavar="FRAME",
                        help="指定フレームの上面図をASCIIで表示する（地面除去の確認用）")
    args = parser.parse_args(argv)

    from lidar_pcap import VelodynePcap

    started = time.perf_counter()
    pcap = VelodynePcap(Path(args.pcap))
    print(f"索引: {pcap.model_name} {len(pcap.frames)}フレーム "
          f"{pcap.duration:.1f}秒 ({time.perf_counter() - started:.1f}秒)")
    first = max(0, min(args.first, args.last))
    last = min(len(pcap.frames) - 1, max(args.first, args.last))
    scan = scan_frames(pcap, first, last)
    selection = select_overtaking(scan["tracks"], side=args.side,
                                  expected_pass_time=args.expected_pass_time,
                                  exclude_track_keys=args.exclude_track)
    ground = scan["ground_z"]
    print(f"走査: フレーム{first}-{last} {scan['frames']}枚 {scan['elapsed']:.1f}秒 "
          f"({scan['elapsed'] / max(1, scan['frames']) * 1000:.0f}ms/フレーム) "
          f"トラック{len(scan['tracks'])}本 "
          f"地面高 {'不明' if ground is None else f'{ground:.2f}m'} "
          f"前進方向 {'+Y' if selection['forward'] > 0 else '-Y'} "
          f"静止物見かけ速度 {selection['ego_speed_y']:+.2f}m/s")

    if args.list_tracks:
        forward, ego = selection["forward"], selection["ego_speed_y"]
        rows = []
        for track in scan["tracks"]:
            if len(track.clusters) < 3:
                continue
            summary = summarize_track(track)
            score, reasons = score_track(summary, forward, ego, args.side)
            rows.append((score, _describe(summary, forward, ego, score)
                         + ("" if score > 0 else " / " + "、".join(reasons[:2]))))
        for _score, line in sorted(rows, key=lambda item: -item[0])[:25]:
            print("  " + line)

    if selection["candidates"]:
        print(f"候補: {len(selection['candidates'])}本（採用順）")
        for candidate in selection["candidates"][:10]:
            pass_time = candidate["pass_time"]
            print(f"  {candidate['track_key']:>14} 通過 "
                  f"{'不明' if pass_time is None else f'{pass_time:8.2f}s'} "
                  f"score {candidate['score']:.3f} LC目安 {candidate['lc_estimate']}")

    warnings = list(scan["warnings"]) + list(selection["warnings"])
    summary = selection["summary"]
    if summary is None:
        print("追い越し車両を検出できませんでした")
        for warning in warnings + ["追い越し車両らしいトラックが見つかりませんでした"]:
            print("  警告: " + warning)
        if args.ascii is not None:
            print(_ascii_view(pcap, args.ascii))
        return 1
    frames = track_to_frames(summary.track, first_frame=first, last_frame=last)
    if not frames:
        print("矩形を生成できませんでした")
        return 1
    result = {"ok": True, "frames": frames,
              "confidence": round(float(selection["score"]), 3), "warnings": warnings,
              "pass_time": summary.pass_time, "track_key": summary.track_key}
    print(f"検出: {len(frames)}フレーム (フレーム{frames[0]['index']}-{frames[-1]['index']}) "
          f"confidence {result['confidence']:.3f} "
          f"track {summary.track_key} 通過 "
          f"{'不明' if summary.pass_time is None else f'{summary.pass_time:.2f}s'}")
    for warning in result["warnings"]:
        print("  警告: " + warning)
    if args.dump_csv:
        _dump_csv(Path(args.dump_csv), frames)
        print(f"矩形列を書き出しました: {args.dump_csv}")
    if args.ascii is not None:
        selected = next((f for f in frames if f["index"] == args.ascii), None)
        print(_ascii_view(pcap, args.ascii, selected["bounds"] if selected else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
