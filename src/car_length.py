"""追い越し車の車長を、動画から切り出した写真1枚と ChatGPT で求める（全自動実行の段階6.5）。

相対速度 = 3.6 × (距離p3 + 車長) / 通過時間 の「車長」をここで用意する
（``docs/計算方法.md`` 9.5）。

各イベント（「除外」以外）について:
  1. 動画（``out/clips`` のクリップ。無ければ元動画）の最接近時刻の前後を YOLO + ByteTrack
     で調べ、追い越し車が画面の端で切れずに最も大きく写った1枚を、車の周りを余白付きで
     切り抜く（1イベント1枚）。
  2. その1枚を ChatGPT（OpenAI Responses API）へ送り、車名と全長（カタログ値。特定でき
     なければ車体形状からの推定）を答えさせる。
  3. ``out/car_length/car_lengths.csv`` へ書く。

出力（``out/car_length/``）:
  car_lengths.csv     イベントごとの車長。source が manual の行（人が確認・入力した車長）は
                      どの場合も上書きしない。推定済み（gpt:...）の行は --force のときだけ推定し直す
  images/<ID>.jpg     ChatGPT へ送った切り抜き。手で差し替えると次の推定ではその画像を送る
  frames/<ID>.jpg     確認用（全体フレームに枠を描いた1枚）
  gpt_raw/<ID>.json   ChatGPT の生の応答（判断の根拠を後から確かめるため）

APIキーは環境変数 OPENAI_API_KEY、無ければ本体フォルダの ``.env``、``AutoAnalysis/.env``
の順に探す。キーの値は画面にもログにも出さない。モデルは OPENAI_MODEL で変えられる。

切り出しの規則は車長変換ツール（``AutoAnalysis/extract_car_images.py``）と同じ。
あちらは単独で配布するため、コードは共有せずここに持つ。

使い方（全自動実行の後から、1セッションずつ実行する場合）:
    python car_length.py --out output/w5o/ID1            # 推定して解析xlsx・最終Excelを作り直す
    python car_length.py --out output/w5o/ID1 --dry-run  # 送る予定の一覧だけ（APIは呼ばない）
    python car_length.py --import-lengths output_車長変換/car_lengths.csv --src output
        # 車長変換ツールで推定・確認済みの車長を各セッションへ取り込む（APIは呼ばない）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 埋め込みPython（portable/python）は実行したスクリプトの場所を import の起点にしないので、
# 隣のモジュール（src/）を読めるよう自分のフォルダを足す。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

# このファイルは src/ にある。APP_DIR はプロジェクトのルート（portable/・output/・tools/・
# yolov8n.pt・設定ファイルの置き場）、SRC_DIR はコードと画面（web_ui*/）の置き場。
SRC_DIR = Path(__file__).resolve().parent
APP_DIR = SRC_DIR.parent
CAR_LENGTH_DIR = "car_length"
LENGTHS_NAME = "car_lengths.csv"
COLUMNS = ["event_id", "car_length_m", "source", "make", "model", "body_type",
           "confidence", "image", "status", "note", "updated_at"]
MODEL_PATH = APP_DIR / "yolov8n.pt"

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.5"
ENV_PATHS = (APP_DIR / ".env", APP_DIR / "AutoAnalysis" / ".env")
KEY_NAMES = ("OPENAI_API_KEY", "OPENAI_KEY", "CHATGPT_API_KEY", "CHATGPT_KEY", "API_KEY")

# ---- 写真の切り出し（AutoAnalysis/extract_car_images.py と同じ値） -------------------
VEHICLE_CLASSES = {2: "car", 5: "bus", 7: "truck"}
CLIP_MARGIN = 2.0          # detect_overtaking の --margin 既定値（クリップ先頭の余白）
SAMPLE_EVERY = 2           # 何フレームごとに調べるか（24fps → 約12枚/秒）
# 至近距離で車が画面からはみ出しても、YOLO の枠は写っている部分だけを囲み画面端まで
# 届かないことが多い。左右は画面幅の 10%、下は 5% の帯に枠がかかったら「切れている」とみなす。
EDGE_X = 0.10
EDGE_BOTTOM = 0.05
EDGE_TOP = 0.01
# 切り抜きの余白（枠の幅・高さに対する割合）。斜め後ろから見た車では YOLO の枠が後半分
# しか囲まず車の前部が欠けやすいので、横は大きめに取る。
PAD_X = 0.45
PAD_Y = 0.12
MIN_AREA = 0.01            # 画面面積に対するこの割合未満の小さい車は使わない
PEAK_TOLERANCE_S = 2.5     # 追い越し車の最大面積がこの範囲で最接近時刻に近いこと
# 窓（最接近時刻からの相対秒）。前向きカメラでは追い越し後に車が前方へ抜けて全体が写り、
# 後ろ向きでは追い越し前に後方から近づく姿が写る。
WINDOWS = {"front": (-1.5, 4.0), "rear": (-4.0, 1.5)}
# 日本は左側通行で車は自転車の右を抜ける: 前向きカメラでは画面の右寄り、後ろ向きでは
# 左寄りに写る。左側の建物や駐車車両の誤検出・遠くの車を候補から外すのに使う。
SIDE_OK = {"front": lambda cx: cx >= 0.55, "rear": lambda cx: cx <= 0.45}
MIN_PEAK_AREA = 0.02       # 自転車の真横を抜ける車ならこれ以上の大きさで写る

# ---- ChatGPT（AutoAnalysis/car_length_gpt.py と同じ問い） ------------------------------
PROMPT = """これは日本の公道で、自転車に取り付けたカメラから撮影した、自転車を追い越していく自動車の写真です。
写っている自動車1台について、次を答えてください。
1. メーカー・車名・型式（世代）をできるだけ特定する。
2. その車のカタログ上の全長（メートル）。特定できない場合は、車体形状と大きさから全長を推定する。
3. 推定の確かさ（0〜1）。車名を特定できカタログ値を使えたなら高く、形状からの推定なら低くする。
軽自動車（黄色ナンバー等）なら全長は3.4m以下であることに注意してください。
画像に自動車が写っていない・複数の車のどれか判断できない場合は vehicle_visible を false にし、length_m は null にしてください。"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "vehicle_visible": {"type": "boolean"},
        "make": {"type": "string"},
        "model": {"type": "string"},
        "body_type": {"type": "string", "description": "軽自動車/セダン/ミニバン/SUV/トラック/バス など"},
        "length_m": {"type": ["number", "null"]},
        "length_basis": {"type": "string", "enum": ["catalog", "estimate", "unknown"]},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["vehicle_visible", "make", "model", "body_type", "length_m",
                 "length_basis", "confidence", "notes"],
}
# 応答の全長がこの範囲外なら採らない（二輪〜大型バス・トレーラーまでを含む幅）。
MIN_LENGTH_M, MAX_LENGTH_M = 1.0, 30.0


