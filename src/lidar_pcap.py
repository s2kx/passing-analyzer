"""Small, dependency-free Velodyne PCAP reader used by the web UI.

The reader intentionally keeps only a frame index in memory.  Point data is
decoded on demand, so multi-gigabyte captures can be opened without loading the
whole file.  The packet layout is the common 1206-byte Velodyne data packet.
"""

from __future__ import annotations

import csv
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path


ROI_HEADER = (
    "Frame", "Time", "Laser ID", "Intensity", "Distance",
    "Azimuth", "Elevation", "Selected", "Points:0", "Points:1", "Points:2",
)


def _select_points(points, min_x, max_x, min_y, max_y, lasers):
    """Filter decoded points by a rectangular ROI and optional laser set."""
    return [
        p for p in points
        if min_x <= p[0] <= max_x and min_y <= p[1] <= max_y
        and (lasers is None or p[4] in lasers)
    ]


def _write_roi_rows(writer, index: int, timestamp: float, selected) -> None:
    """Write VeloView-compatible ROI rows for one frame's selected points."""
    for x, y, z, intensity, laser in selected:
        distance = math.sqrt(x * x + y * y + z * z)
        writer.writerow((
            index, f"{timestamp:.6f}", laser, intensity, f"{distance:.4f}",
            f"{math.degrees(math.atan2(x, y)):.3f}",
            f"{math.degrees(math.atan2(z, math.hypot(x, y))):.3f}", 1,
            f"{x:.4f}", f"{y:.4f}", f"{z:.4f}",
        ))


VLP16_ANGLES = (-15, 1, -13, 3, -11, 5, -9, 7, -7, 9, -5, 11, -3, 13, -1, 15)
HDL32_ANGLES = (
    -30.67, -9.329, -29.33, -8, -28, -6.671, -26.67, -5.333,
    -25.33, -4, -24, -2.667, -22.67, -1.333, -21.33, 0,
    -20, 1.333, -18.67, 2.667, -17.33, 4, -16, 5.333,
    -14.67, 6.667, -13.33, 8, -12, 9.333, -10.67, 10.67,
)
VLP32C_ANGLES = (
    -25, -1, -1.667, -15.639, -11.31, 0, -0.667, -8.843,
    -7.254, 0.333, -0.333, -6.148, -5.333, 1.333, 0.667, -4,
    -4.667, 1.667, 1, -3.667, -3.333, 3.333, 2.333, -2.667,
    -3, 7, 4.667, -2.333, -2, 15, 10.333, -1.333,
)


@dataclass(frozen=True)
class PacketRef:
    offset: int
    timestamp: float


@dataclass(frozen=True)
class FrameRef:
    first_packet: int
    last_packet: int
    timestamp: float


