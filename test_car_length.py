"""car_length.py（動画の写真1枚から ChatGPT で車長を推定する）のテスト。

OpenAI API と YOLO は呼ばない。API は ``car_length.call_openai`` を差し替え、写真は
あらかじめ置いた画像を使う（既存の写真は切り出し直さない仕様を利用する）。

    portable\\python\\python.exe test_car_length.py
"""
from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

import car_length
from car_length import (
    answer_to_row,
    estimate_car_lengths,
    find_api_settings,
    image_path,
    import_lengths,
    load_car_lengths,
    lookup_car_length,
    target_events,
    write_length_rows,
)


def _response(answer: dict) -> dict:
    return {"output": [{"type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}


ANSWER = {"vehicle_visible": True, "make": "トヨタ", "model": "プリウス", "body_type": "セダン",
          "length_m": 4.6, "length_basis": "catalog", "confidence": 0.9, "notes": ""}


def test_answer_to_row_rejects_invisible_and_out_of_range():
    row = answer_to_row(ANSWER, "gpt-x")
    assert row["car_length_m"] == "4.600" and row["source"] == "gpt:gpt-x:catalog", row
    assert answer_to_row({**ANSWER, "vehicle_visible": False}, "m")["car_length_m"] == ""
    assert answer_to_row({**ANSWER, "length_m": 45.0}, "m")["car_length_m"] == ""
    assert answer_to_row({**ANSWER, "length_m": None}, "m")["car_length_m"] == ""
    print("  OK: 車が見えない・範囲外の全長は採らない")


def test_load_and_lookup():
    with tempfile.TemporaryDirectory() as temp:
        out = Path(temp)
        write_length_rows(out, [
            {"event_id": "001", "car_length_m": "4.2", "source": "manual"},
            {"event_id": "002", "car_length_m": "", "source": "gpt:m:unknown"},
            {"event_id": "L 3", "car_length_m": "12", "source": "gpt:m:estimate"},
        ])
        lengths = load_car_lengths(out)
        assert set(lengths) == {"001", "L 3"}, lengths
        assert lookup_car_length(out, "001").meters == 4.2
        assert lookup_car_length(out, "L_3").meters == 12.0      # ファイル名用のIDでも引ける
        assert lookup_car_length(out, "002") is None
        assert load_car_lengths(out / "none") == {}
    print("  OK: 車長一覧の読み込みとIDの引き当て")


def test_target_events_skip_rejected():
    events = [{"event_id": "1", "review_status": "採用"}, {"event_id": "2", "review_status": "除外"},
              {"event_id": "3", "review_status": "未確認"}, {"event_id": "4"}]
    assert [e["event_id"] for e in target_events(events)] == ["1", "3", "4"]
    assert len(target_events(events, include_rejected=True)) == 4
    print("  OK: 除外の候補は対象にしない")


def test_estimate_uses_existing_image_and_keeps_manual():
    calls: list[Path] = []
    original = car_length.call_openai

    def fake_call(api_key, model, image, retries=3):
        calls.append(image)
        return _response(ANSWER)

    car_length.call_openai = fake_call
    try:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            write_length_rows(out, [{"event_id": "002", "car_length_m": "3.3", "source": "manual"}])
            img = image_path(out, "001")
            img.parent.mkdir(parents=True)
            img.write_bytes(b"\xff\xd8fake")
            events = [{"event_id": "001"}, {"event_id": "002"}]
            summary = estimate_car_lengths(out, events, api_settings=("sk-test", "gpt-x"),
                                           log=lambda _line: None)
            assert calls == [img], calls                     # 手入力の002は送らない
            assert (summary["estimated"], summary["kept"], summary["failed"]) == (1, 1, 0), summary
            lengths = load_car_lengths(out)
            assert lengths["001"].meters == 4.6 and lengths["001"].source == "gpt:gpt-x:catalog"
            assert lengths["002"].meters == 3.3 and lengths["002"].source == "manual"
            assert (out / "car_length" / "gpt_raw" / "001.json").is_file()
            # 2回目は推定済みなので API を呼ばない。--force なら推定し直す（manual は保つ）
            estimate_car_lengths(out, events, api_settings=("sk-test", "gpt-x"), log=lambda _l: None)
            assert len(calls) == 1, calls
    finally:
        car_length.call_openai = original
    print("  OK: 置いてある写真を送り、手入力・推定済みの車長は保つ")


def test_estimate_without_key_does_not_call_api():
    original = car_length.call_openai
    car_length.call_openai = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API を呼んだ"))
    try:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            img = image_path(out, "001")
            img.parent.mkdir(parents=True)
            img.write_bytes(b"\xff\xd8fake")
            summary = estimate_car_lengths(out, [{"event_id": "001"}], api_settings=("", "m"),
                                           log=lambda _line: None)
            assert summary["no_key"] and summary["estimated"] == 0, summary
            assert load_car_lengths(out) == {}
    finally:
        car_length.call_openai = original
    print("  OK: APIキーが無ければ推定せず、その旨を返す")


def test_find_api_settings_reads_env_file():
    import os

    saved = {k: os.environ.pop(k) for k in (*car_length.KEY_NAMES, "OPENAI_MODEL") if k in os.environ}
    try:
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / ".env"
            env.write_text("OPENAI_API_KEY=sk-abc\nOPENAI_MODEL=gpt-y\n", encoding="utf-8")
            assert find_api_settings((Path(temp) / "missing.env", env)) == ("sk-abc", "gpt-y")
            assert find_api_settings((Path(temp) / "missing.env",)) == ("", car_length.DEFAULT_MODEL)
    finally:
        os.environ.update(saved)
    print("  OK: .env から APIキーとモデルを読む")


def test_import_lengths_from_conversion_tool():
    with tempfile.TemporaryDirectory() as temp:
        src = Path(temp) / "output"
        (src / "w5o" / "ID1").mkdir(parents=True)
        write_length_rows(src / "w5o" / "ID1", [{"event_id": "002", "car_length_m": "3.0",
                                                  "source": "manual"}])
        table = Path(temp) / "car_lengths.csv"
        with table.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["session", "event_id", "car_length_m", "source", "make", "model"])
            writer.writerow(["w5o/ID1", "001", "4.100", "gpt:gpt-5.5:catalog", "日産", "ノート"])
            writer.writerow(["w5o/ID1", "002", "4.800", "gpt:gpt-5.5:catalog", "", ""])
            writer.writerow(["w5o/ID1", "003", "", "", "", ""])
            writer.writerow(["w5o/ID9", "001", "4.0", "manual", "", ""])   # フォルダが無い
        counts = import_lengths(table, src)
        assert counts == {"w5o/ID1": 1}, counts
        lengths = load_car_lengths(src / "w5o" / "ID1")
        assert lengths["001"].meters == 4.1 and lengths["001"].make == "日産", lengths
        assert lengths["002"].meters == 3.0, "取り込みで手入力の車長を上書きした"
    print("  OK: 車長変換ツールの一覧を取り込む（既存の車長は上書きしない）")


def test_conversion_tool_matches_main_formulas():
    """車長変換ツールが解析xlsxの行から計算し直す値が、本体の analyze_edit_points と一致する。

    本体の新しい解析xlsx（車長が無く相対速度が空欄）を入れても、相対速度の行を
    二重に作らず、空欄だった行へ書く。
    """
    import sys

    from edit_csv import analyze_edit_points, read_edit_points, write_edit_analysis_xlsx

    sys.path.insert(0, str(Path(__file__).resolve().parent / "AutoAnalysis"))
    from convert_relative_speed import Length, convert_analysis, read_analysis
    from xlsx_cells import read_cells

    with tempfile.TemporaryDirectory() as temp:
        edit_dir = Path(temp) / "out" / "lidar_edit"
        edit_dir.mkdir(parents=True)
        edit = edit_dir / "overtaking_001_edit.csv"
        rows = ["unixtime,X,Y,Z,distance(m),speed(km/h)"]
        track = [(0.0, -0.5, -8.0), (0.5, -1.0, -7.0), (1.0, -1.5, -5.0), (1.5, -2.0, -2.0),
                 (2.0, -2.0, 0.0), (2.5, -2.5, 2.0), (3.0, -1.0, 3.5), (3.5, -1.0, 6.0)]
        prev = None
        for t, x, y in track:
            dist = (x * x + y * y) ** 0.5
            speed = "" if prev is None else \
                f"{((x - prev[1]) ** 2 + (y - prev[2]) ** 2) ** 0.5 / (t - prev[0]) * 3.6:.4f}"
            rows.append(f"{t},{x},{y},0,{dist:.4f},{speed}")
            prev = (t, x, y)
        edit.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
        xlsx = write_edit_analysis_xlsx(edit)                # 車長なし（相対速度は空欄）
        main = analyze_edit_points(read_edit_points(edit), car_length=4.5)

        metrics = read_analysis(xlsx)
        assert abs(metrics.recomputed["passing_time"] - main["passing_time"]) < 1e-6, metrics.recomputed
        assert abs(metrics.recomputed["ttc"] - main["ttc"]) < 1e-6, metrics.recomputed
        dest = Path(temp) / "converted.xlsx"
        new = convert_analysis(metrics, Length(4.5, "manual"), dest)
        assert abs(new["relative_speed"] - main["relative_speed"]) < 1e-6, (new, main)
        cells = read_cells(dest)
        labels = [v for (r, c), v in cells.items() if c == 8 and isinstance(v, str)]
        assert labels.count("相対速度") == 1, labels
        row = next(r for (r, c), v in cells.items() if c == 8 and v == "相対速度")
        assert abs(cells[(row, 9)] - main["relative_speed"]) < 1e-6, cells[(row, 9)]
    print("  OK: 車長変換ツールの再計算が本体の式と一致する")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for t in tests:
        t()
    print("[test] 全テスト成功")