@dataclass
class CarLength:
    meters: float
    source: str
    make: str = ""
    model: str = ""


# ================================================================ 車長一覧の読み書き

def car_length_dir(out_dir: Path) -> Path:
    return Path(out_dir) / CAR_LENGTH_DIR


def lengths_path(out_dir: Path) -> Path:
    return car_length_dir(out_dir) / LENGTHS_NAME


def _to_float(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN を除く


def read_length_rows(out_dir: Path) -> list[dict]:
    path = lengths_path(out_dir)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_length_rows(out_dir: Path, rows: list[dict]) -> Path:
    path = lengths_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return path


def load_car_lengths(out_dir: Path) -> dict[str, CarLength]:
    """``event_id -> CarLength``。車長が空欄・範囲外の行は含めない。"""
    lengths: dict[str, CarLength] = {}
    for row in read_length_rows(out_dir):
        meters = _to_float(row.get("car_length_m"))
        if meters is None or not MIN_LENGTH_M <= meters <= MAX_LENGTH_M:
            continue
        lengths[str(row.get("event_id", "")).strip()] = CarLength(
            meters, (row.get("source") or "csv").strip(),
            (row.get("make") or "").strip(), (row.get("model") or "").strip())
    return lengths


def event_id_for_path(event_id: str) -> str:
    """ファイル名に使うイベントID（``final_export._event_id_for_path`` と同じ規則）。"""
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(event_id or "")) or "event"


