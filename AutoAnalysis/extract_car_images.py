"""clips/ の動画から、追い越し車の写真（車の部分の切り抜き）を作る。

car_length_gpt.py が ChatGPT へ送る画像を用意するためのスクリプト。

各イベントについて:
  1. レビュー済みCSVの最接近時刻（peak）から、クリップ内での時刻を求める。
     クリップは「動画の t_start - 2秒」から無再エンコードで切り出されている
     （detect_overtaking.cut_clips）。キーフレーム単位で切られるので開始が少し
     ずれることがあり、時刻だけで1枚を選ばず、前後の窓を YOLO で調べる。
  2. 窓の中を YOLO + ByteTrack で追跡し、最接近時刻の近くで最も大きく写る車を
     「追い越し車」とみなす。
  3. その車が画面の端で切れていないフレームのうち、最も大きく写った1枚を選び、
     車の周りを余白付きで切り抜いて保存する（1イベント1枚）。

出力:
  AutoAnalysis/car_images/<セッション>/<イベントID>.jpg        ChatGPTへ送る切り抜き
  AutoAnalysis/car_frames/<セッション>/<イベントID>.jpg        確認用（全体に枠を描いた1枚）
  AutoAnalysis/car_frames/<セッション>_一覧.jpg                 確認用画像を並べた一覧
  AutoAnalysis/car_images/extract_log.csv                       全イベントの結果

既に画像があるイベントは飛ばす（手で差し替えた画像を消さないため）。--force で作り直す。

    portable\\python\\python.exe AutoAnalysis\\extract_car_images.py            # 採用イベントだけ
    portable\\python\\python.exe AutoAnalysis\\extract_car_images.py --all      # 全イベント
    portable\\python\\python.exe AutoAnalysis\\extract_car_images.py --session w6b/obihiro
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
# リポジトリ内（AutoAnalysis/ 配下）でも配布版（単独フォルダ）でも動くよう、本体の web_gui.py が隣に
# あればその階層を、無ければ自分の階層をルート（output・yolov8n.pt・portable/python の置き場）にする。
ROOT = HERE.parent if (HERE.parent / "web_gui.py").is_file() else HERE
DEFAULT_SRC = ROOT / "output"
IMAGES_DIR = HERE / "car_images"
FRAMES_DIR = HERE / "car_frames"
MODEL_PATH = ROOT / "yolov8n.pt"

VEHICLE_CLASSES = {2: "car", 5: "bus", 7: "truck"}
CLIP_MARGIN = 2.0          # detect_overtaking の --margin 既定値（クリップ先頭の余白）
SAMPLE_EVERY = 2           # 何フレームごとに調べるか（24fps → 約12枚/秒）
# 至近距離で車が画面からはみ出しても、YOLO の枠は写っている部分だけを囲み、画面端まで
# 届かないことが多い（実測: 枠の右端 0.93〜0.96 でも車体の前半が切れていた）。そのため
# 左右は画面幅の 10%、下は 5% の帯に枠がかかったら「切れている」とみなす。
EDGE_X = 0.10
EDGE_BOTTOM = 0.05
EDGE_TOP = 0.01
# 切り抜きの余白（枠の幅・高さに対する割合）。斜め後ろから見た車では YOLO の枠が後半分
# しか囲まず車の前部が欠けやすいので、横は大きめに取る。
PAD_X = 0.45
PAD_Y = 0.12
MIN_AREA = 0.01            # 画面面積に対するこの割合未満の小さい車は使わない
PEAK_TOLERANCE_S = 2.5     # 追い越し車の最大面積がこの範囲で peak に近いこと
# 窓（peak からの相対秒）。前向きカメラでは追い越し後に車が前方へ抜けて全体が写り、
# 後ろ向きでは追い越し前に後方から近づく姿が写る。
WINDOWS = {"front": (-1.5, 4.0), "rear": (-4.0, 1.5)}
# 追い越し車が最も大きく写るときの横位置（枠の中心 / 画面幅）。日本は左側通行なので
# 車は自転車の右を抜ける: 前向きカメラでは画面の右寄り、後ろ向きでは左寄りに写る。
# 左側の建物や駐車車両の誤検出・遠くの車を候補から外すのに使う。
SIDE_OK = {"front": lambda cx: cx >= 0.55, "rear": lambda cx: cx <= 0.45}
MIN_PEAK_AREA = 0.02       # 自転車の真横を抜ける車ならこれ以上の大きさで写る


@dataclass
class Sighting:
    t: float
    frame_index: int
    box: tuple[float, float, float, float]
    area: float          # 画面面積に対する割合
    clipped: bool


@dataclass
class Track:
    track_id: int
    sightings: list[Sighting] = field(default_factory=list)

    def peak(self) -> Sighting:
        return max(self.sightings, key=lambda s: s.area)


def to_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def find_clip(session: Path, event: dict) -> Path | None:
    """make_report.find_clip と同じ規則でクリップを探す。"""
    clips = session / "clips"
    if not clips.is_dir():
        return None
    event_id = event.get("event_id", "")
    side = (event.get("side") or "").strip()
    names = ([f"event_{event_id}_{side}.mp4"] if side else []) + \
        [f"event_{event_id}_left.mp4", f"event_{event_id}_right.mp4"]
    for name in names:
        if (clips / name).is_file():
            return clips / name
    return next(iter(sorted(clips.glob(f"event_{event_id}_*.mp4"))), None)


def session_view(session: Path) -> str:
    report = session / "pipeline_report.json"
    if report.exists():
        view = json.loads(report.read_text(encoding="utf-8")).get("view", "")
        if view in WINDOWS:
            return view
    return "front"


def peak_in_clip(event: dict, clip_duration: float | None = None) -> float | None:
    """最接近時刻をクリップ内の秒へ直す。

    クリップは無再エンコードで切られ、開始が要求位置より前のキーフレームへずれる
    （実測で 0〜1 秒）。そのぶんクリップは要求より長くなるので、差を足して補正する。
    """
    t_start, peak = to_float(event.get("t_start_s")), to_float(event.get("peak_t_s"))
    local_start = to_float(event.get("local_t_start_s"))
    if t_start is None or peak is None or local_start is None:
        return None
    local_peak = peak - (t_start - local_start)
    clip_start = max(0.0, local_start - CLIP_MARGIN)
    shift = 0.0
    local_end = to_float(event.get("local_t_end_s"))
    if clip_duration and local_end is not None:
        requested = (local_end - local_start) + 2 * CLIP_MARGIN  # cut_clips の -t と同じ
        shift = min(max(0.0, clip_duration - requested), 1.5)
    return local_peak - clip_start + shift


def clip_duration(cv2, clip: Path) -> float | None:
    cap = cv2.VideoCapture(str(clip))
    fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return frames / fps if fps and frames else None


def track_window(model_path: Path, cv2, clip: Path, t0: float, t1: float) -> tuple[list[Track], float, tuple[int, int]]:
    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    first = max(0, int(t0 * fps))
    last = min(total - 1, int(t1 * fps)) if total else int(t1 * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    tracks: dict[int, Track] = {}
    size = (0, 0)
    # イベントごとにモデルを読み直す。使い回すと前のイベントの追跡状態が残り、
    # 全件まとめて実行したときだけ車を見失うことがあった（単独実行では見つかる）。
    from ultralytics import YOLO
    model = YOLO(str(model_path))
    for index in range(first, last + 1):
        ok, frame = cap.read()
        if not ok:
            break
        if (index - first) % SAMPLE_EVERY:
            continue
        h, w = frame.shape[:2]
        size = (w, h)
        result = model.track(frame, persist=True, classes=list(VEHICLE_CLASSES), conf=0.25,
                             agnostic_nms=True, tracker="bytetrack.yaml", verbose=False)[0]
        if result.boxes is None or result.boxes.id is None:
            continue
        for box, tid in zip(result.boxes.xyxy.tolist(), result.boxes.id.int().tolist()):
            x1, y1, x2, y2 = box
            clipped = (x1 <= EDGE_X * w or x2 >= (1 - EDGE_X) * w
                       or y2 >= (1 - EDGE_BOTTOM) * h or y1 <= EDGE_TOP * h)
            area = (x2 - x1) * (y2 - y1) / (w * h)
            tracks.setdefault(tid, Track(tid)).sightings.append(
                Sighting(index / fps, index, (x1, y1, x2, y2), area, clipped))
    cap.release()
    return list(tracks.values()), fps, size


def choose_track(tracks: list[Track], peak_t: float, view: str, width: int) -> Track | None:
    """追い越し車らしい車: 追い越し側に大きく写り、最接近時刻に近いもののうち最大。"""
    def cx(s: Sighting) -> float:
        return (s.box[0] + s.box[2]) / 2 / width if width else 0.5

    plausible = [t for t in tracks if len(t.sightings) >= 3 and t.peak().area >= MIN_PEAK_AREA
                 and SIDE_OK[view](cx(t.peak()))]
    near = [t for t in plausible if abs(t.peak().t - peak_t) <= PEAK_TOLERANCE_S]
    pool = near or plausible
    return max(pool, key=lambda t: t.peak().area) if pool else None


def choose_frame(track: Track) -> tuple[Sighting, bool]:
    """車全体が写ったフレームのうち最も大きく写った1枚を選ぶ。

    全体が写るのは追い越し後に少し前へ抜けたところ（斜め後ろからの姿）。全体が写った
    フレームが無ければ、はみ出していても最大の1枚を使い、その旨を返す。
    """
    full = [s for s in track.sightings if not s.clipped and s.area >= MIN_AREA]
    if full:
        return max(full, key=lambda s: s.area), False
    return track.peak(), True


def read_frame(cv2, clip: Path, index: int):
    cap = cv2.VideoCapture(str(clip))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def crop(frame, box):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * PAD_X, (y2 - y1) * PAD_Y
    x1, y1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    x2, y2 = min(w, int(x2 + px)), min(h, int(y2 + py))
    return frame[y1:y2, x1:x2]


def imwrite(cv2, path: Path, image) -> None:
    """日本語パスでも書けるよう、エンコードしてから書き込む（cv2.imwrite は非ASCIIパス不可）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError(f"JPEGに変換できません: {path}")
    path.write_bytes(data.tobytes())