class VelodynePcap:
    """Index and decode a classic-PCAP Velodyne capture."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.packets: list[PacketRef] = []
        self.frames: list[FrameRef] = []
        self.model_id = 0
        self.model_name = "Unknown Velodyne"
        self._endian = "<"
        self._nanosecond = False
        self._unix_start: float = 0.0
        self._index()

    def _index(self) -> None:
        with self.path.open("rb") as stream:
            magic = stream.read(4)
            formats = {
                b"\xd4\xc3\xb2\xa1": ("<", False), b"\xa1\xb2\xc3\xd4": (">", False),
                b"\x4d\x3c\xb2\xa1": ("<", True), b"\xa1\xb2\x3c\x4d": (">", True),
            }
            if magic not in formats:
                raise ValueError("対応しているのはclassic PCAP形式です（pcapngは未対応）")
            self._endian, self._nanosecond = formats[magic]
            if len(stream.read(20)) != 20:
                raise ValueError("PCAPヘッダーが途中で終わっています")
            first_capture_time: float | None = None
            previous_azimuth: int | None = None
            frame_start = 0
            while True:
                record = stream.read(16)
                if not record:
                    break
                if len(record) != 16:
                    raise ValueError("PCAPレコードヘッダーが壊れています")
                sec, fraction, included, _original = struct.unpack(self._endian + "IIII", record)
                data_offset = stream.tell()
                data = stream.read(included)
                if len(data) != included:
                    raise ValueError("PCAPパケットが途中で終わっています")
                payload_at = self._find_velodyne_payload(data)
                if payload_at < 0:
                    continue
                capture_time = sec + fraction / (1e9 if self._nanosecond else 1e6)
                if first_capture_time is None:
                    first_capture_time = capture_time
                    self._unix_start = capture_time
                relative_time = capture_time - first_capture_time
                packet_index = len(self.packets)
                self.packets.append(PacketRef(data_offset + payload_at, relative_time))
                azimuth = struct.unpack_from("<H", data, payload_at + 2)[0]
                if previous_azimuth is not None and azimuth + 18000 < previous_azimuth:
                    if packet_index > frame_start:
                        self.frames.append(FrameRef(frame_start, packet_index, self.packets[frame_start].timestamp))
                    frame_start = packet_index
                previous_azimuth = azimuth
                self.model_id = data[payload_at + 1205]
            if self.packets and frame_start < len(self.packets):
                self.frames.append(FrameRef(frame_start, len(self.packets), self.packets[frame_start].timestamp))
        if not self.packets:
            raise ValueError("1206-byte Velodyneデータパケットが見つかりません")
        self.model_name = {0x21: "HDL-32E", 0x22: "VLP-16", 0x23: "VLP-16 Hi-Res", 0x24: "VLP-32C"}.get(
            self.model_id, f"Velodyne model 0x{self.model_id:02X}"
        )

    @staticmethod
    def _find_velodyne_payload(data: bytes) -> int:
        start = 0
        while True:
            at = data.find(b"\xff\xee", start)
            if at < 0 or at + 1206 > len(data):
                return -1
            if all(data[at + block * 100:at + block * 100 + 2] == b"\xff\xee" for block in range(12)):
                return at
            start = at + 1

    @property
    def duration(self) -> float:
        return self.frames[-1].timestamp if self.frames else 0.0

    @property
    def start_unix_time(self) -> float:
        """Unix timestamp (UTC) of the first captured LiDAR packet."""
        return self._unix_start

    def nearest_frame(self, seconds: float) -> int:
        if not self.frames:
            return 0
        lo, hi = 0, len(self.frames)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.frames[mid].timestamp < seconds:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return 0
        if lo == len(self.frames):
            return lo - 1
        return lo if abs(self.frames[lo].timestamp - seconds) < abs(self.frames[lo - 1].timestamp - seconds) else lo - 1

    def _angles(self) -> tuple[float, ...]:
        if self.model_id in {0x22, 0x23}:
            return VLP16_ANGLES
        if self.model_id == 0x24:
            return VLP32C_ANGLES
        return HDL32_ANGLES

    def decode_frame(self, index: int, max_points: int | None = None) -> list[tuple[float, float, float, int, int]]:
        if not 0 <= index < len(self.frames):
            raise IndexError("LiDARフレーム番号が範囲外です")
        ref = self.frames[index]
        angles = self._angles()
        points: list[tuple[float, float, float, int, int]] = []
        with self.path.open("rb") as stream:
            for packet_no in range(ref.first_packet, ref.last_packet):
                stream.seek(self.packets[packet_no].offset)
                payload = stream.read(1206)
                if len(payload) != 1206:
                    continue
                block_azimuths = [struct.unpack_from("<H", payload, block * 100 + 2)[0] for block in range(12)]
                for block, azimuth_raw in enumerate(block_azimuths):
                    next_raw = block_azimuths[min(block + 1, 11)]
                    delta = (next_raw - azimuth_raw) % 36000
                    for laser in range(32):
                        distance_raw = struct.unpack_from("<H", payload, block * 100 + 4 + laser * 3)[0]
                        if distance_raw == 0:
                            continue
                        intensity = payload[block * 100 + 6 + laser * 3]
                        if len(angles) == 16:
                            channel = laser % 16
                            fraction = (laser // 16) * 0.5 + channel / 32.0
                        else:
                            channel = laser
                            fraction = laser / 32.0
                        azimuth = math.radians(((azimuth_raw + delta * fraction) % 36000) / 100.0)
                        vertical = math.radians(angles[channel])
                        distance = distance_raw * 0.002
                        horizontal = distance * math.cos(vertical)
                        points.append((
                            horizontal * math.sin(azimuth),
                            horizontal * math.cos(azimuth),
                            distance * math.sin(vertical), intensity, channel,
                        ))
        if max_points and len(points) > max_points:
            step = math.ceil(len(points) / max_points)
            return points[::step]
        return points

    @property
    def laser_count(self) -> int:
        return len(self._angles())

    def frame_data(self, index: int, max_points: int = 45000) -> dict:
        points = self.decode_frame(index, max_points=max_points)
        return {
            "index": index, "timestamp": self.frames[index].timestamp,
            "points": [[round(x, 3), round(y, 3), round(z, 3), intensity, laser] for x, y, z, intensity, laser in points],
        }

    def export_roi(self, folder: Path, first: int, last: int, bounds: dict[str, float],
                   lasers: "set[int] | None" = None) -> tuple[int, int]:
        folder.mkdir(parents=True, exist_ok=True)
        first, last = sorted((max(0, first), min(len(self.frames) - 1, last)))
        min_x, max_x = sorted((float(bounds["min_x"]), float(bounds["max_x"])))
        min_y, max_y = sorted((float(bounds["min_y"]), float(bounds["max_y"])))
        total = 0
        for index in range(first, last + 1):
            points = self.decode_frame(index)
            selected = _select_points(points, min_x, max_x, min_y, max_y, lasers)
            timestamp = self.frames[index].timestamp
            target = folder / f"lidar_{index:06d}_{timestamp:010.3f}.csv"
            with target.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(ROI_HEADER)
                _write_roi_rows(writer, index, timestamp, selected)
            total += len(selected)
        return last - first + 1, total

    def export_overtaking(self, folder: Path, event_id, frames: list[dict]) -> tuple[Path, int, int, int]:
        """Export one overtaking as a single CSV, using a per-frame ROI box.

        ``frames`` is an ordered list of ``{"index", "bounds", "lasers"}`` entries,
        each describing the manually adjusted selection box for that frame.  All
        frames are concatenated into one file, distinguished by the ``Frame`` column.
        """
        folder.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(event_id or "")) or "event"
        target = folder / f"overtaking_{safe_id}.csv"
        total = 0
        written_frames = 0
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(ROI_HEADER)
            for frame in sorted(frames, key=lambda f: int(f["index"])):
                index = int(frame["index"])
                if not 0 <= index < len(self.frames):
                    continue
                bounds = frame.get("bounds") or {}
                min_x, max_x = sorted((float(bounds["min_x"]), float(bounds["max_x"])))
                min_y, max_y = sorted((float(bounds["min_y"]), float(bounds["max_y"])))
                laser_value = frame.get("lasers")
                lasers = {int(v) for v in laser_value} if laser_value else None
                timestamp = self.frames[index].timestamp
                selected = _select_points(self.decode_frame(index), min_x, max_x, min_y, max_y, lasers)
                _write_roi_rows(writer, index, timestamp, selected)
                total += len(selected)
                if selected:
                    written_frames += 1
        return target, len(frames), written_frames, total
