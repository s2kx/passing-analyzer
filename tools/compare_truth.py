"""全自動出力の最終Excelを手作業の正解データと照合する受け入れ評価ツール。

使い方::

    python tools/compare_truth.py --out output/ID1_v2 --id 1
    python tools/compare_truth.py --out out_f/ID1_new --id 1 --truth <正解.xlsx>

自動側の最終Excel（``<out>/final/*_final.xlsx``）の各行と、正解Excelの「LiDARの
タイムスタンプ」を ``--window`` 秒以内で貪欲に1対1対応付けし、離隔距離LC・通過時間・
距離p2・相対速度・TTC・自転車速度・車種を比較する。対応付けは時刻差の小さい組から
順に確定させる（第2波の受け入れ評価と同じ手順）。

正解Excelの列位置（0始まり）:
    0=整理ID, 3=離隔距離, 4=自転車速度, 5=自動車速度, 7=通過時間,
    8=距離p2, 9=距離p3, 10=相対速度, 11=TTC, 16=LiDARのタイムスタンプ, 17=車種
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 正解Excelの列位置
T_ID, T_LC, T_BIKE, T_PASS = 0, 3, 4, 7
T_P2, T_P3, T_RS, T_TTC, T_TS, T_KIND = 8, 9, 10, 11, 16, 17

# 自動側の最終Excelの列位置（final_export.FINAL_HEADERS と同じ並び）
A_ID, A_LC, A_BIKE, A_PASS = 0, 3, 4, 7
A_P2, A_P3, A_RS, A_TTC, A_TS, A_KIND = 8, 9, 10, 11, 16, 17

# 比較する指標: (表示名, 正解列, 自動列, 単位)
METRICS = [
    ("離隔距離LC", T_LC, A_LC, "m"),
    ("通過時間", T_PASS, A_PASS, "s"),
    ("距離p2", T_P2, A_P2, "m"),
    ("距離p3", T_P3, A_P3, "m"),
    ("相対速度", T_RS, A_RS, "km/h"),
    ("TTC", T_TTC, A_TTC, "s"),
    ("自転車速度", T_BIKE, A_BIKE, "km/h"),
]


def _col_index(ref: str) -> int:
    index = 0
    for ch in ref:
        if ch.isalpha():
            index = index * 26 + ord(ch.upper()) - 64
    return index - 1


def read_sheet(path: Path, sheet_index: int = 0) -> list[list[str]]:
    """xlsx の1シートを文字列の2次元リストとして読む（stdlibのみ）。"""
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in si.iter(NS + "t"))
                for si in root.findall(NS + "si")
            ]
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        root = ET.fromstring(book.read(sheets[sheet_index]))

    rows: list[list[str]] = []
    for row in root.iter(NS + "row"):
        cells: dict[int, str] = {}
        for cell in row.findall(NS + "c"):
            index = _col_index(cell.attrib.get("r", ""))
            kind = cell.attrib.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(NS + "t"))
            else:
                node = cell.find(NS + "v")
                text = node.text if node is not None and node.text else ""
                if kind == "s" and text:
                    text = shared[int(text)]
            cells[index] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1 if cells else 0)])
    return rows


def _num(value) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def load_truth(path: Path, organize_id: str) -> list[list[str]]:
    rows = read_sheet(path)[1:]
    return [
        r for r in rows
        if len(r) > T_KIND and _cell(r, T_ID).strip() == organize_id and _num(_cell(r, T_TS))
    ]


def load_auto(out_dir: Path) -> list[list[str]]:
    finals = sorted((out_dir / "final").glob("*_final.xlsx"))
    if not finals:
        raise SystemExit(f"最終Excelが見つかりません: {out_dir / 'final'}")
    rows = read_sheet(finals[0])[1:]
    return [r for r in rows if len(r) > A_TS and _cell(r, A_ID).strip() and _num(_cell(r, A_TS))]


def match(truth: list[list[str]], auto: list[list[str]], window: float):
    """時刻差の小さい組から貪欲に1対1対応付けする。"""
    candidates = sorted(
        (abs(_num(_cell(t, T_TS)) - _num(_cell(a, A_TS))), ti, ai)
        for ti, t in enumerate(truth)
        for ai, a in enumerate(auto)
        if abs(_num(_cell(t, T_TS)) - _num(_cell(a, A_TS))) <= window
    )
    used_t: set[int] = set()
    used_a: set[int] = set()
    pairs = []
    for delta, ti, ai in candidates:
        if ti in used_t or ai in used_a:
            continue
        used_t.add(ti)
        used_a.add(ai)
        pairs.append((truth[ti], auto[ai], delta))
    pairs.sort(key=lambda p: _num(_cell(p[0], T_TS)))
    unmatched_t = [t for i, t in enumerate(truth) if i not in used_t]
    unmatched_a = [a for i, a in enumerate(auto) if i not in used_a]
    return pairs, unmatched_t, unmatched_a


def main() -> int:
    parser = argparse.ArgumentParser(description="全自動出力を正解データと照合する")
    parser.add_argument("--out", required=True, help="パイプライン出力フォルダ（final/ を含む）")
    parser.add_argument("--id", required=True, help="正解Excelの整理ID（例: 1）")
    parser.add_argument(
        "--truth",
        default=r"E:\松本研究室\データ\260701\260701_データまとめ_海野.xlsx",
        help="正解Excelのパス",
    )
    parser.add_argument("--window", type=float, default=6.0, help="対応付けの時刻窓[秒]")
    args = parser.parse_args()

    truth = load_truth(Path(args.truth), str(args.id).strip())
    auto = load_auto(Path(args.out))
    pairs, missed, extra = match(truth, auto, args.window)

    ts0 = min(_num(_cell(t, T_TS)) for t in truth) if truth else 0.0
    print(f"正解 {len(truth)} 件 / 自動 {len(auto)} 行 / 対応 {len(pairs)} 件（窓±{args.window:g}秒）")
    print()
    header = f"{'ID':>4} {'Δt':>5}"
    for name, _, _, _ in METRICS:
        header += f" {name + '(正/自)':>19}"
    print(header)
    for t, a, delta in pairs:
        line = f"{_cell(a, A_ID):>4} {delta:5.2f}"
        for _, ti, ai, _ in METRICS:
            tv, av = _num(_cell(t, ti)), _num(_cell(a, ai))
            text = f"{tv:.2f}/{av:.2f}" if tv is not None and av is not None else "-/-"
            line += f" {text:>19}"
        print(line)

    print()
    print(f"{'指標':<12} {'平均絶対差':>10} {'平均差(自-正)':>13} {'最大絶対差':>10} {'n':>3}")
    for name, ti, ai, unit in METRICS:
        diffs = [
            _num(_cell(a, ai)) - _num(_cell(t, ti))
            for t, a, _ in pairs
            if _num(_cell(t, ti)) is not None and _num(_cell(a, ai)) is not None
        ]
        if not diffs:
            continue
        abs_diffs = [abs(d) for d in diffs]
        print(
            f"{name:<12} {sum(abs_diffs) / len(abs_diffs):10.3f} "
            f"{sum(diffs) / len(diffs):13.3f} {max(abs_diffs):10.3f} {len(diffs):3d}  {unit}"
        )

    kinds = [(_cell(t, T_KIND), _cell(a, A_KIND)) for t, a, _ in pairs]
    same = sum(1 for x, y in kinds if x == y)
    print(f"{'車種一致':<12} {same:>10} / {len(kinds)}")

    ids = [_cell(a, A_ID) for _, a, _ in pairs]
    print()
    print(f"再現率        : {len(pairs)}/{len(truth)}")
    print(f"重複出力      : {len(ids) - len(set(ids))} 件")
    print(f"余剰行        : {len(extra)} 件")
    if missed:
        print("未対応の正解  : " + ", ".join(f"{_num(_cell(t, T_TS)) - ts0:+.1f}s(LC {_cell(t, T_LC)})" for t in missed))
    if extra:
        print("余剰の自動行  : " + ", ".join(f"{_cell(a, A_ID)}(LC {_num(_cell(a, A_LC)):.2f})" for a in extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
