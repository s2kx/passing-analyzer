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


# 追い越し車の軌跡（1秒刻み）。後方から近づいて横に並び、前方へ抜けていく。
#   フェーズ2 = 4行目(t=3), フェーズ3 = 5行目(t=4, |Y|<3), フェーズ4 = 6行目(t=5)
# 通過区間（|Y| < 3m）を後ろから前へ抜けきる形にしてある。抜けきらない軌跡だと
# 距離p3が MIN_PASS_PROGRESS を下回り、自動レビューが「未確認」にする。
TRAJECTORY = [
    (0.0, -0.5, -8.0),
    (1.0, -1.0, -7.0),
    (2.0, -1.5, -6.0),
    (3.0, -2.0, -5.0),
    (4.0, -2.5, -2.0),
    (5.0, -1.0, 3.5),
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
        score: float = 0.9, confidence: float = 0.9,
        length: float = 4.4, width: float = 1.8, height: float = 1.5) -> dict:
    """``scripted_tracker`` へ渡す 1 台分の車の定義。

    寸法の既定は普通車（4.4m × 1.8m × 1.5m）。段階6aの車種判定で使う。
    """
    return {"track_key": track_key, "pass_time": pass_time, "score": score,
            "lc_estimate": lc_estimate, "median_x": lc_estimate,
            "confidence": confidence, "length": length, "width": width,
            "height": height, "near_length": length, "points": 5000}


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


def stub_sync_checker(estimated: float, peak: float = 0.9, prominence: float = 0.4,
                      spread: float = 0.2, calls: list | None = None):
    """段階3.5 のダミー ``sync_check.check_session``。"""

    def check_session(out_dir, session_dir=None, **options):
        if calls is not None:
            calls.append({"out_dir": Path(out_dir), "options": options})
        return {"status": "ok", "estimated_offset": estimated,
                "correlation_peak": peak, "correlation_prominence": prominence,
                "segment_spread_s": spread, "difference": None}

    return check_session


def test_sync_check_reports_a_large_offset_mismatch():
    """自己検証の推定オフセットが大きくずれていたら警告と人手項目を出す。

    段階3のオフセットは GoPro の GPMF 時刻と PCAP の記録時刻の引き算なので、
    LiDAR 収録PCの時計がずれていると丸ごとずれる（260701 では全5セッションで
    +4.2〜5.2 秒ずれていた）。既定では差し替えずに人へ知らせる。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        calls: list = []
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(
            config, tracker=scripted_tracker([car("f10x+1.2", 1.0)]),
            sync_checker=stub_sync_checker(4.4, calls=calls),
        )
        data = report.to_dict()
        stage = next(s for s in data["stages"] if s["name"] == "synccheck")

        assert stage["status"] == "warning", stage
        assert stage["estimated_offset"] == 4.4 and stage["trusted"] is True, stage
        assert not stage.get("applied"), stage
        assert any("--auto-offset" in item for item in data["manual_actions"]), \
            data["manual_actions"]
        assert data["info"]["時刻オフセット(秒)"] == 0.0, data["info"]
        # 索引済みの PCAP を渡して再索引を避けている
        assert calls and "pcap" in calls[0]["options"], calls
        print("  OK: 同期の自己検証で大きなずれを警告する")


def test_auto_offset_replaces_the_offset():
    """--auto-offset なら、信用できる推定オフセットへその場で差し替える。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps",
                                          "--auto-offset"]))
        report = execute_pipeline(
            config, tracker=scripted_tracker([car("f10x+1.2", 4.4)]),
            sync_checker=stub_sync_checker(4.4),
        )
        data = report.to_dict()
        stage = next(s for s in data["stages"] if s["name"] == "synccheck")

        assert stage["status"] == "warning" and stage["applied"] is True, stage
        assert data["info"]["時刻オフセット(秒)"] == 4.4, data["info"]
        sync = json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))
        assert (sync["offset"], sync["source"]) == (4.4, "sync_check"), sync
        assert sync["gpmf_offset"] == 0.0, sync
        print("  OK: --auto-offset で推定オフセットへ差し替える")


