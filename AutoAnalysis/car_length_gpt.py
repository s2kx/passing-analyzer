"""車の写真から ChatGPT（OpenAI API）で車長を推定し、car_lengths.csv へ書き込む。

画像の置き場所（イベントごとに1〜4枚。ファイル名はイベントIDで始める）:
    AutoAnalysis/car_images/<セッション>/<イベントID>[_任意].jpg|jpeg|png|webp
    例) AutoAnalysis/car_images/w6b/ID1/001.jpg, 001_b.jpg

APIキーは AutoAnalysis/.env の OPENAI_API_KEY から読む（環境変数があればそちらを優先）。
キーの値は画面にもログにも一切出さない。モデルは .env の OPENAI_MODEL で変えられる。

    python AutoAnalysis/car_length_gpt.py --dry-run      # 送る予定の一覧だけ表示（APIは呼ばない）
    python AutoAnalysis/car_length_gpt.py                # 推定して car_lengths.csv を更新
    python AutoAnalysis/car_length_gpt.py --force        # 推定済み(gpt)の行も推定し直す

source が "manual" の行（人が確認・入力した車長）はどの場合も上書きしない。
API の生の応答は --raw のフォルダ（既定 AutoAnalysis/gpt_raw/）に保存する（後から根拠を確認できるように）。
"""
from __future__ import annotations

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
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
IMAGES_DIR = HERE / "car_images"
LENGTHS_PATH = HERE / "car_lengths.csv"
RAW_DIR = HERE / "gpt_raw"
API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.5"
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MAX_IMAGES = 4

sys.path.insert(0, str(HERE))
from convert_relative_speed import LENGTH_COLUMNS  # noqa: E402

PROMPT = """これは日本の公道で、自転車に取り付けたカメラから撮影した、自転車を追い越していく自動車の写真です（複数枚は同じ車）。
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


KEY_NAMES = ("OPENAI_API_KEY", "OPENAI_KEY", "CHATGPT_API_KEY", "CHATGPT_KEY", "API_KEY")


def load_env(path: Path) -> dict[str, str]:
    """.env を読む。KEY=VALUE / KEY: VALUE / キーだけの行（sk-...）に対応。値は表示しない。"""
    values: dict[str, str] = {}
    if not path.exists():
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


def find_api_key(env: dict[str, str]) -> str:
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    for name in KEY_NAMES:
        if env.get(name):
            return env[name]
    if env.get("_BARE_KEY"):
        return env["_BARE_KEY"]
    # 名前が想定外でも、値が sk- で始まる変数が1つだけならそれを使う
    candidates = [v for v in env.values() if v.startswith("sk-")]
    return candidates[0] if len(candidates) == 1 else ""


def find_images(images_dir: Path) -> dict[tuple[str, str], list[Path]]:
    """{(セッション, イベントID): [画像...]}。イベントIDはファイル名の先頭（_ の前）。"""
    groups: dict[tuple[str, str], list[Path]] = {}
    for path in sorted(images_dir.rglob("*")):
        if path.suffix.lower() not in IMAGE_TYPES or not path.is_file():
            continue
        session = path.parent.relative_to(images_dir).as_posix()
        event_id = re.split(r"[_\-\s]", path.stem, maxsplit=1)[0]
        groups.setdefault((session, event_id), []).append(path)
    return groups


def call_openai(api_key: str, model: str, images: list[Path], retries: int = 3) -> dict:
    content: list[dict] = [{"type": "input_text", "text": PROMPT}]
    for image in images[:MAX_IMAGES]:
        data = base64.b64encode(image.read_bytes()).decode("ascii")
        content.append({"type": "input_image",
                        "image_url": f"data:{IMAGE_TYPES[image.suffix.lower()]};base64,{data}"})
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
            detail = exc.read().decode("utf-8", "replace")[:500]
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


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=LENGTH_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="車の写真から ChatGPT で車長を推定する")
    parser.add_argument("--images", type=Path, default=IMAGES_DIR)
    parser.add_argument("--lengths", type=Path, default=LENGTHS_PATH)
    parser.add_argument("--force", action="store_true", help="推定済み(gpt)の行も推定し直す")
    parser.add_argument("--dry-run", action="store_true", help="APIを呼ばず、送る予定の一覧だけ出す")
    parser.add_argument("--limit", type=int, default=0, help="この件数だけ推定する（試し打ち用）")
    parser.add_argument("--raw", type=Path, default=RAW_DIR, help="API の生の応答を保存するフォルダ")
    args = parser.parse_args()

    groups = find_images(args.images)
    if not groups:
        sys.exit(f"画像がありません: {args.images}（<セッション>/<イベントID>.jpg の形で置いてください）")
    rows = read_rows(args.lengths)
    index = {(r.get("session", ""), r.get("event_id", "")): r for r in rows}

    todo = []
    for key, images in sorted(groups.items()):
        row = index.get(key)
        source = (row or {}).get("source", "")
        if source == "manual":
            continue
        if source.startswith("gpt") and not args.force:
            continue
        todo.append((key, images))
    if args.limit:
        todo = todo[:args.limit]
    print(f"推定する対象: {len(todo)} 件（画像のあるイベント {len(groups)} 件）")
    if args.dry_run:
        for (session, event_id), images in todo:
            print(f"  {session} {event_id}: {', '.join(p.name for p in images[:MAX_IMAGES])}")
        return
    if not todo:
        print("推定するものはありません（すべて推定済みか手入力済み）")
        return

    env = load_env(ENV_PATH)
    api_key = find_api_key(env)
    if not api_key:
        names = ", ".join(k for k in env if not k.startswith("_")) or "なし"
        sys.exit(f"APIキーが見つかりません。{ENV_PATH} に OPENAI_API_KEY=sk-... の形で書いてください"
                 f"（.env にある変数名: {names}）")
    model = os.environ.get("OPENAI_MODEL") or env.get("OPENAI_MODEL") or DEFAULT_MODEL
    args.raw.mkdir(parents=True, exist_ok=True)

    failures = 0
    for n, ((session, event_id), images) in enumerate(todo, 1):
        label = f"[{n}/{len(todo)}] {session} {event_id}"
        try:
            response = call_openai(api_key, model, images)
            raw_path = args.raw / f"{session.replace('/', '_')}_{event_id}.json"
            raw_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
            answer = parse_answer(response)
        except Exception as exc:
            failures += 1
            print(f"{label}: 失敗 {exc}")
            continue
        length = answer.get("length_m")
        row = index.get((session, event_id))
        if row is None:
            row = {"session": session, "event_id": event_id}
            rows.append(row)
            index[(session, event_id)] = row
        row.update({
            "car_length_m": f"{length:.3f}" if answer.get("vehicle_visible") and length else "",
            "source": f"gpt:{model}:{answer.get('length_basis', '')}",
            "make": answer.get("make", ""), "model": answer.get("model", ""),
            "body_type": answer.get("body_type", ""),
            "confidence": f"{float(answer.get('confidence') or 0):.2f}",
            "images": ";".join(p.name for p in images[:MAX_IMAGES]),
            "note": answer.get("notes", ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        write_rows(args.lengths, rows)  # 1件ごとに保存（途中で止まっても結果が残る）
        print(f"{label}: {row['make']} {row['model']} 全長 {row['car_length_m'] or '不明'} m "
              f"(確度 {row['confidence']})")
    print(f"完了: 成功 {len(todo) - failures} 件 / 失敗 {failures} 件 → {args.lengths}")


if __name__ == "__main__":
    main()