def lookup_car_length(out_dir: Path, event_id: str) -> CarLength | None:
    """イベントIDの車長。ファイル名用に置き換えたIDでも引けるようにする。"""
    lengths = load_car_lengths(out_dir)
    if event_id in lengths:
        return lengths[event_id]
    by_path = {event_id_for_path(key): value for key, value in lengths.items()}
    return by_path.get(event_id_for_path(event_id))


# ================================================================ 写真の切り出し

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


def find_clip(out_dir: Path, event: dict) -> Path | None:
    """make_report.find_clip と同じ規則でクリップを探す。"""
    clips = Path(out_dir) / "clips"
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


def local_peak(event: dict) -> float | None:
    """最接近時刻を、その候補の元動画の中の秒で返す。"""
    t_start, peak = _to_float(event.get("t_start_s")), _to_float(event.get("peak_t_s"))
    local_start = _to_float(event.get("local_t_start_s"))
    if t_start is None or peak is None or local_start is None:
        return None
    return peak - (t_start - local_start)


def peak_in_clip(event: dict, clip_duration: float | None = None) -> float | None:
    """最接近時刻をクリップ内の秒へ直す。

    クリップは無再エンコードで切られ、開始が要求位置より前のキーフレームへずれる
    （実測で 0〜1 秒）。そのぶんクリップは要求より長くなるので、差を足して補正する。
    """
    peak = local_peak(event)
    local_start = _to_float(event.get("local_t_start_s"))
    if peak is None or local_start is None:
        return None
    clip_start = max(0.0, local_start - CLIP_MARGIN)
    shift = 0.0
    local_end = _to_float(event.get("local_t_end_s"))
    if clip_duration and local_end is not None:
        requested = (local_end - local_start) + 2 * CLIP_MARGIN  # cut_clips の -t と同じ
        shift = min(max(0.0, clip_duration - requested), 1.5)
    return peak - clip_start + shift


def video_for_event(out_dir: Path, event: dict, cv2) -> tuple[Path | None, float | None, str]:
    """(動画, 動画内の最接近時刻, 説明)。クリップを優先し、無ければ元動画を使う。"""
    clip = find_clip(out_dir, event)
    if clip is not None:
        return clip, peak_in_clip(event, _video_duration(cv2, clip)), "clip"
    source = Path(str(event.get("source_video") or ""))
    if str(source) and source.is_file():
        return source, local_peak(event), "source"
    return None, None, ""


def _video_duration(cv2, path: Path) -> float | None:
    cap = cv2.VideoCapture(str(path))
    fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return frames / fps if fps and frames else None


def track_window(cv2, video: Path, t0: float, t1: float,
                 model_path: Path = MODEL_PATH) -> tuple[list[Track], tuple[int, int]]:
    cap = cv2.VideoCapture(str(video))
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
    return list(tracks.values()), size


def choose_track(tracks: list[Track], peak_t: float, view: str, width: int) -> Track | None:
    """追い越し車らしい車: 追い越し側に大きく写り、最接近時刻に近いもののうち最大。"""
    side_ok = SIDE_OK.get(view, SIDE_OK["front"])

    def cx(s: Sighting) -> float:
        return (s.box[0] + s.box[2]) / 2 / width if width else 0.5

    plausible = [t for t in tracks if len(t.sightings) >= 3 and t.peak().area >= MIN_PEAK_AREA
                 and side_ok(cx(t.peak()))]
    near = [t for t in plausible if abs(t.peak().t - peak_t) <= PEAK_TOLERANCE_S]
    pool = near or plausible
    return max(pool, key=lambda t: t.peak().area) if pool else None


def choose_frame(track: Track) -> tuple[Sighting, bool]:
    """車全体が写ったフレームのうち最も大きく写った1枚。無ければ最大の1枚（切れている）。"""
    full = [s for s in track.sightings if not s.clipped and s.area >= MIN_AREA]
    if full:
        return max(full, key=lambda s: s.area), False
    return track.peak(), True


def _read_frame(cv2, video: Path, index: int):
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _crop(frame, box):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * PAD_X, (y2 - y1) * PAD_Y
    x1, y1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    x2, y2 = min(w, int(x2 + px)), min(h, int(y2 + py))
    return frame[y1:y2, x1:x2]


