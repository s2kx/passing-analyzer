#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_pipeline.py の軽量テスト。

YOLO も実データも使わず、合成した最小PCAPと一時フォルダで段階4〜8を検証する。
段階1（YOLO検出）は ``--skip-detect`` 相当のパスで通す。

    .venv\\Scripts\\python.exe test_pipeline.py
"""
from __future__ import annotations

import csv
import json
import math
import re
import struct
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

import run_pipeline
from final_export import DANGER_BY_LEVEL, generate_final_workbook
from run_pipeline import (
    PipelineConfig,
    PipelineError,
    build_config,
    discover_pcap,
    discover_videos,
    load_autotracker,
    parse_args,
    run_pipeline as execute_pipeline,
)


# 追い越し車の軌跡（1秒刻み）。web_gui.py --check のフェーズ判定テストと同じ形。
#   フェーズ2 = 4行目(t=3), フェーズ3 = 5行目(t=4, |Y|<3), フェーズ4 = 6行目(t=5)
TRAJECTORY = [
    (0.0, -0.5, 8.0),
    (1.0, -1.0, 7.0),
    (2.0, -1.5, 6.0),
    (3.0, -2.0, 5.0),
    (4.0, -2.5, 2.5),
    (5.0, -1.0, 3.2),
]
PCAP_START_UNIX = 1782000000.0
TARGET_LASER = 1                    # VLP-16 チャンネル1 = 垂直角 +1度
VERTICAL_DEG = 1.0
DUMMY_AZIMUTH = 100                 # 1.00度。フレーム先頭の空パケット


# ── 合成PCAP ─────────────────────────────────────────────────────────────────

def _velodyne_packet(azimuth_cd: int, distance_raw: int) -> bytes:
    """1206-byte VLP-16データパケット。``distance_raw`` が0なら点を持たない。"""
    payload = bytearray(1206)
    for block in range(12):
        struct.pack_into("<HH", payload, block * 100, 0xEEFF, azimuth_cd)
        if distance_raw:
            struct.pack_into("<H", payload, block * 100 + 4 + TARGET_LASER * 3, distance_raw)
            payload[block * 100 + 6 + TARGET_LASER * 3] = 90
    payload[1204], payload[1205] = 0x37, 0x22   # strongest return / VLP-16
    return bytes(payload)


def write_sample_pcap(path: Path, trajectory=TRAJECTORY) -> None:
    """1フレームに1点だけを持つ最小のclassic PCAPを書き出す。

    フレーム境界は方位角の巻き戻しで決まるため、各フレームを
    「方位角100の空パケット → 目標点のパケット（方位角 > 18100）」の2つで構成する。
    """
    empty = _velodyne_packet(DUMMY_AZIMUTH, 0)
    with path.open("wb") as stream:
        stream.write(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1))
        for seconds, x, y in trajectory:
            azimuth_cd = int(round((math.degrees(math.atan2(x, y)) % 360.0) * 100)) % 36000
            assert azimuth_cd > DUMMY_AZIMUTH + 18000, azimuth_cd
            distance_raw = int(round(
                math.hypot(x, y) / math.cos(math.radians(VERTICAL_DEG)) / 0.002
            ))
            for index, packet in enumerate((empty, _velodyne_packet(azimuth_cd, distance_raw))):
                capture = PCAP_START_UNIX + seconds + index * 0.01
                stream.write(struct.pack(
                    "<IIII", int(capture), round(capture % 1 * 1_000_000),
                    len(packet), len(packet),
                ))
                stream.write(packet)


DUMMY_MP4 = struct.pack(">I", 32) + b"ftyp" + b"\x00" * 24


def make_session(root: Path, videos=("GX010001.MP4",), metadata: dict | None = None) -> Path:
    """`<セッション>/GOPRO/*.MP4` と `<セッション>/LiDAR/*.pcap` を持つ撮影セッションを作る。"""
    session = root / "260701" / "ID1"
    (session / "GOPRO").mkdir(parents=True)
    (session / "LiDAR").mkdir(parents=True)
    for name in videos:
        (session / "GOPRO" / name).write_bytes(DUMMY_MP4)
    write_sample_pcap(session / "LiDAR" / "2026-07-01-10-09-23_Velodyne-VLP-16-Data.pcap")
    if metadata is not None:
        (session / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
    return session


EVENTS_HEADER = (
    "event_id,class,detector_class,side,t_start_s,peak_t_s,t_end_s,duration_s,"
    "kei_plate_score,kei_plate_hits,source_video,local_t_start_s,local_t_end_s\n"
)


def write_events_csv(out_dir: Path, rows: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "overtaking_events.csv"
    path.write_text(EVENTS_HEADER + rows, encoding="utf-8-sig")
    return path


def sheet_cell(sheet_xml: str, ref: str) -> str:
    """最終Excelの1セル（inlineStr）のテキストを取り出す。"""
    match = re.search(rf'<c r="{ref}"[^>]*>(.*?)</c>', sheet_xml)
    if not match:
        return ""
    return "".join(re.findall(r"<t>(.*?)</t>", match.group(1)))


def dummy_tracker(calls: list | None = None):
    """候補一覧を返さない旧契約のダミー ``track_overtaking``。

    ``expected_pass_time`` / ``exclude_track_keys`` を受け取らない古いプラグインでも
    パイプラインが動くこと（後方互換）を確かめるために使う。
    """

    def track_overtaking(pcap, first_frame, last_frame, side="right"):
        if calls is not None:
            calls.append((first_frame, last_frame, side))
        return {
            "ok": True,
            "frames": [
                {"index": index,
                 "bounds": {"min_x": -20.0, "max_x": 20.0, "min_y": -20.0, "max_y": 20.0}}
                for index in range(first_frame, last_frame + 1)
            ],
            "confidence": 0.9,
            "warnings": [],
        }

    return track_overtaking


def scripted_tracker(vehicles: dict[str, list[dict]], calls: list | None = None,
                     confidence: float = 0.9):
    """新契約のダミー ``track_overtaking``。

    ``vehicles`` は ``track_key -> [{"pass_time", "lc_estimate", "score"}]`` ではなく
    「実在する車の一覧」を表す ``[{"track_key", "pass_time", "lc_estimate", "score"}]``。
    追跡窓（フレーム番号）に通過時刻が入る車だけを候補として返し、``select_overtaking``
    と同じ規則（期待通過時刻に近い順、除外キーを飛ばす）で 1 本を選ぶ。
    """
    cars = vehicles if isinstance(vehicles, list) else list(vehicles.values())

    def track_overtaking(pcap, first_frame, last_frame, side="right",
                         expected_pass_time=None, exclude_track_keys=None):
        if calls is not None:
            calls.append({"first": first_frame, "last": last_frame, "side": side,
                          "expected": expected_pass_time,
                          "exclude": list(exclude_track_keys or [])})
        low = pcap.frames[first_frame].timestamp
        high = pcap.frames[last_frame].timestamp
        inside = [car for car in cars if low <= car["pass_time"] <= high]
        if expected_pass_time is None:
            inside.sort(key=lambda car: -car["score"])
        else:
            inside.sort(key=lambda car: (abs(car["pass_time"] - expected_pass_time),
                                         -car["score"]))
        candidates = [{key: value for key, value in car.items() if key != "confidence"}
                      for car in inside]
        excluded = set(exclude_track_keys or ())
        chosen = next((car for car in inside if car["track_key"] not in excluded), None)
        if chosen is None:
            return {"ok": False, "frames": [], "confidence": 0.0, "warnings": [],
                    "pass_time": None, "track_key": None, "candidates": candidates}
        # 車ごとに違う位置の矩形にする（同じ車かどうかを軌跡で判定する側のため）。
        # 合成PCAPの点は Y=2.5〜8m にあるので、どの車の矩形にも点は入る。
        center = chosen["pass_time"] * 3.0
        return {
            "ok": True,
            "frames": [
                {"index": index,
                 "bounds": {"min_x": -20.0, "max_x": 20.0,
                            "min_y": -20.0 + center, "max_y": 20.0 + center}}
                for index in range(first_frame, last_frame + 1)
            ],
            "confidence": chosen.get("confidence", confidence),
            "warnings": [], "pass_time": chosen["pass_time"],
            "track_key": chosen["track_key"], "candidates": candidates,
        }

    return track_overtaking


def car(track_key: str, pass_time: float, lc_estimate: float = 1.2,
        score: float = 0.9, confidence: float = 0.9) -> dict:
    """``scripted_tracker`` へ渡す 1 台分の車の定義。"""
    return {"track_key": track_key, "pass_time": pass_time, "score": score,
            "lc_estimate": lc_estimate, "median_x": lc_estimate,
            "confidence": confidence}


def read_reviewed(out_dir: Path) -> list[dict]:
    """レビュー結果CSVを読む。"""
    with (out_dir / "reviewed_overtaking_events.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        return list(csv.DictReader(stream))


def event_row(event_id: str, start: float, peak: float, end: float,
              side: str = "right") -> str:
    """候補CSVの1行を作る。"""
    return (f"{event_id},普通,car,{side},{start},{peak},{end},{end - start},"
            f"0.1,0,GX010001.MP4,{start},{end}\n")


# ── テスト ────────────────────────────────────────────────────────────────────

def test_session_discovery_and_metadata():
    """セッション規約の自動発見と、CLI引数 > metadata.json の優先順を確認する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(
            Path(temp_value),
            videos=("GX020001.MP4", "GX010001.MP4"),
            metadata={"subject": "ID9", "date": "2026-07-01", "view": "front",
                      "organize_id": 7},
        )
        videos = discover_videos(session)
        assert [v.name for v in videos] == ["GX010001.MP4", "GX020001.MP4"], videos
        assert len(discover_pcap(session)) == 1

        # metadata.json だけの場合はその値を使う
        config = build_config(parse_args([str(session)]))
        assert config.view == "front" and config.view_source == "metadata", config
        assert config.subject_id == "ID9"
        assert config.organize_id == "7" and config.collection_date == "2026-07-01"
        assert config.out_dir == session / "out_auto", config.out_dir

        # CLI引数が metadata.json より優先される
        config = build_config(parse_args([
            str(session), "--view", "rear", "--subject", "ID1", "--organize-id", "3",
            "--out", str(session / "custom"),
        ]))
        assert config.view == "rear" and config.view_source == "cli", config
        assert config.subject_id == "ID1"
        assert config.organize_id == "3" and config.out_dir == session / "custom"
        print("  OK: セッション自動発見と metadata.json / CLI の優先順")


