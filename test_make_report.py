#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_report.py のテスト。

合成した最小の出力フォルダ（レビューCSV + 点群CSV + edit CSV + ダミークリップ）で
report.html と report_data/event_*.js の生成を検証する。実データ（メイン側の
``output/ID1``）が見つかる環境では、その reviewed CSV と点群CSV を一時フォルダへ
写して読み込めることも確かめる（**実データのフォルダには一切書き込まない**）。

    portable\\python\\python.exe test_make_report.py

実データの場所は自動で探す（このファイルから上のフォルダの ``output`` 配下）。
別のフォルダを使うときは環境変数 ``REPORT_REAL_OUT`` に出力フォルダを指定する。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import make_report
from make_report import (
    ReportError,
    _num,
    _sort_key,
    event_js_text,
    generate_report,
    load_point_frames,
    main,
)


# ── 合成データ ───────────────────────────────────────────────────────────────

REVIEWED_HEADER = (
    "event_id,class,detector_class,side,t_start_s,peak_t_s,t_end_s,duration_s,"
    "kei_plate_score,kei_plate_hits,source_video,local_t_start_s,local_t_end_s,"
    "class_reviewed,review_status,danger_level,review_reason,"
    "veloview_t_start,veloview_peak_t,veloview_t_end\n"
)

# 追い越し車の軌跡（1秒刻み）。test_pipeline.py と同じ形で、
# フェーズ2 = t=3 / フェーズ3 = t=4（|Y|<3）/ フェーズ4 = t=5。
TRAJECTORY = [
    (0.0, -0.5, 8.0),
    (1.0, -1.0, 7.0),
    (2.0, -1.5, 6.0),
    (3.0, -2.0, 5.0),
    (4.0, -2.5, 2.5),
    (5.0, -1.0, 3.2),
]


def write_reviewed(out_dir: Path, rows: list[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reviewed_overtaking_events.csv"
    path.write_text(REVIEWED_HEADER + "".join(rows), encoding="utf-8-sig")
    return path


def reviewed_row(event_id: str, status: str = "採用", klass: str = "普通",
                 side: str = "left", reason: str = "") -> str:
    return (f"{event_id},{klass},car,{side},10.0,12.0,18.0,8.0,0.1,0,GX010001.MP4,"
            f"10.0,18.0,False,{status},0,{reason},"
            "00:00:10.000,00:00:12.000,00:00:18.000\n")


def write_lidar_csv(out_dir: Path, event_id: str, frames: int = 4,
                    points_per_frame: int = 500) -> Path:
    """点群CSVを作る。1フレームに ``points_per_frame`` 点を横一列に並べる。"""
    lidar = out_dir / "lidar"
    lidar.mkdir(parents=True, exist_ok=True)
    path = lidar / f"overtaking_{event_id}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Frame", "Time", "Laser ID", "Intensity", "Distance",
                         "Azimuth", "Elevation", "Selected",
                         "Points:0", "Points:1", "Points:2"])
        for frame in range(frames):
            time = 100.0 + frame * 0.1
            for index in range(points_per_frame):
                x = 1.5 + index * 0.001
                y = -8.0 + frame * 3.0 + index * 0.002
                z = -1.5 + (index % 20) * 0.05
                writer.writerow([900 + frame, f"{time:.6f}", index % 16, index % 200,
                                 10.0, 90.0, -1.0, 1,
                                 f"{x:.4f}", f"{y:.4f}", f"{z:.4f}"])
    return path


def write_edit_csv(out_dir: Path, event_id: str) -> Path:
    edit_dir = out_dir / "lidar_edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    path = edit_dir / f"overtaking_{event_id}_edit.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["unixtime", "X", "Y", "Z", "distance(m)", "speed(km/h)"])
        for i, (t, x, y) in enumerate(TRAJECTORY):
            distance = (x * x + y * y) ** 0.5
            speed = "" if i == 0 else "20.0"
            writer.writerow([f"{t:.6f}", f"{x:.4f}", f"{y:.4f}", "-1.2000",
                             f"{distance:.6f}", speed])
    return path


def write_clip(out_dir: Path, event_id: str, side: str = "left") -> Path:
    clips = out_dir / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    path = clips / f"event_{event_id}_{side}.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")   # 中身は使わないダミー
    return path