def _imwrite(cv2, path: Path, image) -> None:
    """日本語パスでも書けるよう、エンコードしてから書き込む（cv2.imwrite は非ASCIIパス不可）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError(f"JPEGに変換できません: {path}")
    path.write_bytes(data.tobytes())


def image_path(out_dir: Path, event_id: str) -> Path:
    return car_length_dir(out_dir) / "images" / f"{event_id_for_path(event_id)}.jpg"


def extract_event_image(cv2, out_dir: Path, event: dict, view: str,
                        model_path: Path = MODEL_PATH) -> tuple[Path | None, str]:
    """追い越し車の切り抜きを1枚保存する。戻り値は (画像, 状態の説明)。"""
    event_id = str(event.get("event_id", ""))
    video, peak_t, kind = video_for_event(out_dir, event, cv2)
    if video is None:
        return None, "動画なし（clips/ にクリップが無く、元動画も見つかりません）"
    if peak_t is None:
        return None, "最接近時刻なし"
    lo, hi = WINDOWS.get(view, WINDOWS["front"])
    tracks, size = track_window(cv2, video, max(0.0, peak_t + lo), peak_t + hi, model_path)
    track = choose_track(tracks, peak_t, view, size[0])
    if track is None:
        return None, "車を検出できず"
    sighting, all_clipped = choose_frame(track)
    frame = _read_frame(cv2, video, sighting.frame_index)
    if frame is None:
        return None, "フレームを読めません"
    out = image_path(out_dir, event_id)
    _imwrite(cv2, out, _crop(frame, sighting.box))
    x1, y1, x2, y2 = map(int, sighting.box)
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 4)
    _imwrite(cv2, car_length_dir(out_dir) / "frames" / out.name, frame)
    where = "クリップ" if kind == "clip" else "元動画"
    return out, f"{where} {sighting.t:.2f}s" + ("（車が画面の端で切れている）" if all_clipped else "")


# ================================================================ ChatGPT

def _load_env(path: Path) -> dict[str, str]:
    """.env を読む。KEY=VALUE / KEY: VALUE / キーだけの行（sk-...）に対応。値は表示しない。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line)
        if match:
            values[match.group(1).upper()] = match.group(2).strip().strip('"').strip("'")
        elif line.startswith("sk-"):
            values.setdefault("_BARE_KEY", line.strip('"').strip("'"))
    return values


def find_api_settings(env_paths: tuple[Path, ...] = ENV_PATHS) -> tuple[str, str]:
    """(APIキー, モデル)。キーが無ければ空文字。環境変数 → .env の順。"""
    envs = [_load_env(path) for path in env_paths]
    model = os.environ.get("OPENAI_MODEL") or next(
        (env["OPENAI_MODEL"] for env in envs if env.get("OPENAI_MODEL")), DEFAULT_MODEL)
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name], model
    for env in envs:
        for name in KEY_NAMES:
            if env.get(name):
                return env[name], model
        if env.get("_BARE_KEY"):
            return env["_BARE_KEY"], model
        candidates = [v for v in env.values() if v.startswith("sk-")]
        if len(candidates) == 1:
            return candidates[0], model
    return "", model