def test_sync_check_ignores_an_unreliable_estimate():
    """相関が弱い推定は信用せず、オフセットも差し替えない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps",
                                          "--auto-offset"]))
        report = execute_pipeline(
            config, tracker=scripted_tracker([car("f10x+1.2", 1.0)]),
            sync_checker=stub_sync_checker(4.4, peak=0.3),
        )
        data = report.to_dict()
        stage = next(s for s in data["stages"] if s["name"] == "synccheck")

        assert stage["trusted"] is False and not stage.get("applied"), stage
        assert data["info"]["時刻オフセット(秒)"] == 0.0, data["info"]
        print("  OK: 信頼度の低い推定オフセットは採用しない")


def test_skip_sync_check_skips_the_stage():
    """--skip-sync-check と --offset の手動指定では自己検証を行わない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        calls: list = []
        for extra in (["--skip-sync-check"], ["--offset", "0"]):
            config = build_config(parse_args(
                [str(session), "--skip-detect", "--skip-gps", *extra]
            ))
            report = execute_pipeline(
                config, tracker=scripted_tracker([car("f10x+1.2", 1.0)]),
                sync_checker=stub_sync_checker(4.4, calls=calls),
            )
            stage = next(s for s in report.to_dict()["stages"] if s["name"] == "synccheck")
            assert stage["status"] == "skipped", (extra, stage)
        assert calls == [], calls
        print("  OK: --skip-sync-check / --offset 指定時は自己検証を省く")


def test_non_advancing_analysis_is_flagged_for_review():
    """フェーズ3→4で車が前進していない解析は採用せず人手確認へ回す。

    通過区間を抜けるまで追えず、代用のフェーズ4が後方の点になると距離p3・
    相対速度・TTC がすべて負になる。最終Excelへ「TTC が負の行」を入れないよう
    「未確認」にする（実データ260701/ID1の281.9s、ID3の869.1sがこの型）。
    """
    config = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                            pcap_path=Path("."), view="front")
    events = [{"event_id": "1"}, {"event_id": "2"}]
    tracking = {"1": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2},
                "2": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
    analyses = {
        "1": {"metrics": {"lc": 1.27, "relative_speed": -0.32, "ttc": -40.31}},
        "2": {"metrics": {"lc": 1.30, "relative_speed": 15.6, "ttc": 1.2}},
    }
    reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses)
    assert [row["review_status"] for row in reviewed] == ["未確認", "採用"], reviewed
    assert "前進していません" in reviewed[0]["review_reason"], reviewed[0]
    print("  OK: 車が前進していない解析を「未確認」にする")


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


def test_same_lane_vehicle_wins_over_a_closer_adjacent_lane_one():
    """時刻がより近くても、隣車線の車より同一車線の車を先に割り当てる。

    追い越しの窓には隣車線を走る車も入る。時刻だけで選ぶと期待時刻が僅かに
    ずれただけで隣車線の車を掴み、そのイベント本来の車が余ってしまう。
    """
    candidates = {
        "001": [car("f10x+3.2", 1.0, lc_estimate=3.2),      # 隣車線・期待時刻ちょうど
                car("f24x+1.1", 2.4, lc_estimate=1.1)],     # 同一車線・1.4秒ずれ
    }
    assignment = run_pipeline.assign_events({"001": 1.0}, candidates)
    assert assignment["001"]["track_key"] == "f24x+1.1", assignment
    assert assignment["001"]["delta"] == 1.4, assignment

    # 同一車線の候補が無ければ、隣車線の車も従来どおり割り当てる
    only_far = {"001": [car("f10x+3.2", 1.0, lc_estimate=3.2)]}
    assert run_pipeline.assign_events({"001": 1.0}, only_far)["001"]["track_key"] == "f10x+3.2"

    # 同一車線どうしなら従来どおり時刻の近い方が勝つ
    both_near = {"001": [car("f10x+1.2", 1.0, lc_estimate=1.2),
                         car("f24x+1.1", 2.4, lc_estimate=1.1)]}
    assert run_pipeline.assign_events({"001": 1.0}, both_near)["001"]["track_key"] == "f10x+1.2"
    print("  OK: 隣車線の車より同一車線の車を優先して割り当て")