def test_session_without_videos_is_rejected():
    """動画が無いセッションは、明示指定が無ければエラーにする。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = Path(temp_value) / "260701" / "ID1"
        (session / "LiDAR").mkdir(parents=True)
        write_sample_pcap(session / "LiDAR" / "sample.pcap")
        try:
            build_config(parse_args([str(session)]))
        except PipelineError as exc:
            assert "GoPro動画が見つかりません" in str(exc), exc
        else:
            raise AssertionError("動画が無くてもエラーにならなかった")
        print("  OK: 動画が無いセッションを検出する")


def test_pipeline_end_to_end():
    """段階4〜8を通し、レビューCSV・点群CSV・edit・最終Excel・レポートを検証する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")

        calls: list = []
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=dummy_tracker(calls))
        data = report.to_dict()
        status = {stage["name"]: stage["status"] for stage in data["stages"]}

        # 段階1・2はスキップ、段階3はGPMFの無いダミー動画なので失敗して0秒へフォールバック
        assert status["detect"] == "skipped", status
        assert status["gps"] == "skipped", status
        assert status["sync"] == "failed", status
        assert data["info"]["時刻オフセット(秒)"] == 0.0, data["info"]
        sync = json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))
        assert sync["source"] == "fallback" and sync["pcap_start_unix"] == PCAP_START_UNIX

        # 段階6: 点群が取れた候補を採用してレビューCSVを書く
        assert status["review"] == "ok", status
        with (out_dir / "reviewed_overtaking_events.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            reviewed = list(csv.DictReader(stream))
        assert len(reviewed) == 1 and reviewed[0]["review_status"] == "採用", reviewed
        assert reviewed[0]["danger_level"] == "0", reviewed
        assert reviewed[0]["veloview_t_start"] == "00:00:00.500", reviewed

        # 段階4: 撮影方向別マージンを足したフレーム範囲でダミー追跡を呼び、点群CSVを書く
        # 通過側は日本では常に右なので、候補CSVの side 列によらず "right" を渡す
        assert status["autotrack"] == "ok", status
        assert calls == [(0, 5, "right")], calls
        lidar_csv = out_dir / "lidar" / "overtaking_001.csv"
        assert lidar_csv.is_file(), lidar_csv
        with lidar_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            lidar_rows = list(csv.reader(stream))
        assert lidar_rows[0][8:10] == ["Points:0", "Points:1"], lidar_rows[0]
        assert len(lidar_rows) > 1, lidar_rows

        # 段階6: 最近接点の軌跡と解析Excel
        assert status["edit"] == "ok", status
        edit_path = out_dir / "lidar_edit" / "overtaking_001_edit.csv"
        assert edit_path.is_file(), edit_path
        with edit_path.open("r", encoding="utf-8-sig", newline="") as stream:
            edit_rows = list(csv.reader(stream))
        assert len(edit_rows) == 1 + len(TRAJECTORY), edit_rows
        for row, (_, x, y) in zip(edit_rows[1:], TRAJECTORY):
            assert abs(float(row[1]) - x) < 0.02, row
            assert abs(float(row[2]) - y) < 0.02, row
        assert (out_dir / "lidar_edit" / "overtaking_001_edit_analysis.xlsx").is_file()

        # 段階7: 最終Excel。危険感は未設定なので空欄。
        assert status["final"] == "ok", status
        final_path = out_dir / "final" / "out_auto_final.xlsx"
        assert final_path.is_file(), final_path
        with ZipFile(final_path) as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "整理ID" in sheet_xml
        assert sheet_cell(sheet_xml, "C2") == "ID1", "被験者IDがセッションフォルダ名から入らなかった"
        assert sheet_cell(sheet_xml, "G2") == "", "危険感が空欄になっていない"

        # 段階8: レポート
        assert (out_dir / "pipeline_report.json").is_file()
        text = (out_dir / "pipeline_report.txt").read_text(encoding="utf-8")
        assert "自動パイプライン レポート" in text
        assert "[人手が必要な項目]" in text
        assert any("危険感" in item for item in data["manual_actions"]), data["manual_actions"]
        review = next(s for s in data["stages"] if s["name"] == "review")
        assert (review["accepted"], review["rejected"]) == (1, 0), review
        print("  OK: 段階4〜8をend-to-endで実行して最終Excelまで生成")


def test_offset_option_and_frame_range():
    """--offset 指定時はGPS同期を省き、その値でフレーム範囲を決める。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,left,2.0,2.5,3.0,1.0,0.1,0,GX010001.MP4,2.0,3.0\n")

        calls: list = []
        config = build_config(parse_args([
            str(session), "--skip-detect", "--skip-gps", "--offset", "1.0",
            "--track-margin", "0.5",
        ]))
        report = execute_pipeline(config, tracker=dummy_tracker(calls))
        status = {stage["name"]: stage["status"] for stage in report.to_dict()["stages"]}
        assert status["sync"] == "ok", status
        # 2.0+1.0-0.5=2.5秒 → フレーム2または3、3.0+1.0+0.5=4.5秒 → フレーム4または5
        # 候補CSVの side は "left" だが、物理的な通過側は常に右なので "right" を渡す
        assert calls == [(2, 4, "right")] or calls == [(3, 5, "right")], calls
        sync = json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))
        assert sync["offset"] == 1.0 and sync["source"] == "manual", sync
        print("  OK: --offset の手動指定とフレーム範囲の変換")


def test_missing_autotracker_is_reported_as_manual_work():
    """lidar_autotrack が無い場合は手動選択が必要な候補としてレポートへ残す。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")

        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        saved = sys.modules.get("lidar_autotrack", "")
        sys.modules["lidar_autotrack"] = None    # import 時に ImportError を起こす
        try:
            assert load_autotracker()[0] is None
            report = execute_pipeline(config)
        finally:
            if saved == "":
                sys.modules.pop("lidar_autotrack", None)
            else:
                sys.modules["lidar_autotrack"] = saved

        data = report.to_dict()
        status = {stage["name"]: stage["status"] for stage in data["stages"]}
        assert status["autotrack"] == "skipped", status
        assert status["edit"] == "skipped", status
        assert not (out_dir / "lidar").is_dir()
        assert any("手動選択" in item for item in data["manual_actions"]), data["manual_actions"]
        # 点群が無くても最終Excelとレポートは出す（人手作業の一覧として使う）
        assert (out_dir / "final" / "out_auto_final.xlsx").is_file()
        assert (out_dir / "pipeline_report.txt").is_file()
        print("  OK: lidar_autotrack 未実装時は手動作業としてレポートする")


def test_autotrack_failure_keeps_pipeline_running():
    """プラグインが例外を投げても、他段階を止めずレポートへ残す。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")

        def broken_tracker(pcap, first, last, side="right"):
            raise RuntimeError("追跡できません")

        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=broken_tracker)
        data = report.to_dict()
        status = {stage["name"]: stage["status"] for stage in data["stages"]}
        assert status["autotrack"] == "failed", status
        assert status["report"] == "ok", status
        assert any("追跡できません" in item for item in data["warnings"]), data["warnings"]
        assert any("手動選択" in item for item in data["manual_actions"]), data["manual_actions"]
        print("  OK: 点群追跡の失敗でパイプラインを止めない")


def test_track_window_is_asymmetric_toward_approach():
    """追跡窓は車が近づいてくる側（front なら開始側）へ長く取る。"""
    event = {"t_start_s": "20.0", "t_end_s": "24.0"}
    front = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                           pcap_path=Path("."), view="front")
    rear = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                          pcap_path=Path("."), view="rear")
    fixed = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                           pcap_path=Path("."), view="front", track_margin=5.0)
    assert front.track_margins() == (8.0, 3.0), front.track_margins()
    assert rear.track_margins() == (3.0, 8.0), rear.track_margins()
    assert run_pipeline.event_window(front, event, 1.0) == (13.0, 28.0)
    assert run_pipeline.event_window(rear, event, 1.0) == (18.0, 33.0)
    # --track-margin を明示した場合は従来どおり前後同じ
    assert run_pipeline.event_window(fixed, event, 1.0) == (16.0, 30.0)
    print("  OK: front は開始側 -8秒 / 終了側 +3秒、rear は逆の非対称窓")


def test_events_are_assigned_to_distinct_vehicles():
    """窓が重なる2候補に、期待通過時刻で別々の車を割り当てる。

    2 台とも両方の窓に入るので、スコア最大で選ぶと同じ車が2回出てしまう。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))

        calls: list = []
        cars = [car("f10x+1.8", 1.0, lc_estimate=1.1, score=0.95),
                car("f40x+2.0", 4.0, lc_estimate=1.4, score=0.50)]
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars, calls))
        data = report.to_dict()

        track = next(s for s in data["stages"] if s["name"] == "autotrack")
        assert track["assigned"] == {"001": "f10x+1.8", "002": "f40x+2.0"}, track["assigned"]
        assert len(set(track["assigned"].values())) == 2, track["assigned"]
        # 期待通過時刻（動画の peak_t_s + オフセット）をプラグインへ渡している
        assert [round(c["expected"], 1) for c in calls[:2]] == [1.0, 4.0], calls
        assert sorted(p.name for p in (out_dir / "lidar").glob("overtaking_*.csv")) == [
            "overtaking_001.csv", "overtaking_002.csv"
        ]
        reviewed = read_reviewed(out_dir)
        assert [row["review_status"] for row in reviewed] == ["採用", "採用"], reviewed
        print("  OK: 窓が重なる2候補へ別々の車を割り当て（重複なし）")