def make_output(base: Path) -> Path:
    """「クリップあり + 点群あり」「点群だけ」「どちらも無い」の3件を作る。"""
    out_dir = base / "out"
    write_reviewed(out_dir, [
        reviewed_row("001"),
        reviewed_row("002", status="未確認", reason="追跡の確信度が低い（0.30）"),
        reviewed_row("003"),
    ])
    write_lidar_csv(out_dir, "001")
    write_edit_csv(out_dir, "001")
    write_clip(out_dir, "001")
    write_lidar_csv(out_dir, "002", frames=3, points_per_frame=120)
    write_edit_csv(out_dir, "002")
    return out_dir


def read_report(out_dir: Path) -> str:
    return (out_dir / "report.html").read_text(encoding="utf-8")


def read_event_js(out_dir: Path, event_id: str) -> dict:
    """``event_<id>.js`` を代入文と見て中身のJSONを取り出す。"""
    text = (out_dir / "report_data" / f"event_{event_id}.js").read_text(encoding="utf-8")
    match = re.match(
        r'window\.__EVT=window\.__EVT\|\|\{\};window\.__EVT\["([^"]+)"\]=(.*);\n\Z',
        text, re.S,
    )
    assert match, f"代入文の形になっていない: {text[:120]}"
    assert match.group(1) == event_id, match.group(1)
    return json.loads(match.group(2))


def card_html(html: str, event_id: str) -> str:
    start = html.index(f'data-id="{event_id}"')
    head = html.rindex("<section", 0, start)
    return html[head:html.index("</section>", head)]


# ── テスト ────────────────────────────────────────────────────────────────────

def test_report_is_generated_from_a_minimal_output():
    """report.html と event js が揃い、必須の要素が入っているか。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = make_output(Path(temp_value))
        result = generate_report(out_dir)

        assert (out_dir / "report.html").is_file(), result
        assert result["events"] == 3 and result["accepted"] == 2, result
        assert result["unreviewed"] == 1 and result["clips"] == 1, result
        assert result["data_files"] == 2, result

        html = read_report(out_dir)
        # ダブルクリックで開ける単体HTML: 文字コードとインライン化
        assert html.startswith("<!DOCTYPE html>"), html[:40]
        assert '<meta charset="utf-8">' in html
        assert "http://" not in html and "https://" not in html, "外部URLを参照している"
        assert 'src="report_data/' not in html, "点群jsを静的に読み込んでいる（遅延ロードのはず）"
        assert "fetch(" not in html and "XMLHttpRequest" not in html, "file:// で動かない"
        # ヘッダ: 件数
        assert "全 3 件" in html and "採用 2 件" in html and "未確認 1 件" in html, html[:2000]
        # イベントカード: ID・レビュー結果・車種・理由
        for event_id in ("001", "002", "003"):
            assert f'data-id="{event_id}"' in html, event_id
        assert 'data-status="未確認"' in html
        assert "追跡の確信度が低い（0.30）" in html
        # 主要数値（合成軌跡から: LC 1.00m / 通過時間 1.00秒）
        card = card_html(html, "001")
        assert "離隔距離 LC" in card and "1.00 m" in card, card
        assert "通過時間" in card and "TTC" in card and "相対速度" in card, card
        # 動画は相対パス参照（コピーしない）
        assert '<video controls preload="none" src="clips/event_001_left.mp4">' in card
        assert not (out_dir / "report_data" / "event_001_left.mp4").exists()
        # 点群ビューの部品
        assert "canvas" in html and "上面図" in html and "側面図" in html
        assert "通過区間 |Y|<3m" in html and "自転車(原点)" in html
        assert "▶ 再生" in html and 'type = "range"' in html
        print("  OK: 最小の出力フォルダから report.html と event js を生成")


def test_event_js_holds_frames_and_trajectory():
    """点群jsがフレーム単位のXYZ+Intensityと軌跡・LCを持つか。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = make_output(Path(temp_value))
        generate_report(out_dir)
        data = read_event_js(out_dir, "001")
        assert len(data["frames"]) == 4, data["frames"]
        for frame in data["frames"]:
            assert len(frame["p"]) == 3 * len(frame["i"]), "XYZ と Intensity の数が合わない"
            assert len(frame["i"]) == 500
        assert data["rawPoints"] == 2000 and data["points"] == 2000, data
        assert abs(data["lc"] - 1.0) < 1e-6, data["lc"]
        assert len(data["traj"]) == len(TRAJECTORY), data["traj"]
        phases = [p["ph"] for p in data["traj"] if p["ph"]]
        assert any("フェーズ3" in p for p in phases), phases
        assert any("フェーズ4" in p for p in phases), phases
        for key in ("x", "y", "z"):
            assert len(data["bounds"][key]) == 2, data["bounds"]
        print("  OK: 点群jsのフレーム構造・軌跡・フェーズ・LC")


