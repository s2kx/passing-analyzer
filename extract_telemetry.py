"""
GoPro GPMF テレメトリ抽出

GoProのMP4ファイルからGPMF (GoPro Metadata Format) テレメトリを抽出し、
GPS・加速度計・ジャイロスコープ等のデータをCSVに保存する。

使い方:
    python extract_telemetry.py 動画.MP4
    python extract_telemetry.py 動画.MP4 --out output_dir

出力 (--out で指定したフォルダ、省略時は <動画名>_telemetry/):
    telemetry_gps.csv    : GPS軌跡 (緯度, 経度, 高度[m], 速度2D[m/s], 速度3D[m/s])
    telemetry_accl.csv   : 加速度計 (x, y, z) [m/s²]
    telemetry_gyro.csv   : ジャイロスコープ (x, y, z) [rad/s]
    telemetry_cori.csv   : カメラ姿勢クォータニオン (w, x, y, z)
    telemetry_iori.csv   : 画像姿勢クォータニオン (w, x, y, z)
    telemetry_grav.csv   : 重力ベクトル (x, y, z)
    その他のストリームも telemetry_<key>.csv として出力
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

# Windows cp932 環境でのUnicode出力エラーを回避
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ── MP4ボックス解析 ──────────────────────────────────────────────────────────

def _boxes(buf: bytes, s: int = 0, e: int | None = None):
    """buf[s:e] 内の各MP4ボックスを (box_type, content_start, content_end) で yield."""
    e = len(buf) if e is None else e
    while s + 8 <= e:
        sz = struct.unpack_from(">I", buf, s)[0]
        bt = buf[s + 4:s + 8].decode("latin-1")
        if sz == 1:
            if s + 16 > e:
                break
            sz = struct.unpack_from(">Q", buf, s + 8)[0]
            cs = s + 16
        elif sz == 0:
            cs = s + 8
            sz = e - s
        else:
            cs = s + 8
        if sz < 8:
            break
        yield bt, cs, s + sz
        s += sz


def _find(buf: bytes, *path: str, s: int = 0, e: int | None = None) -> tuple[int, int] | None:
    rs, re = s, (len(buf) if e is None else e)
    for name in path:
        match = next(((cs, ce) for bt, cs, ce in _boxes(buf, rs, re) if bt == name), None)
        if match is None:
            return None
        rs, re = match
    return rs, re


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from(">Q", buf, off)[0]


def _timescale(moov: bytes, ts: int, te: int) -> int:
    loc = _find(moov, "mdia", "mdhd", s=ts, e=te)
    if not loc:
        return 1000
    ms = loc[0]
    return _u32(moov, ms + 20) if moov[ms] == 1 else _u32(moov, ms + 12)


def _stts(moov: bytes, ss: int, se: int) -> list[tuple[int, int]]:
    loc = _find(moov, "stts", s=ss, e=se)
    if not loc:
        return []
    ms, me = loc
    return [struct.unpack_from(">II", moov, ms + 8 + i * 8)
            for i in range(_u32(moov, ms + 4)) if ms + 8 + i * 8 + 8 <= me]


def _chunks(moov: bytes, ss: int, se: int) -> list[int]:
    loc = _find(moov, "stco", s=ss, e=se)
    if loc:
        ms, me = loc
        return [_u32(moov, ms + 8 + i * 4)
                for i in range(_u32(moov, ms + 4)) if ms + 8 + i * 4 + 4 <= me]
    loc = _find(moov, "co64", s=ss, e=se)
    if loc:
        ms, me = loc
        return [_u64(moov, ms + 8 + i * 8)
                for i in range(_u32(moov, ms + 4)) if ms + 8 + i * 8 + 8 <= me]
    return []


def _sizes(moov: bytes, ss: int, se: int) -> list[int]:
    loc = _find(moov, "stsz", s=ss, e=se)
    if not loc:
        return []
    ms, me = loc
    default, n = _u32(moov, ms + 4), _u32(moov, ms + 8)
    if default:
        return [default] * n
    return [_u32(moov, ms + 12 + i * 4) for i in range(n) if ms + 12 + i * 4 + 4 <= me]


def _stsc(moov: bytes, ss: int, se: int) -> list[tuple[int, int, int]]:
    loc = _find(moov, "stsc", s=ss, e=se)
    if not loc:
        return []
    ms, me = loc
    return [struct.unpack_from(">III", moov, ms + 8 + i * 12)
            for i in range(_u32(moov, ms + 4)) if ms + 8 + i * 12 + 12 <= me]


def _sample_file_offset(idx: int, chunk_offsets: list[int],
                        stsc_entries: list[tuple[int, int, int]],
                        sample_sizes: list[int]) -> int | None:
    if idx >= len(sample_sizes):
        return None
    if not stsc_entries:
        return chunk_offsets[idx] if idx < len(chunk_offsets) else None
    pos = idx
    chunk_1 = 1
    for i, (fc, spc, _) in enumerate(stsc_entries):
        next_fc = stsc_entries[i + 1][0] if i + 1 < len(stsc_entries) else None
        if next_fc is None:
            chunk_1 = fc + pos // spc
            pos = pos % spc
            break
        span = (next_fc - fc) * spc
        if pos < span:
            chunk_1 = fc + pos // spc
            pos = pos % spc
            break
        pos -= span
    c0 = chunk_1 - 1
    if c0 >= len(chunk_offsets):
        return None
    first_in_chunk = idx - pos
    return chunk_offsets[c0] + sum(sample_sizes[first_in_chunk:first_in_chunk + pos])


def _sample_pts(idx: int, stts_entries: list[tuple[int, int]]) -> tuple[int, int]:
    """サンプル idx の (PTS_ticks, DUR_ticks) を返す."""
    tick = 0
    rem = idx
    for cnt, dur in stts_entries:
        if rem < cnt:
            return tick + rem * dur, dur
        tick += cnt * dur
        rem -= cnt
    return tick, 0


# ── GPMF型デコード ──────────────────────────────────────────────────────────

_GPMF_SCALAR_FMT: dict[str, tuple[str, int]] = {
    "b": (">b", 1),
    "B": (">B", 1),
    "s": (">h", 2),
    "S": (">H", 2),
    "l": (">i", 4),
    "L": (">I", 4),
    "j": (">q", 8),
    "J": (">Q", 8),
    "f": (">f", 4),
    "d": (">d", 8),
}

# STRMの中でデータキーの前に現れるメタデータキー (スキップ対象)
_STRM_META_KEYS = frozenset({
    "STMP",  # マイクロ秒タイムスタンプ
    "TSMP",  # 累積サンプル数
    "STNM",  # ストリーム名
    "SIUN",  # SI単位
    "UNIT",  # 表示単位
    "SCAL",  # スケールファクタ
    "MTRX",  # 変換行列
    "ORIN",  # 元の向き
    "ORIO",  # 出力向き
    "MFOV",  # 最小視野角
    "MINF",  # 追加情報
    "GPSF",  # GPSフィックス種別
    "GPSU",  # GPS UTCタイムスタンプ
    "GPSP",  # GPS精度 (PDOP×100)
    "GPSA",  # GPS代替高度
    "DVNM",  # デバイス名
    "DVID",  # デバイスID
    "TMPC",  # 温度
    "TYPE",  # 複合型定義
    "LRVO",  # LRVオフセット
    "LRVS",  # LRVスケール
    "EMPT",  # 空エントリ
    "AALP",  # 音声レベル (メタ)
})


def _gpmf_iter(buf: bytes, offset: int = 0, end: int | None = None):
    """GPMFバッファを走査して (key, type_c, elem_sz, cnt, val_bytes) を yield."""
    pos = offset
    end = len(buf) if end is None else end
    while pos + 8 <= end:
        key = buf[pos:pos + 4].decode("latin-1", errors="replace")
        type_b = buf[pos + 4]
        type_c = chr(type_b) if type_b != 0 else "\x00"
        elem_sz = buf[pos + 5]
        cnt = struct.unpack_from(">H", buf, pos + 6)[0]
        total = elem_sz * cnt
        val = buf[pos + 8:pos + 8 + total]
        aligned = (total + 3) & ~3
        pos += 8 + aligned
        yield key, type_c, elem_sz, cnt, val


def _decode_scal(type_c: str, elem_sz: int, cnt: int, data: bytes) -> list[float]:
    """SCALエントリをデコードしてスケールファクタリストを返す."""
    fmt_info = _GPMF_SCALAR_FMT.get(type_c)
    if fmt_info is None:
        return [1.0]
    fmt, scalar_sz = fmt_info
    result = []
    for i in range(cnt):
        off = i * elem_sz
        if off + scalar_sz > len(data):
            break
        result.append(float(struct.unpack_from(fmt, data, off)[0]))
    return result if result else [1.0]


def _decode_row(type_c: str, elem_sz: int, data: bytes) -> list:
    """1行分のデータをデコードして値リストを返す."""
    if type_c in ("c", "U"):
        return [data.decode("ascii", errors="replace").rstrip("\x00")]

    fmt_info = _GPMF_SCALAR_FMT.get(type_c)
    if fmt_info is None:
        return []  # 未知型 (TYPE定義が必要な複合型など) はスキップ

    fmt, scalar_sz = fmt_info
    n_elems = elem_sz // scalar_sz if scalar_sz > 0 else 0
    values = []
    for i in range(n_elems):
        off = i * scalar_sz
        if off + scalar_sz > len(data):
            break
        values.append(struct.unpack_from(fmt, data, off)[0])
    return values


def _apply_scal(values: list[float], scal: list[float]) -> list[float]:
    """SCALでスケーリング. scal が1要素なら全値に適用."""
    if not scal:
        return [float(v) for v in values]
    result = []
    for i, v in enumerate(values):
        s = scal[i] if i < len(scal) else scal[-1]
        result.append(float(v) / s if s != 0 else float(v))
    return result


# ── GPMFストリーム解析 ───────────────────────────────────────────────────────

def _decode_meta_value(type_c: str, elem_sz: int, cnt: int, data: bytes):
    if type_c in ("c", "U"):
        return data.decode("ascii", errors="replace").rstrip("\x00")
    values = []
    for i in range(cnt):
        row = _decode_row(type_c, elem_sz, data[i * elem_sz:(i + 1) * elem_sz])
        values.extend(row)
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _parse_gpsu(value: object) -> str:
    if not value:
        return ""
    text = str(value).strip().rstrip("\x00")
    for fmt in ("%y%m%d%H%M%S.%f", "%y%m%d%H%M%S"):
        try:
            return _dt.datetime.strptime(text, fmt).isoformat(sep=" ", timespec="milliseconds")
        except ValueError:
            pass
    return text


@dataclass
class TelemetryStream:
    key: str
    name: str = ""
    si_unit: str = ""
    display_unit: str = ""
    scal: list[float] = field(default_factory=lambda: [1.0])
    rows: list[tuple[float, list[float]]] = field(default_factory=list)
    meta_rows: list[dict[str, object]] = field(default_factory=list)
    # rows: [(timestamp_s, [val1, val2, ...]), ...]


def _parse_strm(data: bytes, pts_s: float, dur_s: float) -> TelemetryStream | None:
    """
    1つのSTRMブロックを解析してTelemetryStreamを返す.
    サブサンプルのタイムスタンプは pts_s + (i + 0.5) / N * dur_s で計算.
    """
    scal: list[float] = [1.0]
    name = ""
    si_unit = ""
    display_unit = ""
    meta: dict[str, object] = {}
    custom_type: str | None = None  # TYPE キーで定義された複合型

    # 1パス目: メタデータ収集
    for key, type_c, elem_sz, cnt, val in _gpmf_iter(data):
        if key == "STNM":
            name = val.decode("utf-8", errors="replace").rstrip("\x00")
        elif key == "SIUN":
            si_unit = val.decode("ascii", errors="replace").rstrip("\x00")
        elif key == "UNIT":
            display_unit = val.decode("ascii", errors="replace").rstrip("\x00")
        elif key == "SCAL":
            scal = _decode_scal(type_c, elem_sz, cnt, val)
        elif key == "TYPE":
            custom_type = val.decode("ascii", errors="replace").rstrip("\x00")
        elif key in ("GPSU", "GPSF", "GPSP", "GPSA"):
            meta[key] = _decode_meta_value(type_c, elem_sz, cnt, val)

    # 2パス目: データキー検出
    for key, type_c, elem_sz, cnt, val in _gpmf_iter(data):
        # メタデータキーはスキップ
        if key in _STRM_META_KEYS:
            continue
        # コンテナ型はスキップ
        if type_c == "\x00":
            continue
        # TYPE定義が必要な複合型 ('?') はスキップ
        if type_c == "?":
            continue
        # 文字列のみのキーはデータとして扱わない
        if type_c in ("c", "U") and key not in ("GPS5",):
            continue
        # 4文字英数字でないキーはスキップ (破損データ対策)
        if not all(c.isalnum() for c in key):
            continue

        stream = TelemetryStream(
            key=key, name=name, si_unit=si_unit,
            display_unit=display_unit, scal=scal
        )

        for i in range(cnt):
            row_bytes = val[i * elem_sz:(i + 1) * elem_sz]
            if len(row_bytes) < elem_sz:
                break
            raw_vals = _decode_row(type_c, elem_sz, row_bytes)
            if not raw_vals:
                continue
            # 数値型にSCALを適用
            if all(isinstance(v, (int, float)) for v in raw_vals):
                scaled = _apply_scal([float(v) for v in raw_vals], scal)
            else:
                scaled = [float(v) if isinstance(v, (int, float)) else v for v in raw_vals]
            # サブサンプル中央のタイムスタンプ
            t = pts_s + (i + 0.5) / cnt * dur_s if cnt > 0 else pts_s
            stream.rows.append((t, scaled))
            stream.meta_rows.append(dict(meta))

        return stream  # STRMには通常データキーは1つ

    return None


def _parse_gpmf_sample(payload: bytes, pts_s: float, dur_s: float) -> list[TelemetryStream]:
    """1つのGPMFサンプルペイロードを解析して全ストリームを返す."""
    streams: list[TelemetryStream] = []

    for key, type_c, elem_sz, cnt, val in _gpmf_iter(payload):
        if key == "DEVC" and type_c == "\x00":
            for skey, stype, selem_sz, scnt, sval in _gpmf_iter(val):
                if skey == "STRM" and stype == "\x00":
                    st = _parse_strm(sval, pts_s, dur_s)
                    if st is not None and st.rows:
                        streams.append(st)

    return streams


# ── メイン処理 ──────────────────────────────────────────────────────────────

def extract_telemetry(mp4_path: Path) -> dict[str, TelemetryStream]:
    """
    GoProのMP4からテレメトリを抽出してストリーム辞書を返す.
    戻り値: {stream_key: TelemetryStream}  (同一キーの rows は結合)
    """
    moov: bytes | None = None
    with mp4_path.open("rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            sz = struct.unpack(">I", hdr[:4])[0]
            bt = hdr[4:8].decode("latin-1")
            if sz == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                sz = struct.unpack(">Q", ext)[0]
                content_sz = sz - 16
            else:
                content_sz = sz - 8
            if bt == "moov":
                moov = f.read(content_sz)
                break
            f.seek(content_sz, 1)

    if moov is None:
        raise ValueError("moovボックスが見つかりません。有効なMP4ファイルか確認してください。")

    # GPMFトラック (gpmdコーデック) を検索
    gpmf_trak: tuple[int, int] | None = None
    for bt, cs, ce in _boxes(moov):
        if bt != "trak":
            continue
        stbl = _find(moov, "mdia", "minf", "stbl", s=cs, e=ce)
        if not stbl:
            continue
        stsd = _find(moov, "stsd", s=stbl[0], e=stbl[1])
        if not stsd or stsd[0] + 16 > stsd[1]:
            continue
        codec = moov[stsd[0] + 12:stsd[0] + 16].decode("latin-1", errors="replace")
        if codec == "gpmd":
            gpmf_trak = (cs, ce)
            break

    if gpmf_trak is None:
        raise ValueError(
            "GoPro GPMFトラック (gpmd) が見つかりません。\n"
            "GPS有効で撮影したGoPro動画 (Hero5以降) を指定してください。"
        )

    ts, te = gpmf_trak
    timescale = _timescale(moov, ts, te)
    stbl = _find(moov, "mdia", "minf", "stbl", s=ts, e=te)
    if not stbl:
        raise ValueError("GPMFトラックのstblが見つかりません")
    ss, se = stbl

    stts_entries = _stts(moov, ss, se)
    chunk_offsets = _chunks(moov, ss, se)
    sample_sizes = _sizes(moov, ss, se)
    stsc_entries = _stsc(moov, ss, se)

    if not chunk_offsets or not sample_sizes:
        raise ValueError("GPMFサンプルテーブルが不完全です")

    all_streams: dict[str, TelemetryStream] = {}
    n = len(sample_sizes)
    print(f"GPMFサンプル数: {n}、タイムスケール: {timescale}")

    with mp4_path.open("rb") as f:
        for idx in range(n):
            off = _sample_file_offset(idx, chunk_offsets, stsc_entries, sample_sizes)
            if off is None:
                continue
            f.seek(off)
            payload = f.read(sample_sizes[idx])

            pts_ticks, dur_ticks = _sample_pts(idx, stts_entries)
            pts_s = pts_ticks / timescale
            dur_s = dur_ticks / timescale

            for st in _parse_gpmf_sample(payload, pts_s, dur_s):
                if st.key not in all_streams:
                    all_streams[st.key] = TelemetryStream(
                        key=st.key, name=st.name,
                        si_unit=st.si_unit, display_unit=st.display_unit,
                        scal=st.scal
                    )
                all_streams[st.key].rows.extend(st.rows)
                all_streams[st.key].meta_rows.extend(st.meta_rows)

            if (idx + 1) % 100 == 0 or idx == n - 1:
                print(f"  {idx + 1}/{n} 処理完了", end="\r")

    print()
    return all_streams


# ── CSV出力 ─────────────────────────────────────────────────────────────────

# 既知ストリームのカラム定義
_STREAM_COLS: dict[str, list[str]] = {
    "GPS5": ["latitude_deg", "longitude_deg", "altitude_m", "speed2d_mps", "speed3d_mps"],
    "ACCL": ["ax_mps2", "ay_mps2", "az_mps2"],
    "GYRO": ["gx_rads", "gy_rads", "gz_rads"],
    "CORI": ["qw", "qx", "qy", "qz"],
    "IORI": ["qw", "qx", "qy", "qz"],
    "GRAV": ["gx", "gy", "gz"],
    "MAGN": ["mx_uT", "my_uT", "mz_uT"],
    "SHUT": ["shutter_s"],
    "ISOE": ["iso"],
    "WBAL": ["wb_kelvin"],
    "YAVG": ["luma_avg"],
    "UNIF": ["uniformity"],
    "WNDM": ["wind_enable", "wind_level"],
    "MWET": ["mic_wet", "all_mics", "confidence"],
    "LSKP": ["lrv_frame_skip"],
    "MSKP": ["mrv_frame_skip"],
}

# CSV出力時のファイル名サフィックス
_STREAM_SUFFIX: dict[str, str] = {
    "GPS5": "gps",
    "ACCL": "accl",
    "GYRO": "gyro",
    "CORI": "cori",
    "IORI": "iori",
    "GRAV": "grav",
    "MAGN": "magn",
}


def _write_csv(stream: TelemetryStream, out_path: Path, col_names: list[str] | None) -> int:
    if not stream.rows:
        return 0
    n_cols = len(stream.rows[0][1]) if stream.rows else 0
    if col_names:
        headers = col_names[:n_cols]
        for i in range(len(headers), n_cols):
            headers.append(f"col{i}")
    else:
        headers = [f"col{i}" for i in range(n_cols)]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s"] + headers)
        for t, vals in stream.rows:
            row = [f"{t:.6f}"]
            for v in vals:
                if isinstance(v, float):
                    row.append(f"{v:.8g}")
                else:
                    row.append(str(v))
            writer.writerow(row)
    return len(stream.rows)


def _gps_row_values(t: float, vals: list, meta: dict[str, object], speed2d_unit: str) -> list:
    speed2d = vals[3] if len(vals) > 3 else ""
    if speed2d_unit == "k/s" and isinstance(speed2d, (int, float)):
        speed2d = float(speed2d) / 1000.0
    return [
        round(float(t), 6),
        _parse_gpsu(meta.get("GPSU")),
        vals[0] if len(vals) > 0 else "",
        vals[1] if len(vals) > 1 else "",
        vals[2] if len(vals) > 2 else "",
        speed2d,
        vals[4] if len(vals) > 4 else "",
        meta.get("GPSF", ""),
        meta.get("GPSP", ""),
        meta.get("GPSA", ""),
    ]


def _gps_rows_every_second(gps: TelemetryStream) -> list[list]:
    rows: list[list] = []
    seen_seconds: set[int] = set()
    for idx, (t, vals) in enumerate(gps.rows):
        second = int(float(t))
        if second in seen_seconds:
            continue
        seen_seconds.add(second)
        meta = gps.meta_rows[idx] if idx < len(gps.meta_rows) else {}
        rows.append(_gps_row_values(t, vals, meta, "k/s"))
    return rows


def _gps_rows_default(gps: TelemetryStream) -> list[list]:
    rows: list[list] = []
    for idx, (t, vals) in enumerate(gps.rows):
        meta = gps.meta_rows[idx] if idx < len(gps.meta_rows) else {}
        rows.append(_gps_row_values(t, vals, meta, "m/s"))
    return rows


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
        '<sheetData>' + "".join(row_xml) + '</sheetData>'
        '</worksheet>'
    )


def _write_simple_xlsx(out_path: Path, sheets: list[tuple[str, list[list]]]) -> None:
    workbook_sheets = []
    rels = []
    content_types = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for idx, (name, _rows) in enumerate(sheets, 1):
        workbook_sheets.append(f'<sheet name="{_xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')
        rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    with ZipFile(out_path, "w", ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(content_types) + '</Types>'
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
            '<sheets>' + "".join(workbook_sheets) + '</sheets></workbook>'
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + '</Relationships>'
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
        for idx, (_name, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{idx}.xml", _xlsx_sheet_xml(rows))


def save_gps_workbook(streams: dict[str, TelemetryStream], out_dir: Path) -> Path | None:
    gps = streams.get("GPS5")
    if gps is None or not gps.rows:
        return None
    headers_1 = [
        "cts", "date", "GPS (Lat.) [deg]", "GPS (Long.) [deg]", "GPS (Alt.) [m]",
        "GPS (2D speed) [k/s]", "GPS (3D speed) [m/s]", "fix", "precision", "altitude system",
    ]
    headers_2 = [
        "cts", "date", "GPS (Lat.) [deg]", "GPS (Long.) [deg]", "GPS (Alt.) [m]",
        "GPS (2D speed) [m/s]", "GPS (3D speed) [m/s]", "fix", "precision", "altitude system",
    ]
    out_path = out_dir / "telemetry_gps.xlsx"
    _write_simple_xlsx(out_path, [
        ("1", [headers_1] + _gps_rows_every_second(gps)),
        ("2", [headers_2] + _gps_rows_default(gps)),
    ])
    return out_path


def save_telemetry(streams: dict[str, TelemetryStream], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    for key, st in streams.items():
        suffix = _STREAM_SUFFIX.get(key, key.lower())
        out_path = out_dir / f"telemetry_{suffix}.csv"
        cols = _STREAM_COLS.get(key)
        n = _write_csv(st, out_path, cols)
        unit_str = st.si_unit or st.display_unit or "?"
        print(f"  {out_path.name}: {n:>6} 行  [{key}] {st.name or ''}  ({unit_str})")


# ── エントリポイント ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GoProのMP4からGPMFテレメトリを抽出してCSVに保存する"
    )
    parser.add_argument("mp4", help="入力MP4ファイル")
    parser.add_argument("--out", "-o", default=None,
                        help="出力フォルダ (省略時は <動画名>_telemetry/)")
    args = parser.parse_args()

    mp4_path = Path(args.mp4)
    if not mp4_path.exists():
        sys.exit(f"エラー: {mp4_path} が見つかりません")

    out_dir = (Path(args.out) if args.out
               else mp4_path.parent / (mp4_path.stem + "_telemetry"))

    print(f"動画: {mp4_path}")
    print(f"出力: {out_dir}")
    print()

    try:
        streams = extract_telemetry(mp4_path)
    except ValueError as e:
        sys.exit(f"エラー: {e}")

    if not streams:
        sys.exit("テレメトリストリームが見つかりませんでした。")

    keys = list(streams.keys())
    print(f"\n検出されたストリーム ({len(keys)}件): {keys}")
    print("CSV出力中...")
    save_telemetry(streams, out_dir)
    gps_book = save_gps_workbook(streams, out_dir)
    if gps_book:
        print(f"GPS Excel: {gps_book.name}")
    print(f"\n完了。{out_dir} に保存しました。")


if __name__ == "__main__":
    main()