def test_second_vehicle_of_a_convoy_is_picked_up():
    """同じ車を取り合ったら期待通過時刻が近い方が勝ち、負けた方は2台目を拾う。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        # 2候補とも1台目(通過2.0s)に近いが、002 は 2台目(通過4.0s)を取るべき
        write_events_csv(out_dir, event_row("001", 1.5, 2.2, 2.8)
                         + event_row("002", 2.0, 2.6, 3.2))

        calls: list = []
        cars = [car("f20x+1.8", 2.0, lc_estimate=1.0, score=0.95),
                car("f40x+2.2", 4.0, lc_estimate=1.5, score=0.60)]
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars, calls))
        data = report.to_dict()

        track = next(s for s in data["stages"] if s["name"] == "autotrack")
        assert track["assigned"] == {"001": "f20x+1.8", "002": "f40x+2.2"}, track["assigned"]
        # 2台目を拾うため、1台目を除外して選び直している
        retried = [c for c in calls if c["exclude"]]
        assert retried and retried[-1]["exclude"] == ["f20x+1.8"], calls
        assert len(read_reviewed(out_dir)) == 2
        print("  OK: 車列で1台目を取り合った候補が2台目を拾う")


def test_duplicate_vehicle_output_is_blocked():
    """同じ車を2候補へ出力しようとしたら、2件目を除外して重複を作らない。

    候補一覧を返さない旧プラグインは、どちらの候補でも同じ矩形列を返す。
    ``track_key`` が無くても軌跡の重なりで同一車両と分かるので出力は1件になる。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))

        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=dummy_tracker())
        data = report.to_dict()

        exported = sorted(p.name for p in (out_dir / "lidar").glob("overtaking_*.csv"))
        assert exported == ["overtaking_001.csv"], exported
        review = next(s for s in data["stages"] if s["name"] == "review")
        assert (review["accepted"], review["rejected"]) == (1, 1), review
        assert "同じ車両" in review["reasons"]["002"], review["reasons"]
        reviewed = read_reviewed(out_dir)
        assert [row["event_id"] for row in reviewed] == ["001"], reviewed
        # 除外した候補は最終Excelにも出ない（2行目までで終わる）
        with ZipFile(out_dir / "final" / "out_auto_final.xlsx") as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert sheet_cell(sheet_xml, "C2") == "ID1", sheet_xml[:200]
        assert sheet_cell(sheet_xml, "C3") == "", "除外した候補が最終Excelに出ている"
        print("  OK: 同一車両の重複出力を1件へ抑える")