def test_points_are_thinned_per_frame():
    """1フレームあたりの点数上限で間引く。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("001")])
        write_lidar_csv(out_dir, "001", frames=3, points_per_frame=5000)
        generate_report(out_dir, max_points_per_frame=400, max_points_total=10 ** 7)
        data = read_event_js(out_dir, "001")
        assert data["perFrame"] == 400, data["perFrame"]
        assert data["rawPoints"] == 15000, data["rawPoints"]
        for frame in data["frames"]:
            assert len(frame["i"]) == 400, len(frame["i"])
        assert data["points"] == 1200, data["points"]
        print("  OK: 1フレームあたりの点数上限で間引く")


def test_total_budget_lowers_the_per_frame_limit():
    """総点数の予算がフレーム数で割り切れない場合は1フレームの上限を下げる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("001")])
        write_lidar_csv(out_dir, "001", frames=10, points_per_frame=3000)
        generate_report(out_dir, max_points_per_frame=2000, max_points_total=6000)
        data = read_event_js(out_dir, "001")
        assert data["perFrame"] == 600, data["perFrame"]
        assert data["points"] == 6000, data["points"]
        size = (out_dir / "report_data" / "event_001.js").stat().st_size
        assert size < 2 * 1024 * 1024, size
        # 座標は小数2桁に丸める
        assert re.search(r"\d\.\d{3,}", str(data["frames"][0]["p"])) is None, data["frames"][0]["p"][:20]
        print("  OK: 総点数の予算で1フレームの上限を下げる")


def test_missing_clip_is_shown_as_unavailable():
    """クリップが無いイベントは「クリップなし」と出し、点群だけで成立させる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("001")])
        write_lidar_csv(out_dir, "001")
        result = generate_report(out_dir)
        assert result["clips"] == 0, result
        card = card_html(read_report(out_dir), "001")
        assert "クリップなし" in card and "<video" not in card, card
        assert 'data-js="report_data/event_001.js"' in card, card
        print("  OK: クリップが無くても点群だけでレポートが成立する")


def test_event_without_point_cloud_has_no_data_file():
    """点群CSVが無いイベントは js を作らず、カードにも読み込み先を書かない。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("007")])
        write_clip(out_dir, "007")
        result = generate_report(out_dir)
        assert result["data_files"] == 0, result
        assert not (out_dir / "report_data" / "event_007.js").exists()
        assert 'data-id="007" data-status="採用" data-js=""' in read_report(out_dir)
        print("  OK: 点群の無いイベントは js を作らない")


def test_stale_event_js_is_removed():
    """前回の実行で作った不要な js は消す（古い点群が残らないように）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = make_output(Path(temp_value))
        generate_report(out_dir)
        stale = out_dir / "report_data" / "event_999.js"
        stale.write_text("window.__EVT={};\n", encoding="utf-8")
        generate_report(out_dir)
        assert not stale.exists(), "古い event js が残っている"
        assert (out_dir / "report_data" / "event_001.js").is_file()
        print("  OK: 不要になった event js を削除する")


def test_lidar_only_rows_are_included_and_sorted_last():
    """LiDAR発の未確認行（L###）も出し、動画由来の候補の後ろに並べる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [
            reviewed_row("L001", status="未確認", reason="動画側に対応する検出候補がありません"),
            reviewed_row("010"),
            reviewed_row("002"),
        ])
        write_lidar_csv(out_dir, "L001")
        generate_report(out_dir)
        html = read_report(out_dir)
        order = re.findall(r'data-id="([^"]+)"', html)
        assert order == ["002", "010", "L001"], order
        assert (out_dir / "report_data" / "event_L001.js").is_file()
        assert _sort_key("L001") > _sort_key("039"), "L### が先に来ている"
        print("  OK: LiDAR発の行を含め、L### を末尾に並べる")


