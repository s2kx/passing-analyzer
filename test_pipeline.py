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
    """契約どおりのダミー ``track_overtaking``。合成PCAPの唯一の点を囲う。"""

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

        # 段階4: 全候補を採用してレビューCSVを書く
        assert status["review"] == "ok", status
        with (out_dir / "reviewed_overtaking_events.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            reviewed = list(csv.DictReader(stream))
        assert len(reviewed) == 1 and reviewed[0]["review_status"] == "採用", reviewed
        assert reviewed[0]["danger_level"] == "0", reviewed
        assert reviewed[0]["veloview_t_start"] == "00:00:00.500", reviewed

        # 段階5: マージン1.5秒を足したフレーム範囲でダミー追跡を呼び、点群CSVを書く
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
        assert any("自動レビュー" in item for item in data["manual_actions"]), data["manual_actions"]
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
        assert calls == [(2, 4, "left")] or calls == [(3, 5, "left")], calls
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