def test_auto_review_classifies_by_lidar_result():
    """自動レビューが LiDAR の結果で採用・未確認・除外を振り分ける。"""
    config = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                            pcap_path=Path("."), view="front")
    events = [{"event_id": str(index)} for index in range(1, 6)]
    tracking = {
        "1": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2},
        "2": {"ok": False, "reason": "追跡窓の中に対応する追い越し車両が見つかりませんでした"},
        "3": {"ok": True, "confidence": 0.9, "lc_estimate": 4.4},   # 隣車線
        "4": {"ok": True, "confidence": 0.3, "lc_estimate": 1.0},   # 確信度が低い
        "5": {"ok": True, "confidence": 0.9, "lc_estimate": 1.0},   # 実測LCで隣車線と判明
    }
    analyses = {
        "1": {"metrics": {"lc": 1.35}},
        "5": {"metrics": {"lc": 3.9}},
    }
    reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses)
    assert [row["review_status"] for row in reviewed] == [
        "採用", "除外", "除外", "未確認", "除外"
    ], reviewed
    assert "見つかりません" in reviewed[1]["review_reason"], reviewed[1]
    assert "隣車線" in reviewed[2]["review_reason"], reviewed[2]
    assert "確信度" in reviewed[3]["review_reason"], reviewed[3]
    # 実測LC（edit解析）は追跡時の目安より優先する
    assert "3.90m" in reviewed[4]["review_reason"], reviewed[4]
    assert all(row["danger_level"] == "0" for row in reviewed), reviewed
    print("  OK: LiDAR結果で採用・未確認・除外を判定（実測LC優先）")