def test_metrics_failure_is_shown_as_a_warning():
    """フェーズ判定に失敗しても、その旨を出してレポート全体は生成する。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("001")])
        write_lidar_csv(out_dir, "001")
        edit = out_dir / "lidar_edit" / "overtaking_001_edit.csv"
        edit.parent.mkdir(parents=True, exist_ok=True)
        # |Y| < 3 の点が無いのでフェーズ3が作れない
        edit.write_text(
            "unixtime,X,Y,Z,distance(m),speed(km/h)\n"
            "0.0,1.0,20.0,-1.0,20.02,\n1.0,1.0,18.0,-1.0,18.03,20.0\n",
            encoding="utf-8-sig",
        )
        result = generate_report(out_dir)
        assert result["events"] == 1, result
        card = card_html(read_report(out_dir), "001")
        assert "点群の解析に失敗しました" in card and "フェーズ3" in card, card
        assert "—" in card, "解析できない指標が空欄になっていない"
        print("  OK: フェーズ判定の失敗を警告として出す")


def test_missing_events_csv_is_reported():
    """レビューCSVも検出CSVも無ければ ReportError にする。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        out_dir.mkdir()
        try:
            generate_report(out_dir)
        except ReportError as exc:
            assert "reviewed_overtaking_events.csv" in str(exc), exc
        else:
            raise AssertionError("CSVが無くてもエラーにならなかった")
        print("  OK: 候補CSVが無い出力フォルダを弾く")


def test_detection_csv_is_used_when_review_is_missing():
    """レビュー前（overtaking_events.csv だけ）でもレポートを作れる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "overtaking_events.csv").write_text(
            "event_id,class,detector_class,side,t_start_s,peak_t_s,t_end_s,duration_s,"
            "kei_plate_score,kei_plate_hits,source_video,local_t_start_s,local_t_end_s\n"
            "001,普通,car,left,10.0,12.0,18.0,8.0,0.1,0,GX010001.MP4,10.0,18.0\n",
            encoding="utf-8-sig",
        )
        result = generate_report(out_dir)
        assert result["events"] == 1 and result["unreviewed"] == 1, result
        print("  OK: レビュー前の検出CSVからもレポートを作れる")


def test_session_info_overrides_the_pipeline_json():
    """ヘッダ情報は呼び出し側の指定を優先する（段階7bは json を書く前に走るため）。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        write_reviewed(out_dir, [reviewed_row("001")])
        (out_dir / "pipeline_report.json").write_text(json.dumps({
            "session": "C:/old/260701/ID9", "subject_id": "ID9", "view": "rear",
            "started_at": "2000-01-01T00:00:00",
        }, ensure_ascii=False), encoding="utf-8")

        # 指定なし: pipeline_report.json（前回実行分）を読む
        generate_report(out_dir)
        html = read_report(out_dir)
        assert "セッション: ID9" in html and "撮影方向: rear" in html, html[:900]

        # 指定あり: 実行中の値で上書きし、空の値は無視する
        generate_report(out_dir, session_info={
            "session": "ID1", "subject_id": "", "view": "front",
            "collection_date": "260701", "started_at": "2026-09-01T10:00:00",
        })
        html = read_report(out_dir)
        header = html[:html.index("</header>")]
        assert "セッション: ID1" in header and "撮影方向: front" in header, header
        assert "収集日: 260701" in header and "2026-09-01T10:00:00" in header, header
        assert "被験者: ID9" in header, "空の指定で上書きしてしまっている"
        print("  OK: ヘッダ情報は呼び出し側の指定を優先する")


def test_number_formatting_is_compact():
    """座標の文字列は小数2桁で末尾の0を落とす（jsのサイズを詰めるため）。"""
    assert _num(3.5397) == "3.54"
    assert _num(3.50) == "3.5"
    assert _num(4.0) == "4"
    assert _num(-20.6649) == "-20.66"
    assert _num(-0.001) == "-0"
    assert _num(95.134666, 3) == "95.135"
    print("  OK: 数値の短縮表記")