def test_same_lane_priority_frees_the_vehicle_for_the_next_event():
    """隣車線の車を掴まなくなることで、後続イベントが本来の車を拾えるようになる。

    実データ(260701/ID3)で起きていた取り違えの型。1台目のイベントが隣車線の車を
    掴むと、そのイベント本来の車がどの候補にも割り当たらず捨てられていた。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5)
                         + event_row("002", 3.5, 4.0, 4.5))

        # 001 の期待通過時刻(1.0s)ちょうどに隣車線の車がいる。時刻だけで選ぶと 001 は
        # そちらを掴み、本来の車(2.0s)はどの候補にも割り当たらず捨てられていた。
        cars = [car("f10x+3.2", 1.0, lc_estimate=3.2, score=0.95),   # 隣車線
                car("f20x+1.1", 2.0, lc_estimate=1.1, score=0.90),   # 001 の本来の車
                car("f40x+1.0", 4.0, lc_estimate=1.0, score=0.90)]   # 002 の本来の車
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps",
                                          "--no-lidar-extra"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        track = next(s for s in report.to_dict()["stages"] if s["name"] == "autotrack")

        assert track["assigned"] == {"001": "f20x+1.1", "002": "f40x+1.0"}, track["assigned"]
        print("  OK: 隣車線の車を避けたことで両候補が本来の車を拾う")


def test_retry_exclusion_keeps_the_target_track():
    """選び直しの除外キーに、狙ったトラックと同一視されるキーを混ぜない。

    追跡側は追跡窓のぶれを吸収するため track_key を完全一致ではなく近さで照合する。
    通過フレームが数フレームしか違わない別候補を除外キーに入れると、狙った
    トラックまで除外されて「候補はすべて割り当て済み」になり車を失う
    （実データ260701/ID2 の 674.1s がこれで消えていた）。
    """
    candidates = [{"track_key": "f6662x+2.3"}, {"track_key": "f6671x+1.7"},
                  {"track_key": "f6717x+1.4"}, {"track_key": "f6720x+2.0"}]
    excluded = run_pipeline.keys_before(candidates, "f6720x+2.0")
    assert excluded == ["f6662x+2.3", "f6671x+1.7"], excluded
    # 採用順で後ろにあるキーや、一覧に無いキーは従来どおり何も除外しない
    assert run_pipeline.keys_before(candidates, "f6662x+2.3") == []
    assert run_pipeline.keys_before(candidates, "f9999x+9.9") == []
    print("  OK: 選び直しの除外キーから狙ったトラック自身を外す")


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


def test_vehicle_class_comes_from_the_lidar_size():
    """車種は点群で測った寸法（側面積・軽規格）で決める。"""
    classify = run_pipeline.lidar_vehicle_class
    # 大型: 側面積（車体長 × 車高）が 9m^2 以上
    assert classify({"length": 6.5, "width": 2.5, "height": 2.6}) == "大型"
    # 軽: 軽自動車の規格（3.40m × 1.48m）に点群のふくらみを足した範囲に収まる
    assert classify({"length": 3.3, "width": 1.65, "height": 1.6}) == "軽"
    # 普通: どちらでもない
    assert classify({"length": 4.4, "width": 1.8, "height": 1.5}) == "普通"
    # 規格より小さすぎる測定値は「車の全体を測れていない」ので軽とは呼ばない
    assert classify({"length": 1.7, "width": 0.7, "height": 1.6}) == "普通"
    # 寸法が無い（旧プラグイン・追跡失敗）なら判定しない
    assert classify({"length": None, "width": None, "height": None}) == ""
    assert classify({}) == ""
    print("  OK: 点群寸法から 大型 / 軽 / 普通 を判定")


def test_vehicle_class_is_rewritten_from_the_assigned_track():
    """動画側の車種を、割り当てた車の点群寸法による判定へ差し替える。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        # 候補CSVの車種は「普通」（event_row の既定）
        write_events_csv(out_dir, event_row("001", 0.5, 1.0, 1.5))

        cars = [car("f10x+1.8", 1.0, lc_estimate=1.1,
                    length=3.3, width=1.65, height=1.6)]
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        report = execute_pipeline(config, tracker=scripted_tracker(cars))
        assert report.to_dict()["info"].get("点群の寸法で直した車種") == 1, report.to_dict()["info"]
        assert [row["class"] for row in read_reviewed(out_dir)] == ["軽"]
        print("  OK: 動画の車種を点群寸法の判定へ差し替え（普通→軽）")


