#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GoPro 蜍慕判縺九ｉ縲瑚・蜍戊ｻ翫↓霑ｽ縺・ｶ翫＆繧後ｋ迸ｬ髢薙阪ｒ閾ｪ蜍墓､懷・縺吶ｋ繝代う繝励Λ繧､繝ｳ縲・
蜑肴署:
    GoPro 繧定・霆｢霆翫・蜑榊髄縺・繝輔Ο繝ｳ繝・縺ｾ縺溘・蠕後ｍ蜷代″(繝ｪ繧｢)縺ｫ蜿悶ｊ莉倥￠縺ｦ謦ｮ蠖ｱ縺励◆
    蜍慕判繧貞・蜉帙→縺吶ｋ縲よ聴蠖ｱ譁ｹ蜷代・ --view {front,rear} 縺ｧ繝ｦ繝ｼ繧ｶ繝ｼ縺梧欠螳壹☆繧九・    霑ｽ縺・ｶ翫＠霆贋ｸ｡縺ｮ縺ｿ繧偵瑚ｿｽ縺・ｶ翫＠繧､繝吶Φ繝医阪→縺励※讀懷・縺励∝玄髢捺ュ蝣ｱ繧・CSV 縺ｫ蜃ｺ蜉帙・    隧ｲ蠖灘玄髢薙・繧ｯ繝ｪ繝・・繧貞・繧雁・縺吶・