def call_openai(api_key: str, model: str, image: Path, retries: int = 3) -> dict:
    data = base64.b64encode(image.read_bytes()).decode("ascii")
    content = [{"type": "input_text", "text": PROMPT},
               {"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}"}]
    body = json.dumps({
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "car_length", "strict": True, "schema": SCHEMA}},
    }).encode("utf-8")
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenAI API エラー {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenAI API に接続できません: {exc.reason}") from None
    raise RuntimeError("OpenAI API の再試行回数を超えました")


def parse_answer(response: dict) -> dict:
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                return json.loads(part["text"])
            if part.get("type") == "refusal":
                raise RuntimeError(f"応答が拒否されました: {part.get('refusal')}")
    raise RuntimeError("応答に結果のテキストがありません")


def answer_to_row(answer: dict, model: str) -> dict:
    """ChatGPT の答えを car_lengths.csv の列へ直す。範囲外・車が見えない場合は車長を空欄にする。"""
    length = _to_float(answer.get("length_m"))
    usable = bool(answer.get("vehicle_visible")) and length is not None \
        and MIN_LENGTH_M <= length <= MAX_LENGTH_M
    return {
        "car_length_m": f"{length:.3f}" if usable else "",
        "source": f"gpt:{model}:{answer.get('length_basis', '')}",
        "make": answer.get("make", ""), "model": answer.get("model", ""),
        "body_type": answer.get("body_type", ""),
        "confidence": f"{float(answer.get('confidence') or 0):.2f}",
        "status": "推定済み" if usable else "車長を特定できず",
        "note": answer.get("notes", ""),
    }


# ================================================================ 本体

def target_events(events: list[dict], include_rejected: bool = False) -> list[dict]:
    """車長を求める候補。既定は「除外」以外（採用・未確認）。"""
    if include_rejected:
        return list(events)
    return [e for e in events if (e.get("review_status") or "採用") != "除外"]


def estimate_car_lengths(out_dir: Path, events: list[dict], view: str = "front", *,
                         force: bool = False, dry_run: bool = False,
                         log: Callable[[str], None] = print,
                         api_settings: tuple[str, str] | None = None,
                         model_path: Path = MODEL_PATH) -> dict:
    """候補ごとに写真を1枚切り出して ChatGPT で車長を推定し、car_lengths.csv を更新する。

    戻り値は件数の要約 ``{"estimated", "kept", "failed", "no_key", "messages", "path"}``。
    写真が1枚も無ければ API は呼ばない。APIキーが無いときは写真の切り出しまでで止め、
    ``no_key`` を True にする（相対速度は車長が入るまで空欄）。
    """
    out_dir = Path(out_dir)
    rows = read_length_rows(out_dir)
    index = {str(r.get("event_id", "")): r for r in rows}
    summary = {"estimated": 0, "kept": 0, "failed": 0, "no_key": False,
               "messages": [], "path": str(lengths_path(out_dir))}

    todo: list[dict] = []
    for event in events:
        event_id = str(event.get("event_id", ""))
        source = (index.get(event_id) or {}).get("source", "")
        if source == "manual" or (source.startswith("gpt") and not force):
            summary["kept"] += 1
            continue
        todo.append(event)
    if not todo:
        log(f"[car_length] 推定するものはありません（推定済み・手入力済み {summary['kept']} 件）")
        return summary

    images: dict[str, Path] = {}
    need_extract = [e for e in todo
                    if force or not image_path(out_dir, str(e.get("event_id", ""))).is_file()]
    for event in todo:
        path = image_path(out_dir, str(event.get("event_id", "")))
        if path.is_file() and event not in need_extract:
            images[str(event.get("event_id", ""))] = path
    if need_extract:
        try:
            import cv2
            import ultralytics  # noqa: F401  起動時に読み込めるか確かめる
        except ImportError as exc:
            summary["messages"].append(f"写真を切り出せません（{exc}）")
            need_extract = []
        else:
            for event in need_extract:
                event_id = str(event.get("event_id", ""))
                try:
                    path, status = extract_event_image(cv2, out_dir, event, view, model_path)
                except Exception as exc:  # noqa: BLE001 - 1件の失敗で止めない
                    path, status = None, f"切り出しに失敗: {exc}"
                log(f"[car_length] {event_id}: 写真 {status}")
                if path is not None:
                    images[event_id] = path
                else:
                    _update_row(rows, index, event_id, {"status": status})
                    summary["failed"] += 1
                    summary["messages"].append(f"候補 {event_id}: 写真を切り出せません（{status}）")

    if dry_run:
        log(f"[car_length] 送る予定: {len(images)} 件（APIは呼びません）")
        for event_id, path in sorted(images.items()):
            log(f"  {event_id}: {path.name}")
        return summary

    if images:
        api_key, model = api_settings if api_settings is not None else find_api_settings()
        if not api_key:
            summary["no_key"] = True
            summary["messages"].append(
                "OpenAI の APIキーが見つからないため車長を推定できません（"
                + " / ".join(str(p) for p in ENV_PATHS)
                + " に OPENAI_API_KEY=sk-... を書いてください）")
        else:
            raw_dir = car_length_dir(out_dir) / "gpt_raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            for n, (event_id, path) in enumerate(sorted(images.items()), 1):
                label = f"[car_length] [{n}/{len(images)}] {event_id}"
                try:
                    response = call_openai(api_key, model, path)
                    (raw_dir / f"{event_id_for_path(event_id)}.json").write_text(
                        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
                    values = answer_to_row(parse_answer(response), model)
                except Exception as exc:  # noqa: BLE001 - 1件の失敗で止めない
                    summary["failed"] += 1
                    summary["messages"].append(f"候補 {event_id}: 車長を推定できません（{exc}）")
                    _update_row(rows, index, event_id, {"status": f"推定に失敗: {exc}"[:200],
                                                        "image": path.name})
                    log(f"{label}: 失敗 {exc}")
                    continue
                values["image"] = path.name
                _update_row(rows, index, event_id, values)
                write_length_rows(out_dir, rows)  # 1件ごとに保存（途中で止まっても結果が残る）
                if values["car_length_m"]:
                    summary["estimated"] += 1
                else:
                    summary["failed"] += 1
                    summary["messages"].append(f"候補 {event_id}: 写真から車長を特定できませんでした")
                log(f"{label}: {values['make']} {values['model']} 全長 "
                    f"{values['car_length_m'] or '不明'} m (確度 {values['confidence']})")
    write_length_rows(out_dir, rows)
    return summary


def _update_row(rows: list[dict], index: dict[str, dict], event_id: str, values: dict) -> None:
    row = index.get(event_id)
    if row is None:
        row = {"event_id": event_id}
        rows.append(row)
        index[event_id] = row
    row.update(values)
    row["updated_at"] = datetime.now().isoformat(timespec="seconds")


def import_lengths(csv_path: Path, src: Path) -> dict[str, int]:
    """車長変換ツールの car_lengths.csv（session 列つき）を各セッションの一覧へ取り込む。

    session 列は ``src`` からの相対パス（例: ``w5o/ID1``）。取り込み先に車長がある行
    （特に manual）は上書きしない。戻り値はセッションごとの取り込み件数。
    """
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    by_session: dict[str, list[dict]] = {}
    for row in source_rows:
        meters = _to_float(row.get("car_length_m"))
        if meters is None or not MIN_LENGTH_M <= meters <= MAX_LENGTH_M:
            continue
        by_session.setdefault((row.get("session") or "").strip(), []).append(row)
    counts: dict[str, int] = {}
    for session, items in sorted(by_session.items()):
        out_dir = Path(src) / session
        if not session or not out_dir.is_dir():
            continue
        rows = read_length_rows(out_dir)
        index = {str(r.get("event_id", "")): r for r in rows}
        added = 0
        for item in items:
            event_id = (item.get("event_id") or "").strip()
            current = index.get(event_id) or {}
            if _to_float(current.get("car_length_m")) is not None:
                continue
            _update_row(rows, index, event_id, {
                "car_length_m": item.get("car_length_m", ""),
                "source": item.get("source") or "csv",
                "make": item.get("make", ""), "model": item.get("model", ""),
                "body_type": item.get("body_type", ""), "confidence": item.get("confidence", ""),
                "image": item.get("images", ""), "status": "取り込み",
                "note": item.get("note", ""),
            })
            added += 1
        if added:
            write_length_rows(out_dir, rows)
            counts[session] = added
    return counts


def read_review_events(out_dir: Path) -> list[dict]:
    for name in ("reviewed_overtaking_events.csv", "overtaking_events.csv"):
        path = Path(out_dir) / name
        if path.is_file():
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))
    return []