def test_vehicle_class_keeps_a_reviewed_value():
    """人がレビューで直した車種（class_reviewed）は点群判定で上書きしない。"""
    events = [{"event_id": "001", "class": "大型", "class_reviewed": "True"},
              {"event_id": "002", "class": "大型", "class_reviewed": "False"},
              {"event_id": "003", "class": "", "lidar_extra": True}]
    small = {"ok": True, "length": 3.3, "width": 1.65, "height": 1.6}
    tracking = {"001": small, "002": small, "003": small}
    changes = run_pipeline.refine_vehicle_classes(events, tracking)
    assert [event["class"] for event in events] == ["大型", "軽", ""], events
    assert changes == [("002", "大型", "軽")], changes
    print("  OK: 人が直した車種とLiDAR発の行はそのまま残す")


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


def test_report_html_is_generated_after_the_final_workbook():
    """段階7bで report.html と点群の js を作り、パスをレポートへ残す。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps"]))
        data = execute_pipeline(config, tracker=dummy_tracker()).to_dict()
        stages = {stage["name"]: stage for stage in data["stages"]}

        # 段階7（最終Excel）の後、段階8（レポート）の前に置く
        names = [stage["name"] for stage in data["stages"]]
        assert names.index("final") < names.index("report_html") < names.index("report"), names
        # クリップは合成セッションに無いので warning（動画欄は「クリップなし」）
        assert stages["report_html"]["status"] == "warning", stages["report_html"]
        assert stages["report_html"]["events"] == 1, stages["report_html"]

        html_path = out_dir / "report.html"
        assert html_path.is_file(), html_path
        assert stages["report_html"]["path"] == str(html_path), stages["report_html"]
        assert data["info"]["閲覧用レポート"] == str(html_path), data["info"]
        html = html_path.read_text(encoding="utf-8")
        assert 'data-id="001"' in html and "クリップなし" in html, html[:400]
        # 段階8（pipeline_report.json の保存）より前に走るので、ヘッダの情報は
        # 実行中の config から渡す（前回実行分の json を読ませない）
        header = html[:html.index("</header>")]
        assert "セッション: ID1" in header and "被験者: ID1" in header, header
        assert "撮影方向: front" in header and "解析実行: 20" in header, header
        assert "http://" not in html and "https://" not in html, "外部CDNを参照している"
        js_path = out_dir / "report_data" / "event_001.js"
        assert js_path.is_file(), js_path
        assert js_path.read_text(encoding="utf-8").startswith("window.__EVT="), js_path
        print("  OK: 段階7bで閲覧用HTMLレポートを生成")


def test_no_report_option_skips_the_html():
    """--no-report で段階7bを省く。"""
    with tempfile.TemporaryDirectory() as temp_value:
        session = make_session(Path(temp_value))
        out_dir = session / "out_auto"
        write_events_csv(out_dir, "001,普通,car,right,0.5,2.5,4.5,4.0,0.1,0,GX010001.MP4,0.5,4.5\n")
        config = build_config(parse_args([str(session), "--skip-detect", "--skip-gps",
                                          "--no-report"]))
        assert config.report_html is False, config
        data = execute_pipeline(config, tracker=dummy_tracker()).to_dict()
        stages = {stage["name"]: stage for stage in data["stages"]}
        assert stages["report_html"]["status"] == "skipped", stages["report_html"]
        assert not (out_dir / "report.html").exists()
        assert stages["final"]["status"] == "ok", stages
        print("  OK: --no-report で閲覧用レポートを省略する")


def gps_workbook(path: Path, rows: list[tuple[float, float, float, float]]) -> Path:
    """``extract_telemetry.save_gps_workbook`` と同じ形の xlsx を書く。

    ``rows`` は ``(動画内秒, 緯度, 経度, 速度m/s)``。シート2（間引き無し）だけを
    自車運動の判定に使うので、シート1は見出しだけで足りる。
    """
    from extract_telemetry import _write_simple_xlsx

    headers = ["cts", "date", "GPS (Lat.) [deg]", "GPS (Long.) [deg]", "GPS (Alt.) [m]",
               "GPS (2D speed) [m/s]", "GPS (3D speed) [m/s]", "fix", "precision",
               "altitude system"]
    sheet2 = [headers] + [
        [seconds, "", latitude, longitude, 30.0, speed, speed, 3, 150, ""]
        for seconds, latitude, longitude, speed in rows
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_simple_xlsx(path, [("1", [headers]), ("2", sheet2)])
    return path


def straight_ride(seconds: float, speed: float, count: int = 60,
                  start: float = 0.0) -> list[tuple[float, float, float, float]]:
    """北へ一定速度で直進する GPS 測位列（10Hz）。"""
    rows = []
    for index in range(count):
        elapsed = start + index * 0.1
        rows.append((elapsed, 35.0 + (elapsed - seconds) * speed / 111_320.0,
                     139.0, speed))
    return rows


def test_stopped_ride_is_rejected():
    """自転車が停止している間の候補は「除外」にする。

    信号待ちの横を車が流れていく状況で、追い越しの前提（静止物が自車速度で
    後退する）が崩れている。実データ260701/ID1の 388.4s（GPS 0.33m/s）がこの型で、
    採用した他の19件は最低でも 4.35m/s あった。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        gps_workbook(out_dir / "gps" / "GX010001" / "telemetry_gps.xlsx",
                     straight_ride(0.0, 0.05, count=120))
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, 0.0)
        events = [{"event_id": "001", "source_video": "GX010001.MP4",
                   "t_start_s": 2.0, "t_end_s": 6.0,
                   "local_t_start_s": 2.0, "local_t_end_s": 6.0}]
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}

        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "除外", reviewed
        assert "停止中" in reviewed[0]["review_reason"], reviewed[0]

        # --no-ego-gate を付けたら足切りしない
        config.ego_gate = False
        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "採用", reviewed
        print("  OK: 停止中の候補を除外（--no-ego-gate で無効化）")