def test_pass_anchor_rounds_a_late_peak_toward_the_visible_edge():
    """トラック開始から離れすぎた bbox 面積のピークは期待通過時刻の基準にしない。

    front 撮影では追い越し車は自転車を抜いた直後に画角へ入るので、開始時刻が
    横に並んだ時刻に最も近い。検出が別の車へ乗り移ると peak_t_s が開始の十数秒後
    まで流れ、そのままだと期待通過時刻が数秒ずれる。
    """
    late = {"event_id": "001", "t_start_s": 100.0, "peak_t_s": 114.0, "t_end_s": 116.0}
    normal = {"event_id": "002", "t_start_s": 200.0, "peak_t_s": 200.7, "t_end_s": 208.0}

    # front: 開始から PEAK_ANCHOR_LIMIT 秒より後の peak は開始側へ丸める
    assert run_pipeline.pass_anchor(late, "front") == 103.0
    assert run_pipeline.pass_anchor(normal, "front") == 200.7
    # rear: 抜かれる直前まで映るので終了時刻側が基準
    assert run_pipeline.pass_anchor(late, "rear") == 114.0
    assert run_pipeline.pass_anchor(normal, "rear") == 205.0
    # 撮影方向が分からなければ従来どおり peak をそのまま使う
    assert run_pipeline.pass_anchor(late, "") == 114.0

    expected = run_pipeline.expected_pass_times([late, normal], offset=-10.0, view="front")
    assert expected == {"001": 93.0, "002": 190.7}, expected
    print("  OK: 遅すぎる bbox ピークを期待通過時刻の基準から外す")