def session_view(out_dir: Path) -> str:
    try:
        view = json.loads((Path(out_dir) / "pipeline_report.json").read_text(encoding="utf-8")).get("view", "")
    except (OSError, json.JSONDecodeError):
        view = ""
    return view if view in WINDOWS else "front"


def rewrite_analysis_xlsx(out_dir: Path, log: Callable[[str], None] | None = print) -> int:
    """車長を反映するため、lidar_edit/ の解析xlsx を全部作り直す。作り直した件数を返す。"""
    from edit_csv import write_edit_analysis_xlsx

    rewritten = 0
    for edit in sorted((Path(out_dir) / "lidar_edit").glob("overtaking_*_edit.csv")):
        try:
            write_edit_analysis_xlsx(edit)
            rewritten += 1
        except Exception as exc:  # noqa: BLE001 - 解析できない候補は飛ばす
            if log:
                log(f"[car_length] {edit.name}: 解析xlsxを作り直せません: {exc}")
    if log:
        log(f"[car_length] 解析xlsx {rewritten} 件を作り直しました")
    return rewritten


def estimate_for_final(out_dir: Path, view: str = "",
                       log: Callable[[str], None] = print) -> dict:
    """最終Excelを作る前に、採用の候補の車長を推定して解析xlsx を作り直す（手動GUI用）。

    最終Excelに載るのは「採用」だけなので、それ以外は推定しない（API の料金を抑える）。
    推定済み・手入力済みの候補は飛ばすので、最終Excelを何度作り直しても API は
    新しい候補の分しか呼ばない。戻り値は estimate_car_lengths の要約に ``targets``
    （採用の候補数）を足したもの。
    """
    out_dir = Path(out_dir)
    events = [e for e in read_review_events(out_dir)
              if (e.get("review_status") or "採用") == "採用"]
    summary = estimate_car_lengths(out_dir, events, view if view in WINDOWS else session_view(out_dir),
                                   log=log)
    summary["targets"] = len(events)
    rewrite_analysis_xlsx(out_dir, log)
    return summary