def test_moving_ride_is_kept():
    """走行中（GPSが読める）の候補はこれまでどおり採用する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        gps_workbook(out_dir / "gps" / "GX010001" / "telemetry_gps.xlsx",
                     straight_ride(0.0, 5.0, count=120))
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, 0.0)
        assert ego.sources == ["GPS"], ego.sources
        events = [{"event_id": "001", "source_video": "GX010001.MP4",
                   "t_start_s": 2.0, "t_end_s": 6.0,
                   "local_t_start_s": 2.0, "local_t_end_s": 6.0}]
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}
        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "採用", reviewed
        speed, source = ego.speed(events[0])
        assert abs(speed - 5.0) < 0.01 and source == "GPS", (speed, source)
        assert (ego.yaw_rate(events[0]) or 0.0) < 1.0, ego.yaw_rate(events[0])
        print("  OK: 走行中の候補は採用（GPS速度 5.0m/s）")


def test_lidar_speed_series_is_used_without_gps():
    """GPSが無いセッションでは段階3.5の自車速度で停止を判定する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        out_dir.mkdir(parents=True, exist_ok=True)
        # tools/sync_check.save_series と同じCSV。ok=0 の行は捨てられる。
        (out_dir / "sync_series.npz").write_text(
            "time_s,speed_ms,peak,prominence,ok\n"
            "10.0000,0.0300,0.8,0.3,1\n"
            "11.0000,22.0000,0.1,0.0,0\n"
            "12.0000,0.0500,0.8,0.3,1\n",
            encoding="utf-8",
        )
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, -5.0)
        assert ego.sources == ["LiDAR自車速度"], ego.sources
        # pcap時刻 = gopro時刻 + offset なので、gopro 16.0s → pcap 11.0s
        events = [{"event_id": "001", "source_video": "GX010001.MP4",
                   "t_start_s": 16.0, "t_end_s": 16.5,
                   "local_t_start_s": 16.0, "local_t_end_s": 16.5}]
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}
        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "除外", reviewed
        assert "LiDAR自車速度" in reviewed[0]["review_reason"], reviewed[0]
        print("  OK: GPSが無ければLiDAR自車速度で停止を判定（推定失敗行は無視）")


