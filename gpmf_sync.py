"""GPS-based time sync between GoPro MP4 (GPMF telemetry) and Velodyne PCAP.

Algorithm
---------
1. Load the MP4's moov box into memory (typically 1-10 MB; mdat is skipped).
2. Locate the GoPro metadata track by its 'gpmd' codec in stsd.
3. Read the first GPMF payload that contains a GPSU (GPS UTC timestamp) entry.
4. Subtract the sample's presentation time → UTC at video t=0.
5. Compare with the PCAP's first-packet Unix timestamp → elapsed-time offset.

No third-party dependencies — pure standard library only.
"""
from __future__ import annotations

import datetime
import struct
from pathlib import Path


class SyncError(Exception):
    pass


# ── MP4 box helpers (in-memory buffer) ──────────────────────────────────────

def _boxes(buf: bytes, s: int = 0, e: int | None = None):
    """Yield (box_type, content_start, content_end) for each MP4 box in buf[s:e]."""
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
    """Find a nested box by path. Returns (content_start, content_end) or None."""
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


# ── Sample table helpers ─────────────────────────────────────────────────────

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
    """Return the file byte offset of sample[idx]."""
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


# ── GPMF helpers ─────────────────────────────────────────────────────────────

def _gpmf_iter(payload: bytes):
    """Yield (key, type_char, size_per, count, value_bytes) from a GPMF payload."""
    pos = 0
    while pos + 8 <= len(payload):
        key = payload[pos:pos + 4].decode("latin-1", errors="replace")
        type_c = chr(payload[pos + 4]) if payload[pos + 4] < 128 else "\x00"
        sz = payload[pos + 5]
        cnt = struct.unpack_from(">H", payload, pos + 6)[0]
        total = sz * cnt
        val = payload[pos + 8:pos + 8 + total]
        pos += 8 + ((total + 3) & ~3)
        yield key, type_c, sz, cnt, val


def _find_gpsu(payload: bytes) -> bytes | None:
    """Recursively search GPMF payload for the first GPSU value."""
    for key, type_c, sz, _cnt, val in _gpmf_iter(payload):
        if key == "GPSU" and sz >= 12:
            return val[:sz]
        if type_c == "\x00" and key in ("DEVC", "STRM"):
            found = _find_gpsu(val)
            if found is not None:
                return found
    return None


def _parse_gpsu(raw: bytes) -> datetime.datetime | None:
    """Parse GoPro GPSU bytes 'YYMMDDHHMMSS.SSS' → UTC datetime."""
    try:
        text = raw.decode("ascii", errors="ignore").rstrip("\x00").strip()
        if len(text) < 12:
            return None
        y = 2000 + int(text[0:2])
        mo, d = int(text[2:4]), int(text[4:6])
        h, mi = int(text[6:8]), int(text[8:10])
        s_str = text[10:]
        s = float(s_str) if s_str and s_str.replace(".", "").isdigit() else float(text[10:12] or "0")
        si, us = int(s), round((s - int(s)) * 1_000_000)
        return datetime.datetime(y, mo, d, h, mi, si, us, tzinfo=datetime.timezone.utc)
    except (ValueError, IndexError):
        return None


def _sample_pts_seconds(
    idx: int, stts_entries: list[tuple[int, int]], timescale: int
) -> float:
    tick, rem = 0, idx
    for cnt, dur in stts_entries:
        take = min(cnt, rem)
        tick += take * dur
        rem -= take
        if rem <= 0:
            break
    return tick / timescale


def _stable_start_utc(estimates: list[datetime.datetime]) -> datetime.datetime:
    if not estimates:
        raise SyncError(
            "GPS UTCタイムスタンプ（GPSU）が見つかりませんでした。\n"
            "GoProの設定でGPSをONにして撮影した動画か確認してください（デフォルトはON）。"
        )
    timestamps = sorted(dt.timestamp() for dt in estimates)
    middle = len(timestamps) // 2
    if len(timestamps) % 2:
        median = timestamps[middle]
    else:
        median = (timestamps[middle - 1] + timestamps[middle]) / 2
    clustered = [value for value in timestamps if abs(value - median) <= 0.5]
    if not clustered:
        clustered = timestamps
    stable = sum(clustered) / len(clustered)
    return datetime.datetime.fromtimestamp(stable, tz=datetime.timezone.utc)


