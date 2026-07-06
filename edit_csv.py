"""最近接点の軌跡 edit.csv を生成する。

旧 ``VolodyneConverter`` のモード2（``DisCreateClass.main``）を Python に忠実移植したもの。
VeloView 形式のフレームごと CSV（1 ファイル = 1 時刻、ファイル名 = unixtime、
XYZ 座標が 9, 10, 11 列目）が入ったフォルダを受け取り、各フレームでスキャナ原点に
最も近い点を追跡して ``unixtime, X, Y, Z, distance(m), speed(km/h)`` の軌跡 CSV を書き出す。
"""

from __future__ import annotations

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


def _trajectory_rows(frames, col_x: int = COL_X, col_y: int = COL_Y, col_z: int = COL_Z) -> list[list[str]]:
    """フレーム列から edit.csv 行（ヘッダ込み）を作る共通ロジック。

    ``frames`` は時刻順に並んだ ``(t: float, time_text: str, points: list[list[str]])`` の列。
    旧 ``DisCreateClass.main`` を忠実に再現する:
      * 各フレームで距離 = √(X² + Y²) が最小の点（最近接点）を採用
      * 速度[km/h] = 前フレーム最近接点からの移動距離 / 経過秒 × 3600 / 1000
      * 先頭フレームの速度は空欄
      * 有効点が無いフレームは時刻のみ、X..speed は空欄
    """
    out: list[list[str]] = [list(EDIT_HEADER)]
    if not frames:
        return out

    prev_t = frames[0][0]           # 直前フレームの時刻（最初のフレームで初期化）
    prev_x = 0.0                    # 直前フレーム最近接点 X
    prev_y = 0.0                    # 直前フレーム最近接点 Y

    for i, (t, time_text, points) in enumerate(frames):
        best_dist = 99999999.0       # このフレームの最小距離
        row = [time_text, "", "", "", "", ""]

        speed_text = ""
        for p in points:
            if len(p) <= col_z:
                continue
            try:
                x = float(p[col_x])
                y = float(p[col_y])
            except ValueError:
                continue             # 数値でない点（空欄・見出し行など）はスキップ
            dist = math.sqrt(x * x + y * y)

            # 2 フレーム目以降は前フレーム最近接点との移動から速度を算出
            if i > 0 and (t - prev_t) != 0:
                speed = math.sqrt((x - prev_x) ** 2 + (y - prev_y) ** 2) / (t - prev_t) * 3600.0 / 1000.0
                speed_text = _fmt(speed)

            if dist < best_dist:
                best_dist = dist
                row[1] = p[col_x]                 # X（元の文字列をそのまま）
                row[2] = p[col_y]                 # Y
                row[3] = p[col_z]                 # Z
                row[4] = _fmt(dist)               # distance(m)
                row[5] = "" if i == 0 else speed_text  # speed(km/h)

        out.append(row)

        # 次フレームの速度計算用に「直前」を更新
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


def classify_phases(points: list[EditPoint]) -> dict[str, EditPoint]:
    if not points:
        raise ValueError("分析対象のedit.csvに有効な行がありません")

    for point in points:
        point.phase = ""
    p1 = points[0]
    p1.phase = "フェーズ1"

    p2_candidates = []
    for point in points:
        if abs(point.y) < 3:
            break
        if point.speed is not None:
            p2_candidates.append(point)
    if not p2_candidates:
        raise ValueError("フェーズ2候補が見つかりません（|Y| < 3 になる前にspeed値がありません）")

    single_digit = [point for point in p2_candidates if 0 <= abs(point.speed or 0) < 10]
    p2 = single_digit[-1] if single_digit else min(p2_candidates, key=lambda point: abs(point.speed or 0))
    p2.phase = "フェーズ2"

    p2_index = points.index(p2)
    p3 = next((point for point in points[p2_index + 1:] if abs(point.y) < 3), None)
    if p3 is None:
        raise ValueError("フェーズ3が見つかりません（フェーズ2以降でabs(Y) < 3の点がありません）")
    p3.phase = "フェーズ3"

    p3_index = points.index(p3)
    p4 = next((point for point in points[p3_index + 1:] if abs(point.y) >= 3), None)
    if p4 is None:
        raise ValueError("フェーズ4が見つかりません（フェーズ3以降でabs(Y) >= 3の点がありません）")
    p4.phase = "フェーズ4"

    return {"phase1": p1, "phase2": p2, "phase3": p3, "phase4": p4}


def analyze_edit_points(points: list[EditPoint]) -> dict[str, float | EditPoint]:
    phases = classify_phases(points)
    p2, p3, p4 = phases["phase2"], phases["phase3"], phases["phase4"]
    start = points.index(p3)
    end = points.index(p4)
    section = points[start:end + 1]
    lc_section = [point for point in section if point.distance >= LC_MIN_DISTANCE] or section
    passing_time = p4.unixtime - p3.unixtime
    if passing_time == 0:
        raise ValueError("フェーズ3とフェーズ4の時刻差が0のため計算できません")
    distance_p3 = p4.y - p3.y
    relative_speed = distance_p3 / passing_time * 3.6
    ttc = abs(p2.y) / (relative_speed / 3.6) if relative_speed != 0 else float("nan")
    return {
        **phases,
        "lc": abs(min(point.x for point in lc_section)),
        "passing_time": passing_time,
        "distance_p2": abs(p2.y),
        "distance_p3": distance_p3,
        "relative_speed": relative_speed,
        "ttc": ttc,
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
    }
    for i, point in enumerate(points, start=2):
        analysis = indicator_rows.get(i, ["", "", ""])
        rows.append(point.raw + [point.phase, ""] + analysis)
    return rows


def write_edit_analysis_xlsx(edit_path: Path) -> Path:
    points = read_edit_points(edit_path)
    metrics = analyze_edit_points(points)
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