def test_gps_workbook_reader_is_shared_with_the_writer():
    """GPSワークブックの読み取りは書き出し側（extract_telemetry）に一本化されている。

    段階6の自車運動判定（run_pipeline）と段階3.5の自己検証（tools/sync_check）が
    同じ xlsx を別々に解釈すると、片方だけ単位や列を取り違える。読み取りは
    ``extract_telemetry.read_gps_workbook`` 1本にして、絞り込みだけ各側で行う。
    """
    from extract_telemetry import _write_simple_xlsx, read_gps_workbook

    with tempfile.TemporaryDirectory() as temp_value:
        path = Path(temp_value) / "telemetry_gps.xlsx"
        # シート1しか無い（毎秒1行・k/s＝m/sの1/1000）ワークブック
        headers = ["cts", "date", "GPS (Lat.) [deg]", "GPS (Long.) [deg]", "GPS (Alt.) [m]",
                   "GPS (2D speed) [k/s]", "GPS (3D speed) [m/s]", "fix", "precision",
                   "altitude system"]
        _write_simple_xlsx(path, [("1", [
            headers,
            [2.0, "", 35.0, 139.0, 30.0, 0.005, 5.0, 3, 150, ""],
            [1.0, "", 35.1, 139.1, 30.0, 0.004, 4.0, 0, 900, ""],
        ])])
        fixes = read_gps_workbook(path)
        # 時刻順に並び、k/s は m/s へ直り、測位できていない行も残る
        assert [fix.seconds for fix in fixes] == [1.0, 2.0], fixes
        assert abs(fixes[1].speed - 5.0) < 1e-9, fixes[1]
        assert [fix.fix for fix in fixes] == [0, 3], fixes

        # run_pipeline 側は測位できていない行（fix < 2）を捨てる
        track = run_pipeline.read_gps_track(path)
        assert [row[0] for row in track] == [2.0], track
        assert track[0][1:] == (35.0, 139.0, 5.0), track

    assert read_gps_workbook(Path("存在しない.xlsx")) == []
    print("  OK: GPSワークブックの読み取りは書き出し側に一本化")