# ── Public API ───────────────────────────────────────────────────────────────

def extract_gps_utc_at_start(mp4_path: Path) -> datetime.datetime:
    """
    Parse a GoPro MP4 and return the GPS UTC datetime of video frame t=0.
    Raises SyncError when GPS/GPMF data is unavailable.
    """
    # Stream through the file to find and load the moov box (skips large mdat)
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
        raise SyncError("moovボックスが見つかりません。有効なMP4ファイルか確認してください。")

    # Find the GoPro metadata track by its 'gpmd' codec
    gpmf_trak: tuple[int, int] | None = None
    for bt, cs, ce in _boxes(moov):
        if bt != "trak":
            continue
        stbl = _find(moov, "mdia", "minf", "stbl", s=cs, e=ce)
        if not stbl:
            continue
        stsd = _find(moov, "stsd", s=stbl[0], e=stbl[1])
        # stsd content: ver(1)+flags(3)+count(4) then entries size(4)+codec(4)+...
        if not stsd or stsd[0] + 16 > stsd[1]:
            continue
        codec = moov[stsd[0] + 12:stsd[0] + 16].decode("latin-1", errors="replace")
        if codec == "gpmd":
            gpmf_trak = (cs, ce)
            break

    if gpmf_trak is None:
        raise SyncError(
            "GoPro GPMFトラック（gpmd）が見つかりません。\n"
            "GoProカメラでGPSを有効にして撮影した動画を指定してください（Hero5以降）。"
        )

    ts, te = gpmf_trak
    timescale = _timescale(moov, ts, te)
    stbl = _find(moov, "mdia", "minf", "stbl", s=ts, e=te)
    if not stbl:
        raise SyncError("GPMFトラックのstblが見つかりません")
    ss, se = stbl

    stts_entries = _stts(moov, ss, se)
    chunk_offsets = _chunks(moov, ss, se)
    sample_sizes = _sizes(moov, ss, se)
    stsc_entries = _stsc(moov, ss, se)

    if not chunk_offsets or not sample_sizes:
        raise SyncError("GPMFサンプルテーブルが不完全です")

    estimates: list[datetime.datetime] = []
    with mp4_path.open("rb") as f:
        for idx in range(min(60, len(sample_sizes))):
            off = _sample_file_offset(idx, chunk_offsets, stsc_entries, sample_sizes)
            if off is None:
                continue
            f.seek(off)
            payload = f.read(sample_sizes[idx])
            raw = _find_gpsu(payload)
            if raw is None:
                continue
            dt = _parse_gpsu(raw)
            if dt is None:
                continue
            # Subtract presentation time to get UTC at video t=0.  The first
            # GoPro GPMF sample can be stale, so use a small stable cluster.
            estimates.append(
                dt - datetime.timedelta(
                    seconds=_sample_pts_seconds(idx, stts_entries, timescale)
                )
            )
            if len(estimates) >= 12:
                break

    return _stable_start_utc(estimates)


def offset_from_unix_starts(gopro_unix_start: float, pcap_unix_start: float) -> float:
    """Return offset for: pcap_elapsed = gopro_elapsed + offset."""
    return gopro_unix_start - pcap_unix_start


def compute_gps_offset(mp4_path: Path, pcap_unix_start: float) -> dict:
    """
    Compute the elapsed-time offset so that: pcap_time = gopro_time + offset.

    Returns:
        {"offset": float, "gopro_utc": str, "pcap_utc": str}
    Raises SyncError on failure.
    """
    utc_at_zero = extract_gps_utc_at_start(mp4_path)
    gopro_unix = utc_at_zero.timestamp()
    offset = offset_from_unix_starts(gopro_unix, pcap_unix_start)
    pcap_utc = datetime.datetime.fromtimestamp(pcap_unix_start, tz=datetime.timezone.utc)
    return {
        "offset": round(offset, 3),
        "gopro_utc": utc_at_zero.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "pcap_utc": pcap_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
