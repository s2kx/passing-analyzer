"""最近接点の軌跡 edit.csv を生成する。

旧 ``VolodyneConverter`` のモード2（``DisCreateClass.main``）を Python に忠実移植したもの。
VeloView 形式のフレームごと CSV（1 ファイル = 1 時刻、ファイル名 = unixtime、
XYZ 座標が 9, 10, 11 列目）が入ったフォルダを受け取り、各フレームでスキャナ原点に
最も近い点を追跡して ``unixtime, X, Y, Z, distance(m), speed(km/h)`` の軌跡 CSV を書き出す。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 埋め込みPython（portable/python）は実行したスクリプトの場所を import の起点にしないので、
# 隣のモジュール（src/）を読めるよう自分のフォルダを足す。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape
from zipfile import ZIP_DEFLATED, ZipFile


# 旧プログラムの出力ヘッダと完全一致させる
EDIT_HEADER = ("unixtime", "X", "Y", "Z", "distance(m)", "speed(km/h)")
LC_MIN_DISTANCE = 1.0

# フェーズ3・4を切り分ける Y のしきい値[m]（docs/計算方法.md 8章）。
PHASE_Y_THRESHOLD = 3.0

# フェーズ2候補として妥当な速度の上限[km/h]。最近接点が車体上を移り変わると
# speed 列にスパイクが出るため、これを超える点は候補から外す。
PHASE2_MAX_SPEED_KMH = 100.0

# フェーズ2候補として認める |Y| の上限[m]。フェーズ2は「近接区間に入る直前の
# 基準点」なので、遠方の点まで候補にすると追跡が長いフレームほど遠い点が選ばれ、
# 距離p2 と TTC が系統的に過大になる。ID1(10件)とID3(9件)の正解との照合を合わせた
# 19件で、この上限を 5.5m にしたとき距離p2の平均絶対差が最小（1.44m）になる。
PHASE2_MAX_ABS_Y = 5.5

# 路面点の除去。矩形選択は車両の外接箱に余裕を足した範囲なので、車体の左右外側の
# 路面がわずかに入り込む。この路面点が最近接点になると離隔距離LCが小さめに出る。
# フレーム内の Z 下端（外れ値に強い下側パーセンタイル）から GROUND_MARGIN 以下を
# 路面とみなして落とす。点数が少ないフレームは下端を推定できないので何もしない。
GROUND_PERCENTILE = 0.02
GROUND_MARGIN = 0.10
GROUND_MIN_POINTS = 30

# 自車構造物（センサ架台・搭乗者）の除去。LiDARは自転車に載っているので、
# 原点のごく近傍に「ほぼ同じ位置に留まり続ける点の環」が常時返ってくる。実データ
# （ID1〜ID5の全 edit CSV 計10418点）ではこの環は 0.48〜0.50m に密集し、次に近い
# 点は 1.0m 以上で、0.5〜1.0m には1点も無い。追い越し車がセンサ横を通り過ぎて
# しまうと、この環がフレーム内の最近接点になって軌跡を乗っ取り、フェーズ3〜4が
# 作れなくなる（ID3候補056・ID4候補011の崩壊原因）。環と実車の間のギャップに
# しきい値を置き、これより近い点は最近接点の候補から外す。
EGO_MIN_DISTANCE = 0.8

# 自車構造物の第2層。搭乗者の体・自転車のフレームは 0.8〜2m にも点を返す。こちらは
# 距離だけでは追い越し車（LC 1.0m 前後もある）と区別できないが、通過側の反対側にしか
# 出ない。追い越し車は車体まるごと通過側にあるので、通過側でない点は候補にしない。
# 通過側は遠方フレームのXの符号の多数決で決め、多数派がこの比率に満たない（＝どちらの
# 側とも言えない）ときは側による除去をしない。
PASSING_SIDE_MIN_RATIO = 0.6

# 最近接点が1フレームで動ける上限速度[m/s]。最近接点は車体上を移り変わるので車速より
# ずっと速く動くことがある（実データの approach 区間で最大 75km/h ≒ 21m/s）。ここを
# 超える移動は別物体への乗り移りなので、その時点で追跡は失われたとみなし、以降の
# フレームは空行にする（1台の車の軌跡が途中で別物体にすり替わるのを防ぐ）。
MAX_STEP_SPEED_MPS = 30.0

# XYZ 座標の列位置（0 始まり）。旧プログラムの注記「XYZ座標は9,10,11列目」に対応。
COL_X, COL_Y, COL_Z = 8, 9, 10

# ファイル名末尾の数値（タイムスタンプ）を取り出す。
_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

# このツール自身の出力など、フレームCSVでないものは対象から除外する。
_NON_FRAME = re.compile(r"(?:_edit$|^overtaking_)", re.IGNORECASE)


def _fmt(value: float) -> str:
    """.NET Framework の ``double.ToString()``（有効数字 15 桁 = G15）に合わせて数値を文字列化する。"""
    return format(value, ".15g")


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _frame_time(path: Path) -> float:
    """ファイル名（拡張子なし）から unixtime を取り出す。

    純粋な数値名（``1779258541.02``）はそのまま。``lidar_000001_000000.020`` の
    ような名前は末尾の数値（``000000.020``）を時刻として採用する。数値が無ければ
    ``ValueError`` を送出する。
    """
    stem = path.stem
    try:
        return float(stem)
    except ValueError:
        matches = _NUMBER.findall(stem)
        if matches:
            return float(matches[-1])
        raise ValueError(f"ファイル名から時刻を取得できません: {path.name}")


def _frame_files(folder: Path) -> list[tuple[float, Path]]:
    """フォルダ内のフレームCSVを (時刻, パス) で返す（時刻順、非対象は除外）。"""
    items: list[tuple[float, Path]] = []
    for path in folder.glob("*.csv"):
        if _NON_FRAME.search(path.stem):
            continue
        try:
            items.append((_frame_time(path), path))
        except ValueError:
            continue  # 時刻として読めない名前のCSVはスキップ
    items.sort(key=lambda item: item[0])
    return items


def _read_points(path: Path) -> list[list[str]]:
    """フレーム CSV を読み込み、ヘッダ 1 行を飛ばして各行（点）を返す。

    旧 ``CSVIO.readCSVString(path, header_flg: true)`` 相当。BOM 付き / なしの
    どちらの UTF-8 でも読めるよう ``utf-8-sig`` で開く。
    """
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        next(reader, None)  # ヘッダを読み飛ばす
        for row in reader:
            rows.append(row)
    return rows


def _drop_ground_points(points: list[list[str]], col_z: int) -> list[list[str]]:
    """1フレーム分の点から路面付近の点を落とす。

    フレーム内の Z の下側パーセンタイル（``GROUND_PERCENTILE``）を路面高さとみなし、
    そこから ``GROUND_MARGIN`` 以内の点を除く。点数が ``GROUND_MIN_POINTS`` 未満の
    フレームは路面高さを推定できないため、また全点が落ちてしまう場合も、元の点を
    そのまま返す（車両を見失うより路面が混じるほうが安全なため）。
    """
    if len(points) < GROUND_MIN_POINTS:
        return points
    heights = []
    for p in points:
        if len(p) <= col_z:
            continue
        try:
            heights.append(float(p[col_z]))
        except ValueError:
            continue
    if len(heights) < GROUND_MIN_POINTS:
        return points
    heights.sort()
    ground = heights[int(GROUND_PERCENTILE * (len(heights) - 1))]
    limit = ground + GROUND_MARGIN
    kept = []
    for p in points:
        if len(p) <= col_z:
            continue
        try:
            if float(p[col_z]) >= limit:
                kept.append(p)
        except ValueError:
            continue
    return kept or points


def _nearest_point(points: list[list[str]], col_x: int, col_y: int, col_z: int,
                   accept=None) -> tuple[float, float, float, list[str]] | None:
    """1フレームの点から最近接点を選び ``(距離, X, Y, 点)`` を返す。該当なしは None。

    ``points`` は路面点を落とした後の1フレーム分の点。``accept(x, y, dist)`` を渡すと、
    それが偽を返す点は候補から外す。自車構造物や軌跡の飛びを弾くのに使う。距離
    ``EGO_MIN_DISTANCE`` 未満の点は常に外す。
    """
    best: tuple[float, float, float, list[str]] | None = None
    for p in points:
        if len(p) <= col_z:
            continue
        try:
            x = float(p[col_x])
            y = float(p[col_y])
        except ValueError:
            continue                 # 数値でない点（空欄・見出し行など）はスキップ
        dist = math.sqrt(x * x + y * y)
        if dist < EGO_MIN_DISTANCE:
            continue                 # センサ架台・搭乗者そのもの
        if accept is not None and not accept(x, y, dist):
            continue
        if best is None or dist < best[0]:
            best = (dist, x, y, p)
    return best


def _passing_side(bests) -> float:
    """最近接点の列から車が通過した側（+1: X>0 / -1: X<0 / 0: 判定不能）を返す。

    自車構造物は原点のごく近くにしか無いので、|Y| が ``PHASE_Y_THRESHOLD`` 以上の
    フレーム（＝遠方＝汚染されない区間）の X の符号だけで判定する。多数派が
    ``PASSING_SIDE_MIN_RATIO`` に満たないときは 0（＝側による除去をしない）。
    """
    xs = [best[1] for best in bests if best is not None and abs(best[2]) >= PHASE_Y_THRESHOLD]
    if not xs:
        return 0.0
    positives = sum(1 for x in xs if x > 0)
    negatives = sum(1 for x in xs if x < 0)
    if max(positives, negatives) < PASSING_SIDE_MIN_RATIO * len(xs):
        return 0.0
    if positives == negatives:
        return 1.0 if sum(xs) >= 0 else -1.0
    return 1.0 if positives > negatives else -1.0


def _anchor_frame(frames, bests) -> int | None:
    """追跡の起点にするフレーム番号を返す。有効な点が1つも無ければ None。

    フレーム毎の最近接点を並べ、隣り合うフレームの移動が ``MAX_STEP_SPEED_MPS`` に
    収まる区間（＝同じ物体を見続けている区間）に切り分け、最長の区間の最接近フレームを
    起点にする。単純に「一番近いフレーム」を起点にすると、車が通り過ぎた後に残る
    自車構造物（数フレームだけ最近接になる）を掴んでしまう。
    """
    runs: list[list[int]] = []
    for i, best in enumerate(bests):
        if best is None:
            continue
        prev = bests[i - 1] if i > 0 else None
        if runs and prev is not None and runs[-1][-1] == i - 1:
            step = math.hypot(best[1] - prev[1], best[2] - prev[2])
            if step <= MAX_STEP_SPEED_MPS * abs(frames[i][0] - frames[i - 1][0]):
                runs[-1].append(i)
                continue
        runs.append([i])
    if not runs:
        return None
    # 長さ優先、同じ長さならより近くまで寄った区間を選ぶ
    run = max(runs, key=lambda r: (len(r), -min(bests[i][0] for i in r)))
    return min(run, key=lambda i: bests[i][0])


def _trajectory_rows(frames, col_x: int = COL_X, col_y: int = COL_Y, col_z: int = COL_Z) -> list[list[str]]:
    """フレーム列から edit.csv 行（ヘッダ込み）を作る共通ロジック。

    ``frames`` は時刻順に並んだ ``(t: float, time_text: str, points: list[list[str]])`` の列。
    旧 ``DisCreateClass.main`` を忠実に再現する:
      * 各フレームで距離 = √(X² + Y²) が最小の点（最近接点）を採用
      * 速度[km/h] = 前フレーム最近接点からの移動距離 / 経過秒 × 3600 / 1000
      * 先頭フレームの速度は空欄
      * 有効点が無いフレームは時刻のみ、X..speed は空欄

    旧実装に対して足したのは次の3つで、どれも「最近接点が追い越し車以外へ乗り移る」
    のを防ぐためのものである（``docs/計算方法.md`` 7.2）。
      * ``_drop_ground_points``: 車体の外側に残る路面点（LCが小さめに出る）
      * 自車構造物の除去: ``EGO_MIN_DISTANCE`` 未満の点と、通過側でない側の点
      * 連続性ゲート: 最接近フレームを起点に前後へ、1フレームで
        ``MAX_STEP_SPEED_MPS`` を超えて飛ばない範囲だけ軌跡を伸ばす
    """
    out: list[list[str]] = [list(EDIT_HEADER)]
    if not frames:
        return out

    # 路面点の除去はフレームごとに一度だけ行い、以降はその結果を使い回す。
    cleaned = [_drop_ground_points(points, col_z) for _, _, points in frames]

    # 1回目: 自車構造物（近すぎる点）だけを外して最近接点を求め、通過側を決める。
    side = _passing_side([_nearest_point(points, col_x, col_y, col_z)
                          for points in cleaned])

    def accept(x: float, y: float, dist: float) -> bool:
        # 通過側と反対側の点は搭乗者・自転車・対向物。追い越し車ではありえない。
        return not (side and x * side < 0)

    # 2回目: 通過側に限って最近接点を求め、それが連続している最長の区間（＝追い越し車。
    # 自車構造物や矩形に紛れ込んだ物体は短い区間にしかならない）の最接近フレームを
    # 追跡の起点にする。
    bests = [_nearest_point(points, col_x, col_y, col_z, accept) for points in cleaned]
    anchor = _anchor_frame(frames, bests)
    chain: dict[int, tuple[float, float, float, list[str]]] = {}
    if anchor is not None:
        chain[anchor] = bests[anchor]
        # 起点から前後へ、連続性を保てる間だけ伸ばす。途切れたらそこで打ち切る
        # （追い越し車は途中で消えないので、その先に現れるのは別物体）。
        for step in (1, -1):
            prev = (frames[anchor][0], chain[anchor][1], chain[anchor][2])
            i = anchor + step
            while 0 <= i < len(frames):
                t = frames[i][0]
                base_t, base_x, base_y = prev
                limit = MAX_STEP_SPEED_MPS * abs(t - base_t)
                best = _nearest_point(
                    cleaned[i], col_x, col_y, col_z,
                    lambda x, y, dist: (accept(x, y, dist)
                                        and math.hypot(x - base_x, y - base_y) <= limit),
                )
                if best is None:
                    break
                chain[i] = best
                prev = (t, best[1], best[2])
                i += step

    prev_t = frames[0][0]           # 直前フレームの時刻（最初のフレームで初期化）
    prev_x = 0.0                    # 直前フレーム最近接点 X
    prev_y = 0.0                    # 直前フレーム最近接点 Y

    for i, (t, time_text, points) in enumerate(frames):
        row = [time_text, "", "", "", "", ""]
        best = chain.get(i)
        if best is not None:
            dist, x, y, p = best
            row[1] = p[col_x]                     # X（元の文字列をそのまま）
            row[2] = p[col_y]                     # Y
            row[3] = p[col_z]                     # Z
            row[4] = _fmt(dist)                   # distance(m)
            if i > 0 and (t - prev_t) != 0:
                speed = math.sqrt((x - prev_x) ** 2 + (y - prev_y) ** 2) / (t - prev_t) * 3600.0 / 1000.0
                row[5] = _fmt(speed)              # speed(km/h)

        out.append(row)

        # 次フレームの速度計算用に「直前」を更新（旧実装どおり、空行は原点扱い）
        prev_t = t
        prev_x = float(row[1]) if row[1] != "" else 0.0
        prev_y = float(row[2]) if row[2] != "" else 0.0

    return out


def build_edit_rows(folder: Path) -> list[list[str]]:
    """フォルダ内の全フレーム CSV（1 ファイル = 1 時刻）から edit.csv 行を作る。"""
    frames = []
    for t, path in _frame_files(folder):
        # unixtime 列: 純粋な数値ファイル名はそのまま（旧仕様と一致）、それ以外は数値化した時刻
        time_text = path.stem if _is_number(path.stem) else _fmt(t)
        frames.append((t, time_text, _read_points(path)))
    return _trajectory_rows(frames)


def build_edit_rows_from_file(csv_path: Path) -> list[list[str]]:
    """単一の複数フレーム CSV（GUI の ``overtaking_<id>.csv`` 等）から edit.csv 行を作る。

    ``Frame`` 列でフレームを区切り、``Time`` 列を時刻として扱う。列位置はヘッダ名から
    判定し（``Frame`` / ``Time`` / ``Points:0/1/2``）、無ければ既定（0,1,8,9,10）を使う。
    """
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows:
        return [list(EDIT_HEADER)]

    header = rows[0]

    def col(name: str, default: int) -> int:
        return header.index(name) if name in header else default

    frame_i = col("Frame", 0)
    time_i = col("Time", 1)
    x_i, y_i, z_i = col("Points:0", COL_X), col("Points:1", COL_Y), col("Points:2", COL_Z)
    width = max(x_i, y_i, z_i)

    # Frame ごとに点をまとめる（出現順を保持し、最後に時刻順へ並べ替える）
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for r in rows[1:]:
        if len(r) <= width:
            continue
        key = r[frame_i]
        bucket = grouped.get(key)
        if bucket is None:
            try:
                t = float(r[time_i])
            except ValueError:
                continue
            bucket = {"t": t, "time_text": r[time_i], "points": []}
            grouped[key] = bucket
            order.append(key)
        bucket["points"].append(r)

    frames = sorted((grouped[k] for k in order), key=lambda b: b["t"])
    frame_tuples = [(b["t"], b["time_text"], b["points"]) for b in frames]
    return _trajectory_rows(frame_tuples, x_i, y_i, z_i)


def write_edit_csv(target_path: Path) -> Path:
    """フォルダ／単一CSV のどちらからでも ``<名前>_edit.csv`` を生成し、そのパスを返す。

    * フォルダ … 中のフレーム別CSVを処理。出力は ``親/フォルダ名_edit.csv``
    * 単一CSV（``overtaking_<id>.csv`` 等）… 出力は ``同階層/ファイル名_edit.csv``
    """
    target_path = Path(target_path)

    if target_path.is_dir():
        rows = build_edit_rows(target_path)
        out_path = target_path.parent / f"{target_path.name}_edit.csv"
    elif target_path.is_file():
        rows = build_edit_rows_from_file(target_path)
        out_path = target_path.with_name(f"{target_path.stem}_edit.csv")
    else:
        raise ValueError(f"パスが存在しません: {target_path}")

    if len(rows) <= 1:
        raise ValueError(
            "フレームが見つかりません。追い越し記録CSV（overtaking_*.csv）か、"
            "ファイル名が時刻のフレーム別CSVが入ったフォルダを選んでください。"
        )

    with out_path.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows(rows)
    return out_path


@dataclass
class EditPoint:
    row_number: int
    unixtime_text: str
    unixtime: float
    x: float
    y: float
    z_text: str
    distance: float
    speed: float | None
    raw: list[str]
    phase: str = ""


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_edit_points(edit_path: Path) -> list[EditPoint]:
    with Path(edit_path).open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    points: list[EditPoint] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) < 6:
            continue
        t, x, y, distance = _to_float(row[0]), _to_float(row[1]), _to_float(row[2]), _to_float(row[4])
        if t is None or x is None or y is None or distance is None:
            continue
        points.append(EditPoint(
            row_number=row_number,
            unixtime_text=row[0],
            unixtime=t,
            x=x,
            y=y,
            z_text=row[3],
            distance=distance,
            speed=_to_float(row[5]),
            raw=row[:6],
        ))
    return points


def _mark_phase(point: EditPoint, label: str) -> None:
    """フェーズ名を付ける。フォールバックで1点が2フェーズを兼ねる場合は連結する。"""
    point.phase = f"{point.phase}/{label}" if point.phase else label


def _pick_phase2(candidates: list[EditPoint]) -> EditPoint:
    """フェーズ2候補から1点を選ぶ（速度が一桁の最後の点、無ければ速度絶対値が最小の点）。"""
    single_digit = [point for point in candidates if 0 <= abs(point.speed or 0) < 10]
    return single_digit[-1] if single_digit else min(candidates, key=lambda point: abs(point.speed or 0))


def lateral_clearance(section: list[EditPoint]) -> tuple[float, list[str]]:
    """通過区間から離隔距離 LC を求め、(LC, 警告) を返す。

    通過区間の X は本来どちらか一方の側（車が抜けていった側）に偏る。1点だけ
    反対符号の外れ値が混ざると単純な最小値が外れ値で決まってしまうため、
    点数の多い側（同数なら重心の側）に限定してから |X| の最小値を採る。
    """
    notes: list[str] = []
    xs = [point.x for point in section]
    negatives = sum(1 for x in xs if x < 0)
    positives = sum(1 for x in xs if x > 0)
    if positives == negatives:
        passes_right = sum(xs) >= 0      # 同数のときは重心の符号で通過側を決める
    else:
        passes_right = positives > negatives
    side_text = "X>0側" if passes_right else "X<0側"
    same_side = [x for x in xs if (x >= 0 if passes_right else x <= 0)]
    dropped = len(xs) - len(same_side)
    if dropped:
        notes.append(
            f"離隔距離LC: 通過側({side_text})と符号が逆のX外れ値 {dropped} 点を除外しました"
        )
    return min(abs(x) for x in same_side), notes


def classify_phases(
    points: list[EditPoint],
    strict: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, EditPoint]:
    """edit.csv の有効点からフェーズ1〜4を決める。

    ``strict=True``（既定・従来互換）は該当点が無ければ ``ValueError`` を送出する。
    ``strict=False`` では全自動処理向けに、可能な範囲でフォールバックして
    ``warnings`` に品質警告を積む。

    * フェーズ2が無い（|Y| < 3 になる前に speed 値のある点が無い）
      → フェーズ3直前の有効点をフェーズ2として代用
    * フェーズ4が無い（フェーズ3以降で |Y| >= 3 に戻らない）
      → 最終有効点をフェーズ4として代用
    * フェーズ3が無い（|Y| < 3 が一度も無い）→ 計算不能なので strict によらず例外

    フェーズ2の候補は ``|Y| <= PHASE2_MAX_ABS_Y`` の点に限る（該当が無ければ制限
    なしに戻して警告）。この制限は strict の値によらず常に効く。
    """
    notes = warnings if warnings is not None else []
    if not points:
        raise ValueError("分析対象のedit.csvに有効な行がありません")

    for point in points:
        point.phase = ""
    p1 = points[0]
    _mark_phase(p1, "フェーズ1")

    # フェーズ3: 最初に |Y| < 3 になる点。これが無いと通過区間が作れない。
    p3_index = next(
        (i for i, point in enumerate(points) if abs(point.y) < PHASE_Y_THRESHOLD), None
    )
    if p3_index is None:
        raise ValueError(
            "フェーズ3が見つかりません（abs(Y) < 3 の点が1つもありません）。"
            "点群の選択範囲・同期ずれの可能性があります"
        )

    # フェーズ2: フェーズ3より前で speed 値のある点から選ぶ。
    raw_candidates = [point for point in points[:p3_index] if point.speed is not None]
    sane_candidates = [
        point for point in raw_candidates if abs(point.speed or 0) <= PHASE2_MAX_SPEED_KMH
    ]
    if sane_candidates:
        if len(sane_candidates) != len(raw_candidates):
            notes.append(
                f"フェーズ2候補から速度スパイク({PHASE2_MAX_SPEED_KMH:g} km/h超) "
                f"{len(raw_candidates) - len(sane_candidates)} 点を除外しました"
            )
        # 近接区間に入る直前の点だけを候補にする。上限を外すと追跡が長いほど遠い点が
        # 選ばれ、距離p2 と TTC が系統的に過大になる。候補が空になる場合は制限しない。
        near_candidates = [
            point for point in sane_candidates if abs(point.y) <= PHASE2_MAX_ABS_Y
        ]
        if not near_candidates:
            notes.append(
                f"フェーズ2候補が |Y| <= {PHASE2_MAX_ABS_Y:g} m に1点もないため、"
                "距離の制限なしで選びました（距離p2・TTCが過大になる可能性）"
            )
        p2 = _pick_phase2(near_candidates or sane_candidates)
    elif raw_candidates:
        notes.append(
            f"フェーズ2候補の速度がすべて {PHASE2_MAX_SPEED_KMH:g} km/h を超えています"
            "（最近接点が車体上を移り変わっている可能性）"
        )
        near_candidates = [
            point for point in raw_candidates if abs(point.y) <= PHASE2_MAX_ABS_Y
        ]
        p2 = _pick_phase2(near_candidates or raw_candidates)
    elif strict:
        raise ValueError("フェーズ2候補が見つかりません（|Y| < 3 になる前にspeed値がありません）")
    else:
        p2 = points[p3_index - 1] if p3_index > 0 else points[p3_index]
        notes.append(
            "フェーズ2が見つからないため、フェーズ3直前の有効点をフェーズ2として代用しました"
            "（|Y| < 3 になる前にspeed値がありません）"
        )
    _mark_phase(p2, "フェーズ2")

    p3 = points[p3_index]
    _mark_phase(p3, "フェーズ3")

    # フェーズ4: フェーズ3以降で最初に |Y| >= 3 に戻る点。
    p4 = next(
        (point for point in points[p3_index + 1:] if abs(point.y) >= PHASE_Y_THRESHOLD), None
    )
    if p4 is None:
        if strict:
            raise ValueError("フェーズ4が見つかりません（フェーズ3以降でabs(Y) >= 3の点がありません）")
        if p3_index >= len(points) - 1:
            raise ValueError(
                "フェーズ4が見つからず、フェーズ3が最終点のため通過区間を作れません。"
                "点群の選択範囲・同期ずれの可能性があります"
            )
        p4 = points[-1]
        notes.append(
            "フェーズ4が見つからないため、最終有効点をフェーズ4として代用しました"
            "（フェーズ3以降でabs(Y) >= 3 に戻りません）"
        )
    _mark_phase(p4, "フェーズ4")

    return {"phase1": p1, "phase2": p2, "phase3": p3, "phase4": p4}


def analyze_edit_points(
    points: list[EditPoint], strict: bool = False, car_length: float | None = None
) -> dict[str, float | EditPoint | list[str] | None]:
    """フェーズ判定と分析指標をまとめて返す。

    既定（``strict=False``）は全自動処理向けにフォールバックを使い、採用した
    フォールバックや外れ値除外を ``"warnings"`` キーに文字列リストで返す。
    ``strict=True`` を渡すと従来どおり、フェーズ2・4が無い場合と、通過時間の終端
    （下記）が取れない場合は例外になる。

    通過時間は「フェーズ4の1つ前の有効行」（通過区間 ``|Y| < 3m`` にいた最後の点。
    戻り値の ``"passing_end"``）の時刻からフェーズ3の時刻を引いたもの。フェーズ4は
    区間を出たあとの最初の点なので、その時刻は通過時間に含めない。距離p3は従来どおり
    フェーズ4の行で測る（``docs/計算方法.md`` 9.2）。

    相対速度 = 3.6 × (距離p3 + 車長) / 通過時間。車長（``car_length``、m）は
    ``car_length.py`` が動画の写真から求める。車長が無ければ相対速度は ``None``。

    TTC = フェーズ2の distance / (フェーズ2の speed / 3.6)。相対速度と車長には
    依らない。フェーズ2の speed が空欄か0以下なら ``None``。
    """
    warnings: list[str] = []
    phases = classify_phases(points, strict=strict, warnings=warnings)
    p2, p3, p4 = phases["phase2"], phases["phase3"], phases["phase4"]
    # 同じ値の行があっても取り違えないよう、同一性で位置を求める。
    start = next(i for i, point in enumerate(points) if point is p3)
    end = next(i for i, point in enumerate(points) if point is p4)
    section = points[start:end + 1]
    lc_section = [point for point in section if point.distance >= LC_MIN_DISTANCE] or section
    # 通過時間の終端 = フェーズ4の1つ前の有効行。フェーズ3の直後がフェーズ4（通過区間内の
    # 点がフェーズ3の1点だけ）だと終端が取れないので、全自動ではフェーズ4の行で代用して
    # 警告を残す（「代用」を含む警告は自動レビューが「未確認」にする）。
    if end - 1 > start:
        passing_end = points[end - 1]
    elif strict:
        raise ValueError(
            "フェーズ3とフェーズ4の間に行が無いため通過時間を計算できません"
            "（通過区間内の点がフェーズ3の1点だけです）"
        )
    else:
        passing_end = p4
        warnings.append(
            "通過区間内の点がフェーズ3の1点だけのため、通過時間の終端をフェーズ4の行で"
            "代用しました（本来はフェーズ4の1つ前の行。通過時間・相対速度・TTCは目安）"
        )
    passing_time = passing_end.unixtime - p3.unixtime
    if passing_time == 0:
        raise ValueError("フェーズ3と通過時間の終端行の時刻差が0のため計算できません")
    lc, lc_notes = lateral_clearance(lc_section)
    warnings.extend(lc_notes)
    distance_p3 = p4.y - p3.y
    if car_length is not None and not (math.isfinite(car_length) and car_length > 0):
        car_length = None
    relative_speed = (
        3.6 * (distance_p3 + car_length) / passing_time if car_length is not None else None
    )
    # TTC はフェーズ2の行の値だけで決まる（距離 / その時点の速度）。
    p2_speed = p2.speed
    if p2_speed is not None and p2_speed > 0:
        ttc: float | None = p2.distance / (p2_speed / 3.6)
    else:
        ttc = None
        warnings.append(
            "フェーズ2の speed が"
            + ("空欄" if p2_speed is None else f" {p2_speed:g} km/h（0以下）")
            + " のため TTC を計算できません"
        )
    if distance_p3 <= 0:
        # 追い越し車ならフェーズ3→4で Y は必ず増える。負や0になるのは軌跡が
        # 別物体へ乗り移った証拠で、通過時間・相対速度も成り立たない。
        warnings.append(
            f"距離p3 が {distance_p3:.2f} m（0以下）です。フェーズ4がフェーズ3より"
            "手前にあり、追い越し車以外を追跡している可能性が高いので点群を目視で"
            "確認してください"
        )
    return {
        **phases,
        "passing_end": passing_end,
        "lc": lc,
        "passing_time": passing_time,
        "distance_p2": abs(p2.y),
        "distance_p3": distance_p3,
        "car_length": car_length,
        "relative_speed": relative_speed,
        "ttc": ttc,
        "warnings": warnings,
    }


def _xlsx_col_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _xlsx_cell(row: int, col: int, value) -> str:
    ref = f"{_xlsx_col_name(col)}{row}"
    if value is None:
        value = ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            value = ""
        else:
            return f'<c r="{ref}"><v>{value}</v></c>'
    text = _xml_escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _xlsx_sheet_xml(rows: list[list]) -> str:
    row_xml = []
    for r_idx, row in enumerate(rows, 1):
        cells = "".join(_xlsx_cell(r_idx, c_idx, value) for c_idx, value in enumerate(row))
        row_xml.append(f'<row r="{r_idx}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<sheetData>' + "".join(row_xml) + '</sheetData>'
        '</worksheet>'
    )


def _write_simple_xlsx(out_path: Path, sheet_name: str, rows: list[list]) -> None:
    with ZipFile(out_path, "w", ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '</Types>'
        ))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{_xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ))
        z.writestr("docProps/core.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>OvertakingTool</dc:creator></cp:coreProperties>'
        ))
        z.writestr("docProps/app.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            '<Application>OvertakingTool</Application></Properties>'
        ))
        z.writestr("xl/worksheets/sheet1.xml", _xlsx_sheet_xml(rows))


def build_analysis_rows(points: list[EditPoint], metrics: dict[str, float | EditPoint]) -> list[list]:
    rows = [list(EDIT_HEADER) + ["フェーズ区分", "", "", "", ""]]
    indicator_rows = {
        2: ["分析指標", "計算値（数式）", "単位"],
        3: ["離隔距離 (LC)", metrics["lc"], "m"],
        4: ["通過時間", metrics["passing_time"], "秒"],
        5: ["距離p2", metrics["distance_p2"], "m"],
        6: ["距離p3", metrics["distance_p3"], "m"],
        7: ["相対速度", metrics["relative_speed"], "km/h"],
        8: ["TTC", metrics["ttc"], "秒"],
        9: ["車長", metrics.get("car_length"), "m"],
        10: ["車長の出典", metrics.get("car_length_source", ""), ""],
    }
    for i, point in enumerate(points, start=2):
        analysis = indicator_rows.get(i, ["", "", ""])
        rows.append(point.raw + [point.phase, ""] + analysis)
    # 点が少ないと指標欄が途中で切れる（TTC・車長が書かれない）ので、点の列を空けて続ける。
    blank_point = [""] * (len(EDIT_HEADER) + 2)
    for i in range(len(points) + 2, max(indicator_rows) + 1):
        rows.append(blank_point + indicator_rows[i])
    return rows


def _event_id_from_edit_path(edit_path: Path) -> str:
    match = re.match(r"overtaking_(.+)_edit$", Path(edit_path).stem)
    return match.group(1) if match else ""


def write_edit_analysis_xlsx(edit_path: Path, car_length: float | None = None,
                             car_length_source: str = "") -> Path:
    """解析xlsxを書く。車長を渡さなければ ``<out>/car_length/car_lengths.csv`` から引く。"""
    points = read_edit_points(edit_path)
    if car_length is None:
        from car_length import lookup_car_length  # 循環を避けるためここで読む

        found = lookup_car_length(Path(edit_path).parent.parent,
                                  _event_id_from_edit_path(edit_path))
        if found is not None:
            car_length, car_length_source = found.meters, found.source
    metrics = analyze_edit_points(points, car_length=car_length)
    metrics["car_length_source"] = car_length_source if metrics["car_length"] is not None else ""
    out_path = Path(edit_path).with_name(f"{Path(edit_path).stem}_analysis.xlsx")
    _write_simple_xlsx(out_path, "走行挙動解析", build_analysis_rows(points, metrics))
    return out_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python edit_csv.py <frame-csv-folder>")
        raise SystemExit(2)
    written = write_edit_csv(Path(sys.argv[1]))
    print(f"wrote {written}")