def test_event_without_local_time_does_not_use_gps():
    """動画内時刻の無い候補ではGPSを使わない（0秒として読むと停止と誤判定する）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        # 走り出す前の停止（0〜4秒）と、その後の走行
        rows = straight_ride(0.0, 0.05, count=40) + straight_ride(4.0, 5.0, count=80, start=4.0)
        gps_workbook(out_dir / "gps" / "GX010001" / "telemetry_gps.xlsx", rows)
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, 0.0)
        event = {"event_id": "001", "source_video": "GX010001.MP4",
                 "t_start_s": 8.0, "t_end_s": 9.0, "local_t_start_s": "", "local_t_end_s": ""}
        assert ego.speed(event) == (None, ""), ego.speed(event)
        assert ego.yaw_rate(event) is None, ego.yaw_rate(event)
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}
        reviewed = run_pipeline.auto_review_events([event], config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "採用", reviewed
        print("  OK: 動画内時刻が無ければGPSを使わない")


def test_turning_ride_is_flagged_for_review():
    """曲がり角（旋回中）の候補は「未確認」にして人手確認へ回す。

    旋回中はセンサのヨーで静止物が横へ流れ、追い越し車と見分けがつかない。
    自車運動補正をしていないので、値は採用せず目視確認へ回す。
    """
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        # 5m/s で半径 10m ほどの角を曲がる（約 29度/秒）
        rows = []
        angle = 0.0
        latitude, longitude = 35.0, 139.0
        for index in range(80):
            elapsed = index * 0.1
            angle += math.radians(29.0) * 0.1
            latitude += 5.0 * 0.1 * math.cos(angle) / 111_320.0
            longitude += 5.0 * 0.1 * math.sin(angle) / (111_320.0 * math.cos(math.radians(35.0)))
            rows.append((elapsed, latitude, longitude, 5.0))
        gps_workbook(out_dir / "gps" / "GX010001" / "telemetry_gps.xlsx", rows)
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, 0.0)
        events = [{"event_id": "001", "source_video": "GX010001.MP4",
                   "t_start_s": 2.0, "t_end_s": 6.0,
                   "local_t_start_s": 2.0, "local_t_end_s": 6.0}]
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}
        yaw = ego.yaw_rate(events[0])
        assert yaw is not None and yaw > 20.0, yaw
        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "未確認", reviewed
        assert "旋回中" in reviewed[0]["review_reason"], reviewed[0]
        print(f"  OK: 旋回中（{yaw:.0f}度/秒）の候補を未確認へ")


def test_adjacent_lane_suspicion_is_flagged_for_review():
    """同一車線と言い切れない離隔距離（2.6〜3.5m）は「未確認」にする。

    260701/ID1 を正解12件と突き合わせると、正しく追えた11件の実測LCは 1.89m 以下、
    正解に対応しない（隣車線の車を掴んだ）7件は 3.10m 以上だった。除外はせず、
    人が点群を見て決められるよう残す。
    """
    config = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                            pcap_path=Path("."), view="front")
    events = [{"event_id": "1"}, {"event_id": "2"}, {"event_id": "3"}]
    tracking = {key: {"ok": True, "confidence": 0.9, "lc_estimate": 1.0}
                for key in ("1", "2", "3")}
    analyses = {
        "1": {"metrics": {"lc": 1.89, "relative_speed": 15.0, "distance_p3": 6.0}},
        "2": {"metrics": {"lc": 3.10, "relative_speed": 15.0, "distance_p3": 6.0}},
        "3": {"metrics": {"lc": 3.60, "relative_speed": 15.0, "distance_p3": 6.0}},
    }
    reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses)
    assert [row["review_status"] for row in reviewed] == ["採用", "未確認", "除外"], reviewed
    assert "同一車線の目安" in reviewed[1]["review_reason"], reviewed[1]
    assert "隣車線" in reviewed[2]["review_reason"], reviewed[2]
    print("  OK: 離隔距離 2.6〜3.5m を未確認、3.5m 超を除外")


def test_truncated_pass_is_flagged_for_review():
    """通過区間を抜けきれていない解析（距離p3が短い）は「未確認」にする。

    追跡が通過区間の途中で切れると通過時間が短く出て、相対速度・TTCがまとめて
    狂う。実データ260701/ID1の 389.8s が距離p3 2.51m でこの型（採用行は 5.27m 以上）。
    """
    config = PipelineConfig(session_dir=Path("."), out_dir=Path("."), videos=[],
                            pcap_path=Path("."), view="front")
    events = [{"event_id": "1"}, {"event_id": "2"}, {"event_id": "3"}]
    tracking = {key: {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}
                for key in ("1", "2", "3")}
    analyses = {
        "1": {"metrics": {"lc": 1.3, "relative_speed": 15.0, "distance_p3": 2.51}},
        "2": {"metrics": {"lc": 1.3, "relative_speed": 15.0, "distance_p3": 6.0}},
        "3": {"metrics": {"lc": 1.3, "relative_speed": 15.0, "distance_p3": 6.0,
                          "warnings": ["フェーズ2が見つからないため、フェーズ3直前の"
                                       "有効点をフェーズ2として代用しました"]}},
    }
    reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses)
    assert [row["review_status"] for row in reviewed] == ["未確認", "採用", "未確認"], reviewed
    assert "抜けきれていません" in reviewed[0]["review_reason"], reviewed[0]
    assert "代用" in reviewed[2]["review_reason"], reviewed[2]
    print("  OK: 通過区間を抜けきれていない・フェーズを代用した解析を未確認へ")


def test_ego_gate_is_skipped_without_any_series():
    """GPSも自車速度も無ければ足切りせず、レポートへ警告だけ残す。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value)
        config = PipelineConfig(session_dir=Path("."), out_dir=out_dir, videos=[],
                                pcap_path=Path("."), view="front")
        ego = run_pipeline.load_ego_motion(config, 0.0)
        assert not ego.available(), ego
        events = [{"event_id": "001", "source_video": "GX010001.MP4",
                   "t_start_s": 2.0, "t_end_s": 6.0,
                   "local_t_start_s": 2.0, "local_t_end_s": 6.0}]
        tracking = {"001": {"ok": True, "confidence": 0.9, "lc_estimate": 1.2}}
        analyses = {"001": {"metrics": {"lc": 1.3, "relative_speed": 15.0,
                                        "distance_p3": 6.0}}}
        reviewed = run_pipeline.auto_review_events(events, config, tracking, analyses, ego)
        assert reviewed[0]["review_status"] == "採用", reviewed
        print("  OK: 時系列が無ければ自車運動の足切りを飛ばす")


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