def test_unassigned_vehicle_is_reported_as_a_detection_gap():
    """どの候補にも割り当たらない車は、動画検出の取りこぼしとしてレポートへ挙げる。

    実データ(260701/ID1)で未検出だった車列の2台目は、LiDAR には居るのに動画側の
    候補が無い状態だった。採否は決められないので人手項目として残す。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))

        cars = [car("f10x+1.8", 1.0), car("f40x+2.0", 4.0),
                car("f25x+1.9", 2.5, lc_estimate=1.0, score=0.95)]   # 候補の無い車
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        data = report.to_dict()

        assert data["info"]["割り当て先の無い車(動画検出の取りこぼし候補)"] == 1, data["info"]
        assert any("取りこぼし" in item and "2.5s" in item for item in data["manual_actions"]), \
            data["manual_actions"]
        # 隣車線相当・評価値の低い車は挙げない（誤検出が多いため）
        quiet = run_pipeline.unassigned_vehicles(
            {"001": [car("f60x+4.4", 6.0, lc_estimate=4.4, score=0.95),
                     car("f80x+1.5", 8.0, lc_estimate=1.5, score=0.40)]}, {},
        )
        assert quiet == [], quiet
        print("  OK: 割り当て先の無い車を検出の取りこぼし候補として報告")


def test_lidar_only_vehicle_is_issued_as_an_unreviewed_row():
    """割り当て先の無い車を L### の「未確認」行として起票する。

    動画検出が取りこぼした車は、点群CSV・edit CSV まで作ってレビューCSVへ
    ``review_status="未確認"`` で残す。列は動画由来の候補行と同じ構成にそろえ、
    最終Excel（採用行のみ）へは出さない。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))

        cars = [car("f10x+1.8", 1.0), car("f40x+2.0", 4.0),
                car("f25x+1.9", 2.5, lc_estimate=1.0, score=0.95)]   # 候補の無い車
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        assert config.lidar_extra is True, config
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        data = report.to_dict()

        # 点群CSVとedit CSVが L001 として残る
        assert (out_dir / "lidar" / "overtaking_L001.csv").is_file()
        assert (out_dir / "lidar_edit" / "overtaking_L001_edit.csv").is_file()
        track = next(s for s in data["stages"] if s["name"] == "autotrack")
        assert len(track["lidar_extra"]) == 1, track
        assert "候補 2 件のうち 2 件" in track["message"], track["message"]

        # レビューCSVでは「未確認」。列は動画由来の候補行と同じ構成のまま
        reviewed = read_reviewed(out_dir)
        assert [row["event_id"] for row in reviewed] == ["001", "002", "L001"], reviewed
        extra = reviewed[-1]
        assert extra["review_status"] == "未確認", extra
        assert "対応する検出候補がありません" in extra["review_reason"], extra
        assert set(extra) == set(reviewed[0]), (set(extra) ^ set(reviewed[0]))
        assert "lidar_extra" not in extra, extra
        # 時刻は動画側の秒で入り、veloview 表示（pcap時刻）も埋まる
        assert 2.0 <= float(extra["peak_t_s"]) <= 3.0, extra
        assert extra["veloview_peak_t"] and extra["class"] == "", extra

        review = next(s for s in data["stages"] if s["name"] == "review")
        assert (review["accepted"], review["unreviewed"]) == (2, 1), review
        # 最終Excelは採用のみ（2行）。3行目は空
        with ZipFile(out_dir / "final" / "out_auto_final.xlsx") as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert sheet_cell(sheet_xml, "C3") == "ID1", sheet_xml[:200]
        assert sheet_cell(sheet_xml, "C4") == "", "未確認の行が最終Excelに出ている"
        assert any("L001" in item for item in data["manual_actions"]), data["manual_actions"]
        print("  OK: 割り当て先の無い車を L001 の「未確認」行として起票")