def load_events(session: Path, include_all: bool) -> list[dict]:
    path = session / "reviewed_overtaking_events.csv"
    if not path.exists():
        path = session / "overtaking_events.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not include_all:
        rows = [r for r in rows if r.get("review_status", "採用") == "採用"]
    return rows


def write_overview(cv2, frames_dir: Path, skey: str) -> Path | None:
    """確認用画像を4列に並べた一覧（<セッション>_一覧.jpg）を作る。目視の見直し用。"""
    import numpy as np
    paths = sorted((frames_dir / skey).glob("*.jpg"))
    if not paths:
        return None
    tiles = []
    for path in paths:
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        image = cv2.resize(image, (480, 270))
        cv2.putText(image, path.stem, (8, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3)
        tiles.append(image)
    while len(tiles) % 4:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)])
    out = frames_dir / f"{skey.replace('/', '_')}_一覧.jpg"
    imwrite(cv2, out, sheet)
    return out


LOG_COLUMNS = ["session", "event_id", "status", "clip", "view", "peak_in_clip_s",
               "image_times_s", "max_area_ratio", "all_clipped", "track_count", "images"]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="clips/ の動画から追い越し車の写真を切り出す")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--images", type=Path, default=IMAGES_DIR)
    parser.add_argument("--frames", type=Path, default=FRAMES_DIR)
    parser.add_argument("--session", default="", help="このセッションだけ（例: w6b/obihiro）")
    parser.add_argument("--all", action="store_true", help="採用以外（未確認・除外）のイベントも対象にする")
    parser.add_argument("--force", action="store_true", help="画像があるイベントも作り直す")
    parser.add_argument("--model", type=Path, default=MODEL_PATH)
    args = parser.parse_args()

    import cv2
    from ultralytics import YOLO  # noqa: F401  起動時に読み込めるか確かめる

    src = args.src.resolve()
    sessions = sorted(p.parent for p in src.rglob("clips") if p.is_dir())
    if args.session:
        sessions = [s for s in sessions if s.relative_to(src).as_posix() == args.session]
    if not sessions:
        sys.exit("clips/ を持つセッションがありません")

    log_path = args.images / "extract_log.csv"
    log: dict[tuple[str, str], dict] = {}
    if log_path.exists():
        with log_path.open("r", encoding="utf-8-sig", newline="") as stream:
            log = {(r["session"], r["event_id"]): r for r in csv.DictReader(stream)}

    counts: dict[str, int] = {}
    for session in sessions:
        skey = session.relative_to(src).as_posix()
        view = session_view(session)
        events = load_events(session, args.all)
        print(f"== {skey}（{view}）: {len(events)} イベント", flush=True)
        for event in events:
            event_id = event.get("event_id", "")
            out_dir = args.images / skey
            existing = sorted([*out_dir.glob(f"{event_id}.jpg"), *out_dir.glob(f"{event_id}_*.jpg")])                 if out_dir.exists() else []
            if existing and not args.force:
                counts["既存のため省略"] = counts.get("既存のため省略", 0) + 1
                continue
            for old in existing:
                old.unlink()
            stale = args.frames / skey / f"{event_id}.jpg"
            if stale.exists():  # 前回の確認用画像が今回の結果と食い違わないように消す
                stale.unlink()
            entry = {"session": skey, "event_id": event_id, "view": view}
            clip = find_clip(session, event)
            peak_t = peak_in_clip(event, clip_duration(cv2, clip)) if clip else None
            if clip is None:
                entry["status"] = "クリップなし"
            elif peak_t is None:
                entry.update(status="最接近時刻なし", clip=clip.name)
            else:
                lo, hi = WINDOWS[view]
                tracks, _fps, size = track_window(args.model, cv2, clip, max(0.0, peak_t + lo), peak_t + hi)
                track = choose_track(tracks, peak_t, view, size[0])
                entry.update(clip=clip.name, peak_in_clip_s=f"{peak_t:.2f}", track_count=len(tracks))
                if track is None:
                    entry["status"] = "車を検出できず"
                else:
                    s, all_clipped = choose_frame(track)
                    frame = read_frame(cv2, clip, s.frame_index)
                    name = ""
                    if frame is not None:
                        name = f"{event_id}.jpg"
                        imwrite(cv2, out_dir / name, crop(frame, s.box))
                        x1, y1, x2, y2 = map(int, s.box)
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 4)
                        imwrite(cv2, args.frames / skey / f"{event_id}.jpg", frame)
                    entry.update(
                        status=("端で切れている" if all_clipped else "OK") if name else "保存失敗",
                        image_times_s=f"{s.t:.2f}", max_area_ratio=f"{s.area:.3f}",
                        all_clipped=all_clipped, images=name)
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            log[(skey, event_id)] = entry
            print(f"  {event_id}: {entry['status']} {entry.get('images', '')}", flush=True)

        write_overview(cv2, args.frames, skey)
        args.images.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=LOG_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(log[k] for k in sorted(log))
    print("結果: " + " / ".join(f"{k} {v}件" for k, v in counts.items()))
    print(f"確認用の画像: {args.frames}")


if __name__ == "__main__":
    main()
