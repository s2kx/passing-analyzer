"""既存 xlsx のセルを読み書きする最小限の道具（標準ライブラリのみ）。

本体（edit_csv.py / final_export.py）は openpyxl を使わず xlsx を自前で書いている。
変換では書式・入力規則・人が後から入力した列（危険感など）を壊さないよう、
シートXMLの該当セルだけを文字列として差し替え、それ以外のパーツはそのまま写す。
（ElementTree で再シリアライズすると、Excel で保存し直したファイルの名前空間宣言が
落ちて「修復が必要」と言われることがあるため使わない。）
"""
from __future__ import annotations

import math
import posixpath
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def col_name(index: int) -> str:
    """0始まりの列番号 → 'A', 'B', ... 。"""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def split_ref(ref: str) -> tuple[int, int]:
    """'J7' → (行7, 列9[0始まり])。"""
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    if not match:
        raise ValueError(f"セル参照が不正です: {ref}")
    col = 0
    for ch in match.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(match.group(2)), col - 1


def first_sheet_path(book: ZipFile) -> str:
    """ブックの先頭シートの ZIP 内パス。"""
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    sheet = workbook.find("m:sheets/m:sheet", NS)
    if sheet is None:
        raise ValueError("シートがありません")
    rid = sheet.get(f"{{{NS['r']}}}id")
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("rel:Relationship", NS):
        if rel.get("Id") == rid:
            target = rel.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return posixpath.normpath(posixpath.join("xl", target))
    return "xl/worksheets/sheet1.xml"


def _shared_strings(book: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
            for si in root.findall("m:si", NS)]


def read_cells(path: Path) -> dict[tuple[int, int], object]:
    """先頭シートの全セル値を {(行, 列0始まり): 値} で返す。数値は float、他は str。"""
    with ZipFile(path) as book:
        strings = _shared_strings(book)
        root = ET.fromstring(book.read(first_sheet_path(book)))
    cells: dict[tuple[int, int], object] = {}
    for c in root.iter(f"{{{NS['m']}}}c"):
        ref = c.get("r")
        if not ref:
            continue
        kind = c.get("t", "")
        v = c.find("m:v", NS)
        if kind == "inlineStr":
            value: object = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
        elif kind == "s" and v is not None and v.text is not None:
            value = strings[int(v.text)]
        elif kind in ("str", "e", "b"):
            value = v.text if v is not None and v.text is not None else ""
        elif v is not None and v.text is not None:
            try:
                value = float(v.text)
            except ValueError:
                value = v.text
        else:
            value = ""
        cells[split_ref(ref)] = value
    return cells


def to_float(value: object) -> float | None:
    """セル値を数値へ（文字列の数字も可）。空欄や数値でなければ None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _cell_xml(ref: str, value: object, style: str | None) -> str:
    s = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s}/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return f'<c r="{ref}"{s}/>'
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{s}><is><t>{_xml_escape(str(value))}</t></is></c>'


_CELL_RE = r'<c r="{ref}"(\s[^>]*?)?(?:/>|>.*?</c>)'


def _set_cell(xml: str, ref: str, value: object, style: str | None) -> str:
    row_no, col_no = split_ref(ref)
    pattern = re.compile(_CELL_RE.format(ref=ref), re.S)
    match = pattern.search(xml)
    if match:
        if style is None:
            old = re.search(r'\ss="(\d+)"', match.group(1) or "")
            style = old.group(1) if old else None
        return xml[:match.start()] + _cell_xml(ref, value, style) + xml[match.end():]

    new_cell = _cell_xml(ref, value, style)
    row_match = re.search(rf'<row r="{row_no}"(\s[^>]*?)?(/>|>(.*?)</row>)', xml, re.S)
    if row_match:
        if row_match.group(2) == "/>":
            open_tag = xml[row_match.start():row_match.end() - 2] + ">"
            return xml[:row_match.start()] + open_tag + new_cell + "</row>" + xml[row_match.end():]
        body_start = row_match.start(3)
        body = row_match.group(3)
        # 列順を保って差し込む（Excel は列順が乱れたファイルを修復扱いにする）。
        for cm in re.finditer(r'<c r="([A-Z]+)\d+"', body):
            if split_ref(cm.group(1) + "1")[1] > col_no:
                pos = body_start + cm.start()
                return xml[:pos] + new_cell + xml[pos:]
        pos = body_start + len(body)
        return xml[:pos] + new_cell + xml[pos:]

    new_row = f'<row r="{row_no}">{new_cell}</row>'
    for rm in re.finditer(r'<row r="(\d+)"', xml):
        if int(rm.group(1)) > row_no:
            return xml[:rm.start()] + new_row + xml[rm.start():]
    if "<sheetData/>" in xml:
        return xml.replace("<sheetData/>", f"<sheetData>{new_row}</sheetData>", 1)
    pos = xml.index("</sheetData>")
    return xml[:pos] + new_row + xml[pos:]


def write_with_updates(src: Path, dest: Path,
                       updates: dict[str, tuple[object, str | None]]) -> None:
    """src を dest へ写し、先頭シートのセルだけ差し替える。

    ``updates`` は {"J7": (値, スタイル番号 or None)}。None なら既存セルのスタイルを保つ。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(src) as book:
        sheet_path = first_sheet_path(book)
        xml = book.read(sheet_path).decode("utf-8")
        for ref, (value, style) in updates.items():
            xml = _set_cell(xml, ref, value, style)
        # dimension を新しく増えたセルまで広げる（無くても開けるが、あれば合わせる）。
        dim = re.search(r'<dimension ref="([A-Z]+\d+)(?::([A-Z]+\d+))?"\s*/>', xml)
        if dim and updates:
            refs = [split_ref(r) for r in updates]
            last = split_ref(dim.group(2) or dim.group(1))
            max_row = max([last[0]] + [r for r, _ in refs])
            max_col = max([last[1]] + [c for _, c in refs])
            xml = xml[:dim.start()] + f'<dimension ref="{dim.group(1)}:{col_name(max_col)}{max_row}"/>' \
                + xml[dim.end():]
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with ZipFile(tmp, "w", ZIP_DEFLATED) as out:
            for item in book.infolist():
                data = xml.encode("utf-8") if item.filename == sheet_path else book.read(item.filename)
                out.writestr(item, data)
    tmp.replace(dest)