def test_no_lidar_extra_skips_the_issue_and_cleans_up():
    """--no-lidar-extra なら起票せず、前回の L### 出力も残さない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))
        cars = [car("f10x+1.8", 1.0), car("f40x+2.0", 4.0),
                car("f25x+1.9", 2.5, lc_estimate=1.0, score=0.95)]

        # まず起票する実行で L001 を作る
        execute_pipeline(build_config(parse_args([str(session), "--skip-detect", "--skip-gps"])),
                         tracker=scripted_tracker(cars))
        assert (out_dir / "lidar" / "overtaking_L001.csv").is_file()

        # 同じ出力先へ --no-lidar-extra で再実行すると、起票も前回の出力も消える
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps",
                                          "--no-lidar-extra"]))
        assert config.lidar_extra is False, config
        data = execute_pipeline(config, tracker=scripted_tracker(cars)).to_dict()
        assert not (out_dir / "lidar" / "overtaking_L001.csv").exists()
        assert not (out_dir / "lidar_edit" / "overtaking_L001_edit.csv").exists()
        assert [row["event_id"] for row in read_reviewed(out_dir)] == ["001", "002"]
        review = next(s for s in data["stages"] if s["name"] == "review")
        assert review["unreviewed"] == 0, review
        # 起票しなくても取りこぼしの報告そのものは残る
        assert data["info"]["割り当て先の無い車(動画検出の取りこぼし候補)"] == 1, data["info"]
        print("  OK: --no-lidar-extra で起票を止め、前回のL###も削除する")


def test_measured_lc_overrides_the_tracking_estimate():
    """edit解析で LC が求まれば、追跡時の目安ではなく実測値でしきい値判定する。

    合成PCAPの軌跡から求まる実測 LC は 1.0m。追跡側が隣車線相当（4.2m）と
    見積もっても、実測が範囲内なら採用する（逆も同じ）。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        cars = [car("f10x+4.2", 1.0, lc_estimate=4.2)]
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        review = next(s for s in report.to_dict()["stages"] if s["name"] == "review")
        assert (review["accepted"], review["rejected"]) == (1, 0), review
        analysis = run_pipeline.run_edit_csv(config, report)["001"]
        measured = analysis["metrics"]["lc"]
        assert 0.0 < measured < run_pipeline.MAX_LANE_CLEARANCE, measured
        print("  OK: 実測LCが求まれば追跡時の目安より優先する")


def test_low_confidence_track_is_flagged_for_review():
    """確信度が低い候補は「未確認」にして人手項目へ回す（最終Excelには出さない）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        cars = [car("f10x+1.8", 1.0, lc_estimate=1.1, confidence=0.35)]
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        data = report.to_dict()

        review = next(s for s in data["stages"] if s["name"] == "review")
        assert (review["accepted"], review["unreviewed"]) == (0, 1), review
        assert any("確信度" in item for item in data["manual_actions"]), data["manual_actions"]
        reviewed = read_reviewed(out_dir)
        assert [row["review_status"] for row in reviewed] == ["未確認"], reviewed
        print("  OK: 確信度 < 0.5 は未確認としてレポートの人手項目へ")


def test_danger_level_is_blank_unless_reviewed():
    """危険感は未設定("0")なら空欄、1〜3なら従来どおりのラベルにする。"""
    assert DANGER_BY_LEVEL["0"] == "", DANGER_BY_LEVEL
    assert DANGER_BY_LEVEL["1"] == "やや危険", DANGER_BY_LEVEL
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "260701_1"
        edit_dir = out_dir / "lidar_edit"
        edit_dir.mkdir(parents=True)
        (out_dir / "reviewed_overtaking_events.csv").write_text(
            "event_id,class,source_video,local_t_start_s,t_start_s,review_status,danger_level\n"
            "001,普通,GX010001.MP4,10.0,10.0,採用,0\n"
            "002,普通,GX010001.MP4,20.0,20.0,採用,2\n",
            encoding="utf-8-sig",
        )
        for event_id in ("001", "002"):
            (edit_dir / f"overtaking_{event_id}_edit.csv").write_text(
                "unixtime,X,Y,Z,distance(m),speed(km/h)\n"
                "0,-0.5,8,0,8.0,\n1,-1.0,7,0,7.1,12\n2,-1.5,6,0,6.2,8\n"
                "3,-2.0,5,0,5.4,7\n4,-2.5,2.5,0,2.8,14\n5,-1.0,3.2,0,3.35,16\n",
                encoding="utf-8-sig",
            )
        result = generate_final_workbook(out_dir, organize_id="1", subject_id="ID1")
        with ZipFile(Path(result["path"])) as book:
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert result["rows"] == 2, result
        assert sheet_cell(sheet_xml, "G1") == "危険感", sheet_cell(sheet_xml, "G1")
        assert sheet_cell(sheet_xml, "G2") == "", "危険感0が空欄になっていない"
        assert sheet_cell(sheet_xml, "G3") == "危険", "危険感2のラベルが失われている"
        print("  OK: 危険感は未設定なら空欄、明示した場合だけラベルを出す")


def _stub_view_detector(view, confidence=0.9, calls=None):
    """段階0に差し込む detect_view のスタブ。"""

    def detector(videos, work_dir, *, samples=3, sample_seconds=60.0):
        if calls is not None:
            calls.append((list(videos), samples, sample_seconds))
        return {
            "view": view,
            "confidence": confidence if view else 0.0,
            "detail": {
                "method": "background_optical_flow",
                "radial_ratio": {"front": 0.87, "rear": -0.81}.get(view, 0.05),
                "moving_pairs": 120,
                "reason": "テスト用のスタブ判定",
            },
        }

    return detector


def _run_with_existing_events(session: Path, argv: list[str], detector, calls=None):
    """検出済みCSVを置いた状態でパイプラインを回し、段階の状態を返す。"""
    out_dir = session / "out_auto"
    write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")
    config = build_config(parse_args([str(session), *argv]))
    report = execute_pipeline(config, tracker=dummy_tracker(), view_detector=detector)
    data = report.to_dict()
    return config, data, {stage["name"]: stage for stage in data["stages"]}


def test_view_explicit_option_skips_auto_detection():
    """--view の明示指定が常に優先し、自動判定は走らせない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        calls: list = []
        config, data, stages = _run_with_existing_events(
            session, ["--skip-gps", "--view", "rear"],
            _stub_view_detector("front", calls=calls),
        )
        assert config.view == "rear" and config.view_source == "cli", config
        assert stages["view"]["status"] == "ok", stages["view"]
        assert calls == [], "明示指定なのに自動判定が走った"
        assert data["view"] == "rear", data
        print("  OK: --view の明示指定は自動判定より優先される")