def test_frames_keep_csv_order():
    """フレームは Frame 列でグループ化し、CSVの並び順を保つ。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "out"
        path = write_lidar_csv(out_dir, "001", frames=5, points_per_frame=10)
        cloud = load_point_frames(path)
        assert [f["f"] for f in cloud["frames"]] == ["900", "901", "902", "903", "904"]
        assert [round(f["t"], 2) for f in cloud["frames"]] == [100.0, 100.1, 100.2, 100.3, 100.4]
        text = event_js_text("001", cloud, [], {})
        assert text.startswith("window.__EVT=window.__EVT||{};"), text[:40]
        assert text.isascii(), "js に非ASCII文字が混ざっている（文字コード取り違えの元）"
        print("  OK: Frame 列でのグループ化と js の ASCII 化")


def test_cli_regenerates_the_report():
    """`python make_report.py --out <フォルダ>` で既存出力から作り直せる。"""
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = make_output(Path(temp_value))
        assert main(["--out", str(out_dir), "--max-points-per-frame", "50"]) == 0
        data = read_event_js(out_dir, "001")
        assert all(len(f["i"]) == 50 for f in data["frames"]), data["frames"][0]["i"][:5]
        assert main(["--out", str(out_dir / "no-such-folder")]) == 1
        print("  OK: 単体CLIで既存出力から再生成できる")


# ── 実データ（output/ 配下の実行結果を読むだけ。書き込みは一時フォルダへ）──────

def real_output_dir() -> Path | None:
    """読み取りに使える実データの出力フォルダを1つ返す（無ければ ``None``）。

    セッションごとに `output/<名前>` や `output/<まとめ>/<名前>` へ出すので、
    特定のIDを決め打ちせず `output` の下から reviewed CSV のあるフォルダを探す。
    候補が複数あるときは名前順で最初のものを使う（どれでも読めれば十分）。
    """
    value = os.environ.get("REPORT_REAL_OUT", "")
    if value:
        path = Path(value)
        return path if path.is_dir() else None
    here = Path(__file__).resolve().parent
    for base in [here, *here.parents]:
        root = base / "output"
        if not root.is_dir():
            continue
        found = sorted(root.glob("*/reviewed_overtaking_events.csv"))             + sorted(root.glob("*/*/reviewed_overtaking_events.csv"))
        if found:
            return found[0].parent
    return None


def test_real_output_folder_is_readable():
    """実データの reviewed CSV と点群CSV を読んでレポートを作れるか。

    実データのフォルダには書き込まない。必要なCSVだけ一時フォルダへ写して生成する。
    """
    source = real_output_dir()
    if source is None:
        print("  SKIP: 実データの出力フォルダ（output/**/reviewed_overtaking_events.csv）が"
              "見つからないので省略")
        return
    with tempfile.TemporaryDirectory() as temp_value:
        out_dir = Path(temp_value) / "real"
        (out_dir / "lidar").mkdir(parents=True)
        (out_dir / "lidar_edit").mkdir(parents=True)
        shutil.copy2(source / "reviewed_overtaking_events.csv", out_dir)
        if (source / "pipeline_report.json").is_file():
            shutil.copy2(source / "pipeline_report.json", out_dir)
        # 点群は大きいのでレビュー行に対応するものを2件だけ写す
        # （クリップは1本60MB前後あるので写さない）
        with (out_dir / "reviewed_overtaking_events.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            wanted = {str(row.get("event_id", "")) for row in csv.DictReader(stream)}
        clouds = [p for p in sorted((source / "lidar").glob("overtaking_*.csv"))
                  if not p.stem.endswith("_edit")
                  and p.stem[len("overtaking_"):] in wanted][:2]
        assert len(clouds) == 2, f"レビュー行に対応する点群CSVが足りません: {source / 'lidar'}"
        for cloud in clouds:
            shutil.copy2(cloud, out_dir / "lidar" / cloud.name)
            edit = source / "lidar_edit" / f"{cloud.stem}_edit.csv"
            if edit.is_file():
                shutil.copy2(edit, out_dir / "lidar_edit" / edit.name)

        result = generate_report(out_dir)
        assert result["events"] >= len(clouds), result
        assert result["data_files"] == len(clouds), result
        assert result["clips"] == 0, "クリップを写していないのに検出された"
        html = read_report(out_dir)
        assert "自転車追い越し解析レポート" in html
        for cloud in clouds:
            event_id = cloud.stem[len("overtaking_"):]
            data = read_event_js(out_dir, event_id)
            assert data["frames"], event_id
            assert data["rawPoints"] > data["points"] or data["points"] == data["rawPoints"]
            for frame in data["frames"]:
                assert len(frame["i"]) <= make_report.MAX_POINTS_PER_FRAME, len(frame["i"])
                assert len(frame["p"]) == 3 * len(frame["i"])
            size = (out_dir / "report_data" / f"event_{event_id}.js").stat().st_size
            assert size < 2 * 1024 * 1024, f"{event_id}: {size} バイト（2MB超）"
        print(f"  OK: 実データ {source.name} の {len(clouds)} 件を読んでレポート生成")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"[test] {len(tests)} 件実行")
    for test in tests:
        test()
    print("[test] 全テスト成功")
