from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

from edit_csv import EditPoint, analyze_edit_points, read_edit_points


FINAL_HEADERS = [
    "整理ID", "データ収集日", "被験者名またはID", "離隔距離（ｍ）",
    "自転車速度（km/h)", "自動車速度（km/h)", "危険感",
    "動画ファイル名", "動画のタイムスタンプ", "LiDARデータファイル名",
    "日本時間", "LiDARのタイムスタンプ", "車種", "",
]
VEHICLE_LIST = ["普通", "軽", "大型", "二輪"]
DANGER_LIST = ["非常に危険", "危険", "やや危険", "なし"]
DANGER_BY_LEVEL = {"0": "なし", "1": "やや危険", "2": "危険", "3": "非常に危険"}
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))


@dataclass
class Cell:
    value: object = ""
    style: int = 0


def _to_float(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _events_path(out_dir: Path) -> Path:
    reviewed = out_dir / "reviewed_overtaking_events.csv"
    if reviewed.is_file():
        return reviewed
    return out_dir / "overtaking_events.csv"


def _accepted_events(out_dir: Path) -> list[dict[str, str]]:
    path = _events_path(out_dir)
    if not path.is_file():
        raise FileNotFoundError("reviewed_overtaking_events.csv または overtaking_events.csv が見つかりません")
    rows = _read_csv_dicts(path)
    accepted = [row for row in rows if (row.get("review_status") or "採用") == "採用"]

    def key(row: dict[str, str]) -> tuple[int, str]:
        raw = row.get("event_id", "")
        return (int(raw) if raw.isdigit() else 999999, raw)

    return sorted(accepted, key=key)


def _event_id_for_path(event_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(event_id or "")) or "event"


def _collection_date(out_dir: Path, value: str = "") -> date | None:
    value = str(value or "").strip()
    if value:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%y%m%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                pass
    match = re.match(r"(\d{2})(\d{2})(\d{2})_", out_dir.name)
    if match:
        yy, mm, dd = map(int, match.groups())
        return date(2000 + yy, mm, dd)
    return None


def _excel_date(d: date | None) -> float | str:
    if d is None:
        return ""
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return (dt - EXCEL_EPOCH).days


def _excel_time_from_seconds(seconds: float | None) -> float | str:
    if seconds is None or not math.isfinite(seconds):
        return ""
    return seconds / 86400.0


def _excel_jst_time_from_unix(timestamp: float | None) -> float | str:
    if timestamp is None or not math.isfinite(timestamp):
        return ""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(JST)
    return (dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000) / 86400.0


def _cell_text(row: ET.Element, strings: list[str], col_index: int) -> str:
    for cell in row:
        ref = cell.attrib.get("r", "")
        if _col_index_from_ref(ref) != col_index:
            continue
        value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
        if cell.attrib.get("t") == "inlineStr":
            return "".join(t.text or "" for t in cell.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        if value is None:
            return ""
        text = value.text or ""
        if cell.attrib.get("t") == "s" and text:
            return strings[int(text)]
        return text
    return ""


def _col_index_from_ref(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch.upper()) - 64
    return index - 1


def _read_shared_strings(book: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return [
        "".join(t.text or "" for t in si.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        for si in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si")
    ]


def _gps_speed_rows(path: Path) -> list[tuple[float, float]]:
    if not path.is_file():
        return []
    with ZipFile(path) as book:
        sheet_name = "xl/worksheets/sheet2.xml" if "xl/worksheets/sheet2.xml" in book.namelist() else "xl/worksheets/sheet1.xml"
        strings = _read_shared_strings(book)
        sheet = ET.fromstring(book.read(sheet_name))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows = sheet.findall(f".//{ns}sheetData/{ns}row")
    if not rows:
        return []
    headers = [_cell_text(rows[0], strings, i) for i in range(12)]
    try:
        cts_col = headers.index("cts")
    except ValueError:
        cts_col = 0
    speed_col = next((i for i, header in enumerate(headers) if "GPS (2D speed)" in header), 5)
    result: list[tuple[float, float]] = []
    for row in rows[1:]:
        cts = _to_float(_cell_text(row, strings, cts_col))
        speed = _to_float(_cell_text(row, strings, speed_col))
        if cts is not None and speed is not None:
            # sheet2 is m/s. If sheet1 is used, the header is k/s.
            if "k/s" in headers[speed_col]:
                speed *= 1000.0
            result.append((cts, speed))
    return result


def _gps_cache(out_dir: Path) -> dict[str, list[tuple[float, float]]]:
    cache = {}
    gps_dir = out_dir / "gps"
    if not gps_dir.is_dir():
        return cache
    for book in gps_dir.glob("*/telemetry_gps.xlsx"):
        cache[book.parent.name.lower()] = _gps_speed_rows(book)
    return cache


def _avg_bicycle_speed_kmh(
    event: dict[str, str],
    phases: dict[str, float | EditPoint],
    gps_rows: dict[str, list[tuple[float, float]]],
    offset: float,
) -> float | str:
    source = Path(event.get("source_video", "")).stem.lower()
    rows = gps_rows.get(source)
    if not rows:
        return ""
    p3 = phases["phase3"]
    p4 = phases["phase4"]
    if not isinstance(p3, EditPoint) or not isinstance(p4, EditPoint):
        return ""
    global_offset = (_to_float(event.get("t_start_s")) or 0.0) - (_to_float(event.get("local_t_start_s")) or 0.0)
    start = p3.unixtime - offset - global_offset
    end = p4.unixtime - offset - global_offset
    lo, hi = sorted((start, end))
    speeds = [speed for cts, speed in rows if lo <= cts <= hi]
    if not speeds:
        return ""
    return sum(speeds) / len(speeds) * 3.6


def _round_or_blank(value: object, digits: int = 2) -> object:
    number = _to_float(value)
    if number is None or not math.isfinite(number):
        return ""
    return round(number, digits)


def _vehicle_speed_kmh(bicycle_speed: object, relative_speed: object) -> float | str:
    bicycle = _to_float(bicycle_speed)
    relative = _to_float(relative_speed)
    if bicycle is None or relative is None:
        return ""
    return bicycle + relative


def _final_rows(
    out_dir: Path,
    organize_id: str,
    subject_id: str,
    collection_date: str,
    offset: float,
    pcap_path: str,
    pcap_start_unix: float | None,
) -> tuple[list[list[Cell]], list[str]]:
    rows: list[list[Cell]] = [[Cell(value, 1) for value in FINAL_HEADERS] + [Cell("") for _ in range(6)] + [Cell("危険感", 1)]]
    warnings: list[str] = []
    events = _accepted_events(out_dir)
    gps_rows = _gps_cache(out_dir)
    data_date = _excel_date(_collection_date(out_dir, collection_date))
    pcap_name = Path(pcap_path).name if pcap_path else ""

    for event in events:
        event_id = event.get("event_id", "")
        edit_path = out_dir / "lidar_edit" / f"overtaking_{_event_id_for_path(event_id)}_edit.csv"
        metrics: dict[str, object] = {}
        phase3_time: float | None = None
        try:
            points = read_edit_points(edit_path)
            metrics = analyze_edit_points(points)
            p3 = metrics.get("phase3")
            if isinstance(p3, EditPoint):
                phase3_time = p3.unixtime
        except Exception as exc:
            warnings.append(f"候補 {event_id}: 分析できません ({exc})")

        lidar_timestamp = (pcap_start_unix + phase3_time) if pcap_start_unix is not None and phase3_time is not None else phase3_time
        if pcap_start_unix is None and phase3_time is not None:
            warnings.append(f"候補 {event_id}: PCAP開始Unix時刻がないためLiDAR時刻は相対秒です")

        bike_speed = ""
        if metrics:
            bike_speed = _avg_bicycle_speed_kmh(event, metrics, gps_rows, offset)
        vehicle_speed = _vehicle_speed_kmh(bike_speed, metrics.get("relative_speed"))
        row = [
            Cell(organize_id or event_id, 0),
            Cell(data_date, 2),
            Cell(subject_id, 0),
            Cell(_round_or_blank(metrics.get("lc")), 4),
            Cell(_round_or_blank(bike_speed), 4),
            Cell(_round_or_blank(vehicle_speed), 4),
            Cell(DANGER_BY_LEVEL.get(str(event.get("danger_level", "0")), "なし"), 0),
            Cell(Path(event.get("source_video", "")).stem, 0),
            Cell(_excel_time_from_seconds(_to_float(event.get("local_t_start_s"))), 3),
            Cell(pcap_name, 0),
            Cell(_excel_jst_time_from_unix(lidar_timestamp if pcap_start_unix is not None else None), 3),
            Cell(_round_or_blank(lidar_timestamp, 6), 5),
            Cell(event.get("class", ""), 0),
            Cell("", 0),
        ]
        rows.append(row + [Cell("") for _ in range(7)])

    while len(rows) < 51:
        rows.append([Cell("") for _ in range(21)])

    for i, value in enumerate(VEHICLE_LIST, start=1):
        rows[i][19] = Cell(value)
    for i, value in enumerate(DANGER_LIST, start=1):
        rows[i][20] = Cell(value)
    return rows, warnings


def _col_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _xlsx_cell(row: int, col: int, cell: Cell) -> str:
    ref = f"{_col_name(col)}{row}"
    style = f' s="{cell.style}"' if cell.style else ""
    value = cell.value
    if value is None:
        value = ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            value = ""
        else:
            return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = _xml_escape(str(value))
    return f'<c r="{ref}" t="inlineStr"{style}><is><t>{text}</t></is></c>'


def _sheet_xml(rows: list[list[Cell]]) -> str:
    cols = (
        '<cols>'
        '<col min="1" max="1" width="10" customWidth="1"/>'
        '<col min="2" max="3" width="16" customWidth="1"/>'
        '<col min="4" max="6" width="17" customWidth="1"/>'
        '<col min="7" max="8" width="15" customWidth="1"/>'
        '<col min="9" max="12" width="22" customWidth="1"/>'
        '<col min="13" max="14" width="16" customWidth="1"/>'
        '<col min="20" max="21" width="14" customWidth="1"/>'
        '</cols>'
    )
    row_xml = []
    for r_idx, row in enumerate(rows, 1):
        cells = "".join(_xlsx_cell(r_idx, c_idx, cell) for c_idx, cell in enumerate(row))
        row_xml.append(f'<row r="{r_idx}">{cells}</row>')
    validations = (
        '<dataValidations count="2">'
        '<dataValidation type="list" allowBlank="1" showInputMessage="1" showErrorMessage="1" sqref="G2:G51"><formula1>$U$2:$U$5</formula1></dataValidation>'
        '<dataValidation type="list" allowBlank="1" showInputMessage="1" showErrorMessage="1" sqref="M2:M51"><formula1>$T$2:$T$5</formula1></dataValidation>'
        '</dataValidations>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'{cols}<sheetData>{"".join(row_xml)}</sheetData>{validations}</worksheet>'
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="3"><numFmt numFmtId="164" formatCode="yyyy/m/d"/><numFmt numFmtId="165" formatCode="h:mm:ss"/><numFmt numFmtId="166" formatCode="0.00"/></numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Yu Gothic"/></font><font><b/><sz val="11"/><name val="Yu Gothic"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE7E6E6"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border/><border><left style="thin"><color rgb="FFD9D9D9"/></left><right style="thin"><color rgb="FFD9D9D9"/></right><top style="thin"><color rgb="FFD9D9D9"/></top><bottom style="thin"><color rgb="FFD9D9D9"/></bottom></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _write_workbook(out_path: Path, rows: list[list[Cell]]) -> None:
    with ZipFile(out_path, "w", ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
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
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/styles.xml", _styles_xml())
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
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


def generate_final_workbook(
    out_dir: Path,
    organize_id: str = "",
    subject_id: str = "",
    collection_date: str = "",
    offset: float = 0.0,
    pcap_path: str = "",
    pcap_start_unix: float | None = None,
) -> dict:
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise FileNotFoundError(f"出力フォルダが見つかりません: {out_dir}")
    rows, warnings = _final_rows(
        out_dir, organize_id, subject_id, collection_date, offset, pcap_path, pcap_start_unix
    )
    final_dir = out_dir / "final"
    final_dir.mkdir(exist_ok=True)
    out_path = final_dir / f"{out_dir.name}_final.xlsx"
    _write_workbook(out_path, rows)
    data_rows = sum(1 for row in rows[1:51] if any(str(cell.value or "").strip() for cell in row[:14]))
    return {"path": str(out_path), "rows": data_rows, "warnings": warnings}