def test_view_metadata_skips_auto_detection():
    """metadata.json に view があれば自動判定しない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value), metadata={"view": "front"})
        calls: list = []
        config, _, stages = _run_with_existing_events(
            session, ["--skip-gps"], _stub_view_detector("rear", calls=calls),
        )
        assert config.view == "front" and config.view_source == "metadata", config
        assert calls == [], "metadata.json の指定があるのに自動判定が走った"
        assert "metadata.json" in stages["view"]["message"], stages["view"]
        print("  OK: metadata.json の view があれば自動判定しない")


def test_view_is_auto_detected_when_unspecified():
    """--view も metadata.json も無ければ自動判定し、根拠をレポートへ残す。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        calls: list = []
        config, data, stages = _run_with_existing_events(
            session, ["--skip-gps"], _stub_view_detector("front", calls=calls),
        )
        assert config.view == "front" and config.view_source == "auto", config
        assert len(calls) == 1 and calls[0][0] == config.videos, calls
        assert stages["view"]["status"] == "ok", stages["view"]
        assert stages["view"]["confidence"] == 0.9, stages["view"]
        # 判定根拠（背景フローの放射成分）がレポートの段階メッセージに残る
        assert "放射成分 +0.87" in stages["view"]["message"], stages["view"]
        assert data["view_source"] == "auto", data
        assert data["info"]["撮影方向の判定根拠"] == "テスト用のスタブ判定", data["info"]
        assert "撮影方向" in report_text(session), "レポート本文に撮影方向が出ていない"
        print("  OK: 撮影方向が未指定なら自動判定して根拠を記録する")


def test_view_undecided_falls_back_to_front_with_manual_action():
    """判定不能なら front を仮定し、警告と人手項目を残す（パイプラインは止めない）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        config, data, stages = _run_with_existing_events(
            session, ["--skip-gps"], _stub_view_detector(None),
        )
        assert config.view == "front" and config.view_source == "fallback", config
        assert stages["view"]["status"] == "warning", stages["view"]
        assert any("自動判定できません" in item for item in data["warnings"]), data["warnings"]
        assert any("--view" in item for item in data["manual_actions"]), data["manual_actions"]
        # 判定不能でも後続段階は動く
        assert stages["review"]["status"] == "ok", stages
        print("  OK: 判定不能時は front を仮定して警告と人手項目を出す")


def test_view_detection_error_does_not_stop_pipeline():
    """自動判定が例外で落ちても、front を仮定して続行する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))

        def broken(videos, work_dir, *, samples=3, sample_seconds=60.0):
            raise run_pipeline.DetectViewError("ffmpeg が見つかりません")

        config, data, stages = _run_with_existing_events(session, ["--skip-gps"], broken)
        assert config.view == "front" and config.view_source == "fallback", config
        assert stages["view"]["status"] == "failed", stages["view"]
        assert stages["final"]["status"] in {"ok", "warning"}, stages
        assert any("ffmpeg" in item for item in data["warnings"]), data["warnings"]
        print("  OK: 自動判定の失敗でパイプラインを止めない")


def test_view_auto_detection_is_skipped_with_skip_detect():
    """--skip-detect なら検出をやり直さないので、GPUを使う自動判定も行わない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        calls: list = []
        config, _, stages = _run_with_existing_events(
            session, ["--skip-gps", "--skip-detect"],
            _stub_view_detector("rear", calls=calls),
        )
        assert calls == [], "--skip-detect なのに自動判定が走った"
        assert stages["view"]["status"] == "skipped", stages["view"]
        assert config.view == "front", config
        print("  OK: --skip-detect 時は自動判定を行わない")


def report_text(session: Path) -> str:
    return (session / "out_auto" / "pipeline_report.txt").read_text(encoding="utf-8")


def test_detection_command_matches_gui():
    """段階1の検出コマンドがGUIの start_detection と同じ形か確認する。"""
    config = PipelineConfig(
        session_dir=Path("S"), out_dir=Path("O"),
        videos=[Path("a.MP4"), Path("b.MP4")], pcap_path=Path("p.pcap"), view="front",
    )
    cmd = run_pipeline.detection_command(config)
    assert cmd[1].endswith("detect_overtaking.py"), cmd
    assert cmd[2:4] == ["a.MP4", "b.MP4"], cmd
    for option, value in (("--view", "front"), ("--out", "O"), ("--imgsz", "640")):
        assert cmd[cmd.index(option) + 1] == value, cmd
    assert "--clip" in cmd, cmd
    print("  OK: 検出コマンドの組み立て")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for test in tests:
        test()
    print("[test] 全テスト成功")