謦ｮ蠖ｱ譁ｹ蜷代↓繧医ｋ霑ｽ縺・ｶ翫＠霆贋ｸ｡縺ｮ隕九∴譁ｹ:
    譌･譛ｬ(蟾ｦ蛛ｴ騾夊｡・縺ｧ縺ｯ霑ｽ縺・ｶ翫＠霆贋ｸ｡縺ｯ閾ｪ霆｢霆翫・蜿ｳ蛛ｴ繧呈栢縺代※縺・￥縲・    --view front (蜑榊髄縺・: 霑ｽ縺・ｶ翫＠霆翫・逕ｻ髱｢縺ｮ蜿ｳ遶ｯ莉倩ｿ代°繧臥樟繧後∫判髱｢荳ｭ螟ｮ縺ｸ蜷代°縺｣縺ｦ
        遘ｻ蜍輔＠縺ｦ謚懊￠繧九ょｷｦ遶ｯ縺ｾ縺ｧ騾壹ｊ謚懊￠繧玖ｻ・讓ｪ蛻・ｊ)縺ｯ髯､螟悶☆繧九・    --view rear  (蠕後ｍ蜷代″): 縺昴・騾・〒縲∝ｷｦ遶ｯ莉倩ｿ代°繧臥樟繧御ｸｭ螟ｮ縺ｸ蜷代°縺｣縺ｦ遘ｻ蜍輔☆繧九・        蜿ｳ遶ｯ縺ｾ縺ｧ騾壹ｊ謚懊￠繧玖ｻ翫・髯､螟悶☆繧九・
讀懷・繝ｭ繧ｸ繝・け(讓ｪ菴咲ｽｮ cx 縺ｮ縺ｿ縺ｧ蛻､螳壹る擇遨阪・諠・ｱ險倬鹸縺ｮ縺ｿ):
    1. 蜷・ヵ繝ｬ繝ｼ繝繧・YOLO 縺ｧ霆贋ｸ｡讀懷・ (car / truck / bus / motorcycle)
    2. ByteTrack 縺ｧ蜷御ｸ霆贋ｸ｡縺ｫ track ID 繧剃ｻ倅ｸ弱＠縲・ 繝輔Ξ繝ｼ繝莉･荳願ｿｽ霍｡
    3. 蜃ｺ迴ｾ遶ｯ縺九ｉ迴ｾ繧後※縺・ｋ (front=蜿ｳ遶ｯ / rear=蟾ｦ遶ｯ)
    4. 逕ｻ髱｢荳ｭ螟ｮ縺ｸ蜷代°縺｣縺ｦ遘ｻ蜍輔＠縺ｦ縺・ｋ
    5. 蜿榊ｯｾ遶ｯ縺ｧ豸医∴繧玖ｻ贋ｸ｡縺ｯ髯､螟・(front=蟾ｦ遶ｯ / rear=蜿ｳ遶ｯ)
    6. 閾ｪ霆企°蜍輔ご繝ｼ繝・譌｢螳壽怏蜉ｹ): 閾ｪ霆｢霆翫′蛛懈ｭ｢荳ｭ(閭梧勹縺後⊇縺ｼ蜍輔°縺ｪ縺・縺ｫ逋ｺ逕溘＠縺・       track 繧帝勁螟悶ゆｿ｡蜿ｷ蠕・■繝ｻT蟄苓ｷｯ縺ｪ縺ｩ縺ｮ蛛懈ｭ｢荳ｭ縺ｮ隱､讀懃衍繧呈椛縺医ｋ縲・-no-stop-filter 縺ｧ辟｡蜉ｹ蛹・    -- 荳願ｨ倥ｒ貅縺溘☆蛹ｺ髢薙・髢句ｧ・邨ゆｺ・凾蛻ｻ縺ｨ霑ｽ縺・ｶ翫＠蛛ｴ繧定ｨ倬鹸
    7. ffmpeg 縺ｧ蜑榊ｾ後・繝ｼ繧ｸ繝ｳ莉倥″繧ｯ繝ｪ繝・・繧呈嶌縺榊・縺・
菴ｿ縺・婿:
    python detect_overtaking.py input.mp4 --view rear
    python detect_overtaking.py input.mp4 --view front --clip --overlay
    python detect_overtaking.py input.mp4 --view rear --model yolov8m.pt --conf 0.35

蜃ｺ蜉・
    out/overtaking_events.csv   讀懷・繧､繝吶Φ繝井ｸ隕ｧ
    out/clips/event_001.mp4 ... 蛻・ｊ蜃ｺ縺励け繝ｪ繝・・ (--clip 謖・ｮ壽凾)
    out/annotated.mp4           讀懷・蜿ｯ隕門喧蜍慕判 (--overlay 謖・ｮ壽凾)
"""

import argparse
import csv
import queue
import shutil
import statistics
import subprocess
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

VEHICLE_CLASSES = {2: "car", 5: "bus", 7: "truck"}


def ffmpeg_executable() -> str | None:
    """Return bundled ffmpeg first, then PATH ffmpeg."""
    app_dir = Path(__file__).resolve().parent
    names = ["ffmpeg.exe", "ffmpeg"] if sys.platform == "win32" else ["ffmpeg"]
    for folder in (app_dir / "tools", app_dir):
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffmpeg")


@dataclass
class TrackSample:
    """1 track 縺ｮ 1 繝輔Ξ繝ｼ繝蛻・・隕ｳ貂ｬ"""
    frame: int
    t: float
    cx: float
    cy: float         # bbox 荳ｭ蠢・y (0..1 豁｣隕丞喧)
    area: float       # bbox 髱｢遨・(0..1 豁｣隕丞喧, 逕ｻ髱｢蜈ｨ菴・1)
    cls: str
    bottom_y: float | None = None  # bbox 荳狗ｫｯ y縲る％霍ｯ荳翫・謗･蝨ｰ轤ｹ縺ｮ霑台ｼｼ
    confidence: float = 1.0        # YOLO 讀懷・菫｡鬆ｼ蠎ｦ
    yellow_plate_score: float = 0.0  # 鮟・牡繝翫Φ繝舌・繝励Ξ繝ｼ繝医ｉ縺励＆ (0..1)


@dataclass
class Track:
    tid: int
    samples: list = field(default_factory=list)

    @property
    def cls(self) -> str:
        if not self.samples:
            return "?"
        counts = detector_class_votes(self)
        return max(counts, key=counts.get)


@dataclass
class Event:
    tid: int
    cls: str
    t_start: float
    t_end: float
    side: str          # "left" / "right"
    peak_area: float   # 譛謗･霑第凾縺ｮ bbox 髱｢遨・(螟ｧ縺阪＞縺ｻ縺ｩ霑代＞縲よュ蝣ｱ逕ｨ)
    peak_t: float
    ego_motion: float = float("nan")  # track 蛹ｺ髢薙・閾ｪ霆願レ譎ｯ繝輔Ο繝ｼ荳ｭ螟ｮ蛟､(px縲よュ蝣ｱ逕ｨ)
    area_change_ratio: float = float("nan")
    vertical_motion: float = float("nan")
    lateral_vertical_ratio: float = float("nan")
    road_ratio: float = float("nan")
    detector_cls: str = ""
    kei_plate_score: float = 0.0
    kei_plate_hits: int = 0
    source_video: str = ""             # クリップ切り出し元の動画パス
    time_offset: float = 0.0           # 連結タイムライン上での先頭からの累積オフセット(秒)

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start

    @property
    def global_t_start(self) -> float:
        return self.t_start + self.time_offset

    @property
    def global_peak_t(self) -> float:
        return self.peak_t + self.time_offset

    @property
    def global_t_end(self) -> float:
        return self.t_end + self.time_offset


def parse_args():
    p = argparse.ArgumentParser(description="Detect overtaking candidates from a GoPro video")
    p.add_argument("videos", nargs="+", help="input video file(s); processed in the given order as one continuous timeline")
    p.add_argument("--view", choices=["front", "rear"], required=True, help="camera direction")
    p.add_argument("--out", default="out", help="output directory")
    p.add_argument("--model", default="yolov8n.pt", help="YOLO model path")
    p.add_argument("--conf", type=float, default=0.30, help="YOLO confidence threshold")
    p.add_argument("--appear-edge", type=float, default=0.70, help="edge threshold for track appearance")
    p.add_argument("--exit-edge", type=float, default=0.40, help="opposite-edge threshold used to reject crossing tracks")
    p.add_argument("--margin", type=float, default=2.0, help="clip margin in seconds")
    p.add_argument("--clip", action="store_true", help="cut event clips with ffmpeg")
    p.add_argument("--overlay", action="store_true", help="write annotated video")
    p.add_argument("--stride", type=int, default=1, help="process every N frames")
    p.add_argument("--device", default="auto", help="auto, 0, or cpu")
    p.add_argument("--imgsz", type=int, default=640, help="YOLO image size")
    p.add_argument("--half", action="store_true", help="use FP16 when supported")
    p.add_argument("--batch", type=int, default=32, help="YOLO batch size")
    p.add_argument("--prefetch-batches", type=int, default=2, help="number of batches to prefetch")
    p.add_argument("--decoder", choices=["auto", "opencv", "nvdec"], default="auto", help="video decoder")
    p.add_argument("--no-stop-filter", action="store_true", help="disable ego-motion stop filter")
    p.add_argument("--stop-flow", type=float, default=0.8, help="ego-motion threshold")
    p.add_argument("--flow-stride", type=int, default=3, help="optical-flow sampling interval")
    p.add_argument("--min-track-seconds", type=float, default=0.6, help="minimum track duration")
    p.add_argument("--min-vertical-motion", type=float, default=0.025, help="minimum vertical motion")
    p.add_argument("--max-lateral-vertical-ratio", type=float, default=8.0, help="maximum lateral/vertical motion ratio")
    p.add_argument("--min-area-ratio", type=float, default=1.15, help="minimum bbox area change ratio")
    p.add_argument("--max-mid-peak-ratio", type=float, default=1.8, help="maximum mid-track area peak ratio")
    p.add_argument("--close-pass-peak-area", type=float, default=0.08, help="peak bbox area needed for close front/rear pass rescue")
    p.add_argument("--close-pass-min-shrink", type=float, default=1.8, help="minimum shrink after close-pass peak")
    p.add_argument("--close-pass-min-lateral", type=float, default=0.08, help="minimum lateral move away from close-pass peak")
    p.add_argument("--close-pass-side-threshold", type=float, default=0.55, help="side position threshold for close-pass peak")
    p.add_argument("--close-pass-min-road-ratio", type=float, default=0.50, help="minimum road ROI occupancy for close-pass rescue")
    p.add_argument("--no-trajectory-filter", action="store_true", help="disable trajectory filter")
    p.add_argument("--no-area-filter", action="store_true", help="disable area-change filter")
    p.add_argument("--road-roi", action="store_true", help="enable road ROI filter")
    p.add_argument("--vanishing-x", type=float, default=0.5, help="road ROI vanishing point x")
    p.add_argument("--vanishing-y", type=float, default=0.42, help="road ROI vanishing point y")
    p.add_argument("--road-bottom-left", type=float, default=0.12, help="road ROI bottom-left x")
    p.add_argument("--road-bottom-right", type=float, default=0.88, help="road ROI bottom-right x")
    p.add_argument("--road-top-half-width", type=float, default=0.08, help="road ROI top half width")
    p.add_argument("--min-road-ratio", type=float, default=0.25, help="minimum road ROI occupancy ratio")
    p.add_argument("--kei-plate-threshold", type=float, default=0.45, help="kei plate color threshold")
    p.add_argument("--kei-min-frames", type=int, default=2, help="minimum kei evidence frames")
    p.add_argument("--kei-large-override-threshold", type=float, default=0.75, help="kei override threshold for large classes")
    p.add_argument("--large-vote-threshold", type=float, default=0.70, help="large vehicle vote threshold")
    p.add_argument("--large-min-frames", type=int, default=5, help="minimum large vehicle evidence frames")
    return p.parse_args()
def ego_flow_magnitude(prev_gray, gray, mask=None):
    """Estimate background optical-flow magnitude."""
    pts = cv2.goodFeaturesToTrack(prev_gray, maxCorners=200, qualityLevel=0.01,
                                  minDistance=8, mask=mask)
    if pts is None or len(pts) < 10:
        return 0.0
    nxt, st1, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts, None)
    if nxt is None:
        return 0.0
    back, st2, _ = cv2.calcOpticalFlowPyrLK(gray, prev_gray, nxt, None)
    if back is None:
        return 0.0
    fb_err = np.linalg.norm((back - pts).reshape(-1, 2), axis=1)
    good = (st1.reshape(-1) == 1) & (st2.reshape(-1) == 1) & (fb_err < 1.0)
    if good.sum() < 10:
        return 0.0
    d = (nxt - pts).reshape(-1, 2)[good]
    return float(np.median(np.linalg.norm(d, axis=1)))


def yellow_plate_score(frame, bbox) -> float:
    """Estimate yellow license plate evidence for kei cars."""
    if cv2 is None or np is None:
        return 0.0
    frame_h, frame_w = frame.shape[:2]
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    x1, x2 = max(0, x1), min(frame_w, x2)
    y1, y2 = max(0, y1), min(frame_h, y2)
    width, height = x2 - x1, y2 - y1
    if width < 40 or height < 30:
        return 0.0

    rx1 = x1 + int(width * 0.15)
    rx2 = x1 + int(width * 0.85)
    ry1 = y1 + int(height * 0.48)
    ry2 = y1 + int(height * 0.95)
    roi = frame[ry1:ry2, rx1:rx2]
    if roi.size == 0:
        return 0.0

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (15, 85, 75), (40, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    vehicle_area = float(width * height)
    best = 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        bx, by, bw, bh = cv2.boundingRect(contour)
        if bw < 6 or bh < 3 or area < 18:
            continue
        aspect = bw / max(bh, 1)
        relative_area = area / max(vehicle_area, 1.0)
        rectangularity = area / max(float(bw * bh), 1.0)
        if not (1.15 <= aspect <= 3.8 and 0.0003 <= relative_area <= 0.06):
            continue
        size_score = min(1.0, relative_area / 0.004)
        aspect_score = max(0.0, 1.0 - abs(aspect - 2.0) / 2.0)
        score = 0.50 * rectangularity + 0.30 * size_score + 0.20 * aspect_score
        best = max(best, min(1.0, score))
    return best


def detector_class_votes(track: Track) -> dict[str, float]:
    """Return confidence-weighted detector class votes for a track."""
    counts: dict[str, float] = defaultdict(float)
    for sample in track.samples:
        edge_distance = min(max(sample.cx, 0.0), max(1.0 - sample.cx, 0.0))
        center_weight = 0.35 + 0.65 * min(1.0, edge_distance / 0.35)
        counts[sample.cls] += max(0.05, sample.confidence) * center_weight
    return counts


def classify_vehicle_type(track: Track, kei_threshold=0.45, kei_min_frames=2,
                         large_vote_threshold=0.70, large_min_frames=5,
                         kei_large_override_threshold=0.75):
    """Classify a track as large, normal, or kei."""
    votes = detector_class_votes(track)
    detector_cls = max(votes, key=votes.get) if votes else "?"
    total_vote = sum(votes.values())
    large_vote = votes.get("truck", 0.0) + votes.get("bus", 0.0)
    large_share = large_vote / total_vote if total_vote else 0.0
    large_frames = sum(
        sample.cls in {"truck", "bus"} and sample.confidence >= 0.40
        for sample in track.samples
    )
    strong_large = (
        detector_cls in {"truck", "bus"}
        and large_share >= float(large_vote_threshold)
        and large_frames >= max(1, int(large_min_frames))
    )

    scores = [sample.yellow_plate_score for sample in track.samples]
    hits = sum(score >= kei_threshold for score in scores)
    required = max(1, int(kei_min_frames))
    evidence_score = float(statistics.median(sorted(scores, reverse=True)[:3])) if scores else 0.0

    longest_run = current_run = 0
    for score in scores:
        if score >= kei_threshold:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    enough_hits = hits >= max(3, required)
    strong_plate_for_large = evidence_score >= float(kei_large_override_threshold)
    if longest_run >= required and enough_hits and (not strong_large or strong_plate_for_large):
        return "軽", detector_cls, evidence_score, hits

    vehicle_type = "大型" if strong_large else "普通"
    return vehicle_type, detector_cls, evidence_score, hits


def _read_exact(stream, size: int) -> bytes:
    """Read exactly size bytes from a pipe unless EOF is reached."""
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _opencv_frames(video: str):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"蜍慕判繧帝幕縺代∪縺帙ｓ: {video}")
    try:
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame_idx, frame
            frame_idx += 1
    finally:
        cap.release()


def _nvdec_frames(video: str, width: int, height: int):
    """Decode frames with FFmpeg NVDEC."""
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    frame_bytes = width * height * 3
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
        "-i", video, "-an", "-sn", "-dn", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=frame_bytes * 2, creationflags=creationflags,
    )
    assert process.stdout is not None
    try:
        frame_idx = 0
        while True:
            raw = _read_exact(process.stdout, frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError("NVDEC縺九ｉ荳榊ｮ悟・縺ｪ繝輔Ξ繝ｼ繝繧貞女菫｡縺励∪縺励◆")
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
            yield frame_idx, frame
            frame_idx += 1
        code = process.wait()
        if code:
            error = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(error.strip() or f"FFmpeg NVDEC exit={code}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _resolve_yolo_model(args, YOLO):
    return YOLO(str(Path(args.model)))


def build_tracks(args, out_dir: Path, video: str, overlay_suffix: str = ""):
    """Build tracked vehicle trajectories from video frames."""
    if cv2 is None or np is None:
        sys.exit("opencv-python / numpy was not found. Run pip install -r requirements.txt.")
    try:
        from ultralytics import YOLO
    except ImportError:
        sys.exit("ultralytics was not found. Run pip install -r requirements.txt.")

    metadata_cap = cv2.VideoCapture(video)
    if not metadata_cap.isOpened():
        sys.exit(f"蜍慕判繧帝幕縺代∪縺帙ｓ: {video}")
    fps = metadata_cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(metadata_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(metadata_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(metadata_cap.get(cv2.CAP_PROP_FRAME_COUNT))
    metadata_cap.release()
    duration = total / fps if fps else 0.0
    print(f"[info] {w}x{h} {fps:.2f}fps frames={total} ({duration:.2f}s)")

    # 繝・ヰ繧､繧ｹ驕ｸ謚・ auto 縺ｪ繧・CUDA 縺後≠繧後・ GPU縲∫┌縺代ｌ縺ｰ CPU
    if args.device == "auto":
        try:
            import torch
            device = 0 if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    else:
        device = int(args.device) if args.device.isdigit() else args.device
    use_gpu = device != "cpu"
    half = bool(args.half or use_gpu)
    # Ultralytics 8.4+ renamed the FP16 flag `half` -> `quantize` (16=FP16, None=FP32).
    # Detect which the installed version accepts so older (>=8.2) installs still work.
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT
        _use_quantize = "quantize" in DEFAULT_CFG_DICT
    except Exception:
        _use_quantize = False
    precision_kwargs = {"quantize": 16 if half else None} if _use_quantize else {"half": half}
    if use_gpu:
        try:
            import torch
            print(f"[info] GPU 菴ｿ逕ｨ: {torch.cuda.get_device_name(0)} (fp16={half}, imgsz={args.imgsz})")
        except Exception:
            print(f"[info] GPU 菴ｿ逕ｨ (device={device}, fp16={half}, imgsz={args.imgsz})")
    else:
        print(f"[warn] CPU 縺ｧ謗ｨ隲悶＠縺ｾ縺・(菴朱・縲・PU迚・orch縺ｮ蟆主・繧呈耳螂ｨ縲Ｊmgsz={args.imgsz}")

    model = _resolve_yolo_model(args, YOLO)

    from ultralytics.utils import YAML, IterableSimpleNamespace
    from ultralytics.utils.checks import check_yaml
    from ultralytics.trackers import BYTETracker
    tcfg = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
    eff_fps = fps / max(1, args.stride)
    tracker = BYTETracker(tcfg)

    tracks: dict[int, Track] = {}

    writer = None
    if args.overlay:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(out_dir / f"annotated{overlay_suffix}.mp4"), fourcc, eff_fps, (w, h))

    def predict_batch(frames):
        return model.predict(
            frames, classes=list(VEHICLE_CLASSES.keys()), conf=args.conf,
            verbose=False, device=device, imgsz=args.imgsz, **precision_kwargs,
        )

    # 閾ｪ霆企°蜍輔ご繝ｼ繝育畑縺ｮ迥ｶ諷九らｸｮ蟆上げ繝ｬ繝ｼ繧ｹ繧ｱ繝ｼ繝ｫ荳翫〒縲∵､懷・縺励◆霆贋ｸ｡鬆伜沺繧帝勁螟悶＠縺ｦ
    stop_filter = not args.no_stop_filter
    sw = 320
    sh = max(1, int(round(h * sw / w)))
    fx, fy = sw / w, sh / h
    flow_stride = max(1, int(getattr(args, "flow_stride", 3)))
    prev_small = None
    prev_bgmask = None
    prev_flow_frame = None
    last_flow = None
    motion: dict[int, float] = {}   # frame_idx -> 閭梧勹繝輔Ο繝ｼ驥・px)

    def handle(frame_idx, frame, result):
        """Process one frame result and update tracks."""
        nonlocal prev_small, prev_bgmask, prev_flow_frame, last_flow
        t = frame_idx / fps

        if stop_filter:
            should_measure = prev_flow_frame is None or frame_idx - prev_flow_frame >= flow_stride
            if should_measure:
                small = cv2.cvtColor(cv2.resize(frame, (sw, sh)), cv2.COLOR_BGR2GRAY)
                bgmask = np.full((sh, sw), 255, np.uint8)  # 閭梧勹=255
                if result.boxes is not None:
                    for x1, y1, x2, y2 in result.boxes.xyxy.cpu().numpy():
                        X1 = max(0, int(x1 * fx) - 2); X2 = min(sw, int(x2 * fx) + 2)
                        Y1 = max(0, int(y1 * fy) - 2); Y2 = min(sh, int(y2 * fy) + 2)
                        bgmask[Y1:Y2, X1:X2] = 0
                if prev_small is not None and prev_flow_frame is not None:
                    last_flow = ego_flow_magnitude(prev_small, small, prev_bgmask)
                    motion[frame_idx] = last_flow
                prev_small, prev_bgmask, prev_flow_frame = small, bgmask, frame_idx
            elif last_flow is not None:
                motion[frame_idx] = last_flow

        det = result.boxes.cpu().numpy()
        out = tracker.update(det, frame)  # 蜷・｡・ [x1,y1,x2,y2,id,score,cls,idx]
        for row in out:
            x1, y1, x2, y2 = row[:4]
            tid = int(row[4])
            cls_name = VEHICLE_CLASSES.get(int(row[6]), "vehicle")
            confidence = float(row[5])
            cx = ((x1 + x2) / 2) / w
            cy = ((y1 + y2) / 2) / h
            area = ((x2 - x1) * (y2 - y1)) / (w * h)
            plate_score = yellow_plate_score(frame, (x1, y1, x2, y2))
            tracks.setdefault(tid, Track(tid)).samples.append(
                TrackSample(frame_idx, t, cx, cy, area, cls_name, y2 / h,
                            confidence, plate_score)
            )
            if writer is not None:
                cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)),
                              (0, 255, 0), 2)
                cv2.putText(frame, f"ID{tid} {cls_name}", (int(x1), int(y1) - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if writer is not None:
            writer.write(frame)

    def decoded_frames():
        decoder = getattr(args, "decoder", "auto")
        try_nvdec = decoder == "nvdec" or (decoder == "auto" and use_gpu and ffmpeg_executable())
        if try_nvdec:
            iterator = _nvdec_frames(video, w, h)
            try:
                first = next(iterator)
            except Exception as exc:
                print(f"[warn] NVDEC繧帝幕蟋九〒縺阪↑縺・◆繧＾penCV縺ｸ謌ｻ縺励∪縺・ {exc}")
            else:
                print("[info] 蜍慕判繝・さ繝ｼ繝・ FFmpeg NVDEC (GPU)")
                yield first
                yield from iterator
                return
        print("[info] 蜍慕判繝・さ繝ｼ繝・ OpenCV (CPU)")
        yield from _opencv_frames(video)

    prefetch_count = max(0, int(getattr(args, "prefetch_batches", 2)))
    batch_queue: queue.Queue = queue.Queue(maxsize=max(1, prefetch_count))
    sentinel = object()

    def decode_worker():
        batch_frames, batch_idx = [], []
        try:
            for frame_idx, frame in decoded_frames():
                if args.stride > 1 and frame_idx % args.stride != 0:
                    continue
                batch_frames.append(frame)
                batch_idx.append(frame_idx)
                if len(batch_frames) >= args.batch:
                    batch_queue.put((batch_idx, batch_frames))
                    batch_frames, batch_idx = [], []
            if batch_frames:
                batch_queue.put((batch_idx, batch_frames))
        except Exception as exc:
            batch_queue.put(exc)
        finally:
            batch_queue.put(sentinel)

    decoder_thread = threading.Thread(target=decode_worker, name="video-prefetch", daemon=True)
    decoder_thread.start()
    print(f"[info] RAM prefetch: {prefetch_count} batches")
    decoder_error = None
    while True:
        item = batch_queue.get()
        if item is sentinel:
            break
        if isinstance(item, Exception):
            decoder_error = item
            continue
        batch_idx, batch_frames = item
        results = predict_batch(batch_frames)
        for fi, fr, res in zip(batch_idx, batch_frames, results):
            handle(fi, fr, res)
        print(f"\r[info] processing frame {batch_idx[-1]}/{total}", end="", flush=True)
    decoder_thread.join(timeout=5)
    if decoder_error is not None:
        raise decoder_error

    print()
    if writer is not None:
        writer.release()
    return tracks, fps, (motion if stop_filter else None), duration


def _road_occupancy_ratio(samples, args) -> float:
    """Return the fraction of samples inside the road ROI."""
    vx = getattr(args, "vanishing_x", 0.5)
    vy = getattr(args, "vanishing_y", 0.42)
    top_half = getattr(args, "road_top_half_width", 0.08)
    bottom_left = getattr(args, "road_bottom_left", 0.12)
    bottom_right = getattr(args, "road_bottom_right", 0.88)
    inside = 0
    for x in samples:
        y = x.bottom_y if x.bottom_y is not None else x.cy
        alpha = min(1.0, max(0.0, (y - vy) / max(1e-6, 1.0 - vy)))
        left = (vx - top_half) * (1.0 - alpha) + bottom_left * alpha
        right = (vx + top_half) * (1.0 - alpha) + bottom_right * alpha
        inside += left <= x.cx <= right
    return inside / len(samples) if samples else 0.0


def _close_pass_rescue(
    args, cxs: list[float], areas: list[float], peak_i: int,
    cx_end: float, area_end: float, road_ratio: float,
) -> tuple[bool, float]:
    if len(cxs) < 5 or not areas:
        return False, float("nan")
    peak_area = float(areas[peak_i])
    peak_cx = float(cxs[peak_i])
    pre_samples = peak_i
    post_samples = len(cxs) - peak_i - 1
    if pre_samples < 3 or post_samples < 3:
        return False, float("nan")

    shrink_after_peak = peak_area / max(area_end, 1e-9)
    min_road = getattr(args, "close_pass_min_road_ratio", 0.50)
    if road_ratio < min_road:
        return False, shrink_after_peak
    if peak_area < getattr(args, "close_pass_peak_area", 0.08):
        return False, shrink_after_peak
    if shrink_after_peak < getattr(args, "close_pass_min_shrink", 1.8):
        return False, shrink_after_peak

    side_threshold = getattr(args, "close_pass_side_threshold", 0.55)
    min_lateral = getattr(args, "close_pass_min_lateral", 0.08)
    if args.view == "front":
        side_peak = peak_cx >= side_threshold
        leaves_side = (peak_cx - cx_end) >= min_lateral
    else:  # rear
        side_peak = peak_cx <= (1.0 - side_threshold)      # 最接近ピークが左側
        leaves_side = (peak_cx - cx_end) >= min_lateral     # ピーク後さらに左へ抜ける
    return bool(side_peak and leaves_side), shrink_after_peak


def classify_overtaking(track: Track, args, motion=None) -> Event | None:
    """Classify one track as an overtaking event when it matches the trajectory filters."""
    s = track.samples

    min_track_seconds = getattr(args, "min_track_seconds", 0.6)
    if len(s) < 5 or s[-1].t - s[0].t < min_track_seconds:
        return None

    ego = float("nan")
    if motion and not args.no_stop_filter:
        vals = [motion[x.frame] for x in s if x.frame in motion]
        if vals:
            ego = float(statistics.median(vals))
            if ego < args.stop_flow:
                return None

    cxs = [x.cx for x in s]
    ground_ys = [x.bottom_y if x.bottom_y is not None else x.cy for x in s]
    areas = [x.area for x in s]
    q = max(1, len(cxs) // 4)
    cx_start = float(statistics.median(cxs[:q]))   # 蜃ｺ迴ｾ菴咲ｽｮ
    cx_end = float(statistics.median(cxs[-q:]))    # 豸亥､ｱ菴咲ｽｮ
    y_start = float(statistics.median(ground_ys[:q]))
    y_end = float(statistics.median(ground_ys[-q:]))
    area_start = float(statistics.median(areas[:q]))
    area_end = float(statistics.median(areas[-q:]))
    peak_i = max(range(len(areas)), key=areas.__getitem__)
    peak_in_middle = int(0.2 * len(s)) <= peak_i <= int(0.8 * len(s))
    mid_peak_ratio = float(areas[peak_i]) / max(area_start, area_end, 1e-9)
    road_ratio = _road_occupancy_ratio(s, args)
    close_pass, close_pass_area_ratio = _close_pass_rescue(
        args, cxs, areas, peak_i, cx_end, area_end, road_ratio,
    )

    if args.view == "front":
        appeared = cx_start >= args.appear_edge          # 3) 蜿ｳ遶ｯ縺九ｉ迴ｾ繧後ｋ
        toward_center = cx_end < cx_start
        exits_far_edge = cx_end <= args.exit_edge
        side = "left"
    else:  # rear
        # 後方カメラでは追い越し車は消失点(画面中央)付近に小さく現れ、右側を抜けるため、
        # 左右反転した映像では「中央→左端」へ拡大しながら流れる(前方の奥行き進行と逆)。
        appeared = cx_end <= (1.0 - args.appear_edge)       # 左端で最接近して抜ける
        toward_center = cx_end < cx_start                    # 中央→左端(左へ流れる)
        exits_far_edge = cx_start >= (1.0 - args.exit_edge)  # 右端から現れて横切る車は除外
        side = "right"

    ordinary_path = appeared and toward_center and not exits_far_edge
    if not ordinary_path and not close_pass:
        return None

    vertical_motion = (y_start - y_end) if args.view == "front" else (y_end - y_start)
    lateral_motion = abs(cx_end - cx_start)
    lateral_vertical_ratio = lateral_motion / max(abs(y_end - y_start), 1e-6)
    if not close_pass and not getattr(args, "no_trajectory_filter", False):
        if vertical_motion < getattr(args, "min_vertical_motion", 0.025):
            return None
        if lateral_vertical_ratio > getattr(args, "max_lateral_vertical_ratio", 8.0):
            return None

    area_change_ratio = (
        area_start / max(area_end, 1e-9)
        if args.view == "front"
        else area_end / max(area_start, 1e-9)
    )
    if close_pass:
        area_change_ratio = close_pass_area_ratio
    if not close_pass and not getattr(args, "no_area_filter", False):
        if area_change_ratio < getattr(args, "min_area_ratio", 1.15):
            return None
        if peak_in_middle and mid_peak_ratio > getattr(args, "max_mid_peak_ratio", 1.8):
            return None

    if getattr(args, "road_roi", False):
        if road_ratio < getattr(args, "min_road_ratio", 0.25):
            return None

    vehicle_type, detector_cls, kei_score, kei_hits = classify_vehicle_type(
        track,
        getattr(args, "kei_plate_threshold", 0.45),
        getattr(args, "kei_min_frames", 2),
        getattr(args, "large_vote_threshold", 0.70),
        getattr(args, "large_min_frames", 5),
        getattr(args, "kei_large_override_threshold", 0.75),
    )

    return Event(
        tid=track.tid,
        cls=vehicle_type,
        t_start=s[0].t,
        t_end=s[-1].t,
        side=side,
        peak_area=float(areas[peak_i]),
        ego_motion=ego,
        peak_t=s[peak_i].t,
        area_change_ratio=area_change_ratio,
        vertical_motion=vertical_motion,
        lateral_vertical_ratio=lateral_vertical_ratio,
        road_ratio=road_ratio,
        detector_cls=detector_cls,
        kei_plate_score=kei_score,
        kei_plate_hits=kei_hits,
    )


def write_csv(events: list[Event], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        # レビューUIで使う列 + 記録用（継続秒数・元動画・元動画内時刻）を出力する
        wr.writerow([
            "event_id", "class", "detector_class", "side",
            "t_start_s", "peak_t_s", "t_end_s", "duration_s",
            "kei_plate_score", "kei_plate_hits",
            "source_video", "local_t_start_s", "local_t_end_s",
        ])
        for i, e in enumerate(events, 1):
            # t_*_s = global (concatenated) time, local_*_s = time within source video
            wr.writerow([
                f"{i:03d}", e.cls, e.detector_cls, e.side,
                f"{e.global_t_start:.2f}", f"{e.global_peak_t:.2f}", f"{e.global_t_end:.2f}",
                f"{e.duration:.2f}",
                f"{e.kei_plate_score:.3f}", e.kei_plate_hits,
                Path(e.source_video).name, f"{e.t_start:.2f}", f"{e.t_end:.2f}",
            ])


def cut_clips(events: list[Event], args, out_dir: Path):
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        print("[warn] ffmpeg not found; skipping clip extraction.")
        return
    clip_dir = out_dir / "clips"
    clip_dir.mkdir(exist_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    for i, e in enumerate(events, 1):
        start = max(0.0, e.t_start - args.margin)
        dur = (e.t_end - e.t_start) + 2 * args.margin
        out = clip_dir / f"event_{i:03d}_{e.side}.mp4"
        cmd = [
            ffmpeg, "-y", "-ss", f"{start:.2f}", "-i", e.source_video,
            "-t", f"{dur:.2f}", "-c", "copy", str(out),
        ]
        print(f"[clip] event {i:03d}: {start:.1f}s +{dur:.1f}s -> {out.name}")
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )


def main():
    # 出力を最初の行から UTF-8 に固定する。Windows 既定(CP932)のままだと、Ultralytics が
    # モデル読込時に stdout を UTF-8 へ切り替える前に出す最初の行だけ CP932 になり、
    # UTF-8 前提で読む GUI が「動画 1/N」の行を解析できず進捗表示が欠ける。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = parse_args()
    videos = list(args.videos)
    for video in videos:
        if not Path(video).exists():
            sys.exit(f"蜈･蜉帛虚逕ｻ縺悟ｭ伜惠縺励∪縺帙ｓ: {video}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    multiple = len(videos) > 1
    events: list[Event] = []
    cumulative_offset = 0.0   # 連結タイムライン上での各動画先頭の累積秒数
    # 解析開始時に総本数を出す。GUI・コンソール双方で「何本中の何本目か」を最初から示す。
    print(f"[info] 解析対象の動画: {len(videos)} 本", flush=True)
    for index, video in enumerate(videos):
        # 単一動画でも必ず「何本目/全体」を表示し、flush で GUI・コンソールへ即時反映する
        # (flush しないと 1 本目の表示が最初のバッチ処理までバッファに滞留する)。
        print(f"[info] === 動画 {index + 1}/{len(videos)}: {Path(video).name} "
              f"(offset={cumulative_offset:.2f}s) ===", flush=True)
        suffix = f"_{index + 1}" if multiple else ""
        tracks, fps, motion, duration = build_tracks(args, out_dir, video, suffix)
        print(f"[info] tracks={len(tracks)}"
              + ("" if motion is None else " (閾ｪ霆企°蜍輔ご繝ｼ繝・ 譛牙柑)"))

        for tr in tracks.values():
            ev = classify_overtaking(tr, args, motion)
            if ev is not None:
                ev.source_video = video
                ev.time_offset = cumulative_offset
                events.append(ev)
        cumulative_offset += duration

    # 連結タイムライン上の時刻で全動画を通して並べ替え
    events.sort(key=lambda e: e.global_t_start)

    print(f"[result] 霑ｽ縺・ｶ翫＠繧､繝吶Φ繝・ {len(events)} 莉ｶ")
    for i, e in enumerate(events, 1):
        ego = "" if e.ego_motion != e.ego_motion else f" ego={e.ego_motion:.2f}"
        src = f" [{Path(e.source_video).name}]" if multiple else ""
        print(f"  {i:03d} {e.cls:10s} side={e.side:5s} "
              f"{e.global_t_start:7.1f}s->{e.global_t_end:7.1f}s peak_area={e.peak_area:.4f}{ego}{src}")

    csv_path = out_dir / "overtaking_events.csv"
    write_csv(events, csv_path)
    print(f"[out] {csv_path}")

    if args.clip:
        cut_clips(events, args, out_dir)

    print("[done]")


if __name__ == "__main__":
    main()
