"""既存の解析結果の通過時間・相対速度・TTC を、本体の新しい式（2026-11-07）へ変換する。

    通過時間   旧: t(フェーズ4) - t(フェーズ3)
               新: t(フェーズ4の1つ前の行) - t(フェーズ3)                    [秒]
    相対速度   旧: 距離p3 / 通過時間 × 3.6
               新: 3.6 × (距離p3 + 車長) / 通過時間（新しい通過時間）       [km/h]
    TTC        旧: 距離p2 / (相対速度 / 3.6)
               新: フェーズ2の distance / (フェーズ2の speed / 3.6)          [秒]
    自動車速度 = 自転車速度 + 相対速度（式は同じ。新しい相対速度で計算し直す）
    距離p2・距離p3・離隔距離は変わらない。

本体（edit_csv.py の analyze_edit_points）と同じ定義。このツールは単独でも配布するので
本体のコードは読まず、解析xlsx の行（unixtime, X, Y, Z, distance, speed とフェーズ区分）
から計算し直す。フェーズ3とフェーズ4の間に行が無い（通過区間内の点がフェーズ3の1点
だけ）場合は、本体と同じく通過時間の終端をフェーズ4の行で代用し、一覧の note に残す。
フェーズ2の speed が空欄か0以下なら TTC は空欄。本体の新しい式で作った解析xlsx を
入れても同じ値になる（何度変換しても結果は変わらない）。

元のファイル（output/ 以下）は一切書き換えない。変換結果はすべて AutoAnalysis/ に置く。

    AutoAnalysis/
      car_lengths.csv                 車長の一覧（入力。car_length_gpt.py か手入力で埋める）
      converted/<データ>/<セッション>/lidar_edit/overtaking_XXX_edit_analysis.xlsx
      converted/<データ>/<セッション>/final/<セッション>_final.xlsx
      converted/conversion_summary.csv  全イベントの新旧比較と変換できなかった理由

車長の無いイベントも通過時間と TTC は新しい式へ直す。相対速度・自動車速度は空欄にする
（旧式の値と新式の値が同じ列に混ざるのを防ぐため）。

使い方:
    python AutoAnalysis/convert_relative_speed.py --init-lengths   # car_lengths.csv の雛形を作る
    python AutoAnalysis/convert_relative_speed.py                  # 変換する
    python AutoAnalysis/convert_relative_speed.py --fallback lidar # 車長が無いイベントは点群の実測長で代用
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_cells import col_name, read_cells, to_float, write_with_updates  # noqa: E402

HERE = Path(__file__).resolve().parent
# リポジトリ内（AutoAnalysis/ 配下）でも配布版（単独フォルダ）でも動くよう、本体の src/web_gui.py が隣に
# あればその階層を、無ければ自分の階層をルート（output・yolov8n.pt・portable/python の置き場）にする。
ROOT = HERE.parent if (HERE.parent / "src" / "web_gui.py").is_file() else HERE
DEFAULT_SRC = ROOT / "output"
DEFAULT_DEST = HERE / "converted"
DEFAULT_LENGTHS = HERE / "car_lengths.csv"

LENGTH_COLUMNS = ["session", "event_id", "car_length_m", "source", "make", "model",
                  "body_type", "confidence", "images", "note", "class", "lidar_length_m",
                  "updated_at"]

# --fallback class のときの車種別の代表長[m]。軽は規格上限、普通・大型は道路構造令の
# 設計車両（小型自動車 4.7m / 普通自動車 12m）。個々の車の長さではないので目安扱い。
CLASS_DEFAULT_LENGTH = {"軽": 3.4, "普通": 4.7, "大型": 12.0, "二輪": 2.2}

# 最終Excelは小数2桁に丸めてあるので、解析xlsxとの照合はこの幅で行う。
MATCH_TOL = 0.0051
FORMULAS = (
    ("式（通過時間）", "t(フェーズ4の1つ前の行)-t(フェーズ3)"),
    ("式（相対速度）", "3.6×(距離p3+車長)/通過時間"),
    ("式（TTC）", "フェーズ2のdistance/(フェーズ2のspeed/3.6)"),
)
PHASE_COLUMN = "フェーズ区分"
POINT_COLUMNS = {"unixtime": "t", "distance(m)": "distance", "speed(km/h)": "speed"}


@dataclass
class EventMetrics:
    event_id: str
    path: Path
    cells: dict
    value_cells: dict[str, tuple[int, int]] = field(default_factory=dict)
    # 元の解析xlsxに書かれていた値（最終Excelとの照合に使う）
    values: dict[str, float] = field(default_factory=dict)
    # 新しい式で計算し直した通過時間・TTC（recompute_metrics の結果）
    recomputed: dict = field(default_factory=dict)


@dataclass
class Length:
    meters: float
    source: str


# ---------------------------------------------------------------- 入力の収集

def find_sessions(src: Path) -> list[Path]:
    """lidar_edit を持つフォルダをセッションとみなす。"""
    return sorted(p.parent for p in src.rglob("lidar_edit") if p.is_dir())


def session_key(src: Path, session: Path) -> str:
    return session.relative_to(src).as_posix()


INDICATOR_LABELS = {
    "離隔距離": "lc", "通過時間": "passing_time", "距離p2": "distance_p2",
    "距離p3": "distance_p3", "相対速度": "relative_speed", "TTC": "ttc",
}


def read_analysis(path: Path) -> EventMetrics:
    match = re.match(r"overtaking_(.+)_edit_analysis\.xlsx$", path.name)
    event_id = match.group(1) if match else path.stem
    cells = read_cells(path)
    metrics = EventMetrics(event_id, path, cells)
    # 指標欄は「ラベル列の右隣が値」。列位置は固定（I/J）だが、ラベルで探して頑健にする。
    for (row, col), value in cells.items():
        if not isinstance(value, str):
            continue
        for label, key in INDICATOR_LABELS.items():
            if value.startswith(label) and key not in metrics.value_cells:
                # 値が空欄の行も位置は覚える（本体の新しい解析xlsxは車長が無いと相対速度が空欄）
                metrics.value_cells[key] = (row, col + 1)
                number = to_float(cells.get((row, col + 1)))
                if number is not None:
                    metrics.values[key] = number
    metrics.recomputed = recompute_metrics(cells)
    return metrics


def recompute_metrics(cells: dict) -> dict:
    """解析xlsx の点の行とフェーズ区分から、新しい式の通過時間と TTC を求める。

    戻り値は ``{"passing_time", "ttc", "notes"}``。フェーズ3・4の行が見つからなければ
    ``{"error": 理由}``。点の行は本体の build_analysis_rows と同じく有効点だけが並ぶので、
    「フェーズ4の1つ前の行」はフェーズ4の直前の点の行になる。
    """
    header = {str(value).strip(): col for (row, col), value in cells.items()
              if row == 1 and isinstance(value, str)}
    if PHASE_COLUMN not in header or not all(name in header for name in POINT_COLUMNS):
        return {"error": "解析xlsxに点の列かフェーズ区分の列がありません"}
    phase_col = header[PHASE_COLUMN]
    max_row = max((r for r, _ in cells), default=1)
    points: list[dict] = []
    for row in range(2, max_row + 1):
        t = to_float(cells.get((row, header["unixtime"])))
        if t is None:
            continue  # 点の列が空の行（指標欄だけの行）
        points.append({
            "t": t,
            "distance": to_float(cells.get((row, header["distance(m)"]))),
            "speed": to_float(cells.get((row, header["speed(km/h)"]))),
            "phases": str(cells.get((row, phase_col)) or "").split("/"),
        })

    def find(label: str) -> int | None:
        return next((i for i, p in enumerate(points) if label in p["phases"]), None)

    i2, i3, i4 = find("フェーズ2"), find("フェーズ3"), find("フェーズ4")
    if i3 is None or i4 is None:
        return {"error": "フェーズ3・フェーズ4の行が見つかりません"}
    notes: list[str] = []
    if i4 - 1 > i3:
        end = points[i4 - 1]
    else:
        end = points[i4]
        notes.append("通過区間内の点がフェーズ3の1点だけのため、通過時間の終端をフェーズ4の行で代用")
    passing_time = end["t"] - points[i3]["t"]
    ttc = None
    if i2 is None:
        notes.append("フェーズ2の行が無いため TTC は空欄")
    else:
        p2 = points[i2]
        if p2["speed"] is not None and p2["speed"] > 0 and p2["distance"] is not None:
            ttc = p2["distance"] / (p2["speed"] / 3.6)
        else:
            notes.append("フェーズ2の speed が空欄か0以下のため TTC は空欄")
    return {"passing_time": passing_time, "ttc": ttc, "notes": notes}


def read_lengths(path: Path) -> dict[tuple[str, str], Length]:
    lengths: dict[tuple[str, str], Length] = {}
    if not path.exists():
        return lengths
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            meters = to_float(row.get("car_length_m"))
            if meters is None or meters <= 0:
                continue
            key = (row.get("session", "").strip(), row.get("event_id", "").strip())
            lengths[key] = Length(meters, (row.get("source") or "csv").strip())
    return lengths


def session_hints(session: Path) -> tuple[dict[str, float], dict[str, str]]:
    """点群で実測した車長（自動追跡の結果）と、レビュー済みの車種。"""
    lidar: dict[str, float] = {}
    candidates = session / "lidar" / "autotrack_candidates.json"
    if candidates.exists():
        data = json.loads(candidates.read_text(encoding="utf-8"))
        for event in data.get("events", []):
            assigned = event.get("assigned") or {}
            if to_float(assigned.get("length")):
                lidar[str(event.get("event_id"))] = float(assigned["length"])
    classes: dict[str, str] = {}
    for name in ("reviewed_overtaking_events.csv", "overtaking_events.csv"):
        events_csv = session / name
        if events_csv.exists():
            with events_csv.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    classes.setdefault(row.get("event_id", ""), row.get("class", ""))
            break
    return lidar, classes


def resolve_length(key: tuple[str, str], lengths: dict, fallback: str,
                   lidar: dict[str, float], classes: dict[str, str]) -> Length | None:
    if key in lengths:
        return lengths[key]
    event_id = key[1]
    if fallback == "lidar" and event_id in lidar:
        return Length(lidar[event_id], "lidar(点群の実測長)")
    if fallback == "class" and classes.get(event_id) in CLASS_DEFAULT_LENGTH:
        return Length(CLASS_DEFAULT_LENGTH[classes[event_id]], f"class({classes[event_id]}の代表値)")
    return None


# ---------------------------------------------------------------- 計算

def new_relative_speed(distance_p3: float, passing_time: float, car_length: float) -> float:
    return (distance_p3 + car_length) * 3.6 / passing_time


def new_metrics(metrics: EventMetrics, length: Length | None) -> dict:
    """新しい式の通過時間・相対速度・TTC。車長が無ければ相対速度は None。"""
    passing_time = metrics.recomputed["passing_time"]
    rel = (new_relative_speed(metrics.values["distance_p3"], passing_time, length.meters)
           if length is not None and passing_time > 0 else None)
    return {"passing_time": passing_time, "relative_speed": rel, "ttc": metrics.recomputed["ttc"]}


# ---------------------------------------------------------------- 解析xlsx

def convert_analysis(metrics: EventMetrics, length: Length | None, dest: Path) -> dict:
    v = metrics.values
    new = new_metrics(metrics, length)
    _, value_col = metrics.value_cells["distance_p3"]
    label_col = value_col - 1
    updates: dict[str, tuple[object, str | None]] = {}
    # 軌跡の点数が少ないと元の解析xlsxに相対速度・TTCの行が無い（指標欄が点の行に
    # 間借りしているため）。その場合は距離p3の下に行を作って書く。
    next_row = max(r for r, _ in metrics.value_cells.values()) + 1
    for key, label, unit in (("passing_time", "通過時間", "秒"),
                             ("relative_speed", "相対速度", "km/h"),
                             ("ttc", "TTC", "秒")):
        value = new[key] if new[key] is not None else ""
        if key in metrics.value_cells:
            r, c = metrics.value_cells[key]
            updates[f"{col_name(c)}{r}"] = (value, None)
        else:
            updates[f"{col_name(label_col)}{next_row}"] = (label, None)
            updates[f"{col_name(value_col)}{next_row}"] = (value, None)
            updates[f"{col_name(value_col + 1)}{next_row}"] = (unit, None)
            next_row += 1
    # 指標欄の下へ車長・出典・旧値・式を書き足す（TTC の次の行から）。
    extra = [
        ("車長", length.meters if length else "", "m"),
        ("車長の出典", length.source if length else "車長未取得（相対速度は空欄）", ""),
        ("旧通過時間(t4-t3)", v.get("passing_time", ""), "秒"),
        ("旧相対速度(距離p3/通過時間)", v.get("relative_speed", ""), "km/h"),
        ("旧TTC(距離p2/相対速度)", v.get("ttc", ""), "秒"),
        *((label, text, "") for label, text in FORMULAS),
        *(("注記", note, "") for note in metrics.recomputed.get("notes", [])),
    ]
    for i, (label, value, unit) in enumerate(extra):
        row = next_row + i
        updates[f"{col_name(label_col)}{row}"] = (label, None)
        updates[f"{col_name(value_col)}{row}"] = (value, None)
        updates[f"{col_name(value_col + 1)}{row}"] = (unit, None)
    write_with_updates(metrics.path, dest, updates)
    return new


# ---------------------------------------------------------------- 最終Excel

FINAL_KEYS = {
    "lc": "離隔距離", "bike": "自転車速度", "vehicle": "自動車速度", "passing_time": "通過時間",
    "distance_p2": "距離p2", "distance_p3": "距離p3", "relative_speed": "相対速度", "ttc": "TTC",
}
MATCH_KEYS = ("distance_p3", "passing_time", "lc", "distance_p2")


def _header_columns(cells: dict) -> tuple[dict[str, int], int]:
    header = {col: str(value) for (row, col), value in cells.items() if row == 1 and value != ""}
    columns: dict[str, int] = {}
    for key, label in FINAL_KEYS.items():
        for col, text in sorted(header.items()):
            if text.strip().startswith(label):
                columns[key] = col
                break
    for name in ("車長(m)", "車長の出典"):
        for col, text in header.items():
            if text.strip() == name:
                columns[name] = col
    return columns, max(header) if header else -1


def _match_event(row_values: dict[str, float | None], events: list[EventMetrics]) -> list[EventMetrics]:
    hits = []
    for event in events:
        compared = 0
        for key in MATCH_KEYS:
            mine = row_values.get(key)
            # 通過時間は、最終Excelが旧式・新式のどちらで作られていても対応付けられるよう両方と比べる
            theirs = [value for value in (event.values.get(key), event.recomputed.get(key))
                      if value is not None]
            if mine is None or not theirs:
                continue
            if all(abs(round(value, 2) - mine) > MATCH_TOL for value in theirs):
                break
            compared += 1
        else:
            if compared >= 2:
                hits.append(event)
    return hits


def convert_final(path: Path, dest: Path, skey: str, events: list[EventMetrics],
                  resolve, summary: list[dict]) -> None:
    cells = read_cells(path)
    columns, last_col = _header_columns(cells)
    missing = [k for k in ("distance_p3", "passing_time", "relative_speed") if k not in columns]
    if missing:
        summary.append({"session": skey, "file": path.name, "status": f"最終Excelの列が見つかりません: {missing}"})
        return
    len_col = columns.get("車長(m)", last_col + 1)
    src_col = columns.get("車長の出典", len_col + 1)
    updates: dict[str, tuple[object, str | None]] = {
        f"{col_name(len_col)}1": ("車長(m)", "1"),
        f"{col_name(src_col)}1": ("車長の出典", "1"),
    }
    max_row = max((r for r, _ in cells), default=1)
    for row in range(2, max_row + 1):
        values = {k: to_float(cells.get((row, c))) for k, c in columns.items() if k in FINAL_KEYS}
        if values.get("distance_p3") is None and values.get("relative_speed") is None:
            continue  # 空行
        record = {"session": skey, "file": path.name, "row": row,
                  "old_passing_time": values.get("passing_time"),
                  "old_relative_speed": values.get("relative_speed"), "old_ttc": values.get("ttc"),
                  "old_vehicle_speed": values.get("vehicle")}
        hits = _match_event(values, events)

        def put(key: str, value: float | None) -> None:
            if key in columns:
                updates[f"{col_name(columns[key])}{row}"] = (
                    round(value, 2) if value is not None else "", None)

        def blank(reason: str, keys=("passing_time", "relative_speed", "ttc", "vehicle")) -> None:
            # 新しい式で計算できない値は空欄にする（旧式の値を残すと列の中で式が混ざる）
            for k in keys:
                put(k, None)
            updates[f"{col_name(len_col)}{row}"] = ("", "4")
            updates[f"{col_name(src_col)}{row}"] = (reason, None)
            summary.append({**record, "status": reason})

        if len(hits) != 1:
            blank("解析xlsxと対応付け不可" if not hits else "解析xlsxの候補が複数")
            continue
        event = hits[0]
        record["event_id"] = event.event_id
        if "error" in event.recomputed:
            blank(event.recomputed["error"])
            continue
        record["note"] = " / ".join(event.recomputed.get("notes", []))
        length = resolve(event.event_id)
        new = new_metrics(event, length)
        record.update(distance_p3=event.values["distance_p3"],
                      new_passing_time=new["passing_time"], new_ttc=new["ttc"])
        # 通過時間と TTC は車長に依らないので、車長が無くても新しい式の値にする
        put("passing_time", new["passing_time"])
        put("ttc", new["ttc"])
        if new["passing_time"] <= 0:
            blank("通過時間が0以下", keys=("relative_speed", "vehicle"))
            continue
        if length is None:
            blank("車長未取得", keys=("relative_speed", "vehicle"))
            continue
        rel = new["relative_speed"]
        bike = values.get("bike")
        vehicle = bike + rel if bike is not None else None
        put("relative_speed", rel)
        put("vehicle", vehicle)
        updates[f"{col_name(len_col)}{row}"] = (round(length.meters, 3), "4")
        updates[f"{col_name(src_col)}{row}"] = (length.source, None)
        summary.append({**record, "status": "変換済み", "car_length_m": length.meters,
                        "length_source": length.source, "new_relative_speed": rel,
                        "new_vehicle_speed": vehicle})
    write_with_updates(path, dest, updates)


# ---------------------------------------------------------------- 本体

def init_lengths(src: Path, lengths_path: Path) -> int:
    """全イベントを並べた car_lengths.csv を作る（既存の行は残し、無いイベントだけ足す）。"""
    rows: list[dict] = []
    existing: set[tuple[str, str]] = set()
    if lengths_path.exists():
        with lengths_path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                rows.append(row)
                existing.add((row.get("session", ""), row.get("event_id", "")))
    added = 0
    for session in find_sessions(src):
        skey = session_key(src, session)
        lidar, classes = session_hints(session)
        for path in sorted((session / "lidar_edit").glob("overtaking_*_edit_analysis.xlsx")):
            event_id = re.match(r"overtaking_(.+)_edit_analysis", path.name).group(1)
            if (skey, event_id) in existing:
                continue
            rows.append({"session": skey, "event_id": event_id, "class": classes.get(event_id, ""),
                         "lidar_length_m": lidar.get(event_id, "")})
            added += 1
    lengths_path.parent.mkdir(parents=True, exist_ok=True)
    with lengths_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=LENGTH_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return added


def run(src: Path, dest: Path, lengths_path: Path, fallback: str) -> list[dict]:
    lengths = read_lengths(lengths_path)
    summary: list[dict] = []
    for session in find_sessions(src):
        skey = session_key(src, session)
        lidar, classes = session_hints(session)

        def resolve(event_id: str, _skey=skey, _lidar=lidar, _classes=classes) -> Length | None:
            return resolve_length((_skey, event_id), lengths, fallback, _lidar, _classes)

        events: list[EventMetrics] = []
        for path in sorted((session / "lidar_edit").glob("overtaking_*_edit_analysis.xlsx")):
            try:
                metrics = read_analysis(path)
            except Exception as exc:  # 壊れたファイルは飛ばして一覧に残す
                summary.append({"session": skey, "file": path.name, "status": f"読めません: {exc}"})
                continue
            events.append(metrics)
            need = {"distance_p3", "passing_time"}
            if not need <= metrics.values.keys():
                summary.append({"session": skey, "file": path.name, "event_id": metrics.event_id,
                                "status": "解析xlsxに指標がありません"})
                continue
            if "error" in metrics.recomputed:
                summary.append({"session": skey, "file": path.name, "event_id": metrics.event_id,
                                "status": metrics.recomputed["error"]})
                continue
            # 車長が無くても通過時間・TTC は新しい式へ直す（相対速度は空欄）。
            # 最終Excel側の一覧には「車長未取得」として残る。
            convert_analysis(metrics, resolve(metrics.event_id), dest / skey / "lidar_edit" / path.name)

        for final in sorted((session / "final").glob("*_final.xlsx")):
            convert_final(final, dest / skey / "final" / final.name, skey, events, resolve, summary)

        # 最終Excelに載っていないイベント（未採用など）も、車長が無ければ一覧に出す。
        listed = {(r.get("session"), r.get("event_id")) for r in summary}
        for metrics in events:
            if (skey, metrics.event_id) not in listed and resolve(metrics.event_id) is None:
                summary.append({"session": skey, "file": metrics.path.name, "event_id": metrics.event_id,
                                "status": "車長未取得（最終Excel対象外）"})
    return summary


SUMMARY_COLUMNS = ["session", "file", "row", "event_id", "status", "car_length_m", "length_source",
                   "distance_p3", "old_passing_time", "new_passing_time",
                   "old_relative_speed", "new_relative_speed", "old_ttc", "new_ttc",
                   "old_vehicle_speed", "new_vehicle_speed", "note"]


def write_summary(summary: list[dict], dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "conversion_summary.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in summary:
            writer.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in row.items()})
    return path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="通過時間・相対速度・TTC を本体の新しい式へ変換する")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="元の解析結果フォルダ（既定: output）")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="変換結果の保存先（既定: AutoAnalysis/converted）")
    parser.add_argument("--lengths", type=Path, default=DEFAULT_LENGTHS, help="車長の一覧CSV")
    parser.add_argument("--fallback", choices=["none", "lidar", "class"], default="none",
                        help="車長がCSVに無いときの代用（既定: 代用しない）")
    parser.add_argument("--init-lengths", action="store_true", help="car_lengths.csv の雛形を作って終わる")
    args = parser.parse_args()

    src, dest = args.src.resolve(), args.dest.resolve()
    if dest == src or src in dest.parents:
        sys.exit("保存先が元データと同じ場所です。--dest を変えてください")
    if args.init_lengths:
        added = init_lengths(src, args.lengths)
        print(f"{args.lengths} に {added} 件のイベントを追加しました")
        return

    summary = run(src, dest, args.lengths, args.fallback)
    path = write_summary(summary, dest)
    final_rows = [r for r in summary if r.get("row")]
    done = sum(1 for r in final_rows if r.get("status") == "変換済み")
    no_len = sum(1 for r in final_rows if r.get("status") == "車長未取得")
    others = [r for r in summary if not r.get("row")]
    print(f"[{datetime.now():%H:%M:%S}] 最終Excel {len(final_rows)} 行: 変換 {done} / 車長未取得 {no_len} / "
          f"その他 {len(final_rows) - done - no_len}")
    if others:
        print(f"最終Excelに載っていないイベント・読めないファイル: {len(others)} 件（一覧の status を参照）")
    print(f"一覧: {path}")


if __name__ == "__main__":
    main()