def refresh_outputs(out_dir: Path, log: Callable[[str], None] = print) -> None:
    """車長を反映するため、解析xlsx と最終Excel（実行レポートの設定で）を作り直す。"""
    from final_export import generate_final_workbook

    out_dir = Path(out_dir)
    rewrite_analysis_xlsx(out_dir, log)
    try:
        report = json.loads((out_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("[car_length] pipeline_report.json が無いため最終Excelは作り直しません"
            "（GUIの「最終Excelを再生成」で作り直してください）")
        return
    info = report.get("info") or {}
    try:
        sync = json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sync = {}
    offset = _to_float(info.get("時刻オフセット(秒)"))
    if offset is None:
        offset = _to_float(sync.get("offset")) or 0.0
    result = generate_final_workbook(
        out_dir,
        organize_id=str(report.get("organize_id") or ""),
        subject_id=str(report.get("subject_id") or ""),
        collection_date=str(report.get("collection_date") or ""),
        offset=offset,
        pcap_path=str(report.get("pcap") or ""),
        pcap_start_unix=info.get("PCAP開始Unix時刻"),
    )
    log(f"[car_length] 最終Excelを作り直しました: {result['path']}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="動画の写真1枚から ChatGPT で追い越し車の車長を推定する")
    parser.add_argument("--out", type=Path, help="全自動実行の出力フォルダ（1セッション）")
    parser.add_argument("--all", action="store_true", help="「除外」の候補も対象にする")
    parser.add_argument("--force", action="store_true",
                        help="写真と推定済み(gpt)の車長を作り直す（manual の車長は保つ）")
    parser.add_argument("--dry-run", action="store_true", help="写真の切り出しまで行い、APIは呼ばない")
    parser.add_argument("--no-refresh", action="store_true", help="解析xlsx・最終Excelを作り直さない")
    parser.add_argument("--import-lengths", type=Path,
                        help="車長変換ツールの car_lengths.csv（session 列つき）を取り込む")
    parser.add_argument("--src", type=Path, help="--import-lengths の session 列の基準フォルダ（例: output）")
    args = parser.parse_args(argv)

    if args.import_lengths:
        if not args.src:
            parser.error("--import-lengths には --src（session 列の基準フォルダ）が必要です")
        counts = import_lengths(args.import_lengths, args.src)
        for session, added in counts.items():
            print(f"{session}: {added} 件取り込みました")
        print(f"合計 {sum(counts.values())} 件")
        if not args.no_refresh:
            for session in counts:
                refresh_outputs(args.src / session)
        return 0
    if not args.out or not args.out.is_dir():
        parser.error("--out に全自動実行の出力フォルダを指定してください")
    events = target_events(read_review_events(args.out), include_rejected=args.all)
    if not events:
        print("対象の候補がありません（reviewed_overtaking_events.csv を確認してください）")
        return 1
    summary = estimate_car_lengths(args.out, events, session_view(args.out),
                                   force=args.force, dry_run=args.dry_run)
    for message in summary["messages"]:
        print(f"[car_length] {message}")
    print(f"[car_length] 推定 {summary['estimated']} / 既存 {summary['kept']} / "
          f"失敗 {summary['failed']} → {summary['path']}")
    if not args.dry_run and not args.no_refresh:
        refresh_outputs(args.out)
    return 2 if summary["no_key"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
